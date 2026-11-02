"""Minimal bounded worker loop over WorkspaceAciV2."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import posixpath

from harness.context_manager.models import ContextBundle
from harness.control_plane import WorkContract
from harness.executor.models import (
    ExecutionMessage,
    ExecutionOutcome,
    ExecutionRequest,
    WorkerResult,
)
from harness.router.scoring import DecisionType, RoutingDecision
from harness.workspace import WorkspaceSession
from harness.workspace_aci_v2 import WorkspaceAciV2
from harness.integration.failure_policy import FailureKindV1
from harness.repo_intelligence.models import RepositoryFileKind
from harness.repo_intelligence.scanner import _kind

from .models import (
    SimpleWorkerActionKindV2,
    SimpleWorkerOutcomeV2,
    SimpleWorkerPolicyV2,
    SimpleWorkerResultV2,
)
from .interaction_state import InteractionStateV1
from .prompt import (
    completion_rejected_message,
    initial_messages,
    observation_message,
    protocol_error_message,
    rejected_candidate_unchanged,
)
from .protocol import (
    SimpleWorkerProtocolError,
    parse_simple_worker_action,
    simple_worker_action_json_schema,
)


class SimpleWorkerV2:
    """One fixed model decision, one small action loop, no recovery orchestration."""

    def __init__(
        self,
        *,
        executor,
        aci: WorkspaceAciV2 | None = None,
        policy: SimpleWorkerPolicyV2 | None = None,
        observer=None,
        compact_edit_contract: bool = True,
    ) -> None:
        if executor is None or not callable(getattr(executor, "execute", None)):
            raise TypeError("executor must expose execute(request)")
        self.executor = executor
        self.aci = aci or WorkspaceAciV2()
        self.policy = policy or SimpleWorkerPolicyV2()
        self.observer = observer
        if type(compact_edit_contract) is not bool:
            raise TypeError("compact_edit_contract must be bool")
        self.compact_edit_contract = compact_edit_contract

    @staticmethod
    def _compact_creation_correction(path: str, viewed_files: dict[str, str], was_read: bool) -> dict:
        fields = (["file", "lines", "replacement"] if path in viewed_files
                  else ["file", "old_text", "replacement"] if was_read else ["path"])
        return {"error_code": "EXACT_REPLACEMENT_REQUIRED", "path": path,
                "rejected_edit_shape": "CREATE", "required_action": "EDIT" if was_read else "VIEW",
                "required_fields": fields, "workspace_mutated": False,
                "reset_condition": "explicit non-creation EDIT for this path"}

    @staticmethod
    def _requires_mutation(contract: WorkContract) -> bool:
        value = contract.constraints.get("REQUIRES_WORKSPACE_MUTATION", False)
        if not isinstance(value, bool):
            raise ValueError(
                "REQUIRES_WORKSPACE_MUTATION must be a bool when present"
            )
        return value

    @staticmethod
    def _result(
        outcome: SimpleWorkerOutcomeV2,
        *,
        summary: str | None,
        model_turns: int,
        aci_calls: int,
        changed_paths: list[str],
        model_id: str | None,
        reason: str | None = None,
        failure_kind: FailureKindV1 | None = None,
    ) -> SimpleWorkerResultV2:
        return SimpleWorkerResultV2(
            outcome=outcome,
            final_summary=summary,
            model_turns=model_turns,
            aci_calls=aci_calls,
            changed_paths=tuple(changed_paths),
            model_id=model_id,
            reason=reason,
            failure_kind=failure_kind,
        )

    def execute(
        self,
        *,
        decision: RoutingDecision,
        contract: WorkContract,
        context_bundle: ContextBundle,
        workspace_session: WorkspaceSession,
        request_id: str | None = None,
        task_id: str | None = None,
        keep_alive: int | str = -1,
        verification_feedback: dict | None = None,
        candidate_dirty_paths: tuple[str, ...] = (),
        interaction_state: InteractionStateV1 | None = None,
        persist_interaction_state=None,
    ) -> SimpleWorkerResultV2:
        if not isinstance(decision, RoutingDecision):
            raise TypeError("decision must be RoutingDecision")
        if (
            decision.decision_type is not DecisionType.WORKER
            or decision.selected_model is None
        ):
            return self._result(
                SimpleWorkerOutcomeV2.NEEDS_ATTENTION,
                summary=None,
                model_turns=0,
                aci_calls=0,
                changed_paths=[],
                model_id=None,
                reason=decision.routing_reason or "routing decision is not WORKER",
            )
        if not isinstance(contract, WorkContract):
            raise TypeError("contract must be WorkContract")
        if not isinstance(context_bundle, ContextBundle):
            raise TypeError("context_bundle must be ContextBundle")
        if not isinstance(workspace_session, WorkspaceSession):
            raise TypeError("workspace_session must be WorkspaceSession")
        if (not isinstance(candidate_dirty_paths, tuple)
                or any(not isinstance(path, str) or not path.strip()
                       for path in candidate_dirty_paths)):
            raise TypeError("candidate_dirty_paths must be a tuple of non-empty paths")
        if verification_feedback is not None:
            if not isinstance(verification_feedback, dict):
                raise TypeError("verification_feedback must be dict or null")
            encoded_feedback = json.dumps(verification_feedback, ensure_ascii=False, allow_nan=False)
            if len(encoded_feedback.encode("utf-8")) > 16384:
                raise ValueError("verification feedback exceeds bounded message contract")
            verification_feedback = json.loads(encoded_feedback)

        requires_mutation = self._requires_mutation(contract)
        requires_regression = contract.constraints.get("REQUIRES_REGRESSION_CHANGE", False)
        if type(requires_regression) is not bool:
            raise ValueError("REQUIRES_REGRESSION_CHANGE must be bool when present")
        # Mechanical artifact progress only. verification alone checks test content
        # and acceptance. Do not claim prior-slice paths as this slice's edits.
        test_mutation_observed = any(_kind(path) is RepositoryFileKind.TEST
                                     for path in candidate_dirty_paths)
        if verification_feedback is not None:
            failed = verification_feedback.get("failed_required_gates", ())
            if any(item.get("gate_id") == "regression_change" for item in failed):
                test_mutation_observed = False
            elif "regression_change" in verification_feedback.get("observed_passed_gate_ids", ()):
                test_mutation_observed = True
        messages = list(
            initial_messages(
                context_bundle=context_bundle,
                contract=contract,
                workspace_session=workspace_session,
                requires_workspace_mutation=requires_mutation,
            )
        )
        if interaction_state is not None:
            if interaction_state.todo_id != contract.todo_id or interaction_state.run_id != workspace_session.run_id:
                raise ValueError('interaction identity must match the active execution')
            interaction_state = interaction_state.validated(workspace_session, run_id=workspace_session.run_id)
            if persist_interaction_state is not None:
                persist_interaction_state(interaction_state)
        changed_paths: list[str] = []
        model_turns = 0
        aci_calls = 0
        protocol_failures = 0
        viewed_files: dict[str, str] = {} if interaction_state is None else interaction_state.viewed_files()
        seen_reads: set[str] = set()
        no_effect_edits: set[tuple[str, str]] = (set() if interaction_state is None else
            {(path, proposal) for path, version, proposal in interaction_state.failed_edit_fingerprints})
        excluded_actions: set[str] = set()
        fragment_contract_enabled = bool(viewed_files)
        invalid_file_rewrites: dict[str, int] = {}
        disabled_file_rewrites: set[str] = set()
        line_only_edit_paths: set[str] = set()
        creation_misuse_paths: set[str] = set()
        corrective_turn_required = False
        known_view_paths: set[str] = set()
        consumed_view_paths: set[str] = set(viewed_files)
        directory_inventory_available = False

        while True:
            if model_turns >= self.policy.max_model_turns:
                return self._result(
                    SimpleWorkerOutcomeV2.QUOTA_EXHAUSTED,
                    summary=None,
                    model_turns=model_turns,
                    aci_calls=aci_calls,
                    changed_paths=changed_paths,
                    model_id=decision.selected_model,
                    reason="model turn quota exhausted",
                    failure_kind=FailureKindV1.WORKER_QUOTA,
                )

            message_bytes = sum(
                len(message.content.encode("utf-8"))
                for message in messages
            )
            contract_files = viewed_files if fragment_contract_enabled else {}
            request_excluded_actions = set(excluded_actions)
            prior_mutation = interaction_state is not None and bool(candidate_dirty_paths)
            if requires_mutation and not changed_paths and not prior_mutation:
                request_excluded_actions.add("FINISH")
            pending_test_mutation = requires_regression and not test_mutation_observed
            if pending_test_mutation:
                request_excluded_actions.add("FINISH")
            if corrective_turn_required:
                request_excluded_actions.update(("VIEW", "SEARCH"))
            available_views = (tuple(sorted(known_view_paths - consumed_view_paths))
                               if directory_inventory_available else None)
            # Durable knowledge is advisory, never a global VIEW ban. A model
            # may explicitly reinspect unchanged files; repetition is telemetry.
            if interaction_state is not None:
                available_views = None
            action_contract = ExecutionMessage("user", "ACTION_CONTRACT\n" + json.dumps({
                "execution_state": {
                    "remaining_model_turns": self.policy.max_model_turns - model_turns,
                    "remaining_aci_calls": self.policy.max_aci_calls - aci_calls,
                    "remaining_protocol_failures": self.policy.max_protocol_failures - protocol_failures,
                    "effective_mutation_paths": list(changed_paths),
                    "requires_workspace_mutation": requires_mutation,
                    "requires_regression_change": requires_regression,
                    "pending_mutation_artifact_kinds": ["TEST_FILE"] if pending_test_mutation else [],
                    "artifact_progress_is_acceptance": False,
                },
                "verification_feedback": (None if verification_feedback is None else {
                    **verification_feedback,
                    "state": "HISTORICAL_FAILED_CANDIDATE" if changed_paths else "CURRENT_FAILED_CANDIDATE",
                    "snapshot_relation": "PREVIOUS_CANDIDATE" if changed_paths else "BOUND_AT_EXECUTION_START",
                    "pending_gates_cleared_by": "fresh Phase7 verification only",
                }),
                "available_actions": [kind.value for kind in SimpleWorkerActionKindV2
                                      if kind.value not in request_excluded_actions
                                      and not (kind.value == "VIEW" and available_views == ())],
                "observed_edit_paths": list(contract_files),
                "available_view_paths": available_views,
                "view_refresh_condition": "mutation of that path or RUN",
                "file_rewrite_disabled_paths": sorted(disabled_file_rewrites),
                "line_only_edit_paths": sorted(line_only_edit_paths),
                "tool_correction_required": corrective_turn_required or bool(creation_misuse_paths),
                "tool_corrections": [self._compact_creation_correction(
                    path, contract_files, path in consumed_view_paths)
                    for path in sorted(creation_misuse_paths)],
                "edit_match_kinds": {path: (["RANGE: single observed target line"] if path in line_only_edit_paths else
                                            ["RANGE"] if path in disabled_file_rewrites else ["RANGE", "FILE"])
                                     for path in contract_files},
                "existing_file_edit_shapes": (
                    ["EDIT: file, lines {first,last}, replacement literal source text; full observed span permits explicit rewrite"]
                    if contract_files else ["EDIT: file, old_text exact unique fragment, replacement literal source text"]),
                "new_file": "EDIT: file, replacement complete source; no lines; creation only",
            }, sort_keys=True))
            source_index = ExecutionMessage("user", "UNTRUSTED_SOURCE_LINE_INDEX\n" + "\n".join(
                f"PATH {path}\n" + "\n".join(f"{number} | {line}" for number, line in enumerate(content.splitlines(), 1))
                for path, content in contract_files.items()) + "\nEND_SOURCE_LINE_INDEX")
            extras = (source_index, action_contract) if contract_files else (action_contract,)
            if interaction_state is not None:
                extras += (ExecutionMessage('user', 'KNOWN_INTERACTION_STATE_V1\n' +
                    interaction_state.projection(current_dirty_paths=tuple(dict.fromkeys(
                        (*candidate_dirty_paths, *changed_paths))),
                        remaining=(('WORKSPACE_MUTATION',) if requires_mutation and not changed_paths and not prior_mutation else ()) +
                        (('REGRESSION_CHANGE',) if pending_test_mutation else ()))),)
            if message_bytes + sum(len(m.content.encode("utf-8")) for m in extras) > self.policy.max_message_bytes:
                return self._result(
                    SimpleWorkerOutcomeV2.NEEDS_ATTENTION, summary=None,
                    model_turns=model_turns, aci_calls=aci_calls, changed_paths=changed_paths,
                    model_id=decision.selected_model, reason="simple worker message budget exceeded",
                )
            request = ExecutionRequest(
                routing_decision=decision,
                messages=(*messages, *extras),
                keep_alive=keep_alive,
                request_id=request_id,
                task_id=task_id,
                response_format=simple_worker_action_json_schema(
                    viewed_files=contract_files or None,
                    excluded_actions=frozenset(request_excluded_actions),
                    disabled_file_rewrites=frozenset(disabled_file_rewrites),
                    line_only_edit_paths=frozenset(line_only_edit_paths),
                    range_edits=True,
                    compact_edits=self.compact_edit_contract,
                    compact_fragments=not fragment_contract_enabled,
                    available_view_paths=available_views),
                temperature=0.0,
            )
            try:
                result: WorkerResult = self.executor.execute(request)
            except Exception as exc:
                return self._result(
                    SimpleWorkerOutcomeV2.NEEDS_ATTENTION,
                    summary=None,
                    model_turns=model_turns,
                    aci_calls=aci_calls,
                    changed_paths=changed_paths,
                    model_id=decision.selected_model,
                    reason=f"{type(exc).__name__}: {exc}",
                )

            model_turns += 1
            if result.outcome is not ExecutionOutcome.SUCCESS:
                return self._result(
                    SimpleWorkerOutcomeV2.NEEDS_ATTENTION,
                    summary=None,
                    model_turns=model_turns,
                    aci_calls=aci_calls,
                    changed_paths=changed_paths,
                    model_id=result.model_id or decision.selected_model,
                    reason=result.error or f"executor outcome {result.outcome.value}",
                )

            raw = result.content or ""
            if self.observer is not None:
                self.observer("MODEL_RESPONSE", {"turn": model_turns, "raw": raw[:16384], "truncated": len(raw) > 16384, "model_id": result.model_id})
            try:
                action = parse_simple_worker_action(raw, viewed_files=contract_files)
                edit = action.aci_action
                if action.action is SimpleWorkerActionKindV2.EDIT and edit is not None:
                    source = contract_files.get(edit.path)
                    args = json.loads(raw)["arguments"]
                    if "file" in args:
                        match_kind = "RANGE" if "lines" in args else "FRAGMENT" if "old_text" in args else "CREATE"
                        match = {"start_line": args["lines"]["first"], "end_line": args["lines"]["last"]} if "lines" in args else {}
                        single_replacement = "\n" not in args["replacement"] and "\r" not in args["replacement"]
                    else:
                        match = args["match"]
                        match_kind = match["kind"]
                        single_replacement = len(args.get("replacement_lines", [])) <= 1
                    range_is_single = (match_kind == "RANGE" and match["start_line"] == match["end_line"]
                                       and single_replacement)
                    if edit.path in line_only_edit_paths and match_kind != "LINE" and not range_is_single:
                        return self._result(SimpleWorkerOutcomeV2.NEEDS_ATTENTION,
                            summary=None, model_turns=model_turns, aci_calls=aci_calls,
                            changed_paths=changed_paths, model_id=result.model_id or decision.selected_model,
                            reason="EDIT_CONTRACT_NOT_APPLIED")
                    if match_kind == "FILE" and edit.path in disabled_file_rewrites:
                        return self._result(SimpleWorkerOutcomeV2.NEEDS_ATTENTION,
                            summary=None, model_turns=model_turns, aci_calls=aci_calls,
                            changed_paths=changed_paths, model_id=result.model_id or decision.selected_model,
                            reason="EDIT_CONTRACT_NOT_APPLIED")
                    if match_kind == "FILE" and source is None:
                        raise SimpleWorkerProtocolError("FILE requires a current bounded VIEW of the complete file")
                    if edit.path in disabled_file_rewrites and source is None:
                        raise SimpleWorkerProtocolError("restricted edit requires a current bounded VIEW")
                    if source is not None:
                        if match_kind == "RANGE" and edit.path in disabled_file_rewrites and not range_is_single:
                            lines = source.splitlines(keepends=True)
                            if not "".join(lines[:match["start_line"] - 1] + lines[match["end_line"]:]).strip():
                                return self._result(SimpleWorkerOutcomeV2.NEEDS_ATTENTION,
                                    summary=None, model_turns=model_turns, aci_calls=aci_calls,
                                    changed_paths=changed_paths, model_id=result.model_id or decision.selected_model,
                                    reason="EDIT_CONTRACT_NOT_APPLIED")
                        if match_kind == "FILE" and edit.old_text != source:
                            raise SimpleWorkerProtocolError("FILE requires full observed old_text")
                        if match_kind == "LINE" and edit.old_text not in source.splitlines():
                            raise SimpleWorkerProtocolError("LINE requires one complete observed old_text line")
                        if match_kind == "FRAGMENT" and edit.old_text in source:
                            before, _, after = source.partition(edit.old_text)
                            if not (before + after).strip():
                                raise SimpleWorkerProtocolError("full-file replacement requires explicit FILE kind")
            except SimpleWorkerProtocolError as exc:
                correction = None
                if exc.error_code == "INVALID_SOURCE_LINE_ARRAY" and exc.path in contract_files:
                    line_only_edit_paths.add(exc.path)
                    corrective_turn_required = True
                    correction = {"error_code": exc.error_code, "path": exc.path,
                        "allowed_edit_kind": "RANGE", "target_span": "one observed line",
                        "replacement_lines_max_items": 1,
                        "workspace_mutated": False,
                        "reset_condition": "successful mutation of this file"}
                if self.observer is not None:
                    self.observer("PROTOCOL_ERROR", {"turn": model_turns, "reason": str(exc),
                        "correction": correction})
                protocol_failures += 1
                if protocol_failures >= self.policy.max_protocol_failures:
                    return self._result(
                        SimpleWorkerOutcomeV2.NEEDS_ATTENTION,
                        summary=None,
                        model_turns=model_turns,
                        aci_calls=aci_calls,
                        changed_paths=changed_paths,
                        model_id=result.model_id or decision.selected_model,
                        reason="simple worker protocol failure limit reached",
                        failure_kind=FailureKindV1.PROTOCOL_INVALID,
                    )
                messages.append(protocol_error_message(str(exc)))
                if correction is not None:
                    messages.append(ExecutionMessage("user", "TOOL_CORRECTION_REQUIRED\n"
                        + json.dumps(correction, sort_keys=True)))
                continue

            if action.action is SimpleWorkerActionKindV2.FINISH:
                if pending_test_mutation:
                    if self.observer is not None:
                        self.observer("COMPLETION_REJECTED", {"turn": model_turns,
                            "error_code": "REQUIRED_TEST_MUTATION_MISSING",
                            "required_artifact_kind": "TEST_FILE"})
                    messages.append(ExecutionMessage("assistant", raw))
                    messages.append(completion_rejected_message(required_test_mutation=True))
                    continue
                if requires_mutation and not changed_paths and not prior_mutation:
                    messages.append(ExecutionMessage("assistant", raw))
                    messages.append(completion_rejected_message())
                    continue
                return self._result(
                    SimpleWorkerOutcomeV2.SUCCESS,
                    summary=action.summary,
                    model_turns=model_turns,
                    aci_calls=aci_calls,
                    changed_paths=changed_paths,
                    model_id=result.model_id or decision.selected_model,
                )

            if action.action.value in request_excluded_actions:
                return self._result(
                    SimpleWorkerOutcomeV2.NEEDS_ATTENTION, summary=None,
                    model_turns=model_turns, aci_calls=aci_calls, changed_paths=changed_paths,
                    model_id=decision.selected_model,
                    reason=("TOOL_CORRECTION_NOT_APPLIED" if corrective_turn_required
                            and action.action.value not in excluded_actions else "NO_PROGRESS_CONSTRAINT_NOT_APPLIED"),
                    failure_kind=FailureKindV1.TOOL_LOOP,
                )

            if action.action is SimpleWorkerActionKindV2.VIEW and available_views is not None:
                path = posixpath.normpath(action.aci_action.path.replace("\\", "/"))
                path = "" if path == "." else path
                if path not in available_views:
                    return self._result(SimpleWorkerOutcomeV2.NEEDS_ATTENTION,
                        summary=None, model_turns=model_turns, aci_calls=aci_calls,
                        changed_paths=changed_paths, model_id=decision.selected_model,
                        reason="VIEW_AFFORDANCE_NOT_APPLIED")

            if aci_calls >= self.policy.max_aci_calls:
                return self._result(
                    SimpleWorkerOutcomeV2.QUOTA_EXHAUSTED,
                    summary=None,
                    model_turns=model_turns,
                    aci_calls=aci_calls,
                    changed_paths=changed_paths,
                    model_id=result.model_id or decision.selected_model,
                    reason="ACI call quota exhausted",
                    failure_kind=FailureKindV1.TOOL_CALL_QUOTA,
                )

            assert action.aci_action is not None
            edit_key = None
            if action.action is SimpleWorkerActionKindV2.EDIT:
                edit_key = (posixpath.normpath(action.aci_action.path.replace("\\", "/")),
                            hashlib.sha256(json.dumps({"old_text": action.aci_action.old_text,
                                "new_text": action.aci_action.new_text}, sort_keys=True,
                                ensure_ascii=False).encode("utf-8")).hexdigest())
                if edit_key in no_effect_edits:
                    if self.observer is not None:
                        self.observer("TOOL_LOOP", {"turn": model_turns, "action": "EDIT",
                            "error_code": "NO_EFFECT", "path": edit_key[0],
                            "proposal_fingerprint": edit_key[1], "workspace_mutated": False})
                    return self._result(SimpleWorkerOutcomeV2.NEEDS_ATTENTION,
                        summary=None, model_turns=model_turns, aci_calls=aci_calls,
                        changed_paths=changed_paths, model_id=result.model_id or decision.selected_model,
                        reason=f"repeated no-effect EDIT for {edit_key[0]}",
                        failure_kind=FailureKindV1.TOOL_LOOP)
            if self.compact_edit_contract and action.action is SimpleWorkerActionKindV2.EDIT:
                correction_path = posixpath.normpath(action.aci_action.path.replace("\\", "/"))
                if match_kind == "CREATE" and correction_path in creation_misuse_paths:
                    if self.observer is not None:
                        self.observer("TOOL_LOOP", {"turn": model_turns,
                            "reason": "TOOL_CORRECTION_NOT_APPLIED",
                            "error_code": "EXACT_REPLACEMENT_REQUIRED", "path": correction_path})
                    return self._result(SimpleWorkerOutcomeV2.NEEDS_ATTENTION,
                        summary=None, model_turns=model_turns, aci_calls=aci_calls,
                        changed_paths=changed_paths, model_id=result.model_id or decision.selected_model,
                        reason="TOOL_CORRECTION_NOT_APPLIED", failure_kind=FailureKindV1.TOOL_LOOP)
                if match_kind != "CREATE":
                    creation_misuse_paths.discard(correction_path)
            if self.observer is not None:
                self.observer("ACI_ACTION", {"turn": model_turns, "action": action.action.value,
                    "raw": raw[:16384], "truncated": len(raw) > 16384})
            observation = self.aci.execute(
                session=workspace_session,
                call_id=f"simple-worker:{model_turns}:{aci_calls + 1}",
                action=action.aci_action,
            )
            aci_calls += 1
            if (self.compact_edit_contract and action.action is SimpleWorkerActionKindV2.EDIT
                    and match_kind == "CREATE" and observation.error_code is not None
                    and observation.error_code.value == "EXACT_REPLACEMENT_REQUIRED"):
                path = posixpath.normpath(action.aci_action.path.replace("\\", "/"))
                creation_misuse_paths.add(path)
                correction = self._compact_creation_correction(
                    path, contract_files, path in consumed_view_paths)
                observation = replace(observation, metadata={**observation.metadata,
                    "required_action": correction["required_action"],
                    "required_fields": correction["required_fields"],
                    "tool_correction": correction})
            if edit_key is not None and observation.error_code is not None and observation.error_code.value == "NO_EFFECT":
                no_effect_edits.add(edit_key)
                observation = replace(observation, metadata={**observation.metadata,
                    "progress_status": "NO_CHANGE", "repeated_proposal_failure_kind": FailureKindV1.TOOL_LOOP.value})
            if observation.ok and action.action is SimpleWorkerActionKindV2.VIEW:
                path = posixpath.normpath(action.aci_action.path.replace("\\", "/"))
                path = "" if path == "." else path
                consumed_view_paths.add(path)
                if "entry_count" in observation.metadata:
                    entries = json.loads(observation.output)
                    known_view_paths.update(entry["path"] for entry in entries)
                    directory_inventory_available = True
                    # A large directory retains the unrestricted VIEW contract;
                    # do not offer an incomplete bounded inventory as authority.
                    if (len(known_view_paths) > 256 or
                            sum(len(p.encode("utf-8")) for p in known_view_paths) > 16384):
                        directory_inventory_available = False
            if action.action is SimpleWorkerActionKindV2.RUN:
                consumed_view_paths.clear()
                excluded_actions.discard("VIEW")
                known_view_paths.add("")
            # One explicit corrective EDIT/RUN consumes the turn requirement.
            # Another rejected syntax proposal renews it below. A conflict or
            # occurrence mismatch may require a fresh VIEW, which stays available.
            corrective_turn_required = False
            if (observation.error_code is not None
                    and observation.error_code.value == "LOW_LEVEL_CONFLICT"
                    and observation.metadata.get("required_action") == "VIEW"):
                stale_path = observation.metadata.get("path")
                if isinstance(stale_path, str):
                    viewed_files.pop(stale_path, None)
                    consumed_view_paths.discard(stale_path)
                    known_view_paths.add(stale_path)
            if observation.changed_paths:
                for path in observation.changed_paths:
                    creation_misuse_paths.discard(posixpath.normpath(path.replace("\\", "/")))
                    consumed_view_paths.discard(path)
                    if observation.metadata.get("edit_mode") == "create":
                        consumed_view_paths.discard(posixpath.dirname(path))
                known_view_paths.update(observation.changed_paths)
            if (action.action is SimpleWorkerActionKindV2.EDIT
                    and match_kind == "FILE"
                    and observation.error_code is not None
                    and observation.error_code.value == "SYNTAX_PREFLIGHT_FAILED"):
                path = action.aci_action.path
                invalid_file_rewrites[path] = invalid_file_rewrites.get(path, 0) + 1
                if invalid_file_rewrites[path] >= self.policy.max_invalid_file_rewrites:
                    disabled_file_rewrites.add(path)
                    observation = replace(observation, metadata={**observation.metadata,
                        "edit_contract": {"path": path, "allowed_kinds": ["RANGE"],
                            "target_span": "proper source span or single observed line",
                            "reason": "REPEATED_SYNTAX_PREFLIGHT_FAILURE",
                            "reset_condition": "successful mutation of this file"}})
            if (action.action is SimpleWorkerActionKindV2.EDIT
                    and match_kind in ("FRAGMENT", "RANGE")
                    and action.aci_action.path in contract_files
                    and observation.error_code is not None
                    and observation.error_code.value == "SYNTAX_PREFLIGHT_FAILED"
                    and ((match_kind == "RANGE" and not range_is_single)
                         or (match_kind == "FRAGMENT" and
                             ("\n" in action.aci_action.old_text or "\n" in action.aci_action.new_text)))):
                path = action.aci_action.path
                line_only_edit_paths.add(path)
                observation = replace(observation, metadata={**observation.metadata,
                    "edit_contract": {"path": path, "allowed_kinds": ["RANGE"], "target_span": "single observed line",
                        "reason": "REJECTED_MULTILINE_SYNTAX_PREFLIGHT",
                        "reset_condition": "successful mutation of this file"}})
            if (action.action is SimpleWorkerActionKindV2.EDIT
                    and action.aci_action.path in line_only_edit_paths
                    and observation.error_code is not None
                    and observation.error_code.value == "SYNTAX_PREFLIGHT_FAILED"):
                corrective_turn_required = True
            if observation.ok and action.action in (SimpleWorkerActionKindV2.VIEW, SimpleWorkerActionKindV2.SEARCH):
                read_key = hashlib.sha256(json.dumps({
                    "action": action.action.value, "arguments": json.loads(raw)["arguments"],
                    "output": observation.output,
                }, sort_keys=True).encode("utf-8")).hexdigest()
                if read_key in seen_reads and interaction_state is None:
                    excluded_actions.add(action.action.value)
                    observation = replace(observation, metadata={
                        **observation.metadata, "progress_status": "NO_NEW_INFORMATION",
                        "temporarily_unavailable_actions": sorted(excluded_actions),
                        "reset_condition": "successful workspace mutation",
                    })
                seen_reads.add(read_key)
            if (observation.ok and action.action is SimpleWorkerActionKindV2.VIEW
                    and "path" in observation.metadata and "size_bytes" in observation.metadata
                    and len(observation.output.encode("utf-8")) <= 16384
                    and len(observation.output.splitlines()) <= 128):
                # Bounded model-facing exact-fragment contract; no hashes or
                # inference of intended changes. Reading tests must not remove
                # the previously observed implementation from the edit space.
                path = observation.metadata["path"]
                fragment_contract_enabled = True
                viewed_files.pop(path, None)
                viewed_files[path] = observation.output
                while (len(viewed_files) > 4 or
                       sum(len(text.encode("utf-8")) for text in viewed_files.values()) > 16384):
                    evicted = next(iter(viewed_files))
                    viewed_files.pop(evicted)
                    consumed_view_paths.discard(evicted)
            elif observation.ok and action.action is SimpleWorkerActionKindV2.VIEW and "path" in observation.metadata:
                # Larger files retain the generic exact-fragment ACI contract.
                fragment_contract_enabled = False
            if observation.changed_paths:
                for path in observation.changed_paths:
                    disabled_file_rewrites.discard(path)
                    invalid_file_rewrites.pop(path, None)
                    line_only_edit_paths.discard(path)
                seen_reads.clear()
                no_effect_edits.clear()
                excluded_actions.clear()
                viewed_files = {path: text for path, text in viewed_files.items()
                                if path not in observation.changed_paths}
            if interaction_state is not None:
                interaction_state, repeated = interaction_state.observe(
                    workspace_session, action.aci_action, observation, edit_key=edit_key)
                if action.action is SimpleWorkerActionKindV2.RUN:
                    viewed_files.clear()
                    fragment_contract_enabled = False
                if repeated:
                    observation = replace(observation, metadata={**observation.metadata,
                        'repeated_observation': True, 'progress_status': 'NO_NEW_INFORMATION',
                        'no_progress_count': interaction_state.no_progress_count})
                if persist_interaction_state is not None:
                    persist_interaction_state(interaction_state)
                if self.observer is not None:
                    self.observer('INTERACTION_PROGRESS', {'repeated_observation': repeated,
                        'no_progress_count': interaction_state.no_progress_count,
                        'state_version': interaction_state.state_version,
                        'action': action.action.value, 'path': action.aci_action.path,
                        'effective_mutation': bool(observation.changed_paths)})
            if self.observer is not None:
                self.observer("ACI_OBSERVATION", {"turn": model_turns, "observation": observation_message(observation, max_bytes=self.policy.max_observation_bytes).content})
            for path in observation.changed_paths:
                if path not in changed_paths:
                    changed_paths.append(path)
                if _kind(path) is RepositoryFileKind.TEST:
                    test_mutation_observed = True

            if interaction_state is not None and interaction_state.no_progress_count >= 3:
                # First/second legitimate reinspections are allowed and expose
                # typed telemetry. Sustained unchanged rediscovery hands off to
                # the existing failure/recovery policy, never a new controller.
                return self._result(SimpleWorkerOutcomeV2.NEEDS_ATTENTION,
                    summary=None, model_turns=model_turns, aci_calls=aci_calls,
                    changed_paths=changed_paths, model_id=result.model_id or decision.selected_model,
                    reason='NO_NEW_INFORMATION: repeated version-bound inspections without new evidence',
                    failure_kind=FailureKindV1.TOOL_LOOP)

            # A rejected preflight proposal never became workspace source. Keep
            # its full bounded body in durable evidence, not as a new example
            # of candidate code in the next model request.
            if not rejected_candidate_unchanged(observation):
                messages.append(ExecutionMessage("assistant", raw))
            messages.append(
                observation_message(
                    observation,
                    max_bytes=self.policy.max_observation_bytes,
                    quarantine_rejected_source=True,
                )
            )

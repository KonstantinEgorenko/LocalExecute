"""Bounded deterministic model↔tool coding loop for one Control Plane Todo."""
from __future__ import annotations

import json
import os
from collections import Counter
from collections.abc import Mapping
from typing import Any, Callable

from ..context_manager.integration import ControlPlaneArtifactBridge, artifact_uri, parse_artifact_uri
from ..context_manager.models import ArtifactKind, ArtifactOrigin, ContextBundle
from ..context_manager.store import ArtifactStore
from ..control_plane import (
    AttentionItem,
    AttentionSeverity,
    AttentionStatus,
    AttemptRecord,
    Evidence,
    EvidenceStatus,
    Handoff,
    QuotaUsage,
    Run,
    RunStatus,
    WorkContract,
)
from ..control_plane.store import RunStore
from ..errors import ExecutionError, StaleRoutingDecisionError
from ..executor.models import ExecutionMessage, ExecutionOutcome, ExecutionRequest, ExecutionStage, WorkerResult
from ..orchestrator.models import RoutingTaskSpec
from ..observability import ExecutionObserver
from ..orchestrator.profile_factory import TaskProfileFactory
from ..router.profile import AttemptFailure, FailureReason
from ..router.scoring import DecisionType, RoutingDecision
from ..workspace.artifacts import WorkspaceArtifactBridge
from ..workspace.dispatcher import WorkspaceToolDispatcher
from ..workspace.errors import WorkspacePathError
from ..workspace.models import ToolName, ToolRequest, ToolResult, ToolStatus, WorkspaceSession
from ..workspace.workspace import normalize_relative_path
from .errors import AgentLoopStateError, AgentProtocolError, AgentResumeError
from .models import AgentActionKind, AgentLoopPolicy, AgentLoopResult, AgentOutcome
from .prompt import completion_error_message, initial_messages, mutation_no_effect_message, mutation_progress_message, protocol_error_message, safe_edit_required_message, tool_correction_message, tool_result_message, verification_repair_required_message
from .protocol import agent_action_json_schema, parse_agent_action


_CAS_MUTATION_TOOLS = frozenset(
    {
        ToolName.WRITE_TEXT,
        ToolName.REPLACE_TEXT,
        ToolName.DELETE_FILE,
    }
)


_MUTATION_PHASE_BASE_TOOLS = frozenset(
    {
        ToolName.WRITE_TEXT,
        ToolName.REPLACE_TEXT,
    }
)


_MUTATION_EVIDENCE_TOOLS = frozenset(
    {
        ToolName.WRITE_TEXT.value,
        ToolName.REPLACE_TEXT.value,
        ToolName.DELETE_FILE.value,
    }
)


def _tool_path_key(value: object) -> str | None:
    try:
        normalized = normalize_relative_path(value)
    except WorkspacePathError:
        return None
    return os.path.normcase(normalized).replace("\\", "/")


def _path_is_file_key(result: ToolResult) -> str | None:
    if (
        result.tool is ToolName.LIST_FILES
        and result.status is ToolStatus.INVALID
        and result.metadata.get("error_code") == "PATH_IS_FILE"
    ):
        return _tool_path_key(result.metadata.get("path"))
    return None


def _effective_mutation_from_detail(detail: dict[str, Any]) -> bool:
    tool = detail.get("tool")
    if tool not in _MUTATION_EVIDENCE_TOOLS:
        return False
    changed = detail.get("changed_paths", [])
    if not isinstance(changed, list) or not any(isinstance(path, str) and path for path in changed):
        return False
    if tool == ToolName.DELETE_FILE.value:
        return True
    arguments = detail.get("arguments", {})
    metadata = detail.get("metadata", {})
    if not isinstance(arguments, dict) or not isinstance(metadata, dict):
        return True
    expected = arguments.get("expected_sha256")
    resulting = metadata.get("sha256")
    if isinstance(expected, str) and isinstance(resulting, str) and expected and resulting:
        return expected.casefold() != resulting.casefold()
    return True


def _effective_mutation_from_runtime(request: ToolRequest, result: ToolResult) -> bool:
    return _effective_mutation_from_detail(
        {
            "tool": request.tool.value,
            "arguments": dict(request.arguments),
            "changed_paths": list(result.changed_paths),
            "metadata": dict(result.metadata),
        }
    )


def _verification_repair_mutation_required_from_events(
    events: tuple[Any, ...],
    todo_id: str,
) -> bool:
    latest_failed_verification = -1
    latest_successful_mutation = -1

    for event in events:
        if getattr(getattr(event, "event_type", None), "value", None) != "EVIDENCE_RECORDED":
            continue

        data = event.to_dict()
        payload = data.get("payload", {})
        if payload.get("todo_id") != todo_id:
            continue

        evidence_type = payload.get("evidence_type")
        status = payload.get("status")
        sequence = getattr(event, "sequence", -1)

        if (
            evidence_type == "VERIFICATION_REPORT"
            and status == EvidenceStatus.FAILED.value
        ):
            latest_failed_verification = max(latest_failed_verification, sequence)
            continue

        if (
            evidence_type == "TOOL_RESULT"
            and status == EvidenceStatus.PASSED.value
        ):
            detail = payload.get("payload", {})
            if not isinstance(detail, dict):
                continue
            effective = detail.get("effective_mutation")
            if not isinstance(effective, bool):
                effective = _effective_mutation_from_detail(detail)
            if effective:
                latest_successful_mutation = max(
                    latest_successful_mutation,
                    sequence,
                )

    return (
        latest_failed_verification >= 0
        and latest_failed_verification > latest_successful_mutation
    )


def _mutation_phase_required(
    *,
    requires_workspace_mutation: bool,
    changed_paths: list[str] | tuple[str, ...],
    read_path_counts: Mapping[str, int],
) -> bool:
    """Enter mutation-only action space only after a demonstrated read loop."""
    return (
        requires_workspace_mutation
        and not changed_paths
        and any(count >= 2 for count in read_path_counts.values())
    )


def _mutation_phase_allowed_tools(*, allow_file_delete: bool) -> frozenset[ToolName]:
    tools = set(_MUTATION_PHASE_BASE_TOOLS)
    if allow_file_delete:
        tools.add(ToolName.DELETE_FILE)
    return frozenset(tools)


def _bind_observed_expected_sha256(
    tool: ToolName,
    arguments: Mapping[str, Any],
    observed_sha256_by_path: Mapping[str, str],
) -> tuple[dict[str, Any], bool]:
    result = dict(arguments)

    if tool not in _CAS_MUTATION_TOOLS:
        return result, False
    if result.get("expected_sha256") is not None:
        return result, False

    path = result.get("path")
    if not isinstance(path, str):
        return result, False

    digest = observed_sha256_by_path.get(path)
    if not isinstance(digest, str) or len(digest) != 64:
        return result, False

    result["expected_sha256"] = digest
    return result, True

def _allows_file_delete(contract: WorkContract) -> bool:
    value = contract.constraints.get("ALLOW_FILE_DELETE", False)
    if not isinstance(value, bool):
        raise AgentLoopStateError("ALLOW_FILE_DELETE WorkContract constraint must be a bool")
    return value


def _allows_full_rewrite_after_syntax_failure(contract: WorkContract) -> bool:
    value = contract.constraints.get("ALLOW_FULL_REWRITE_AFTER_SYNTAX_FAILURE", False)
    if not isinstance(value, bool):
        raise AgentLoopStateError(
            "ALLOW_FULL_REWRITE_AFTER_SYNTAX_FAILURE WorkContract constraint must be a bool"
        )
    return value


class CodingAgentLoop:
    'Orchestrate bounded worker turns and explicit workspace tools.\n\n    Router still chooses the model. WorkerExecutor still performs exactly one model\n    call. This class owns only the model↔tool transition loop for one Todo.\n    '

    def __init__(
        self,
        *,
        run_store: RunStore,
        artifact_store: ArtifactStore,
        router: Any,
        executor: Any,
        snapshot_provider: Callable[[], Any],
        dispatcher: WorkspaceToolDispatcher | None = None,
        policies: Mapping[str, Any] | None = None,
        profile_factory: TaskProfileFactory | None = None,
        observer: ExecutionObserver | None = None,
    ) -> None:
        if not isinstance(run_store, RunStore):
            raise ValueError("run_store must implement RunStore")
        if not isinstance(artifact_store, ArtifactStore):
            raise ValueError("artifact_store must implement ArtifactStore")
        self.run_store = run_store
        self.artifact_store = artifact_store
        self.router = router
        self.executor = executor
        self.snapshot_provider = snapshot_provider
        self.dispatcher = dispatcher or WorkspaceToolDispatcher()
        self.policies = dict(policies or {})
        self.retry_policy = self.policies.get("retry_and_anti_loop", self.policies)
        self.loop_policy = AgentLoopPolicy.from_policies(self.policies)
        self.profile_factory = profile_factory or TaskProfileFactory()
        self.cp_artifacts = ControlPlaneArtifactBridge(run_store, artifact_store)
        self.workspace_artifacts = WorkspaceArtifactBridge(artifact_store)
        self.observer = observer

    def execute(
        self,
        *,
        run_id: str,
        contract: WorkContract,
        routing_spec: RoutingTaskSpec,
        context_bundle: ContextBundle,
        workspace_session: WorkspaceSession,
        request_id: str | None = None,
        task_id: str | None = None,
        keep_alive: int | str = -1,
    ) -> AgentLoopResult:
        run = self.run_store.get_run(run_id)
        todo = self.run_store.get_todo(contract.todo_id)
        self._validate_identities(run, todo, contract, context_bundle, workspace_session)

        finalized = self._existing_final(run_id, contract.todo_id, context_bundle.bundle_id)
        if finalized is not None:
            satisfied = self._requirements_satisfied(contract, run_id)
            if satisfied and run.status is RunStatus.RUNNING:
                run = self.run_store.update_run(
                    Run(run.run_id, run.goal_id, run.active_todo_id, RunStatus.COMPLETED, run.quota_usage)
                )
            if satisfied:
                return finalized
            return AgentLoopResult(
                AgentOutcome.WAITING_EVIDENCE,
                finalized.run_id,
                finalized.todo_id,
                finalized.context_bundle_id,
                finalized.final_summary,
                finalized.model_turns,
                finalized.tool_calls,
                finalized.changed_paths,
                finalized.last_model_id,
                "final worker result already exists; required evidence or gates are still pending",
            )
        if run.status is not RunStatus.RUNNING:
            raise AgentLoopStateError("Run must be RUNNING for CodingAgentLoop")

        requires_workspace_mutation = self._requires_workspace_mutation(contract)
        allow_file_delete = _allows_file_delete(contract)
        messages = list(
            initial_messages(
                context_bundle,
                workspace_session,
                requires_workspace_mutation=requires_workspace_mutation,
                allow_file_delete=allow_file_delete,
            )
        )
        messages.extend(self._resume_messages(run_id, contract.todo_id))
        failures = self._failure_history(run_id, contract.todo_id)
        quota = run.quota_usage
        tool_calls = self._tool_call_count(run_id, contract.todo_id)
        tool_failures = self._tool_failure_count(run_id, contract.todo_id)
        protocol_failures = self._protocol_failure_count(run_id, contract.todo_id)
        tool_fingerprints = Counter(self._tool_fingerprints(run_id, contract.todo_id))
        changed_paths = list(self._changed_paths(run_id, contract.todo_id))
        read_path_counts = Counter(self._successful_read_paths(run_id, contract.todo_id))
        post_mutation_read_counts = Counter(
            self._successful_reads_after_latest_mutation(run_id, contract.todo_id)
        )
        observed_sha256_by_path = self._observed_read_sha256(run_id, contract.todo_id)
        pending_tool_correction = self._pending_tool_correction(run_id, contract.todo_id)
        pending_no_effect = self._pending_no_effect_mutation(run_id, contract.todo_id)
        verification_repair_required = _verification_repair_mutation_required_from_events(
            self.run_store.list_events(run_id),
            contract.todo_id,
        )
        safe_edit_path = (
            self._syntax_repair_path(run_id, contract.todo_id)
            if verification_repair_required
            and not _allows_full_rewrite_after_syntax_failure(contract)
            else None
        )
        pending_safe_edit_correction = (
            self._pending_safe_edit_correction(run_id, contract.todo_id, safe_edit_path)
            if safe_edit_path is not None else False
        )
        last_model: str | None = None
        recorded_handoffs = self._handoffs(run_id, contract.todo_id)
        if verification_repair_required:
            messages.append(verification_repair_required_message())

        if safe_edit_path is not None:
            messages.append(safe_edit_required_message(safe_edit_path))

        if pending_no_effect is not None:
            messages.append(mutation_no_effect_message(pending_no_effect[0], pending_no_effect[1]))

        if requires_workspace_mutation and not changed_paths and read_path_counts:
            repeated = [(count, path) for path, count in read_path_counts.items() if count >= 2]
            if repeated:
                count, path = max(repeated)
                messages.append(mutation_progress_message(path, count))

        while True:
            message_bytes = sum(len(message.content.encode("utf-8")) for message in messages)
            if message_bytes > self.loop_policy.max_message_bytes:
                return self._attention_result(
                    run,
                    contract.todo_id,
                    context_bundle.bundle_id,
                    AgentOutcome.NEEDS_ATTENTION,
                    "agent message budget exceeded",
                    quota,
                    tool_calls,
                    changed_paths,
                    last_model,
                    category="context",
                )
            max_calls = min(
                contract.quota.max_worker_calls,
                int(self.retry_policy.get("max_total_worker_calls_per_task", contract.quota.max_worker_calls)),
            )
            if quota.worker_calls >= max_calls:
                return self._attention_result(
                    run,
                    contract.todo_id,
                    context_bundle.bundle_id,
                    AgentOutcome.QUOTA_EXHAUSTED,
                    "worker call quota exhausted",
                    quota,
                    tool_calls,
                    changed_paths,
                    last_model,
                    category="quota",
                )

            if self.observer is not None:
                self.observer.routing_started(run_id=run_id, todo_id=contract.todo_id, attempt_index=quota.worker_calls)
            decision: RoutingDecision = self.router.route(
                self.profile_factory.build(routing_spec, self.snapshot_provider(), tuple(failures))
            )
            if self.observer is not None:
                self.observer.routing_decision(run_id=run_id, todo_id=contract.todo_id, attempt_index=quota.worker_calls, decision=decision)
            if decision.decision_type is not DecisionType.WORKER or decision.selected_model is None:
                return self._attention_result(
                    run,
                    contract.todo_id,
                    context_bundle.bundle_id,
                    AgentOutcome.NEEDS_ATTENTION,
                    decision.routing_reason,
                    quota,
                    tool_calls,
                    changed_paths,
                    last_model,
                    category="router",
                )

            if decision.escalation_from:
                transition = (decision.escalation_from, decision.selected_model)
                if transition not in recorded_handoffs:
                    max_escalations = min(
                        contract.quota.max_escalations,
                        int(self.retry_policy.get("max_escalations_per_task", contract.quota.max_escalations)),
                    )
                    if quota.escalations >= max_escalations:
                        return self._attention_result(
                            run,
                            contract.todo_id,
                            context_bundle.bundle_id,
                            AgentOutcome.QUOTA_EXHAUSTED,
                            "escalation quota exhausted",
                            quota,
                            tool_calls,
                            changed_paths,
                            last_model,
                            category="quota",
                        )
                    self.run_store.record_handoff(
                        run_id,
                        Handoff(
                            contract.todo_id,
                            decision.escalation_from,
                            decision.selected_model,
                            "agent-loop router escalation",
                            quota.worker_calls,
                        ),
                    )
                    recorded_handoffs.add(transition)
                    quota = self._usage(quota, escalations=1)
                    run = self._persist(run, quota)

            attempt_index = quota.worker_calls
            quota = self._usage(quota, worker_calls=1, model=decision.selected_model)
            run = self._persist(run, quota)
            last_model = decision.selected_model

            mutation_phase_required = (
                verification_repair_required
                or _mutation_phase_required(
                    requires_workspace_mutation=requires_workspace_mutation,
                    changed_paths=changed_paths,
                    read_path_counts=read_path_counts,
                )
            )
            mutation_phase_tools = (
                (
                    frozenset({ToolName.REPLACE_TEXT})
                    if safe_edit_path is not None and pending_safe_edit_correction
                    else _mutation_phase_allowed_tools(allow_file_delete=allow_file_delete)
                )
                if mutation_phase_required
                else None
            )
            allow_final = not (
                requires_workspace_mutation
                and (not changed_paths or verification_repair_required)
            )

            execution_request = ExecutionRequest(
                decision,
                tuple(messages),
                keep_alive,
                request_id,
                task_id,
                response_format=agent_action_json_schema(
                    allow_file_delete=allow_file_delete,
                    allowed_tools=mutation_phase_tools,
                    allow_final=allow_final,
                ),
                temperature=0.0,
            )
            if self.observer is not None:
                self.observer.model_call_started(run_id=run_id, todo_id=contract.todo_id, attempt_index=attempt_index, decision=decision, request=execution_request)
            try:
                try:
                    result: WorkerResult = self.executor.execute(execution_request)
                except Exception as exc:
                    if self.observer is not None:
                        self.observer.model_call_failed(run_id=run_id, todo_id=contract.todo_id, attempt_index=attempt_index, decision=decision, error=exc)
                    raise
            except StaleRoutingDecisionError as exc:
                self.run_store.record_attempt(
                    run_id,
                    AttemptRecord(
                        attempt_index,
                        contract.todo_id,
                        decision,
                        decision.selected_model,
                        ExecutionOutcome.REPLAN_REQUIRED,
                        None,
                        str(exc),
                    ),
                )
                failures.append(AttemptFailure(decision.selected_model, FailureReason.COMPLEXITY_RECLASSIFIED, attempt_index))
                max_replans = min(
                    contract.quota.max_replans,
                    int(self.retry_policy.get("max_replans_per_task", contract.quota.max_replans)),
                )
                if quota.replans >= max_replans:
                    return self._attention_result(
                        run,
                        contract.todo_id,
                        context_bundle.bundle_id,
                        AgentOutcome.QUOTA_EXHAUSTED,
                        "replan quota exhausted",
                        quota,
                        tool_calls,
                        changed_paths,
                        last_model,
                        category="quota",
                    )
                quota = self._usage(quota, replans=1)
                run = self._persist(run, quota)
                continue
            except ExecutionError as exc:
                stage = getattr(getattr(exc, "failure_context", None), "stage", None)
                self.run_store.record_attempt(
                    run_id,
                    AttemptRecord(
                        attempt_index,
                        contract.todo_id,
                        decision,
                        decision.selected_model,
                        ExecutionOutcome.EXECUTION_FAILURE,
                        stage,
                        f"{type(exc).__name__}: {exc}",
                    ),
                )
                failures.append(AttemptFailure(decision.selected_model, FailureReason.REPEATED_SAME_FAILURE, attempt_index))
                continue

            if self.observer is not None:
                self.observer.model_call_finished(run_id=run_id, todo_id=contract.todo_id, attempt_index=attempt_index, decision=decision, result=result)
            raw = result.content or ""
            try:
                action = parse_agent_action(raw)
            except AgentProtocolError as exc:
                protocol_failures += 1
                self.run_store.record_attempt(
                    run_id,
                    AttemptRecord(
                        attempt_index,
                        contract.todo_id,
                        decision,
                        decision.selected_model,
                        ExecutionOutcome.EXECUTION_FAILURE,
                        ExecutionStage.RESPONSE_VALIDATION,
                        f"AgentProtocolError: {exc}",
                    ),
                )
                self._record_worker_text(
                    run_id,
                    contract.todo_id,
                    attempt_index,
                    raw,
                    decision.selected_model,
                    evidence_type="AGENT_PROTOCOL_FAILURE",
                    status=EvidenceStatus.FAILED,
                    payload={"reason": str(exc)},
                )
                failures.append(AttemptFailure(decision.selected_model, FailureReason.WORKER_LOW_CONFIDENCE, attempt_index))
                if protocol_failures >= self.loop_policy.max_protocol_failures:
                    return self._attention_result(
                        run,
                        contract.todo_id,
                        context_bundle.bundle_id,
                        AgentOutcome.NEEDS_ATTENTION,
                        "agent protocol failure limit reached",
                        quota,
                        tool_calls,
                        changed_paths,
                        last_model,
                        category="agent_protocol",
                    )
                messages.append(ExecutionMessage("assistant", raw))
                messages.append(protocol_error_message(str(exc)))
                continue

            self.run_store.record_attempt(
                run_id,
                AttemptRecord(
                    attempt_index,
                    contract.todo_id,
                    decision,
                    decision.selected_model,
                    ExecutionOutcome.SUCCESS,
                ),
            )

            if action.action is AgentActionKind.FINAL:
                assert action.final is not None
                if requires_workspace_mutation and (
                    not changed_paths or verification_repair_required
                ):
                    reason = (
                        "FINAL rejected: WorkContract requires a harness-confirmed "
                        "workspace mutation in the current mutation/repair epoch"
                    )
                    self._record_worker_text(
                        run_id,
                        contract.todo_id,
                        attempt_index,
                        raw,
                        decision.selected_model,
                        evidence_type="AGENT_COMPLETION_FAILURE",
                        status=EvidenceStatus.FAILED,
                        payload={
                            "reason": reason,
                            "requires_workspace_mutation": True,
                        },
                    )
                    messages.append(ExecutionMessage("assistant", raw))
                    messages.append(completion_error_message(reason))
                    continue
                final_summary = action.final.summary
                self._record_final(
                    run_id,
                    contract,
                    context_bundle.bundle_id,
                    final_summary,
                    decision.selected_model,
                    attempt_index,
                    quota.worker_calls,
                    tool_calls,
                    tuple(changed_paths),
                )
                outcome = AgentOutcome.SUCCESS if self._requirements_satisfied(contract, run_id) else AgentOutcome.WAITING_EVIDENCE
                if outcome is AgentOutcome.SUCCESS:
                    run = self.run_store.update_run(
                        Run(run.run_id, run.goal_id, run.active_todo_id, RunStatus.COMPLETED, quota)
                    )
                return AgentLoopResult(
                    outcome,
                    run_id,
                    contract.todo_id,
                    context_bundle.bundle_id,
                    final_summary,
                    quota.worker_calls,
                    tool_calls,
                    tuple(changed_paths),
                    last_model,
                )

            assert action.tool_call is not None
            action_path_key = _tool_path_key(action.tool_call.arguments.get("path"))
            if (
                pending_no_effect is not None
                and action.tool_call.fingerprint() == pending_no_effect[2]
            ):
                return self._attention_result(
                    run, contract.todo_id, context_bundle.bundle_id,
                    AgentOutcome.LOOP_DETECTED,
                    "TOOL_CORRECTION_NOT_APPLIED: MUTATION_NO_EFFECT",
                    quota, tool_calls, changed_paths, last_model,
                    category="tool_correction",
                )
            if (
                safe_edit_path is not None
                and pending_safe_edit_correction
                and action.tool_call.tool is ToolName.WRITE_TEXT
                and action_path_key == _tool_path_key(safe_edit_path)
            ):
                return self._attention_result(
                    run, contract.todo_id, context_bundle.bundle_id,
                    AgentOutcome.LOOP_DETECTED,
                    "TOOL_CORRECTION_NOT_APPLIED: SAFE_EDIT_REPLACE_REQUIRED "
                    f"WRITE_TEXT {safe_edit_path}",
                    quota, tool_calls, changed_paths, last_model,
                    category="tool_correction",
                )
            if (
                pending_tool_correction is not None
                and action.tool_call.tool is ToolName.LIST_FILES
                and action_path_key == pending_tool_correction
            ):
                return self._attention_result(
                    run, contract.todo_id, context_bundle.bundle_id,
                    AgentOutcome.LOOP_DETECTED,
                    "TOOL_CORRECTION_NOT_APPLIED: PATH_IS_FILE LIST_FILES "
                    f"{pending_tool_correction}",
                    quota, tool_calls, changed_paths, last_model,
                    category="tool_correction",
                )
            if tool_calls >= self.loop_policy.max_tool_calls:
                return self._attention_result(
                    run,
                    contract.todo_id,
                    context_bundle.bundle_id,
                    AgentOutcome.QUOTA_EXHAUSTED,
                    "tool call quota exhausted",
                    quota,
                    tool_calls,
                    changed_paths,
                    last_model,
                    category="quota",
                )
            fingerprint = action.tool_call.fingerprint()
            tool_fingerprints[fingerprint] += 1
            if tool_fingerprints[fingerprint] > self.loop_policy.max_same_tool_call:
                return self._attention_result(
                    run,
                    contract.todo_id,
                    context_bundle.bundle_id,
                    AgentOutcome.LOOP_DETECTED,
                    "repeated identical tool call limit reached",
                    quota,
                    tool_calls,
                    changed_paths,
                    last_model,
                    category="agent_loop",
                )

            bound_arguments, expected_sha256_bound = _bind_observed_expected_sha256(
                action.tool_call.tool,
                action.tool_call.arguments,
                observed_sha256_by_path,
            )

            self._record_worker_text(
                run_id,
                contract.todo_id,
                attempt_index,
                raw,
                decision.selected_model,
                evidence_type="AGENT_TURN",
                status=EvidenceStatus.PASSED,
                payload={
                    "tool_fingerprint": fingerprint,
                    "expected_sha256_bound": expected_sha256_bound,
                },
            )
            messages.append(ExecutionMessage("assistant", raw))

            tool_index = tool_calls
            request = ToolRequest(
                call_id=f"{run_id}:tool:{tool_index}",
                run_id=run_id,
                todo_id=contract.todo_id,
                tool=action.tool_call.tool,
                arguments=bound_arguments,
            )
            if self.observer is not None:
                self.observer.tool_call_started(run_id=run_id, todo_id=contract.todo_id, attempt_index=attempt_index, request=request)
            try:
                if request.tool is ToolName.DELETE_FILE and not allow_file_delete:
                    tool_result = ToolResult(
                        request.call_id,
                        request.run_id,
                        request.todo_id,
                        request.tool,
                        ToolStatus.DENIED,
                        "DELETE_FILE requires WorkContract constraint ALLOW_FILE_DELETE=true",
                    )
                else:
                    if (
                        safe_edit_path is not None
                        and request.tool is ToolName.WRITE_TEXT
                        and _tool_path_key(request.arguments.get("path")) == _tool_path_key(safe_edit_path)
                    ):
                        tool_result = ToolResult(
                            request.call_id, request.run_id, request.todo_id,
                            request.tool, ToolStatus.INVALID,
                            "full-file rewrite is blocked for the syntax repair path",
                            {
                                "error_code": "SAFE_EDIT_REPLACE_REQUIRED",
                                "path": safe_edit_path,
                                "suggested_tool": ToolName.REPLACE_TEXT.value,
                            },
                        )
                    else:
                        tool_result = self.dispatcher.execute(workspace_session, request)
            except Exception as exc:
                if self.observer is not None:
                    self.observer.tool_call_failed(run_id=run_id, todo_id=contract.todo_id, attempt_index=attempt_index, request=request, error=exc)
                raise
            if self.observer is not None:
                self.observer.tool_call_finished(run_id=run_id, todo_id=contract.todo_id, attempt_index=attempt_index, request=request, result=tool_result)
            tool_calls += 1
            self._record_tool_result(
                run_id,
                contract.todo_id,
                tool_index,
                request,
                tool_result,
                action_fingerprint=fingerprint,
            )

            error_path_key = _path_is_file_key(tool_result)
            if error_path_key is not None:
                pending_tool_correction = error_path_key
            if tool_result.metadata.get("error_code") == "SAFE_EDIT_REPLACE_REQUIRED":
                pending_safe_edit_correction = True
            elif (
                pending_tool_correction is not None
                and request.tool is ToolName.READ_TEXT
                and tool_result.status is ToolStatus.SUCCESS
                and action_path_key == pending_tool_correction
            ):
                pending_tool_correction = None

            if tool_result.status is ToolStatus.SUCCESS:
                metadata = dict(tool_result.metadata)
                observed_path = metadata.get("path")
                if isinstance(observed_path, str):
                    if request.tool in (
                        ToolName.READ_TEXT,
                        ToolName.WRITE_TEXT,
                        ToolName.REPLACE_TEXT,
                    ):
                        observed_digest = metadata.get("sha256")
                        if isinstance(observed_digest, str) and len(observed_digest) == 64:
                            observed_sha256_by_path[observed_path] = observed_digest
                    elif request.tool is ToolName.DELETE_FILE:
                        observed_sha256_by_path.pop(observed_path, None)

            effective_mutation = _effective_mutation_from_runtime(request, tool_result)
            runtime_changed_paths = (
                tuple(tool_result.changed_paths)
                if request.tool.value not in _MUTATION_EVIDENCE_TOOLS or effective_mutation
                else ()
            )
            for path in runtime_changed_paths:
                if path not in changed_paths:
                    changed_paths.append(path)
            if effective_mutation:
                post_mutation_read_counts.clear()
                pending_no_effect = None

            if verification_repair_required and effective_mutation:
                verification_repair_required = False
                safe_edit_path = None
                pending_safe_edit_correction = False

            if (
                tool_result.status is ToolStatus.SUCCESS
                and request.tool.value in _MUTATION_EVIDENCE_TOOLS
                and tool_result.changed_paths
                and not effective_mutation
            ):
                path = next((value for value in tool_result.changed_paths if isinstance(value, str) and value), "")
                resulting_sha = tool_result.metadata.get("sha256")
                messages.append(
                    mutation_no_effect_message(
                        path,
                        resulting_sha if isinstance(resulting_sha, str) else None,
                    )
                )
                pending_no_effect = (
                    path,
                    resulting_sha if isinstance(resulting_sha, str) else None,
                    action.tool_call.fingerprint(),
                )

            if tool_result.status is not ToolStatus.SUCCESS:
                tool_failures += 1
                failures.append(AttemptFailure(decision.selected_model, FailureReason.TOOL_FAILURE, attempt_index))
                if tool_failures >= self.loop_policy.max_tool_failures:
                    return self._attention_result(
                        run,
                        contract.todo_id,
                        context_bundle.bundle_id,
                        AgentOutcome.NEEDS_ATTENTION,
                        "tool failure limit reached",
                        quota,
                        tool_calls,
                        changed_paths,
                        last_model,
                        category="tool",
                    )
            messages.append(tool_result_message(tool_result))
            correction_message = tool_correction_message(tool_result)
            if correction_message is not None:
                messages.append(correction_message)
            if (
                requires_workspace_mutation
                and not changed_paths
                and tool_result.status is ToolStatus.SUCCESS
                and request.tool.value == "READ_TEXT"
            ):
                read_path = request.arguments.get("path")
                if isinstance(read_path, str) and read_path.strip():
                    read_path_counts[read_path] += 1
                    if read_path_counts[read_path] >= 2:
                        messages.append(
                            mutation_progress_message(
                                read_path,
                                read_path_counts[read_path],
                            )
                        )
            elif (
                requires_workspace_mutation
                and changed_paths
                and tool_result.status is ToolStatus.SUCCESS
                and request.tool is ToolName.READ_TEXT
            ):
                read_path = request.arguments.get("path")
                if isinstance(read_path, str) and read_path.strip():
                    post_mutation_read_counts[read_path] += 1
                    if post_mutation_read_counts[read_path] >= 2:
                        return self._attention_result(
                            run,
                            contract.todo_id,
                            context_bundle.bundle_id,
                            AgentOutcome.LOOP_DETECTED,
                            "POST_MUTATION_NO_PROGRESS: repeated READ_TEXT after "
                            f"workspace mutation for {read_path}",
                            quota,
                            tool_calls,
                            changed_paths,
                            last_model,
                            category="agent_loop",
                        )

    def _observed_read_sha256(
        self,
        run_id: str,
        todo_id: str,
    ) -> dict[str, str]:
        observed: dict[str, str] = {}
        for event in self.run_store.list_events(run_id):
            if event.event_type.value != "EVIDENCE_RECORDED":
                continue
            payload = event.to_dict()["payload"]
            if (
                payload.get("todo_id") != todo_id
                or payload.get("evidence_type") != "TOOL_RESULT"
                or payload.get("status") != EvidenceStatus.PASSED.value
            ):
                continue

            detail = payload.get("payload", {})
            if not isinstance(detail, dict):
                continue

            tool = detail.get("tool")
            metadata = detail.get("metadata", {})
            if not isinstance(metadata, dict):
                continue

            path = metadata.get("path")
            if not isinstance(path, str):
                continue

            if tool == ToolName.READ_TEXT.value:
                digest = metadata.get("sha256")
                if isinstance(digest, str) and len(digest) == 64:
                    observed[path] = digest
            elif tool in (ToolName.WRITE_TEXT.value, ToolName.REPLACE_TEXT.value):
                digest = metadata.get("sha256")
                if isinstance(digest, str) and len(digest) == 64:
                    observed[path] = digest
            elif tool == ToolName.DELETE_FILE.value:
                observed.pop(path, None)

        return observed

    @staticmethod
    def _validate_identities(run, todo, contract, bundle, session) -> None:
        if not isinstance(contract, WorkContract):
            raise ValueError("contract must be a WorkContract")
        if not isinstance(bundle, ContextBundle):
            raise ValueError("context_bundle must be a ContextBundle")
        if not isinstance(session, WorkspaceSession):
            raise ValueError("workspace_session must be a WorkspaceSession")
        values = {
            run.goal_id,
            todo.goal_id,
            contract.goal_id,
            bundle.goal_id,
        }
        if len(values) != 1:
            raise AgentLoopStateError("Goal identity mismatch across Run/Todo/Contract/Context")
        if run.active_todo_id != todo.todo_id or todo.todo_id != contract.todo_id or bundle.todo_id != todo.todo_id:
            raise AgentLoopStateError("Todo identity mismatch across Run/Todo/Contract/Context")
        if session.run_id != run.run_id or session.todo_id != todo.todo_id:
            raise AgentLoopStateError("WorkspaceSession identity mismatch")

    def _record_worker_text(
        self,
        run_id: str,
        todo_id: str,
        attempt_index: int,
        content: str,
        model_id: str,
        *,
        evidence_type: str,
        status: EvidenceStatus,
        payload: Mapping[str, Any],
    ) -> None:
        self.cp_artifacts.record_text_evidence(
            run_id,
            artifact_id=f"{run_id}:agent-turn:{attempt_index}",
            todo_id=todo_id,
            evidence_id=f"{run_id}:agent-turn:{attempt_index}",
            evidence_type=evidence_type,
            source=model_id,
            content=content,
            kind=ArtifactKind.WORKER_RESULT,
            origin=ArtifactOrigin.WORKER,
            status=status,
            metadata={"attempt_index": attempt_index, "model_id": model_id},
            evidence_payload=dict(payload),
        )

    def _record_tool_result(
        self,
        run_id: str,
        todo_id: str,
        tool_index: int,
        request: ToolRequest,
        result: ToolResult,
        *,
        action_fingerprint: str | None = None,
    ) -> None:
        if action_fingerprint is None:
            action_fingerprint = json.dumps(
                {"tool": request.tool.value, "arguments": dict(request.arguments)},
                sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            )
        ref = self.workspace_artifacts.capture_tool_result(
            result,
            artifact_id=f"{run_id}:tool:{tool_index}:result",
            created_at="agent-loop",
        )
        self.run_store.record_evidence(
            run_id,
            Evidence(
                evidence_id=f"{run_id}:tool:{tool_index}",
                todo_id=todo_id,
                evidence_type="TOOL_RESULT",
                source="WorkspaceToolDispatcher",
                status=EvidenceStatus.PASSED if result.status is ToolStatus.SUCCESS else EvidenceStatus.FAILED,
                payload={
                    "call_id": request.call_id,
                    "tool": request.tool.value,
                    "arguments": dict(request.arguments),
                    "tool_status": result.status.value,
                    "metadata": dict(result.metadata),
                    "changed_paths": list(result.changed_paths),
                    "effective_mutation": _effective_mutation_from_runtime(request, result),
                    # Preserve the model-declared action identity separately from
                    # the request after deterministic CAS binding.  Resume-time
                    # correction must compare against what the model can repeat.
                    "action_fingerprint": action_fingerprint,
                    "fingerprint": json.dumps(request.to_dict(), sort_keys=True, ensure_ascii=False),
                },
                reference=artifact_uri(ref.artifact_id),
            ),
        )

    def _record_final(
        self,
        run_id: str,
        contract: WorkContract,
        bundle_id: str,
        summary: str,
        model_id: str,
        attempt_index: int,
        model_turns: int,
        tool_calls: int,
        changed_paths: tuple[str, ...],
    ) -> None:
        content = json.dumps(
            {
                "summary": summary,
                "changed_paths": list(changed_paths),
                "model_id": model_id,
                "model_turns": model_turns,
                "tool_calls": tool_calls,
                "context_bundle_id": bundle_id,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        self.cp_artifacts.record_text_evidence(
            run_id,
            artifact_id=f"{run_id}:worker-result:{attempt_index}",
            todo_id=contract.todo_id,
            evidence_id=f"{run_id}:worker-result:{attempt_index}",
            evidence_type="WORKER_RESULT",
            source="CodingAgentLoop",
            content=content,
            kind=ArtifactKind.WORKER_RESULT,
            origin=ArtifactOrigin.WORKER,
            status=EvidenceStatus.PASSED,
            scope_tags=changed_paths,
            metadata={"model_id": model_id, "context_bundle_id": bundle_id},
            evidence_payload={
                "summary": summary,
                "changed_paths": list(changed_paths),
                "model_id": model_id,
                "model_turns": model_turns,
                "tool_calls": tool_calls,
                "context_bundle_id": bundle_id,
            },
        )

    def _existing_final(self, run_id: str, todo_id: str, bundle_id: str) -> AgentLoopResult | None:
        for event in self.run_store.list_events(run_id):
            if event.event_type.value != "EVIDENCE_RECORDED":
                continue
            payload = event.to_dict()["payload"]
            if (
                payload.get("todo_id") != todo_id
                or payload.get("evidence_type") != "WORKER_RESULT"
                or payload.get("status") != EvidenceStatus.PASSED.value
            ):
                continue
            data = payload.get("payload", {})
            if not isinstance(data, dict):
                raise AgentResumeError("WORKER_RESULT evidence payload must be an object")
            if data.get("context_bundle_id") not in (None, bundle_id):
                raise AgentResumeError("existing WORKER_RESULT belongs to a different context bundle")
            changed = data.get("changed_paths", [])
            if not isinstance(changed, list) or any(not isinstance(item, str) for item in changed):
                raise AgentResumeError("existing WORKER_RESULT changed_paths invalid")
            return AgentLoopResult(
                AgentOutcome.ALREADY_FINALIZED,
                run_id,
                todo_id,
                bundle_id,
                data.get("summary") if isinstance(data.get("summary"), str) else "already finalized",
                int(data.get("model_turns", 0)),
                int(data.get("tool_calls", 0)),
                tuple(changed),
                data.get("model_id") if isinstance(data.get("model_id"), str) else None,
            )
        return None

    def _resume_messages(self, run_id: str, todo_id: str) -> tuple[ExecutionMessage, ...]:
        messages: list[ExecutionMessage] = []
        pending_tool_turn = False
        for event in self.run_store.list_events(run_id):
            if event.event_type.value != "EVIDENCE_RECORDED":
                continue
            payload = event.to_dict()["payload"]
            if payload.get("todo_id") != todo_id:
                continue
            evidence_type = payload.get("evidence_type")
            if evidence_type == "AGENT_TURN" and payload.get("status") == EvidenceStatus.PASSED.value:
                if pending_tool_turn:
                    raise AgentResumeError("multiple AGENT_TURN records without an intervening TOOL_RESULT")
                artifact_id = parse_artifact_uri(payload.get("reference"))
                if artifact_id is None:
                    raise AgentResumeError("AGENT_TURN evidence requires artifact reference")
                artifact = self.artifact_store.get_artifact(artifact_id)
                messages.append(ExecutionMessage("assistant", artifact.content))
                pending_tool_turn = True
            elif evidence_type == "AGENT_COMPLETION_FAILURE" and payload.get("status") == EvidenceStatus.FAILED.value:
                if pending_tool_turn:
                    raise AgentResumeError("AGENT_COMPLETION_FAILURE cannot interrupt a pending TOOL turn")
                artifact_id = parse_artifact_uri(payload.get("reference"))
                if artifact_id is None:
                    raise AgentResumeError("AGENT_COMPLETION_FAILURE evidence requires artifact reference")
                artifact = self.artifact_store.get_artifact(artifact_id)
                detail = payload.get("payload", {})
                if not isinstance(detail, dict) or not isinstance(detail.get("reason"), str):
                    raise AgentResumeError("AGENT_COMPLETION_FAILURE payload requires reason")
                messages.append(ExecutionMessage("assistant", artifact.content))
                messages.append(completion_error_message(detail["reason"]))
            elif evidence_type == "TOOL_RESULT":
                artifact_id = parse_artifact_uri(payload.get("reference"))
                if artifact_id is None:
                    raise AgentResumeError("TOOL_RESULT evidence requires artifact reference")
                artifact = self.artifact_store.get_artifact(artifact_id)
                detail = payload.get("payload", {})
                if not isinstance(detail, dict):
                    raise AgentResumeError("TOOL_RESULT payload must be an object")
                result = ToolResult(
                    call_id=str(detail.get("call_id")),
                    run_id=run_id,
                    todo_id=todo_id,
                    tool=self._tool_from_value(detail.get("tool")),
                    status=self._status_from_value(detail.get("tool_status")),
                    output=artifact.content,
                    metadata=detail.get("metadata", {}),
                    changed_paths=tuple(detail.get("changed_paths", [])),
                )
                if not pending_tool_turn:
                    raise AgentResumeError("TOOL_RESULT has no preceding durable AGENT_TURN")
                messages.append(
                    tool_result_message(
                        result, max_output_bytes=self.loop_policy.max_tool_feedback_bytes
                    )
                )
                correction_message = tool_correction_message(result)
                if correction_message is not None:
                    messages.append(correction_message)
                pending_tool_turn = False
        if pending_tool_turn:
            raise AgentResumeError(
                "incomplete durable TOOL turn; refusing to replay or continue without a ToolResult"
            )
        return tuple(messages)

    @staticmethod
    def _tool_from_value(value: object):
        from ..workspace.models import ToolName

        try:
            return ToolName(value)
        except (TypeError, ValueError) as exc:
            raise AgentResumeError("invalid durable tool name") from exc

    @staticmethod
    def _status_from_value(value: object):
        from ..workspace.models import ToolStatus

        try:
            return ToolStatus(value)
        except (TypeError, ValueError) as exc:
            raise AgentResumeError("invalid durable tool status") from exc

    def _failure_history(self, run_id: str, todo_id: str) -> list[AttemptFailure]:
        failures: list[AttemptFailure] = []
        for event in self.run_store.list_events(run_id):
            if event.event_type.value != "ATTEMPT_RECORDED":
                continue
            payload = event.to_dict()["payload"]
            if payload.get("todo_id") != todo_id or payload.get("execution_outcome") not in {
                ExecutionOutcome.EXECUTION_FAILURE.value,
                ExecutionOutcome.REPLAN_REQUIRED.value,
            }:
                continue
            model_id = payload.get("selected_model")
            if isinstance(model_id, str):
                failures.append(
                    AttemptFailure(model_id, FailureReason.REPEATED_SAME_FAILURE, int(payload["attempt_index"]))
                )
        return failures

    def _tool_fingerprints(self, run_id: str, todo_id: str) -> tuple[str, ...]:
        values: list[str] = []
        for event in self.run_store.list_events(run_id):
            if event.event_type.value != "EVIDENCE_RECORDED":
                continue
            payload = event.to_dict()["payload"]
            if payload.get("todo_id") == todo_id and payload.get("evidence_type") == "TOOL_RESULT":
                detail = payload.get("payload", {})
                if isinstance(detail, dict) and isinstance(detail.get("fingerprint"), str):
                    # The durable fingerprint is the exact ToolRequest JSON, not model prose.
                    try:
                        raw = json.loads(detail["fingerprint"])
                        values.append(
                            json.dumps(
                                {"tool": raw["tool"], "arguments": raw["arguments"]},
                                sort_keys=True,
                                separators=(",", ":"),
                                ensure_ascii=False,
                            )
                        )
                    except (KeyError, TypeError, json.JSONDecodeError) as exc:
                        raise AgentResumeError("invalid durable tool fingerprint") from exc
        return tuple(values)

    def _pending_tool_correction(self, run_id: str, todo_id: str) -> str | None:
        """Rebuild correction state from durable tool results on every resume."""
        pending: str | None = None
        for event in self.run_store.list_events(run_id):
            if event.event_type.value != "EVIDENCE_RECORDED":
                continue
            evidence = event.to_dict()["payload"]
            if evidence.get("todo_id") != todo_id or evidence.get("evidence_type") != "TOOL_RESULT":
                continue
            detail = evidence.get("payload", {})
            if not isinstance(detail, dict):
                continue
            metadata = detail.get("metadata", {})
            arguments = detail.get("arguments", {})
            if not isinstance(metadata, dict) or not isinstance(arguments, dict):
                continue
            if (
                detail.get("tool") == ToolName.LIST_FILES.value
                and detail.get("tool_status") == ToolStatus.INVALID.value
                and metadata.get("error_code") == "PATH_IS_FILE"
            ):
                pending = _tool_path_key(metadata.get("path"))
            elif (
                pending is not None
                and detail.get("tool") == ToolName.READ_TEXT.value
                and detail.get("tool_status") == ToolStatus.SUCCESS.value
                and _tool_path_key(arguments.get("path")) == pending
            ):
                pending = None
        return pending

    def _syntax_repair_path(self, run_id: str, todo_id: str) -> str | None:
        """Identify one previously read Python file from the durable failed gate."""
        events = [event.to_dict()["payload"] for event in self.run_store.list_events(run_id)
                  if event.event_type.value == "EVIDENCE_RECORDED"]
        reports = [
            (index, evidence) for index, evidence in enumerate(events)
            if evidence.get("todo_id") == todo_id
            and evidence.get("evidence_type") == "VERIFICATION_REPORT"
            and evidence.get("status") == EvidenceStatus.FAILED.value
        ]
        if not reports:
            return None
        report_index, report_evidence = reports[-1]
        artifact_id = parse_artifact_uri(report_evidence.get("reference"))
        if artifact_id is None:
            return None
        try:
            report = json.loads(self.artifact_store.get_artifact(artifact_id).content)
        except (KeyError, ValueError, TypeError):
            return None
        results = report.get("results") if isinstance(report, dict) else None
        if not isinstance(results, list) or not any(
            isinstance(gate, dict)
            and gate.get("gate_id") == "python_syntax"
            and gate.get("outcome") == "FAILED"
            for gate in results
        ):
            return None

        read_paths: set[str] = set()
        mutated_paths: set[str] = set()
        previously_read_mutations: set[str] = set()
        for evidence in events[:report_index]:
            if evidence.get("todo_id") != todo_id or evidence.get("evidence_type") != "TOOL_RESULT":
                continue
            detail = evidence.get("payload", {})
            if not isinstance(detail, dict) or detail.get("tool_status") != ToolStatus.SUCCESS.value:
                continue
            arguments = detail.get("arguments", {})
            if not isinstance(arguments, dict):
                continue
            path = arguments.get("path")
            if detail.get("tool") == ToolName.READ_TEXT.value:
                key = _tool_path_key(path)
                if key is not None:
                    read_paths.add(key)
            elif detail.get("tool") in _MUTATION_EVIDENCE_TOOLS and detail.get("effective_mutation") is True:
                for changed in detail.get("changed_paths", []):
                    key = _tool_path_key(changed)
                    if key is not None:
                        mutated_paths.add(key)
                        if key in read_paths:
                            previously_read_mutations.add(key)
        if len(mutated_paths) != 1 or mutated_paths != previously_read_mutations:
            return None
        path = next(iter(mutated_paths))
        return path if path.casefold().endswith(".py") else None

    def _pending_safe_edit_correction(
        self, run_id: str, todo_id: str, safe_edit_path: str
    ) -> bool:
        pending = False
        target = _tool_path_key(safe_edit_path)
        for event in self.run_store.list_events(run_id):
            if event.event_type.value != "EVIDENCE_RECORDED":
                continue
            evidence = event.to_dict()["payload"]
            if evidence.get("todo_id") != todo_id or evidence.get("evidence_type") != "TOOL_RESULT":
                continue
            detail = evidence.get("payload", {})
            if not isinstance(detail, dict):
                continue
            metadata = detail.get("metadata", {})
            arguments = detail.get("arguments", {})
            if not isinstance(metadata, dict) or not isinstance(arguments, dict):
                continue
            if (
                metadata.get("error_code") == "SAFE_EDIT_REPLACE_REQUIRED"
                and _tool_path_key(metadata.get("path")) == target
            ):
                pending = True
            elif (
                pending
                and detail.get("tool") == ToolName.REPLACE_TEXT.value
                and detail.get("tool_status") == ToolStatus.SUCCESS.value
                and _tool_path_key(arguments.get("path")) == target
                and detail.get("effective_mutation") is True
            ):
                pending = False
        return pending

    def _successful_read_paths(self, run_id: str, todo_id: str) -> tuple[str, ...]:
        paths: list[str] = []
        for event in self.run_store.list_events(run_id):
            if event.event_type.value != "EVIDENCE_RECORDED":
                continue
            payload = event.to_dict()["payload"]
            if (
                payload.get("todo_id") != todo_id
                or payload.get("evidence_type") != "TOOL_RESULT"
                or payload.get("status") != EvidenceStatus.PASSED.value
            ):
                continue
            detail = payload.get("payload", {})
            if not isinstance(detail, dict) or detail.get("tool") != "READ_TEXT":
                continue
            arguments = detail.get("arguments", {})
            if not isinstance(arguments, dict):
                continue
            path = arguments.get("path")
            if isinstance(path, str) and path.strip():
                paths.append(path)
        return tuple(paths)

    def _successful_reads_after_latest_mutation(
        self, run_id: str, todo_id: str
    ) -> tuple[str, ...]:
        """Return durable successful reads since the latest effective mutation."""
        paths: list[str] = []
        mutation_seen = False
        for event in self.run_store.list_events(run_id):
            if event.event_type.value != "EVIDENCE_RECORDED":
                continue
            payload = event.to_dict()["payload"]
            if (
                payload.get("todo_id") != todo_id
                or payload.get("evidence_type") != "TOOL_RESULT"
            ):
                continue
            detail = payload.get("payload", {})
            if not isinstance(detail, dict):
                continue
            effective = detail.get("effective_mutation")
            if not isinstance(effective, bool):
                effective = _effective_mutation_from_detail(detail)
            if effective:
                mutation_seen = True
                paths.clear()
                continue
            if (
                mutation_seen
                and payload.get("status") == EvidenceStatus.PASSED.value
                and detail.get("tool") == ToolName.READ_TEXT.value
            ):
                arguments = detail.get("arguments", {})
                path = arguments.get("path") if isinstance(arguments, dict) else None
                if isinstance(path, str) and path.strip():
                    paths.append(path)
        return tuple(paths)

    def _pending_no_effect_mutation(
        self, run_id: str, todo_id: str
    ) -> tuple[str, str | None, str] | None:
        pending: tuple[str, str | None, str] | None = None
        for event in self.run_store.list_events(run_id):
            if event.event_type.value != "EVIDENCE_RECORDED":
                continue
            payload = event.to_dict()["payload"]
            if (
                payload.get("todo_id") != todo_id
                or payload.get("evidence_type") != "TOOL_RESULT"
            ):
                continue
            detail = payload.get("payload", {})
            if not isinstance(detail, dict) or detail.get("tool") not in _MUTATION_EVIDENCE_TOOLS:
                continue
            effective = detail.get("effective_mutation")
            if not isinstance(effective, bool):
                effective = _effective_mutation_from_detail(detail)
            if effective:
                pending = None
                continue
            arguments = detail.get("arguments", {})
            metadata = detail.get("metadata", {})
            if not isinstance(arguments, dict) or not isinstance(metadata, dict):
                continue
            path = arguments.get("path")
            tool = detail.get("tool")
            if not isinstance(path, str) or not path.strip() or not isinstance(tool, str):
                continue
            fingerprint = detail.get("action_fingerprint")
            if not isinstance(fingerprint, str) or not fingerprint:
                fingerprint = json.dumps(
                    {"tool": tool, "arguments": arguments},
                    sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                )
            sha = metadata.get("sha256")
            pending = (path, sha if isinstance(sha, str) else None, fingerprint)
        return pending

    def _changed_paths(self, run_id: str, todo_id: str) -> tuple[str, ...]:
        paths: list[str] = []
        for event in self.run_store.list_events(run_id):
            if event.event_type.value != "EVIDENCE_RECORDED":
                continue
            payload = event.to_dict()["payload"]
            if payload.get("todo_id") != todo_id or payload.get("evidence_type") != "TOOL_RESULT":
                continue
            detail = payload.get("payload", {})
            if not isinstance(detail, dict):
                continue
            if detail.get("tool") in _MUTATION_EVIDENCE_TOOLS:
                effective = detail.get("effective_mutation")
                if not isinstance(effective, bool):
                    effective = _effective_mutation_from_detail(detail)
                if not effective:
                    continue
            for path in detail.get("changed_paths", []):
                if isinstance(path, str) and path not in paths:
                    paths.append(path)
        return tuple(paths)

    def _tool_call_count(self, run_id: str, todo_id: str) -> int:
        return sum(
            1
            for event in self.run_store.list_events(run_id)
            if event.event_type.value == "EVIDENCE_RECORDED"
            and (payload := event.to_dict()["payload"]).get("todo_id") == todo_id
            and payload.get("evidence_type") == "TOOL_RESULT"
        )

    def _tool_failure_count(self, run_id: str, todo_id: str) -> int:
        return sum(
            1
            for event in self.run_store.list_events(run_id)
            if event.event_type.value == "EVIDENCE_RECORDED"
            and (payload := event.to_dict()["payload"]).get("todo_id") == todo_id
            and payload.get("evidence_type") == "TOOL_RESULT"
            and payload.get("status") == EvidenceStatus.FAILED.value
        )

    def _protocol_failure_count(self, run_id: str, todo_id: str) -> int:
        return sum(
            1
            for event in self.run_store.list_events(run_id)
            if event.event_type.value == "EVIDENCE_RECORDED"
            and (payload := event.to_dict()["payload"]).get("todo_id") == todo_id
            and payload.get("evidence_type") == "AGENT_PROTOCOL_FAILURE"
        )

    def _handoffs(self, run_id: str, todo_id: str) -> set[tuple[str, str]]:
        result: set[tuple[str, str]] = set()
        for event in self.run_store.list_events(run_id):
            if event.event_type.value != "HANDOFF_RECORDED":
                continue
            payload = event.to_dict()["payload"]
            if payload.get("todo_id") == todo_id:
                source, target = payload.get("from_model"), payload.get("to_model")
                if isinstance(source, str) and isinstance(target, str):
                    result.add((source, target))
        return result

    @staticmethod
    def _requires_workspace_mutation(contract: WorkContract) -> bool:
        value = contract.constraints.get("REQUIRES_WORKSPACE_MUTATION", False)
        if not isinstance(value, bool):
            raise AgentLoopStateError(
                "REQUIRES_WORKSPACE_MUTATION WorkContract constraint must be boolean"
            )
        return value

    def _requirements_satisfied(self, contract: WorkContract, run_id: str) -> bool:
        evidence = set()
        gates: dict[str, str] = {}
        for event in self.run_store.list_events(run_id):
            payload = event.to_dict()["payload"]
            if event.event_type.value == "EVIDENCE_RECORDED" and payload.get("todo_id") == contract.todo_id:
                if payload.get("status") == EvidenceStatus.PASSED.value:
                    evidence.add(payload.get("evidence_type"))
            elif event.event_type.value == "GATE_RECORDED" and payload.get("todo_id") == contract.todo_id:
                gate = payload.get("gate", {})
                if isinstance(gate, dict):
                    gates[gate.get("gate_id")] = gate.get("status")
        todo = self.run_store.get_todo(contract.todo_id)
        return all(item in evidence for item in contract.required_evidence) and all(
            gates.get(gate_id) == "SATISFIED" for gate_id in todo.required_gates
        )

    @staticmethod
    def _usage(
        quota: QuotaUsage,
        *,
        worker_calls: int = 0,
        replans: int = 0,
        escalations: int = 0,
        model: str | None = None,
    ) -> QuotaUsage:
        attempts = dict(quota.worker_attempts)
        if model:
            attempts[model] = attempts.get(model, 0) + 1
        return QuotaUsage(
            quota.worker_calls + worker_calls,
            quota.replans + replans,
            quota.escalations + escalations,
            attempts,
        )

    def _persist(self, run: Run, quota: QuotaUsage) -> Run:
        return self.run_store.update_run(
            Run(run.run_id, run.goal_id, run.active_todo_id, run.status, quota)
        )

    def _attention_result(
        self,
        run: Run,
        todo_id: str,
        bundle_id: str,
        outcome: AgentOutcome,
        reason: str,
        quota: QuotaUsage,
        tool_calls: int,
        changed_paths: list[str],
        last_model: str | None,
        *,
        category: str,
    ) -> AgentLoopResult:
        updated = self.run_store.update_run(
            Run(run.run_id, run.goal_id, run.active_todo_id, RunStatus.NEEDS_ATTENTION, quota)
        )
        self.run_store.record_attention(
            updated.run_id,
            AttentionItem(
                attention_id=f"{updated.run_id}:agent-attention:{len(self.run_store.list_events(updated.run_id))}",
                reason=reason,
                severity=AttentionSeverity.HIGH,
                status=AttentionStatus.OPEN,
                todo_id=todo_id,
                run_id=updated.run_id,
                category=category,
            ),
        )
        return AgentLoopResult(
            outcome,
            updated.run_id,
            todo_id,
            bundle_id,
            None,
            quota.worker_calls,
            tool_calls,
            tuple(changed_paths),
            last_model,
            reason,
        )

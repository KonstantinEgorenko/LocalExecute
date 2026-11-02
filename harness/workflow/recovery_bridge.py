'Bridge frozen recovery diagnosis/replan into the workflow workflow loop.\n\nThis module does not change recovery. It adapts workflow execution/verification\nobservations into the frozen FailureEvidenceV2 contract and returns a bounded\nRecoveryObservationV1 that the workflow orchestrator can feed back through\nRouter V2 on a fresh workspace.\n'
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from ..evaluation.models import BenchmarkDifficulty, BenchmarkTaskClass
from harness.router.policy_models import TaskProfileV2
from ..recovery.clients import MinistralDiagnosisClient, OrnithReplanClient
from ..recovery.controller import RecoveryControllerV2
from ..workspace.diagnosis_domain import DiagnosisPathDomainV1
from ..recovery.models import (
    FailureEvidenceV2,
    RecoveryAction,
    RecoveryEvidenceItem,
)
from .models import (
    ExecutionObservationV1,
    RecoveryObservationV1,
    VerificationObservationV1,
    WorkflowTodoV1,
)


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _texts(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    result: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip() and item not in result:
            result.append(item.replace("\\", "/"))
    return tuple(result)


def build_workflow_failure_evidence(
    *,
    todo: WorkflowTodoV1,
    objective: str,
    execution: ExecutionObservationV1,
    verification: VerificationObservationV1,
    failed_profile_ids: tuple[str, ...],
    recovery_cycle: int,
    review_replan: Mapping[str, Any] | None = None,
) -> FailureEvidenceV2:
    'Build frozen recovery evidence from an actual workflow failed attempt.'
    routing = todo.routing_task_spec
    if isinstance(routing, TaskProfileV2):
        source_task_class = routing.task_class
        source_difficulty = routing.difficulty
        known_targets = routing.known_target_files
    else:
        source_task_class = BenchmarkTaskClass.MULTI_FILE_CHANGE
        source_difficulty = BenchmarkDifficulty.NORMAL
        known_targets = ()

    raw = execution.raw
    provenance = raw.get('path_authority_provenance')
    if (raw.get('execution_mode') == 'DIRECT' or 'direct_result' in raw) and (
            provenance is None or 'diagnosis_path_domain' not in raw):
        raise ValueError('DIRECT replan path authority missing')
    if provenance is not None:
        from ..verification.acceptance import fingerprint
        if (provenance.get('source') != 'WorkspaceSessionFactory'
                or provenance.get('run_id') != raw.get('run_id')
                or provenance.get('todo_id') != todo.todo_id
                or provenance.get('lease_id') != raw.get('lease_id')
                or provenance.get('contract_fingerprint') != fingerprint(todo.work_contract.to_dict())):
            raise ValueError('path authority parent identity mismatch')
    domain = (DiagnosisPathDomainV1.from_dict(raw['diagnosis_path_domain'])
              if 'diagnosis_path_domain' in raw else None)
    workspace_paths = list(_texts(raw.get("workspace_paths")))
    for path in known_targets:
        if path not in workspace_paths:
            workspace_paths.append(path)

    immutable_paths = list(_texts(raw.get("immutable_paths")))
    if not immutable_paths:
        immutable_paths.extend(
            str(path).replace("\\", "/")
            for path in getattr(todo.work_contract, "forbidden_scope", ())
            if isinstance(path, str) and path.strip()
        )
    for path in immutable_paths:
        if path not in workspace_paths:
            workspace_paths.append(path)

    # A generic production adapter should expose the worktree file set in raw.
    # The fallback still permits known targets/scopes, but a replan can only
    # target paths explicitly present in this immutable evidence bundle.
    if not workspace_paths:
        workspace_paths.extend(
            str(path).replace("\\", "/")
            for path in getattr(todo.work_contract, "allowed_scope", ())
            if isinstance(path, str) and path.strip()
        )

    if domain is not None:
        # Already authorized at the Workspace boundary. Immutable infrastructure
        # is negative authority, never a substitute for the task file domain.
        workspace_paths = list(domain.workspace_paths)
        immutable_paths = list(domain.immutable_paths)

    verification_id = (
        f"phase16:{todo.todo_id}:recovery:{recovery_cycle}:"
        f"verification:{verification.fingerprint[:16]}"
    )
    execution_id = (
        f"phase16:{todo.todo_id}:recovery:{recovery_cycle}:"
        f"execution:{execution.profile_id}"
    )
    evidence = (
        RecoveryEvidenceItem(
            verification_id,
            "DETERMINISTIC_VERIFICATION",
            verification.summary[:4000],
            {
                "report_id": verification.report_id,
                "fingerprint": verification.fingerprint,
                "evidence_ids": list(verification.evidence_ids),
                "raw": _plain(verification.raw),
            },
        ),
        RecoveryEvidenceItem(
            execution_id,
            "FAILED_EXECUTION",
            (
                f"profile={execution.profile_id}; outcome={execution.outcome}; "
                f"finalized={execution.finalized}; changed_paths={list(execution.changed_paths)}; "
                f"summary={execution.summary or ''}"
            )[:4000],
            {
                "profile_id": execution.profile_id,
                "outcome": execution.outcome,
                "finalized": execution.finalized,
                "changed_paths": list(execution.changed_paths),
                "model_calls": execution.model_calls,
                "tool_calls": execution.tool_calls,
                "raw": _plain(execution.raw),
            },
        ),
    )
    if review_replan is not None:
        evidence += (RecoveryEvidenceItem(
            f"phase16:{todo.todo_id}:recovery:{recovery_cycle}:judge-replan",
            "JUDGE_REPLAN",
            "Durable Judge requested structural replanning after deterministic verification",
            {"review_replan": _plain(review_replan)},
        ),)

    if domain is not None:
        evidence += (
            RecoveryEvidenceItem(f'phase16:{todo.todo_id}:objective', 'OBJECTIVE_CONSTRAINTS',
                'Original task objective and immutable constraints',
                {'objective': objective, 'required_evidence': list(todo.work_contract.required_evidence),
                 'immutable_paths_mean': 'must not be modified; never positive edit authority'}),
            RecoveryEvidenceItem(f'phase16:{todo.todo_id}:path-domain', 'AUTHORIZED_PATH_METADATA',
                'Workspace-authorized path identities only; no current content assertion', domain.to_dict()),
        )

    return FailureEvidenceV2(
        task_id=todo.todo_id,
        objective=objective,
        source_task_class=source_task_class,
        source_difficulty=source_difficulty,
        source_metadata={
            "phase": "16",
            "goal_id": todo.work_contract.goal_id,
            "run_id": todo.run_id,
            "recovery_cycle": recovery_cycle,
            "dependencies": list(todo.dependencies),
            **({'diagnosis_path_domain': domain.to_dict()} if domain is not None else {}),
        },
        workspace_paths=tuple(dict.fromkeys(workspace_paths)),
        immutable_paths=tuple(dict.fromkeys(immutable_paths)),
        failed_profile_ids=tuple(dict.fromkeys(failed_profile_ids)),
        evidence_items=evidence,
    )


def _is_frozen_protocol_exhaustion(stage: str, exc: RuntimeError) -> bool:
    'Recognize only the bounded protocol-exhaustion errors emitted by frozen recovery.\n\n    Unknown RuntimeError instances remain exceptional: this adapter must not hide\n    infrastructure, configuration, or programming failures behind a semantic STOP.\n    '
    from ..recovery.diagnosis_receipts import DiagnosisExhaustedError, DiagnosisInterruptedError
    if isinstance(exc, (DiagnosisExhaustedError, DiagnosisInterruptedError)):
        return stage == exc.stage
    prefixes = {
        "diagnosis": "Ministral diagnosis exhausted ",
        "replan": "Ornith replan exhausted ",
    }
    prefix = prefixes.get(stage)
    return prefix is not None and str(exc).startswith(prefix)


def _protocol_exhaustion_observation(
    *,
    stage: str,
    exc: RuntimeError,
    evidence_fingerprint: str,
) -> RecoveryObservationV1:
    return RecoveryObservationV1(
        False,
        reason=(
            f"Phase15 {stage} model protocol exhausted; "
            "deterministic verification remains authoritative"
        ),
        raw={
            "phase15_bridge": True,
            "evidence_fingerprint": evidence_fingerprint,
            "controller_action": RecoveryAction.STOP.value,
            "bridge_failure_kind": getattr(exc,"error_code","MODEL_PROTOCOL_EXHAUSTED"),
            "failure_kind": getattr(exc,"error_code","MODEL_PROTOCOL_EXHAUSTED"),
            "protocol_failure_origin": stage,
            "protocol_attempts": getattr(exc,"attempts",None),
            "protocol_receipt_ids": list(getattr(exc,"receipt_ids",())),
            "exception_type": type(exc).__name__,
            "cause_classification_source": "TYPED_EXCEPTION" if hasattr(exc,"error_code") else "LEGACY_FROZEN_CLIENT_PREFIX",
            "bridge_failure_stage": stage,
            "bridge_failure": str(exc),
        },
    )


class WorkflowRecoveryBridge:
    'Concrete workflow RecoveryPort backed by frozen recovery clients.'

    def __init__(self, root: Path, *, artifact_store=None) -> None:
        self.root = Path(root)
        self.controller = RecoveryControllerV2.from_project_root(self.root)
        self.policy = self.controller.policy
        self.diagnosis_client = MinistralDiagnosisClient(self.root, self.policy, artifact_store=artifact_store)
        self.replan_client = OrnithReplanClient(self.root, self.policy)

    def recover(
        self,
        todo: WorkflowTodoV1,
        *,
        objective: str,
        execution: ExecutionObservationV1,
        verification: VerificationObservationV1,
        failed_profile_ids: tuple[str, ...],
        recovery_cycle: int,
        review_replan: Mapping[str, Any] | None = None,
    ) -> RecoveryObservationV1:
        bundle = build_workflow_failure_evidence(
            todo=todo,
            objective=objective,
            execution=execution,
            verification=verification,
            failed_profile_ids=failed_profile_ids,
            recovery_cycle=recovery_cycle,
            review_replan=review_replan,
        )
        try:
            diagnosis_call = self.diagnosis_client.diagnose(bundle)
        except RuntimeError as exc:
            if not _is_frozen_protocol_exhaustion("diagnosis", exc):
                raise
            return _protocol_exhaustion_observation(
                stage="diagnosis",
                exc=exc,
                evidence_fingerprint=bundle.fingerprint,
            )
        diagnosis = diagnosis_call.diagnosis
        action = self.controller.next_action(
            diagnosis,
            replans_used=max(0, recovery_cycle - 1),
        )
        if review_replan is not None:
            # The durable Judge decision is the semantic authority. Diagnosis
            # may enrich its evidence, but cannot downgrade REPLAN to a retry.
            replans_used = getattr(self.controller, "actual_replans_used", max(0, recovery_cycle - 1))
            action = (
                RecoveryAction.ORNITH_REPLAN
                if replans_used < self.policy.max_replans_per_task
                else RecoveryAction.STOP
            )
        raw: dict[str, Any] = {
            "phase15_bridge": True,
            "evidence_fingerprint": bundle.fingerprint,
            "diagnosis": diagnosis.to_dict(),
            "diagnosis_model_id": diagnosis_call.model_id,
            "diagnosis_model_tag": diagnosis_call.model_tag,
            "diagnosis_model_calls": diagnosis_call.model_calls,
            "diagnosis_latency_ms": diagnosis_call.latency_ms,
            "controller_action": action.value,
        }
        if review_replan is not None:
            raw["review_replan"] = _plain(review_replan)

        if action is RecoveryAction.RETRY_INFRASTRUCTURE:
            return RecoveryObservationV1(
                False,
                reason="Phase15 infrastructure diagnosis requires neutral retry",
                raw=raw,
            )
        if action in {RecoveryAction.HUMAN_REVIEW, RecoveryAction.STOP}:
            return RecoveryObservationV1(
                False,
                reason=f"Phase15 recovery stopped: {action.value}",
                raw=raw,
            )
        if action is RecoveryAction.EXPAND_CONTEXT:
            request = self.controller.context_request(todo.todo_id, diagnosis)
            routing = todo.routing_task_spec
            expanded = routing
            if isinstance(routing, TaskProfileV2):
                expanded_tokens = min(
                    self.policy.max_context_tokens,
                    max(routing.context_tokens + 2048, routing.context_tokens * 2),
                )
                expanded = replace(
                    routing,
                    context_tokens=expanded_tokens,
                    previous_failures=self.controller.failure_signals(failed_profile_ids),
                )
            queries = tuple(
                dict.fromkeys(
                    (
                        diagnosis.root_cause,
                        *diagnosis.implicated_files,
                    )
                )
            )
            raw["context_expansion_request"] = request.to_dict()
            return RecoveryObservationV1(
                True,
                revised_objective=objective,
                reason="Phase15 requested bounded context expansion",
                routing_task_spec=expanded,
                expansion_queries=queries,
                raw=raw,
            )

        if action is not RecoveryAction.ORNITH_REPLAN:
            return RecoveryObservationV1(
                False,
                reason=f"Unsupported Phase15 recovery action: {action.value}",
                raw=raw,
            )

        try:
            replan_call = self.replan_client.replan(bundle, diagnosis)
        except RuntimeError as exc:
            if not _is_frozen_protocol_exhaustion("replan", exc):
                raise
            return _protocol_exhaustion_observation(
                stage="replan",
                exc=exc,
                evidence_fingerprint=bundle.fingerprint,
            )
        plan = replan_call.plan
        task_profile = self.controller.task_profile(
            plan,
            failed_profile_ids=bundle.failed_profile_ids,
        )
        raw.update(
            {
                "replan": plan.to_dict(),
                "replan_model_id": replan_call.model_id,
                "replan_model_tag": replan_call.model_tag,
                "replan_model_calls": replan_call.model_calls,
                "replan_latency_ms": replan_call.latency_ms,
            }
        )
        return RecoveryObservationV1(
            True,
            revised_objective=plan.revised_objective,
            reason="Phase15 Ministral diagnosis -> Ornith replan",
            routing_task_spec=task_profile,
            expansion_queries=tuple(plan.target_files),
            raw=raw,
        )

# Verified legacy import/pickle compatibility.
Phase15RecoveryBridge = WorkflowRecoveryBridge

build_phase16_failure_evidence = build_workflow_failure_evidence

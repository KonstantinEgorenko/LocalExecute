'Concrete adapters that compose existing subsystems under workflow.\n\nNo policy authority is moved into this module.  It translates frozen public\ncontracts and keeps durable identities aligned across Scheduler, Control Plane,\nworktrees, verification, review, recovery, and the external context spine.\n'

from __future__ import annotations

import hashlib
import json
import subprocess
import threading
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ..agent_loop import ControlPlaneCodingAgent
from ..context_manager import ArtifactStore, ContextBudget
from ..control_plane import QuotaUsage, Run, RunStatus
from ..critic.errors import CriticExhaustedError, CriticProtocolError, CriticRoutingError, CriticStateError, CriticConformanceError
from ..context_manager.errors import ContextResolutionError
from ..evaluation.models import BenchmarkDifficulty
from ..judge import JudgeAction, JudgeDecision
from ..judge.errors import JudgeExhaustedError, JudgeProtocolError, JudgeStateError
from ..orchestrator.models import RoutingTaskSpec
from harness.router.policy_models import TaskProfileV2
from ..workflow.context_spine import ExternalContextSpine
from ..router import Complexity
from .failure_policy import FailureKindV1
from .d3_frozen_repair import SEARCH_TEXT_WORKER_GUIDANCE, repair_repository_grounding
from ..workflow.models import (
    ExecutionObservationV1,
    WorkflowExecutionPolicy,
    ProfileSelectionV1,
    ProjectSnapshotV1,
    RecoveryObservationV1,
    ReviewObservationV1,
    VerificationObservationV1,
    WorkflowTodoV1,
)
from ..workflow.ports import RepositoryContextV1
from ..mission.models import (
    DagAmendmentOperationV1, DagAmendmentV1, DagOperationKind,
    GlobalCompletionGateV1, MissionCheckpointV1, MissionSpecV1, PlanObservationV1,
)
from ..repo_intelligence import RepositoryContextBridge, RepositoryIntelligenceService
from ..scheduler import SchedulerCoordinator, SchedulerTodoStatus
from ..verification import VerificationCoordinator, VerificationOutcome
from ..verification.errors import VerificationStateError
from ..workspace.session import WorkspaceSessionFactory
from ..worktree import GitWorktreeManager
from .work_item_registry import DurableWorkItemRegistry
from .candidate_continuity import (
    candidate_restart_authorized,
    checkpoint_candidate,
    record_candidate_disposition,
    record_delta_summary,
    record_recovery_block,
    validate_reuse,
)
from .failure_policy import RecoveryWorkspaceDispositionV1, recovery_workspace_disposition
from .recovery_cause import ReviewRecoveryCauseKindV1, resolve_review_recovery_cause, publish_review_failure, resolve_review_failure, record_review_failure_block
from .anti_loop_window import persist_tool_loop_reuse_window
from .tool_failure_window import persist_workflow_execution_window
from .progress_evidence import ContinuationEvaluator, ProgressEvidenceBuilder, persist_progress_evidence
from .candidate_cold_restart import persist_cold_restart_checkpoint
from .repair_activation import (
    build_repair_activation,
    latest_repair_activation,
    persist_repair_activation,
    record_repair_directive_presented,
    record_repair_execution_observation,
)
from .candidate_continuation_policy import (
    CandidateContinuationPolicy, CandidateDispositionV1, context_from_rc4_execution,
    persist_candidate_continuation_decision,
)



def _rc5_candidate_decision(*, artifact_store, run_store, execution, run_id: str, todo_id: str,
                            lease_id: str, checkpoint, failure_kind: str,
                            base_disposition: RecoveryWorkspaceDispositionV1, candidate_valid: bool, contract, review_recovery_cause: str | None = None):
    """Evaluate RC-5 only from the exact RC-4 artifact attached to this execution."""
    context = context_from_rc4_execution(
        artifact_store=artifact_store, execution=execution, run_id=run_id, todo_id=todo_id,
        lease_id=lease_id, candidate_id=checkpoint.fingerprint, failure_kind=failure_kind,
        base_disposition=base_disposition, candidate_valid=candidate_valid,
        review_recovery_cause=review_recovery_cause,
    )
    if context is None:
        return None, None
    decision = CandidateContinuationPolicy().evaluate(context)
    artifact_id = persist_candidate_continuation_decision(artifact_store, run_store, context, decision)
    raw = getattr(execution, "raw", {})
    artifact_ids = raw.get("progress_evidence_artifacts") if isinstance(raw, Mapping) else None
    if decision.disposition is CandidateDispositionV1.REUSE_CANDIDATE and isinstance(artifact_ids, list) and len(artifact_ids) == 1 and isinstance(artifact_ids[0], str):
        persist_cold_restart_checkpoint(
            artifact_store=artifact_store, run_store=run_store, checkpoint=checkpoint,
            context=context, decision=decision, progress_artifact_id=artifact_ids[0],
            decision_artifact_id=artifact_id, contract=contract,
        )
    return decision, artifact_id

@dataclass(frozen=True, slots=True)
class ProductionAgentResultV1:
    outcome: Any
    run_id: str
    todo_id: str
    context_bundle_id: str
    final_summary: str | None
    model_turns: int
    tool_calls: int
    changed_paths: tuple[str, ...]
    last_model_id: str | None = None
    reason: str | None = None
    failure_kind: str | None = None
    quota_scope: str | None = None
    category: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "outcome": self.outcome.value if hasattr(self.outcome, "value") else str(self.outcome),
            "run_id": self.run_id,
            "todo_id": self.todo_id,
            "context_bundle_id": self.context_bundle_id,
            "final_summary": self.final_summary,
            "model_turns": self.model_turns,
            "tool_calls": self.tool_calls,
            "changed_paths": list(self.changed_paths),
            "last_model_id": self.last_model_id,
            "reason": self.reason,
        }
        if self.failure_kind is not None:
            d["failure_kind"] = self.failure_kind
        if self.quota_scope is not None:
            d["quota_scope"] = self.quota_scope
        if self.category is not None:
            d["category"] = self.category
        return d
from .execution_slices import (
    ExecutionSliceCheckpointV1,
    ExecutionSliceCheckpointV2,
    ExecutionSliceEvaluator,
    ExecutionSliceGrantDecision,
    FinalizationStage,
    ProductionExecutionSlicePolicy,
    checkpoint_artifact_id,
    latest_checkpoint,
    persist_checkpoint,
    restart_continuation_authorized,
    slice_aware_agent,
    slice_contract,
)


def _git(root: Path, *args: str, check: bool = True) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if check and completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {detail}")
    return completed.stdout.strip()


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _trace_event(trace_registry, event_type: str, *, run_id: str, todo_id: str, payload: Mapping[str, Any] | None = None) -> None:
    if trace_registry is None:
        return
    trace_registry.resolve_for_run(run_id).record(
        event_type,
        run_id=run_id,
        todo_id=todo_id,
        payload={} if payload is None else payload,
    )


def _project_verified_dependency_run_ids(
    *,
    dependencies: Mapping[str, str],
    dependency_context_refs: tuple[Any, ...],
    spine: Any,
    goal_id: str,
) -> dict[str, str]:
    'Project explicit verified outcomes onto logical workflow Run identities.'
    projected = dict(dependencies)

    if not dependency_context_refs:
        return projected

    verified_outcome = getattr(spine, "verified_outcome", None)
    if not callable(verified_outcome):
        return projected

    refs_by_id = {
        ref.artifact_id: ref
        for ref in dependency_context_refs
        if isinstance(getattr(ref, "artifact_id", None), str)
    }

    for dependency in sorted(projected):
        outcome, ref = verified_outcome(goal_id, dependency)

        expected_ref = refs_by_id.get(
            getattr(ref, "artifact_id", None)
        )
        if expected_ref is None or expected_ref != ref:
            raise ContextResolutionError(
                f"verified dependency context ref identity mismatch: {dependency}"
            )

        if (
            getattr(outcome, "goal_id", None) != goal_id
            or getattr(outcome, "todo_id", None) != dependency
        ):
            raise ContextResolutionError(
                f"verified dependency outcome logical identity mismatch: {dependency}"
            )

        logical_run_id = getattr(outcome, "run_id", None)
        if not isinstance(logical_run_id, str) or not logical_run_id:
            raise ContextResolutionError(
                f"verified dependency outcome missing logical Run identity: {dependency}"
            )

        projected[dependency] = logical_run_id

    return projected


class ProductionWorkspaceCoordinator:
    """One isolated attempt worktree at a time; accepted candidates are retained."""

    def __init__(
        self,
        *,
        run_store,
        manager: GitWorktreeManager,
        source_repo_root: Path,
        spine: ExternalContextSpine,
        artifact_store: ArtifactStore | None = None,
        execution_slice_policy: ProductionExecutionSlicePolicy | None = None,
        trace_registry=None,
        acceptance_interpreter=None,
    ) -> None:
        self.run_store = run_store
        self.manager = manager
        self.source_repo_root = Path(source_repo_root).resolve(strict=True)
        self.spine = spine
        self.artifact_store = artifact_store
        self.execution_slice_policy = execution_slice_policy or ProductionExecutionSlicePolicy()
        self.trace_registry = trace_registry
        self.acceptance_interpreter = acceptance_interpreter
        self.scheduler_store = None
        self.dependency_handoffs = {}
        self.factory = WorkspaceSessionFactory(command_prefixes=(), protected_paths=(".git", "state"))
        self._current: dict[str, tuple[str, str]] = {}
        self.active_todo_id: str | None = None
        self._restore_active()

    def _restore_active(self) -> None:
        for lease in self.manager.reconcile():
            try:
                run = self.run_store.get_run(lease.run_id)
            except Exception:
                continue
            # RC-3 may restore accounting for an active execution, but it cannot
            # authorize a retained NEEDS_ATTENTION candidate after cold restart.
            if run.active_todo_id and run.status is RunStatus.RUNNING:
                self._current[run.active_todo_id] = (run.run_id, lease.lease_id)

    def _attempt_number(self, base_run_id: str) -> int:
        prefix = f"{base_run_id}-attempt-"
        runs = getattr(self.run_store, "_runs", {})
        values = []
        for run_id in runs:
            if run_id.startswith(prefix):
                try:
                    values.append(int(run_id[len(prefix):]))
                except ValueError:
                    continue
        return max(values, default=0) + 1

    def _base_ref(self, goal_id: str) -> str:
        try:
            snapshot, _ = self.spine.latest_project_snapshot(goal_id)
            return snapshot.head_commit
        except ContextResolutionError:
            return "HEAD"

    def ensure(self, todo: WorkflowTodoV1) -> tuple[str, str]:
        from .execution_responsibility import (
            persisted_responsibility, root_snapshot, find_root_snapshot,
            record_once, bind_acceptance_lineage,
        )
        from ..verification.acceptance import fingerprint
        handoff = self.handoff(todo.work_contract)
        if handoff is not None:
            handoff.prerequisites(todo)
        responsibility = self._responsibility(todo.work_contract)
        root = find_root_snapshot(self.run_store, todo.todo_id)
        for run in self.run_store.list_runs():
            if run.active_todo_id != todo.todo_id or run.status not in {RunStatus.RUNNING, RunStatus.NEEDS_ATTENTION}:
                continue
            bound = persisted_responsibility(self.run_store, run.run_id)
            frozen = root_snapshot(self.run_store, run.run_id)
            legacy_matches = (bound is None and responsibility.source_amendment_id is None
                and (frozen is None or frozen.contract_fingerprint == responsibility.work_contract_fingerprint))
            if bound != responsibility and not legacy_matches:
                record_once(self.run_store, run.run_id, todo.todo_id, 'STALE_RESPONSIBILITY', {
                    'current_responsibility': responsibility.to_dict(),
                    'previous_responsibility': bound.to_dict() if bound else None,
                    'previous_contract_fingerprint': frozen.contract_fingerprint if frozen else None,
                })
                self.run_store.update_run(replace(run, status=RunStatus.CANCELLED))
        # Reconcile every owned lease, including a CANCELLED Run whose release
        # was interrupted. No fresh candidate may start until stale cleanup ends.
        for lease in self.manager.reconcile():
            if lease.todo_id != todo.todo_id:
                continue
            run = self.run_store.get_run(lease.run_id)
            bound = persisted_responsibility(self.run_store, run.run_id)
            frozen = root_snapshot(self.run_store, run.run_id)
            legacy_matches = (bound is None and responsibility.source_amendment_id is None
                and (frozen is None or frozen.contract_fingerprint == responsibility.work_contract_fingerprint))
            stale = (bound != responsibility and not legacy_matches)
            if run.status is RunStatus.CANCELLED or stale:
                if stale and responsibility.source_amendment_id is not None:
                    from .candidate_continuity import capture_replan_draft
                    capture_replan_draft(self, lease, todo.work_contract, responsibility)
                if run.status is not RunStatus.CANCELLED:
                    self.run_store.update_run(replace(run, status=RunStatus.CANCELLED))
                self.manager.release(lease.lease_id, force=True, delete_branch=True)
                self._current.pop(todo.todo_id, None)
                if self.trace_registry is not None:
                    self.trace_registry.resolve_for_goal(todo.work_contract.goal_id).record(
                        'STALE_RUN_SUPERSEDED', run_id=run.run_id, todo_id=todo.todo_id,
                        payload={'responsibility': responsibility.to_dict(), 'lease_id': lease.lease_id})
            elif run.status is RunStatus.RUNNING:
                self._current[todo.todo_id] = (run.run_id, lease.lease_id)
                record_once(self.run_store, run.run_id, todo.todo_id,
                    'EXECUTION_RESPONSIBILITY_V1', responsibility.to_dict())
        if responsibility.source_amendment_id is not None and root is None:
            raise ValueError('revised execution requires immutable root acceptance')
        self.active_todo_id = todo.todo_id
        existing = self._current.get(todo.todo_id)
        if existing is not None:
            active = False
            try:
                run = self.run_store.get_run(existing[0])
                self.manager.get_active(existing[1])
                active = run.status is RunStatus.RUNNING
            except Exception:
                pass
            if active:
                if responsibility.source_amendment_id is not None:
                    from .candidate_continuity import apply_replan_draft
                    apply_replan_draft(self, self.manager.get_active(existing[1]), todo.work_contract, responsibility)
                if handoff is not None:
                    handoff.validate_consumer(todo, run.run_id, self.manager.get_active(existing[1]))
                self._bind_candidate_trace(todo, run.run_id, existing[1])
                from .acceptance_bridge import freeze_initial_acceptance
                freeze_initial_acceptance(store=self.run_store,run_id=run.run_id,contract=todo.work_contract,
                    session=self.resolve(run_id=run.run_id,contract=todo.work_contract),
                    interpreter=self.acceptance_interpreter,
                    accepted_dependency=self.accepted_dependency(todo,run.run_id))
                return existing
        retained = self._restore_retained_candidate(todo)
        if retained is not None:
            if handoff is not None:
                handoff.validate_consumer(todo,retained[0],self.manager.get_active(retained[1]))
            self._bind_candidate_trace(todo, retained[0], retained[1])
            return retained
        attempt = self._attempt_number(todo.run_id)
        run_id = f"{todo.run_id}-attempt-{attempt}"
        run = Run(run_id, todo.work_contract.goal_id, todo.todo_id, RunStatus.RUNNING, QuotaUsage())
        # Resume an allocation interrupted before worktree acquisition/binding.
        allocated_runs = {lease.run_id for lease in self.manager.store.all()}
        pending = [r for r in self.run_store.list_runs() if r.active_todo_id == todo.todo_id
            and r.status is RunStatus.RUNNING and not r.quota_usage.worker_calls
            and r.run_id not in allocated_runs
            and persisted_responsibility(self.run_store, r.run_id) == responsibility]
        if len(pending) > 1:
            raise ValueError('ambiguous pending execution responsibility')
        if pending:
            run_id = pending[0].run_id
            attempt = int(run_id.rsplit('-attempt-', 1)[-1])
        else:
            self.run_store.create_run(run)
        record_once(self.run_store, run_id, todo.todo_id,
            'EXECUTION_RESPONSIBILITY_V1', responsibility.to_dict())
        record_once(self.run_store, run_id, todo.todo_id,
            'EXECUTION_WORK_CONTRACT_V1', todo.work_contract.to_dict())
        if responsibility.source_amendment_id is not None:
            bind_acceptance_lineage(self.run_store, run_id, todo.work_contract, responsibility, root)
        binding = handoff.bind(todo,run_id) if handoff is not None else None
        lease = self.manager.acquire(
            source_repo_root=self.source_repo_root,
            run_id=run_id,
            todo_id=todo.todo_id,
            base_ref=binding['consumer_base_commit'] if binding else self._base_ref(todo.work_contract.goal_id),
        )
        self._current[todo.todo_id] = (run_id, lease.lease_id)
        if responsibility.source_amendment_id is not None:
            from .candidate_continuity import apply_replan_draft
            apply_replan_draft(self, lease, todo.work_contract, responsibility)
        if handoff is not None:
            handoff.validate_consumer(todo,run_id,lease)
        if self.trace_registry is not None:
            self._bind_candidate_trace(todo, run_id, lease.lease_id)
            if responsibility.source_amendment_id is not None:
                _trace_event(self.trace_registry, 'ACCEPTANCE_LINEAGE_BOUND', run_id=run_id,
                    todo_id=todo.todo_id, payload={'root_run_id': root.run_id,
                        'root_acceptance_fingerprint': fingerprint(root.to_dict()),
                        'responsibility': responsibility.to_dict()})
            _trace_event(self.trace_registry, "CANDIDATE_LEASE_CREATED", run_id=run_id, todo_id=todo.todo_id, payload={"candidate_lease_id": lease.lease_id, "candidate_ref": getattr(lease, "branch_name", None), "base_commit": getattr(lease, "base_commit", None), "attempt_index": attempt})
            _trace_event(self.trace_registry, "CANDIDATE_BASE_SELECTED", run_id=run_id, todo_id=todo.todo_id, payload={"candidate_lease_id": lease.lease_id, "base_commit": getattr(lease, "base_commit", None)})
        from .acceptance_bridge import freeze_initial_acceptance
        frozen = freeze_initial_acceptance(store=self.run_store, run_id=run_id,
            contract=todo.work_contract, session=self.resolve(run_id=run_id, contract=todo.work_contract),
            interpreter=self.acceptance_interpreter,
            accepted_dependency=self.accepted_dependency(todo,run_id))
        if responsibility.source_amendment_id is None:
            _trace_event(self.trace_registry, 'ACCEPTANCE_OBLIGATIONS_FROZEN', run_id=run_id,
                todo_id=todo.todo_id, payload={'fingerprint':frozen.contract_fingerprint,
                    'boundary':'BEFORE_WORKER_EXECUTION'})
        return run_id, lease.lease_id

    def handoff(self, contract):
        from harness.lead.required_graph import REQUIRED_GRAPH
        if REQUIRED_GRAPH not in contract.constraints:
            return None
        from .dependency_handoff import require
        value = self.dependency_handoffs.get(contract.goal_id)
        require(value is not None,'DEPENDENCY_ADMISSION_UNAVAILABLE','Mission must be bound before allocation')
        return value

    def accepted_dependency(self,todo,run_id):
        handoff=self.handoff(todo.work_contract)
        if handoff is None or not todo.dependencies:
            return None
        return handoff.public_binding(todo,run_id,self.manager.get_active(self._current[todo.todo_id][1]))

    def validate_dependency_context(self,todo,run_id,text):
        handoff=self.handoff(todo.work_contract)
        if handoff is not None and todo.dependencies:
            handoff.validate_context(todo,run_id,text)

    def _bind_candidate_trace(self, todo, run_id, lease_id):
        if self.trace_registry is None:
            return
        from .mission_trace_observer import MissionTraceCorrelationV1, MissionTraceCorrelationError
        try:
            prior = self.trace_registry.correlation_for_run(run_id)
        except MissionTraceCorrelationError:
            goal = self.trace_registry.correlation_for_goal(todo.work_contract.goal_id)
            attempt = int(run_id.rsplit('-attempt-', 1)[-1]) if '-attempt-' in run_id else None
            self.trace_registry.bind_run(MissionTraceCorrelationV1(
                goal.mission_id, goal.goal_id, todo.todo_id, run_id, attempt))
        else:
            if (prior.goal_id != todo.work_contract.goal_id or prior.todo_id != todo.todo_id
                or prior.candidate_lease_id not in (None, lease_id)):
                raise MissionTraceCorrelationError('conflicting restored candidate trace binding')
        self.trace_registry.bind_candidate(run_id, lease_id)

    def _responsibility(self, contract):
        from .execution_responsibility import ExecutionResponsibilityV1, StaleResponsibilityError
        from ..verification.acceptance import fingerprint
        store = getattr(self, 'scheduler_store', None)
        revision = store.responsibility_revision(contract.todo_id) if store is not None else None
        if store is not None and store.get_todo(contract.todo_id).objective != contract.objective:
            raise StaleResponsibilityError('STALE_RESPONSIBILITY: scheduler objective differs')
        if revision is not None and revision['objective'] != contract.objective:
            raise StaleResponsibilityError('STALE_RESPONSIBILITY: amendment objective differs')
        return ExecutionResponsibilityV1(contract.todo_id, fingerprint(contract.to_dict()),
            revision['amendment_id'] if revision else None, revision['version'] if revision else None)

    def _restore_retained_candidate(self, todo: WorkflowTodoV1) -> tuple[str, str] | None:
        """Find only a lease whose durable checkpoint validates against this Todo contract."""
        for lease in self.manager.reconcile():
            if lease.todo_id != todo.todo_id:
                continue
            try:
                run = self.run_store.get_run(lease.run_id)
                if (
                    run.active_todo_id == todo.todo_id
                    and run.status is RunStatus.NEEDS_ATTENTION
                    and candidate_restart_authorized(
                        artifact_store=self.artifact_store, run_store=self.run_store, manager=self.manager, run=run,
                        lease=lease, contract=todo.work_contract,
                    )
                ):
                    self._current[todo.todo_id] = (run.run_id, lease.lease_id)
                    return self._current[todo.todo_id]
            except Exception:
                continue
        return None

    def current(self, todo_id: str) -> tuple[str, Any]:
        if todo_id not in self._current:
            raise RuntimeError(f"no active worktree attempt for Todo {todo_id}")
        run_id, lease_id = self._current[todo_id]
        from .execution_responsibility import persisted_responsibility, StaleResponsibilityError
        run = self.run_store.get_run(run_id)
        bound = persisted_responsibility(self.run_store, run_id)
        store = getattr(self, 'scheduler_store', None)
        revision = store.responsibility_revision(todo_id) if store is not None else None
        if run.status is RunStatus.CANCELLED or (store is not None and (
            (revision is not None and (bound is None
                or bound.source_amendment_id != revision['amendment_id']
                or bound.amendment_version != revision['version']))
            or (revision is None and bound is not None and bound.source_amendment_id is not None))):
            raise StaleResponsibilityError('STALE_RESPONSIBILITY: candidate authority retired')
        handoff = self.dependency_handoffs.get(run.goal_id)
        lease = (handoff.active_lease(handoff.todo(todo_id),run_id,lease_id)
                 if handoff is not None else self.manager.get_active(lease_id))
        return run_id, lease

    def resolve(self, *, run_id: str, contract) -> Any:
        from .execution_responsibility import persisted_responsibility, StaleResponsibilityError
        run = self.run_store.get_run(run_id)
        bound = persisted_responsibility(self.run_store, run_id)
        current_responsibility = self._responsibility(contract)
        if run.status is RunStatus.CANCELLED or (bound is not None and bound != current_responsibility):
            raise StaleResponsibilityError('STALE_RESPONSIBILITY: Run cannot resolve candidate')
        current_run_id, lease = self.current(contract.todo_id)
        if current_run_id != run_id:
            raise RuntimeError("workspace attempt Run identity mismatch")
        run = self.run_store.get_run(run_id)
        todo = self.run_store.get_todo(contract.todo_id)
        return self.factory.create(run=run, todo=todo, contract=contract, root=lease.worktree_root)

    def rotate(self, todo: WorkflowTodoV1) -> tuple[str, str]:
        current = self._current.pop(todo.todo_id, None)
        if current is not None:
            try:
                self.manager.release(
                    current[1],
                    force=True,
                    delete_branch=True,
                )
            except Exception:
                # Release failed. Ownership must not be forgotten: otherwise
                # ensure() may create a second attempt while the previous
                # physical candidate still exists.
                self._current[todo.todo_id] = current
                raise
        return self.ensure(todo)

    def completed_run_id(self, todo_id: str) -> str | None:
        runs = getattr(self.run_store, "_runs", {})
        completed = [
            run.run_id for run in runs.values()
            if run.active_todo_id == todo_id and run.status is RunStatus.COMPLETED
        ]
        return sorted(completed)[-1] if completed else None

    def complete_accepted_run(self, todo_id: str) -> str:
        """Durably finalize the exact accepted execution attempt.

        WorkflowTodo.run_id is the stable scheduler/base identity. Production
        execution creates a distinct ``-attempt-N`` Control Plane Run. A Todo
        must not become scheduler-SUCCEEDED until that exact accepted attempt
        is durably COMPLETED.

        The fallback to completed_run_id() makes this transition idempotent
        across a crash after Run completion but before scheduler completion.
        """
        current = self._current.get(todo_id)

        if current is not None:
            run_id, _lease_id = current
            run = self.run_store.get_run(run_id)

            if run.active_todo_id != todo_id:
                raise RuntimeError(
                    "accepted Run active Todo identity mismatch"
                )

            if run.status is RunStatus.COMPLETED:
                return run.run_id

            if run.status is not RunStatus.RUNNING:
                raise RuntimeError(
                    "accepted Run must be RUNNING or COMPLETED "
                    f"before scheduler success; got {run.status.value}"
                )

            self.run_store.update_run(
                Run(
                    run.run_id,
                    run.goal_id,
                    run.active_todo_id,
                    RunStatus.COMPLETED,
                    run.quota_usage,
                )
            )
            return run.run_id

        # Crash-safe replay: if the process died after completing the Run but
        # before the scheduler terminal transition, _restore_active() will not
        # repopulate a COMPLETED Run. Recover that exact durable completion.
        completed = self.completed_run_id(todo_id)
        if completed is not None:
            return completed

        raise RuntimeError(
            f"no accepted execution Run available for Todo {todo_id}"
        )

    def commit_current(self, *, goal_id: str, epoch: int) -> ProjectSnapshotV1:
        if self.active_todo_id is None:
            raise RuntimeError("no active Todo for candidate snapshot")
        _, lease = self.current(self.active_todo_id)
        from .execution_responsibility import evidence_payloads, record_once
        from ..verification.acceptance import fingerprint
        before = self.manager.snapshot(lease.lease_id)
        accepted = None
        if getattr(self, '_review_authority_required', False):
            rows = evidence_payloads(self.run_store, lease.run_id, 'ACCEPTED_CANDIDATE_AUTHORITY_V1')
            accepted = rows[-1] if rows else None
            if (accepted is None or accepted['lease_id'] != lease.lease_id
                    or accepted['content_sha256'] != getattr(before, 'content_sha256', None)):
                raise VerificationStateError('accepted export does not match judged raw candidate')
            exports = evidence_payloads(self.run_store, lease.run_id, 'ACCEPTED_EXPORT_V1')
            if accepted['head'] != before.head_commit or accepted['diff_sha256'] != before.diff_sha256:
                if not exports or exports[-1]['accepted_head'] != before.head_commit:
                    raise VerificationStateError('candidate revision changed after Judge ACCEPT')
        root = Path(lease.worktree_root)
        status = _git(root, "status", "--porcelain=v1", "--untracked-files=all")
        if status:
            _git(root, "add", "--all")
            _git(
                root,
                "-c", "user.name=Swarm Harness",
                "-c", "user.email=swarm@example.invalid",
                "commit", "-m", f"swarm verified {self.active_todo_id}",
            )
        snapshot = self.manager.snapshot(lease.lease_id)
        if accepted is not None:
            if getattr(snapshot, 'content_sha256', None) != accepted['content_sha256']:
                raise VerificationStateError('commit changed judged candidate bytes')
            record_once(self.run_store, lease.run_id, lease.todo_id, 'ACCEPTED_EXPORT_V1', {
                'lease_id':lease.lease_id, 'accepted_head':snapshot.head_commit,
                'content_sha256':getattr(snapshot, 'content_sha256', None), 'files':[list(r) for r in getattr(snapshot, 'file_sha256', ())],
                'judge_binding_fingerprint':fingerprint(accepted),
                'verification_report_id':accepted['verification_report_id'],
                'critic_report_id':accepted['critic_report_id'], 'judge_report_id':accepted['judge_report_id']})
        return ProjectSnapshotV1(
            goal_id,
            epoch,
            snapshot.head_commit,
            snapshot.diff_sha256,
            snapshot.dirty_paths,
        )


class VerificationTodoOverlayRunStore:
    """Delegate RunStore operations while exposing safety-normalized Todos.

    OrnithLead durably creates the original Control Plane Todo before the
    integration adapter can add mandatory gates. Verification is the only
    consumer that needs the normalized projection; all writes still go to the
    authoritative FileRunStore.
    """

    def __init__(self, base) -> None:
        self.base = base
        self._todos: dict[str, Any] = {}

    def project(self, todos) -> None:
        for todo in todos:
            original = self.base.get_todo(todo.todo_id)
            if original.goal_id != todo.goal_id or original.objective != todo.objective:
                raise RuntimeError(f"normalized Todo identity mismatch: {todo.todo_id}")
            self._todos[todo.todo_id] = todo

    def get_todo(self, todo_id: str):
        return self._todos.get(todo_id) or self.base.get_todo(todo_id)

    def __getattr__(self, name: str):
        return getattr(self.base, name)

class WorkflowSchedulerAdapter:
    def __init__(
        self,
        *,
        coordinator: SchedulerCoordinator,
        registry: DurableWorkItemRegistry,
        workspace: ProductionWorkspaceCoordinator,
        todo_projection=None,
        heartbeat_interval_seconds: float | None = None,
    ) -> None:
        self.coordinator = coordinator
        self.store = coordinator.store
        self.registry = registry
        self.workspace = workspace
        if isinstance(workspace, ProductionWorkspaceCoordinator):
            workspace.scheduler_store = self.store
        self.todo_projection = todo_projection
        self._specs: dict[str, MissionSpecV1] = {}

        if heartbeat_interval_seconds is None:
            heartbeat_interval_seconds = max(
                1.0,
                min(
                    300.0,
                    float(self.coordinator.policy.claim_ttl_seconds) / 3.0,
                ),
            )
        if heartbeat_interval_seconds <= 0:
            raise ValueError("heartbeat_interval_seconds must be positive")

        self._heartbeat_interval_seconds = float(
            heartbeat_interval_seconds
        )
        self._heartbeat_stops: dict[str, threading.Event] = {}
        self._heartbeat_threads: dict[str, threading.Thread] = {}
        self._heartbeat_errors: dict[str, Exception] = {}

    @staticmethod
    def _claim_owner(todo_id: str) -> str:
        return f"phase16-{todo_id}"

    def _start_heartbeat(
        self,
        *,
        todo_id: str,
        claim_id: str,
    ) -> None:
        # begin() can be replayed for an already RUNNING Todo. Never leave
        # duplicate keepers for the same scheduler claim.
        self._stop_heartbeat(todo_id, raise_error=False)

        owner_id = self._claim_owner(todo_id)

        # Renew synchronously first. This validates ownership and ensures that
        # a resumed RUNNING Todo does not enter expensive work with a nearly
        # expired claim.
        self.coordinator.heartbeat(
            claim_id,
            owner_id=owner_id,
        )

        stop = threading.Event()

        def keep_alive() -> None:
            while not stop.wait(self._heartbeat_interval_seconds):
                try:
                    self.coordinator.heartbeat(
                        claim_id,
                        owner_id=owner_id,
                    )
                except Exception as exc:
                    self._heartbeat_errors[todo_id] = exc
                    return

        thread = threading.Thread(
            target=keep_alive,
            name=f"phase16-claim-heartbeat-{todo_id}",
            daemon=True,
        )

        self._heartbeat_stops[todo_id] = stop
        self._heartbeat_threads[todo_id] = thread
        thread.start()

    def _stop_heartbeat(
        self,
        todo_id: str,
        *,
        raise_error: bool,
    ) -> None:
        stop = self._heartbeat_stops.pop(todo_id, None)
        thread = self._heartbeat_threads.pop(todo_id, None)

        if stop is not None:
            stop.set()

        if thread is not None:
            join_timeout = max(
                2.0,
                float(self.coordinator.policy.lock_timeout_seconds) + 1.0,
            )
            thread.join(timeout=join_timeout)

            if thread.is_alive() and raise_error:
                raise RuntimeError(
                    f"scheduler heartbeat did not stop for Todo {todo_id}"
                )

        error = self._heartbeat_errors.pop(todo_id, None)
        if error is not None and raise_error:
            raise RuntimeError(
                f"scheduler heartbeat failed for Todo {todo_id}: {error}"
            ) from error

    def bind_spec(self, spec: MissionSpecV1) -> None:
        self._specs[spec.goal_id] = spec
        from ..workflow.root_projection import VERIFIED_MANDATORY_CONTEXT_PROJECTION_V1, VerifiedRootProjectionAuthority
        if VERIFIED_MANDATORY_CONTEXT_PROJECTION_V1 in spec.global_constraints:
            spine = self.workspace.spine
            inner = getattr(spine, 'inner', spine)
            authority = getattr(inner, 'root_projection_authority', None)
            if authority is None:
                inner.root_projection_authority = VerifiedRootProjectionAuthority(
                    inner, lambda goal_id: self._specs.get(goal_id), self._items, spec,
                    ContextProfileRouterAdapter.ORDER)
            else:
                authority.admit(spec)
        todos = self.registry.load_control_todos(spec)
        if self.todo_projection is not None:
            self.todo_projection(todos)
        if not self.store.list_todos(spec.goal_id):
            work_items = self.registry.load_work_items(spec)
            run_ids = {item.todo_id: item.run_id for item in work_items}
            self.coordinator.register_goal(todos, run_ids=run_ids)
        from harness.lead.required_graph import REQUIRED_GRAPH
        if REQUIRED_GRAPH in spec.global_constraints:
            from .dependency_handoff import DependencyHandoff
            handoff=DependencyHandoff(self.workspace,self,spec)
            self.workspace.dependency_handoffs[spec.goal_id]=handoff
            self.workspace.spine.install_dependency_handoff(self._dependency_context,self._record_dependency_context)

    def _dependency_context(self,goal_id,todo_id):
        handoff=self.workspace.dependency_handoffs.get(goal_id)
        return handoff.context(handoff.todo(todo_id)) if handoff is not None else None

    def _record_dependency_context(self,goal_id,todo_id,objective,pack):
        handoff=self.workspace.dependency_handoffs.get(goal_id)
        if handoff is not None:
            handoff.record_context(handoff.todo(todo_id),objective,pack)

    def prepare_success(self,todo,verification,review):
        handoff=self.workspace.handoff(todo.work_contract) if isinstance(self.workspace,ProductionWorkspaceCoordinator) else None
        return handoff.prepare(todo,verification,review) if handoff is not None else None

    def publish_success(self,todo,snapshot,outcome_ref):
        handoff=self.workspace.handoff(todo.work_contract) if isinstance(self.workspace,ProductionWorkspaceCoordinator) else None
        if handoff is not None:
            handoff.publish(todo,snapshot,outcome_ref)

    def resume_success(self,todo):
        handoff=self.workspace.handoff(todo.work_contract) if isinstance(self.workspace,ProductionWorkspaceCoordinator) else None
        if handoff is None:
            return None
        receipt=handoff.resume(todo)
        if receipt is None:
            return False
        self._finish(todo,SchedulerTodoStatus.SUCCEEDED,receipt['verification_report_id'])
        handoff.resolve(todo)
        return True

    def load_plan(self, spec: MissionSpecV1) -> PlanObservationV1 | None:
        return self.registry.load_plan(spec)

    def save_plan(self, spec, result, observation) -> None:
        self.registry.save_plan(spec, result, observation)
        from harness.lead.required_graph import REQUIRED_GRAPH
        if REQUIRED_GRAPH not in spec.global_constraints:
            self.bind_spec(spec)
        # Opt-in graphs require their durable qualification/commit receipt first.
        # Production workflow.run binds the Mission before any scheduler dispatch.

    def _items(self, goal_id: str) -> dict[str, WorkflowTodoV1]:
        spec = self._specs.get(goal_id)
        if spec is None:
            raise RuntimeError(f"Phase16 scheduler is not bound to goal {goal_id}")
        states = {state.todo_id: state for state in self.store.list_todos(goal_id)}
        items = {}
        for item in self.registry.load_work_items(spec):
            state = states[item.todo_id]
            if state.objective != item.objective:
                item = replace(
                    item, objective=state.objective,
                    work_contract=replace(item.work_contract, objective=state.objective),
                )
            items[item.todo_id] = item
        return items

    def ready(self, goal_id: str, *, limit: int) -> tuple[WorkflowTodoV1, ...]:
        states = self.store.refresh_readiness(goal_id)
        candidates = [
            state for state in states
            if state.status in {SchedulerTodoStatus.READY, SchedulerTodoStatus.RUNNING}
        ]
        candidates.sort(key=lambda value: (-value.priority, value.todo_id.casefold()))
        items = self._items(goal_id)
        return tuple(items[state.todo_id] for state in candidates[:limit])

    def begin(self, todo: WorkflowTodoV1) -> None:
        state = self.store.get_todo(todo.todo_id)

        if state.status is SchedulerTodoStatus.READY:
            claim = self.store.claim(
                todo.todo_id,
                self._claim_owner(todo.todo_id),
                now=self.coordinator.clock(),
                ttl_seconds=self.coordinator.policy.claim_ttl_seconds,
                max_attempts=self.coordinator.policy.max_attempts_per_todo,
                max_active_claims=self.coordinator.policy.max_active_claims,
            )
            self.coordinator.start(claim.claim_id)

        elif state.status is not SchedulerTodoStatus.RUNNING:
            raise RuntimeError(
                f"Todo {todo.todo_id} is not runnable: {state.status.value}"
            )

        state = self.store.get_todo(todo.todo_id)
        if (
            state.status is not SchedulerTodoStatus.RUNNING
            or not state.active_claim_id
        ):
            raise RuntimeError(
                f"Todo {todo.todo_id} has no active RUNNING scheduler claim"
            )

        self._start_heartbeat(
            todo_id=todo.todo_id,
            claim_id=state.active_claim_id,
        )

        try:
            self.workspace.ensure(todo)
        except Exception:
            self._stop_heartbeat(
                todo.todo_id,
                raise_error=False,
            )
            raise

    def _finish(
        self,
        todo: WorkflowTodoV1,
        status: SchedulerTodoStatus,
        report_id: str,
        reason: str | None = None,
    ) -> None:
        # Keep the claim alive throughout execution/review/recovery. Stop the
        # background keeper only when the Todo is about to make its terminal
        # scheduler transition.
        self._stop_heartbeat(
            todo.todo_id,
            raise_error=True,
        )

        state = self.store.get_todo(todo.todo_id)

        if state.status is status:
            return

        if not state.active_claim_id:
            raise RuntimeError(
                f"Todo {todo.todo_id} has no active claim while finishing"
            )

        # Final synchronous renewal closes the race between stopping the
        # keeper and wait_decision()/finish_claim().
        self.coordinator.heartbeat(
            state.active_claim_id,
            owner_id=self._claim_owner(todo.todo_id),
        )

        if state.status is SchedulerTodoStatus.RUNNING:
            self.coordinator.wait_decision(
                state.active_claim_id
            )
            state = self.store.get_todo(todo.todo_id)

        if (
            state.status is not SchedulerTodoStatus.WAITING_DECISION
            or not state.active_claim_id
        ):
            raise RuntimeError(
                f"Todo {todo.todo_id} cannot finish from "
                f"{state.status.value}"
            )

        self.store.finish_claim(
            claim_id=state.active_claim_id,
            now=self.coordinator.clock(),
            status=status,
            last_judge_report_id=report_id,
            next_action=(
                JudgeAction.NONE
                if status is SchedulerTodoStatus.SUCCEEDED
                else JudgeAction.REPLAN
                if status is SchedulerTodoStatus.REPLAN_REQUIRED
                else JudgeAction.STOP
            ),
            blocked_reason=reason,
        )

    def mark_succeeded(self, todo, verification, outcome_artifact_id) -> None:
        # The verified outcome and project snapshot are already durable when
        # workflow calls this method. Complete the exact execution attempt
        # before exposing scheduler SUCCEEDED so dependent Todos can resolve
        # an authoritative COMPLETED dependency Run.
        handoff=self.workspace.handoff(todo.work_contract) if isinstance(self.workspace,ProductionWorkspaceCoordinator) else None
        if handoff is not None:
            receipt=handoff.commit(todo)
            from .dependency_handoff import require
            require(receipt['accepted_outcome_reference']==outcome_artifact_id,
                    'DEPENDENCY_OUTCOME_MISMATCH','scheduler success')
        else:
            self.workspace.complete_accepted_run(todo.todo_id)
        self._finish(todo, SchedulerTodoStatus.SUCCEEDED, verification.report_id)

    def mark_retryable(self, todo, reason) -> None:
        self._finish(todo, SchedulerTodoStatus.REPLAN_REQUIRED, f"replan-{todo.todo_id}", reason)

    def mark_failed(self, todo, reason) -> None:
        self._finish(todo, SchedulerTodoStatus.FAILED, f"failed-{todo.todo_id}", reason)

    def all_succeeded(self, goal_id: str) -> bool:
        states = self.store.list_todos(goal_id)
        handoff=self.workspace.dependency_handoffs.get(goal_id) if isinstance(self.workspace,ProductionWorkspaceCoordinator) else None
        if handoff is not None:
            for state in states:
                if state.status is SchedulerTodoStatus.SUCCEEDED:
                    handoff.resolve(handoff.todo(state.todo_id))
        return bool(states) and all(state.status is SchedulerTodoStatus.SUCCEEDED for state in states)

    def succeeded_todo_ids(self, goal_id: str) -> tuple[str, ...]:
        """Return durable successes in the immutable plan order."""

        spec = self._specs.get(goal_id)
        if spec is None:
            raise RuntimeError(f"Phase16 scheduler is not bound to goal {goal_id}")
        plan = self.registry.load_plan(spec)
        if plan is None:
            return ()
        states = {state.todo_id: state for state in self.store.list_todos(goal_id)}
        return tuple(
            todo_id
            for todo_id in plan.todo_ids
            if todo_id in states and states[todo_id].status is SchedulerTodoStatus.SUCCEEDED
        )

    def terminal_reason(self, goal_id: str) -> str | None:
        terminal = [
            state for state in self.store.list_todos(goal_id)
            if state.status in {
                SchedulerTodoStatus.FAILED,
                SchedulerTodoStatus.BLOCKED,
                SchedulerTodoStatus.HUMAN_REVIEW,
                SchedulerTodoStatus.CANCELLED,
            }
        ]
        if not terminal:
            return None
        return "; ".join(f"{item.todo_id}:{item.status.value}:{item.blocked_reason or ''}" for item in terminal)

    def pending_replan_todo_id(self, goal_id: str) -> str | None:
        states = {state.todo_id: state for state in self.store.list_todos(goal_id)}
        pending = {state.todo_id for state in states.values()
                   if state.status is SchedulerTodoStatus.REPLAN_REQUIRED}
        for ref in self.registry.artifact_store.list_refs():
            artifact = self.registry.artifact_store.get_artifact(ref.artifact_id)
            if (artifact.metadata.get("phase16_role") != "RECOVERY_CONTEXT"
                    or artifact.metadata.get("goal_id") != goal_id):
                continue
            todo_id = artifact.metadata.get("todo_id")
            if todo_id not in states or self.store.has_applied_amendment(_judge_replan_amendment_id(artifact.artifact_id)):
                continue
            payload = json.loads(artifact.content)
            if payload.get("recovery", {}).get("semantic_replan_required") is True:
                pending.add(todo_id)
        if len(pending) > 1:
            raise RuntimeError("multiple pending semantic replans require explicit resolution")
        return next(iter(pending)) if pending else None

    def ensure_replan_required(self, todo: WorkflowTodoV1, reason: str) -> None:
        state = self.store.get_todo(todo.todo_id)
        if state.status in {SchedulerTodoStatus.RUNNING, SchedulerTodoStatus.WAITING_DECISION}:
            self.mark_retryable(todo, reason)
        else:
            self.store.mark_replan_required(todo_id=todo.todo_id, reason=reason)

    def ensure_pending_replan_required(self, goal_id: str, todo_id: str) -> None:
        state = self.store.get_todo(todo_id)
        if state.goal_id != goal_id:
            raise RuntimeError("pending replan belongs to another goal")
        if state.status is not SchedulerTodoStatus.REPLAN_REQUIRED:
            self.ensure_replan_required(
                self._items(goal_id)[todo_id],
                "durable Judge REPLAN awaiting DAG amendment",
            )


class WorkflowRepositoryAdapter:
    def __init__(self, *, service: RepositoryIntelligenceService, bridge: RepositoryContextBridge, workspace: ProductionWorkspaceCoordinator, spine=None) -> None:
        self.service = service
        self.bridge = bridge
        self.workspace = workspace
        self.spine = spine

    def _goal_capsule(self, goal_id: str) -> str:
        """Return only the already-bounded root capsule for repository ranking."""
        if self.spine is None:
            return ""
        try:
            refs = self.spine.root_context(goal_id)
            artifact = self.spine.artifact_store.get_artifact(refs.capsule.artifact_id)
            payload = json.loads(artifact.content)
            value = payload.get("goal_capsule")
            return value if isinstance(value, str) else ""
        except (AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return ""

    def retrieve(self, todo, *, objective, expansion_queries=()) -> RepositoryContextV1:
        _, lease = self.workspace.current(todo.todo_id)
        index = self.service.build_index(lease.lease_id)
        query = objective
        if expansion_queries:
            query += "\n" + "\n".join(expansion_queries)
        goal_context = self._goal_capsule(todo.work_contract.goal_id)
        grounding_query = query if not goal_context else f"{query}\n{goal_context}"
        explicit = tuple(todo.work_contract.allowed_scope)
        retrieval = self.service.retrieve(index, grounding_query, explicit_paths=explicit)
        retrieval = repair_repository_grounding(
            index, retrieval, grounding_query,
            max_results=min(self.bridge.policy.max_results, self.bridge.policy.max_context_files),
            max_bytes=self.bridge.policy.max_context_bytes,
        )
        refs = self.bridge.materialize(index, retrieval)
        known = tuple(hit.path for hit in retrieval.hits)
        return RepositoryContextV1(tuple(refs), known, retrieval.retrieval_id)

class ContextProfileRouterAdapter:
    """Deterministic context-profile selection; AgentLoop retains model routing authority."""

    ORDER = ("deepseek_micro", "iquest_repo", "qwen38_heavy_fast", "qwen36_heavy_quality")

    def __init__(self, policy: WorkflowExecutionPolicy) -> None:
        self.policy = policy

    @staticmethod
    def _tier_value(spec: object) -> str:
        if isinstance(spec, RoutingTaskSpec):
            if not isinstance(spec.complexity, Complexity):
                raise TypeError(f"RoutingTaskSpec.complexity must be Complexity enum, got {type(spec.complexity).__name__}")
            value = spec.complexity.value
        elif isinstance(spec, TaskProfileV2):
            if not isinstance(spec.difficulty, BenchmarkDifficulty):
                raise TypeError(f"TaskProfileV2.difficulty must be BenchmarkDifficulty enum, got {type(spec.difficulty).__name__}")
            value = spec.difficulty.value
        else:
            raise TypeError(f"unsupported routing task spec type: {type(spec).__name__}")

        if not isinstance(value, str):
            raise ValueError(f"expected string tier value, got {type(value).__name__}")
        return value

    def select(self, todo, repository, *, objective, failed_profile_ids) -> ProfileSelectionV1:
        complexity = self._tier_value(todo.routing_task_spec)
        if complexity == "trivial":
            first = 0
        elif complexity == "normal":
            first = 1
        elif complexity == "hard":
            first = 2
        else:
            first = 3
        preferred = [
            name
            for name in self.ORDER[first:]
            if name in self.policy.profile_contexts
        ]
        candidates = [
            name
            for name in preferred
            if name not in failed_profile_ids
        ]

        # Context-profile failures are bounded to the current recovery cycle.
        # Production AgentLoop independently owns worker/model escalation.
        #
        # A fresh recovery recovery adds evidence/context, so it must never
        # downshift a normal/hard Todo into a smaller context class merely
        # because the preferred context profiles were exercised previously.
        # Retry the original complexity-appropriate context range instead.
        if not candidates:
            candidates = preferred

        if not candidates:
            raise RuntimeError(
                "no Phase16 context profile configured for task complexity"
            )

        return ProfileSelectionV1(
            candidates[0],
            tuple(candidates[1:]),
            "rule-based context profile cascade",
        )


class WorkflowExecutionAdapter:
    def __init__(self, *, agent: ControlPlaneCodingAgent, workspace: ProductionWorkspaceCoordinator, policy: WorkflowExecutionPolicy, artifact_store: ArtifactStore | None = None, execution_slice_policy: ProductionExecutionSlicePolicy | None = None, trace_registry=None) -> None:
        self.execution_slice_policy = execution_slice_policy or ProductionExecutionSlicePolicy()
        self.trace_registry = trace_registry
        self.workspace = workspace
        self._tool_failure_request_ids: dict[str, str] = {}
        self._tool_failure_window_enabled = isinstance(agent, ControlPlaneCodingAgent)
        self.agent = (
            slice_aware_agent(
                agent,
                self.execution_slice_policy,
                candidate_lease_provider=lambda todo_id: self.workspace.current(todo_id)[1].lease_id,
                tool_failure_request_provider=self._tool_failure_request_id_for,
            )
            if isinstance(agent, ControlPlaneCodingAgent)
            else agent
        )
        self.policy = policy
        self.artifact_store = artifact_store or getattr(getattr(self.agent, "loop", None), "artifact_store", None)
        if self.artifact_store is None:
            raise ValueError("artifact_store is required for execution slices")

    def _agent_routing_spec(self, todo) -> RoutingTaskSpec:
        'Keep recovery routing and AgentLoop model routing as separate contracts.\n\n        recovery recovery may replace ``todo.routing_task_spec`` with\n        ``TaskProfileV2`` for workflow recovery/context-profile selection.\n        CodingAgentLoop still owns model routing through ``RoutingTaskSpec``.\n        For a recovery view, restore the original immutable Lead routing\n        contract from the existing durable work-item artifact.\n        '
        routing = todo.routing_task_spec
        if isinstance(routing, RoutingTaskSpec):
            return routing
        if not isinstance(routing, TaskProfileV2):
            raise TypeError(
                "Phase16 execution routing must be RoutingTaskSpec or TaskProfileV2 recovery view"
            )
        if routing.task_id != todo.todo_id:
            raise RuntimeError(
                "TaskProfileV2 task identity mismatch at Phase16 execution boundary"
            )

        original = DurableWorkItemRegistry(self.artifact_store).load_original_work_item(
            goal_id=todo.work_contract.goal_id,
            todo_id=todo.todo_id,
        )
        if not isinstance(original.routing_task_spec, RoutingTaskSpec):
            raise TypeError("durable original work item does not contain RoutingTaskSpec")
        return original.routing_task_spec

    def _tool_failure_request_id_for(self, todo_id: str) -> str:
        return self._tool_failure_request_ids.get(todo_id, "")

    @staticmethod
    def _is_exact_worker_call_exhaustion(result) -> bool:
        from ..agent_loop import AgentOutcome
        return result.outcome is AgentOutcome.QUOTA_EXHAUSTED and result.reason == "worker call quota exhausted"

    def _reopen_for_slice(self, run_id: str) -> None:
        current = self.workspace.run_store.get_run(run_id)
        if current.status in (RunStatus.NEEDS_ATTENTION, RunStatus.FAILED):
            self.workspace.run_store.update_run(
                Run(current.run_id, current.goal_id, current.active_todo_id, RunStatus.RUNNING, current.quota_usage)
            )

    def _semantic_discovery_stop_recorded(self, run_id: str, todo_id: str) -> bool:
        """Read the durable ToolResult boundary rather than an in-memory exception."""
        for event in reversed(self.workspace.run_store.list_events(run_id)):
            payload = event.to_dict().get("payload", {})
            if payload.get("todo_id") != todo_id or payload.get("evidence_type") != "TOOL_RESULT":
                continue
            detail = payload.get("payload", {})
            metadata = detail.get("metadata", {}) if isinstance(detail, Mapping) else {}
            if isinstance(metadata, Mapping) and metadata.get("semantic_discovery_stop") is True:
                return True
        return False
    def execute(self, todo, *, profile_id, objective_with_context, attempt_index, fresh_workspace) -> ExecutionObservationV1:
        from ..workflow.root_projection import VERIFIED_MANDATORY_CONTEXT_PROJECTION_V1
        spine = getattr(self.workspace, 'spine', None)
        authority = getattr(spine, 'root_projection_authority', None)
        if (VERIFIED_MANDATORY_CONTEXT_PROJECTION_V1 in todo.work_contract.constraints or
                authority is not None and todo.work_contract.goal_id in authority.admitted_missions):
            if authority is None:
                raise RuntimeError('ROOT_PROJECTION_INVALID: authority missing at dispatch')
            authority.validate_dispatch(todo, profile_id, objective_with_context)
        handoff=self.workspace.handoff(todo.work_contract) if isinstance(self.workspace,ProductionWorkspaceCoordinator) else None
        if handoff is not None:
            active_run,_=self.workspace.current(todo.todo_id)
            self.workspace.validate_dependency_context(todo,active_run,objective_with_context)
        typed = todo.work_contract.constraints.get("TYPED_SCENARIO_EXECUTION", False)
        if type(typed) is not bool:
            raise ValueError("TYPED_SCENARIO_EXECUTION must be bool")
        if typed:
            adapter = getattr(self, "typed_executor", None)
            if adapter is None:
                raise RuntimeError("typed scenario executor is not configured")
            return adapter.execute(todo, profile_id=profile_id, objective_with_context=objective_with_context,
                                   attempt_index=attempt_index, fresh_workspace=fresh_workspace)
        run_id, lease = self.workspace.current(todo.todo_id)
        self._reopen_for_slice(run_id)
        repair_activation = latest_repair_activation(
            self.workspace.run_store, self.artifact_store,
            run_id=run_id, todo_id=todo.todo_id, lease_id=lease.lease_id,
        )
        repair_directive = "" if repair_activation is None else "\n\n" + repair_activation.directive()
        contract = replace(
            todo.work_contract,
            objective=objective_with_context + repair_directive + "\n\n" + SEARCH_TEXT_WORKER_GUIDANCE,
        )
        profile = self.policy.profile(profile_id)
        dependencies = {
            dependency: run_id_value
            for dependency in todo.dependencies
            if (run_id_value := self.workspace.completed_run_id(dependency)) is not None
        }

        dependency_context_refs = ()
        spine = getattr(self.workspace, "spine", None)
        verified_outcome_ref = getattr(
            spine,
            "verified_outcome_ref",
            None,
        )

        if callable(verified_outcome_ref) and handoff is None:
            dependency_context_refs = tuple(
                verified_outcome_ref(
                    contract.goal_id,
                    dependency,
                )
                for dependency in sorted(dependencies)
            )
            dependencies = _project_verified_dependency_run_ids(
                dependencies=dependencies,
                dependency_context_refs=dependency_context_refs,
                spine=spine,
                goal_id=contract.goal_id,
            )
        if handoff is not None and todo.dependencies:
            resolved=handoff.prerequisites(todo)
            dependencies={d['producer_physical_todo_id']:d['producer_run_id'] for d,_ in resolved}
            dependency_context_refs=handoff.execution_context_refs(todo,run_id)

        checkpoint_ids: list[str] = []
        initial_calls = self.workspace.run_store.get_run(run_id).quota_usage.worker_calls
        start_slice = max(1, min(self.execution_slice_policy.max_slices_per_todo, initial_calls // self.execution_slice_policy.max_worker_calls_per_slice + 1))
        evaluator = ExecutionSliceEvaluator(self.execution_slice_policy)
        result = None
        last_eval = None
        repair_tool_calls_before = 0
        try:
            for slice_index in range(start_slice, self.execution_slice_policy.max_slices_per_todo + 1):
                if handoff is not None:
                    self.workspace.validate_dependency_context(todo,run_id,objective_with_context)
                if self.trace_registry is not None:
                    self.trace_registry.bind_slice(run_id, slice_index)
                worker_calls_before = self.workspace.run_store.get_run(run_id).quota_usage.worker_calls
                request_id = f"{run_id}:phase16:{attempt_index}:slice:{slice_index}"
                if self._tool_failure_window_enabled:
                    self._tool_failure_request_ids[todo.todo_id] = request_id
                    persist_workflow_execution_window(
                        run_store=self.workspace.run_store,
                        artifact_store=self.artifact_store,
                        run_id=run_id,
                        todo_id=todo.todo_id,
                        lease_id=lease.lease_id,
                        request_id=request_id,
                    )
                if repair_activation is not None:
                    repair_tool_calls_before = sum(
                        1 for event in self.workspace.run_store.list_events(run_id)
                        if event.to_dict().get("payload", {}).get("evidence_type") == "TOOL_RESULT"
                    )
                    record_repair_directive_presented(
                        run_store=self.workspace.run_store, run_id=run_id, todo_id=todo.todo_id,
                        activation=repair_activation, request_id=request_id,
                    )
                result = self.agent.execute(
                    run_id=run_id,
                    contract=slice_contract(contract, self.execution_slice_policy, slice_index),
                    workspace_contract=todo.work_contract,
                    routing_spec=self._agent_routing_spec(todo),
                    budget=ContextBudget(
                        max_tokens=profile.model_context_tokens,
                        max_items=profile.max_items + 8,
                        max_bytes=profile.max_bytes,
                    ),
                    dependency_run_ids=dependencies,
                    required_artifacts=dependency_context_refs,
                    request_id=request_id,
                    task_id=todo.todo_id,
                    keep_alive=-1,
                )
                after_run = self.workspace.run_store.get_run(run_id)
                current_run_id, current_lease = self.workspace.current(todo.todo_id)
                if current_run_id != run_id or current_lease.lease_id != lease.lease_id:
                    raise RuntimeError("execution slice changed Run identity or workspace lease")

                root = Path(lease.worktree_root)
                diff_text = _git(root, "diff", "--binary", "HEAD", "--", check=False)
                diff_sha = _sha(diff_text)
                dirty_status = _git(root, "status", "--porcelain=v1", "--untracked-files=all", check=False)
                dirty_paths = tuple(sorted(line[3:].strip() for line in dirty_status.splitlines() if line.strip()))

                decision, reason, stage, verif_att, verif_succ, last_test_diff = evaluator.evaluate(
                    result=result,
                    run=after_run,
                    todo_id=todo.todo_id,
                    slice_index=slice_index,
                    run_store=self.workspace.run_store,
                    dirty_paths=dirty_paths,
                    diff_sha256=diff_sha,
                )
                last_eval = (decision, reason, stage, verif_att, verif_succ, last_test_diff)

                if decision is not ExecutionSliceGrantDecision.GRANT_NEXT_SLICE or slice_index >= self.execution_slice_policy.max_slices_per_todo:
                    break

                checkpoint = ExecutionSliceCheckpointV2(
                    schema_version=2,
                    run_id=run_id,
                    todo_id=todo.todo_id,
                    slice_index=slice_index,
                    max_slices=self.execution_slice_policy.max_slices_per_todo,
                    worker_calls_used_total=after_run.quota_usage.worker_calls,
                    tool_calls_used_total=result.tool_calls,
                    worker_soft_ceiling=self.execution_slice_policy.cumulative_ceiling(slice_index),
                    worker_hard_ceiling=self.execution_slice_policy.max_total_worker_calls_per_todo,
                    tool_hard_ceiling=self.execution_slice_policy.max_tool_calls_hard,
                    changed_paths=tuple(sorted(result.changed_paths)),
                    dirty_paths=dirty_paths,
                    diff_sha256=diff_sha,
                    verification_attempted=verif_att,
                    verification_succeeded=verif_succ,
                    verification_diff_sha256=last_test_diff,
                    finalized=False,
                    grant_decision=decision.value,
                    grant_reason=reason,
                    last_model_id=result.last_model_id,
                    context_bundle_id=result.context_bundle_id,
                    status="CONTINUE",
                    reason=result.reason,
                    exploration_extension_used=(slice_index >= 2 and not bool(result.changed_paths) and not bool(dirty_paths)),
                )
                checkpoint_ids.append(persist_checkpoint(self.artifact_store, checkpoint))
                self._reopen_for_slice(run_id)
        except Exception as exc:
            error_type = type(exc).__name__
            failure_kind = None
            if "ContextBudget" in error_type or "ContextBudgetError" in str(type(exc)):
                failure_kind = FailureKindV1.CONTEXT_BUDGET.value
            elif "Resource" in error_type or "Admission" in error_type or "VRAM" in error_type:
                failure_kind = FailureKindV1.RESOURCE_INSUFFICIENT.value
            elif "Timeout" in error_type or isinstance(exc, TimeoutError):
                failure_kind = FailureKindV1.MODEL_TIMEOUT.value
            if failure_kind is not None:
                return ExecutionObservationV1(
                    profile_id,
                    False,
                    (),
                    0,
                    0,
                    failure_kind,
                    f"{error_type}: {exc}",
                    {
                        "failure_kind": failure_kind,
                        "error_type": error_type,
                        "run_id": run_id,
                        "lease_id": lease.lease_id,
                        "fresh_workspace": fresh_workspace,
                    },
                )
            raise

        assert result is not None
        root = Path(lease.worktree_root)
        workspace_paths = tuple(line for line in _git(root, "ls-files").splitlines() if line)
        immutable = tuple(todo.work_contract.forbidden_scope)
        diff = _git(root, "diff", "--binary", "HEAD", "--", check=False)
        if repair_activation is not None:
            record_repair_execution_observation(
                run_store=self.workspace.run_store, run_id=run_id, todo_id=todo.todo_id,
                activation=repair_activation, request_id=request_id, candidate_diff_after=_sha(diff),
                tool_calls_delta=max(0, result.tool_calls - repair_tool_calls_before),
            )
        finalized = (repair_activation is None and result.outcome.value in {"SUCCESS", "ALREADY_FINALIZED"})

        # Authoritative structured failure_kind / quota_scope from result
        failure_kind = getattr(result, "failure_kind", None)
        if failure_kind is None:
            quota_scope = getattr(result, "quota_scope", None)
            if quota_scope == "WORKER":
                failure_kind = FailureKindV1.WORKER_QUOTA.value
            elif quota_scope == "TOOL":
                failure_kind = FailureKindV1.TOOL_CALL_QUOTA.value
            elif quota_scope is not None:
                failure_kind = FailureKindV1.UNKNOWN.value

        semantic_discovery_stop = self._semantic_discovery_stop_recorded(run_id, todo.todo_id)
        attention_category = getattr(result, "category", None)
        if attention_category is None and hasattr(self.workspace, "run_store"):
            try:
                for ev in reversed(self.workspace.run_store.list_events(run_id)):
                    etype = getattr(ev, "event_type", None)
                    if str(etype) in {"ATTENTION_RECORDED", "ControlPlaneEventType.ATTENTION_RECORDED"}:
                        pl = getattr(ev, "payload", {})
                        if isinstance(pl, Mapping) and pl.get("todo_id") == todo.todo_id:
                            attention_category = pl.get("category")
                            break
            except Exception:
                pass

        if semantic_discovery_stop:
            failure_kind = FailureKindV1.TOOL_LOOP.value
        elif failure_kind is None and result.outcome.value == "LOOP_DETECTED":
            failure_kind = FailureKindV1.TOOL_LOOP.value
        elif failure_kind is None and result.outcome.value == "QUOTA_EXHAUSTED":
            quota_spec = getattr(getattr(todo, "work_contract", None), "quota", None)
            max_tools = getattr(quota_spec, "max_tool_calls", None)
            if max_tools is not None and result.tool_calls >= max_tools:
                failure_kind = FailureKindV1.TOOL_CALL_QUOTA.value
            else:
                failure_kind = FailureKindV1.WORKER_QUOTA.value
        elif failure_kind is None and result.outcome.value == "NEEDS_ATTENTION":
            if attention_category == "tool":
                failure_kind = FailureKindV1.TOOL_FAILURE.value
            elif attention_category == "agent_protocol":
                failure_kind = FailureKindV1.PROTOCOL_INVALID.value
            elif attention_category == "context":
                failure_kind = FailureKindV1.CONTEXT_BUDGET.value

        if repair_activation is not None:
            # A reused candidate remains unverified until a fresh verifier report binds the repaired diff.
            failure_kind = FailureKindV1.VERIFICATION_FAILED.value

        previous_checkpoint = latest_checkpoint(
            self.artifact_store, run_id=run_id, todo_id=todo.todo_id,
            policy=self.execution_slice_policy,
        )
        if not isinstance(previous_checkpoint, ExecutionSliceCheckpointV2):
            previous_checkpoint = None
        previous_checkpoint_id = (
            checkpoint_artifact_id(run_id, todo.todo_id, previous_checkpoint.slice_index)
            if previous_checkpoint is not None else None
        )
        progress_evidence = ProgressEvidenceBuilder().build(
            run_store=self.workspace.run_store, run_id=run_id, todo_id=todo.todo_id,
            lease_id=lease.lease_id, slice_index=slice_index,
            previous_checkpoint=previous_checkpoint, previous_checkpoint_id=previous_checkpoint_id,
            current_checkpoint_id=None, worker_calls_after=after_run.quota_usage.worker_calls,
            tool_calls_after=result.tool_calls, dirty_paths_after=dirty_paths,
            current_diff_sha256=diff_sha, verification_attempted=last_eval[3],
            verification_succeeded=last_eval[4], verification_diff_sha256=last_eval[5],
            finalized=finalized, technical_failure_kind=failure_kind,
        )
        continuation_intent = ContinuationEvaluator().evaluate(
            progress_evidence, capacity_decision=last_eval[0]
        )
        progress_evidence_id = None
        if all(hasattr(self.artifact_store, name) for name in ("has_artifact", "get_artifact", "create_artifact")) and hasattr(self.workspace.run_store, "record_evidence"):
            progress_evidence_id = persist_progress_evidence(
                self.artifact_store, self.workspace.run_store, progress_evidence, continuation_intent
            )
        raw_dict = {
            "agent_result": result.to_dict(),
            "run_id": run_id,
            "lease_id": lease.lease_id,
            "workspace_paths": list(workspace_paths),
            "immutable_paths": list(immutable),
            "candidate_diff": diff[-20000:],
            "fresh_workspace": fresh_workspace,
            "execution_slice_checkpoints": checkpoint_ids,
            "progress_evidence_artifacts": [] if progress_evidence_id is None else [progress_evidence_id],
            "continuation_intents": [continuation_intent.to_dict()],
            "max_worker_calls_per_slice": self.execution_slice_policy.max_worker_calls_per_slice,
            "max_slices_per_todo": self.execution_slice_policy.max_slices_per_todo,
            "max_total_worker_calls_per_todo": self.execution_slice_policy.max_total_worker_calls_per_todo,
        }
        if last_eval is not None:
            raw_dict["grant_decision"] = last_eval[0].value
            raw_dict["grant_reason"] = last_eval[1]
            raw_dict["finalization_stage"] = last_eval[2].value
            raw_dict["verification_attempted"] = last_eval[3]
            raw_dict["verification_succeeded"] = last_eval[4]
            if last_eval[2] is FinalizationStage.DIRTY_UNVERIFIED and result.tool_calls >= self.execution_slice_policy.max_tool_calls_hard:
                raw_dict["no_tool_headroom"] = True

        if semantic_discovery_stop:
            raw_dict["semantic_discovery_stop"] = True
        if failure_kind is not None:
            raw_dict["failure_kind"] = failure_kind
        if attention_category is not None:
            raw_dict["attention_category"] = attention_category

        return ExecutionObservationV1(
            profile_id,
            finalized,
            result.changed_paths,
            result.model_turns,
            result.tool_calls,
            result.outcome.value,
            "semantic discovery no-progress limit reached" if semantic_discovery_stop else (result.final_summary or result.reason),
            raw_dict,
        )


class WorkflowVerificationAdapter:
    def __init__(self, *, coordinator: VerificationCoordinator, workspace: ProductionWorkspaceCoordinator, trace_registry=None) -> None:
        self.coordinator = coordinator
        self.workspace = workspace
        self.trace_registry = trace_registry

    @staticmethod
    def _report_observation(report) -> VerificationObservationV1:
        summaries = [f"{gate.gate_id}:{gate.outcome.value}" for gate in report.results]
        return VerificationObservationV1(
            report.outcome is VerificationOutcome.PASSED,
            report.report_id, report.plan_fingerprint,
            ", ".join(summaries) or report.outcome.value,
            (report.report_id,), report.to_dict())

    def verify(self, todo, execution) -> VerificationObservationV1:
        run_id, lease = self.workspace.current(todo.todo_id)
        run = self.workspace.run_store.get_run(run_id)
        _trace_event(self.trace_registry, "VERIFICATION_STARTED", run_id=run_id, todo_id=todo.todo_id, payload={"candidate_lease_id": lease.lease_id})

        # verification accepts only verifiable Run states. NEEDS_ATTENTION is a
        # legitimate AgentLoop outcome, not a verification infrastructure
        # failure. Preserve it as a deterministic failed observation so
        # workflow can cascade profiles and ultimately invoke recovery recovery.
        #
        # Do not manufacture a VerificationReport and do not relax the frozen
        # VerificationCoordinator state contract.
        if run.status is RunStatus.NEEDS_ATTENTION:
            if (execution.raw.get("typed_scenario_execution") is True
                    and execution.raw.get("candidate_failure") == "VERIFICATION_FAILED"):
                from .simple_worker_adapter import current_failed_verification_feedback
                from ..verification.errors import VerificationIntegrityError
                resolved = current_failed_verification_feedback(
                    workspace=self.workspace, coordinator=self.coordinator,
                    contract=todo.work_contract, run_id=run_id,
                    todo_id=todo.todo_id, lease_id=lease.lease_id)
                if (resolved is None or resolved[0].report_id !=
                        execution.raw.get("candidate_verification_report_id")):
                    raise VerificationIntegrityError(
                        "declared candidate failure has no current authoritative failed report")
                report, _ = resolved
                _trace_event(self.trace_registry, "VERIFICATION_FINISHED",
                    run_id=run_id, todo_id=todo.todo_id, payload={
                        "candidate_lease_id": lease.lease_id,
                        "verification_report_id": report.report_id,
                        "outcome": "FAILED", "reused_current_failed_report": True,
                        "execution_failure_kind": execution.raw.get("execution_failure_kind"),
                    })
                # Read-only FAILED observation for recovery: no verifier call,
                # no Run reopening, no passed report or acceptance publication.
                return self._report_observation(report)
            summary = (
                "execution requires attention before deterministic verification: "
                f"outcome={execution.outcome}; "
                f"reason={execution.summary or 'unspecified'}"
            )
            fingerprint = _sha(
                "|".join(
                    (
                        run_id,
                        run.status.value,
                        execution.profile_id,
                        execution.outcome,
                        execution.summary or "",
                        ",".join(execution.changed_paths),
                    )
                )
            )
            report_id = (
                f"phase16:{todo.todo_id}:"
                f"execution-not-verifiable:{fingerprint[:16]}"
            )
            observation = VerificationObservationV1(
                False,
                report_id,
                fingerprint,
                summary,
                (),
                {
                    "kind": "EXECUTION_NOT_VERIFIABLE",
                    "run_id": run_id,
                    "run_status": run.status.value,
                    "profile_id": execution.profile_id,
                    "execution_outcome": execution.outcome,
                    "execution_summary": execution.summary,
                    "changed_paths": list(execution.changed_paths),
                    "agent_result": execution.raw.get("agent_result", {}),
                },
            )
            _trace_event(self.trace_registry, "VERIFICATION_FINISHED", run_id=run_id, todo_id=todo.todo_id, payload={"candidate_lease_id": lease.lease_id, "verification_report_id": report_id, "outcome": "FAILED", "kind": "EXECUTION_NOT_VERIFIABLE"})
            return observation

        try:
            result = self.coordinator.verify(
                run_id=run_id,
                contract=todo.work_contract,
                lease_id=lease.lease_id,
            )
        except VerificationStateError as exc:
            if str(exc) != "candidate worktree must contain a dirty change before verification":
                _trace_event(self.trace_registry, "VERIFICATION_FAILED", run_id=run_id, todo_id=todo.todo_id, payload={"candidate_lease_id": lease.lease_id, "exception_type": type(exc).__name__, "message": str(exc)})
                raise
            snapshot = self.workspace.manager.snapshot(lease.lease_id)
            if snapshot.dirty:
                _trace_event(self.trace_registry, "VERIFICATION_FAILED", run_id=run_id, todo_id=todo.todo_id, payload={"candidate_lease_id": lease.lease_id, "exception_type": type(exc).__name__, "message": str(exc)})
                raise
            summary = (
                "candidate worktree contains no modifications before deterministic verification: "
                f"outcome={execution.outcome}; "
                f"reason={execution.summary or 'unspecified'}"
            )
            fingerprint = _sha(
                "|".join(
                    (
                        run_id,
                        lease.lease_id,
                        "CANDIDATE_NOT_DIRTY",
                        run.status.value,
                        execution.profile_id,
                        execution.outcome,
                        execution.summary or "",
                        ",".join(execution.changed_paths),
                    )
                )
            )
            report_id = (
                f"phase16:{todo.todo_id}:"
                f"candidate-not-dirty:{fingerprint[:16]}"
            )
            observation = VerificationObservationV1(
                False,
                report_id,
                fingerprint,
                summary,
                (),
                {
                    "kind": "CANDIDATE_NOT_DIRTY",
                    "run_id": run_id,
                    "lease_id": lease.lease_id,
                    "run_status": run.status.value,
                    "profile_id": execution.profile_id,
                    "execution_outcome": execution.outcome,
                    "execution_summary": execution.summary,
                    "changed_paths": list(execution.changed_paths),
                    "candidate_dirty": False,
                    "agent_result": execution.raw.get("agent_result", {}),
                },
            )
            _trace_event(self.trace_registry, "VERIFICATION_FINISHED", run_id=run_id, todo_id=todo.todo_id, payload={"candidate_lease_id": lease.lease_id, "verification_report_id": report_id, "outcome": "FAILED", "kind": "CANDIDATE_NOT_DIRTY"})
            return observation
        except Exception as exc:
            _trace_event(self.trace_registry, "VERIFICATION_FAILED", run_id=run_id, todo_id=todo.todo_id, payload={"candidate_lease_id": lease.lease_id, "exception_type": type(exc).__name__, "message": str(exc)})
            raise

        report = result.report
        for gate in report.results:
            gate_payload = {"verification_report_id": report.report_id, "candidate_lease_id": lease.lease_id, "gate_id": gate.gate_id, "outcome": gate.outcome.value}
            _trace_event(self.trace_registry, "VERIFICATION_GATE_STARTED", run_id=run_id, todo_id=todo.todo_id, payload=gate_payload)
            _trace_event(self.trace_registry, "VERIFICATION_GATE_FINISHED", run_id=run_id, todo_id=todo.todo_id, payload=gate_payload)
        _trace_event(self.trace_registry, "VERIFICATION_FINISHED", run_id=run_id, todo_id=todo.todo_id, payload={"verification_report_id": report.report_id, "candidate_lease_id": lease.lease_id, "outcome": report.outcome.value})
        return self._report_observation(report)


def _required_capability_failure(workspace, todo, run_id, lease, verification):
    from .acceptance_bridge import load_frozen_acceptance
    from ..verification.acceptance import unsupported_required_obligation, fingerprint
    from ..control_plane import Evidence, EvidenceStatus
    store = getattr(workspace, 'run_store', None)
    if store is None:
        return None
    frozen = load_frozen_acceptance(store, run_id, todo.work_contract)
    failure = unsupported_required_obligation(frozen)
    if failure is None:
        return None
    snapshot = workspace.manager.snapshot(lease.lease_id)
    payload = {**failure.to_dict(), 'run_id':run_id, 'todo_id':todo.todo_id,
        'lease_id':lease.lease_id, 'candidate_head':snapshot.head_commit,
        'candidate_diff_sha256':snapshot.diff_sha256,
        'candidate_content_sha256':getattr(snapshot, 'content_sha256', None),
        'verification_report_id':getattr(verification, 'report_id', None),
        'failure_kind':failure.code, 'model_incorrectness_proven':False,
        'recovery_workspace_disposition':'BLOCK'}
    payload = json.loads(json.dumps(payload))
    from .execution_responsibility import record_once
    record_once(store, run_id, todo.todo_id, failure.code + ':' + fingerprint(payload), payload)
    return payload


class WorkflowReviewAdapter:
    def __init__(self, *, critic, judge, workspace: ProductionWorkspaceCoordinator, trace_registry=None) -> None:
        self.critic = critic
        self.judge = judge
        self.workspace = workspace
        self.workspace._review_authority_required = True
        self.trace_registry = trace_registry

    def review(self, todo, execution, verification) -> ReviewObservationV1:
        run_id, lease = self.workspace.current(todo.todo_id)
        unsupported = _required_capability_failure(self.workspace, todo, run_id, lease, verification)
        if unsupported is not None:
            return ReviewObservationV1(False, True, 'UNSUPPORTED_REQUIRED_OBLIGATION',
                'Required public verifier capability unavailable; implementation correctness not disproved',
                tuple(verification.evidence_ids), unsupported)
        _trace_event(self.trace_registry, "CRITIC_STARTED", run_id=run_id, todo_id=todo.todo_id, payload={"candidate_lease_id": lease.lease_id, "verification_report_id": getattr(verification, "report_id", None)})
        try:
            critic_result = self.critic.review(
                run_id=run_id,
                contract=todo.work_contract,
                lease_id=lease.lease_id,
            )
        except CriticConformanceError as exc:
            _trace_event(self.trace_registry, 'CRITIC_FAILED', run_id=run_id, todo_id=todo.todo_id,
                payload={'candidate_lease_id':lease.lease_id,'exception_type':type(exc).__name__,
                         'message':str(exc),'failure_diagnostic':exc.failure_diagnostic})
            # Explicit single-generation conformance failures are terminal;
            # mission persists this first cause rather than starting recovery.
            raise
        except (CriticExhaustedError, CriticProtocolError, CriticRoutingError, CriticStateError) as exc:
            _trace_event(
                self.trace_registry,
                "CRITIC_FAILED",
                run_id=run_id,
                todo_id=todo.todo_id,
                payload={
                    "candidate_lease_id": lease.lease_id,
                    "verification_report_id": getattr(verification, "report_id", None),
                    "exception_type": type(exc).__name__,
                    "message": str(exc),
                },
            )
            return ReviewObservationV1(
                False,
                True,
                "CRITIC_UNAVAILABLE",
                f"critic review failed closed: {type(exc).__name__}: {exc}",
                tuple(verification.evidence_ids),
                {"critic_error": {"type": type(exc).__name__, "message": str(exc)}},
            )
        _trace_event(self.trace_registry, "CRITIC_FINISHED", run_id=run_id, todo_id=todo.todo_id, payload={"candidate_lease_id": lease.lease_id, "review_report_id": critic_result.report.report_id})
        _trace_event(self.trace_registry, "JUDGE_STARTED", run_id=run_id, todo_id=todo.todo_id, payload={"candidate_lease_id": lease.lease_id, "verification_report_id": getattr(verification, "report_id", None)})
        try:
            judge_result = self.judge.review(
                run_id=run_id,
                contract=todo.work_contract,
                lease_id=lease.lease_id,
            )
        except (JudgeExhaustedError, JudgeProtocolError, JudgeStateError) as exc:
            receipt = None
            artifact_store = getattr(self.workspace, "artifact_store", None)
            verification_id = getattr(verification, "report_id", None)
            if artifact_store is not None and type(verification_id) is str and verification_id:
                receipt = publish_review_failure(artifact_store=artifact_store,
                    run_id=run_id,todo_id=todo.todo_id,verification_report_id=verification_id,exc=exc)
            # Legacy callers may lack durable anchors. Preserve unavailable
            # observation without inventing a report identity or acceptance.
            _trace_event(
                self.trace_registry,
                "JUDGE_FAILED",
                run_id=run_id,
                todo_id=todo.todo_id,
                payload={
                    "candidate_lease_id": lease.lease_id,
                    "verification_report_id": getattr(verification, "report_id", None),
                    "exception_type": type(exc).__name__,
                    "message": str(exc),
                },
            )
            return ReviewObservationV1(
                False,
                True,
                "JUDGE_UNAVAILABLE",
                f"judge review failed closed: {type(exc).__name__}: {exc}",
                tuple(dict.fromkeys((critic_result.report.report_id, *verification.evidence_ids))),
                {
                    "critic": critic_result.report.to_dict(),
                    "judge_error": {"type": type(exc).__name__, "message": str(exc),
                        "stage": "JUDGE", "failure_kind": getattr(exc,"failure_kind","UNKNOWN"),
                        "request_id": getattr(exc,"request_id",None), "attempts": getattr(exc,"attempts",None)},
                    "review_failure": None if receipt is None else receipt.to_dict(),
                    "review_failure_persistence": "UNANCHORED" if receipt is None else "DURABLE",
                },
            )
        analysis = judge_result.report.analysis
        _trace_event(self.trace_registry, "JUDGE_FINISHED", run_id=run_id, todo_id=todo.todo_id, payload={"candidate_lease_id": lease.lease_id, "review_report_id": judge_result.report.report_id, "verdict": analysis.decision.value})
        accept = verification.passed and analysis.decision is JudgeDecision.ACCEPT
        if accept:
            from .execution_responsibility import record_once
            snapshot = self.workspace.manager.snapshot(lease.lease_id)
            record_once(self.workspace.run_store, run_id, todo.todo_id, 'ACCEPTED_CANDIDATE_AUTHORITY_V1', {
                'lease_id':lease.lease_id, 'head':snapshot.head_commit, 'diff_sha256':snapshot.diff_sha256,
                'content_sha256':getattr(snapshot, 'content_sha256', None), 'verification_report_id':verification.report_id,
                'critic_report_id':critic_result.report.report_id, 'judge_report_id':judge_result.report.report_id})
        return ReviewObservationV1(
            accept,
            not accept,
            analysis.decision.value,
            analysis.summary,
            tuple(dict.fromkeys((critic_result.report.report_id, judge_result.report.report_id, *analysis.evidence))),
            {
                "critic": critic_result.report.to_dict(),
                "judge": judge_result.report.to_dict(),
            },
        )


def _semantic_discovery_retry_is_ungrounded(execution, recovery) -> bool:
    """Allow a discovery-loop retry only when recovery adds bounded grounding."""
    reason = str(getattr(execution, "reason", "")).casefold()
    if "semantic discovery no-progress limit reached" not in reason:
        return False
    return not bool(recovery.context_artifact_ids or recovery.expansion_queries)


def _trace_recovery_terminal(method):
    """Close every Recovery trace path without changing recovery authority."""

    def traced(
        self,
        todo,
        *,
        objective,
        execution,
        verification,
        failed_profile_ids,
        recovery_cycle,
    ):
        run_id, lease = self.workspace.current(todo.todo_id)

        _trace_event(
            self.trace_registry,
            "RECOVERY_STARTED",
            run_id=run_id,
            todo_id=todo.todo_id,
            payload={
                "candidate_lease_id": lease.lease_id,
                "recovery_cycle": recovery_cycle,
            },
        )

        try:
            result = method(
                self,
                todo,
                objective=objective,
                execution=execution,
                verification=verification,
                failed_profile_ids=failed_profile_ids,
                recovery_cycle=recovery_cycle,
            )
        except Exception as exc:
            _trace_event(
                self.trace_registry,
                "RECOVERY_FAILED",
                run_id=run_id,
                todo_id=todo.todo_id,
                payload={
                    "candidate_lease_id": lease.lease_id,
                    "recovery_cycle": recovery_cycle,
                    "exception_type": type(exc).__name__,
                    "message": str(exc),
                },
            )
            raise

        raw = getattr(result, "raw", {})
        disposition = (
            raw.get("recovery_workspace_disposition")
            if isinstance(raw, Mapping)
            else None
        )
        # Terminal STOP may never publish a recovery context. Retain only the
        # bounded diagnostic cause, through the recorder's normal redaction.
        bridge_diagnostic = {}
        if isinstance(raw, Mapping) and raw.get("bridge_failure_kind"):
            for key, bound in (("bridge_failure_kind", 64), ("bridge_failure_stage", 64),
                               ("bridge_failure", 2048)):
                value = raw.get(key)
                if isinstance(value, str):
                    encoded = value.encode("utf-8")
                    bridge_diagnostic[key] = encoded[:bound].decode("utf-8", "ignore")
                    bridge_diagnostic[key + "_truncated"] = len(encoded) > bound

        _trace_event(
            self.trace_registry,
            "RECOVERY_FINISHED",
            run_id=run_id,
            todo_id=todo.todo_id,
            payload={
                "candidate_lease_id": lease.lease_id,
                "recovery_cycle": recovery_cycle,
                "continue_work": result.continue_work,
                "reason": result.reason,
                "recovery_workspace_disposition": disposition,
                **bridge_diagnostic,
            },
        )

        return result

    return traced


class WorkflowRecoveryAdapter:
    def __init__(self, *, bridge, critic, workspace: ProductionWorkspaceCoordinator, trace_registry=None) -> None:
        # Bind the existing diagnostic port to the existing artifact owner.
        # Composition/budgets/routing remain unchanged through wrapper layers.
        owner = bridge
        seen = set()
        while owner is not None and id(owner) not in seen:
            seen.add(id(owner))
            client = getattr(owner, "diagnosis_client", None)
            if client is not None:
                if hasattr(client, "artifact_store"):
                    client.artifact_store = workspace.artifact_store
                break
            owner = getattr(owner, "bridge", getattr(owner, "inner", None))
        self.bridge = bridge
        self.critic = critic
        self.workspace = workspace
        self.trace_registry = trace_registry

    @_trace_recovery_terminal
    def recover(self, todo, *, objective, execution, verification, failed_profile_ids, recovery_cycle) -> RecoveryObservationV1:
        run_id, lease = self.workspace.current(todo.todo_id)
        execution_raw = getattr(execution, 'raw', {})
        if execution_raw.get('execution_mode') == 'DIRECT' or 'direct_result' in execution_raw:
            from ..verification.acceptance import fingerprint
            authority = execution_raw.get('path_authority_provenance')
            if (not isinstance(authority, Mapping) or authority.get('run_id') != run_id
                    or authority.get('lease_id') != lease.lease_id
                    or authority.get('todo_id') != todo.todo_id
                    or authority.get('contract_fingerprint') != fingerprint(todo.work_contract.to_dict())):
                record_recovery_block(run_store=self.workspace.run_store,run_id=run_id,todo_id=todo.todo_id,
                    failure_kind=FailureKindV1.TOOL_FAILURE.value,reason='PATH_AUTHORITY_MISMATCH')
                return RecoveryObservationV1(False,reason='PATH_AUTHORITY_MISMATCH',raw={
                    'failure_kind':FailureKindV1.TOOL_FAILURE.value,'cause_code':'PATH_AUTHORITY_MISMATCH',
                    'recovery_workspace_disposition':'BLOCK'})
        if getattr(execution, 'raw', {}).get('native_failure_category') == 'NO_PROGRESS':
            record_recovery_block(run_store=self.workspace.run_store,run_id=run_id,todo_id=todo.todo_id,
                failure_kind=FailureKindV1.TOOL_LOOP.value,reason='NO_PROGRESS: identical native recovery frontier')
            return RecoveryObservationV1(False,reason='NO_PROGRESS',raw={
                'failure_kind':FailureKindV1.TOOL_LOOP.value,'cause_code':'NO_PROGRESS',
                'recovery_workspace_disposition':'BLOCK'})
        unsupported = _required_capability_failure(self.workspace, todo, run_id, lease, verification)
        if unsupported is not None:
            return RecoveryObservationV1(False, reason='UNSUPPORTED_REQUIRED_OBLIGATION', raw=unsupported)
        is_semantic_review = verification is not None and bool(getattr(verification, "passed", False))
        cause = (
            resolve_review_recovery_cause(
                run_store=getattr(self.workspace, "run_store", None),
                run_id=run_id,
                todo_id=todo.todo_id,
                verification=verification,
            )
            if is_semantic_review else None
        )
        failure_receipt = resolve_review_failure(artifact_store=getattr(self.workspace,"artifact_store",None),
            run_id=run_id,todo_id=todo.todo_id,verification_report_id=getattr(verification,"report_id",None)) if is_semantic_review and cause is None else None
        failure_kind = (
            failure_receipt.kind.value if failure_receipt is not None else
            "UNRESOLVED_REVIEW_CAUSE" if is_semantic_review and cause is None else
            cause.kind.value if cause is not None else
            getattr(execution, "raw", {}).get("failure_kind")
            if isinstance(getattr(execution, "raw", None), Mapping) else None
        )
        _trace_event(self.trace_registry, "FAILURE_CLASSIFIED", run_id=run_id, todo_id=todo.todo_id, payload={"candidate_lease_id": lease.lease_id, "failure_kind": failure_kind, "verification_report_id": None if verification is None else getattr(verification, "report_id", None)})
        if failure_receipt is not None:
            reason = "review unavailable: " + failure_receipt.kind.value
            record_review_failure_block(run_store=self.workspace.run_store,receipt=failure_receipt)
            return RecoveryObservationV1(False,reason=reason,raw={"failure_kind":failure_receipt.kind.value,
                "protocol_failure_origin":failure_receipt.stage,"review_failure":failure_receipt.to_dict(),
                "recovery_workspace_disposition":"BLOCK","recovery_workspace_block_reason":reason})
        try:
            self.critic.review(run_id=run_id, contract=todo.work_contract, lease_id=lease.lease_id)
        except Exception:
            # recovery remains authoritative for recovery.  Critic failures are
            # preserved by its own artifacts/events and must not become PASS.
            pass

        if is_semantic_review:
            if cause is None:
                reason = "review recovery cause unresolved"
                if hasattr(self.workspace, "run_store"):
                    record_recovery_block(
                        run_store=self.workspace.run_store,
                        run_id=run_id,
                        todo_id=todo.todo_id,
                        failure_kind="UNRESOLVED_REVIEW_CAUSE",
                        reason=reason,
                    )
                raw = {
                    "recovery_workspace_disposition": "BLOCK",
                    "recovery_workspace_block_reason": reason,
                }
                return RecoveryObservationV1(
                    False,
                    revised_objective=None,
                    context_artifact_ids=(),
                    reason=reason,
                    routing_task_spec=None,
                    expansion_queries=(),
                    raw=raw,
                )

            if cause.kind is ReviewRecoveryCauseKindV1.REVIEW_REPLAN:
                _trace_event(
                    self.trace_registry, "REPLAN_STARTED", run_id=run_id,
                    todo_id=todo.todo_id,
                    payload={"judge_report_id": cause.judge_report_id,
                             "verification_report_id": cause.verification_report_id,
                             "review_recovery_cause": cause.kind.value},
                )
                review_evidence = cause.to_dict()
                judge_artifact_id = f"{cause.judge_report_id}:report"
                if self.workspace.artifact_store.has_artifact(judge_artifact_id):
                    judge_report = json.loads(self.workspace.artifact_store.get_artifact(judge_artifact_id).content)
                    if (judge_report.get("report_id") != cause.judge_report_id
                            or judge_report.get("verification_report_id") != verification.report_id):
                        raise RuntimeError("durable Judge report does not match review recovery cause")
                    review_evidence["judge_report"] = judge_report
                    critic_id = judge_report.get("critic_report_id")
                    if isinstance(critic_id, str):
                        critic_artifact_id = f"{critic_id}:report"
                        if self.workspace.artifact_store.has_artifact(critic_artifact_id):
                            critic_report = json.loads(self.workspace.artifact_store.get_artifact(critic_artifact_id).content)
                            if critic_report.get("report_id") != critic_id:
                                raise RuntimeError("durable Critic report identity mismatch")
                            review_evidence["critic_report"] = critic_report
                spine = getattr(self.workspace, "spine", None)
                if spine is not None:
                    root_ref = spine.root_context(todo.work_contract.goal_id).source
                    root_payload = json.loads(self.workspace.artifact_store.get_artifact(root_ref.artifact_id).content)
                    root_objective = root_payload.get("objective")
                    if isinstance(root_objective, str):
                        review_evidence["original_mission"] = root_objective[:12000]
                        review_evidence["original_mission_truncated"] = len(root_objective) > 12000
                plan_rows = []
                for ref in self.workspace.artifact_store.list_refs():
                    artifact = self.workspace.artifact_store.get_artifact(ref.artifact_id)
                    metadata = artifact.metadata
                    if metadata.get("goal_id") != todo.work_contract.goal_id:
                        continue
                    if metadata.get("production_role") == "PLAN":
                        review_evidence["current_plan"] = json.loads(artifact.content)
                    elif metadata.get("production_role") == "WORK_ITEM" and len(plan_rows) < 16:
                        item_payload = json.loads(artifact.content).get("todo", {})
                        plan_rows.append({
                            "todo_id": item_payload.get("todo_id"),
                            "objective": str(item_payload.get("objective", ""))[:1200],
                            "dependencies": item_payload.get("dependencies", []),
                        })
                if plan_rows:
                    review_evidence["current_plan_todos"] = plan_rows
                result = self.bridge.recover(
                    todo, objective=objective, execution=execution,
                    verification=verification, failed_profile_ids=failed_profile_ids,
                    recovery_cycle=recovery_cycle, review_replan=review_evidence,
                )
                raw = dict(result.raw)
                raw["review_recovery_cause"] = cause.to_dict()
                _trace_event(
                    self.trace_registry, "REPLAN_FINISHED", run_id=run_id,
                    todo_id=todo.todo_id,
                    payload={"judge_report_id": cause.judge_report_id,
                             "controller_action": raw.get("controller_action"),
                             "planner_invoked": raw.get("replan_model_calls", 0) > 0,
                             "revised_objective_sha256": (
                                 _sha(result.revised_objective)
                                 if result.revised_objective is not None else None
                             )},
                )
                if result.continue_work and raw.get("controller_action") == "ORNITH_REPLAN":
                    replan_contract = raw.get("replan")
                    fresh_required = (
                        isinstance(replan_contract, Mapping)
                        and replan_contract.get("fresh_workspace_required") is True
                    )
                    checkpoint = checkpoint_candidate(
                        artifact_store=self.workspace.artifact_store,
                        run_store=self.workspace.run_store,
                        manager=self.workspace.manager,
                        run_id=run_id, todo_id=todo.todo_id, lease=lease,
                        failure_kind=cause.kind.value,
                    )
                    allowed, detail = (False, "candidate checkpoint unavailable")
                    if checkpoint is not None:
                        allowed, detail = validate_reuse(
                            manager=self.workspace.manager, lease=lease,
                            checkpoint=checkpoint, contract=todo.work_contract,
                        )
                        if fresh_required:
                            allowed, detail = False, "Phase15 replan contract requires fresh workspace"
                        decision, decision_id = _rc5_candidate_decision(
                            artifact_store=self.workspace.artifact_store,
                            run_store=self.workspace.run_store,
                            execution=execution, run_id=run_id, todo_id=todo.todo_id,
                            lease_id=lease.lease_id, checkpoint=checkpoint,
                            failure_kind=cause.kind.value,
                            base_disposition=RecoveryWorkspaceDispositionV1.FRESH_CANDIDATE,
                            candidate_valid=allowed, contract=todo.work_contract,
                            review_recovery_cause=cause.kind.value,
                        )
                        if decision is not None:
                            if decision.disposition is CandidateDispositionV1.BLOCK:
                                raw["recovery_workspace_disposition"] = "BLOCK"
                                raw["candidate_continuation_decision"] = decision.to_dict()
                                return RecoveryObservationV1(False, reason="RC-5 blocked Judge replan candidate", raw=raw)
                            allowed = decision.disposition is CandidateDispositionV1.REUSE_CANDIDATE
                            detail = ";".join(decision.reason_codes)
                            raw["candidate_continuation_decision"] = decision.to_dict()
                            raw["candidate_continuation_decision_id"] = decision_id
                        if fresh_required:
                            allowed, detail = False, "Phase15 replan contract requires fresh workspace"
                        record_candidate_disposition(
                            run_store=self.workspace.run_store, run_id=run_id,
                            todo_id=todo.todo_id, checkpoint=checkpoint,
                            reused=allowed, reason=detail,
                        )
                        raw["candidate_checkpoint_fingerprint"] = checkpoint.fingerprint
                    raw["candidate_reuse_reason"] = detail
                    if allowed:
                        record_delta_summary(
                            artifact_store=self.workspace.artifact_store,
                            run_store=self.workspace.run_store, run_id=run_id,
                            todo_id=todo.todo_id, checkpoint=checkpoint,
                        )
                    # Defer fresh allocation until mission applies the revised
                    # responsibility. rotate() here would freeze the old contract.
                    raw["semantic_replan_required"] = True
                    raw["revised_objective"] = result.revised_objective
                    raw["recovery_workspace_disposition"] = (
                        "REUSE_CANDIDATE" if allowed else "FRESH_CANDIDATE"
                    )
                    return RecoveryObservationV1(
                        False, reason="Judge REPLAN requires durable DAG amendment",
                        context_artifact_ids=result.context_artifact_ids, raw=raw,
                    )
                raw["recovery_workspace_disposition"] = "BLOCK"
                return RecoveryObservationV1(
                    False, reason="Judge REPLAN could not produce a valid replan",
                    context_artifact_ids=result.context_artifact_ids, raw=raw,
                )

            if cause.kind in {ReviewRecoveryCauseKindV1.REVIEW_HUMAN_REVIEW, ReviewRecoveryCauseKindV1.REVIEW_BLOCKED}:
                reason = f"Judge review requested: {cause.judge_decision}"
                if hasattr(self.workspace, "run_store"):
                    record_recovery_block(
                        run_store=self.workspace.run_store,
                        run_id=run_id,
                        todo_id=todo.todo_id,
                        failure_kind=cause.kind.value,
                        reason=reason,
                    )
                raw = {
                    "review_recovery_cause": cause.to_dict(),
                    "recovery_workspace_disposition": "BLOCK",
                    "recovery_workspace_block_reason": reason,
                }
                return RecoveryObservationV1(
                    False,
                    revised_objective=None,
                    context_artifact_ids=(),
                    reason=reason,
                    routing_task_spec=None,
                    expansion_queries=(),
                    raw=raw,
                )

            result = self.bridge.recover(
                todo,
                objective=objective,
                execution=execution,
                verification=verification,
                failed_profile_ids=failed_profile_ids,
                recovery_cycle=recovery_cycle,
            )

            # Case A: recovery requests RETRY_INFRASTRUCTURE -> neutral retry on fresh candidate
            if (
                not result.continue_work
                and result.raw.get("controller_action") == "RETRY_INFRASTRUCTURE"
            ):
                raw = dict(result.raw)
                raw["review_recovery_cause"] = cause.to_dict()
                raw["recovery_workspace_disposition"] = "FRESH_CANDIDATE"
                if hasattr(self.workspace, "rotate"):
                    self.workspace.rotate(todo)
                return RecoveryObservationV1(
                    True,
                    revised_objective=objective,
                    context_artifact_ids=result.context_artifact_ids,
                    reason="Phase15 infrastructure diagnosis requested bounded neutral retry",
                    routing_task_spec=None,
                    expansion_queries=(),
                    raw=raw,
                )

            # Case B: recovery returns terminal stop -> retain candidate for evidence, do NOT rotate
            if not result.continue_work:
                raw = dict(result.raw)
                raw["review_recovery_cause"] = cause.to_dict()
                return RecoveryObservationV1(
                    False,
                    revised_objective=None,
                    context_artifact_ids=result.context_artifact_ids,
                    reason=result.reason,
                    routing_task_spec=None,
                    expansion_queries=(),
                    raw=raw,
                )

            # Case C: recovery continues -> attempt candidate reuse
            checkpoint = checkpoint_candidate(
                artifact_store=self.workspace.artifact_store,
                run_store=self.workspace.run_store,
                manager=self.workspace.manager,
                run_id=run_id,
                todo_id=todo.todo_id,
                lease=lease,
                failure_kind=cause.kind.value,
            )
            if checkpoint is not None:
                allowed, detail = validate_reuse(
                    manager=self.workspace.manager,
                    lease=lease,
                    checkpoint=checkpoint,
                    contract=todo.work_contract,
                )
                rc5_decision, rc5_decision_id = _rc5_candidate_decision(
                    artifact_store=self.workspace.artifact_store, run_store=self.workspace.run_store,
                    execution=execution, run_id=run_id, todo_id=todo.todo_id, lease_id=lease.lease_id,
                    checkpoint=checkpoint, failure_kind=cause.kind.value,
                    base_disposition=RecoveryWorkspaceDispositionV1.FRESH_CANDIDATE,
                    candidate_valid=allowed, contract=todo.work_contract, review_recovery_cause=cause.kind.value,
                )
                if rc5_decision is not None:
                    if rc5_decision.disposition is CandidateDispositionV1.BLOCK:
                        reason = "RC-5 candidate continuation blocked unsafe recovery"
                        raw = dict(result.raw)
                        raw.update({"review_recovery_cause": cause.to_dict(), "recovery_workspace_disposition": "BLOCK",
                                    "candidate_continuation_decision": rc5_decision.to_dict(),
                                    "candidate_continuation_decision_id": rc5_decision_id})
                        return RecoveryObservationV1(False, None, result.context_artifact_ids, reason, None, (), raw)
                    allowed = rc5_decision.disposition is CandidateDispositionV1.REUSE_CANDIDATE
                    detail = ";".join(rc5_decision.reason_codes)
                record_candidate_disposition(
                    run_store=self.workspace.run_store,
                    run_id=run_id,
                    todo_id=todo.todo_id,
                    checkpoint=checkpoint,
                    reused=allowed,
                    reason=detail,
                )
                raw = dict(result.raw)
                raw.update({
                    "review_recovery_cause": cause.to_dict(),
                    "recovery_workspace_disposition": (
                        "REUSE_CANDIDATE" if allowed else "FRESH_CANDIDATE"
                    ),
                    "candidate_checkpoint_fingerprint": checkpoint.fingerprint,
                    "candidate_reuse_reason": detail,
                    "candidate_continuation_decision": None if rc5_decision is None else rc5_decision.to_dict(),
                    "candidate_continuation_decision_id": rc5_decision_id,
                })
                result = RecoveryObservationV1(
                    result.continue_work,
                    result.revised_objective,
                    result.context_artifact_ids,
                    result.reason,
                    result.routing_task_spec,
                    result.expansion_queries,
                    raw,
                )
                if allowed:
                    record_delta_summary(
                        artifact_store=self.workspace.artifact_store,
                        run_store=self.workspace.run_store,
                        run_id=run_id,
                        todo_id=todo.todo_id,
                        checkpoint=checkpoint,
                    )
                    return result
                if hasattr(self.workspace, "rotate"):
                    self.workspace.rotate(todo)
                return result

            raw = dict(result.raw)
            raw.update({
                "review_recovery_cause": cause.to_dict(),
                "recovery_workspace_disposition": "FRESH_CANDIDATE",
            })
            if hasattr(self.workspace, "rotate"):
                self.workspace.rotate(todo)
            return RecoveryObservationV1(
                result.continue_work,
                result.revised_objective,
                result.context_artifact_ids,
                result.reason,
                result.routing_task_spec,
                result.expansion_queries,
                raw,
            )

        # Technical path
        result = self.bridge.recover(
            todo,
            objective=objective,
            execution=execution,
            verification=verification,
            failed_profile_ids=failed_profile_ids,
            recovery_cycle=recovery_cycle,
        )

        if (
            not result.continue_work
            and result.raw.get("controller_action") == "RETRY_INFRASTRUCTURE"
        ):
            result = RecoveryObservationV1(
                True,
                revised_objective=objective,
                context_artifact_ids=result.context_artifact_ids,
                reason="Phase15 infrastructure diagnosis requested bounded neutral retry",
                routing_task_spec=None,
                expansion_queries=(),
                raw=result.raw,
            )

        if result.continue_work and _semantic_discovery_retry_is_ungrounded(execution, result):
            reason = "semantic discovery retry suppressed: recovery supplied no new grounded paths or fresh read evidence"
            if hasattr(self.workspace, "run_store"):
                record_recovery_block(
                    run_store=self.workspace.run_store, run_id=run_id, todo_id=todo.todo_id,
                    failure_kind="TOOL_LOOP", reason=reason,
                )
            raw = dict(result.raw)
            raw.update({
                "recovery_workspace_disposition": "BLOCK",
                "recovery_workspace_block_reason": reason,
                "semantic_discovery_retry_suppressed": True,
            })
            return RecoveryObservationV1(
                False, None, result.context_artifact_ids, reason, None, (), raw
            )

        if result.continue_work:
            failure = result.raw.get("deterministic_failure", {})
            kind = failure.get("failure_kind") if isinstance(failure, Mapping) else "UNKNOWN"
            disposition = recovery_workspace_disposition(kind)
            if disposition is RecoveryWorkspaceDispositionV1.BLOCK:
                reason = f"deterministic failure {kind} blocks workspace recovery"
                if hasattr(self.workspace, "run_store"):
                    record_recovery_block(
                        run_store=self.workspace.run_store,
                        run_id=run_id,
                        todo_id=todo.todo_id,
                        failure_kind=kind,
                        reason=reason,
                    )
                raw = dict(result.raw)
                raw.update({
                    "recovery_workspace_disposition": "BLOCK",
                    "recovery_workspace_block_reason": reason,
                })
                return RecoveryObservationV1(
                    False,
                    revised_objective=None,
                    context_artifact_ids=result.context_artifact_ids,
                    reason=reason,
                    routing_task_spec=None,
                    expansion_queries=(),
                    raw=raw,
                )
            # RC-5 may preserve useful quota-limited work even when the legacy
            # failure table chose FRESH_CANDIDATE.  It never grants RC-3 capacity.
            if (
                disposition is RecoveryWorkspaceDispositionV1.FRESH_CANDIDATE
                and all(hasattr(self.workspace, name) for name in ("artifact_store", "run_store", "manager"))
                and isinstance(getattr(execution, "raw", None), Mapping)
                and isinstance(execution.raw.get("progress_evidence_artifacts"), list)
            ):
                checkpoint = checkpoint_candidate(
                    artifact_store=self.workspace.artifact_store,
                    run_store=self.workspace.run_store,
                    manager=self.workspace.manager,
                    run_id=run_id,
                    todo_id=todo.todo_id,
                    lease=lease,
                    failure_kind=kind,
                )
                if checkpoint is not None:
                    candidate_valid, detail = validate_reuse(
                        manager=self.workspace.manager, lease=lease,
                        checkpoint=checkpoint, contract=todo.work_contract,
                    )
                    rc5_decision, rc5_decision_id = _rc5_candidate_decision(
                        artifact_store=self.workspace.artifact_store, run_store=self.workspace.run_store,
                        execution=execution, run_id=run_id, todo_id=todo.todo_id, lease_id=lease.lease_id,
                        checkpoint=checkpoint, failure_kind=kind, base_disposition=disposition,
                        candidate_valid=candidate_valid, contract=todo.work_contract,
                    )
                    if rc5_decision is not None:
                        reused = rc5_decision.disposition is CandidateDispositionV1.REUSE_CANDIDATE
                        record_candidate_disposition(
                            run_store=self.workspace.run_store, run_id=run_id, todo_id=todo.todo_id,
                            checkpoint=checkpoint, reused=reused, reason=";".join(rc5_decision.reason_codes),
                        )
                        raw = dict(result.raw)
                        raw.update({
                            "recovery_workspace_disposition": rc5_decision.disposition.value,
                            "candidate_checkpoint_fingerprint": checkpoint.fingerprint,
                            "candidate_reuse_reason": ";".join(rc5_decision.reason_codes),
                            "candidate_continuation_decision": rc5_decision.to_dict(),
                            "candidate_continuation_decision_id": rc5_decision_id,
                        })
                        if rc5_decision.disposition is CandidateDispositionV1.BLOCK:
                            return RecoveryObservationV1(False, None, result.context_artifact_ids,
                                                         "RC-5 candidate continuation blocked unsafe recovery", None, (), raw)
                        result = RecoveryObservationV1(
                            result.continue_work, result.revised_objective, result.context_artifact_ids,
                            result.reason, result.routing_task_spec, result.expansion_queries, raw,
                        )
                        if reused:
                            record_delta_summary(
                                artifact_store=self.workspace.artifact_store, run_store=self.workspace.run_store,
                                run_id=run_id, todo_id=todo.todo_id, checkpoint=checkpoint,
                            )
                            return result
                        if hasattr(self.workspace, "rotate"):
                            self.workspace.rotate(todo)
                        return result
            if disposition is RecoveryWorkspaceDispositionV1.REUSE_CANDIDATE:
                checkpoint = checkpoint_candidate(
                    artifact_store=self.workspace.artifact_store,
                    run_store=self.workspace.run_store,
                    manager=self.workspace.manager,
                    run_id=run_id,
                    todo_id=todo.todo_id,
                    lease=lease,
                    failure_kind=kind,
                )
                if checkpoint is not None:
                    allowed, detail = validate_reuse(
                        manager=self.workspace.manager,
                        lease=lease,
                        checkpoint=checkpoint,
                        contract=todo.work_contract,
                    )
                    rc5_decision, rc5_decision_id = _rc5_candidate_decision(
                        artifact_store=self.workspace.artifact_store, run_store=self.workspace.run_store,
                        execution=execution, run_id=run_id, todo_id=todo.todo_id, lease_id=lease.lease_id,
                        checkpoint=checkpoint, failure_kind=kind, base_disposition=disposition,
                        candidate_valid=allowed, contract=todo.work_contract,
                    )
                    if rc5_decision is not None:
                        if rc5_decision.disposition is CandidateDispositionV1.BLOCK:
                            reason = "RC-5 candidate continuation blocked unsafe recovery"
                            raw = dict(result.raw)
                            raw.update({"recovery_workspace_disposition": "BLOCK",
                                        "candidate_continuation_decision": rc5_decision.to_dict(),
                                        "candidate_continuation_decision_id": rc5_decision_id})
                            return RecoveryObservationV1(False, None, result.context_artifact_ids, reason, None, (), raw)
                        allowed = rc5_decision.disposition is CandidateDispositionV1.REUSE_CANDIDATE
                        detail = ";".join(rc5_decision.reason_codes)
                    record_candidate_disposition(
                        run_store=self.workspace.run_store,
                        run_id=run_id,
                        todo_id=todo.todo_id,
                        checkpoint=checkpoint,
                        reused=allowed,
                        reason=detail,
                    )
                    raw = dict(result.raw)
                    raw.update({
                        "recovery_workspace_disposition": (
                            "REUSE_CANDIDATE" if allowed else "FRESH_CANDIDATE"
                        ),
                        "candidate_checkpoint_fingerprint": checkpoint.fingerprint,
                        "candidate_reuse_reason": detail,
                    })
                    result = RecoveryObservationV1(
                        result.continue_work,
                        result.revised_objective,
                        result.context_artifact_ids,
                        result.reason,
                        result.routing_task_spec,
                        result.expansion_queries,
                        raw,
                    )
                    if allowed:
                        if (kind == FailureKindV1.VERIFICATION_FAILED.value
                                and result.raw.get("production_action") == "RETRY_EXECUTION"):
                            activation = build_repair_activation(
                                todo=todo, run_id=run_id, lease_id=lease.lease_id,
                                checkpoint=checkpoint, verification=verification,
                                recovery=result, recovery_cycle=recovery_cycle,
                            )
                            activation_id = persist_repair_activation(
                                artifact_store=self.workspace.artifact_store,
                                run_store=self.workspace.run_store, activation=activation,
                            )
                            _trace_event(self.trace_registry, "REPAIR_ACTIVATION_CREATED", run_id=run_id, todo_id=todo.todo_id, payload={"candidate_lease_id": lease.lease_id, "activation_id": activation.fingerprint, "activation_artifact_id": activation_id, "verification_report_id": activation.verification_report_id, "failure_kind": activation.failure_kind})
                            _trace_event(self.trace_registry, "FRESH_VERIFICATION_REQUIRED", run_id=run_id, todo_id=todo.todo_id, payload={"candidate_lease_id": lease.lease_id, "activation_id": activation.fingerprint})
                            raw = dict(result.raw)
                            raw.update({
                                "repair_activation_id": activation.fingerprint,
                                "repair_activation_artifact_id": activation_id,
                                "agent_execution_state": "REOPEN_FOR_REPAIR",
                                "active_failure_kind": FailureKindV1.VERIFICATION_FAILED.value,
                            })
                            result = RecoveryObservationV1(
                                result.continue_work, result.revised_objective,
                                result.context_artifact_ids, result.reason,
                                result.routing_task_spec, result.expansion_queries, raw,
                            )
                        if kind == "TOOL_LOOP":
                            window = persist_tool_loop_reuse_window(
                                run_store=self.workspace.run_store,
                                artifact_store=self.workspace.artifact_store,
                                run_id=run_id,
                                todo_id=todo.todo_id,
                                lease_id=lease.lease_id,
                                recovery_cycle=recovery_cycle,
                                checkpoint_fingerprint=checkpoint.fingerprint,
                            )
                            raw = dict(result.raw)
                            raw.update({
                                "anti_loop_window_activation_id": window.activation_id,
                                "anti_loop_window_baseline_event_sequence": window.baseline_event_sequence,
                            })
                            result = RecoveryObservationV1(
                                result.continue_work,
                                result.revised_objective,
                                result.context_artifact_ids,
                                result.reason,
                                result.routing_task_spec,
                                result.expansion_queries,
                                raw,
                            )
                        record_delta_summary(
                            artifact_store=self.workspace.artifact_store,
                            run_store=self.workspace.run_store,
                            run_id=run_id,
                            todo_id=todo.todo_id,
                            checkpoint=checkpoint,
                        )
                        return result
            if hasattr(self.workspace, "rotate"):
                self.workspace.rotate(todo)
        return result

class WorkflowSnapshotAdapter:
    def __init__(self, *, source_repo_root: Path, workspace: ProductionWorkspaceCoordinator) -> None:
        self.source_repo_root = Path(source_repo_root).resolve(strict=True)
        self.workspace = workspace

    def current(self, *, goal_id: str, epoch: int) -> ProjectSnapshotV1:
        if epoch == 0:
            head = _git(self.source_repo_root, "rev-parse", "HEAD")
            diff = _git(self.source_repo_root, "diff", "--binary", "HEAD", "--", check=False)
            status = _git(self.source_repo_root, "status", "--porcelain=v1", "--untracked-files=all", check=False)
            dirty = tuple(sorted(line[3:].strip() for line in status.splitlines() if len(line) >= 4))
            return ProjectSnapshotV1(goal_id, 0, head, _sha(diff), dirty)
        return self.workspace.commit_current(goal_id=goal_id, epoch=epoch)


class WorkflowFinalSynthesisAdapter:
    def __init__(self, spine: ExternalContextSpine) -> None:
        self.spine = spine

    def synthesize(self, *, goal_id: str, completed_todo_ids: tuple[str, ...]) -> str:
        rows = []
        for todo_id in completed_todo_ids:
            outcome, _ = self.spine.verified_outcome(goal_id, todo_id)
            rows.append(f"{todo_id}: {outcome.summary}")
        return "Verified mission outcomes:\n" + "\n".join(rows)


class NoDagAmendmentAdapter:
    'Production default: workflow recovery handles a Todo; no speculative DAG mutation.'

    def propose(self, spec, checkpoint, workflow_result):
        return None

    def apply(self, amendment) -> None:
        raise RuntimeError("no production DAG amendment was proposed")


def _judge_replan_amendment_id(artifact_id: str) -> str:
    return f"judge-replan-{hashlib.sha256(artifact_id.encode()).hexdigest()[:20]}"


class JudgeReplanAmendmentAdapter:
    """Apply only a durable Judge-requested, model-produced Todo revision."""

    def __init__(self, *, artifact_store: ArtifactStore, scheduler: WorkflowSchedulerAdapter) -> None:
        self.artifact_store = artifact_store
        self.scheduler = scheduler

    def propose(self, spec, checkpoint, workflow_result):
        todo_id = workflow_result.failed_todo_id
        if not todo_id or workflow_result.final_summary != f"semantic_replan_required:{todo_id}":
            return None
        used = {evidence_id for amendment in checkpoint.amendments for evidence_id in amendment.evidence_ids}
        candidates = []
        for ref in self.artifact_store.list_refs():
            artifact = self.artifact_store.get_artifact(ref.artifact_id)
            if (artifact.metadata.get("phase16_role") != "RECOVERY_CONTEXT"
                    or artifact.metadata.get("goal_id") != spec.goal_id
                    or artifact.metadata.get("todo_id") != todo_id
                    or artifact.artifact_id in used):
                continue
            payload = json.loads(artifact.content)
            recovery = payload.get("recovery", {})
            if recovery.get("semantic_replan_required") is True:
                candidates.append((artifact.artifact_id, recovery))
        if len(candidates) != 1:
            raise RuntimeError("Judge REPLAN must have exactly one unconsumed durable recovery artifact")
        artifact_id, recovery = candidates[0]
        cause = recovery.get("review_recovery_cause", {})
        if cause.get("kind") != "REVIEW_REPLAN" or recovery.get("controller_action") != "ORNITH_REPLAN":
            raise RuntimeError("durable Judge/Ornith replan authority mismatch")
        objective = recovery.get("revised_objective")
        if not isinstance(objective, str) or not objective.strip():
            raise RuntimeError("Ornith replan lacks revised objective")
        if objective == self.scheduler.store.get_todo(todo_id).objective:
            raise RuntimeError("Ornith replan did not revise the Todo objective")
        return DagAmendmentV1(
            amendment_id=_judge_replan_amendment_id(artifact_id),
            reason="Judge REPLAN -> Ornith revised Todo objective",
            operations=(DagAmendmentOperationV1(
                DagOperationKind.REVISE_PENDING_TODO, todo_id,
                payload={"revised_objective": objective},
            ),),
            evidence_ids=(artifact_id,),
        )

    def apply(self, amendment) -> None:
        if len(amendment.operations) != 1:
            raise RuntimeError("Judge replan accepts exactly one Todo revision")
        operation = amendment.operations[0]
        if operation.kind is not DagOperationKind.REVISE_PENDING_TODO:
            raise RuntimeError("unsupported Judge replan DAG operation")
        objective = operation.payload.get("revised_objective")
        self.scheduler.store.revise_replan_required(
            todo_id=operation.todo_id, objective=objective,
            amendment_id=amendment.amendment_id,
        )


class ProductionCompletionAdapter:
    def __init__(self, *, spine: ExternalContextSpine, scheduler: WorkflowSchedulerAdapter, verification_policy=None) -> None:
        self.spine = spine
        self.scheduler = scheduler
        self.verification_policy = verification_policy

    def evaluate(self, spec: MissionSpecV1, checkpoint: MissionCheckpointV1, workflow_result) -> GlobalCompletionGateV1:
        from harness.verification.global_contract import GLOBAL_VERIFICATION
        from .external_evaluation import independent_acceptance_required
        independent = independent_acceptance_required(self.spine, spec)
        if GLOBAL_VERIFICATION in getattr(spec, 'global_constraints', {}) or independent:
            return self._evaluate_integrated(spec, checkpoint, workflow_result)
        expected = () if checkpoint.plan is None else checkpoint.plan.todo_ids
        completed = set(checkpoint.completed_todo_ids)
        evidence = []
        all_verified = bool(expected) and all(todo_id in completed for todo_id in expected)
        if all_verified:
            for todo_id in expected:
                _, ref = self.spine.verified_outcome(spec.goal_id, todo_id)
                evidence.append(ref.artifact_id)
        try:
            self.spine.root_context(spec.goal_id)
            root_ok = True
            snapshot, snapshot_ref = self.spine.latest_project_snapshot(spec.goal_id)
            evidence.append(snapshot_ref.artifact_id)
            clean = not snapshot.dirty_paths
            snapshot_ok = bool(snapshot.head_commit)
        except ContextResolutionError:
            root_ok = False
            clean = False
            snapshot_ok = False
        succeeded = workflow_result.outcome.value == "SUCCEEDED"
        scheduler_ok = self.scheduler.all_succeeded(spec.goal_id)
        return GlobalCompletionGateV1(
            workflow_succeeded=succeeded,
            all_todos_verified=all_verified and scheduler_ok,
            final_regression_passed=all_verified,
            root_invariants_satisfied=root_ok,
            no_blocked_todos=scheduler_ok,
            workspace_clean=clean,
            project_snapshot_consistent=snapshot_ok,
            evidence_ids=tuple(dict.fromkeys(evidence)),
            summary=(
                "All Phase16 Todos passed configured deterministic verification, "
                "accepted candidate commits are clean, and durable context is consistent."
                if succeeded and all_verified and scheduler_ok and root_ok and clean and snapshot_ok
                else "Global completion invariants are incomplete."
            ),
        )

    def _evaluate_integrated(self, spec, checkpoint, workflow_result):
        from .global_verification import GlobalIntegratedVerifier, admit_global_contract
        from harness.verification.global_contract import GlobalVerificationError
        evidence=[];complete=False;clean=False;snapshot_ok=False;passed=False;independent_pass=False
        succeeded=workflow_result.outcome.value=='SUCCEEDED'
        service=None
        try:
            if self.verification_policy is None:
                raise GlobalVerificationError('GLOBAL_POLICY_REQUIRED','trusted production verification policy')
            service=GlobalIntegratedVerifier(workspace=self.scheduler.workspace,scheduler=self.scheduler,policy=self.verification_policy)
            contract=admit_global_contract(spec,self.verification_policy)
            _,receipts,_=service.resolve_input(spec,contract)
            required={d['producer_physical_todo_id'] for d in receipts}
            expected=set(() if checkpoint.plan is None else checkpoint.plan.todo_ids)
            complete=bool(required) and expected==required==set(checkpoint.completed_todo_ids) and self.scheduler.all_succeeded(spec.goal_id)
            if not succeeded or not complete:
                raise GlobalVerificationError('GLOBAL_TODO_COMPLETION_REQUIRED','all exact admitted Todos must finish first')
            receipt=service.verify(spec).to_dict()
            passed=receipt['global_verification_status']=='PASSED'
            clean=snapshot_ok=passed
            evidence.append(service.key(spec,'receipt'))
            summary='Fresh global integrated Phase7 PASSED; INDEPENDENT_ACCEPTANCE_NOT_QUALIFIED (C1.5).'
            from .external_evaluation import independent_acceptance_required, independent_verifier_type
            if independent_acceptance_required(self.spine, spec):
                service=independent_verifier_type()(workspace=self.scheduler.workspace,scheduler=self.scheduler,policy=self.verification_policy)
                accepted=service.verify(spec)
                independent_pass=accepted.to_dict()['independent_acceptance_status']=='PASSED'
                evidence.append(service.key(spec,'receipt'))
                summary='Fresh global Phase7 and trusted independent oracle PASSED; exact accepted final tree verified.'
        except Exception as exc:
            if service is not None:evidence.append(service.failure(spec,exc))
            summary=str(exc)[:16000]
        return GlobalCompletionGateV1(succeeded,complete,passed,independent_pass,complete,clean,snapshot_ok,
            tuple(evidence),summary)

    def revalidate_completed(self,spec,checkpoint):
        """Terminal restart validates acceptance without executing any process."""
        from .external_evaluation import independent_acceptance_required, independent_verifier_type
        if not independent_acceptance_required(self.spine, spec):return None
        try:
            self.scheduler.bind_spec(spec)
            service=independent_verifier_type()(workspace=self.scheduler.workspace,scheduler=self.scheduler,policy=self.verification_policy)
            service.validate(spec)
            return checkpoint.completion_gate
        except Exception as exc:
            return replace(checkpoint.completion_gate,root_invariants_satisfied=False,
                summary='INDEPENDENT_TERMINAL_INVALID: '+str(exc)[:12000])

    def finalize_summary(self,spec,summary,gate):
        from .external_evaluation import independent_acceptance_required
        if not independent_acceptance_required(self.spine, spec):return summary
        if not gate.passed:raise RuntimeError('INDEPENDENT_FINAL_SUMMARY_REQUIRES_ACCEPTANCE')
        return summary+'\nFinal accepted-tree verification evidence: '+', '.join(gate.evidence_ids)

__all__ = ['ContextProfileRouterAdapter', 'NoDagAmendmentAdapter', 'ProductionCompletionAdapter', 'ProductionWorkspaceCoordinator', 'VerificationTodoOverlayRunStore', 'WorkflowExecutionAdapter', 'WorkflowFinalSynthesisAdapter', 'WorkflowRecoveryAdapter', 'WorkflowRepositoryAdapter', 'WorkflowReviewAdapter', 'WorkflowSchedulerAdapter', 'WorkflowSnapshotAdapter', 'WorkflowVerificationAdapter']

# Verified legacy class/pickle aliases; outside canonical __all__.
Phase16ContextProfileRouterAdapter = ContextProfileRouterAdapter
Phase16ExecutionAdapter = WorkflowExecutionAdapter
Phase16FinalSynthesisAdapter = WorkflowFinalSynthesisAdapter
Phase16RecoveryAdapter = WorkflowRecoveryAdapter
Phase16RepositoryAdapter = WorkflowRepositoryAdapter
Phase16ReviewAdapter = WorkflowReviewAdapter
Phase16SchedulerAdapter = WorkflowSchedulerAdapter
Phase16SnapshotAdapter = WorkflowSnapshotAdapter
Phase16VerificationAdapter = WorkflowVerificationAdapter

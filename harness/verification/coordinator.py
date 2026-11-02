'verification deterministic verification orchestration around a candidate worktree.'
from __future__ import annotations

from ..context_manager.store import ArtifactStore
from ..control_plane.models import EvidenceStatus, GateStatus, Run, RunStatus, WorkContract
from ..control_plane.store import RunStore
from ..worktree.manager import GitWorktreeManager
from .errors import VerificationIntegrityError, VerificationStateError
from .models import VerificationCoordinatorResult, VerificationOutcome
from .policy import VerificationPlanCompiler, VerificationPolicy
from .publisher import VerificationPublisher
from .runner import VerificationRunner


class VerificationCoordinator:
    def __init__(
        self,
        *,
        run_store: RunStore,
        artifact_store: ArtifactStore,
        manager: GitWorktreeManager,
        policy: VerificationPolicy,
    ) -> None:
        self.run_store = run_store
        self.artifact_store = artifact_store
        self.manager = manager
        self.policy = policy
        self.compiler = VerificationPlanCompiler(policy)
        self.runner = VerificationRunner(manager=manager, policy=policy)
        self.publisher = VerificationPublisher(run_store=run_store, artifact_store=artifact_store)

    def verify(self, *, run_id: str, contract: WorkContract, lease_id: str) -> VerificationCoordinatorResult:
        run = self.run_store.get_run(run_id)
        if run.active_todo_id != contract.todo_id or run.goal_id != contract.goal_id:
            raise VerificationStateError("Run and WorkContract identity mismatch")
        if run.status not in (RunStatus.RUNNING, RunStatus.COMPLETED, RunStatus.FAILED):
            raise VerificationStateError(f"Run status is not verifiable: {run.status.value}")
        todo = self.run_store.get_todo(contract.todo_id)
        lease = self.manager.get_active(lease_id)
        if lease.run_id != run_id or lease.todo_id != todo.todo_id:
            raise VerificationStateError("worktree lease does not belong to Run/Todo")
        plan = self.compiler.compile(run=run, todo=todo, contract=contract, lease=lease)
        snapshot = self.manager.snapshot(lease_id)
        self._guard_terminal_snapshot(run_id, contract.todo_id, run.status, snapshot.diff_sha256, snapshot.content_sha256)
        report_id = self.runner.report_id(plan, snapshot)
        existing = self.publisher.find_existing(report_id=report_id)
        reused = existing is not None
        if existing is not None:
            if existing.candidate_content_sha256 != snapshot.content_sha256:
                raise VerificationIntegrityError('persisted verification raw candidate identity mismatch')
            self.publisher.validate_report_identity(existing, plan, head=snapshot.head_commit, diff_sha256=snapshot.diff_sha256)
            report = existing
        else:
            report = self.runner.run(plan=plan, contract=contract)
        self.publisher.publish(report)
        run = self._update_run(run_id, contract, report.outcome)
        return VerificationCoordinatorResult(report, reused, run.status.value)


    def _guard_terminal_snapshot(self, run_id: str, todo_id: str, status: RunStatus, diff_sha256: str, content_sha256=None) -> None:
        recorded: set[tuple[str, str | None]] = set()
        for event in self.run_store.list_events(run_id):
            if event.event_type.value != "EVIDENCE_RECORDED":
                continue
            payload = event.to_dict()["payload"]
            if payload.get("todo_id") != todo_id or payload.get("evidence_type") != "VERIFICATION_REPORT":
                continue
            detail = payload.get("payload")
            if not isinstance(detail, dict):
                raise VerificationIntegrityError("verification Evidence payload must be an object")
            report_id = detail.get("report_id")
            recorded_diff = detail.get("candidate_diff_sha256")
            if not isinstance(report_id, str) or not report_id or not isinstance(recorded_diff, str):
                raise VerificationIntegrityError("verification Evidence identity is incomplete")
            persisted = self.publisher.find_existing(report_id=report_id)
            if persisted is None:
                raise VerificationIntegrityError("verification Evidence references missing report artifact")
            if persisted.candidate_diff_sha256 != recorded_diff:
                raise VerificationIntegrityError("verification Evidence/report candidate digest mismatch")
            recorded.add((recorded_diff, persisted.candidate_content_sha256))
        if status in (RunStatus.COMPLETED, RunStatus.FAILED) and recorded and (diff_sha256, content_sha256) not in recorded:
            raise VerificationIntegrityError(f"{status.value.lower()} Run candidate changed after verification")

    def _update_run(self, run_id: str, contract: WorkContract, outcome: VerificationOutcome) -> Run:
        run = self.run_store.get_run(run_id)
        if outcome is VerificationOutcome.FAILED:
            if run.status is RunStatus.COMPLETED:
                raise VerificationIntegrityError("completed Run cannot acquire a failing verification report")
            if run.status is not RunStatus.FAILED:
                run = self.run_store.update_run(
                    Run(run.run_id, run.goal_id, run.active_todo_id, RunStatus.FAILED, run.quota_usage)
                )
            return run
        if not self._requirements_satisfied(run_id, contract):
            return run
        if run.status is RunStatus.FAILED:
            raise VerificationIntegrityError("failed Run cannot be converted to COMPLETED by replay")
        if run.status is RunStatus.RUNNING:
            run = self.run_store.update_run(
                Run(run.run_id, run.goal_id, run.active_todo_id, RunStatus.COMPLETED, run.quota_usage)
            )
        return run

    def _requirements_satisfied(self, run_id: str, contract: WorkContract) -> bool:
        evidence: set[str] = set()
        gates: dict[str, str] = {}
        for event in self.run_store.list_events(run_id):
            payload = event.to_dict()["payload"]
            if event.event_type.value == "EVIDENCE_RECORDED" and payload.get("todo_id") == contract.todo_id:
                if payload.get("status") == EvidenceStatus.PASSED.value:
                    evidence_type = payload.get("evidence_type")
                    if isinstance(evidence_type, str):
                        evidence.add(evidence_type)
            elif event.event_type.value == "GATE_RECORDED" and payload.get("todo_id") == contract.todo_id:
                gate = payload.get("gate")
                if isinstance(gate, dict) and isinstance(gate.get("gate_id"), str):
                    gates[gate["gate_id"]] = str(gate.get("status"))
        todo = self.run_store.get_todo(contract.todo_id)
        return all(item in evidence for item in contract.required_evidence) and all(
            gates.get(gate_id) == GateStatus.SATISFIED.value for gate_id in todo.required_gates
        )

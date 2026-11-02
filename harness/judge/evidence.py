'Resolve the exact current verification + critic evidence for judge Judge.'
from __future__ import annotations

from typing import Any
import shlex

from ..context_manager.store import ArtifactStore
from ..control_plane.models import WorkContract
from ..control_plane.store import RunStore
from ..critic.models import CriticReport
from ..critic.publisher import CriticPublisher
from ..verification.models import VerificationReport
from ..verification.publisher import VerificationPublisher
from ..worktree.manager import GitWorktreeManager
from .errors import JudgeIntegrityError, JudgeStateError
from .models import JudgeEvidenceBundle, JudgePolicy, request_fingerprint


def _truncate_utf8(value: str, max_bytes: int) -> str:
    data = value.encode("utf-8")
    if len(data) <= max_bytes:
        return value
    marker = "\n...[TRUNCATED BY PHASE 9]"
    budget = max(0, max_bytes - len(marker.encode("utf-8")))
    return data[:budget].decode("utf-8", errors="ignore") + marker


def _candidate_diff_by_kind(diff, *, tests):
    from ..repo_intelligence.scanner import _kind
    from ..repo_intelligence.models import RepositoryFileKind
    selected=[]
    for block in diff.split('diff --git ')[1:]:
        header=block.splitlines()[0]
        try:
            tokens=shlex.split(header)
        except ValueError:
            continue
        if len(tokens)==2 and tokens[1].startswith('b/') and (_kind(tokens[1][2:]) is RepositoryFileKind.TEST) == tests:
            selected.append('diff --git '+block)
    return ''.join(selected)


def _generated_test_diff(diff):
    return _candidate_diff_by_kind(diff, tests=True)


class JudgeEvidenceResolver:
    def __init__(self, *, run_store: RunStore, artifact_store: ArtifactStore, manager: GitWorktreeManager, policy: JudgePolicy, require_acceptance=False) -> None:
        self.run_store = run_store
        self.artifact_store = artifact_store
        self.manager = manager
        self.policy = policy
        self.require_acceptance = require_acceptance
        self.verification = VerificationPublisher(run_store=run_store, artifact_store=artifact_store)
        self.critic = CriticPublisher(run_store=run_store, artifact_store=artifact_store)

    def resolve(self, *, run_id: str, contract: WorkContract, lease_id: str) -> JudgeEvidenceBundle:
        run = self.run_store.get_run(run_id)
        todo = self.run_store.get_todo(contract.todo_id)
        if run.goal_id != contract.goal_id or todo.goal_id != contract.goal_id or run.active_todo_id != contract.todo_id:
            raise JudgeStateError("Run/Todo/WorkContract identity mismatch")
        lease = self.manager.get_active(lease_id)
        if lease.run_id != run_id or lease.todo_id != contract.todo_id:
            raise JudgeStateError("worktree lease does not belong to Run/Todo")
        snapshot = self.manager.snapshot(lease_id)
        verification = self._current_verification(run_id, contract.todo_id, lease_id, snapshot.diff_sha256)
        if verification.candidate_head != snapshot.head_commit or verification.candidate_content_sha256 != getattr(snapshot, 'content_sha256', None):
            raise JudgeIntegrityError('verification report raw candidate identity mismatch')
        critic = self._current_critic(run_id, contract.todo_id, verification, snapshot.diff_sha256)
        if critic.analysis.deterministic_outcome is not verification.outcome:
            raise JudgeIntegrityError("CriticReport deterministic outcome does not match VerificationReport")
        from ..integration.acceptance_bridge import load_frozen_acceptance
        acceptance = load_frozen_acceptance(self.run_store, run_id, contract)
        if acceptance is None and self.require_acceptance:
            from ..verification.acceptance import build_acceptance_obligations
            # Legacy/resumed candidates cannot invent a pre-mutation snapshot.
            # The original contract is known; missing public provenance remains
            # unresolved rather than retroactively freezing candidate content.
            acceptance = build_acceptance_obligations(run_id=run_id, contract=contract,
                sources={}, tests={}, unresolved=('Missing pre-mutation acceptance snapshot',))
        hypotheses = ()
        for event in self.run_store.list_events(run_id):
            entry = event.to_dict()['payload']
            detail = entry.get('payload',{})
            if (entry.get('todo_id') == contract.todo_id and entry.get('evidence_type') == 'SIMPLE_WORKER_EVENT'
                    and detail.get('kind') == 'EDIT_INTENT_RESOLVED'):
                hypotheses = tuple(detail['intent']['relevant_facts'])
        fingerprint = request_fingerprint({
            "run_id": run_id,
            "goal_id": contract.goal_id,
            "todo_id": contract.todo_id,
            "lease_id": lease_id,
            "verification_report_id": verification.report_id,
            "critic_report_id": critic.report_id,
            "candidate_head": snapshot.head_commit,
            "candidate_diff_sha256": snapshot.diff_sha256,
            "work_contract": contract.to_dict(),
            "policy_model": self.policy.model_id,
            'acceptance':None if acceptance is None else acceptance.to_dict(),
            'explorer_hypotheses':hypotheses,
        })
        return JudgeEvidenceBundle(
            request_id=f"{run_id}:judge:{fingerprint[:20]}",
            request_fingerprint=fingerprint,
            run_id=run_id,
            goal_id=contract.goal_id,
            todo_id=contract.todo_id,
            objective=contract.objective,
            verification_report=verification,
            critic_report=critic,
            candidate_head=snapshot.head_commit,
            candidate_diff_sha256=snapshot.diff_sha256,
            candidate_diff_text=_truncate_utf8(snapshot.diff_text, self.policy.max_diff_bytes),
            dirty_paths=snapshot.dirty_paths,
            work_contract=contract.to_dict(),
            allowed_evidence_ids=frozenset(("verification_report", "critic_report", "candidate_diff")),
            acceptance_obligations=acceptance,
            generated_test_diff_text=_truncate_utf8(_generated_test_diff(snapshot.diff_text),32768),
            explorer_hypotheses=hypotheses,
        )

    def _current_verification(self, run_id: str, todo_id: str, lease_id: str, diff_sha256: str) -> VerificationReport:
        matches: list[VerificationReport] = []
        for event in self.run_store.list_events(run_id):
            if event.event_type.value != "EVIDENCE_RECORDED":
                continue
            payload = event.to_dict()["payload"]
            if payload.get("todo_id") != todo_id or payload.get("evidence_type") != "VERIFICATION_REPORT":
                continue
            detail = payload.get("payload")
            if not isinstance(detail, dict) or not isinstance(detail.get("report_id"), str):
                raise JudgeIntegrityError("VERIFICATION_REPORT evidence identity is invalid")
            report = self.verification.find_existing(report_id=detail["report_id"])
            if report is None:
                raise JudgeIntegrityError("VERIFICATION_REPORT evidence references missing artifact")
            if report.lease_id == lease_id and report.candidate_diff_sha256 == diff_sha256:
                matches.append(report)
        if not matches:
            raise JudgeStateError("no verification report exists for current candidate snapshot")
        return matches[-1]

    def _current_critic(self, run_id: str, todo_id: str, verification: VerificationReport, diff_sha256: str) -> CriticReport:
        matches: list[CriticReport] = []
        for event in self.run_store.list_events(run_id):
            if event.event_type.value != "EVIDENCE_RECORDED":
                continue
            payload = event.to_dict()["payload"]
            if payload.get("todo_id") != todo_id or payload.get("evidence_type") != "CRITIC_REPORT":
                continue
            detail = payload.get("payload")
            if not isinstance(detail, dict) or not isinstance(detail.get("report_id"), str):
                raise JudgeIntegrityError("CRITIC_REPORT evidence identity is invalid")
            report = self.critic.find_report(detail["report_id"])
            if report is None:
                raise JudgeIntegrityError("CRITIC_REPORT evidence references missing artifact")
            if report.verification_report_id == verification.report_id and report.candidate_diff_sha256 == diff_sha256:
                matches.append(report)
        if not matches:
            raise JudgeStateError("no critic report exists for current verification/candidate snapshot")
        return matches[-1]

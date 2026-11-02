'Build a bounded, artifact-grounded critic critic evidence bundle.'
from __future__ import annotations

from typing import Any

from ..context_manager.integration import parse_artifact_uri
from ..context_manager.store import ArtifactStore
from ..control_plane.models import WorkContract
from ..control_plane.store import RunStore
from ..verification.models import VerificationReport, source_revision_identity
import hashlib
from .verification_context import verification_context
from ..verification.publisher import VerificationPublisher
from ..worktree.manager import GitWorktreeManager
from .errors import CriticIntegrityError, CriticStateError
from .models import CriticEvidenceBundle, CriticEvidenceItem, CriticPolicy, canonical_json, request_fingerprint


def _truncate_utf8(value: str, max_bytes: int) -> str:
    data = value.encode("utf-8")
    if len(data) <= max_bytes:
        return value
    marker = "\n...[TRUNCATED BY PHASE 8]"
    budget = max(0, max_bytes - len(marker.encode("utf-8")))
    return data[:budget].decode("utf-8", errors="ignore") + marker


class CriticEvidenceResolver:
    def __init__(self, *, run_store: RunStore, artifact_store: ArtifactStore, manager: GitWorktreeManager, policy: CriticPolicy) -> None:
        self.run_store = run_store
        self.artifact_store = artifact_store
        self.manager = manager
        self.policy = policy
        self.verification = VerificationPublisher(run_store=run_store, artifact_store=artifact_store)

    def resolve(self, *, run_id: str, contract: WorkContract, lease_id: str) -> CriticEvidenceBundle:
        run = self.run_store.get_run(run_id)
        todo = self.run_store.get_todo(contract.todo_id)
        if run.goal_id != contract.goal_id or todo.goal_id != contract.goal_id or run.active_todo_id != contract.todo_id:
            raise CriticStateError("Run/Todo/WorkContract identity mismatch")
        lease = self.manager.get_active(lease_id)
        if lease.run_id != run_id or lease.todo_id != contract.todo_id:
            raise CriticStateError("worktree lease does not belong to Run/Todo")
        snapshot = self.manager.snapshot(lease_id)
        verification_event, report = self._current_verification(run_id, contract.todo_id, lease_id, snapshot.diff_sha256)
        if report.candidate_content_sha256 != getattr(snapshot, 'content_sha256', None):
            raise CriticIntegrityError('verification report raw candidate identity mismatch')
        if report.run_id != run_id or report.todo_id != contract.todo_id or report.candidate_head != snapshot.head_commit:
            raise CriticIntegrityError("verification report Run/Todo/revision identity mismatch")
        original_identity = source_revision_identity(lease.base_commit, hashlib.sha256(b'').hexdigest())
        evidence = self._evidence_items(run_id, contract.todo_id, verification_event, snapshot.diff_sha256)
        diff = _truncate_utf8(snapshot.diff_text, self.policy.max_diff_bytes)
        fingerprint_payload = {
            "run_id": run_id,
            "goal_id": contract.goal_id,
            "todo_id": contract.todo_id,
            "lease_id": lease_id,
            "verification_report_id": report.report_id,
            "verification_outcome": report.outcome.value,
            "candidate_head": snapshot.head_commit,
            "candidate_diff_sha256": snapshot.diff_sha256,
            "work_contract": contract.to_dict(),
            "evidence_ids": [item.evidence_id for item in evidence],
            "policy_model": self.policy.model_id,
            "verification_report": report.to_dict(),
            "original_source_revision_identity": original_identity,
        }
        fingerprint = request_fingerprint(fingerprint_payload)
        bundle = CriticEvidenceBundle(
            request_id=f"{run_id}:critic:{fingerprint[:20]}",
            request_fingerprint=fingerprint,
            run_id=run_id,
            goal_id=contract.goal_id,
            todo_id=contract.todo_id,
            objective=contract.objective,
            verification_report_id=report.report_id,
            verification_outcome=report.outcome,
            candidate_head=snapshot.head_commit,
            candidate_diff_sha256=snapshot.diff_sha256,
            candidate_diff_text=diff,
            dirty_paths=snapshot.dirty_paths,
            work_contract=contract.to_dict(),
            verification_report=report.to_dict(),
            evidence_items=evidence,
            original_source_revision_identity=original_identity,
        )
        verification_context(bundle)  # Mandatory validation before routing/model dispatch.
        return bundle

    def _current_verification(self, run_id: str, todo_id: str, lease_id: str, diff_sha256: str) -> tuple[dict[str, Any], VerificationReport]:
        matches: list[tuple[dict[str, Any], VerificationReport]] = []
        for event in self.run_store.list_events(run_id):
            if event.event_type.value != "EVIDENCE_RECORDED":
                continue
            payload = event.to_dict()["payload"]
            if payload.get("todo_id") != todo_id or payload.get("evidence_type") != "VERIFICATION_REPORT":
                continue
            detail = payload.get("payload")
            if not isinstance(detail, dict) or not isinstance(detail.get("report_id"), str):
                raise CriticIntegrityError("VERIFICATION_REPORT evidence identity is invalid")
            report = self.verification.find_existing(report_id=detail["report_id"])
            if report is None:
                raise CriticIntegrityError("VERIFICATION_REPORT evidence references missing report artifact")
            if report.lease_id == lease_id and report.candidate_diff_sha256 == diff_sha256:
                matches.append((payload, report))
        if not matches:
            raise CriticStateError("no verification report exists for current candidate snapshot")
        return matches[-1]

    def _evidence_items(self, run_id: str, todo_id: str, verification_event: dict[str, Any], diff_sha256: str) -> tuple[CriticEvidenceItem, ...]:
        items: list[CriticEvidenceItem] = []
        verification_id = str(verification_event["evidence_id"])
        detail = verification_event.get("payload", {})
        items.append(CriticEvidenceItem(
            verification_id,
            "VERIFICATION_REPORT",
            str(verification_event.get("source", "VerificationRunner")),
            str(verification_event.get("status", "UNKNOWN")),
            _truncate_utf8(canonical_json(detail if isinstance(detail, dict) else {}), self.policy.max_artifact_bytes),
            verification_event.get("reference") if isinstance(verification_event.get("reference"), str) else None,
        ))
        items.append(CriticEvidenceItem(
            "candidate_diff",
            "CANDIDATE_DIFF",
            "GitWorktreeManager",
            "OBSERVED",
            f"candidate diff sha256={diff_sha256}",
            None,
        ))
        existing_ids = {item.evidence_id for item in items}
        for event in self.run_store.list_events(run_id):
            if event.event_type.value != "EVIDENCE_RECORDED":
                continue
            payload = event.to_dict()["payload"]
            if payload.get("todo_id") != todo_id or payload.get("evidence_type") not in ("WORKER_RESULT", "TOOL_RESULT"):
                continue
            if len(items) >= self.policy.max_evidence_items:
                break
            evidence_id = payload.get("evidence_id")
            if not isinstance(evidence_id, str) or evidence_id in existing_ids:
                continue
            summary_payload = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
            summary = canonical_json(summary_payload)
            reference = payload.get("reference") if isinstance(payload.get("reference"), str) else None
            artifact_id = parse_artifact_uri(reference)
            if artifact_id is not None and self.artifact_store.has_artifact(artifact_id):
                artifact = self.artifact_store.get_artifact(artifact_id)
                summary += "\nARTIFACT_CONTENT\n" + artifact.content
            items.append(CriticEvidenceItem(
                evidence_id,
                str(payload.get("evidence_type")),
                str(payload.get("source", "unknown")),
                str(payload.get("status", "UNKNOWN")),
                _truncate_utf8(summary, self.policy.max_artifact_bytes),
                reference,
            ))
            existing_ids.add(evidence_id)
        return tuple(items[: self.policy.max_evidence_items])

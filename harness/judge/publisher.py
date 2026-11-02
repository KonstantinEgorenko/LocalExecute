'Durable judge raw turns and JUDGE_REPORT evidence.'
from __future__ import annotations

import json
from datetime import datetime, timezone

from ..context_manager.integration import artifact_uri, parse_artifact_uri
from ..context_manager.models import Artifact, ArtifactKind, ArtifactOrigin
from ..context_manager.store import ArtifactStore
from ..control_plane.models import Evidence, EvidenceStatus
from ..control_plane.store import RunStore
from .errors import JudgeIntegrityError
from .models import JudgeEvidenceBundle, JudgeReport, JudgeTurnRecord, canonical_json


class JudgePublisher:
    def __init__(self, *, run_store: RunStore, artifact_store: ArtifactStore) -> None:
        self.run_store = run_store
        self.artifact_store = artifact_store

    @staticmethod
    def turn_artifact_id(request_id: str, attempt_index: int) -> str:
        return f"{request_id}:turn:{attempt_index}"

    @staticmethod
    def report_artifact_id(request_id: str) -> str:
        return f"{request_id}:report"

    @staticmethod
    def evidence_id(request_id: str) -> str:
        return f"{request_id}:evidence"

    def publish_turn(self, turn: JudgeTurnRecord) -> str:
        artifact_id = self.turn_artifact_id(turn.request_id, turn.attempt_index)
        content = canonical_json(turn.to_dict())
        if self.artifact_store.has_artifact(artifact_id):
            if self.artifact_store.get_artifact(artifact_id).content != content:
                raise JudgeIntegrityError("existing judge turn artifact conflicts")
            return artifact_id
        self.artifact_store.create_artifact(Artifact.from_text(
            artifact_id=artifact_id,
            kind=ArtifactKind.OTHER,
            origin=ArtifactOrigin.CONTROL_PLANE,
            content=content,
            media_type="application/json",
            source="OrnithJudge",
            created_at=datetime.now(timezone.utc).isoformat(),
            metadata={"attempt_index": turn.attempt_index, "status": turn.status.value, "model_id": turn.model_id},
        ))
        return artifact_id

    def find_turn(self, request_id: str, attempt_index: int) -> JudgeTurnRecord | None:
        artifact_id = self.turn_artifact_id(request_id, attempt_index)
        if not self.artifact_store.has_artifact(artifact_id):
            return None
        artifact = self.artifact_store.get_artifact(artifact_id)
        if artifact.source != "OrnithJudge":
            raise JudgeIntegrityError("judge turn artifact has unexpected identity")
        try:
            payload = json.loads(artifact.content)
            if not isinstance(payload, dict):
                raise ValueError("turn payload must be object")
            turn = JudgeTurnRecord.from_dict(payload)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise JudgeIntegrityError(f"invalid persisted judge turn: {exc}") from exc
        if turn.request_id != request_id or turn.attempt_index != attempt_index:
            raise JudgeIntegrityError("persisted judge turn identity mismatch")
        return turn

    def publish_report(self, report: JudgeReport) -> None:
        artifact_id = self.report_artifact_id(report.report_id)
        content = canonical_json(report.to_dict())
        if self.artifact_store.has_artifact(artifact_id):
            if self.artifact_store.get_artifact(artifact_id).content != content:
                raise JudgeIntegrityError("existing judge report artifact conflicts")
        else:
            self.artifact_store.create_artifact(Artifact.from_text(
                artifact_id=artifact_id,
                kind=ArtifactKind.CONTROL_PLANE_EVIDENCE,
                origin=ArtifactOrigin.CONTROL_PLANE,
                content=content,
                media_type="application/json",
                source="OrnithJudge",
                created_at=datetime.now(timezone.utc).isoformat(),
                metadata={
                    "request_fingerprint": report.request_fingerprint,
                    "candidate_diff_sha256": report.candidate_diff_sha256,
                    "decision": report.analysis.decision.value,
                    "next_action": report.analysis.next_action.value,
                },
            ))
        evidence = Evidence(
            evidence_id=self.evidence_id(report.report_id),
            todo_id=report.todo_id,
            evidence_type="JUDGE_REPORT",
            source="OrnithJudge",
            status=EvidenceStatus.PASSED,
            payload={
                "report_id": report.report_id,
                "request_fingerprint": report.request_fingerprint,
                "verification_report_id": report.verification_report_id,
                "critic_report_id": report.critic_report_id,
                "candidate_diff_sha256": report.candidate_diff_sha256,
                "deterministic_outcome": report.analysis.deterministic_outcome.value,
                "decision": report.analysis.decision.value,
                "next_action": report.analysis.next_action.value,
                "confidence": report.analysis.confidence,
                "model_id": report.model_id,
                "model_tag": report.model_tag,
                "model_attempts": report.model_attempts,
                **({'acceptance_evidence':[e.to_dict() for e in report.analysis.acceptance_evidence],
                    'semantic_acceptance':'SATISFIED' if all(e.status == 'SATISFIED' for e in report.analysis.acceptance_evidence)
                        else 'VIOLATED' if any(e.status == 'VIOLATED' for e in report.analysis.acceptance_evidence) else 'UNRESOLVED'}
                   if report.analysis.acceptance_evidence else {}),
            },
            reference=artifact_uri(artifact_id),
        )
        existing = self._existing_evidence(report.run_id, evidence.evidence_id)
        if existing is None:
            self.run_store.record_evidence(report.run_id, evidence)
        elif existing != evidence.to_dict():
            raise JudgeIntegrityError("existing JUDGE_REPORT evidence conflicts")

    def find_report(self, request_id: str) -> JudgeReport | None:
        artifact_id = self.report_artifact_id(request_id)
        if not self.artifact_store.has_artifact(artifact_id):
            return None
        artifact = self.artifact_store.get_artifact(artifact_id)
        if artifact.kind is not ArtifactKind.CONTROL_PLANE_EVIDENCE or artifact.source != "OrnithJudge":
            raise JudgeIntegrityError("judge report artifact has unexpected identity")
        try:
            payload = json.loads(artifact.content)
            if not isinstance(payload, dict):
                raise ValueError("report payload must be object")
            report = JudgeReport.from_dict(payload)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise JudgeIntegrityError(f"invalid persisted judge report: {exc}") from exc
        if report.report_id != request_id:
            raise JudgeIntegrityError("persisted judge report identity mismatch")
        return report

    def validate_report(self, report: JudgeReport, bundle: JudgeEvidenceBundle) -> None:
        if (
            report.report_id != bundle.request_id
            or report.request_fingerprint != bundle.request_fingerprint
            or report.run_id != bundle.run_id
            or report.todo_id != bundle.todo_id
            or report.verification_report_id != bundle.verification_report.report_id
            or report.critic_report_id != bundle.critic_report.report_id
            or report.candidate_diff_sha256 != bundle.candidate_diff_sha256
            or report.analysis.deterministic_outcome is not bundle.deterministic_outcome
        ):
            raise JudgeIntegrityError("persisted judge report does not match current evidence bundle")
        if not set(report.analysis.evidence).issubset(bundle.allowed_evidence_ids):
            raise JudgeIntegrityError("persisted judge report cites evidence outside current bundle")
        if not self.artifact_store.has_artifact(report.raw_turn_artifact_id):
            raise JudgeIntegrityError("judge report references missing raw turn artifact")
        if bundle.acceptance_obligations is not None:
            from .protocol import parse_judge_response
            turn = self.find_turn(bundle.request_id, report.model_attempts - 1)
            if turn is None or parse_judge_response(turn.content, bundle) != report.analysis:
                raise JudgeIntegrityError('persisted semantic Judge result mismatches authoritative raw turn')

    def _existing_evidence(self, run_id: str, evidence_id: str) -> dict[str, object] | None:
        found = None
        for event in self.run_store.list_events(run_id):
            if event.event_type.value != "EVIDENCE_RECORDED":
                continue
            payload = event.to_dict()["payload"]
            if payload.get("evidence_id") != evidence_id:
                continue
            if found is not None and found != payload:
                raise JudgeIntegrityError("conflicting duplicate judge Evidence")
            artifact_id = parse_artifact_uri(payload.get("reference"))
            if artifact_id is None or not self.artifact_store.has_artifact(artifact_id):
                raise JudgeIntegrityError("judge Evidence references missing artifact")
            found = payload
        return found

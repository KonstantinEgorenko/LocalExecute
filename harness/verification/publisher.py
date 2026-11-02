'Durable verification verification report, Gate, and Evidence publication.'
from __future__ import annotations

import json
from datetime import datetime, timezone

from ..context_manager.integration import artifact_uri, parse_artifact_uri
from ..context_manager.models import Artifact, ArtifactKind, ArtifactOrigin
from ..context_manager.store import ArtifactStore
from ..control_plane.models import Evidence, EvidenceStatus, Gate, GateStatus
from ..control_plane.store import RunStore
from .errors import VerificationIntegrityError
from .models import GateOutcome, VerificationOutcome, VerificationPlan, VerificationReport, canonical_json


class VerificationPublisher:
    def __init__(self, *, run_store: RunStore, artifact_store: ArtifactStore) -> None:
        self.run_store = run_store
        self.artifact_store = artifact_store

    @staticmethod
    def artifact_id(report_id: str) -> str:
        return f"{report_id}:report"

    @staticmethod
    def evidence_id(report_id: str) -> str:
        return f"{report_id}:evidence"

    def find_existing(self, *, report_id: str) -> VerificationReport | None:
        artifact_id = self.artifact_id(report_id)
        if not self.artifact_store.has_artifact(artifact_id):
            return None
        artifact = self.artifact_store.get_artifact(artifact_id)
        if artifact.kind is not ArtifactKind.TEST_RESULT or artifact.source != "VerificationRunner":
            raise VerificationIntegrityError("existing verification artifact has unexpected identity")
        try:
            payload = json.loads(artifact.content)
            if not isinstance(payload, dict):
                raise ValueError("report payload must be object")
            report = VerificationReport.from_dict(payload)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise VerificationIntegrityError(f"invalid persisted verification report: {exc}") from exc
        if report.report_id != report_id:
            raise VerificationIntegrityError("persisted verification report_id mismatch")
        return report

    def publish(self, report: VerificationReport) -> None:
        content = canonical_json(report.to_dict())
        artifact_id = self.artifact_id(report.report_id)
        if self.artifact_store.has_artifact(artifact_id):
            existing = self.artifact_store.get_artifact(artifact_id)
            if existing.content != content:
                raise VerificationIntegrityError("existing report artifact content conflicts with current report")
        else:
            artifact = Artifact.from_text(
                artifact_id=artifact_id,
                kind=ArtifactKind.TEST_RESULT,
                origin=ArtifactOrigin.CONTROL_PLANE,
                content=content,
                media_type="application/json",
                source="VerificationRunner",
                created_at=datetime.now(timezone.utc).isoformat(),
                scope_tags=report.dirty_paths,
                metadata={
                    "plan_fingerprint": report.plan_fingerprint,
                    "candidate_diff_sha256": report.candidate_diff_sha256,
                    "outcome": report.outcome.value,
                },
            )
            self.artifact_store.create_artifact(artifact)

        existing_gates = self._existing_gates(report.run_id, report.todo_id)
        for result in report.results:
            status = GateStatus.SATISFIED if result.outcome is GateOutcome.PASSED else GateStatus.FAILED
            desired = Gate(result.gate_id, result.gate_type.value, result.required, status)
            previous = existing_gates.get(result.gate_id)
            if previous is None:
                self.run_store.record_gate(report.run_id, report.todo_id, desired)
            elif previous != desired.to_dict():
                if not self._can_revise_gate(report, result.gate_id, previous):
                    raise VerificationIntegrityError(f"existing Control Plane gate conflicts: {result.gate_id}")
                self.run_store.record_gate(report.run_id, report.todo_id, desired)

        evidence_id = self.evidence_id(report.report_id)
        desired_evidence = Evidence(
            evidence_id=evidence_id,
            todo_id=report.todo_id,
            evidence_type="VERIFICATION_REPORT",
            source="VerificationRunner",
            status=EvidenceStatus.PASSED if report.outcome is VerificationOutcome.PASSED else EvidenceStatus.FAILED,
            payload={
                "report_id": report.report_id,
                "plan_id": report.plan_id,
                "plan_fingerprint": report.plan_fingerprint,
                "candidate_diff_sha256": report.candidate_diff_sha256,
                "candidate_content_sha256": report.candidate_content_sha256,
                "outcome": report.outcome.value,
                "required_gate_count": sum(1 for item in report.results if item.required),
                "gate_count": len(report.results),
            },
            reference=artifact_uri(artifact_id),
        )
        previous_evidence = self._existing_evidence(report.run_id, evidence_id)
        if previous_evidence is None:
            self.run_store.record_evidence(report.run_id, desired_evidence)
        elif previous_evidence != desired_evidence.to_dict():
            raise VerificationIntegrityError("existing verification Evidence conflicts with report")

    def validate_report_identity(self, report: VerificationReport, plan: VerificationPlan, *, head: str, diff_sha256: str) -> None:
        if (
            report.plan_id != plan.plan_id
            or report.plan_fingerprint != plan.fingerprint
            or report.run_id != plan.run_id
            or report.todo_id != plan.todo_id
            or report.lease_id != plan.lease_id
            or report.candidate_head != head
            or report.candidate_diff_sha256 != diff_sha256
        ):
            raise VerificationIntegrityError("persisted verification report does not match current plan/candidate")

    def _existing_gates(self, run_id: str, todo_id: str) -> dict[str, dict[str, object]]:
        result: dict[str, dict[str, object]] = {}
        for event in self.run_store.list_events(run_id):
            if event.event_type.value != "GATE_RECORDED":
                continue
            payload = event.to_dict()["payload"]
            if payload.get("todo_id") != todo_id or not isinstance(payload.get("gate"), dict):
                continue
            gate = payload["gate"]
            gate_id = gate.get("gate_id")
            if not isinstance(gate_id, str):
                continue
            result[gate_id] = gate
        return result

    def _can_revise_gate(self, report: VerificationReport, gate_id: str, previous_gate: dict[str, object]) -> bool:
        events = self.run_store.list_events(report.run_id)
        for index in range(len(events) - 1, -1, -1):
            event = events[index]
            if event.event_type.value != "EVIDENCE_RECORDED":
                continue
            payload = event.to_dict()["payload"]
            if (
                payload.get("todo_id") != report.todo_id
                or payload.get("evidence_type") != "VERIFICATION_REPORT"
                or payload.get("source") != "VerificationRunner"
            ):
                continue
            artifact_id = parse_artifact_uri(payload.get("reference"))
            if artifact_id is None or not self.artifact_store.has_artifact(artifact_id):
                raise VerificationIntegrityError("verification Evidence references missing artifact")
            evidence_payload = payload.get("payload")
            report_id = evidence_payload.get("report_id") if isinstance(evidence_payload, dict) else None
            if not isinstance(report_id, str):
                raise VerificationIntegrityError("verification Evidence is missing report_id")
            previous_report = self.find_existing(report_id=report_id)
            if previous_report is None:
                raise VerificationIntegrityError("verification Evidence report artifact is missing")
            prior_result = next((item for item in previous_report.results if item.gate_id == gate_id), None)
            if prior_result is None:
                continue
            prior_status = GateStatus.SATISFIED if prior_result.outcome is GateOutcome.PASSED else GateStatus.FAILED
            expected_gate = Gate(prior_result.gate_id, prior_result.gate_type.value, prior_result.required, prior_status).to_dict()
            if expected_gate != previous_gate:
                continue
            if (previous_report.report_id == report.report_id or
                    previous_report.candidate_diff_sha256 == report.candidate_diff_sha256
                    and previous_report.candidate_content_sha256 == report.candidate_content_sha256):
                return False
            for later in events[index + 1:]:
                if later.event_type.value != "GATE_RECORDED":
                    continue
                gate_payload = later.to_dict()["payload"]
                gate = gate_payload.get("gate")
                if gate_payload.get("todo_id") == report.todo_id and isinstance(gate, dict) and gate.get("gate_id") == gate_id:
                    return False
            return True
        return False
    def _existing_evidence(self, run_id: str, evidence_id: str) -> dict[str, object] | None:
        found: dict[str, object] | None = None
        for event in self.run_store.list_events(run_id):
            if event.event_type.value != "EVIDENCE_RECORDED":
                continue
            payload = event.to_dict()["payload"]
            if payload.get("evidence_id") != evidence_id:
                continue
            if found is not None and found != payload:
                raise VerificationIntegrityError("conflicting duplicate verification Evidence")
            artifact_id = parse_artifact_uri(payload.get("reference"))
            if artifact_id is None or not self.artifact_store.has_artifact(artifact_id):
                raise VerificationIntegrityError("verification Evidence references missing artifact")
            found = payload
        return found

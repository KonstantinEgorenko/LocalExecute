"""Bounded, durable repair activation for a reused failed candidate."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping

from ..context_manager import ArtifactKind, ArtifactOrigin
from ..context_manager.integration import ControlPlaneArtifactBridge, artifact_uri, parse_artifact_uri
from ..control_plane import Evidence, EvidenceStatus


def _bounded(value: object, limit: int) -> str:
    text = value if isinstance(value, str) else ""
    return text.encode("utf-8")[:limit].decode("utf-8", "ignore")


def _texts(value: object, count: int, width: int) -> tuple[str, ...]:
    values = value if isinstance(value, (tuple, list)) else ()
    return tuple(sorted({_bounded(item, width) for item in values if isinstance(item, str) and item.strip()})[:count])


@dataclass(frozen=True, slots=True)
class RepairActivationV1:
    schema_version: int
    mission_id: str | None
    goal_id: str
    todo_id: str
    run_id: str
    candidate_lease_id: str
    failure_kind: str
    verification_report_id: str | None
    critic_report_id: str | None
    candidate_checkpoint_fingerprint: str
    candidate_diff_sha256: str
    recovery_cycle: int
    original_objective: str
    failed_gates: tuple[str, ...]
    failing_output: str
    corrected_invariants: tuple[str, ...]
    implicated_files: tuple[str, ...]
    candidate_changed_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != 1 or self.failure_kind != "VERIFICATION_FAILED":
            raise ValueError("repair activation requires VERIFICATION_FAILED schema v1")
        if not all(isinstance(value, str) and value for value in (self.goal_id, self.todo_id, self.run_id, self.candidate_lease_id, self.candidate_checkpoint_fingerprint, self.candidate_diff_sha256, self.original_objective)):
            raise ValueError("repair activation identity is incomplete")
        if type(self.recovery_cycle) is not int or self.recovery_cycle <= 0:
            raise ValueError("repair activation recovery_cycle must be positive")

    def to_dict(self) -> dict[str, Any]:
        return {name: (list(value) if isinstance(value, tuple) else value) for name, value in {
            "schema_version": self.schema_version, "mission_id": self.mission_id, "goal_id": self.goal_id,
            "todo_id": self.todo_id, "run_id": self.run_id, "candidate_lease_id": self.candidate_lease_id,
            "failure_kind": self.failure_kind, "verification_report_id": self.verification_report_id,
            "critic_report_id": self.critic_report_id, "candidate_checkpoint_fingerprint": self.candidate_checkpoint_fingerprint,
            "candidate_diff_sha256": self.candidate_diff_sha256, "recovery_cycle": self.recovery_cycle,
            "original_objective": self.original_objective, "failed_gates": self.failed_gates,
            "failing_output": self.failing_output, "corrected_invariants": self.corrected_invariants,
            "implicated_files": self.implicated_files,
            "candidate_changed_paths": self.candidate_changed_paths,
        }.items()}

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()

    def directive(self) -> str:
        return "AUTHORITATIVE_REPAIR_ACTIVATION\n" + json.dumps({
            "repair_activation_id": self.fingerprint, "agent_execution_state": "REOPEN_FOR_REPAIR",
            "candidate_state": "REUSE_UNVERIFIED", "original_todo_objective": self.original_objective,
            "failure_kind": self.failure_kind, "verification_report_id": self.verification_report_id,
            "failed_gates": list(self.failed_gates), "failing_output": self.failing_output,
            "corrected_invariants": list(self.corrected_invariants), "implicated_files": list(self.implicated_files),
            "candidate_lease_id": self.candidate_lease_id, "candidate_delta_summary": {
                "checkpoint_fingerprint": self.candidate_checkpoint_fingerprint,
                "diff_sha256": self.candidate_diff_sha256,
                "changed_paths": list(self.candidate_changed_paths),
            },
            "candidate_state_instruction": "The existing candidate is UNVERIFIED.",
            "required_action": "Modify the existing candidate or return an evidence-backed typed reason no safe repair is possible. Completion summaries alone are not progress.",
        }, sort_keys=True, ensure_ascii=False)


def _failed_gates(verification: Any) -> tuple[str, ...]:
    raw = getattr(verification, "raw", {})
    raw = raw if isinstance(raw, Mapping) else {}
    values: list[str] = []
    results = raw.get("results")
    if isinstance(results, (tuple, list)):
        for result in results:
            if not isinstance(result, Mapping):
                continue
            outcome = result.get("outcome")
            gate_id = result.get("gate_id")
            if isinstance(gate_id, str) and isinstance(outcome, str) and outcome != "PASSED":
                values.append(f"{gate_id}:{outcome}")
    if not values:
        values.extend(item for item in getattr(verification, "evidence_ids", ()) if isinstance(item, str))
    return _texts(values, 8, 192)


def _failing_output(verification: Any) -> str:
    raw = getattr(verification, "raw", {})
    raw = raw if isinstance(raw, Mapping) else {}
    results = raw.get("results")
    if isinstance(results, (tuple, list)):
        for result in results:
            if not isinstance(result, Mapping) or result.get("required") is not True:
                continue
            if result.get("outcome") == "PASSED":
                continue
            detail = "\n".join(
                value for value in (result.get("stdout"), result.get("stderr"), result.get("detail"))
                if isinstance(value, str) and value.strip()
            )
            if detail:
                return _bounded(detail, 768)
    return _bounded(getattr(verification, "summary", ""), 768)

def build_repair_activation(*, todo, run_id: str, lease_id: str, checkpoint, verification, recovery: Any, recovery_cycle: int) -> RepairActivationV1:
    raw = getattr(recovery, "raw", {})
    raw = dict(raw) if isinstance(raw, Mapping) else {}
    diagnosis = raw.get("diagnosis", {})
    diagnosis = diagnosis if isinstance(diagnosis, Mapping) else {}
    failed_gates = _failed_gates(verification)
    return RepairActivationV1(
        1, None, todo.work_contract.goal_id, todo.todo_id, run_id, lease_id, "VERIFICATION_FAILED",
        getattr(verification, "report_id", None), raw.get("critic_report_id") if isinstance(raw.get("critic_report_id"), str) else None,
        checkpoint.fingerprint, checkpoint.diff_sha256, recovery_cycle, todo.work_contract.objective,
        failed_gates, _failing_output(verification),
        _texts(diagnosis.get("corrected_invariants"), 4, 192), _texts(diagnosis.get("implicated_files"), 8, 128),
        _texts(getattr(checkpoint, "changed_paths", ()), 16, 128),
    )

def persist_repair_activation(*, artifact_store, run_store, activation: RepairActivationV1) -> str:
    artifact_id = f"repair-activation:{activation.fingerprint[:32]}"
    content = json.dumps(activation.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    ControlPlaneArtifactBridge(run_store, artifact_store).record_text_evidence(
        activation.run_id, artifact_id=artifact_id, todo_id=activation.todo_id,
        evidence_id=f"{activation.run_id}:repair-activation:{activation.fingerprint[:16]}",
        evidence_type="REPAIR_ACTIVATION_CREATED", source="RepairActivationV1", content=content,
        kind=ArtifactKind.CONTROL_PLANE_EVIDENCE, origin=ArtifactOrigin.CONTROL_PLANE,
        media_type="application/json", status=EvidenceStatus.PASSED,
        evidence_payload={"repair_activation_id": activation.fingerprint, "failure_kind": activation.failure_kind,
                          "candidate_lease_id": activation.candidate_lease_id, "verification_report_id": activation.verification_report_id,
                          "candidate_diff_sha256": activation.candidate_diff_sha256}, created_at="1970-01-01T00:00:00+00:00",
    )
    source_payloads = (
        ("REPAIR_SOURCE_VERIFICATION_REPORT", {"verification_report_id": activation.verification_report_id}),
        ("REPAIR_SOURCE_CRITIC_REPORT", {"critic_report_id": activation.critic_report_id}),
        ("REPAIR_FAILURE_KIND", {"failure_kind": activation.failure_kind}),
        ("REPAIR_CANDIDATE_LEASE", {"candidate_lease_id": activation.candidate_lease_id}),
        ("REPAIR_CANDIDATE_DIFF_BEFORE", {"candidate_diff_sha256": activation.candidate_diff_sha256}),
    )
    for evidence_type, payload in source_payloads:
        run_store.record_evidence(activation.run_id, Evidence(
            evidence_id=f"{activation.run_id}:repair-source:{evidence_type}:{activation.fingerprint[:16]}",
            todo_id=activation.todo_id, evidence_type=evidence_type, source="RepairActivationV1",
            status=EvidenceStatus.PASSED, payload=payload,
        ))
    return artifact_id


def latest_repair_activation(run_store, artifact_store, *, run_id: str, todo_id: str, lease_id: str) -> RepairActivationV1 | None:
    for event in reversed(run_store.list_events(run_id)):
        payload = event.to_dict().get("payload", {})
        if not isinstance(payload, Mapping) or payload.get("todo_id") != todo_id or payload.get("evidence_type") != "REPAIR_ACTIVATION_CREATED":
            continue
        artifact_id = parse_artifact_uri(payload.get("reference"))
        if artifact_id is None:
            raise RuntimeError("repair activation artifact reference missing")
        raw = json.loads(artifact_store.get_artifact(artifact_id).content)
        activation = RepairActivationV1(**{**raw, "failed_gates": tuple(raw["failed_gates"]), "corrected_invariants": tuple(raw["corrected_invariants"]), "implicated_files": tuple(raw["implicated_files"]), "candidate_changed_paths": tuple(raw.get("candidate_changed_paths", ()))})
        if activation.run_id != run_id or activation.todo_id != todo_id or activation.candidate_lease_id != lease_id:
            raise RuntimeError("repair activation identity mismatch")
        return activation
    return None



def record_repair_directive_presented(*, run_store, run_id: str, todo_id: str,
                                      activation: RepairActivationV1, request_id: str) -> None:
    if activation.run_id != run_id or activation.todo_id != todo_id:
        raise ValueError("repair directive identity mismatch")
    payload = {
        "repair_activation_id": activation.fingerprint,
        "request_id": request_id,
        "failure_kind": activation.failure_kind,
        "verification_report_id": activation.verification_report_id,
        "failed_gates": list(activation.failed_gates),
        "corrected_invariants": list(activation.corrected_invariants),
        "candidate_lease_id": activation.candidate_lease_id,
    }
    run_store.record_evidence(run_id, Evidence(
        evidence_id=f"{run_id}:repair-directive:{activation.fingerprint[:16]}:{request_id}",
        todo_id=todo_id, evidence_type="REPAIR_DIRECTIVE_PRESENTED",
        source="RepairActivationV1", status=EvidenceStatus.PASSED, payload=payload,
    ))


def record_repair_execution_observation(*, run_store, run_id: str, todo_id: str,
                                        activation: RepairActivationV1, request_id: str,
                                        candidate_diff_after: str, tool_calls_delta: int) -> None:
    if activation.run_id != run_id or activation.todo_id != todo_id:
        raise ValueError("repair observation identity mismatch")
    if type(tool_calls_delta) is not int or tool_calls_delta < 0:
        raise ValueError("repair tool call delta must be non-negative integer")
    for evidence_type, payload in (
        ("REPAIR_CANDIDATE_DIFF_AFTER", {"candidate_diff_sha256": candidate_diff_after}),
        ("REPAIR_TOOL_CALL_DELTA", {"tool_calls_delta": tool_calls_delta}),
    ):
        run_store.record_evidence(run_id, Evidence(
            evidence_id=f"{run_id}:repair-observation:{evidence_type}:{activation.fingerprint[:16]}:{request_id}",
            todo_id=todo_id, evidence_type=evidence_type, source="RepairActivationV1",
            status=EvidenceStatus.PASSED, payload=payload,
        ))
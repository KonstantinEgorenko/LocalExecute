"""Durable production-only tool-failure activation windows."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..agent_loop.errors import AgentResumeError
from ..context_manager import ArtifactKind, ArtifactOrigin
from ..context_manager.integration import ControlPlaneArtifactBridge
from ..control_plane import EvidenceStatus


_REASON = "PHASE16_EXECUTION_ACTIVATION"
_KEYS = frozenset({
    "schema_version", "run_id", "todo_id", "candidate_lease_id",
    "activation_id", "request_id", "baseline_event_sequence", "reason",
})


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


@dataclass(frozen=True, slots=True)
class ToolFailureWindowBaselineV1:
    schema_version: int
    run_id: str
    todo_id: str
    candidate_lease_id: str
    activation_id: str
    request_id: str
    baseline_event_sequence: int
    reason: str

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("schema_version must be exactly integer 1")
        for name in ("run_id", "todo_id", "candidate_lease_id", "activation_id", "request_id"):
            _text(getattr(self, name), name)
        if type(self.baseline_event_sequence) is not int or self.baseline_event_sequence < -1:
            raise ValueError("baseline_event_sequence must be an integer >= -1")
        if self.reason != _REASON:
            raise ValueError("unsupported tool-failure window reason")

    @classmethod
    def from_dict(cls, payload: object) -> "ToolFailureWindowBaselineV1":
        if not isinstance(payload, Mapping) or set(payload) != _KEYS:
            raise ValueError("tool-failure window keys must match schema exactly")
        return cls(
            schema_version=payload["schema_version"],
            run_id=payload["run_id"],
            todo_id=payload["todo_id"],
            candidate_lease_id=payload["candidate_lease_id"],
            activation_id=payload["activation_id"],
            request_id=payload["request_id"],
            baseline_event_sequence=payload["baseline_event_sequence"],
            reason=payload["reason"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "todo_id": self.todo_id,
            "candidate_lease_id": self.candidate_lease_id,
            "activation_id": self.activation_id,
            "request_id": self.request_id,
            "baseline_event_sequence": self.baseline_event_sequence,
            "reason": self.reason,
        }

    @property
    def semantic_identity(self) -> tuple[str, str, str, str, str]:
        return (self.run_id, self.todo_id, self.candidate_lease_id, self.request_id, self.reason)

    def canonical_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


def _activation_id(identity: tuple[str, str, str, str, str]) -> str:
    return hashlib.sha256("\0".join(identity).encode("utf-8")).hexdigest()[:32]


def load_tool_failure_windows(run_store, *, run_id: str, todo_id: str) -> list[ToolFailureWindowBaselineV1]:
    """Strictly load every durable failure window for a Run/Todo."""
    values: list[ToolFailureWindowBaselineV1] = []
    semantic_content: dict[tuple[str, str, str, str, str], str] = {}
    activation_content: dict[str, tuple[tuple[str, str, str, str, str], str]] = {}
    for event in run_store.list_events(run_id):
        if event.event_type.value != "EVIDENCE_RECORDED":
            continue
        envelope = event.to_dict()["payload"]
        if envelope.get("todo_id") != todo_id or envelope.get("evidence_type") != "TOOL_FAILURE_WINDOW_STARTED":
            continue
        try:
            baseline = ToolFailureWindowBaselineV1.from_dict(envelope.get("payload"))
        except (TypeError, ValueError) as exc:
            raise AgentResumeError("malformed durable tool-failure window") from exc
        if baseline.run_id != run_id or baseline.todo_id != todo_id:
            raise AgentResumeError("tool-failure window identity mismatch")
        if baseline.activation_id != _activation_id(baseline.semantic_identity):
            raise AgentResumeError("non-deterministic durable tool-failure activation identity")
        if baseline.baseline_event_sequence >= event.sequence:
            raise AgentResumeError("tool-failure window baseline sequence must precede its evidence event")
        content = baseline.canonical_json()
        previous = semantic_content.get(baseline.semantic_identity)
        if previous is not None and previous != content:
            raise AgentResumeError("conflicting durable tool-failure window duplicate")
        semantic_content[baseline.semantic_identity] = content
        activation_previous = activation_content.get(baseline.activation_id)
        if activation_previous is not None and activation_previous != (baseline.semantic_identity, content):
            raise AgentResumeError("conflicting durable tool-failure activation identity")
        activation_content[baseline.activation_id] = (baseline.semantic_identity, content)
        values.append(baseline)
    return values


def persist_workflow_execution_window(
    *, run_store, artifact_store, run_id: str, todo_id: str, lease_id: str, request_id: str,
) -> ToolFailureWindowBaselineV1:
    'Persist one deterministic workflow execution activation, idempotently.'
    requested = ToolFailureWindowBaselineV1(
        1, run_id, todo_id, lease_id,
        _activation_id((run_id, todo_id, lease_id, request_id, _REASON)),
        request_id, -1, _REASON,
    )
    existing = load_tool_failure_windows(run_store, run_id=run_id, todo_id=todo_id)
    if any(value.candidate_lease_id != lease_id for value in existing):
        raise AgentResumeError("tool-failure window candidate lease mismatch")
    matching = [value for value in existing if value.semantic_identity == requested.semantic_identity]
    if matching:
        return matching[0]

    events = run_store.list_events(run_id)
    baseline = ToolFailureWindowBaselineV1(
        1, run_id, todo_id, lease_id, requested.activation_id, request_id,
        events[-1].sequence if events else -1, _REASON,
    )
    bridge = ControlPlaneArtifactBridge(run_store, artifact_store)
    bridge.record_text_evidence(
        run_id,
        artifact_id=f"{run_id}:tool-failure-window:{baseline.activation_id}",
        todo_id=todo_id,
        evidence_id=f"{run_id}:tool-failure-window:{baseline.activation_id}",
        evidence_type="TOOL_FAILURE_WINDOW_STARTED",
        source="ToolFailureWindowBaselineV1",
        content=baseline.canonical_json(),
        kind=ArtifactKind.CONTROL_PLANE_EVIDENCE,
        origin=ArtifactOrigin.CONTROL_PLANE,
        media_type="application/json",
        status=EvidenceStatus.PASSED,
        evidence_payload=baseline.to_dict(),
        created_at="1970-01-01T00:00:00+00:00",
    )
    return baseline
# Verified compatibility aliases for historical Python interfaces.
persist_phase16_execution_window = persist_workflow_execution_window

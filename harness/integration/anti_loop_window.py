"""Durable production-only anti-loop enforcement windows.

Tool history remains a Run-level audit record. A window only narrows the
historical fingerprints used to initialize the frozen loop's repeated-tool
counter after a validated TOOL_LOOP candidate reuse.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ..agent_loop import AgentLoopResult, AgentOutcome, CodingAgentLoop
from ..agent_loop.errors import AgentResumeError
from ..context_manager import ArtifactKind, ArtifactOrigin
from ..context_manager.integration import ControlPlaneArtifactBridge
from ..control_plane import EvidenceStatus
from .tool_failure_window import load_tool_failure_windows


_REASON = "SAFE_CANDIDATE_REUSE_AFTER_TOOL_LOOP"
_BASELINE_KEYS = frozenset({
    "schema_version", "run_id", "todo_id", "candidate_lease_id",
    "activation_id", "parent_activation_id", "baseline_event_sequence",
    "reason", "recovery_cycle", "candidate_checkpoint_fingerprint",
})


def _nonempty_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


@dataclass(frozen=True, slots=True)
class AntiLoopWindowBaselineV1:
    schema_version: int
    run_id: str
    todo_id: str
    candidate_lease_id: str
    activation_id: str
    parent_activation_id: str | None
    baseline_event_sequence: int
    reason: str
    recovery_cycle: int | None
    candidate_checkpoint_fingerprint: str | None

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("schema_version must be exactly integer 1")
        for name in ("run_id", "todo_id", "candidate_lease_id", "activation_id"):
            _nonempty_text(getattr(self, name), name)
        if self.parent_activation_id is not None:
            _nonempty_text(self.parent_activation_id, "parent_activation_id")
        if type(self.baseline_event_sequence) is not int or self.baseline_event_sequence < -1:
            raise ValueError("baseline_event_sequence must be an integer >= -1")
        if self.reason != _REASON:
            raise ValueError("unsupported anti-loop window reason")
        if type(self.recovery_cycle) is not int or self.recovery_cycle <= 0:
            raise ValueError("recovery_cycle is required and must be a positive integer")
        _nonempty_text(self.candidate_checkpoint_fingerprint, "candidate_checkpoint_fingerprint")

    @classmethod
    def from_dict(cls, payload: object) -> "AntiLoopWindowBaselineV1":
        if not isinstance(payload, Mapping) or set(payload) != _BASELINE_KEYS:
            raise ValueError("anti-loop window keys must match schema exactly")
        return cls(
            schema_version=payload["schema_version"],
            run_id=payload["run_id"],
            todo_id=payload["todo_id"],
            candidate_lease_id=payload["candidate_lease_id"],
            activation_id=payload["activation_id"],
            parent_activation_id=payload["parent_activation_id"],
            baseline_event_sequence=payload["baseline_event_sequence"],
            reason=payload["reason"],
            recovery_cycle=payload["recovery_cycle"],
            candidate_checkpoint_fingerprint=payload["candidate_checkpoint_fingerprint"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "todo_id": self.todo_id,
            "candidate_lease_id": self.candidate_lease_id,
            "activation_id": self.activation_id,
            "parent_activation_id": self.parent_activation_id,
            "baseline_event_sequence": self.baseline_event_sequence,
            "reason": self.reason,
            "recovery_cycle": self.recovery_cycle,
            "candidate_checkpoint_fingerprint": self.candidate_checkpoint_fingerprint,
        }

    @property
    def semantic_identity(self) -> tuple[str, str, str, int, str, str]:
        return (
            self.run_id, self.todo_id, self.candidate_lease_id,
            self.recovery_cycle, self.candidate_checkpoint_fingerprint, self.reason,
        )

    def canonical_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


def _window_events(run_store, *, run_id: str, todo_id: str) -> list[AntiLoopWindowBaselineV1]:
    """Load and validate all durable windows relevant to one Run/Todo.

    This is deliberately shared by persistence and resume reads: a damaged
    append-only history must never grant either a duplicate reset or a resumed
    activation with ambiguous enforcement state.
    """
    values: list[AntiLoopWindowBaselineV1] = []
    semantic_content: dict[tuple[str, str, str, int, str, str], str] = {}
    activation_content: dict[str, tuple[tuple[str, str, str, int, str, str], str]] = {}
    for event in run_store.list_events(run_id):
        if event.event_type.value != "EVIDENCE_RECORDED":
            continue
        envelope = event.to_dict()["payload"]
        if envelope.get("todo_id") != todo_id or envelope.get("evidence_type") != "ANTI_LOOP_WINDOW_STARTED":
            continue
        try:
            baseline = AntiLoopWindowBaselineV1.from_dict(envelope.get("payload"))
        except (TypeError, ValueError) as exc:
            raise AgentResumeError("malformed durable anti-loop window") from exc
        if baseline.run_id != run_id or baseline.todo_id != todo_id:
            raise AgentResumeError("anti-loop window identity mismatch")
        content = baseline.canonical_json()
        previous = semantic_content.get(baseline.semantic_identity)
        if previous is not None and previous != content:
            raise AgentResumeError("conflicting durable anti-loop window duplicate")
        semantic_content[baseline.semantic_identity] = content
        activation_previous = activation_content.get(baseline.activation_id)
        if activation_previous is not None and activation_previous != (baseline.semantic_identity, content):
            raise AgentResumeError("conflicting durable anti-loop activation identity")
        activation_content[baseline.activation_id] = (baseline.semantic_identity, content)
        values.append(baseline)
    return values


def _activation_id(semantic_identity: tuple[str, str, str, int, str, str]) -> str:
    return hashlib.sha256("\0".join(map(str, semantic_identity)).encode("utf-8")).hexdigest()[:32]


def persist_tool_loop_reuse_window(
    *, run_store, artifact_store, run_id: str, todo_id: str, lease_id: str,
    recovery_cycle: int, checkpoint_fingerprint: str,
) -> AntiLoopWindowBaselineV1:
    """Persist the one authorized anti-loop reset, idempotently."""
    requested = AntiLoopWindowBaselineV1(
        1, run_id, todo_id, lease_id,
        _activation_id((run_id, todo_id, lease_id, recovery_cycle, checkpoint_fingerprint, _REASON)),
        None, -1, _REASON, recovery_cycle, checkpoint_fingerprint,
    )
    existing = _window_events(run_store, run_id=run_id, todo_id=todo_id)
    matching = [value for value in existing if value.semantic_identity == requested.semantic_identity]
    if matching:
        first = matching[0]
        if any(value.to_dict() != first.to_dict() for value in matching[1:]):
            raise AgentResumeError("conflicting durable anti-loop window duplicate")
        return first

    lineage = [value for value in existing if value.candidate_lease_id == lease_id]
    parent = lineage[-1].activation_id if lineage else None
    events = run_store.list_events(run_id)
    baseline = AntiLoopWindowBaselineV1(
        1, run_id, todo_id, lease_id, requested.activation_id, parent,
        events[-1].sequence if events else -1, _REASON, recovery_cycle, checkpoint_fingerprint,
    )
    bridge = ControlPlaneArtifactBridge(run_store, artifact_store)
    bridge.record_text_evidence(
        run_id, artifact_id=f"{run_id}:anti-loop-window:{baseline.activation_id}",
        todo_id=todo_id, evidence_id=f"{run_id}:anti-loop-window:{baseline.activation_id}",
        evidence_type="ANTI_LOOP_WINDOW_STARTED", source="AntiLoopWindowBaselineV1",
        content=json.dumps(baseline.to_dict(), sort_keys=True, separators=(",", ":")),
        kind=ArtifactKind.CONTROL_PLANE_EVIDENCE, origin=ArtifactOrigin.CONTROL_PLANE,
        media_type="application/json", status=EvidenceStatus.PASSED,
        evidence_payload=baseline.to_dict(), created_at="1970-01-01T00:00:00+00:00",
    )
    return baseline


def _extract_canonical_tool_fingerprints(
    run_store,
    *,
    run_id: str,
    todo_id: str,
    baseline_event_sequence: int = -1,
) -> tuple[str, ...]:
    values: list[str] = []
    for event in run_store.list_events(run_id):
        if event.sequence <= baseline_event_sequence or event.event_type.value != "EVIDENCE_RECORDED":
            continue
        payload = event.to_dict()["payload"]
        if payload.get("todo_id") == todo_id and payload.get("evidence_type") == "TOOL_RESULT":
            detail = payload.get("payload", {})
            if isinstance(detail, dict) and isinstance(detail.get("fingerprint"), str):
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


class ProductionWindowedCodingAgentLoop(CodingAgentLoop):
    """Frozen loop with a validated per-activation fingerprint history view."""

    def __init__(self, *, candidate_lease_provider: Callable[[str], str] | None = None,
                 tool_failure_request_provider: Callable[[str], str] | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self._candidate_lease_provider = candidate_lease_provider
        self._tool_failure_request_provider = tool_failure_request_provider

    def _current_candidate_lease_id(self, todo_id: str) -> str:
        if self._candidate_lease_provider is None:
            raise AgentResumeError("anti-loop window requires a current candidate lease provider")
        try:
            return _nonempty_text(self._candidate_lease_provider(todo_id), "current candidate lease id")
        except AgentResumeError:
            raise
        except Exception as exc:
            raise AgentResumeError("unable to resolve current candidate lease") from exc

    def _current_tool_failure_request_id(self, todo_id: str) -> str:
        if self._tool_failure_request_provider is None:
            raise AgentResumeError("tool-failure window requires a current execution request provider")
        try:
            return _nonempty_text(self._tool_failure_request_provider(todo_id), "current execution request id")
        except AgentResumeError:
            raise
        except Exception as exc:
            raise AgentResumeError("unable to resolve current execution request") from exc

    def _current_execution_window(self, run_id: str, todo_id: str):
        baselines = load_tool_failure_windows(self.run_store, run_id=run_id, todo_id=todo_id)
        if not baselines:
            return None
        current_lease_id = self._current_candidate_lease_id(todo_id)
        if any(value.candidate_lease_id != current_lease_id for value in baselines):
            raise AgentResumeError("tool-failure window candidate lease mismatch")
        request_id = self._current_tool_failure_request_id(todo_id)
        matching = [value for value in baselines if value.request_id == request_id]
        if not matching:
            raise AgentResumeError("missing durable tool-failure window for current execution request")
        return matching[0]

    def _tool_failure_count(self, run_id: str, todo_id: str) -> int:
        baseline = self._current_execution_window(run_id, todo_id)
        if baseline is None:
            return super()._tool_failure_count(run_id, todo_id)
        return sum(
            1
            for event in self.run_store.list_events(run_id)
            if event.sequence > baseline.baseline_event_sequence
            and event.event_type.value == "EVIDENCE_RECORDED"
            and (payload := event.to_dict()["payload"]).get("todo_id") == todo_id
            and payload.get("evidence_type") == "TOOL_RESULT"
            and payload.get("status") == EvidenceStatus.FAILED.value
        )

    def _existing_final(self, run_id: str, todo_id: str, bundle_id: str) -> AgentLoopResult | None:
        baseline = self._current_execution_window(run_id, todo_id)
        if baseline is None:
            return super()._existing_final(run_id, todo_id, bundle_id)
        for event in self.run_store.list_events(run_id):
            if event.sequence <= baseline.baseline_event_sequence or event.event_type.value != "EVIDENCE_RECORDED":
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

    def _tool_fingerprints(self, run_id: str, todo_id: str) -> tuple[str, ...]:
        execution_window = self._current_execution_window(run_id, todo_id)
        if execution_window is not None:
            anti_loop_baselines = _window_events(self.run_store, run_id=run_id, todo_id=todo_id)
            if anti_loop_baselines:
                current_lease_id = self._current_candidate_lease_id(todo_id)
                if any(value.candidate_lease_id != current_lease_id for value in anti_loop_baselines):
                    raise AgentResumeError("anti-loop window candidate lease mismatch")
            return _extract_canonical_tool_fingerprints(
                self.run_store,
                run_id=run_id,
                todo_id=todo_id,
                baseline_event_sequence=execution_window.baseline_event_sequence,
            )

        anti_loop_baselines = _window_events(self.run_store, run_id=run_id, todo_id=todo_id)
        if not anti_loop_baselines:
            return super()._tool_fingerprints(run_id, todo_id)

        current_lease_id = self._current_candidate_lease_id(todo_id)
        if any(value.candidate_lease_id != current_lease_id for value in anti_loop_baselines):
            raise AgentResumeError("anti-loop window candidate lease mismatch")
        baseline = anti_loop_baselines[-1]
        return _extract_canonical_tool_fingerprints(
            self.run_store,
            run_id=run_id,
            todo_id=todo_id,
            baseline_event_sequence=baseline.baseline_event_sequence,
        )

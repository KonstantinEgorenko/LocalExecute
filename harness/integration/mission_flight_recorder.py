"""Durable, mission-local, observational execution flight recorder."""
from __future__ import annotations
from harness.compatibility.configuration import legacy_keywords

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from .model_invocation_lineage import ModelInvocationManifestV1
from .planning_trace import _plain, _redact, _secret_values


_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _mission_path(root: Path, mission_id: str) -> Path:
    safe = _SAFE.sub("-", mission_id).strip(".-") or "mission"
    return root / "traces" / safe / "mission_trace.jsonl"


def _utc() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class MissionTraceEventV1:
    schema_version: int
    sequence: int
    occurred_at: str
    elapsed_ms: int
    event_type: str
    mission_id: str
    goal_id: str
    todo_id: str | None
    run_id: str | None
    attempt_index: int | None
    slice_index: int | None
    actor: str | None
    model_call_id: str | None
    tool_call_id: str | None
    duration_ms: int | None
    payload: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


class MissionFlightRecorder:
    """Append+flush recorder. It is never read by production decision paths."""

    schema_version = 1

    def __init__(self, state_root: Path, *, mission_id: str, goal_id: str) -> None:
        self.state_root = Path(state_root)
        self.mission_id, self.goal_id = mission_id, goal_id
        self.path = _mission_path(self.state_root, mission_id)
        self._started = time.monotonic()
        self._sequence = self._recover_sequence()
        self._secret_values: tuple[str, ...] = ()

    def _recover_sequence(self) -> int:
        if not self.path.exists():
            return 0
        sequence = 0
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                sequence = max(sequence, int(json.loads(line)["sequence"]))
        return sequence

    def register_request_secrets(self, value: Any) -> None:
        self._secret_values = tuple(sorted(set(self._secret_values) | set(_secret_values(_plain(value))), key=lambda item: (-len(item), item)))

    def record(self, event_type: str, *, payload: Mapping[str, Any] | None = None, todo_id: str | None = None,
               run_id: str | None = None, attempt_index: int | None = None, slice_index: int | None = None,
               actor: str | None = None, model_call_id: str | None = None, tool_call_id: str | None = None,
               duration_ms: int | None = None) -> MissionTraceEventV1:
        if not isinstance(event_type, str) or not event_type:
            raise ValueError("event_type is required")
        self._sequence += 1
        clean = _redact(_plain({} if payload is None else payload), self._secret_values)
        event = MissionTraceEventV1(self.schema_version, self._sequence, _utc(), max(0, int((time.monotonic() - self._started) * 1000)), event_type,
                                    self.mission_id, self.goal_id, todo_id, run_id, attempt_index, slice_index,
                                    actor, model_call_id, tool_call_id, duration_ms, clean)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        return event

    def record_model_invocation(
        self,
        manifest: ModelInvocationManifestV1,
    ) -> MissionTraceEventV1:
        """Project one typed invocation into the existing mission trace ledger."""
        if not isinstance(manifest, ModelInvocationManifestV1):
            raise TypeError("manifest must be ModelInvocationManifestV1")
        if manifest.mission_id != self.mission_id or manifest.goal_id != self.goal_id:
            raise ValueError("model invocation mission/goal does not match recorder")
        return self.record(
            "MODEL_INVOCATION_MANIFEST",
            todo_id=manifest.todo_id,
            run_id=manifest.run_id,
            attempt_index=manifest.attempt_index,
            slice_index=manifest.slice_index,
            actor=manifest.logical_model_id,
            model_call_id=manifest.invocation_id,
            duration_ms=manifest.duration_ms,
            payload=manifest.to_dict(),
        )

    def seal(self, *, status: str, terminal_reason: str | None = None) -> Path:
        self.record("TRACE_SEALED", payload={"status": status, "terminal_reason": terminal_reason})
        events = self.events()
        calls = [event for event in events if event["event_type"] == "MODEL_CALL_FINISHED"]
        tools = [event for event in events if event["event_type"] == "TOOL_CALL_FINISHED"]
        summary = {"mission_id": self.mission_id, "goal_id": self.goal_id, "started_at": events[0]["occurred_at"] if events else None,
                   "ended_at": events[-1]["occurred_at"] if events else None, "duration_ms": events[-1]["elapsed_ms"] if events else 0,
                   "status": status, "terminal_reason": terminal_reason, "trace_file": str(self.path), "trace_event_count": len(events),
                   "model_call_count": len(calls), "tool_call_count": len(tools),
                   "first_error_event": next((event for event in events if "FAILED" in event["event_type"] or "ERROR" in event["event_type"]), None),
                   "first_blocking_event": next((event for event in events if "BLOCK" in event["event_type"]), None),
                   "last_successful_event": next((event for event in reversed(events) if "FINISHED" in event["event_type"] or "COMPLETED" in event["event_type"]), None)}
        path = self.path.with_name("mission_summary.json")
        path.write_text(json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":")), encoding="utf-8")
        return path

    def events(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]

    @staticmethod
    def fingerprint(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()


class FlightRecorderRuntime:
    """Small protocol wrapper for model/tool integrations that opt in explicitly."""
    def __init__(self, recorder: MissionFlightRecorder, inner: Any, *, logical_role: str, model_id: str | None = None) -> None:
        self.recorder, self.inner, self.logical_role, self.model_id = recorder, inner, logical_role, model_id

    def complete(self, system_prompt: str, user_prompt: str, response_schema: Any = None) -> Any:
        call_id = f"{self.recorder.mission_id}:model:{self.recorder._sequence + 1}"
        started = time.monotonic()
        self.recorder.record(
            "MODEL_CALL_STARTED",
            actor=self.logical_role,
            model_call_id=call_id,
            payload={
                "logical_role": self.logical_role,
                "model_id": self.model_id,
                "system_prompt_fingerprint": self.recorder.fingerprint(system_prompt),
                "system_prompt_bytes": len(system_prompt.encode("utf-8")),
                "user_prompt_fingerprint": self.recorder.fingerprint(user_prompt),
                "user_prompt_bytes": len(user_prompt.encode("utf-8")),
            },
        )
        try:
            response = self.inner.complete(system_prompt, user_prompt, response_schema)
        except Exception as exc:
            diagnostic = str(exc)
            self.recorder.record(
                "MODEL_CALL_FAILED",
                actor=self.logical_role,
                model_call_id=call_id,
                duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                payload={
                    "logical_role": self.logical_role,
                    "model_id": self.model_id,
                    "exception_type": type(exc).__name__,
                    "diagnostic_fingerprint": self.recorder.fingerprint(diagnostic),
                    "diagnostic_bytes": len(diagnostic.encode("utf-8")),
                },
            )
            raise
        content = str(getattr(response, "content", ""))
        self.recorder.record(
            "MODEL_CALL_FINISHED",
            actor=self.logical_role,
            model_call_id=call_id,
            duration_ms=max(0, int((time.monotonic() - started) * 1000)),
            payload={
                "logical_role": self.logical_role,
                "model_id": self.model_id,
                "response_fingerprint": self.recorder.fingerprint(content),
                "response_bytes": len(content.encode("utf-8")),
            },
        )
        return response


@legacy_keywords({'phase16': 'workflow_bridge'})
def record_dual_context_budget(recorder: MissionFlightRecorder, *, todo_id: str, run_id: str,
                               profile_id: str, workflow_bridge: Mapping[str, Any], agent: Mapping[str, Any],
                               attempt_index: int | None = None, slice_index: int | None = None,
                               candidate_lease_id: str | None = None) -> None:
    """Project both authoritative context layers; never changes their decision."""
    common = {"profile_id": profile_id, "candidate_lease_id": candidate_lease_id}
    recorder.record("PHASE16_CONTEXT_BUILD_STARTED", todo_id=todo_id, run_id=run_id, attempt_index=attempt_index, slice_index=slice_index, payload=common)
    recorder.record("PHASE16_CONTEXT_BUDGET_DECISION", todo_id=todo_id, run_id=run_id, attempt_index=attempt_index, slice_index=slice_index, payload={**common, **dict(workflow_bridge)})
    recorder.record("PHASE16_CONTEXT_BUILD_FINISHED", todo_id=todo_id, run_id=run_id, attempt_index=attempt_index, slice_index=slice_index, payload={**common, "status": workflow_bridge.get("status")})
    recorder.record("AGENT_CONTEXT_BUILD_STARTED", todo_id=todo_id, run_id=run_id, attempt_index=attempt_index, slice_index=slice_index, payload=common)
    for item in agent.get("required_items", ()):
        recorder.record("AGENT_CONTEXT_ITEM_CONSIDERED", todo_id=todo_id, run_id=run_id, attempt_index=attempt_index, slice_index=slice_index, payload={**common, "required": True, "item": item})
    recorder.record("AGENT_CONTEXT_BUDGET_DECISION", todo_id=todo_id, run_id=run_id, attempt_index=attempt_index, slice_index=slice_index, payload={**common, **dict(agent)})
    recorder.record("AGENT_CONTEXT_BUILD_FINISHED", todo_id=todo_id, run_id=run_id, attempt_index=attempt_index, slice_index=slice_index, payload={**common, "status": agent.get("status")})
    if workflow_bridge.get("status") == "FIT" and agent.get("status") == "OVER_BUDGET":
        recorder.record("CONTEXT_BUDGET_AUTHORITY_MISMATCH", todo_id=todo_id, run_id=run_id, attempt_index=attempt_index, slice_index=slice_index, payload={**common, "phase16_budget_fingerprint": MissionFlightRecorder.fingerprint(json.dumps(_plain(workflow_bridge), sort_keys=True)), "agent_budget_fingerprint": MissionFlightRecorder.fingerprint(json.dumps(_plain(agent), sort_keys=True)), "phase16_status": "FIT", "agent_status": "OVER_BUDGET"})
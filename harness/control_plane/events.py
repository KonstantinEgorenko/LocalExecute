"""Immutable append-only Control Plane event envelopes."""
from __future__ import annotations
import math
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping

class ControlPlaneEventType(StrEnum):
    GOAL_CREATED = "GOAL_CREATED"
    TODO_CREATED = "TODO_CREATED"
    RUN_CREATED = "RUN_CREATED"
    RUN_UPDATED = "RUN_UPDATED"
    ATTEMPT_RECORDED = "ATTEMPT_RECORDED"
    EVIDENCE_RECORDED = "EVIDENCE_RECORDED"
    GATE_RECORDED = "GATE_RECORDED"
    HANDOFF_RECORDED = "HANDOFF_RECORDED"
    ATTENTION_RECORDED = "ATTENTION_RECORDED"

class ControlPlaneEntityType(StrEnum):
    GOAL = "GOAL"
    TODO = "TODO"
    RUN = "RUN"
    ATTEMPT = "ATTEMPT"
    EVIDENCE = "EVIDENCE"
    GATE = "GATE"
    HANDOFF = "HANDOFF"
    ATTENTION = "ATTENTION"

def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value

def _freeze(value: Any, name: str) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value): raise ValueError(f"{name} must not contain non-finite floats")
        return value
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str): raise ValueError(f"{name} mapping keys must be strings")
            result[key] = _freeze(item, f"{name}.{key}")
        return MappingProxyType(result)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item, name) for item in value)
    raise ValueError(f"{name} must be JSON-safe")

def _plain(value: Any) -> Any:
    if isinstance(value, Mapping): return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, tuple): return [_plain(item) for item in value]
    return value

@dataclass(frozen=True, slots=True)
class ControlPlaneEvent:
    event_id: str
    run_id: str
    sequence: int
    event_type: ControlPlaneEventType
    occurred_at: str
    entity_type: ControlPlaneEntityType
    entity_id: str
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        for name in ("event_id", "run_id", "occurred_at", "entity_id"):
            _text(getattr(self, name), name)
        if not isinstance(self.sequence, int) or isinstance(self.sequence, bool) or self.sequence < 0:
            raise ValueError("sequence must be a non-boolean integer >= 0")
        if not isinstance(self.event_type, ControlPlaneEventType):
            raise ValueError("event_type must be a ControlPlaneEventType")
        if not isinstance(self.entity_type, ControlPlaneEntityType):
            raise ValueError("entity_type must be a ControlPlaneEntityType")
        if not isinstance(self.payload, Mapping):
            raise ValueError("payload must be a mapping")
        object.__setattr__(self, "payload", _freeze(self.payload, "payload"))

    def to_dict(self) -> dict[str, Any]:
        return {"event_id": self.event_id, "run_id": self.run_id, "sequence": self.sequence,
                "event_type": self.event_type.value, "occurred_at": self.occurred_at,
                "entity_type": self.entity_type.value, "entity_id": self.entity_id,
                "payload": _plain(self.payload)}
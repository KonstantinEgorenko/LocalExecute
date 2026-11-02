"""Immutable Control Plane domain contracts; no orchestration or persistence."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping, TypeAlias

from ..executor.models import ExecutionOutcome, ExecutionStage
from ..router.scoring import RoutingDecision

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | tuple["JsonValue", ...] | Mapping[str, "JsonValue"]


class GoalStatus(StrEnum):
    CREATED = "CREATED"
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    NEEDS_ATTENTION = "NEEDS_ATTENTION"
    CANCELLED = "CANCELLED"


class TodoStatus(StrEnum):
    PENDING = "PENDING"
    READY = "READY"
    CLAIMED = "CLAIMED"
    RUNNING = "RUNNING"
    WAITING_EVIDENCE = "WAITING_EVIDENCE"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    NEEDS_ATTENTION = "NEEDS_ATTENTION"
    CANCELLED = "CANCELLED"


class RunStatus(StrEnum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    NEEDS_ATTENTION = "NEEDS_ATTENTION"
    CANCELLED = "CANCELLED"


class EvidenceStatus(StrEnum):
    PENDING = "PENDING"
    PASSED = "PASSED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class GateStatus(StrEnum):
    PENDING = "PENDING"
    SATISFIED = "SATISFIED"
    FAILED = "FAILED"


class AttentionSeverity(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class AttentionStatus(StrEnum):
    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    RESOLVED = "RESOLVED"
    CANCELLED = "CANCELLED"


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _optional_text(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field_name)


def _non_negative_int(value: object, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{field_name} must be a non-boolean integer >= 0")
    return value


def _freeze_json(value: Any, field_name: str) -> JsonValue:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{field_name} must not contain non-finite floats")
        return value
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        frozen: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{field_name} mapping keys must be strings")
            frozen[key] = _freeze_json(item, f"{field_name}.{key}")
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item, field_name) for item in value)
    raise ValueError(f"{field_name} must be JSON-safe")


def _freeze_mapping(value: Mapping[str, Any] | None, field_name: str) -> Mapping[str, JsonValue]:
    if value is None:
        return MappingProxyType({})
    frozen = _freeze_json(value, field_name)
    if not isinstance(frozen, Mapping):
        raise ValueError(f"{field_name} must be a mapping")
    return frozen


def _id_references(values: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        raise ValueError(f"{field_name} must be a list or tuple of ID references")
    normalized = tuple(_required_text(item, field_name) for item in values)
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{field_name} must not contain duplicates")
    return normalized


def _json_value_to_dict(value: JsonValue) -> Any:
    if isinstance(value, Mapping):
        return {key: _json_value_to_dict(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_value_to_dict(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class Quota:
    max_worker_calls: int
    max_same_fast_worker_attempts: int
    max_same_heavy_worker_attempts: int
    max_replans: int
    max_escalations: int

    def __post_init__(self) -> None:
        for field_name in (
            "max_worker_calls", "max_same_fast_worker_attempts", "max_same_heavy_worker_attempts",
            "max_replans", "max_escalations",
        ):
            _non_negative_int(getattr(self, field_name), field_name)

    def to_dict(self) -> dict[str, int]:
        return {
            "max_worker_calls": self.max_worker_calls,
            "max_same_fast_worker_attempts": self.max_same_fast_worker_attempts,
            "max_same_heavy_worker_attempts": self.max_same_heavy_worker_attempts,
            "max_replans": self.max_replans,
            "max_escalations": self.max_escalations,
        }


@dataclass(frozen=True, slots=True)
class QuotaUsage:
    worker_calls: int = 0
    replans: int = 0
    escalations: int = 0
    worker_attempts: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name in ("worker_calls", "replans", "escalations"):
            _non_negative_int(getattr(self, field_name), field_name)
        if not isinstance(self.worker_attempts, Mapping):
            raise ValueError("worker_attempts must be a mapping")
        normalized = {}
        for worker_id, attempts in self.worker_attempts.items():
            normalized[_required_text(worker_id, "worker_attempts key")] = _non_negative_int(attempts, f"worker_attempts.{worker_id}")
        object.__setattr__(self, "worker_attempts", MappingProxyType(normalized))

    def to_dict(self) -> dict[str, Any]:
        return {"worker_calls": self.worker_calls, "replans": self.replans, "escalations": self.escalations, "worker_attempts": dict(self.worker_attempts)}


@dataclass(frozen=True, slots=True)
class Goal:
    goal_id: str
    objective: str
    status: GoalStatus
    created_at: str
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _required_text(self.goal_id, "goal_id")
        _required_text(self.objective, "objective")
        _required_text(self.created_at, "created_at")
        if not isinstance(self.status, GoalStatus):
            raise ValueError("status must be a GoalStatus")
        object.__setattr__(self, "metadata", _freeze_mapping(self.metadata, "metadata"))

    def to_dict(self) -> dict[str, Any]:
        return {"goal_id": self.goal_id, "objective": self.objective, "status": self.status.value, "created_at": self.created_at, "metadata": _json_value_to_dict(self.metadata)}


@dataclass(frozen=True, slots=True)
class Todo:
    todo_id: str
    goal_id: str
    objective: str
    status: TodoStatus
    dependencies: tuple[str, ...] = ()
    required_gates: tuple[str, ...] = ()
    priority: int = 0

    def __post_init__(self) -> None:
        _required_text(self.todo_id, "todo_id")
        _required_text(self.goal_id, "goal_id")
        _required_text(self.objective, "objective")
        if not isinstance(self.status, TodoStatus):
            raise ValueError("status must be a TodoStatus")
        object.__setattr__(self, "dependencies", _id_references(self.dependencies, "dependencies"))
        object.__setattr__(self, "required_gates", _id_references(self.required_gates, "required_gates"))
        _non_negative_int(self.priority, "priority")

    def to_dict(self) -> dict[str, Any]:
        return {"todo_id": self.todo_id, "goal_id": self.goal_id, "objective": self.objective, "status": self.status.value, "dependencies": list(self.dependencies), "required_gates": list(self.required_gates), "priority": self.priority}


@dataclass(frozen=True, slots=True)
class WorkContract:
    goal_id: str
    todo_id: str
    objective: str
    constraints: Mapping[str, JsonValue]
    allowed_scope: tuple[str, ...]
    forbidden_scope: tuple[str, ...]
    required_evidence: tuple[str, ...]
    quota: Quota

    def __post_init__(self) -> None:
        _required_text(self.goal_id, "goal_id")
        _required_text(self.todo_id, "todo_id")
        _required_text(self.objective, "objective")
        object.__setattr__(self, "constraints", _freeze_mapping(self.constraints, "constraints"))
        object.__setattr__(self, "allowed_scope", _id_references(self.allowed_scope, "allowed_scope"))
        object.__setattr__(self, "forbidden_scope", _id_references(self.forbidden_scope, "forbidden_scope"))
        object.__setattr__(self, "required_evidence", _id_references(self.required_evidence, "required_evidence"))
        if not isinstance(self.quota, Quota):
            raise ValueError("quota must be a Quota")

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal_id": self.goal_id, "todo_id": self.todo_id, "objective": self.objective,
            "constraints": _json_value_to_dict(self.constraints), "allowed_scope": list(self.allowed_scope),
            "forbidden_scope": list(self.forbidden_scope), "required_evidence": list(self.required_evidence),
            "quota": self.quota.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class Run:
    run_id: str
    goal_id: str
    active_todo_id: str | None
    status: RunStatus
    quota_usage: QuotaUsage

    def __post_init__(self) -> None:
        _required_text(self.run_id, "run_id")
        _required_text(self.goal_id, "goal_id")
        _optional_text(self.active_todo_id, "active_todo_id")
        if not isinstance(self.status, RunStatus):
            raise ValueError("status must be a RunStatus")
        if not isinstance(self.quota_usage, QuotaUsage):
            raise ValueError("quota_usage must be a QuotaUsage")

    def to_dict(self) -> dict[str, Any]:
        return {"run_id": self.run_id, "goal_id": self.goal_id, "active_todo_id": self.active_todo_id, "status": self.status.value, "quota_usage": self.quota_usage.to_dict()}


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    attempt_index: int
    todo_id: str
    routing_decision: RoutingDecision
    selected_model: str | None
    execution_outcome: ExecutionOutcome | None = None
    failure_stage: ExecutionStage | None = None
    failure_reason: str | None = None

    def __post_init__(self) -> None:
        _non_negative_int(self.attempt_index, "attempt_index")
        _required_text(self.todo_id, "todo_id")
        if not isinstance(self.routing_decision, RoutingDecision):
            raise ValueError("routing_decision must be a RoutingDecision")
        _optional_text(self.selected_model, "selected_model")
        _optional_text(self.failure_reason, "failure_reason")
        if self.execution_outcome is not None and not isinstance(self.execution_outcome, ExecutionOutcome):
            raise ValueError("execution_outcome must be an ExecutionOutcome or None")
        if self.failure_stage is not None and not isinstance(self.failure_stage, ExecutionStage):
            raise ValueError("failure_stage must be an ExecutionStage or None")
        if self.selected_model != self.routing_decision.selected_model:
            raise ValueError("selected_model must match routing_decision.selected_model")

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt_index": self.attempt_index, "todo_id": self.todo_id,
            "routing_decision": self.routing_decision.to_dict(), "selected_model": self.selected_model,
            "execution_outcome": self.execution_outcome.value if self.execution_outcome else None,
            "failure_stage": self.failure_stage.value if self.failure_stage else None,
            "failure_reason": self.failure_reason,
        }


@dataclass(frozen=True, slots=True)
class Evidence:
    evidence_id: str
    todo_id: str
    evidence_type: str
    source: str
    status: EvidenceStatus
    payload: Mapping[str, JsonValue] = field(default_factory=dict)
    reference: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("evidence_id", "todo_id", "evidence_type", "source"):
            _required_text(getattr(self, field_name), field_name)
        _optional_text(self.reference, "reference")
        if not isinstance(self.status, EvidenceStatus):
            raise ValueError("status must be an EvidenceStatus")
        object.__setattr__(self, "payload", _freeze_mapping(self.payload, "payload"))

    def to_dict(self) -> dict[str, Any]:
        return {"evidence_id": self.evidence_id, "todo_id": self.todo_id, "evidence_type": self.evidence_type, "source": self.source, "status": self.status.value, "payload": _json_value_to_dict(self.payload), "reference": self.reference}


@dataclass(frozen=True, slots=True)
class Gate:
    gate_id: str
    gate_type: str
    required: bool
    status: GateStatus

    def __post_init__(self) -> None:
        _required_text(self.gate_id, "gate_id")
        _required_text(self.gate_type, "gate_type")
        if not isinstance(self.required, bool):
            raise ValueError("required must be a bool")
        if not isinstance(self.status, GateStatus):
            raise ValueError("status must be a GateStatus")

    def to_dict(self) -> dict[str, Any]:
        return {"gate_id": self.gate_id, "gate_type": self.gate_type, "required": self.required, "status": self.status.value}


@dataclass(frozen=True, slots=True)
class Handoff:
    todo_id: str
    from_model: str
    to_model: str
    reason: str
    attempt_index: int

    def __post_init__(self) -> None:
        for field_name in ("todo_id", "from_model", "to_model", "reason"):
            _required_text(getattr(self, field_name), field_name)
        _non_negative_int(self.attempt_index, "attempt_index")

    def to_dict(self) -> dict[str, Any]:
        return {"todo_id": self.todo_id, "from_model": self.from_model, "to_model": self.to_model, "reason": self.reason, "attempt_index": self.attempt_index}


@dataclass(frozen=True, slots=True)
class AttentionItem:
    attention_id: str
    reason: str
    severity: AttentionSeverity
    status: AttentionStatus
    todo_id: str | None = None
    run_id: str | None = None
    category: str | None = None

    def __post_init__(self) -> None:
        _required_text(self.attention_id, "attention_id")
        _required_text(self.reason, "reason")
        if not isinstance(self.severity, AttentionSeverity):
            raise ValueError("severity must be an AttentionSeverity")
        if not isinstance(self.status, AttentionStatus):
            raise ValueError("status must be an AttentionStatus")
        _optional_text(self.todo_id, "todo_id")
        _optional_text(self.run_id, "run_id")
        _optional_text(self.category, "category")
        if self.todo_id is None and self.run_id is None:
            raise ValueError("AttentionItem requires todo_id or run_id")

    def to_dict(self) -> dict[str, Any]:
        return {"attention_id": self.attention_id, "todo_id": self.todo_id, "run_id": self.run_id, "reason": self.reason, "severity": self.severity.value, "category": self.category, "status": self.status.value}
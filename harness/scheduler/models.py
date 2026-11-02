'Immutable scheduler scheduler / claim / lease contracts.'
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping

from ..judge.models import JudgeAction, JudgeDecision


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty string")
    return value


def _non_negative_int(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be integer >= 0")
    return value


def _positive_int(value: object, name: str) -> int:
    value = _non_negative_int(value, name)
    if value == 0:
        raise ValueError(f"{name} must be > 0")
    return value


def _unique_text(values: object, name: str) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        raise ValueError(f"{name} must be list/tuple")
    result = tuple(_text(x, name) for x in values)
    if len(set(result)) != len(result):
        raise ValueError(f"{name} must not contain duplicates")
    return result


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def stable_id(prefix: str, payload: object, *, length: int = 24) -> str:
    return f"{prefix}-{hashlib.sha256(canonical_json(payload).encode('utf-8')).hexdigest()[:length]}"


class SchedulerTodoStatus(StrEnum):
    PENDING = "PENDING"
    READY = "READY"
    CLAIMED = "CLAIMED"
    RUNNING = "RUNNING"
    WAITING_DECISION = "WAITING_DECISION"
    REPLAN_REQUIRED = "REPLAN_REQUIRED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    CANCELLED = "CANCELLED"

    @property
    def terminal(self) -> bool:
        return self in {
            SchedulerTodoStatus.SUCCEEDED,
            SchedulerTodoStatus.FAILED,
            SchedulerTodoStatus.BLOCKED,
            SchedulerTodoStatus.HUMAN_REVIEW,
            SchedulerTodoStatus.CANCELLED,
        }


class ClaimStatus(StrEnum):
    ACTIVE = "ACTIVE"
    RELEASED = "RELEASED"
    EXPIRED = "EXPIRED"


class SchedulerLeaseStatus(StrEnum):
    ACTIVE = "ACTIVE"
    RELEASED = "RELEASED"
    EXPIRED = "EXPIRED"


@dataclass(frozen=True, slots=True)
class SchedulerPolicy:
    max_parallel_todos: int = 2
    max_active_claims: int = 8
    max_attempts_per_todo: int = 6
    claim_ttl_seconds: int = 900
    resource_lease_ttl_seconds: int = 900
    lock_timeout_seconds: int = 10
    serialize_unknown_working_sets: bool = True
    require_judge_report: bool = True
    allow_parallel_conflict_free: bool = True

    def __post_init__(self) -> None:
        for name in (
            "max_parallel_todos", "max_active_claims", "max_attempts_per_todo",
            "claim_ttl_seconds", "resource_lease_ttl_seconds", "lock_timeout_seconds",
        ):
            _positive_int(getattr(self, name), name)
        if self.max_parallel_todos > self.max_active_claims:
            raise ValueError("max_parallel_todos must not exceed max_active_claims")
        for name in ("serialize_unknown_working_sets", "require_judge_report", "allow_parallel_conflict_free"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be bool")
        if self.serialize_unknown_working_sets is not True:
            raise ValueError("Phase 11 V1 requires serialize_unknown_working_sets=true")
        if self.require_judge_report is not True:
            raise ValueError("Phase 11 scheduler requires JudgeReport authority")

    @classmethod
    def from_policies(cls, policies: Mapping[str, Any]) -> "SchedulerPolicy":
        raw = policies.get("scheduler", {})
        if not isinstance(raw, Mapping):
            raise ValueError("policies.scheduler must be a mapping")
        return cls(
            max_parallel_todos=int(raw.get("max_parallel_todos", 2)),
            max_active_claims=int(raw.get("max_active_claims", 8)),
            max_attempts_per_todo=int(raw.get("max_attempts_per_todo", 6)),
            claim_ttl_seconds=int(raw.get("claim_ttl_seconds", 900)),
            resource_lease_ttl_seconds=int(raw.get("resource_lease_ttl_seconds", 900)),
            lock_timeout_seconds=int(raw.get("lock_timeout_seconds", 10)),
            serialize_unknown_working_sets=bool(raw.get("serialize_unknown_working_sets", True)),
            require_judge_report=bool(raw.get("require_judge_report", True)),
            allow_parallel_conflict_free=bool(raw.get("allow_parallel_conflict_free", True)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_parallel_todos": self.max_parallel_todos,
            "max_active_claims": self.max_active_claims,
            "max_attempts_per_todo": self.max_attempts_per_todo,
            "claim_ttl_seconds": self.claim_ttl_seconds,
            "resource_lease_ttl_seconds": self.resource_lease_ttl_seconds,
            "lock_timeout_seconds": self.lock_timeout_seconds,
            "serialize_unknown_working_sets": self.serialize_unknown_working_sets,
            "require_judge_report": self.require_judge_report,
            "allow_parallel_conflict_free": self.allow_parallel_conflict_free,
        }


@dataclass(frozen=True, slots=True)
class WorkingSetPrediction:
    retrieval_id: str | None
    paths: tuple[str, ...] = ()
    unknown: bool = True

    def __post_init__(self) -> None:
        if self.retrieval_id is not None:
            _text(self.retrieval_id, "retrieval_id")
        normalized = tuple(sorted({str(x).replace("\\", "/").strip("/") for x in self.paths if isinstance(x, str) and x.strip()}, key=str.casefold))
        if any(path.startswith("../") or "/../" in f"/{path}/" or path.startswith("/") for path in normalized):
            raise ValueError("working-set paths must be safe repository-relative paths")
        object.__setattr__(self, "paths", normalized)
        if not isinstance(self.unknown, bool):
            raise ValueError("unknown must be bool")
        if self.paths:
            if self.retrieval_id is None:
                raise ValueError("grounded working-set paths require retrieval_id")
            if self.unknown:
                object.__setattr__(self, "unknown", False)
        elif not self.unknown:
            object.__setattr__(self, "unknown", True)

    def to_dict(self) -> dict[str, Any]:
        return {"retrieval_id": self.retrieval_id, "paths": list(self.paths), "unknown": self.unknown}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "WorkingSetPrediction":
        return cls(raw.get("retrieval_id"), tuple(str(x) for x in raw.get("paths", ())), bool(raw.get("unknown", True)))


@dataclass(frozen=True, slots=True)
class SchedulerTodoState:
    todo_id: str
    goal_id: str
    run_id: str
    objective: str
    dependencies: tuple[str, ...]
    priority: int
    status: SchedulerTodoStatus = SchedulerTodoStatus.PENDING
    attempt_count: int = 0
    active_claim_id: str | None = None
    worktree_lease_id: str | None = None
    last_judge_report_id: str | None = None
    next_action: JudgeAction = JudgeAction.NONE
    working_set: WorkingSetPrediction = field(default_factory=lambda: WorkingSetPrediction(None, (), True))
    blocked_reason: str | None = None
    version: int = 0

    def __post_init__(self) -> None:
        for name in ("todo_id", "goal_id", "run_id", "objective"):
            _text(getattr(self, name), name)
        object.__setattr__(self, "dependencies", _unique_text(self.dependencies, "dependencies"))
        _non_negative_int(self.priority, "priority")
        _non_negative_int(self.attempt_count, "attempt_count")
        _non_negative_int(self.version, "version")
        if not isinstance(self.status, SchedulerTodoStatus):
            raise ValueError("status must be SchedulerTodoStatus")
        if self.active_claim_id is not None:
            _text(self.active_claim_id, "active_claim_id")
        if self.worktree_lease_id is not None:
            _text(self.worktree_lease_id, "worktree_lease_id")
        if self.last_judge_report_id is not None:
            _text(self.last_judge_report_id, "last_judge_report_id")
        if not isinstance(self.next_action, JudgeAction):
            raise ValueError("next_action must be JudgeAction")
        if not isinstance(self.working_set, WorkingSetPrediction):
            raise ValueError("working_set must be WorkingSetPrediction")
        if self.blocked_reason is not None and not isinstance(self.blocked_reason, str):
            raise ValueError("blocked_reason must be str|None")
        if self.status in {SchedulerTodoStatus.CLAIMED, SchedulerTodoStatus.RUNNING, SchedulerTodoStatus.WAITING_DECISION} and self.active_claim_id is None:
            raise ValueError("active scheduler state requires active_claim_id")
        if self.status.terminal and self.active_claim_id is not None:
            raise ValueError("terminal scheduler state cannot retain active claim")

    def to_dict(self) -> dict[str, Any]:
        return {
            "todo_id": self.todo_id, "goal_id": self.goal_id, "run_id": self.run_id, "objective": self.objective,
            "dependencies": list(self.dependencies), "priority": self.priority, "status": self.status.value,
            "attempt_count": self.attempt_count, "active_claim_id": self.active_claim_id,
            "worktree_lease_id": self.worktree_lease_id, "last_judge_report_id": self.last_judge_report_id,
            "next_action": self.next_action.value, "working_set": self.working_set.to_dict(),
            "blocked_reason": self.blocked_reason, "version": self.version,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SchedulerTodoState":
        return cls(
            str(raw["todo_id"]), str(raw["goal_id"]), str(raw["run_id"]), str(raw["objective"]),
            tuple(str(x) for x in raw.get("dependencies", ())), int(raw.get("priority", 0)),
            SchedulerTodoStatus(str(raw.get("status", "PENDING"))), int(raw.get("attempt_count", 0)),
            raw.get("active_claim_id"), raw.get("worktree_lease_id"), raw.get("last_judge_report_id"),
            JudgeAction(str(raw.get("next_action", "NONE"))), WorkingSetPrediction.from_dict(raw.get("working_set", {})),
            raw.get("blocked_reason"), int(raw.get("version", 0)),
        )


@dataclass(frozen=True, slots=True)
class SchedulerClaim:
    claim_id: str
    todo_id: str
    owner_id: str
    generation: int
    acquired_at: int
    expires_at: int
    status: ClaimStatus = ClaimStatus.ACTIVE
    released_at: int | None = None

    def __post_init__(self) -> None:
        for name in ("claim_id", "todo_id", "owner_id"):
            _text(getattr(self, name), name)
        _positive_int(self.generation, "generation")
        _non_negative_int(self.acquired_at, "acquired_at")
        _positive_int(self.expires_at, "expires_at")
        if self.expires_at <= self.acquired_at:
            raise ValueError("expires_at must be greater than acquired_at")
        if not isinstance(self.status, ClaimStatus):
            raise ValueError("status must be ClaimStatus")
        if self.released_at is not None:
            _non_negative_int(self.released_at, "released_at")

    @classmethod
    def create(cls, *, todo_id: str, owner_id: str, generation: int, now: int, ttl_seconds: int) -> "SchedulerClaim":
        payload = {"todo_id": todo_id, "owner_id": owner_id, "generation": generation}
        return cls(stable_id("claim", payload), todo_id, owner_id, generation, now, now + ttl_seconds)

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id, "todo_id": self.todo_id, "owner_id": self.owner_id,
            "generation": self.generation, "acquired_at": self.acquired_at, "expires_at": self.expires_at,
            "status": self.status.value, "released_at": self.released_at,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SchedulerClaim":
        return cls(str(raw["claim_id"]), str(raw["todo_id"]), str(raw["owner_id"]), int(raw["generation"]), int(raw["acquired_at"]), int(raw["expires_at"]), ClaimStatus(str(raw["status"])), raw.get("released_at"))


@dataclass(frozen=True, slots=True)
class SchedulerLease:
    lease_id: str
    resource_key: str
    claim_id: str
    owner_id: str
    acquired_at: int
    expires_at: int
    status: SchedulerLeaseStatus = SchedulerLeaseStatus.ACTIVE
    released_at: int | None = None

    def __post_init__(self) -> None:
        for name in ("lease_id", "resource_key", "claim_id", "owner_id"):
            _text(getattr(self, name), name)
        _non_negative_int(self.acquired_at, "acquired_at")
        _positive_int(self.expires_at, "expires_at")
        if self.expires_at <= self.acquired_at:
            raise ValueError("expires_at must be greater than acquired_at")
        if not isinstance(self.status, SchedulerLeaseStatus):
            raise ValueError("status must be SchedulerLeaseStatus")
        if self.released_at is not None:
            _non_negative_int(self.released_at, "released_at")

    @classmethod
    def create(cls, *, resource_key: str, claim_id: str, owner_id: str, now: int, ttl_seconds: int) -> "SchedulerLease":
        payload = {"resource_key": resource_key, "claim_id": claim_id, "acquired_at": now}
        return cls(stable_id("sched-lease", payload), resource_key, claim_id, owner_id, now, now + ttl_seconds)

    def to_dict(self) -> dict[str, Any]:
        return {
            "lease_id": self.lease_id, "resource_key": self.resource_key, "claim_id": self.claim_id,
            "owner_id": self.owner_id, "acquired_at": self.acquired_at, "expires_at": self.expires_at,
            "status": self.status.value, "released_at": self.released_at,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SchedulerLease":
        return cls(str(raw["lease_id"]), str(raw["resource_key"]), str(raw["claim_id"]), str(raw["owner_id"]), int(raw["acquired_at"]), int(raw["expires_at"]), SchedulerLeaseStatus(str(raw["status"])), raw.get("released_at"))


@dataclass(frozen=True, slots=True)
class SchedulerDecisionResult:
    todo_id: str
    previous_status: SchedulerTodoStatus
    status: SchedulerTodoStatus
    decision: JudgeDecision
    next_action: JudgeAction
    attempt_count: int

    def __post_init__(self) -> None:
        _text(self.todo_id, "todo_id")
        if not isinstance(self.previous_status, SchedulerTodoStatus) or not isinstance(self.status, SchedulerTodoStatus):
            raise ValueError("scheduler status fields invalid")
        if not isinstance(self.decision, JudgeDecision) or not isinstance(self.next_action, JudgeAction):
            raise ValueError("judge fields invalid")
        _non_negative_int(self.attempt_count, "attempt_count")

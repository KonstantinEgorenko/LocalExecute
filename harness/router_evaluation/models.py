"""Immutable Router evaluation evaluation and ablation models."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping


class AblationMode(StrEnum):
    FULL = "full_v2"
    NO_CASCADE = "no_failure_aware_cascade"
    NO_FINALIZATION_GRACE = "no_finalization_grace"
    NO_TARGETED_QWEN36 = "no_targeted_qwen36"
    NO_EMPIRICAL_PRIORS = "no_empirical_priors"
    NO_WARM_BONUS = "no_warm_bonus"
    NO_FAILURE_PENALTY = "no_failure_penalty"
    NO_LATENCY_LOAD_AWARENESS = "no_latency_load_awareness"


def _nonempty(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty string")
    return value


def _nonneg(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be integer >= 0")
    return value


@dataclass(frozen=True, slots=True)
class AttemptObservation:
    profile_id: str
    verified: bool
    latency_ms: int
    model_calls: int
    tool_calls: int
    protocol_failures: int
    tool_failures: int
    finalization_calls: int
    outcome: str

    def __post_init__(self) -> None:
        _nonempty(self.profile_id, "profile_id")
        _nonempty(self.outcome, "outcome")
        if not isinstance(self.verified, bool):
            raise ValueError("verified must be bool")
        for name in (
            "latency_ms", "model_calls", "tool_calls", "protocol_failures",
            "tool_failures", "finalization_calls",
        ):
            _nonneg(getattr(self, name), name)
        if self.finalization_calls > 1:
            raise ValueError("finalization_calls must be <= 1")

    @property
    def heavy(self) -> bool:
        return self.profile_id in {"qwen38_heavy_fast", "qwen36_heavy_quality"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "verified": self.verified,
            "latency_ms": self.latency_ms,
            "model_calls": self.model_calls,
            "tool_calls": self.tool_calls,
            "protocol_failures": self.protocol_failures,
            "tool_failures": self.tool_failures,
            "finalization_calls": self.finalization_calls,
            "outcome": self.outcome,
        }


@dataclass(frozen=True, slots=True)
class CaseObservation:
    run_key: str
    source: str
    repetition: int
    case_id: str
    task_class: str
    selected_profile_id: str | None
    attempts: tuple[AttemptObservation, ...]
    verified: bool
    latency_ms: int
    infrastructure_invalid: bool = False
    router_selection_match: bool = True

    def __post_init__(self) -> None:
        _nonempty(self.run_key, "run_key")
        _nonempty(self.source, "source")
        _nonempty(self.case_id, "case_id")
        _nonempty(self.task_class, "task_class")
        _nonneg(self.repetition, "repetition")
        _nonneg(self.latency_ms, "latency_ms")
        if self.selected_profile_id is not None:
            _nonempty(self.selected_profile_id, "selected_profile_id")
        if not isinstance(self.attempts, tuple) or any(
            not isinstance(item, AttemptObservation) for item in self.attempts
        ):
            raise ValueError("attempts must be tuple[AttemptObservation, ...]")
        for name in ("verified", "infrastructure_invalid", "router_selection_match"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be bool")
        if self.attempts and self.selected_profile_id != self.attempts[0].profile_id:
            raise ValueError("first attempt must equal selected_profile_id")
        if self.verified and not any(item.verified for item in self.attempts):
            raise ValueError("verified case requires a verified attempt")

    @property
    def selected_verified(self) -> bool:
        return bool(self.attempts and self.attempts[0].verified)

    @property
    def fallback_used(self) -> bool:
        return len(self.attempts) > 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_key": self.run_key,
            "source": self.source,
            "repetition": self.repetition,
            "case_id": self.case_id,
            "task_class": self.task_class,
            "selected_profile_id": self.selected_profile_id,
            "attempts": [item.to_dict() for item in self.attempts],
            "verified": self.verified,
            "latency_ms": self.latency_ms,
            "infrastructure_invalid": self.infrastructure_invalid,
            "router_selection_match": self.router_selection_match,
        }


@dataclass(frozen=True, slots=True)
class EvaluationMetrics:
    raw_runs: int
    valid_runs: int
    infrastructure_invalid_runs: int
    selected_verified_runs: int
    selected_verified_rate: float
    cascade_verified_runs: int
    cascade_verified_rate: float
    fallback_runs: int
    fallback_rate: float
    total_attempts: int
    heavy_attempts: int
    heavy_attempt_share: float
    model_calls: int
    tool_calls: int
    protocol_failures: int
    tool_failures: int
    wasted_failed_latency_ms: int
    mean_time_to_verified_success_ms: float | None
    p50_time_to_verified_success_ms: float | None
    p95_time_to_verified_success_ms: float | None
    profile_attempt_counts: Mapping[str, int]
    profile_verified_counts: Mapping[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw_runs": self.raw_runs,
            "valid_runs": self.valid_runs,
            "infrastructure_invalid_runs": self.infrastructure_invalid_runs,
            "selected_verified_runs": self.selected_verified_runs,
            "selected_verified_rate": self.selected_verified_rate,
            "cascade_verified_runs": self.cascade_verified_runs,
            "cascade_verified_rate": self.cascade_verified_rate,
            "fallback_runs": self.fallback_runs,
            "fallback_rate": self.fallback_rate,
            "total_attempts": self.total_attempts,
            "heavy_attempts": self.heavy_attempts,
            "heavy_attempt_share": self.heavy_attempt_share,
            "model_calls": self.model_calls,
            "tool_calls": self.tool_calls,
            "protocol_failures": self.protocol_failures,
            "tool_failures": self.tool_failures,
            "wasted_failed_latency_ms": self.wasted_failed_latency_ms,
            "mean_time_to_verified_success_ms": self.mean_time_to_verified_success_ms,
            "p50_time_to_verified_success_ms": self.p50_time_to_verified_success_ms,
            "p95_time_to_verified_success_ms": self.p95_time_to_verified_success_ms,
            "profile_attempt_counts": dict(self.profile_attempt_counts),
            "profile_verified_counts": dict(self.profile_verified_counts),
        }


@dataclass(frozen=True, slots=True)
class ExecutionAblationResult:
    mode: AblationMode
    semantics: str
    metrics: EvaluationMetrics

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "semantics": self.semantics,
            "metrics": self.metrics.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class DecisionAblationResult:
    mode: AblationMode
    semantics: str
    cases: int
    selection_changes: int
    selections: Mapping[str, str | None]

    def __post_init__(self) -> None:
        if not isinstance(self.mode, AblationMode):
            raise ValueError("mode must be AblationMode")
        _nonempty(self.semantics, "semantics")
        _nonneg(self.cases, "cases")
        _nonneg(self.selection_changes, "selection_changes")

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "semantics": self.semantics,
            "cases": self.cases,
            "selection_changes": self.selection_changes,
            "selections": dict(self.selections),
        }

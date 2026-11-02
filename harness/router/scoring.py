"""Explainable deterministic candidate and decision structures."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any


class DecisionType(StrEnum):
    WORKER = "WORKER"
    ORCHESTRATOR = "ORCHESTRATOR"
    NO_ELIGIBLE_MODEL = "NO_ELIGIBLE_MODEL"


class ResourceFeasibilityMode(StrEnum):
    REUSE_LOADED = "REUSE_LOADED"
    LOAD_DIRECT = "LOAD_DIRECT"
    SWITCH_REQUIRED = "SWITCH_REQUIRED"
    NOT_FEASIBLE = "NOT_FEASIBLE"


@dataclass(frozen=True, slots=True)
class ResourceFeasibility:
    allowed: bool
    mode: ResourceFeasibilityMode
    requested_vram_gb: float | None
    incremental_vram_gb: float | None
    current_free_vram_gb: float | None
    reclaimable_vram_gb: float
    projected_free_vram_gb: float | None
    soft_target_exceeded: bool
    reason: str
    vram_source: str | None = None
    observed_vram_gb: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "mode": self.mode.value}


@dataclass(frozen=True, slots=True)
class CandidateScore:
    model_id: str
    base_score: int
    capability_score: int
    task_fit_score: int
    resource_score: int
    failure_penalty: int
    escalation_adjustment: int
    total_score: int
    eligible: bool
    rejection_reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    decision_type: DecisionType
    selected_model: str | None
    score: int | None
    router_confidence: float
    routing_reason: str
    alternatives: tuple[CandidateScore, ...]
    resource_admission: dict[str, Any] | None
    escalation_from: str | None
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_type": self.decision_type.value,
            "selected_model": self.selected_model,
            "score": self.score,
            "router_confidence": self.router_confidence,
            "routing_reason": self.routing_reason,
            "alternatives": [item.to_dict() for item in self.alternatives],
            "resource_admission": self.resource_admission,
            "escalation_from": self.escalation_from,
            "warnings": list(self.warnings),
        }
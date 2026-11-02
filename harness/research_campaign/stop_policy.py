'Deterministic durable research stopping continue/stop policy contracts.'
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping, Protocol

from .campaign import ResearchBudgetUsageV1
from .serialization import safe_id, stable_sha256, text, timestamp, unique_text
from .strategy_contracts import ResearchStrategyBudgetV1


class ResearchStopReasonV1(StrEnum):
    CONTINUE = "CONTINUE"
    OBJECTIVE_REACHED = "OBJECTIVE_REACHED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    NO_ADMISSIBLE_PROPOSALS = "NO_ADMISSIBLE_PROPOSALS"
    PLATEAU_DETECTED = "PLATEAU_DETECTED"
    EVIDENCE_SATURATED = "EVIDENCE_SATURATED"
    TECHNICALLY_BLOCKED = "TECHNICALLY_BLOCKED"
    HUMAN_DECISION_REQUIRED = "HUMAN_DECISION_REQUIRED"


@dataclass(frozen=True, slots=True)
class ResearchStopDecisionV1:
    campaign_id: str
    reason: ResearchStopReasonV1
    explanation: str
    supporting_evidence_refs: tuple[str, ...]
    budget_snapshot: ResearchStrategyBudgetV1
    strategy_decision_refs: tuple[str, ...]
    created_at: str

    def __post_init__(self) -> None:
        safe_id(self.campaign_id, "campaign_id")
        if not isinstance(self.reason, ResearchStopReasonV1):
            raise ValueError("reason must be ResearchStopReasonV1")
        object.__setattr__(self, "explanation", text(self.explanation, "explanation", max_length=32_000))
        object.__setattr__(self, "supporting_evidence_refs", unique_text(self.supporting_evidence_refs, "supporting_evidence_refs", maximum=512, item_max_length=8_000))
        object.__setattr__(self, "strategy_decision_refs", unique_text(self.strategy_decision_refs, "strategy_decision_refs", maximum=512, item_max_length=8_000))
        if not isinstance(self.budget_snapshot, ResearchStrategyBudgetV1):
            raise ValueError("budget_snapshot must be ResearchStrategyBudgetV1")
        object.__setattr__(self, "created_at", timestamp(self.created_at, "created_at"))

    @property
    def fingerprint(self) -> str:
        return stable_sha256(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {"schema_version": 1, "campaign_id": self.campaign_id, "reason": self.reason.value,
                "explanation": self.explanation, "supporting_evidence_refs": list(self.supporting_evidence_refs),
                "budget_snapshot": self.budget_snapshot.to_dict(), "strategy_decision_refs": list(self.strategy_decision_refs),
                "created_at": self.created_at}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ResearchStopDecisionV1":
        if raw.get("schema_version") != 1:
            raise ValueError("ResearchStopDecisionV1 schema_version must be 1")
        result = cls(str(raw["campaign_id"]), ResearchStopReasonV1(str(raw["reason"])),
                     str(raw["explanation"]), tuple(raw.get("supporting_evidence_refs", ())),
                     ResearchStrategyBudgetV1.from_dict(raw["budget_snapshot"]),
                     tuple(raw.get("strategy_decision_refs", ())), str(raw["created_at"]))
        expected = raw.get("fingerprint")
        if expected is not None and expected != result.fingerprint:
            raise ValueError("ResearchStopDecisionV1 fingerprint mismatch")
        return result




@dataclass(frozen=True, slots=True)
class ResearchStopPolicyV1:
    plateau_valid_experiments: int = 3
    plateau_epsilon: float = 0.0
    technical_failure_limit: int = 3

    def __post_init__(self) -> None:
        if self.plateau_valid_experiments < 1 or self.technical_failure_limit < 1:
            raise ValueError("stop-policy count limits must be positive")
        if self.plateau_epsilon < 0:
            raise ValueError("plateau_epsilon must be non-negative")

    def decide(self, *, campaign_id: str, budget: ResearchStrategyBudgetV1, usage: ResearchBudgetUsageV1,
               created_at: str, evidence_refs: tuple[str, ...] = (), strategy_refs: tuple[str, ...] = (),
               objective_reached: bool = False, no_admissible_proposals: bool = False,
               evidence_saturated: bool = False, human_decision_required: bool = False,
               valid_dev_metrics: tuple[float, ...] = (), metric_direction: str = "MAXIMIZE",
               objective_target: float | None = None) -> ResearchStopDecisionV1:
        direction = str(metric_direction).upper()
        if valid_dev_metrics and direction not in {"MAXIMIZE", "MINIMIZE"}:
            raise ValueError("metric_direction must be MAXIMIZE or MINIMIZE")
        target_reached = objective_reached
        if objective_target is not None and valid_dev_metrics:
            best = max(valid_dev_metrics) if direction == "MAXIMIZE" else min(valid_dev_metrics)
            target_reached = best >= objective_target if direction == "MAXIMIZE" else best <= objective_target
        plateau = self._plateau(valid_dev_metrics, direction)
        if target_reached:
            reason, explanation = ResearchStopReasonV1.OBJECTIVE_REACHED, "persisted DEV objective criterion is satisfied"
        elif budget.remaining_experiments <= 0:
            reason, explanation = ResearchStopReasonV1.BUDGET_EXHAUSTED, "no experiment budget remains"
        elif human_decision_required:
            reason, explanation = ResearchStopReasonV1.HUMAN_DECISION_REQUIRED, "campaign contract requires a human decision"
        elif no_admissible_proposals:
            reason, explanation = ResearchStopReasonV1.NO_ADMISSIBLE_PROPOSALS, "all persisted proposals were hard-rejected"
        elif evidence_saturated:
            reason, explanation = ResearchStopReasonV1.EVIDENCE_SATURATED, "bounded evidence contract is saturated"
        elif usage.consecutive_crashes >= self.technical_failure_limit:
            reason, explanation = ResearchStopReasonV1.TECHNICALLY_BLOCKED, "repeated technical failures blocked execution; this is not scientific evidence"
        elif plateau:
            reason, explanation = ResearchStopReasonV1.PLATEAU_DETECTED, "direction-aware valid scientific DEV improvement is within plateau_epsilon"
        else:
            reason, explanation = ResearchStopReasonV1.CONTINUE, "remaining budget and persisted evidence permit another sequential cycle"
        return ResearchStopDecisionV1(campaign_id, reason, explanation, evidence_refs, budget, strategy_refs, created_at)

    def _plateau(self, metrics: tuple[float, ...], direction: str) -> bool:
        window_size = self.plateau_valid_experiments
        if len(metrics) <= window_size:
            return False
        previous, window = metrics[:-window_size], metrics[-window_size:]
        if direction == "MAXIMIZE":
            improvement = max(window) - max(previous)
        else:
            improvement = min(previous) - min(window)
        return improvement <= self.plateau_epsilon


class ResearchStopPolicyPort(Protocol):
    def decide(self, **kwargs: object) -> ResearchStopDecisionV1: ...

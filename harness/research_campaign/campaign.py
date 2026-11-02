'Durable Research Campaign runtime state for research campaign.R3.'
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping

from .serialization import (
    non_negative_int,
    non_negative_number,
    optional_safe_id,
    optional_sha256_hex,
    optional_text,
    text,
    safe_id,
    timestamp,
)


class ResearchCampaignStatusV1(StrEnum):
    CREATED = "CREATED"
    BASELINE_REQUIRED = "BASELINE_REQUIRED"
    SEARCHING = "SEARCHING"
    WAITING_FOR_INPUT = "WAITING_FOR_INPUT"
    WAITING_FOR_RESOURCE = "WAITING_FOR_RESOURCE"
    STAGNATED = "STAGNATED"
    CONFIRMING_TOP_K = "CONFIRMING_TOP_K"
    FINAL_EVALUATION = "FINAL_EVALUATION"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    COMPLETED = "COMPLETED"


_TERMINAL_CAMPAIGN_STATUSES = {
    ResearchCampaignStatusV1.BUDGET_EXHAUSTED,
    ResearchCampaignStatusV1.COMPLETED,
}

_ALLOWED_TRANSITIONS: dict[ResearchCampaignStatusV1, set[ResearchCampaignStatusV1]] = {
    ResearchCampaignStatusV1.CREATED: {
        ResearchCampaignStatusV1.BASELINE_REQUIRED,
        ResearchCampaignStatusV1.WAITING_FOR_INPUT,
        ResearchCampaignStatusV1.WAITING_FOR_RESOURCE,
        ResearchCampaignStatusV1.BUDGET_EXHAUSTED,
    },
    ResearchCampaignStatusV1.BASELINE_REQUIRED: {
        ResearchCampaignStatusV1.BASELINE_REQUIRED,
        ResearchCampaignStatusV1.SEARCHING,
        ResearchCampaignStatusV1.WAITING_FOR_INPUT,
        ResearchCampaignStatusV1.WAITING_FOR_RESOURCE,
        ResearchCampaignStatusV1.STAGNATED,
        ResearchCampaignStatusV1.BUDGET_EXHAUSTED,
    },
    ResearchCampaignStatusV1.SEARCHING: {
        ResearchCampaignStatusV1.SEARCHING,
        ResearchCampaignStatusV1.WAITING_FOR_INPUT,
        ResearchCampaignStatusV1.WAITING_FOR_RESOURCE,
        ResearchCampaignStatusV1.STAGNATED,
        ResearchCampaignStatusV1.CONFIRMING_TOP_K,
        ResearchCampaignStatusV1.BUDGET_EXHAUSTED,
    },
    ResearchCampaignStatusV1.WAITING_FOR_INPUT: {
        ResearchCampaignStatusV1.BASELINE_REQUIRED,
        ResearchCampaignStatusV1.SEARCHING,
        ResearchCampaignStatusV1.STAGNATED,
        ResearchCampaignStatusV1.CONFIRMING_TOP_K,
        ResearchCampaignStatusV1.FINAL_EVALUATION,
        ResearchCampaignStatusV1.WAITING_FOR_INPUT,
        ResearchCampaignStatusV1.WAITING_FOR_RESOURCE,
        ResearchCampaignStatusV1.BUDGET_EXHAUSTED,
    },
    ResearchCampaignStatusV1.WAITING_FOR_RESOURCE: {
        ResearchCampaignStatusV1.BASELINE_REQUIRED,
        ResearchCampaignStatusV1.SEARCHING,
        ResearchCampaignStatusV1.STAGNATED,
        ResearchCampaignStatusV1.CONFIRMING_TOP_K,
        ResearchCampaignStatusV1.FINAL_EVALUATION,
        ResearchCampaignStatusV1.WAITING_FOR_INPUT,
        ResearchCampaignStatusV1.WAITING_FOR_RESOURCE,
        ResearchCampaignStatusV1.BUDGET_EXHAUSTED,
    },
    ResearchCampaignStatusV1.STAGNATED: {
        ResearchCampaignStatusV1.STAGNATED,
        ResearchCampaignStatusV1.BASELINE_REQUIRED,
        ResearchCampaignStatusV1.SEARCHING,
        ResearchCampaignStatusV1.WAITING_FOR_INPUT,
        ResearchCampaignStatusV1.WAITING_FOR_RESOURCE,
        ResearchCampaignStatusV1.CONFIRMING_TOP_K,
        ResearchCampaignStatusV1.BUDGET_EXHAUSTED,
    },
    ResearchCampaignStatusV1.CONFIRMING_TOP_K: {
        ResearchCampaignStatusV1.CONFIRMING_TOP_K,
        ResearchCampaignStatusV1.SEARCHING,
        ResearchCampaignStatusV1.STAGNATED,
        ResearchCampaignStatusV1.WAITING_FOR_INPUT,
        ResearchCampaignStatusV1.WAITING_FOR_RESOURCE,
        ResearchCampaignStatusV1.FINAL_EVALUATION,
        ResearchCampaignStatusV1.BUDGET_EXHAUSTED,
    },
    ResearchCampaignStatusV1.FINAL_EVALUATION: {
        ResearchCampaignStatusV1.FINAL_EVALUATION,
        ResearchCampaignStatusV1.STAGNATED,
        ResearchCampaignStatusV1.WAITING_FOR_INPUT,
        ResearchCampaignStatusV1.WAITING_FOR_RESOURCE,
        ResearchCampaignStatusV1.COMPLETED,
        ResearchCampaignStatusV1.BUDGET_EXHAUSTED,
    },
    ResearchCampaignStatusV1.BUDGET_EXHAUSTED: set(),
    ResearchCampaignStatusV1.COMPLETED: set(),
}


@dataclass(frozen=True, slots=True)
class ResearchCampaignStateV1:
    campaign_id: str
    status: ResearchCampaignStatusV1
    generation: int
    updated_at: str
    champion_experiment_id: str | None = None
    last_experiment_id: str | None = None
    reason: str | None = None
    resume_status: ResearchCampaignStatusV1 | None = None
    pending_strategy_decision_fingerprint: str | None = None

    def __post_init__(self) -> None:
        safe_id(self.campaign_id, "campaign_id")
        if not isinstance(self.status, ResearchCampaignStatusV1):
            raise ValueError("status must be ResearchCampaignStatusV1")
        object.__setattr__(self, "generation", non_negative_int(self.generation, "generation"))
        object.__setattr__(self, "updated_at", timestamp(self.updated_at, "updated_at"))
        object.__setattr__(
            self,
            "champion_experiment_id",
            optional_safe_id(self.champion_experiment_id, "champion_experiment_id"),
        )
        object.__setattr__(
            self,
            "last_experiment_id",
            optional_safe_id(self.last_experiment_id, "last_experiment_id"),
        )
        object.__setattr__(
            self,
            "pending_strategy_decision_fingerprint",
            optional_sha256_hex(
                self.pending_strategy_decision_fingerprint,
                "pending_strategy_decision_fingerprint",
            ),
        )
        object.__setattr__(self, "reason", optional_text(self.reason, "reason", max_length=32_000))
        waiting = self.status in {ResearchCampaignStatusV1.WAITING_FOR_INPUT, ResearchCampaignStatusV1.WAITING_FOR_RESOURCE}
        if waiting:
            if not isinstance(self.resume_status, ResearchCampaignStatusV1):
                raise ValueError("waiting campaign state requires resume_status")
            if self.resume_status in _TERMINAL_CAMPAIGN_STATUSES or self.resume_status in {
                ResearchCampaignStatusV1.CREATED,
                ResearchCampaignStatusV1.WAITING_FOR_INPUT,
                ResearchCampaignStatusV1.WAITING_FOR_RESOURCE,
            }:
                raise ValueError("waiting campaign resume_status must be an active non-waiting phase")
        elif self.resume_status is not None:
            raise ValueError("non-waiting campaign state cannot contain resume_status")
        if self.status in _TERMINAL_CAMPAIGN_STATUSES and self.reason is None:
            raise ValueError("terminal campaign state requires reason")

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL_CAMPAIGN_STATUSES

    def transition(
        self,
        status: ResearchCampaignStatusV1,
        *,
        updated_at: str,
        champion_experiment_id: str | None = None,
        last_experiment_id: str | None = None,
        reason: str | None = None,
        pending_strategy_decision_fingerprint: str | None = None,
        clear_pending_strategy_decision: bool = False,
    ) -> "ResearchCampaignStateV1":
        if clear_pending_strategy_decision and pending_strategy_decision_fingerprint is not None:
            raise ValueError(
                "cannot set and clear pending_strategy_decision_fingerprint together"
            )
        if self.is_terminal:
            raise ValueError(f"terminal campaign state cannot transition: {self.status.value}")
        if status not in _ALLOWED_TRANSITIONS[self.status]:
            raise ValueError(f"invalid campaign transition {self.status.value}->{status.value}")
        waiting_target = status in {ResearchCampaignStatusV1.WAITING_FOR_INPUT, ResearchCampaignStatusV1.WAITING_FOR_RESOURCE}
        waiting_current = self.status in {ResearchCampaignStatusV1.WAITING_FOR_INPUT, ResearchCampaignStatusV1.WAITING_FOR_RESOURCE}
        if waiting_target:
            resume_status = self.resume_status if waiting_current else self.status
        else:
            if waiting_current and status is not ResearchCampaignStatusV1.BUDGET_EXHAUSTED and status is not self.resume_status:
                raise ValueError(
                    f"waiting campaign must resume {self.resume_status.value}, not {status.value}"
                )
            resume_status = None
        return ResearchCampaignStateV1(
            campaign_id=self.campaign_id,
            status=status,
            generation=self.generation + 1,
            updated_at=updated_at,
            champion_experiment_id=(
                self.champion_experiment_id if champion_experiment_id is None else champion_experiment_id
            ),
            last_experiment_id=self.last_experiment_id if last_experiment_id is None else last_experiment_id,
            reason=reason,
            resume_status=resume_status,
            pending_strategy_decision_fingerprint=(
                None
                if clear_pending_strategy_decision
                else self.pending_strategy_decision_fingerprint
                if pending_strategy_decision_fingerprint is None
                else pending_strategy_decision_fingerprint
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "schema_version": 1,
            "campaign_id": self.campaign_id,
            "status": self.status.value,
            "generation": self.generation,
            "updated_at": self.updated_at,
            "champion_experiment_id": self.champion_experiment_id,
            "last_experiment_id": self.last_experiment_id,
            "reason": self.reason,
            "resume_status": None if self.resume_status is None else self.resume_status.value,
        }
        # Preserve exact legacy payload shape until a Phase23.1 strategy cycle
        # actually owns a durable decision reference.
        if self.pending_strategy_decision_fingerprint is not None:
            payload["pending_strategy_decision_fingerprint"] = (
                self.pending_strategy_decision_fingerprint
            )
        return payload

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ResearchCampaignStateV1":
        if raw.get("schema_version") != 1:
            raise ValueError("ResearchCampaignStateV1 schema_version must be 1")
        return cls(
            campaign_id=str(raw["campaign_id"]),
            status=ResearchCampaignStatusV1(str(raw["status"])),
            generation=int(raw["generation"]),
            updated_at=str(raw["updated_at"]),
            champion_experiment_id=(
                None if raw.get("champion_experiment_id") is None else str(raw["champion_experiment_id"])
            ),
            last_experiment_id=None if raw.get("last_experiment_id") is None else str(raw["last_experiment_id"]),
            reason=None if raw.get("reason") is None else str(raw["reason"]),
            resume_status=(
                None if raw.get("resume_status") is None else ResearchCampaignStatusV1(str(raw["resume_status"]))
            ),
            pending_strategy_decision_fingerprint=(
                None
                if raw.get("pending_strategy_decision_fingerprint") is None
                else str(raw["pending_strategy_decision_fingerprint"])
            ),
        )


@dataclass(frozen=True, slots=True)
class ResearchBudgetUsageV1:
    experiment_count: int = 0
    terminal_count: int = 0
    metric_calls: int = 0
    gpu_seconds: float = 0.0
    api_cost: float = 0.0
    external_research_rounds: int = 0
    consecutive_crashes: int = 0
    no_improvement_count: int = 0

    def __post_init__(self) -> None:
        for name in (
            "experiment_count",
            "terminal_count",
            "metric_calls",
            "external_research_rounds",
            "consecutive_crashes",
            "no_improvement_count",
        ):
            object.__setattr__(self, name, non_negative_int(getattr(self, name), name))
        object.__setattr__(self, "gpu_seconds", non_negative_number(self.gpu_seconds, "gpu_seconds"))
        object.__setattr__(self, "api_cost", non_negative_number(self.api_cost, "api_cost"))


@dataclass(frozen=True, slots=True)
class CampaignWaitV1:
    status: ResearchCampaignStatusV1
    request_ids: tuple[str, ...]
    reason: str

    def __post_init__(self) -> None:
        if self.status not in {
            ResearchCampaignStatusV1.WAITING_FOR_INPUT,
            ResearchCampaignStatusV1.WAITING_FOR_RESOURCE,
        }:
            raise ValueError("CampaignWaitV1 status must be a waiting state")
        ids = tuple(safe_id(item, "request_ids") for item in self.request_ids)
        if not ids or len(set(ids)) != len(ids):
            raise ValueError("request_ids must be non-empty and unique")
        object.__setattr__(self, "request_ids", ids)
        object.__setattr__(self, "reason", text(self.reason, "reason", max_length=32_000))

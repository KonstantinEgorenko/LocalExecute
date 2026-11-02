"""Ports owned by the Research Campaign controller, not by execution runtimes."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Protocol

from .campaign import CampaignWaitV1, ResearchBudgetUsageV1, ResearchCampaignStateV1
from .contracts import ResearchContractV1
from .decisions import SearchDecisionV1
from .models import ExperimentFailureKindV1, ExperimentSpecV1
from .search_context import ResearchSearchContextV1
from .serialization import (
    finite_number,
    freeze_json,
    git_commit,
    non_negative_number,
    optional_text,
    sha256_hex,
    timestamp,
    timestamp_value,
    unique_text,
)
from .store import ResearchCampaignSummaryV1, StoredExperimentV1


@dataclass(frozen=True, slots=True)
class ResearchCampaignContextV1:
    state: ResearchCampaignStateV1
    contract: ResearchContractV1
    summary: ResearchCampaignSummaryV1
    usage: ResearchBudgetUsageV1
    champion: StoredExperimentV1 | None = None
    recent_experiments: tuple[StoredExperimentV1, ...] = ()
    known_experiment_fingerprints: frozenset[str] = frozenset()
    research_search_context: ResearchSearchContextV1 | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.known_experiment_fingerprints, frozenset):
            raise ValueError("known_experiment_fingerprints must be frozenset")
        for fingerprint in self.known_experiment_fingerprints:
            sha256_hex(fingerprint, "known_experiment_fingerprints item")
        if self.research_search_context is not None:
            if not isinstance(self.research_search_context, ResearchSearchContextV1):
                raise ValueError("research_search_context must be ResearchSearchContextV1")
            if self.research_search_context.campaign_id != self.state.campaign_id:
                raise ValueError("research_search_context campaign_id must match campaign state")


class ExperimentExecutionStatusV1(StrEnum):
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CRASHED = "CRASHED"
    PRUNED = "PRUNED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True, slots=True)
class ExperimentExecutionOutcomeV1:
    status: ExperimentExecutionStatusV1
    started_at: str
    ended_at: str
    verification_outcome: str | None = None
    artifact_refs: tuple[str, ...] = ()
    stdout_ref: str | None = None
    stderr_ref: str | None = None
    candidate_commit: str | None = None
    telemetry: Mapping[str, Any] = field(default_factory=dict)
    failure_kind: ExperimentFailureKindV1 | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, ExperimentExecutionStatusV1):
            raise ValueError("status must be ExperimentExecutionStatusV1")
        object.__setattr__(self, "started_at", timestamp(self.started_at, "started_at"))
        object.__setattr__(self, "ended_at", timestamp(self.ended_at, "ended_at"))
        if timestamp_value(self.ended_at) < timestamp_value(self.started_at):
            raise ValueError("execution ended_at must not precede started_at")
        object.__setattr__(
            self,
            "verification_outcome",
            optional_text(self.verification_outcome, "verification_outcome", max_length=1_000),
        )
        object.__setattr__(self, "artifact_refs", unique_text(self.artifact_refs, "artifact_refs", maximum=1024))
        object.__setattr__(self, "stdout_ref", optional_text(self.stdout_ref, "stdout_ref", max_length=8_000))
        object.__setattr__(self, "stderr_ref", optional_text(self.stderr_ref, "stderr_ref", max_length=8_000))
        if self.candidate_commit is not None:
            object.__setattr__(self, "candidate_commit", git_commit(self.candidate_commit, "candidate_commit"))
        frozen = freeze_json(self.telemetry, "telemetry")
        if not isinstance(frozen, Mapping):
            raise ValueError("telemetry must be mapping")
        object.__setattr__(self, "telemetry", frozen)
        failed = self.status in {ExperimentExecutionStatusV1.FAILED, ExperimentExecutionStatusV1.CRASHED}
        if failed and self.failure_kind is None:
            raise ValueError(f"{self.status.value} execution outcome requires failure_kind")
        if not failed and self.failure_kind is not None:
            raise ValueError(f"{self.status.value} execution outcome cannot contain failure_kind")


class ExperimentEvaluationStatusV1(StrEnum):
    SCORED = "SCORED"
    INVALID = "INVALID"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class ExperimentEvaluationV1:
    status: ExperimentEvaluationStatusV1
    primary_metric: float | None = None
    secondary_metrics: Mapping[str, float] = field(default_factory=dict)
    metric_variance: float | None = None
    telemetry: Mapping[str, Any] = field(default_factory=dict)
    failure_kind: ExperimentFailureKindV1 | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, ExperimentEvaluationStatusV1):
            raise ValueError("status must be ExperimentEvaluationStatusV1")
        if self.primary_metric is not None:
            object.__setattr__(self, "primary_metric", finite_number(self.primary_metric, "primary_metric"))
        if not isinstance(self.secondary_metrics, Mapping):
            raise ValueError("secondary_metrics must be mapping")
        secondary: dict[str, float] = {}
        for key, value in self.secondary_metrics.items():
            if not isinstance(key, str) or not key:
                raise ValueError("secondary metric names must be non-empty strings")
            secondary[key] = finite_number(value, f"secondary_metrics.{key}")
        object.__setattr__(self, "secondary_metrics", freeze_json(secondary, "secondary_metrics"))
        if self.metric_variance is not None:
            object.__setattr__(self, "metric_variance", non_negative_number(self.metric_variance, "metric_variance"))
        telemetry = freeze_json(self.telemetry, "telemetry")
        if not isinstance(telemetry, Mapping):
            raise ValueError("telemetry must be mapping")
        object.__setattr__(self, "telemetry", telemetry)
        if self.status is ExperimentEvaluationStatusV1.SCORED:
            if self.primary_metric is None:
                raise ValueError("SCORED evaluation requires primary_metric")
            if self.failure_kind is not None:
                raise ValueError("SCORED evaluation cannot contain failure_kind")
        else:
            if self.failure_kind is None:
                raise ValueError(f"{self.status.value} evaluation requires failure_kind")


@dataclass(frozen=True, slots=True)
class ExperimentObservationV1:
    spec: ExperimentSpecV1
    execution: ExperimentExecutionOutcomeV1
    evaluation: ExperimentEvaluationV1 | None


class SearchPolicyPort(Protocol):
    def propose(self, context: ResearchCampaignContextV1) -> ExperimentSpecV1 | None: ...

    def observe(
        self,
        context: ResearchCampaignContextV1,
        observation: ExperimentObservationV1,
    ) -> SearchDecisionV1: ...


class ExperimentExecutionPort(Protocol):
    def execute(self, spec: ExperimentSpecV1, *, resume: bool) -> ExperimentExecutionOutcomeV1: ...


class EvaluationPort(Protocol):
    def evaluate(
        self,
        spec: ExperimentSpecV1,
        execution: ExperimentExecutionOutcomeV1,
    ) -> ExperimentEvaluationV1: ...


class CampaignWaitPort(Protocol):
    def pending(self, campaign_id: str) -> CampaignWaitV1 | None: ...

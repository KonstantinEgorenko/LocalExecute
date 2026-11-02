'research campaign.R1 immutable research and evaluation contracts.'
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping

from .serialization import (
    freeze_json,
    non_negative_number,
    optional_sha256_hex,
    plain,
    positive_int,
    safe_id,
    sha256_hex,
    stable_sha256,
    text,
    timestamp,
    unique_text,
)


class MetricDirectionV1(StrEnum):
    MAXIMIZE = "MAXIMIZE"
    MINIMIZE = "MINIMIZE"


@dataclass(frozen=True, slots=True)
class ResearchBudgetV1:
    """Hard campaign budget envelope.

    ``wall_deadline`` is authoritative wall-clock termination.  The remaining
    counters are independent ceilings; zero cost/GPU budget is valid for CPU or
    fully local campaigns.
    """

    wall_deadline: str
    max_experiments: int
    max_gpu_seconds: float
    max_api_cost: float
    max_metric_calls: int
    max_external_research_rounds: int
    max_consecutive_crashes: int
    max_no_improvement_window: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "wall_deadline", timestamp(self.wall_deadline, "wall_deadline"))
        object.__setattr__(self, "max_experiments", positive_int(self.max_experiments, "max_experiments"))
        object.__setattr__(self, "max_gpu_seconds", non_negative_number(self.max_gpu_seconds, "max_gpu_seconds"))
        object.__setattr__(self, "max_api_cost", non_negative_number(self.max_api_cost, "max_api_cost"))
        object.__setattr__(self, "max_metric_calls", positive_int(self.max_metric_calls, "max_metric_calls"))
        object.__setattr__(
            self,
            "max_external_research_rounds",
            positive_int(self.max_external_research_rounds, "max_external_research_rounds"),
        )
        object.__setattr__(
            self, "max_consecutive_crashes", positive_int(self.max_consecutive_crashes, "max_consecutive_crashes")
        )
        object.__setattr__(
            self,
            "max_no_improvement_window",
            positive_int(self.max_no_improvement_window, "max_no_improvement_window"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "wall_deadline": self.wall_deadline,
            "max_experiments": self.max_experiments,
            "max_gpu_seconds": self.max_gpu_seconds,
            "max_api_cost": self.max_api_cost,
            "max_metric_calls": self.max_metric_calls,
            "max_external_research_rounds": self.max_external_research_rounds,
            "max_consecutive_crashes": self.max_consecutive_crashes,
            "max_no_improvement_window": self.max_no_improvement_window,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ResearchBudgetV1":
        if raw.get("schema_version") != 1:
            raise ValueError("ResearchBudgetV1 schema_version must be 1")
        return cls(
            wall_deadline=str(raw["wall_deadline"]),
            max_experiments=int(raw["max_experiments"]),
            max_gpu_seconds=float(raw["max_gpu_seconds"]),
            max_api_cost=float(raw["max_api_cost"]),
            max_metric_calls=int(raw["max_metric_calls"]),
            max_external_research_rounds=int(raw["max_external_research_rounds"]),
            max_consecutive_crashes=int(raw["max_consecutive_crashes"]),
            max_no_improvement_window=int(raw["max_no_improvement_window"]),
        )


@dataclass(frozen=True, slots=True)
class EvaluationContractV1:
    metric_name: str
    metric_version: str
    direction: MetricDirectionV1
    dataset_fingerprint: str
    split_fingerprint: str
    scorer_fingerprint: str
    aggregation: str
    seed_policy: Mapping[str, Any]
    repetitions: int
    integrity_rules: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        text(self.metric_name, "metric_name", max_length=512)
        text(self.metric_version, "metric_version", max_length=256)
        if not isinstance(self.direction, MetricDirectionV1):
            raise ValueError("direction must be MetricDirectionV1")
        object.__setattr__(self, "dataset_fingerprint", sha256_hex(self.dataset_fingerprint, "dataset_fingerprint"))
        object.__setattr__(self, "split_fingerprint", sha256_hex(self.split_fingerprint, "split_fingerprint"))
        object.__setattr__(self, "scorer_fingerprint", sha256_hex(self.scorer_fingerprint, "scorer_fingerprint"))
        text(self.aggregation, "aggregation", max_length=256)
        frozen_seed = freeze_json(self.seed_policy, "seed_policy")
        frozen_integrity = freeze_json(self.integrity_rules, "integrity_rules")
        if not isinstance(frozen_seed, Mapping) or not isinstance(frozen_integrity, Mapping):
            raise ValueError("seed_policy and integrity_rules must be mappings")
        object.__setattr__(self, "seed_policy", frozen_seed)
        object.__setattr__(self, "integrity_rules", frozen_integrity)
        object.__setattr__(self, "repetitions", positive_int(self.repetitions, "repetitions"))

    @property
    def contract_hash(self) -> str:
        return stable_sha256(self._identity_dict())

    def _identity_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "metric_name": self.metric_name,
            "metric_version": self.metric_version,
            "direction": self.direction.value,
            "dataset_fingerprint": self.dataset_fingerprint,
            "split_fingerprint": self.split_fingerprint,
            "scorer_fingerprint": self.scorer_fingerprint,
            "aggregation": self.aggregation,
            "seed_policy": plain(self.seed_policy),
            "repetitions": self.repetitions,
            "integrity_rules": plain(self.integrity_rules),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._identity_dict(), "contract_hash": self.contract_hash}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "EvaluationContractV1":
        if raw.get("schema_version") != 1:
            raise ValueError("EvaluationContractV1 schema_version must be 1")
        result = cls(
            metric_name=str(raw["metric_name"]),
            metric_version=str(raw["metric_version"]),
            direction=MetricDirectionV1(str(raw["direction"])),
            dataset_fingerprint=str(raw["dataset_fingerprint"]),
            split_fingerprint=str(raw["split_fingerprint"]),
            scorer_fingerprint=str(raw["scorer_fingerprint"]),
            aggregation=str(raw["aggregation"]),
            seed_policy=raw["seed_policy"],
            repetitions=int(raw["repetitions"]),
            integrity_rules=raw.get("integrity_rules", {}),
        )
        expected = raw.get("contract_hash")
        if expected is not None and expected != result.contract_hash:
            raise ValueError("EvaluationContractV1 contract_hash mismatch")
        return result


@dataclass(frozen=True, slots=True)
class ResearchContractV1:
    campaign_id: str
    objective: str
    primary_metric: str
    metric_direction: MetricDirectionV1
    budget: ResearchBudgetV1
    created_at: str
    secondary_metrics: tuple[str, ...] = ()
    constraints: Mapping[str, Any] = field(default_factory=dict)
    dataset_refs: tuple[str, ...] = ()
    data_split_policy: Mapping[str, Any] = field(default_factory=dict)
    allowed_models: tuple[str, ...] = ()
    allowed_mutation_scope: tuple[str, ...] = ()
    forbidden_mutation_scope: tuple[str, ...] = ()
    external_research_policy: Mapping[str, Any] = field(default_factory=dict)
    human_escalation_policy: Mapping[str, Any] = field(default_factory=dict)
    completion_policy: Mapping[str, Any] = field(default_factory=dict)
    contract_version: int = 1
    amends_contract_hash: str | None = None

    def __post_init__(self) -> None:
        safe_id(self.campaign_id, "campaign_id")
        text(self.objective, "objective", max_length=100_000)
        text(self.primary_metric, "primary_metric", max_length=512)
        if not isinstance(self.metric_direction, MetricDirectionV1):
            raise ValueError("metric_direction must be MetricDirectionV1")
        if not isinstance(self.budget, ResearchBudgetV1):
            raise ValueError("budget must be ResearchBudgetV1")
        object.__setattr__(self, "created_at", timestamp(self.created_at, "created_at"))
        secondary = unique_text(self.secondary_metrics, "secondary_metrics", maximum=64, item_max_length=512)
        if self.primary_metric in secondary:
            raise ValueError("primary_metric must not be repeated in secondary_metrics")
        object.__setattr__(self, "secondary_metrics", secondary)
        object.__setattr__(self, "dataset_refs", unique_text(self.dataset_refs, "dataset_refs", maximum=256))
        object.__setattr__(self, "allowed_models", unique_text(self.allowed_models, "allowed_models", maximum=256))
        object.__setattr__(
            self, "allowed_mutation_scope", unique_text(self.allowed_mutation_scope, "allowed_mutation_scope", maximum=256)
        )
        object.__setattr__(
            self,
            "forbidden_mutation_scope",
            unique_text(self.forbidden_mutation_scope, "forbidden_mutation_scope", maximum=256),
        )
        for name in (
            "constraints",
            "data_split_policy",
            "external_research_policy",
            "human_escalation_policy",
            "completion_policy",
        ):
            frozen = freeze_json(getattr(self, name), name)
            if not isinstance(frozen, Mapping):
                raise ValueError(f"{name} must be mapping")
            object.__setattr__(self, name, frozen)
        version = positive_int(self.contract_version, "contract_version")
        object.__setattr__(self, "contract_version", version)
        amended = optional_sha256_hex(self.amends_contract_hash, "amends_contract_hash")
        if version == 1 and amended is not None:
            raise ValueError("contract_version=1 cannot amend another contract")
        if version > 1 and amended is None:
            raise ValueError("contract_version>1 requires amends_contract_hash")
        object.__setattr__(self, "amends_contract_hash", amended)

    @property
    def contract_hash(self) -> str:
        return stable_sha256(self._identity_dict())

    def _identity_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "campaign_id": self.campaign_id,
            "objective": self.objective,
            "primary_metric": self.primary_metric,
            "metric_direction": self.metric_direction.value,
            "budget": self.budget.to_dict(),
            "created_at": self.created_at,
            "secondary_metrics": list(self.secondary_metrics),
            "constraints": plain(self.constraints),
            "dataset_refs": list(self.dataset_refs),
            "data_split_policy": plain(self.data_split_policy),
            "allowed_models": list(self.allowed_models),
            "allowed_mutation_scope": list(self.allowed_mutation_scope),
            "forbidden_mutation_scope": list(self.forbidden_mutation_scope),
            "external_research_policy": plain(self.external_research_policy),
            "human_escalation_policy": plain(self.human_escalation_policy),
            "completion_policy": plain(self.completion_policy),
            "contract_version": self.contract_version,
            "amends_contract_hash": self.amends_contract_hash,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._identity_dict(), "contract_hash": self.contract_hash}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ResearchContractV1":
        if raw.get("schema_version") != 1:
            raise ValueError("ResearchContractV1 schema_version must be 1")
        result = cls(
            campaign_id=str(raw["campaign_id"]),
            objective=str(raw["objective"]),
            primary_metric=str(raw["primary_metric"]),
            metric_direction=MetricDirectionV1(str(raw["metric_direction"])),
            budget=ResearchBudgetV1.from_dict(raw["budget"]),
            created_at=str(raw["created_at"]),
            secondary_metrics=tuple(raw.get("secondary_metrics", ())),
            constraints=raw.get("constraints", {}),
            dataset_refs=tuple(raw.get("dataset_refs", ())),
            data_split_policy=raw.get("data_split_policy", {}),
            allowed_models=tuple(raw.get("allowed_models", ())),
            allowed_mutation_scope=tuple(raw.get("allowed_mutation_scope", ())),
            forbidden_mutation_scope=tuple(raw.get("forbidden_mutation_scope", ())),
            external_research_policy=raw.get("external_research_policy", {}),
            human_escalation_policy=raw.get("human_escalation_policy", {}),
            completion_policy=raw.get("completion_policy", {}),
            contract_version=int(raw.get("contract_version", 1)),
            amends_contract_hash=None if raw.get("amends_contract_hash") is None else str(raw["amends_contract_hash"]),
        )
        expected = raw.get("contract_hash")
        if expected is not None and expected != result.contract_hash:
            raise ValueError("ResearchContractV1 contract_hash mismatch")
        return result

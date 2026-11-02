'research campaign.R1 immutable hypothesis, experiment and insight models.'
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping

from .decisions import SearchDecisionActionV1
from .serialization import (
    finite_number,
    freeze_json,
    git_commit,
    non_negative_int,
    non_negative_number,
    optional_safe_id,
    optional_sha256_hex,
    optional_text,
    optional_timestamp,
    plain,
    safe_id,
    sha256_hex,
    stable_sha256,
    text,
    timestamp,
    timestamp_value,
    unique_ids,
    unique_text,
)


class HypothesisModeV1(StrEnum):
    RESEARCH_QUALITY = "RESEARCH_QUALITY"
    ENUMERATIVE = "ENUMERATIVE"


class HypothesisStatusV1(StrEnum):
    PROPOSED = "PROPOSED"
    ACTIVE = "ACTIVE"
    SUPPORTED = "SUPPORTED"
    REJECTED = "REJECTED"
    INCONCLUSIVE = "INCONCLUSIVE"
    PRUNED = "PRUNED"


class ExperimentStatusV1(StrEnum):
    PLANNED = "PLANNED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CRASHED = "CRASHED"
    PRUNED = "PRUNED"
    INVALID = "INVALID"
    CANCELLED = "CANCELLED"


class ExperimentFailureKindV1(StrEnum):
    IMPLEMENTATION_BUG = "IMPLEMENTATION_BUG"
    TRAINING_CRASH = "TRAINING_CRASH"
    OOM = "OOM"
    TIMEOUT = "TIMEOUT"
    INVALID_METRIC = "INVALID_METRIC"
    DATA_ERROR = "DATA_ERROR"
    EVALUATION_FAILED = "EVALUATION_FAILED"
    RESOURCE_UNAVAILABLE = "RESOURCE_UNAVAILABLE"
    PERMISSION_REQUIRED = "PERMISSION_REQUIRED"
    NO_IMPROVEMENT = "NO_IMPROVEMENT"


@dataclass(frozen=True, slots=True)
class HypothesisV1:
    hypothesis_id: str
    campaign_id: str
    statement: str
    mechanism: str
    predicted_effect: str
    created_by: str
    created_at: str
    mode: HypothesisModeV1 = HypothesisModeV1.RESEARCH_QUALITY
    falsifier: str | None = None
    parent_hypothesis_ids: tuple[str, ...] = ()
    strategy_dimensions: Mapping[str, Any] = field(default_factory=dict)
    evidence_refs: tuple[str, ...] = ()
    status: HypothesisStatusV1 = HypothesisStatusV1.PROPOSED

    def __post_init__(self) -> None:
        safe_id(self.hypothesis_id, "hypothesis_id")
        safe_id(self.campaign_id, "campaign_id")
        text(self.statement, "statement", max_length=32_000)
        text(self.mechanism, "mechanism", max_length=32_000)
        text(self.predicted_effect, "predicted_effect", max_length=16_000)
        text(self.created_by, "created_by", max_length=4_000)
        object.__setattr__(self, "created_at", timestamp(self.created_at, "created_at"))
        if not isinstance(self.mode, HypothesisModeV1):
            raise ValueError("mode must be HypothesisModeV1")
        falsifier = optional_text(self.falsifier, "falsifier", max_length=32_000)
        if self.mode is HypothesisModeV1.RESEARCH_QUALITY and falsifier is None:
            raise ValueError("RESEARCH_QUALITY hypothesis requires falsifier")
        object.__setattr__(self, "falsifier", falsifier)
        parents = unique_ids(self.parent_hypothesis_ids, "parent_hypothesis_ids", maximum=64)
        if self.hypothesis_id in parents:
            raise ValueError("hypothesis cannot be its own parent")
        object.__setattr__(self, "parent_hypothesis_ids", parents)
        frozen = freeze_json(self.strategy_dimensions, "strategy_dimensions")
        if not isinstance(frozen, Mapping):
            raise ValueError("strategy_dimensions must be mapping")
        object.__setattr__(self, "strategy_dimensions", frozen)
        object.__setattr__(self, "evidence_refs", unique_text(self.evidence_refs, "evidence_refs", maximum=256))
        if not isinstance(self.status, HypothesisStatusV1):
            raise ValueError("status must be HypothesisStatusV1")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "hypothesis_id": self.hypothesis_id,
            "campaign_id": self.campaign_id,
            "parent_hypothesis_ids": list(self.parent_hypothesis_ids),
            "statement": self.statement,
            "mechanism": self.mechanism,
            "predicted_effect": self.predicted_effect,
            "falsifier": self.falsifier,
            "strategy_dimensions": plain(self.strategy_dimensions),
            "evidence_refs": list(self.evidence_refs),
            "created_by": self.created_by,
            "created_at": self.created_at,
            "mode": self.mode.value,
            "status": self.status.value,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "HypothesisV1":
        if raw.get("schema_version") != 1:
            raise ValueError("HypothesisV1 schema_version must be 1")
        return cls(
            hypothesis_id=str(raw["hypothesis_id"]),
            campaign_id=str(raw["campaign_id"]),
            parent_hypothesis_ids=tuple(raw.get("parent_hypothesis_ids", ())),
            statement=str(raw["statement"]),
            mechanism=str(raw["mechanism"]),
            predicted_effect=str(raw["predicted_effect"]),
            falsifier=None if raw.get("falsifier") is None else str(raw["falsifier"]),
            strategy_dimensions=raw.get("strategy_dimensions", {}),
            evidence_refs=tuple(raw.get("evidence_refs", ())),
            created_by=str(raw["created_by"]),
            created_at=str(raw["created_at"]),
            mode=HypothesisModeV1(str(raw.get("mode", HypothesisModeV1.RESEARCH_QUALITY.value))),
            status=HypothesisStatusV1(str(raw.get("status", HypothesisStatusV1.PROPOSED.value))),
        )


@dataclass(frozen=True, slots=True)
class ExperimentSpecV1:
    experiment_id: str
    campaign_id: str
    hypothesis_id: str
    strategy_id: str
    code_base_commit: str
    dataset_fingerprint: str
    evaluation_contract_hash: str
    seed: int
    created_at: str
    parameters: Mapping[str, Any] = field(default_factory=dict)
    parent_experiment_ids: tuple[str, ...] = ()
    model_id: str | None = None
    provider: str | None = None
    model_version: str | None = None
    prompt_hash: str | None = None
    sampling_config: Mapping[str, Any] = field(default_factory=dict)
    tool_policy_hash: str | None = None
    candidate_patch_ref: str | None = None
    resource_budget: Mapping[str, Any] = field(default_factory=dict)
    execution: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        safe_id(self.experiment_id, "experiment_id")
        safe_id(self.campaign_id, "campaign_id")
        safe_id(self.hypothesis_id, "hypothesis_id")
        safe_id(self.strategy_id, "strategy_id")
        object.__setattr__(self, "code_base_commit", git_commit(self.code_base_commit, "code_base_commit"))
        object.__setattr__(self, "dataset_fingerprint", sha256_hex(self.dataset_fingerprint, "dataset_fingerprint"))
        object.__setattr__(
            self, "evaluation_contract_hash", sha256_hex(self.evaluation_contract_hash, "evaluation_contract_hash")
        )
        object.__setattr__(self, "seed", non_negative_int(self.seed, "seed"))
        object.__setattr__(self, "created_at", timestamp(self.created_at, "created_at"))
        parents = unique_ids(self.parent_experiment_ids, "parent_experiment_ids", maximum=64)
        if self.experiment_id in parents:
            raise ValueError("experiment cannot be its own parent")
        object.__setattr__(self, "parent_experiment_ids", parents)
        for name in ("model_id", "provider", "model_version"):
            object.__setattr__(self, name, optional_text(getattr(self, name), name, max_length=4_000))
        object.__setattr__(self, "prompt_hash", optional_sha256_hex(self.prompt_hash, "prompt_hash"))
        object.__setattr__(self, "tool_policy_hash", optional_sha256_hex(self.tool_policy_hash, "tool_policy_hash"))
        object.__setattr__(
            self, "candidate_patch_ref", optional_text(self.candidate_patch_ref, "candidate_patch_ref", max_length=8_000)
        )
        for name in ("parameters", "sampling_config", "resource_budget", "execution"):
            frozen = freeze_json(getattr(self, name), name)
            if not isinstance(frozen, Mapping):
                raise ValueError(f"{name} must be mapping")
            object.__setattr__(self, name, frozen)

    @property
    def fingerprint(self) -> str:
        """Logical experiment identity used for restart-safe deduplication.

        IDs, ancestry and creation time are deliberately excluded.  The actual
        strategy/model/code/data/evaluator/seed/resource/execution configuration
        determines whether work has already been attempted.
        """

        return stable_sha256(
            {
                "schema_version": 1,
                "strategy_id": self.strategy_id,
                "parameters": plain(self.parameters),
                "model_id": self.model_id,
                "provider": self.provider,
                "model_version": self.model_version,
                "prompt_hash": self.prompt_hash,
                "sampling_config": plain(self.sampling_config),
                "tool_policy_hash": self.tool_policy_hash,
                "code_base_commit": self.code_base_commit,
                "candidate_patch_ref": self.candidate_patch_ref,
                "dataset_fingerprint": self.dataset_fingerprint,
                "evaluation_contract_hash": self.evaluation_contract_hash,
                "seed": self.seed,
                "resource_budget": plain(self.resource_budget),
                "execution": plain(self.execution),
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "experiment_id": self.experiment_id,
            "campaign_id": self.campaign_id,
            "hypothesis_id": self.hypothesis_id,
            "parent_experiment_ids": list(self.parent_experiment_ids),
            "strategy_id": self.strategy_id,
            "parameters": plain(self.parameters),
            "model_id": self.model_id,
            "provider": self.provider,
            "model_version": self.model_version,
            "prompt_hash": self.prompt_hash,
            "sampling_config": plain(self.sampling_config),
            "tool_policy_hash": self.tool_policy_hash,
            "code_base_commit": self.code_base_commit,
            "candidate_patch_ref": self.candidate_patch_ref,
            "dataset_fingerprint": self.dataset_fingerprint,
            "evaluation_contract_hash": self.evaluation_contract_hash,
            "seed": self.seed,
            "resource_budget": plain(self.resource_budget),
            "execution": plain(self.execution),
            "created_at": self.created_at,
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ExperimentSpecV1":
        if raw.get("schema_version") != 1:
            raise ValueError("ExperimentSpecV1 schema_version must be 1")
        result = cls(
            experiment_id=str(raw["experiment_id"]),
            campaign_id=str(raw["campaign_id"]),
            hypothesis_id=str(raw["hypothesis_id"]),
            parent_experiment_ids=tuple(raw.get("parent_experiment_ids", ())),
            strategy_id=str(raw["strategy_id"]),
            parameters=raw.get("parameters", {}),
            model_id=None if raw.get("model_id") is None else str(raw["model_id"]),
            provider=None if raw.get("provider") is None else str(raw["provider"]),
            model_version=None if raw.get("model_version") is None else str(raw["model_version"]),
            prompt_hash=None if raw.get("prompt_hash") is None else str(raw["prompt_hash"]),
            sampling_config=raw.get("sampling_config", {}),
            tool_policy_hash=None if raw.get("tool_policy_hash") is None else str(raw["tool_policy_hash"]),
            code_base_commit=str(raw["code_base_commit"]),
            candidate_patch_ref=None if raw.get("candidate_patch_ref") is None else str(raw["candidate_patch_ref"]),
            dataset_fingerprint=str(raw["dataset_fingerprint"]),
            evaluation_contract_hash=str(raw["evaluation_contract_hash"]),
            seed=int(raw["seed"]),
            resource_budget=raw.get("resource_budget", {}),
            execution=raw.get("execution", {}),
            created_at=str(raw["created_at"]),
        )
        expected = raw.get("fingerprint")
        if expected is not None and expected != result.fingerprint:
            raise ValueError("ExperimentSpecV1 fingerprint mismatch")
        return result


@dataclass(frozen=True, slots=True)
class ExperimentResultV1:
    experiment_id: str
    campaign_id: str
    status: ExperimentStatusV1
    started_at: str | None = None
    ended_at: str | None = None
    primary_metric: float | None = None
    secondary_metrics: Mapping[str, float] = field(default_factory=dict)
    metric_variance: float | None = None
    verification_outcome: str | None = None
    artifact_refs: tuple[str, ...] = ()
    stdout_ref: str | None = None
    stderr_ref: str | None = None
    candidate_commit: str | None = None
    telemetry: Mapping[str, Any] = field(default_factory=dict)
    failure_kind: ExperimentFailureKindV1 | None = None
    decision: SearchDecisionActionV1 | None = None

    def __post_init__(self) -> None:
        safe_id(self.experiment_id, "experiment_id")
        safe_id(self.campaign_id, "campaign_id")
        if not isinstance(self.status, ExperimentStatusV1):
            raise ValueError("status must be ExperimentStatusV1")
        started = optional_timestamp(self.started_at, "started_at")
        ended = optional_timestamp(self.ended_at, "ended_at")
        object.__setattr__(self, "started_at", started)
        object.__setattr__(self, "ended_at", ended)
        metric = None if self.primary_metric is None else finite_number(self.primary_metric, "primary_metric")
        object.__setattr__(self, "primary_metric", metric)
        if not isinstance(self.secondary_metrics, Mapping):
            raise ValueError("secondary_metrics must be mapping")
        frozen_secondary: dict[str, float] = {}
        for key, value in self.secondary_metrics.items():
            text(key, "secondary_metrics key", max_length=512)
            frozen_secondary[key] = finite_number(value, f"secondary_metrics.{key}")
        object.__setattr__(self, "secondary_metrics", freeze_json(frozen_secondary, "secondary_metrics"))
        variance = None if self.metric_variance is None else non_negative_number(self.metric_variance, "metric_variance")
        object.__setattr__(self, "metric_variance", variance)
        object.__setattr__(
            self, "verification_outcome", optional_text(self.verification_outcome, "verification_outcome", max_length=1_000)
        )
        object.__setattr__(self, "artifact_refs", unique_text(self.artifact_refs, "artifact_refs", maximum=1024))
        object.__setattr__(self, "stdout_ref", optional_text(self.stdout_ref, "stdout_ref", max_length=8_000))
        object.__setattr__(self, "stderr_ref", optional_text(self.stderr_ref, "stderr_ref", max_length=8_000))
        if self.candidate_commit is not None:
            object.__setattr__(self, "candidate_commit", git_commit(self.candidate_commit, "candidate_commit"))
        frozen_telemetry = freeze_json(self.telemetry, "telemetry")
        if not isinstance(frozen_telemetry, Mapping):
            raise ValueError("telemetry must be mapping")
        object.__setattr__(self, "telemetry", frozen_telemetry)
        if self.failure_kind is not None and not isinstance(self.failure_kind, ExperimentFailureKindV1):
            raise ValueError("failure_kind must be ExperimentFailureKindV1")
        if self.decision is not None and not isinstance(self.decision, SearchDecisionActionV1):
            raise ValueError("decision must be SearchDecisionActionV1")

        terminal = self.status not in {ExperimentStatusV1.PLANNED, ExperimentStatusV1.RUNNING}
        if self.status is ExperimentStatusV1.PLANNED:
            if started is not None or ended is not None or metric is not None or self.failure_kind is not None or self.decision is not None:
                raise ValueError("PLANNED result cannot contain execution outcome")
        elif self.status is ExperimentStatusV1.RUNNING:
            if started is None or ended is not None or self.decision is not None:
                raise ValueError("RUNNING result requires started_at and no terminal fields")
        elif terminal:
            if started is None or ended is None:
                raise ValueError("terminal result requires started_at and ended_at")
            if timestamp_value(ended) < timestamp_value(started):
                raise ValueError("ended_at must not precede started_at")
            if self.decision is None:
                raise ValueError("terminal result requires decision")

        if self.status is ExperimentStatusV1.SUCCEEDED:
            if metric is None:
                raise ValueError("SUCCEEDED result requires primary_metric")
            if self.failure_kind is not None:
                raise ValueError("SUCCEEDED result cannot contain failure_kind")
        if self.status in {ExperimentStatusV1.FAILED, ExperimentStatusV1.CRASHED, ExperimentStatusV1.INVALID}:
            if self.failure_kind is None:
                raise ValueError(f"{self.status.value} result requires failure_kind")

    @property
    def is_terminal(self) -> bool:
        return self.status not in {ExperimentStatusV1.PLANNED, ExperimentStatusV1.RUNNING}

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "experiment_id": self.experiment_id,
            "campaign_id": self.campaign_id,
            "status": self.status.value,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "primary_metric": self.primary_metric,
            "secondary_metrics": plain(self.secondary_metrics),
            "metric_variance": self.metric_variance,
            "verification_outcome": self.verification_outcome,
            "artifact_refs": list(self.artifact_refs),
            "stdout_ref": self.stdout_ref,
            "stderr_ref": self.stderr_ref,
            "candidate_commit": self.candidate_commit,
            "telemetry": plain(self.telemetry),
            "failure_kind": None if self.failure_kind is None else self.failure_kind.value,
            "decision": None if self.decision is None else self.decision.value,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ExperimentResultV1":
        if raw.get("schema_version") != 1:
            raise ValueError("ExperimentResultV1 schema_version must be 1")
        failure = raw.get("failure_kind")
        decision = raw.get("decision")
        return cls(
            experiment_id=str(raw["experiment_id"]),
            campaign_id=str(raw["campaign_id"]),
            status=ExperimentStatusV1(str(raw["status"])),
            started_at=None if raw.get("started_at") is None else str(raw["started_at"]),
            ended_at=None if raw.get("ended_at") is None else str(raw["ended_at"]),
            primary_metric=raw.get("primary_metric"),
            secondary_metrics=raw.get("secondary_metrics", {}),
            metric_variance=raw.get("metric_variance"),
            verification_outcome=(None if raw.get("verification_outcome") is None else str(raw["verification_outcome"])),
            artifact_refs=tuple(raw.get("artifact_refs", ())),
            stdout_ref=None if raw.get("stdout_ref") is None else str(raw["stdout_ref"]),
            stderr_ref=None if raw.get("stderr_ref") is None else str(raw["stderr_ref"]),
            candidate_commit=None if raw.get("candidate_commit") is None else str(raw["candidate_commit"]),
            telemetry=raw.get("telemetry", {}),
            failure_kind=None if failure is None else ExperimentFailureKindV1(str(failure)),
            decision=None if decision is None else SearchDecisionActionV1(str(decision)),
        )


@dataclass(frozen=True, slots=True)
class ResearchInsightV1:
    insight_id: str
    campaign_id: str
    statement: str
    confidence: float
    scope: str
    created_at: str
    support_experiment_ids: tuple[str, ...] = ()
    contradicting_experiment_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        safe_id(self.insight_id, "insight_id")
        safe_id(self.campaign_id, "campaign_id")
        text(self.statement, "statement", max_length=32_000)
        confidence = finite_number(self.confidence, "confidence")
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("confidence must be in [0,1]")
        object.__setattr__(self, "confidence", confidence)
        text(self.scope, "scope", max_length=8_000)
        object.__setattr__(self, "created_at", timestamp(self.created_at, "created_at"))
        support = unique_ids(self.support_experiment_ids, "support_experiment_ids", maximum=1024)
        contradicting = unique_ids(self.contradicting_experiment_ids, "contradicting_experiment_ids", maximum=1024)
        if set(support).intersection(contradicting):
            raise ValueError("same experiment cannot both support and contradict an insight")
        if not support and not contradicting:
            raise ValueError("ResearchInsightV1 requires supporting or contradicting experiment evidence")
        object.__setattr__(self, "support_experiment_ids", support)
        object.__setattr__(self, "contradicting_experiment_ids", contradicting)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "insight_id": self.insight_id,
            "campaign_id": self.campaign_id,
            "statement": self.statement,
            "support_experiment_ids": list(self.support_experiment_ids),
            "contradicting_experiment_ids": list(self.contradicting_experiment_ids),
            "confidence": self.confidence,
            "scope": self.scope,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ResearchInsightV1":
        if raw.get("schema_version") != 1:
            raise ValueError("ResearchInsightV1 schema_version must be 1")
        return cls(
            insight_id=str(raw["insight_id"]),
            campaign_id=str(raw["campaign_id"]),
            statement=str(raw["statement"]),
            support_experiment_ids=tuple(raw.get("support_experiment_ids", ())),
            contradicting_experiment_ids=tuple(raw.get("contradicting_experiment_ids", ())),
            confidence=float(raw["confidence"]),
            scope=str(raw["scope"]),
            created_at=str(raw["created_at"]),
        )

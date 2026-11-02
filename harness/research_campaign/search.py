"""Deterministic finite strategy search for Phase 22.R4.

The enumerative policy is intentionally stateless.  Durable experiment fingerprints
from the ResearchCampaignContext are the checkpoint: after restart the policy
reconstructs the same ordered finite search space and skips every logical experiment
already present in the research ledger.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence

from .contracts import MetricDirectionV1
from .decisions import SearchDecisionActionV1, SearchDecisionV1
from .models import ExperimentSpecV1, ExperimentStatusV1
from .ports import (
    ExperimentEvaluationStatusV1,
    ExperimentExecutionStatusV1,
    ExperimentObservationV1,
    ResearchCampaignContextV1,
)
from .serialization import (
    canonical_json,
    finite_number,
    freeze_json,
    git_commit,
    non_negative_int,
    non_negative_number,
    optional_sha256_hex,
    optional_text,
    plain,
    safe_id,
    sha256_hex,
    stable_sha256,
    timestamp,
    unique_ids,
)


class SearchPolicyError(RuntimeError):
    pass


class StrategyDimensionTargetV1(StrEnum):
    PARAMETER = "PARAMETER"
    MODEL_ID = "MODEL_ID"
    PROVIDER = "PROVIDER"
    MODEL_VERSION = "MODEL_VERSION"
    PROMPT_HASH = "PROMPT_HASH"
    SAMPLING_CONFIG = "SAMPLING_CONFIG"
    TOOL_POLICY_HASH = "TOOL_POLICY_HASH"
    SEED = "SEED"
    RESOURCE_BUDGET = "RESOURCE_BUDGET"
    EXECUTION = "EXECUTION"
    STRATEGY_ID = "STRATEGY_ID"


_MAPPING_TARGETS = {
    StrategyDimensionTargetV1.PARAMETER,
    StrategyDimensionTargetV1.SAMPLING_CONFIG,
    StrategyDimensionTargetV1.RESOURCE_BUDGET,
    StrategyDimensionTargetV1.EXECUTION,
}


@dataclass(frozen=True, slots=True)
class StrategyChoiceV1:
    value: Any
    priority: int = 0
    estimated_cost: float = 0.0

    def __post_init__(self) -> None:
        frozen = freeze_json(self.value, "value")
        object.__setattr__(self, "value", frozen)
        if not isinstance(self.priority, int) or isinstance(self.priority, bool):
            raise ValueError("priority must be integer")
        object.__setattr__(self, "estimated_cost", non_negative_number(self.estimated_cost, "estimated_cost"))

    @property
    def identity(self) -> str:
        return canonical_json(self.value)


@dataclass(frozen=True, slots=True)
class StrategyDimensionV1:
    name: str
    target: StrategyDimensionTargetV1
    choices: tuple[StrategyChoiceV1, ...]
    key: str | None = None

    def __post_init__(self) -> None:
        safe_id(self.name, "name")
        if not isinstance(self.target, StrategyDimensionTargetV1):
            raise ValueError("target must be StrategyDimensionTargetV1")
        if not isinstance(self.choices, tuple) or not self.choices:
            raise ValueError("choices must be a non-empty tuple")
        if len(self.choices) > 10_000:
            raise ValueError("choices exceeds maximum=10000")
        if any(not isinstance(choice, StrategyChoiceV1) for choice in self.choices):
            raise ValueError("choices must contain StrategyChoiceV1")
        identities = tuple(choice.identity for choice in self.choices)
        if len(set(identities)) != len(identities):
            raise ValueError("choices must not contain duplicate logical values")
        if self.target in _MAPPING_TARGETS:
            if self.key is None:
                raise ValueError(f"{self.target.value} dimension requires key")
            safe_id(self.key, "key")
        elif self.key is not None:
            raise ValueError(f"{self.target.value} dimension must not define key")


@dataclass(frozen=True, slots=True)
class StrategyExclusionV1:
    """Exclude a partial exact assignment when every listed dimension matches."""

    matches: Mapping[str, Any]
    reason: str = "excluded by strategy constraint"

    def __post_init__(self) -> None:
        if not isinstance(self.matches, Mapping) or not self.matches:
            raise ValueError("matches must be a non-empty mapping")
        normalized: dict[str, Any] = {}
        for key, value in self.matches.items():
            safe_id(key, "matches key")
            normalized[key] = freeze_json(value, f"matches.{key}")
        object.__setattr__(self, "matches", MappingProxyType(normalized))
        if not isinstance(self.reason, str) or not self.reason.strip() or len(self.reason) > 4_000:
            raise ValueError("reason must be non-empty string <= 4000 chars")

    @property
    def identity(self) -> str:
        return canonical_json(self.matches)

    def excludes(self, assignment: Mapping[str, Any]) -> bool:
        return all(key in assignment and canonical_json(assignment[key]) == canonical_json(value) for key, value in self.matches.items())


@dataclass(frozen=True, slots=True)
class StrategyCandidateV1:
    assignment: Mapping[str, Any]
    total_priority: int
    estimated_cost: float

    def __post_init__(self) -> None:
        frozen = freeze_json(self.assignment, "assignment")
        if not isinstance(frozen, Mapping):
            raise ValueError("assignment must be mapping")
        object.__setattr__(self, "assignment", frozen)
        if not isinstance(self.total_priority, int) or isinstance(self.total_priority, bool):
            raise ValueError("total_priority must be integer")
        object.__setattr__(self, "estimated_cost", non_negative_number(self.estimated_cost, "estimated_cost"))

    @property
    def canonical_assignment(self) -> str:
        return canonical_json(self.assignment)


@dataclass(frozen=True, slots=True)
class StrategySpaceV1:
    dimensions: tuple[StrategyDimensionV1, ...]
    exclusions: tuple[StrategyExclusionV1, ...] = ()
    max_candidates: int = 100_000

    def __post_init__(self) -> None:
        if not isinstance(self.dimensions, tuple) or not self.dimensions:
            raise ValueError("dimensions must be a non-empty tuple")
        if len(self.dimensions) > 64:
            raise ValueError("dimensions exceeds maximum=64")
        if any(not isinstance(dimension, StrategyDimensionV1) for dimension in self.dimensions):
            raise ValueError("dimensions must contain StrategyDimensionV1")
        names = tuple(dimension.name for dimension in self.dimensions)
        if len(set(names)) != len(names):
            raise ValueError("dimension names must be unique")
        if any(not isinstance(item, StrategyExclusionV1) for item in self.exclusions):
            raise ValueError("exclusions must contain StrategyExclusionV1")
        exclusion_ids = tuple(item.identity for item in self.exclusions)
        if len(set(exclusion_ids)) != len(exclusion_ids):
            raise ValueError("duplicate strategy exclusions are not allowed")
        known_names = set(names)
        for exclusion in self.exclusions:
            unknown = set(exclusion.matches) - known_names
            if unknown:
                raise ValueError(f"strategy exclusion references unknown dimensions: {sorted(unknown)}")
        if not isinstance(self.max_candidates, int) or isinstance(self.max_candidates, bool) or self.max_candidates <= 0:
            raise ValueError("max_candidates must be positive integer")
        product = 1
        for dimension in self.dimensions:
            product *= len(dimension.choices)
            if product > self.max_candidates:
                raise ValueError(
                    f"raw strategy Cartesian product {product} exceeds max_candidates={self.max_candidates}"
                )

    @property
    def raw_candidate_count(self) -> int:
        product = 1
        for dimension in self.dimensions:
            product *= len(dimension.choices)
        return product

    def candidates(self) -> tuple[StrategyCandidateV1, ...]:
        candidates: list[StrategyCandidateV1] = []
        for choices in itertools.product(*(dimension.choices for dimension in self.dimensions)):
            assignment = {
                dimension.name: choice.value
                for dimension, choice in zip(self.dimensions, choices, strict=True)
            }
            if any(exclusion.excludes(assignment) for exclusion in self.exclusions):
                continue
            candidates.append(
                StrategyCandidateV1(
                    assignment=assignment,
                    total_priority=sum(choice.priority for choice in choices),
                    estimated_cost=sum(choice.estimated_cost for choice in choices),
                )
            )
        candidates.sort(key=lambda item: (-item.total_priority, item.estimated_cost, item.canonical_assignment))
        return tuple(candidates)


@dataclass(frozen=True, slots=True)
class ExperimentTemplateV1:
    campaign_id: str
    hypothesis_id: str
    strategy_id: str
    code_base_commit: str
    dataset_fingerprint: str
    evaluation_contract_hash: str
    seed: int = 0
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
    experiment_id_prefix: str = "exp"

    def __post_init__(self) -> None:
        safe_id(self.campaign_id, "campaign_id")
        safe_id(self.hypothesis_id, "hypothesis_id")
        safe_id(self.strategy_id, "strategy_id")
        object.__setattr__(self, "code_base_commit", git_commit(self.code_base_commit, "code_base_commit"))
        object.__setattr__(self, "dataset_fingerprint", sha256_hex(self.dataset_fingerprint, "dataset_fingerprint"))
        object.__setattr__(
            self,
            "evaluation_contract_hash",
            sha256_hex(self.evaluation_contract_hash, "evaluation_contract_hash"),
        )
        object.__setattr__(self, "seed", non_negative_int(self.seed, "seed"))
        object.__setattr__(self, "parent_experiment_ids", unique_ids(self.parent_experiment_ids, "parent_experiment_ids", maximum=64))
        for name in ("model_id", "provider", "model_version"):
            object.__setattr__(self, name, optional_text(getattr(self, name), name, max_length=4_000))
        object.__setattr__(self, "prompt_hash", optional_sha256_hex(self.prompt_hash, "prompt_hash"))
        object.__setattr__(self, "tool_policy_hash", optional_sha256_hex(self.tool_policy_hash, "tool_policy_hash"))
        object.__setattr__(
            self,
            "candidate_patch_ref",
            optional_text(self.candidate_patch_ref, "candidate_patch_ref", max_length=8_000),
        )
        for name in ("parameters", "sampling_config", "resource_budget", "execution"):
            frozen = freeze_json(getattr(self, name), name)
            if not isinstance(frozen, Mapping):
                raise ValueError(f"{name} must be mapping")
            object.__setattr__(self, name, frozen)
        prefix = safe_id(self.experiment_id_prefix, "experiment_id_prefix")
        if len(prefix) > 96:
            raise ValueError("experiment_id_prefix must be <= 96 chars")

    @property
    def identity_hash(self) -> str:
        return stable_sha256(
            {
                "schema_version": 1,
                "campaign_id": self.campaign_id,
                "hypothesis_id": self.hypothesis_id,
                "strategy_id": self.strategy_id,
                "code_base_commit": self.code_base_commit,
                "dataset_fingerprint": self.dataset_fingerprint,
                "evaluation_contract_hash": self.evaluation_contract_hash,
                "seed": self.seed,
                "parameters": plain(self.parameters),
                "parent_experiment_ids": list(self.parent_experiment_ids),
                "model_id": self.model_id,
                "provider": self.provider,
                "model_version": self.model_version,
                "prompt_hash": self.prompt_hash,
                "sampling_config": plain(self.sampling_config),
                "tool_policy_hash": self.tool_policy_hash,
                "candidate_patch_ref": self.candidate_patch_ref,
                "resource_budget": plain(self.resource_budget),
                "execution": plain(self.execution),
                "experiment_id_prefix": self.experiment_id_prefix,
            }
        )


class EnumerativeStrategyPolicyV1:
    """Stateless deterministic finite search over a StrategySpaceV1."""

    def __init__(
        self,
        *,
        space: StrategySpaceV1,
        template: ExperimentTemplateV1,
        clock: Callable[[], str],
        minimum_improvement: float = 0.0,
    ) -> None:
        if not isinstance(space, StrategySpaceV1):
            raise ValueError("space must be StrategySpaceV1")
        if not isinstance(template, ExperimentTemplateV1):
            raise ValueError("template must be ExperimentTemplateV1")
        if not callable(clock):
            raise ValueError("clock must be callable")
        self.space = space
        self.template = template
        self.clock = clock
        self.minimum_improvement = non_negative_number(minimum_improvement, "minimum_improvement")
        self._candidates = space.candidates()

    @property
    def candidate_count(self) -> int:
        return len(self._candidates)

    def propose(self, context: ResearchCampaignContextV1) -> ExperimentSpecV1 | None:
        if context.state.campaign_id != self.template.campaign_id:
            raise SearchPolicyError("strategy template campaign_id does not match campaign context")
        for candidate in self._candidates:
            spec = self._materialize(candidate)
            if context.contract.allowed_models and spec.model_id not in context.contract.allowed_models:
                continue
            if spec.fingerprint in context.known_experiment_fingerprints:
                continue
            return spec
        return None

    def observe(
        self,
        context: ResearchCampaignContextV1,
        observation: ExperimentObservationV1,
    ) -> SearchDecisionV1:
        if observation.spec.campaign_id != context.state.campaign_id:
            raise SearchPolicyError("observation campaign_id mismatch")
        action: SearchDecisionActionV1
        incumbent_id: str | None = None
        reason: str

        if observation.execution.status is not ExperimentExecutionStatusV1.COMPLETED:
            action = SearchDecisionActionV1.DROP
            reason = f"execution ended as {observation.execution.status.value}; retain result but drop candidate"
        elif observation.evaluation is None:
            action = SearchDecisionActionV1.DROP
            reason = "completed execution has no evaluation; retain result but drop candidate"
        elif observation.evaluation.status is not ExperimentEvaluationStatusV1.SCORED:
            action = SearchDecisionActionV1.DROP
            reason = f"evaluation ended as {observation.evaluation.status.value}; retain result but drop candidate"
        elif self._gate_required(context) and not self._gate_admitted(observation):
            action = SearchDecisionActionV1.KEEP
            reason = "candidate is scored but has not passed independent GATE admission"
        elif context.champion is None:
            action = SearchDecisionActionV1.UPDATE_CHAMPION
            reason = "first scored experiment establishes campaign champion"
        elif context.champion.spec.evaluation_contract_hash != observation.spec.evaluation_contract_hash:
            action = SearchDecisionActionV1.KEEP
            reason = "scored experiment uses a different EvaluationContractHash and is retained as incomparable"
        else:
            incumbent_result = context.champion.result
            if (
                incumbent_result is None
                or incumbent_result.status is not ExperimentStatusV1.SUCCEEDED
                or incumbent_result.primary_metric is None
            ):
                raise SearchPolicyError("campaign champion does not have a successful primary metric")
            candidate_metric = observation.evaluation.primary_metric
            assert candidate_metric is not None
            incumbent_metric = incumbent_result.primary_metric
            if self._improves(candidate_metric, incumbent_metric, context.contract.metric_direction):
                action = SearchDecisionActionV1.UPDATE_CHAMPION
                incumbent_id = context.champion.spec.experiment_id
                reason = (
                    f"candidate metric {candidate_metric} improves incumbent {incumbent_metric} "
                    f"for {context.contract.metric_direction.value}"
                )
            else:
                action = SearchDecisionActionV1.DROP
                reason = (
                    f"candidate metric {candidate_metric} does not exceed minimum improvement over "
                    f"incumbent {incumbent_metric}"
                )

        decision_id = f"enum-{stable_sha256({'experiment_id': observation.spec.experiment_id, 'action': action.value})[:24]}"
        return SearchDecisionV1(
            decision_id=decision_id,
            campaign_id=observation.spec.campaign_id,
            experiment_id=observation.spec.experiment_id,
            action=action,
            reason=reason,
            created_at=observation.execution.ended_at,
            incumbent_experiment_id=incumbent_id,
            evidence_experiment_ids=(context.champion.spec.experiment_id,) if context.champion is not None else (),
            metadata={"policy": "EnumerativeStrategyPolicyV1", "minimum_improvement": self.minimum_improvement},
        )

    @staticmethod
    def _gate_required(context: ResearchCampaignContextV1) -> bool:
        raw = context.contract.completion_policy.get("require_gate_for_champion", False)
        if not isinstance(raw, bool):
            raise SearchPolicyError("completion_policy.require_gate_for_champion must be bool")
        return raw

    @staticmethod
    def _gate_admitted(observation: ExperimentObservationV1) -> bool:
        evaluation = observation.evaluation
        if evaluation is None:
            return False
        return (
            evaluation.telemetry.get("integrity_validated") is True
            and evaluation.telemetry.get("admission_passed") is True
            and evaluation.telemetry.get("admission_surface") == "GATE"
        )

    def _improves(self, candidate: float, incumbent: float, direction: MetricDirectionV1) -> bool:
        candidate = finite_number(candidate, "candidate")
        incumbent = finite_number(incumbent, "incumbent")
        if direction is MetricDirectionV1.MAXIMIZE:
            return candidate > incumbent + self.minimum_improvement
        if direction is MetricDirectionV1.MINIMIZE:
            return candidate < incumbent - self.minimum_improvement
        raise SearchPolicyError(f"unsupported metric direction {direction!r}")

    def _materialize(self, candidate: StrategyCandidateV1) -> ExperimentSpecV1:
        parameters = dict(plain(self.template.parameters))
        sampling = dict(plain(self.template.sampling_config))
        resources = dict(plain(self.template.resource_budget))
        execution = dict(plain(self.template.execution))
        scalar: dict[str, Any] = {
            "strategy_id": self.template.strategy_id,
            "model_id": self.template.model_id,
            "provider": self.template.provider,
            "model_version": self.template.model_version,
            "prompt_hash": self.template.prompt_hash,
            "tool_policy_hash": self.template.tool_policy_hash,
            "seed": self.template.seed,
        }
        dimensions = {dimension.name: dimension for dimension in self.space.dimensions}
        for name, value in candidate.assignment.items():
            dimension = dimensions[name]
            target = dimension.target
            if target is StrategyDimensionTargetV1.PARAMETER:
                parameters[dimension.key or ""] = plain(value)
            elif target is StrategyDimensionTargetV1.SAMPLING_CONFIG:
                sampling[dimension.key or ""] = plain(value)
            elif target is StrategyDimensionTargetV1.RESOURCE_BUDGET:
                resources[dimension.key or ""] = plain(value)
            elif target is StrategyDimensionTargetV1.EXECUTION:
                execution[dimension.key or ""] = plain(value)
            elif target is StrategyDimensionTargetV1.MODEL_ID:
                scalar["model_id"] = value
            elif target is StrategyDimensionTargetV1.PROVIDER:
                scalar["provider"] = value
            elif target is StrategyDimensionTargetV1.MODEL_VERSION:
                scalar["model_version"] = value
            elif target is StrategyDimensionTargetV1.PROMPT_HASH:
                scalar["prompt_hash"] = value
            elif target is StrategyDimensionTargetV1.TOOL_POLICY_HASH:
                scalar["tool_policy_hash"] = value
            elif target is StrategyDimensionTargetV1.SEED:
                scalar["seed"] = value
            elif target is StrategyDimensionTargetV1.STRATEGY_ID:
                scalar["strategy_id"] = value
            else:  # pragma: no cover - enum exhaustiveness guard
                raise SearchPolicyError(f"unsupported strategy target {target!r}")

        digest = stable_sha256(
            {
                "template": self.template.identity_hash,
                "assignment": plain(candidate.assignment),
            }
        )
        experiment_id = f"{self.template.experiment_id_prefix}-{digest[:24]}"
        return ExperimentSpecV1(
            experiment_id=experiment_id,
            campaign_id=self.template.campaign_id,
            hypothesis_id=self.template.hypothesis_id,
            parent_experiment_ids=self.template.parent_experiment_ids,
            strategy_id=scalar["strategy_id"],
            parameters=parameters,
            model_id=scalar["model_id"],
            provider=scalar["provider"],
            model_version=scalar["model_version"],
            prompt_hash=scalar["prompt_hash"],
            sampling_config=sampling,
            tool_policy_hash=scalar["tool_policy_hash"],
            code_base_commit=self.template.code_base_commit,
            candidate_patch_ref=self.template.candidate_patch_ref,
            dataset_fingerprint=self.template.dataset_fingerprint,
            evaluation_contract_hash=self.template.evaluation_contract_hash,
            seed=scalar["seed"],
            resource_budget=resources,
            execution=execution,
            created_at=timestamp(self.clock(), "clock"),
        )

'research campaign.R8.1 deterministic bounded multi-proposal generation.'
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .models import ExperimentSpecV1
from .search import StrategyChoiceV1, StrategyDimensionTargetV1, StrategyDimensionV1, StrategySpaceV1
from .serialization import canonical_json, non_negative_int, plain, positive_int, sha256_hex, stable_sha256, unique_text
from .strategy_contracts import (
    ResearchProposalTargetKindV1,
    ResearchProposalV1,
    ResearchStrategyAuthorityV1,
    ResearchStrategyBudgetV1,
)


class ResearchProposalGenerationError(RuntimeError):
    pass


_TARGET_KIND = {
    StrategyDimensionTargetV1.PARAMETER: ResearchProposalTargetKindV1.EXECUTION,
    StrategyDimensionTargetV1.MODEL_ID: ResearchProposalTargetKindV1.MODEL,
    StrategyDimensionTargetV1.PROVIDER: ResearchProposalTargetKindV1.MODEL,
    StrategyDimensionTargetV1.MODEL_VERSION: ResearchProposalTargetKindV1.MODEL,
    StrategyDimensionTargetV1.PROMPT_HASH: ResearchProposalTargetKindV1.PROMPT,
    StrategyDimensionTargetV1.SAMPLING_CONFIG: ResearchProposalTargetKindV1.EXECUTION,
    StrategyDimensionTargetV1.TOOL_POLICY_HASH: ResearchProposalTargetKindV1.TOOL_POLICY,
    StrategyDimensionTargetV1.SEED: ResearchProposalTargetKindV1.EXECUTION,
    StrategyDimensionTargetV1.RESOURCE_BUDGET: ResearchProposalTargetKindV1.EXECUTION,
    StrategyDimensionTargetV1.EXECUTION: ResearchProposalTargetKindV1.EXECUTION,
    StrategyDimensionTargetV1.STRATEGY_ID: ResearchProposalTargetKindV1.EXECUTION,
}


@dataclass(frozen=True, slots=True)
class ResearchProposalGenerationPolicyV1:
    max_proposals: int = 8
    max_dimensions: int = 32
    max_choices_per_dimension: int = 64

    def __post_init__(self) -> None:
        object.__setattr__(self, "max_proposals", positive_int(self.max_proposals, "max_proposals"))
        object.__setattr__(self, "max_dimensions", positive_int(self.max_dimensions, "max_dimensions"))
        object.__setattr__(self, "max_choices_per_dimension", positive_int(self.max_choices_per_dimension, "max_choices_per_dimension"))
        if self.max_proposals > 1000:
            raise ValueError("max_proposals must be <= 1000")
        if self.max_dimensions > 64:
            raise ValueError("max_dimensions must be <= 64")
        if self.max_choices_per_dimension > 10000:
            raise ValueError("max_choices_per_dimension must be <= 10000")


@dataclass(frozen=True, slots=True)
class ResearchProposalGenerationContextV1:
    champion: ExperimentSpecV1
    strategy_space: StrategySpaceV1
    budget_snapshot: ResearchStrategyBudgetV1
    known_novelty_fingerprints: tuple[str, ...] = ()
    rejected_novelty_fingerprints: tuple[str, ...] = ()
    research_memory_provenance_ids: tuple[str, ...] = ()
    generated_proposal_count: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.champion, ExperimentSpecV1):
            raise ValueError("champion must be ExperimentSpecV1")
        if not isinstance(self.strategy_space, StrategySpaceV1):
            raise ValueError("strategy_space must be StrategySpaceV1")
        if not isinstance(self.budget_snapshot, ResearchStrategyBudgetV1):
            raise ValueError("budget_snapshot must be ResearchStrategyBudgetV1")
        object.__setattr__(self, "known_novelty_fingerprints", _unique_sha256(self.known_novelty_fingerprints, "known_novelty_fingerprints"))
        object.__setattr__(self, "rejected_novelty_fingerprints", _unique_sha256(self.rejected_novelty_fingerprints, "rejected_novelty_fingerprints"))
        object.__setattr__(self, "research_memory_provenance_ids", unique_text(self.research_memory_provenance_ids, "research_memory_provenance_ids", maximum=2048, item_max_length=8000))
        object.__setattr__(self, "generated_proposal_count", non_negative_int(self.generated_proposal_count, "generated_proposal_count"))

    @property
    def excluded_novelty_fingerprints(self) -> frozenset[str]:
        return frozenset(self.known_novelty_fingerprints) | frozenset(self.rejected_novelty_fingerprints)

    @property
    def context_fingerprint(self) -> str:
        return stable_sha256({
            "schema_version": 1,
            "champion_fingerprint": self.champion.fingerprint,
            "strategy_space": _strategy_space_identity(self.strategy_space),
            "budget_snapshot": self.budget_snapshot.to_dict(),
            "known_novelty_fingerprints": sorted(self.known_novelty_fingerprints),
            "rejected_novelty_fingerprints": sorted(self.rejected_novelty_fingerprints),
            "research_memory_provenance_ids": list(self.research_memory_provenance_ids),
            "generated_proposal_count": self.generated_proposal_count,
        })


class ResearchProposalGeneratorPort(Protocol):
    def propose(self, context: ResearchProposalGenerationContextV1) -> tuple[ResearchProposalV1, ...]: ...


@dataclass(frozen=True, slots=True)
class DeterministicResearchProposalGeneratorV1:
    policy: ResearchProposalGenerationPolicyV1 = ResearchProposalGenerationPolicyV1()

    def __post_init__(self) -> None:
        if not isinstance(self.policy, ResearchProposalGenerationPolicyV1):
            raise ValueError("policy must be ResearchProposalGenerationPolicyV1")

    def propose(self, context: ResearchProposalGenerationContextV1) -> tuple[ResearchProposalV1, ...]:
        if not isinstance(context, ResearchProposalGenerationContextV1):
            raise TypeError("context must be ResearchProposalGenerationContextV1")
        if context.budget_snapshot.remaining_experiments <= 0:
            return ()
        if context.generated_proposal_count >= self.policy.max_proposals:
            return ()

        limit = self.policy.max_proposals - context.generated_proposal_count
        excluded = set(context.excluded_novelty_fingerprints)
        emitted: set[str] = set()
        proposals: list[ResearchProposalV1] = []

        dimensions = sorted(context.strategy_space.dimensions, key=lambda item: item.name)[: self.policy.max_dimensions]
        for dimension in dimensions:
            choices = sorted(
                dimension.choices,
                key=lambda item: (-item.priority, item.estimated_cost, item.identity),
            )[: self.policy.max_choices_per_dimension]
            for choice in choices:
                if self._is_noop(context.champion, dimension, choice):
                    continue
                assignment = _candidate_assignment(
                    context.champion,
                    context.strategy_space,
                    dimension,
                    choice,
                )
                if any(item.excludes(assignment) for item in context.strategy_space.exclusions):
                    continue
                proposal = self._materialize(context, dimension, choice)
                novelty = proposal.novelty_fingerprint
                if novelty in excluded or novelty in emitted:
                    continue
                emitted.add(novelty)
                proposals.append(proposal)
                if len(proposals) >= limit:
                    return tuple(proposals)
        return tuple(proposals)

    @staticmethod
    def _is_noop(champion: ExperimentSpecV1, dimension: StrategyDimensionV1, choice: StrategyChoiceV1) -> bool:
        current = _current_value(champion, dimension)
        if current is _MISSING:
            return False
        return canonical_json(current) == canonical_json(choice.value)

    @staticmethod
    def _materialize(context: ResearchProposalGenerationContextV1, dimension: StrategyDimensionV1, choice: StrategyChoiceV1) -> ResearchProposalV1:
        champion = context.champion
        target_kind = _TARGET_KIND.get(dimension.target)
        if target_kind is None:
            raise ResearchProposalGenerationError(f"unsupported strategy target {dimension.target.value}")

        key = dimension.key if dimension.key is not None else dimension.target.value.casefold()
        identity = stable_sha256({
            "schema_version": 1,
            "campaign_id": champion.campaign_id,
            "source_strategy_id": champion.strategy_id,
            "parent_experiment_id": champion.experiment_id,
            "target": dimension.target.value,
            "dimension": dimension.name,
            "key": key,
            "value": plain(choice.value),
        })

        return ResearchProposalV1(
            proposal_id=f"r81p-{identity[:24]}",
            campaign_id=champion.campaign_id,
            source_strategy_id=champion.strategy_id,
            parent_experiment_ids=(champion.experiment_id,),
            hypothesis_statement=f"A bounded {dimension.name} variant of strategy {champion.strategy_id} may improve research utility relative to the current champion.",
            mechanism=f"Keep the current champion fixed except for one {dimension.target.value} choice in dimension {dimension.name}.",
            falsifier=f"The proposed {dimension.name} value fails to preserve validity or provide useful evidence under the same campaign.",
            target_kind=target_kind,
            target_dimension=dimension.name,
            target_key=key,
            target_value=choice.value,
            predicted_effect="The one-axis mutation should preserve comparability while providing new evidence about the primary research objective.",
            estimated_cost=choice.estimated_cost,
            estimated_risk=0.0,
            provenance_ids=(f"experiment:{champion.experiment_id}", *context.research_memory_provenance_ids),
            authority=ResearchStrategyAuthorityV1.PROPOSAL_ONLY,
        )


class _Missing:
    pass


_MISSING = _Missing()


def _current_value(champion: ExperimentSpecV1, dimension: StrategyDimensionV1) -> Any:
    target = dimension.target
    key = dimension.key
    if target is StrategyDimensionTargetV1.PARAMETER:
        return champion.parameters.get(key or "", _MISSING)
    if target is StrategyDimensionTargetV1.MODEL_ID:
        return champion.model_id
    if target is StrategyDimensionTargetV1.PROVIDER:
        return champion.provider
    if target is StrategyDimensionTargetV1.MODEL_VERSION:
        return champion.model_version
    if target is StrategyDimensionTargetV1.PROMPT_HASH:
        return champion.prompt_hash
    if target is StrategyDimensionTargetV1.SAMPLING_CONFIG:
        return champion.sampling_config.get(key or "", _MISSING)
    if target is StrategyDimensionTargetV1.TOOL_POLICY_HASH:
        return champion.tool_policy_hash
    if target is StrategyDimensionTargetV1.SEED:
        return champion.seed
    if target is StrategyDimensionTargetV1.RESOURCE_BUDGET:
        return champion.resource_budget.get(key or "", _MISSING)
    if target is StrategyDimensionTargetV1.EXECUTION:
        return champion.execution.get(key or "", _MISSING)
    if target is StrategyDimensionTargetV1.STRATEGY_ID:
        return champion.strategy_id
    raise ResearchProposalGenerationError(f"unsupported strategy target {target.value}")


def _candidate_assignment(
    champion: ExperimentSpecV1,
    space: StrategySpaceV1,
    changed_dimension: StrategyDimensionV1,
    choice: StrategyChoiceV1,
) -> dict[str, Any]:
    assignment: dict[str, Any] = {}
    for dimension in space.dimensions:
        current = _current_value(champion, dimension)
        if current is not _MISSING:
            assignment[dimension.name] = plain(current)
    assignment[changed_dimension.name] = plain(choice.value)
    return assignment


def _unique_sha256(values: object, name: str) -> tuple[str, ...]:
    if not isinstance(values, (tuple, list)):
        raise ValueError(f"{name} must be list/tuple")
    result = tuple(sha256_hex(item, name) for item in values)
    if len(set(result)) != len(result):
        raise ValueError(f"{name} must not contain duplicates")
    if len(result) > 10000:
        raise ValueError(f"{name} exceeds maximum=10000")
    return result


def _strategy_space_identity(space: StrategySpaceV1) -> dict[str, Any]:
    return {
        "dimensions": [
            {
                "name": dimension.name,
                "target": dimension.target.value,
                "key": dimension.key,
                "choices": [
                    {"value": plain(choice.value), "priority": choice.priority, "estimated_cost": choice.estimated_cost}
                    for choice in dimension.choices
                ],
            }
            for dimension in space.dimensions
        ],
        "exclusions": [{"matches": plain(item.matches), "reason": item.reason} for item in space.exclusions],
        "max_candidates": space.max_candidates,
    }

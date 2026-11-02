"""Phase 22.R7.4 deterministic memory-aware adaptation for enumerative search.

R7.3 makes bounded SEARCH_FEEDBACK memory visible to SearchPolicyPort.  R7.4 uses
that context to *rerank* otherwise-valid enumerative candidates.  The first version
is intentionally conservative: influence is typed, strategy-level, bounded, and
model-free.  It never parses free-form insight statements, never consumes TEST, and
never bypasses the existing experiment-fingerprint deduplication boundary.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .insights import ResearchInsightPolarityV1, ResearchInsightSurfaceV1
from .models import ExperimentSpecV1
from .ports import ResearchCampaignContextV1
from .search import (
    EnumerativeStrategyPolicyV1,
    ExperimentTemplateV1,
    SearchPolicyError,
    StrategyCandidateV1,
    StrategySpaceV1,
)
from .search_context import ResearchSearchContextStatusV1, ResearchSearchMemoryItemV1
from .serialization import finite_number, freeze_json, non_negative_number, plain


@dataclass(frozen=True, slots=True)
class MemoryAwareSearchWeightsV1:
    """Bounded surface weights applied to typed strategy-level evidence."""

    dev: float = 1.0
    gate: float = 3.0
    repair: float = 2.0
    minimum_confidence: float = 0.50
    max_absolute_memory_score: float = 4.0

    def __post_init__(self) -> None:
        for name in ("dev", "gate", "repair", "max_absolute_memory_score"):
            value = non_negative_number(getattr(self, name), name)
            object.__setattr__(self, name, value)
        confidence = finite_number(self.minimum_confidence, "minimum_confidence")
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("minimum_confidence must be in [0,1]")
        object.__setattr__(self, "minimum_confidence", confidence)
        if self.max_absolute_memory_score <= 0:
            raise ValueError("max_absolute_memory_score must be > 0")

    def surface_weight(self, surface: ResearchInsightSurfaceV1) -> float:
        if surface is ResearchInsightSurfaceV1.DEV:
            return self.dev
        if surface is ResearchInsightSurfaceV1.GATE:
            return self.gate
        if surface is ResearchInsightSurfaceV1.REPAIR:
            return self.repair
        raise SearchPolicyError(f"unsupported research memory surface {surface!r}")


@dataclass(frozen=True, slots=True)
class MemoryInfluenceContributionV1:
    insight_id: str
    surface: ResearchInsightSurfaceV1
    polarity: ResearchInsightPolarityV1
    confidence: float
    strategy_id: str
    score: float


@dataclass(frozen=True, slots=True)
class MemoryAwareCandidateRankV1:
    experiment_id: str
    strategy_id: str
    assignment: Mapping[str, Any]
    base_priority: int
    estimated_cost: float
    memory_score: float
    adjusted_priority: float
    contributions: tuple[MemoryInfluenceContributionV1, ...]

    def __post_init__(self) -> None:
        frozen = freeze_json(self.assignment, "assignment")
        if not isinstance(frozen, Mapping):
            raise ValueError("assignment must be mapping")
        object.__setattr__(self, "assignment", frozen)
        if not isinstance(self.base_priority, int) or isinstance(self.base_priority, bool):
            raise ValueError("base_priority must be integer")
        object.__setattr__(self, "estimated_cost", non_negative_number(self.estimated_cost, "estimated_cost"))
        object.__setattr__(self, "memory_score", finite_number(self.memory_score, "memory_score"))
        object.__setattr__(self, "adjusted_priority", finite_number(self.adjusted_priority, "adjusted_priority"))


class MemoryAwareEnumerativePolicyV1(EnumerativeStrategyPolicyV1):
    """Enumerative policy with deterministic bounded strategy-memory reranking.

    Existing candidate validity, allowed-model checks, ExperimentSpec identity and
    known-fingerprint dedup are inherited unchanged.  Memory only changes ordering
    among still-eligible candidates.  If search context is absent, empty, ambiguous,
    cross-contract, or has no typed evidence for a candidate strategy, base ordering
    is preserved.
    """

    def __init__(
        self,
        *,
        space: StrategySpaceV1,
        template: ExperimentTemplateV1,
        clock: Callable[[], str],
        minimum_improvement: float = 0.0,
        memory_weights: MemoryAwareSearchWeightsV1 = MemoryAwareSearchWeightsV1(),
    ) -> None:
        super().__init__(
            space=space,
            template=template,
            clock=clock,
            minimum_improvement=minimum_improvement,
        )
        if not isinstance(memory_weights, MemoryAwareSearchWeightsV1):
            raise ValueError("memory_weights must be MemoryAwareSearchWeightsV1")
        self.memory_weights = memory_weights

    def propose(self, context: ResearchCampaignContextV1) -> ExperimentSpecV1 | None:
        ranked = self.rank_candidates(context)
        if not ranked:
            return None
        selected_id = ranked[0].experiment_id
        for candidate in self._candidates:
            spec = self._materialize(candidate)
            if spec.experiment_id == selected_id:
                return spec
        raise SearchPolicyError("memory-aware ranking selected unknown experiment")

    def rank_candidates(self, context: ResearchCampaignContextV1) -> tuple[MemoryAwareCandidateRankV1, ...]:
        if context.state.campaign_id != self.template.campaign_id:
            raise SearchPolicyError("strategy template campaign_id does not match campaign context")

        eligible: list[tuple[StrategyCandidateV1, ExperimentSpecV1]] = []
        for candidate in self._candidates:
            spec = self._materialize(candidate)
            if context.contract.allowed_models and spec.model_id not in context.contract.allowed_models:
                continue
            if spec.fingerprint in context.known_experiment_fingerprints:
                continue
            eligible.append((candidate, spec))

        search_context = context.research_search_context
        memory_ready = (
            search_context is not None
            and search_context.status is ResearchSearchContextStatusV1.READY
            and search_context.evaluation_contract_hash == self.template.evaluation_contract_hash
        )
        items = () if not memory_ready or search_context is None else search_context.items

        ranked: list[MemoryAwareCandidateRankV1] = []
        for candidate, spec in eligible:
            contributions = self._strategy_contributions(spec.strategy_id, items)
            raw_score = sum(item.score for item in contributions)
            cap = self.memory_weights.max_absolute_memory_score
            memory_score = max(-cap, min(cap, raw_score))
            adjusted_priority = float(candidate.total_priority) + memory_score
            ranked.append(
                MemoryAwareCandidateRankV1(
                    experiment_id=spec.experiment_id,
                    strategy_id=spec.strategy_id,
                    assignment=plain(candidate.assignment),
                    base_priority=candidate.total_priority,
                    estimated_cost=candidate.estimated_cost,
                    memory_score=memory_score,
                    adjusted_priority=adjusted_priority,
                    contributions=contributions,
                )
            )

        ranked.sort(
            key=lambda item: (
                -item.adjusted_priority,
                item.estimated_cost,
                self._canonical_assignment(item.assignment),
            )
        )
        return tuple(ranked)

    @staticmethod
    def _canonical_assignment(assignment: Mapping[str, Any]) -> str:
        from .serialization import canonical_json

        return canonical_json(assignment)

    def _strategy_contributions(
        self,
        strategy_id: str,
        items: tuple[ResearchSearchMemoryItemV1, ...],
    ) -> tuple[MemoryInfluenceContributionV1, ...]:
        contributions: list[MemoryInfluenceContributionV1] = []
        for item in items:
            # R7.4 V1 never parses statements or subject keys.  Only explicit typed
            # strategy lineage may affect ordering.
            if item.strategy_id != strategy_id:
                continue
            if item.polarity is ResearchInsightPolarityV1.NEUTRAL:
                continue
            if item.direct_influence_score is not None:
                # R7.5G belief-derived items already encode consolidated evidence
                # strength.  Do not re-threshold on belief confidence or reapply
                # DEV/GATE/REPAIR weights; either would distort the consolidated net.
                score = item.direct_influence_score
                if score == 0.0:
                    continue
            else:
                if item.confidence < self.memory_weights.minimum_confidence:
                    continue
                direction = 1.0 if item.polarity is ResearchInsightPolarityV1.SUPPORTING else -1.0
                score = direction * self.memory_weights.surface_weight(item.surface) * item.confidence
            contributions.append(
                MemoryInfluenceContributionV1(
                    insight_id=item.insight_id,
                    surface=item.surface,
                    polarity=item.polarity,
                    confidence=item.confidence,
                    strategy_id=strategy_id,
                    score=score,
                )
            )
        contributions.sort(key=lambda item: item.insight_id)
        return tuple(contributions)

'research campaign.R7.5G projection of consolidated research beliefs into search context.\n\nR7.5 keeps immutable typed insights as the audit ledger and derives lineage/scope-aware\nResearchBeliefV1 records.  This adapter is the cutover seam: only CURRENT actionable\nbeliefs become ResearchSearchContextV1 items.  Each belief contributes exactly one\nconsolidated direct influence score, so superseded raw evidence is never counted twice\nby R7.4 MemoryAwareEnumerativePolicyV1.\n'
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .beliefs import (
    ResearchBeliefApplicabilityV1,
    ResearchBeliefEvidenceStateV1,
    ResearchBeliefSetV1,
    ResearchBeliefStatusV1,
    ResearchBeliefV1,
)
from .insights import (
    ResearchInsightKindV1,
    ResearchInsightPolarityV1,
    ResearchInsightSurfaceV1,
)
from .search_context import (
    ResearchSearchContextBudgetV1,
    ResearchSearchContextStatusV1,
    ResearchSearchContextV1,
    ResearchSearchMemoryItemV1,
)
from .serialization import optional_safe_id, safe_id


class ResearchBeliefSearchContextProjectorPort(Protocol):
    def project(
        self,
        *,
        belief_set: ResearchBeliefSetV1,
        focus_experiment_id: str | None = None,
        focus_strategy_id: str | None = None,
    ) -> ResearchSearchContextV1: ...


@dataclass(frozen=True, slots=True)
class DeterministicResearchBeliefSearchContextProjectorV1:
    """Bounded model-free adapter from active beliefs to the existing policy context."""

    budget: ResearchSearchContextBudgetV1 = ResearchSearchContextBudgetV1()

    def __post_init__(self) -> None:
        if not isinstance(self.budget, ResearchSearchContextBudgetV1):
            raise ValueError("budget must be ResearchSearchContextBudgetV1")

    @staticmethod
    def _dominant_surface(belief: ResearchBeliefV1) -> ResearchInsightSurfaceV1:
        active = [item for item in belief.evidence if item.state is ResearchBeliefEvidenceStateV1.ACTIVE]
        if not active:
            return ResearchInsightSurfaceV1.DEV
        rank = {
            ResearchInsightSurfaceV1.GATE: 0,
            ResearchInsightSurfaceV1.REPAIR: 1,
            ResearchInsightSurfaceV1.DEV: 2,
        }
        active.sort(key=lambda item: (-item.weight, rank[item.surface], item.insight_id))
        return active[0].surface

    @staticmethod
    def _kind(belief: ResearchBeliefV1, surface: ResearchInsightSurfaceV1) -> ResearchInsightKindV1:
        if belief.status is ResearchBeliefStatusV1.SUPPORTED:
            return {
                ResearchInsightSurfaceV1.GATE: ResearchInsightKindV1.GATE_CONFIRMED,
                ResearchInsightSurfaceV1.REPAIR: ResearchInsightKindV1.REPAIR_SUCCEEDED,
                ResearchInsightSurfaceV1.DEV: ResearchInsightKindV1.DEV_SUCCEEDED,
            }[surface]
        if belief.status is ResearchBeliefStatusV1.CONTRADICTED:
            return {
                ResearchInsightSurfaceV1.GATE: ResearchInsightKindV1.GATE_REJECTED,
                ResearchInsightSurfaceV1.REPAIR: ResearchInsightKindV1.REPAIR_FAILED,
                ResearchInsightSurfaceV1.DEV: ResearchInsightKindV1.DEV_NO_IMPROVEMENT,
            }[surface]
        return ResearchInsightKindV1.DEV_SUCCEEDED

    @staticmethod
    def _polarity(status: ResearchBeliefStatusV1) -> ResearchInsightPolarityV1:
        if status is ResearchBeliefStatusV1.SUPPORTED:
            return ResearchInsightPolarityV1.SUPPORTING
        if status is ResearchBeliefStatusV1.CONTRADICTED:
            return ResearchInsightPolarityV1.CONTRADICTING
        return ResearchInsightPolarityV1.NEUTRAL

    @staticmethod
    def _statement(belief: ResearchBeliefV1) -> str:
        return (
            f"Consolidated belief for strategy {belief.strategy_id}: status={belief.status.value}, "
            f"support={belief.support_weight:.3f}, contradiction={belief.contradiction_weight:.3f}, "
            f"net={belief.net_strength:+.3f}, confidence={belief.confidence:.3f}."
        )

    def project(
        self,
        *,
        belief_set: ResearchBeliefSetV1,
        focus_experiment_id: str | None = None,
        focus_strategy_id: str | None = None,
    ) -> ResearchSearchContextV1:
        if not isinstance(belief_set, ResearchBeliefSetV1):
            raise TypeError("belief_set must be ResearchBeliefSetV1")
        campaign_id = safe_id(belief_set.campaign_id, "campaign_id")
        focus_experiment_id = optional_safe_id(focus_experiment_id, "focus_experiment_id")
        focus_strategy_id = optional_safe_id(focus_strategy_id, "focus_strategy_id")

        current = [
            item
            for item in belief_set.beliefs
            if item.applicability is ResearchBeliefApplicabilityV1.CURRENT
        ]
        non_current_count = len(belief_set.beliefs) - len(current)
        excluded_statuses = {ResearchBeliefStatusV1.DIAGNOSTIC_ONLY, ResearchBeliefStatusV1.SUPERSEDED}
        diagnostic_or_superseded = [item for item in current if item.status in excluded_statuses]
        actionable = [item for item in current if item.status not in excluded_statuses]

        if not actionable:
            contract_hash = None
            if current:
                hashes = {item.scope.evaluation_contract_hash for item in current}
                if len(hashes) == 1:
                    contract_hash = next(iter(hashes))
            return ResearchSearchContextV1(
                campaign_id=campaign_id,
                status=ResearchSearchContextStatusV1.EMPTY,
                items=(),
                max_items=self.budget.max_items,
                max_chars=self.budget.max_chars,
                chars_per_estimated_token=self.budget.chars_per_estimated_token,
                source_insight_count=len(belief_set.beliefs),
                actionable_insight_count=0,
                diagnostic_only_excluded_count=len(belief_set.beliefs),
                cross_contract_excluded_count=0,
                omitted_by_budget_count=0,
                evaluation_contract_hash=contract_hash,
                focus_experiment_id=focus_experiment_id,
                focus_hypothesis_id=None,
                focus_strategy_id=focus_strategy_id,
            )

        hashes = {item.scope.evaluation_contract_hash for item in actionable}
        if len(hashes) != 1:
            raise ValueError("CURRENT actionable beliefs must share one EvaluationContractHash")
        contract_hash = next(iter(hashes))

        ranked = sorted(
            actionable,
            key=lambda item: (
                0 if focus_strategy_id is not None and item.strategy_id == focus_strategy_id else 1,
                -abs(item.net_strength),
                -item.confidence,
                item.belief_id,
            ),
        )
        projected: list[ResearchSearchMemoryItemV1] = []
        omitted = 0
        current_chars = 0
        for belief in ranked:
            if len(projected) >= self.budget.max_items:
                omitted += 1
                continue
            surface = self._dominant_surface(belief)
            item = ResearchSearchMemoryItemV1(
                insight_id=belief.belief_id,
                kind=self._kind(belief, surface),
                polarity=self._polarity(belief.status),
                surface=surface,
                statement=self._statement(belief),
                subject_key=belief.subject_key,
                evaluation_contract_hash=belief.scope.evaluation_contract_hash,
                confidence=belief.confidence,
                created_at=belief.latest_evidence_at,
                evidence_source_ids=belief.active_insight_ids or tuple(
                    evidence.insight_id for evidence in belief.evidence
                ),
                hypothesis_id=None,
                strategy_id=belief.strategy_id,
                relevance_tier=0 if focus_strategy_id == belief.strategy_id else 3,
                # This is already the consolidated, surface-weighted belief signal.
                # R7.4 must not multiply it by DEV/GATE/REPAIR weights a second time.
                direct_influence_score=belief.net_strength,
            )
            rendered = item.render()
            added_chars = len(rendered) + (1 if projected else 0)
            if current_chars + added_chars > self.budget.max_chars:
                omitted += 1
                continue
            projected.append(item)
            current_chars += added_chars

        if not projected:
            return ResearchSearchContextV1(
                campaign_id=campaign_id,
                status=ResearchSearchContextStatusV1.EMPTY,
                items=(),
                max_items=self.budget.max_items,
                max_chars=self.budget.max_chars,
                chars_per_estimated_token=self.budget.chars_per_estimated_token,
                source_insight_count=len(belief_set.beliefs),
                actionable_insight_count=len(actionable),
                diagnostic_only_excluded_count=len(diagnostic_or_superseded) + non_current_count,
                cross_contract_excluded_count=0,
                omitted_by_budget_count=len(actionable),
                evaluation_contract_hash=contract_hash,
                focus_experiment_id=focus_experiment_id,
                focus_hypothesis_id=None,
                focus_strategy_id=focus_strategy_id,
            )

        return ResearchSearchContextV1(
            campaign_id=campaign_id,
            status=ResearchSearchContextStatusV1.READY,
            items=tuple(projected),
            max_items=self.budget.max_items,
            max_chars=self.budget.max_chars,
            chars_per_estimated_token=self.budget.chars_per_estimated_token,
            source_insight_count=len(belief_set.beliefs),
            actionable_insight_count=len(actionable),
            diagnostic_only_excluded_count=len(diagnostic_or_superseded) + non_current_count,
            cross_contract_excluded_count=0,
            omitted_by_budget_count=omitted,
            evaluation_contract_hash=contract_hash,
            focus_experiment_id=focus_experiment_id,
            focus_hypothesis_id=None,
            focus_strategy_id=focus_strategy_id,
        )

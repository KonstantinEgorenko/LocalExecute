'Durable research stopping evidence/model inputs composed into the existing R8 planner.'
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable

from .evidence import ResearchEvidenceBudgetV1, ResearchEvidenceAcquisitionPort
from .model_proposals import (
    ModelBackedResearchProposalGeneratorV1, ResearchProposalCohortRecordV1,
)
from .serialization import stable_sha256
from .strategy_cycle import (
    DeterministicResearchStrategyPlannerV1, ResearchStrategyPlanningContextV1,
    ResearchStrategyPlanningResultV1,
)
from .strategy_proposals import ResearchProposalGenerationContextV1


@dataclass(frozen=True, slots=True)
class ReplayProposalGeneratorV1:
    proposals: tuple

    def propose(self, context: ResearchProposalGenerationContextV1) -> tuple:
        return self.proposals


@dataclass(frozen=True, slots=True)
class DurableAutonomousStrategyPlannerV1:
    base_planner: DeterministicResearchStrategyPlannerV1
    store: object
    evidence_acquirer: ResearchEvidenceAcquisitionPort
    model_generator_factory: Callable[[object], ModelBackedResearchProposalGeneratorV1]
    clock: Callable[[], str]
    evidence_budget: ResearchEvidenceBudgetV1 = ResearchEvidenceBudgetV1()
    generator_identity: str = "phase23-model-proposal-v1"

    @property
    def strategy_space(self):
        return self.base_planner.strategy_space

    @staticmethod
    def cycle_fingerprint(context: ResearchStrategyPlanningContextV1) -> str:
        champion = context.campaign_context.champion
        return stable_sha256({
            "schema_version": 1,
            "campaign_id": context.campaign_context.state.campaign_id,
            "champion": None if champion is None else {
                "spec": champion.spec.fingerprint,
                "result": None if champion.result is None else stable_sha256(champion.result.to_dict()),
            },
            "experiments": [{"spec": item.spec.fingerprint,
                "result": None if item.result is None else stable_sha256(item.result.to_dict())} for item in context.experiments],
            "belief_set": context.belief_set.fingerprint,
            "budget": context.budget_snapshot.fingerprint,
        })

    def plan(self, context: ResearchStrategyPlanningContextV1) -> ResearchStrategyPlanningResultV1 | None:
        cycle = self.cycle_fingerprint(context)
        cohort = self.store.load_proposal_cohort_for_cycle(context.campaign_context.state.campaign_id, cycle)
        if cohort is None:
            packet = self.store.load_evidence_packet_for_cycle(context.campaign_context.state.campaign_id, cycle)
            if packet is None:
                packet = self.evidence_acquirer.acquire(campaign_id=context.campaign_context.state.campaign_id,
                    budget=self.evidence_budget, context=context)
                self.store.save_evidence_packet(packet, cycle_fingerprint=cycle)
            generator = self.model_generator_factory(packet)
            champion = context.campaign_context.champion
            assert champion is not None
            generation_context = ResearchProposalGenerationContextV1(
                champion=champion.spec, strategy_space=self.base_planner.strategy_space,
                budget_snapshot=context.budget_snapshot,
                known_novelty_fingerprints=tuple(sorted({item.novelty_fingerprint for item in context.historical_proposals})),
                research_memory_provenance_ids=tuple(f"belief:{item.belief_id}" for item in context.belief_set.beliefs),
            )
            outcome = generator.generate(generation_context)
            identity = stable_sha256({"cycle": cycle, "proposals": [item.fingerprint for item in outcome.proposals]})
            cohort = ResearchProposalCohortRecordV1(f"p23cohort-{identity[:20]}", context.campaign_context.state.campaign_id,
                cycle, packet.fingerprint, self.generator_identity, outcome.model_called, outcome.fallback_used,
                outcome.diagnostic, outcome.normalized_response_hash, outcome.proposals, outcome.proposal_confidences, self.clock())
            self.store.save_proposal_cohort(cohort)
        planner = replace(self.base_planner, proposal_generator=ReplayProposalGeneratorV1(cohort.proposals))
        return planner.plan(context)

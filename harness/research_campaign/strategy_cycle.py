'research stopping.1 production composition of the frozen R8 strategy layers.\n\nThis module has planning authority only.  It composes R8.1-R8.6 into one typed\nplanning result, but it does not persist a decision, register an experiment,\nexecute workers, evaluate protected surfaces, or mutate a repository.\n'
from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping, Protocol

from .beliefs import ResearchBeliefSetV1
from .models import ExperimentSpecV1
from .ports import ResearchCampaignContextV1
from .search import StrategyDimensionTargetV1, StrategySpaceV1
from .serialization import plain, safe_id, unique_text
from .store import StoredExperimentV1
from .strategy_admissibility import (
    DeterministicProposalAdmissibilityGateV1,
    ProposalAdmissibilityContextV1,
    ProposalAdmissibilityGatePort,
    ProposalAdmissibilityResultV1,
    ProposalMutationScopeEvidenceV1,
)
from .strategy_contracts import (
    ResearchProposalTargetKindV1,
    ResearchProposalV1,
    ResearchStrategyBudgetV1,
)
from .strategy_memory_priors import (
    DeterministicResearchMemoryPriorProjectorV1,
    ResearchMemoryPriorProjectionV1,
    ResearchMemoryPriorProjectorPort,
)
from .strategy_novelty import (
    DeterministicProposalNoveltyScorerV1,
    ProposalNoveltyAssessmentV1,
    ProposalNoveltyContextV1,
    ProposalNoveltyScorerPort,
)
from .strategy_proposals import (
    DeterministicResearchProposalGeneratorV1,
    ResearchProposalGenerationContextV1,
    ResearchProposalGeneratorPort,
)
from .strategy_selection import (
    DeterministicSequentialPortfolioSelectorV1,
    ResearchPortfolioSelectionV1,
    SequentialPortfolioSelectorPort,
)
from .strategy_utility import (
    DeterministicProposalUtilityScorerV1,
    ProposalUtilityAssessmentV1,
    ProposalUtilityScorerPort,
)


class ResearchStrategyPlanningError(RuntimeError):
    'Fail-closed error for research stopping.1 strategy-cycle planning.'


@dataclass(frozen=True, slots=True)
class ResearchStrategyPlanningContextV1:
    campaign_context: ResearchCampaignContextV1
    belief_set: ResearchBeliefSetV1
    experiments: tuple[StoredExperimentV1, ...]
    historical_proposals: tuple[ResearchProposalV1, ...]
    budget_snapshot: ResearchStrategyBudgetV1

    def __post_init__(self) -> None:
        campaign_id = self.campaign_context.state.campaign_id
        if self.campaign_context.champion is None:
            raise ValueError("Phase23.1 strategy planning requires a durable champion")
        if self.campaign_context.champion.spec.campaign_id != campaign_id:
            raise ValueError("strategy planning champion is cross-campaign")
        if not isinstance(self.belief_set, ResearchBeliefSetV1):
            raise ValueError("belief_set must be ResearchBeliefSetV1")
        if self.belief_set.campaign_id != campaign_id:
            raise ValueError("strategy planning belief_set is cross-campaign")
        if not isinstance(self.experiments, tuple):
            raise ValueError("experiments must be tuple")
        if any(
            not isinstance(item, StoredExperimentV1)
            or item.spec.campaign_id != campaign_id
            for item in self.experiments
        ):
            raise ValueError("strategy planning experiments must belong to campaign")
        if not isinstance(self.historical_proposals, tuple):
            raise ValueError("historical_proposals must be tuple")
        if any(
            not isinstance(item, ResearchProposalV1)
            or item.campaign_id != campaign_id
            for item in self.historical_proposals
        ):
            raise ValueError("historical proposals must belong to campaign")
        if not isinstance(self.budget_snapshot, ResearchStrategyBudgetV1):
            raise ValueError("budget_snapshot must be ResearchStrategyBudgetV1")


@dataclass(frozen=True, slots=True)
class ResearchStrategyPlanningResultV1:
    strategy_space: StrategySpaceV1
    proposals: tuple[ResearchProposalV1, ...]
    admissibility_results: tuple[ProposalAdmissibilityResultV1, ...]
    novelty_assessments: tuple[ProposalNoveltyAssessmentV1, ...]
    memory_prior_projections: tuple[ResearchMemoryPriorProjectionV1, ...]
    utility_assessments: tuple[ProposalUtilityAssessmentV1, ...]
    selection: ResearchPortfolioSelectionV1

    def __post_init__(self) -> None:
        if not isinstance(self.strategy_space, StrategySpaceV1):
            raise ValueError("strategy_space must be StrategySpaceV1")
        if not self.proposals:
            raise ValueError("strategy planning result requires proposals")
        proposal_ids = tuple(item.proposal_id for item in self.proposals)
        if len(set(proposal_ids)) != len(proposal_ids):
            raise ValueError("strategy planning result has duplicate proposal_id")
        if tuple(item.proposal_id for item in self.admissibility_results) != proposal_ids:
            raise ValueError(
                "admissibility_results must preserve generated proposal order"
            )
        admissible_ids = tuple(
            item.proposal_id for item in self.admissibility_results if item.admissible
        )
        if set(item.proposal_id for item in self.novelty_assessments) != set(
            admissible_ids
        ):
            raise ValueError(
                "novelty assessments must exist exactly for admissible proposals"
            )
        if set(item.proposal_id for item in self.memory_prior_projections) != set(
            admissible_ids
        ):
            raise ValueError(
                "memory priors must exist exactly for admissible proposals"
            )
        if set(item.proposal_id for item in self.utility_assessments) != set(
            admissible_ids
        ):
            raise ValueError(
                "utility assessments must exist exactly for admissible proposals"
            )
        if self.selection.decision.ranked_proposal_ids != tuple(
            item.proposal_id for item in self.selection.rank_entries
        ):
            raise ValueError("selection rank order is internally inconsistent")
        if set(self.selection.decision.ranked_proposal_ids) != set(proposal_ids):
            raise ValueError("selection must rank every generated proposal")

    @property
    def selected_proposal(self) -> ResearchProposalV1 | None:
        selected_id = self.selection.decision.selected_proposal_id
        if selected_id is None:
            return None
        return next(item for item in self.proposals if item.proposal_id == selected_id)


class ResearchStrategyPlanningPort(Protocol):
    @property
    def strategy_space(self) -> StrategySpaceV1: ...

    def plan(
        self,
        context: ResearchStrategyPlanningContextV1,
    ) -> ResearchStrategyPlanningResultV1 | None: ...


@dataclass(frozen=True, slots=True)
class DeterministicResearchStrategyPlannerV1:
    """Compose the accepted R8.1-R8.6 planning layers without execution authority."""

    strategy_space: StrategySpaceV1
    mutation_scope_by_dimension: Mapping[str, tuple[str, ...]] = field(
        default_factory=dict
    )
    proposal_generator: ResearchProposalGeneratorPort = field(
        default_factory=DeterministicResearchProposalGeneratorV1
    )
    admissibility_gate: ProposalAdmissibilityGatePort = field(
        default_factory=DeterministicProposalAdmissibilityGateV1
    )
    novelty_scorer: ProposalNoveltyScorerPort = field(
        default_factory=DeterministicProposalNoveltyScorerV1
    )
    memory_prior_projector: ResearchMemoryPriorProjectorPort = field(
        default_factory=DeterministicResearchMemoryPriorProjectorV1
    )
    utility_scorer: ProposalUtilityScorerPort = field(
        default_factory=DeterministicProposalUtilityScorerV1
    )
    selector: SequentialPortfolioSelectorPort = field(
        default_factory=DeterministicSequentialPortfolioSelectorV1
    )

    def __post_init__(self) -> None:
        if not isinstance(self.strategy_space, StrategySpaceV1):
            raise ValueError("strategy_space must be StrategySpaceV1")
        dimension_names = {item.name for item in self.strategy_space.dimensions}
        normalized: dict[str, tuple[str, ...]] = {}
        for dimension, scope in self.mutation_scope_by_dimension.items():
            key = safe_id(dimension, "mutation_scope_by_dimension key")
            if key not in dimension_names:
                raise ValueError(
                    f"mutation scope references unknown strategy dimension: {key}"
                )
            normalized[key] = unique_text(
                tuple(scope),
                f"mutation_scope_by_dimension.{key}",
                maximum=256,
                item_max_length=8_000,
            )
        object.__setattr__(
            self,
            "mutation_scope_by_dimension",
            MappingProxyType(normalized),
        )

    def plan(
        self,
        context: ResearchStrategyPlanningContextV1,
    ) -> ResearchStrategyPlanningResultV1 | None:
        if not isinstance(context, ResearchStrategyPlanningContextV1):
            raise TypeError("context must be ResearchStrategyPlanningContextV1")

        champion = context.campaign_context.champion
        assert champion is not None
        known_novelty = tuple(
            sorted({item.novelty_fingerprint for item in context.historical_proposals})
        )
        memory_provenance = tuple(
            f"belief:{item.belief_id}" for item in context.belief_set.beliefs
        )

        proposals = self.proposal_generator.propose(
            ResearchProposalGenerationContextV1(
                champion=champion.spec,
                strategy_space=self.strategy_space,
                budget_snapshot=context.budget_snapshot,
                known_novelty_fingerprints=known_novelty,
                research_memory_provenance_ids=memory_provenance,
            )
        )
        if not proposals:
            return None

        scope_evidence = tuple(
            ProposalMutationScopeEvidenceV1(
                proposal_id=proposal.proposal_id,
                requested_mutation_scope=self.mutation_scope_by_dimension[
                    proposal.target_dimension
                ],
            )
            for proposal in proposals
            if proposal.target_kind is ResearchProposalTargetKindV1.EXECUTION
            and proposal.target_dimension in self.mutation_scope_by_dimension
        )

        # Proposal novelty is relative to the current champion and therefore does
        # not by itself detect a return to a logical ExperimentSpec that was run
        # before R8 (for example the original baseline). Project each proposal to
        # the same logical fingerprint semantics used by ExperimentSpecV1 and
        # feed exact history collisions into the existing R8 hard duplicate gate.
        existing_experiment_fingerprints = {
            item.spec.fingerprint for item in context.experiments
        }
        rejected_logical_duplicate_novelty = tuple(
            sorted(
                {
                    proposal.novelty_fingerprint
                    for proposal in proposals
                    if self._project_experiment_fingerprint(
                        champion,
                        proposal,
                    )
                    in existing_experiment_fingerprints
                }
            )
        )

        admissibility_context = ProposalAdmissibilityContextV1(
            contract=context.campaign_context.contract,
            budget_snapshot=context.budget_snapshot,
            known_novelty_fingerprints=known_novelty,
            rejected_novelty_fingerprints=rejected_logical_duplicate_novelty,
            scope_evidence=scope_evidence,
        )
        admissibility = tuple(
            self.admissibility_gate.assess(proposal, admissibility_context)
            for proposal in proposals
        )

        admissible_ids = {
            item.proposal_id for item in admissibility if item.admissible
        }
        admissible_proposals = tuple(
            item for item in proposals if item.proposal_id in admissible_ids
        )
        admissible_results = tuple(
            item for item in admissibility if item.admissible
        )

        if admissible_proposals:
            novelty = self.novelty_scorer.assess_batch(
                admissible_proposals,
                admissible_results,
                ProposalNoveltyContextV1(
                    campaign_id=context.campaign_context.state.campaign_id,
                    historical_proposals=context.historical_proposals,
                ),
            )
            prior_projections = self.memory_prior_projector.project_batch(
                proposals=admissible_proposals,
                belief_set=context.belief_set,
                experiments=context.experiments,
                strategy_space=self.strategy_space,
            )
            prior_by_id = {
                item.proposal_id: item.prior for item in prior_projections
            }
            priors = tuple(
                prior_by_id[item.proposal_id] for item in admissible_proposals
            )
            utility = self.utility_scorer.assess_batch(
                admissible_proposals,
                admissible_results,
                novelty,
                priors,
            )
        else:
            novelty = ()
            prior_projections = ()
            utility = ()

        selection = self.selector.select(
            proposals,
            admissibility,
            utility,
            context.budget_snapshot,
        )
        return ResearchStrategyPlanningResultV1(
            strategy_space=self.strategy_space,
            proposals=proposals,
            admissibility_results=admissibility,
            novelty_assessments=novelty,
            memory_prior_projections=prior_projections,
            utility_assessments=utility,
            selection=selection,
        )

    def _project_experiment_fingerprint(
        self,
        champion: StoredExperimentV1,
        proposal: ResearchProposalV1,
    ) -> str:
        source = champion.spec
        dimensions = {
            item.name: item for item in self.strategy_space.dimensions
        }
        dimension = dimensions.get(proposal.target_dimension)
        if dimension is None:
            raise ResearchStrategyPlanningError(
                "proposal target_dimension is absent from strategy_space"
            )

        expected_key = dimension.key or dimension.target.value.casefold()
        if proposal.target_key != expected_key:
            raise ResearchStrategyPlanningError(
                "proposal target_key does not match strategy dimension"
            )

        parameters = dict(plain(source.parameters))
        sampling = dict(plain(source.sampling_config))
        resources = dict(plain(source.resource_budget))
        execution = dict(plain(source.execution))

        model_id = source.model_id
        provider = source.provider
        model_version = source.model_version
        prompt_hash = source.prompt_hash
        tool_policy_hash = source.tool_policy_hash
        seed = source.seed
        strategy_id = source.strategy_id

        value = plain(proposal.target_value)
        target = dimension.target
        key = dimension.key or ""

        if target is StrategyDimensionTargetV1.PARAMETER:
            parameters[key] = value
        elif target is StrategyDimensionTargetV1.MODEL_ID:
            model_id = value
        elif target is StrategyDimensionTargetV1.PROVIDER:
            provider = value
        elif target is StrategyDimensionTargetV1.MODEL_VERSION:
            model_version = value
        elif target is StrategyDimensionTargetV1.PROMPT_HASH:
            prompt_hash = value
        elif target is StrategyDimensionTargetV1.SAMPLING_CONFIG:
            sampling[key] = value
        elif target is StrategyDimensionTargetV1.TOOL_POLICY_HASH:
            tool_policy_hash = value
        elif target is StrategyDimensionTargetV1.SEED:
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ResearchStrategyPlanningError(
                    "projected SEED must be non-negative integer"
                )
            seed = value
        elif target is StrategyDimensionTargetV1.RESOURCE_BUDGET:
            resources[key] = value
        elif target is StrategyDimensionTargetV1.EXECUTION:
            execution[key] = value
        elif target is StrategyDimensionTargetV1.STRATEGY_ID:
            strategy_id = safe_id(value, "projected strategy_id")
        else:  # pragma: no cover - frozen R8 enum exhaustiveness guard
            raise ResearchStrategyPlanningError(
                f"unsupported strategy target {target!r}"
            )

        code_base_commit = source.code_base_commit
        if (
            champion.result is not None
            and champion.result.candidate_commit is not None
        ):
            code_base_commit = champion.result.candidate_commit

        probe = ExperimentSpecV1(
            experiment_id="p231-logical-projection",
            campaign_id=source.campaign_id,
            hypothesis_id="p231-logical-projection-h",
            parent_experiment_ids=(),
            strategy_id=strategy_id,
            parameters=parameters,
            model_id=model_id,
            provider=provider,
            model_version=model_version,
            prompt_hash=prompt_hash,
            sampling_config=sampling,
            tool_policy_hash=tool_policy_hash,
            code_base_commit=code_base_commit,
            candidate_patch_ref=None,
            dataset_fingerprint=source.dataset_fingerprint,
            evaluation_contract_hash=source.evaluation_contract_hash,
            seed=seed,
            resource_budget=resources,
            execution=execution,
            created_at=source.created_at,
        )
        return probe.fingerprint

'Deterministic outer research loop for research campaign.R3.\n\nThe controller decides *what experiment to try next*. It never schedules workers,\nmutates worktrees, evaluates metrics itself, or replaces existing Swarm authority.\nThose responsibilities are accessed only through ports.\n'
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
from typing import Callable, Mapping

from .adaptive_experiment import (
    AdaptiveExperimentMaterializationPort,
    AdaptiveExperimentProposalV1,
)
from .beliefs import (
    DeterministicResearchBeliefConsolidatorV1,
    ResearchBeliefTargetV1,
)
from .belief_search_context import (
    DeterministicResearchBeliefSearchContextProjectorV1,
    ResearchBeliefSearchContextProjectorPort,
)
from .candidate_evaluation import (
    CandidateEvaluationResultV1,
    CandidateEvaluationStatusV1,
    CandidateEvaluationSurfaceV1,
)
from .campaign import (
    ResearchBudgetUsageV1,
    ResearchCampaignStateV1,
    ResearchCampaignStatusV1,
)
from .confirmation import CandidateEvaluationPolicyV1, CandidateEvaluationPort
from .decisions import SearchDecisionActionV1, SearchDecisionV1
from .hypothesis_adaptation import (
    HypothesisAdaptationContextV1,
    HypothesisGenerationPort,
    hypothesis_adaptation_fingerprint_from_hypothesis,
)
from .insights import (
    DeterministicResearchInsightExtractorV1,
    ResearchInsightRecordV1,
)
from .models import (
    ExperimentFailureKindV1,
    ExperimentResultV1,
    ExperimentSpecV1,
    ExperimentStatusV1,
)
from .repair import (
    FailureDrivenRepairPolicyV1,
    GateFailureEvidenceV1,
    RepairPlanningPort,
    RepairProposalV1,
)
from .ports import (
    CampaignWaitPort,
    EvaluationPort,
    ExperimentEvaluationStatusV1,
    ExperimentExecutionPort,
    ExperimentExecutionStatusV1,
    ExperimentObservationV1,
    ResearchCampaignContextV1,
    SearchPolicyPort,
)
from .search_context import ResearchSearchContextProjectorPort
from .serialization import stable_sha256, timestamp, timestamp_value
from .store import ResearchExperimentStore, StoredExperimentV1
from .stop_policy import ResearchStopDecisionV1, ResearchStopPolicyV1, ResearchStopReasonV1
from .strategy_contracts import ResearchRiskClassV1, ResearchStrategyBudgetV1
from .strategy_cycle import (
    ResearchStrategyPlanningContextV1,
    ResearchStrategyPlanningPort,
)
from .strategy_durability import (
    DurableResearchStrategyCoordinatorV1,
    ResearchStrategyCheckpointStageV1,
    ResearchStrategyCheckpointV1,
)


_STOP_DECISION_REF_LIMIT = 512


def _bounded_stop_decision_refs(refs: tuple[str, ...]) -> tuple[str, ...]:
    if len(refs) <= _STOP_DECISION_REF_LIMIT:
        return refs
    return refs[-_STOP_DECISION_REF_LIMIT:]


class ResearchCampaignControllerError(RuntimeError):
    pass


class CampaignTickDispositionV1(StrEnum):
    EXECUTED = "EXECUTED"
    REUSED_TERMINAL = "REUSED_TERMINAL"
    WAITING = "WAITING"
    STAGNATED = "STAGNATED"
    PHASE_ADVANCED = "PHASE_ADVANCED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    COMPLETED = "COMPLETED"
    TERMINAL_NOOP = "TERMINAL_NOOP"
    STOPPED = "STOPPED"


@dataclass(frozen=True, slots=True)
class CampaignTickResultV1:
    campaign_id: str
    disposition: CampaignTickDispositionV1
    state_before: ResearchCampaignStateV1
    state_after: ResearchCampaignStateV1
    experiment_id: str | None = None
    decision_action: SearchDecisionActionV1 | None = None
    reused_existing: bool = False
    reason: str | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ResearchCampaignControllerV1:
    def __init__(
        self,
        *,
        store: ResearchExperimentStore,
        search_policy: SearchPolicyPort,
        execution_port: ExperimentExecutionPort,
        evaluation_port: EvaluationPort,
        wait_port: CampaignWaitPort | None = None,
        candidate_evaluation_port: CandidateEvaluationPort | None = None,
        candidate_evaluation_policy: CandidateEvaluationPolicyV1 | None = None,
        repair_policy: FailureDrivenRepairPolicyV1 | None = None,
        repair_planning_port: RepairPlanningPort | None = None,
        clock: Callable[[], str] = _now,
        recent_limit: int = 32,
        search_context_projector: ResearchSearchContextProjectorPort | None = None,
        belief_consolidator: DeterministicResearchBeliefConsolidatorV1 | None = None,
        belief_search_context_projector: ResearchBeliefSearchContextProjectorPort | None = None,
        insight_extractor: DeterministicResearchInsightExtractorV1 | None = None,
        hypothesis_generator: HypothesisGenerationPort | None = None,
        adaptive_experiment_materializer: AdaptiveExperimentMaterializationPort | None = None,
        strategy_planner: ResearchStrategyPlanningPort | None = None,
        strategy_durability_coordinator: DurableResearchStrategyCoordinatorV1 | None = None,
        stop_policy: ResearchStopPolicyV1 | None = None,
    ) -> None:
        if not isinstance(recent_limit, int) or isinstance(recent_limit, bool) or not 1 <= recent_limit <= 1000:
            raise ValueError("recent_limit must be integer in [1,1000]")
        self.store = store
        self.search_policy = search_policy
        self.execution_port = execution_port
        self.evaluation_port = evaluation_port
        if (candidate_evaluation_port is None) != (candidate_evaluation_policy is None):
            raise ValueError("candidate_evaluation_port and candidate_evaluation_policy must be configured together")
        if (repair_policy is None) != (repair_planning_port is None):
            raise ValueError("repair_policy and repair_planning_port must be configured together")
        if repair_policy is not None and candidate_evaluation_port is None:
            raise ValueError("failure-driven repair requires protected candidate evaluation")
        if (hypothesis_generator is None) != (adaptive_experiment_materializer is None):
            raise ValueError("hypothesis_generator and adaptive_experiment_materializer must be configured together")
        self.wait_port = wait_port
        self.candidate_evaluation_port = candidate_evaluation_port
        self.candidate_evaluation_policy = candidate_evaluation_policy
        self.repair_policy = repair_policy
        self.repair_planning_port = repair_planning_port
        self.clock = clock
        self.recent_limit = recent_limit
        # Explicit search_context_projector is retained as a compatibility override.
        # The default production path is R7.5G belief-driven projection.
        self.search_context_projector = search_context_projector
        self.belief_consolidator = (
            DeterministicResearchBeliefConsolidatorV1()
            if belief_consolidator is None
            else belief_consolidator
        )
        self.belief_search_context_projector = (
            DeterministicResearchBeliefSearchContextProjectorV1()
            if belief_search_context_projector is None
            else belief_search_context_projector
        )
        self.insight_extractor = (
            DeterministicResearchInsightExtractorV1()
            if insight_extractor is None
            else insight_extractor
        )
        self.hypothesis_generator = hypothesis_generator
        self.adaptive_experiment_materializer = adaptive_experiment_materializer
        if strategy_planner is None and strategy_durability_coordinator is not None:
            raise ValueError(
                "strategy_durability_coordinator requires strategy_planner"
            )
        self.strategy_planner = strategy_planner
        self.strategy_durability_coordinator = (
            DurableResearchStrategyCoordinatorV1()
            if strategy_planner is not None and strategy_durability_coordinator is None
            else strategy_durability_coordinator
        )
        self.stop_policy = stop_policy

    def tick(self, campaign_id: str) -> CampaignTickResultV1:
        state_before = self.store.load_campaign_state(campaign_id)
        if state_before.is_terminal:
            return CampaignTickResultV1(
                campaign_id,
                CampaignTickDispositionV1.TERMINAL_NOOP,
                state_before,
                state_before,
                reason=state_before.reason,
            )
        campaign_stopped_state = (
            state_before.status is ResearchCampaignStatusV1.CONFIRMING_TOP_K
            and state_before.reason is not None
            and (state_before.reason.startswith("Phase23 autonomous stop:")
                 or state_before.reason == "R8 strategy planner produced no admissible selected proposal")
        )
        if self.stop_policy is not None and (state_before.status in {
                ResearchCampaignStatusV1.SEARCHING, ResearchCampaignStatusV1.STAGNATED} or campaign_stopped_state):
            persisted_stop = self.store.latest_terminal_stop_decision(campaign_id)
            if persisted_stop is not None:
                return CampaignTickResultV1(campaign_id, CampaignTickDispositionV1.STOPPED, state_before, state_before,
                                            reason=persisted_stop.explanation)


        state = state_before
        if state.status is ResearchCampaignStatusV1.CREATED:
            state = self._transition(state, ResearchCampaignStatusV1.BASELINE_REQUIRED, reason="campaign started")

        state = self._reconcile_durable_outcome(campaign_id, state)
        state = self._reconcile_durable_repair(campaign_id, state)
        # R7.6 closes the evidence→memory seam outside _context(): context construction
        # remains read-only while each tick idempotently materializes durable DEV/GATE/repair insights.
        planning_experiments = self.store.list_experiments(
            campaign_id,
            limit=100_000,
        )
        self._materialize_research_memory(
            campaign_id,
            experiments=planning_experiments,
        )
        planning_insights = self.store.list_typed_insights(
            campaign_id,
            limit=100_000,
        )

        context = self._context(
            campaign_id,
            state,
            experiments=planning_experiments,
            typed_insights=planning_insights,
        )
        budget_reason = self._current_budget_reason(context)
        if budget_reason is not None:
            self._persist_campaign_terminal_decision(
                context,
                ResearchStopReasonV1.BUDGET_EXHAUSTED,
                budget_reason,
            )
            exhausted = self._transition(
                state,
                ResearchCampaignStatusV1.BUDGET_EXHAUSTED,
                reason=budget_reason,
            )
            return CampaignTickResultV1(
                campaign_id,
                CampaignTickDispositionV1.BUDGET_EXHAUSTED,
                state_before,
                exhausted,
                reason=budget_reason,
            )

        wait = None if self.wait_port is None else self.wait_port.pending(campaign_id)
        if wait is not None:
            waiting = self._transition(state, wait.status, reason=wait.reason)
            return CampaignTickResultV1(
                campaign_id,
                CampaignTickDispositionV1.WAITING,
                state_before,
                waiting,
                reason=wait.reason,
            )
        if state.status in {ResearchCampaignStatusV1.WAITING_FOR_INPUT, ResearchCampaignStatusV1.WAITING_FOR_RESOURCE}:
            if state.resume_status is None:
                raise ResearchCampaignControllerError("waiting campaign state lost resume_status")
            state = self._transition(state, state.resume_status, reason="blocking request resolved")
            context = self._context(
                campaign_id,
                state,
                experiments=planning_experiments,
                typed_insights=planning_insights,
            )

        budget_reason = self._current_budget_reason(context)
        if budget_reason is not None:
            self._persist_campaign_terminal_decision(
                context,
                ResearchStopReasonV1.BUDGET_EXHAUSTED,
                budget_reason,
            )
            exhausted = self._transition(
                state,
                ResearchCampaignStatusV1.BUDGET_EXHAUSTED,
                reason=budget_reason,
            )
            return CampaignTickResultV1(
                campaign_id,
                CampaignTickDispositionV1.BUDGET_EXHAUSTED,
                state_before,
                exhausted,
                reason=budget_reason,
            )

        if (
            self.candidate_evaluation_port is not None
            and state.status in {
                ResearchCampaignStatusV1.CONFIRMING_TOP_K,
                ResearchCampaignStatusV1.FINAL_EVALUATION,
            }
        ):
            return self._tick_protected_evaluation(state_before, state)

        strategy_checkpoint: ResearchStrategyCheckpointV1 | None = None
        if self.strategy_planner is not None:
            state, strategy_checkpoint = self._reconcile_strategy_checkpoint(state)
            if state.generation != context.state.generation:
                context = self._context(
                    campaign_id,
                    state,
                    experiments=planning_experiments,
                    typed_insights=planning_insights,
                )

        pending = self._pending_experiment(campaign_id)
        reused_existing = pending is not None
        if strategy_checkpoint is None and pending is None and self.stop_policy is not None and self._strategy_planning_applicable(state, context):
            stopped = self._campaign_stop_before_planning(
                state_before,
                state,
                context,
                experiments=planning_experiments,
                typed_insights=planning_insights,
            )
            if stopped is not None:
                return stopped


        if strategy_checkpoint is not None:
            coordinator = self.strategy_durability_coordinator
            assert coordinator is not None
            if strategy_checkpoint.stage is ResearchStrategyCheckpointStageV1.DECISION_PERSISTED:
                if pending is not None:
                    raise ResearchCampaignControllerError(
                        "DECISION_PERSISTED strategy checkpoint cannot already own a pending experiment"
                    )
                projected_reason = self._projected_budget_reason(
                    context,
                    strategy_checkpoint.planned_experiment,
                )
                if projected_reason is not None:
                    exhausted = self._transition(
                        state,
                        ResearchCampaignStatusV1.BUDGET_EXHAUSTED,
                        reason=projected_reason,
                    )
                    return CampaignTickResultV1(
                        campaign_id,
                        CampaignTickDispositionV1.BUDGET_EXHAUSTED,
                        state_before,
                        exhausted,
                        reason=projected_reason,
                    )
                strategy_checkpoint = coordinator.ensure_selected_experiment_planned(
                    store=self.store,
                    campaign_id=campaign_id,
                    decision_fingerprint=strategy_checkpoint.decision_fingerprint,
                )
                pending = self.store.find_by_fingerprint(
                    campaign_id,
                    strategy_checkpoint.planned_experiment.fingerprint,
                )
                if pending is None:
                    raise ResearchCampaignControllerError(
                        "planned strategy checkpoint lost exact experiment"
                    )
            elif pending is None:
                pending = self.store.find_by_fingerprint(
                    campaign_id,
                    strategy_checkpoint.planned_experiment.fingerprint,
                )
                if pending is None or (
                    pending.result is not None and pending.result.is_terminal
                ):
                    raise ResearchCampaignControllerError(
                        "active EXPERIMENT_PLANNED strategy checkpoint lost pending experiment"
                    )
            if pending.spec.experiment_id != strategy_checkpoint.planned_experiment.experiment_id:
                raise ResearchCampaignControllerError(
                    "pending experiment differs from exact durable strategy plan"
                )
            reused_existing = True

        if pending is None:
            if self._strategy_planning_applicable(state, context):
                assert self.strategy_planner is not None
                coordinator = self.strategy_durability_coordinator
                assert coordinator is not None
                planning = self.strategy_planner.plan(
                    self._strategy_planning_context(
                        context,
                        experiments=planning_experiments,
                        typed_insights=planning_insights,
                    )
                )
                if planning is None or planning.selected_proposal is None:
                    return self._advance_without_strategy_selection(
                        state_before,
                        state,
                        context,
                    )
                strategy_checkpoint = coordinator.persist_decision(
                    store=self.store,
                    selection=planning.selection,
                    proposals=planning.proposals,
                    champion=context.champion,
                    strategy_space=planning.strategy_space,
                    created_at=timestamp(self.clock(), "clock"),
                )
                state = self._transition(
                    state,
                    state.status,
                    pending_strategy_decision_fingerprint=(
                        strategy_checkpoint.decision_fingerprint
                    ),
                    reason=(
                        "persisted exact R8 strategy decision "
                        f"{strategy_checkpoint.decision_fingerprint}"
                    ),
                )
                context = self._context(
                    campaign_id,
                    state,
                    experiments=planning_experiments,
                    typed_insights=planning_insights,
                )
                projected_reason = self._projected_budget_reason(
                    context,
                    strategy_checkpoint.planned_experiment,
                )
                if projected_reason is not None:
                    exhausted = self._transition(
                        state,
                        ResearchCampaignStatusV1.BUDGET_EXHAUSTED,
                        reason=projected_reason,
                    )
                    return CampaignTickResultV1(
                        campaign_id,
                        CampaignTickDispositionV1.BUDGET_EXHAUSTED,
                        state_before,
                        exhausted,
                        reason=projected_reason,
                    )
                strategy_checkpoint = coordinator.ensure_selected_experiment_planned(
                    store=self.store,
                    campaign_id=campaign_id,
                    decision_fingerprint=strategy_checkpoint.decision_fingerprint,
                )
                pending = self.store.find_by_fingerprint(
                    campaign_id,
                    strategy_checkpoint.planned_experiment.fingerprint,
                )
                if pending is None:
                    raise ResearchCampaignControllerError(
                        "strategy coordinator did not register exact selected experiment"
                    )
                reused_existing = False
            else:
                adaptive_proposal: AdaptiveExperimentProposalV1 | None = None
                proposal = self.search_policy.propose(context)
                if proposal is None:
                    adaptive_proposal = self._adaptive_experiment_proposal(context)
                    if adaptive_proposal is None:
                        return self._advance_without_proposal(state_before, state)
                    proposal = adaptive_proposal.experiment
                self._validate_proposal(context, proposal)
                projected_reason = self._projected_budget_reason(context, proposal)
                if projected_reason is not None:
                    exhausted = self._transition(
                        state,
                        ResearchCampaignStatusV1.BUDGET_EXHAUSTED,
                        reason=projected_reason,
                    )
                    return CampaignTickResultV1(
                        campaign_id,
                        CampaignTickDispositionV1.BUDGET_EXHAUSTED,
                        state_before,
                        exhausted,
                        reason=projected_reason,
                    )
                registration = (
                    self.store.register_hypothesis_experiment(
                        adaptive_proposal.hypothesis_proposal.hypothesis,
                        proposal,
                    )
                    if adaptive_proposal is not None
                    else self.store.register_experiment(proposal)
                )
                pending = self.store.find_by_fingerprint(campaign_id, proposal.fingerprint)
                assert pending is not None
                if pending.result is not None and pending.result.is_terminal:
                    refreshed = self._sync_state_from_terminal(state, pending)
                    return CampaignTickResultV1(
                        campaign_id,
                        CampaignTickDispositionV1.REUSED_TERMINAL,
                        state_before,
                        refreshed,
                        experiment_id=pending.spec.experiment_id,
                        decision_action=pending.result.decision,
                        reused_existing=True,
                        reason="logical experiment already has terminal result",
                    )
                reused_existing = registration.reused_existing

        spec = pending.spec
        prior = pending.result
        resume = prior is not None and prior.status is ExperimentStatusV1.RUNNING
        started_at = prior.started_at if resume and prior is not None else timestamp(self.clock(), "clock")
        if prior is None or prior.status is ExperimentStatusV1.PLANNED:
            self.store.save_result(
                ExperimentResultV1(
                    experiment_id=spec.experiment_id,
                    campaign_id=spec.campaign_id,
                    status=ExperimentStatusV1.RUNNING,
                    started_at=started_at,
                )
            )

        execution = self.execution_port.execute(spec, resume=resume)
        if resume and execution.started_at != started_at:
            execution = replace(execution, started_at=started_at)
        observation_context = self._context(
            campaign_id,
            state,
            typed_insights=planning_insights,
        )
        evaluation_execution = self._execution_for_evaluation(execution, observation_context)
        evaluation = None
        if execution.status is ExperimentExecutionStatusV1.COMPLETED:
            evaluation = self.evaluation_port.evaluate(spec, evaluation_execution)
        observation = ExperimentObservationV1(spec=spec, execution=evaluation_execution, evaluation=evaluation)
        decision = self.search_policy.observe(observation_context, observation)
        self._validate_decision(observation_context, observation, decision)
        result = self._materialize_result(observation, decision)
        self.store.save_terminal_outcome(result, decision)
        post_experiments = self.store.list_experiments(
            campaign_id,
            limit=100_000,
        )
        self._materialize_research_memory(
            campaign_id,
            experiments=post_experiments,
        )
        post_insights = self.store.list_typed_insights(
            campaign_id,
            limit=100_000,
        )

        champion = spec.experiment_id if decision.action is SearchDecisionActionV1.UPDATE_CHAMPION else state.champion_experiment_id
        next_status = self._status_after_decision(state, decision.action, champion)
        state = self._transition(
            state,
            next_status,
            champion_experiment_id=champion,
            last_experiment_id=spec.experiment_id,
            reason=decision.reason,
            clear_pending_strategy_decision=(
                self._strategy_reference_matches_experiment(
                    state,
                    spec.experiment_id,
                )
            ),
        )

        post_context = self._context(
            campaign_id,
            state,
            experiments=post_experiments,
            typed_insights=post_insights,
        )
        stagnation_reason = self._stagnation_reason(post_context)
        if stagnation_reason is not None and state.status in {
            ResearchCampaignStatusV1.BASELINE_REQUIRED,
            ResearchCampaignStatusV1.SEARCHING,
        }:
            state = self._transition(state, ResearchCampaignStatusV1.STAGNATED, reason=stagnation_reason)
        else:
            post_budget = self._current_budget_reason(post_context)
            if post_budget is not None and not state.is_terminal:
                state = self._transition(state, ResearchCampaignStatusV1.BUDGET_EXHAUSTED, reason=post_budget)

        disposition = (
            CampaignTickDispositionV1.BUDGET_EXHAUSTED
            if state.status is ResearchCampaignStatusV1.BUDGET_EXHAUSTED
            else CampaignTickDispositionV1.STAGNATED
            if state.status is ResearchCampaignStatusV1.STAGNATED
            else CampaignTickDispositionV1.EXECUTED
        )
        return CampaignTickResultV1(
            campaign_id,
            disposition,
            state_before,
            state,
            experiment_id=spec.experiment_id,
            decision_action=decision.action,
            reused_existing=reused_existing,
            reason=state.reason,
        )

    def _transition(
        self,
        current: ResearchCampaignStateV1,
        status: ResearchCampaignStatusV1,
        *,
        champion_experiment_id: str | None = None,
        last_experiment_id: str | None = None,
        reason: str | None,
        pending_strategy_decision_fingerprint: str | None = None,
        clear_pending_strategy_decision: bool = False,
    ) -> ResearchCampaignStateV1:
        candidate = current.transition(
            status,
            updated_at=timestamp(self.clock(), "clock"),
            champion_experiment_id=champion_experiment_id,
            last_experiment_id=last_experiment_id,
            reason=reason,
            pending_strategy_decision_fingerprint=(
                pending_strategy_decision_fingerprint
            ),
            clear_pending_strategy_decision=clear_pending_strategy_decision,
        )
        self.store.save_campaign_state(candidate, expected_generation=current.generation)
        return candidate

    def _materialize_research_memory(
        self,
        campaign_id: str,
        *,
        experiments: tuple[StoredExperimentV1, ...] | None = None,
    ) -> None:
        """Idempotently materialize durable evidence into typed research memory.

        This deliberately runs outside ``_context`` so the context projection remains
        read-only.  The extractor excludes TEST by contract and SQLite registration is
        fingerprint-idempotent across restart.
        """
        if experiments is None:
            experiments = self.store.list_experiments(campaign_id, limit=100_000)
        candidate_evaluations = self.store.list_candidate_evaluations(campaign_id, limit=100_000)
        repair_proposals = self.store.list_repair_proposals(campaign_id, limit=100_000)
        insights = self.insight_extractor.extract(
            campaign_id=campaign_id,
            experiments=experiments,
            candidate_evaluations=candidate_evaluations,
            repair_proposals=repair_proposals,
            created_at=timestamp(self.clock(), "clock"),
        )
        if insights:
            self.store.register_typed_insights(insights)

    @staticmethod
    def _focus_experiment(
        context: ResearchCampaignContextV1,
        experiments: tuple[StoredExperimentV1, ...],
    ) -> StoredExperimentV1 | None:
        if context.champion is not None:
            return context.champion
        if context.summary.best_experiment_id is not None:
            found = next(
                (item for item in experiments if item.spec.experiment_id == context.summary.best_experiment_id),
                None,
            )
            if found is not None:
                return found
        return next(
            (item for item in reversed(experiments) if item.result is not None and item.result.is_terminal),
            None,
        )

    @staticmethod
    def _lineage_source_for_belief(
        belief,
        experiments_by_id: Mapping[str, StoredExperimentV1],
    ) -> StoredExperimentV1 | None:
        candidates = [
            experiments_by_id[experiment_id]
            for experiment_id in belief.lineage_experiment_ids
            if experiment_id in experiments_by_id
        ]
        if not candidates:
            return None
        candidates.sort(
            key=lambda item: (timestamp_value(item.spec.created_at), item.spec.experiment_id),
            reverse=True,
        )
        return candidates[0]

    def _adaptive_experiment_proposal(
        self,
        context: ResearchCampaignContextV1,
    ) -> AdaptiveExperimentProposalV1 | None:
        if self.hypothesis_generator is None or self.adaptive_experiment_materializer is None:
            return None
        if context.state.status not in {
            ResearchCampaignStatusV1.SEARCHING,
            ResearchCampaignStatusV1.STAGNATED,
        }:
            return None
        dimensions = tuple(self.adaptive_experiment_materializer.dimension_names)
        if not dimensions:
            return None

        campaign_id = context.state.campaign_id
        experiments = self.store.list_experiments(campaign_id, limit=100_000)
        focus = self._focus_experiment(context, experiments)
        if focus is None:
            return None
        target_scope = self.belief_consolidator.scope_for_experiment(focus)
        target = ResearchBeliefTargetV1(
            scope=target_scope,
            experiment_id=focus.spec.experiment_id,
            code_base_commit=focus.spec.code_base_commit,
            strict_code_base=False,
        )
        belief_set = self.belief_consolidator.consolidate(
            campaign_id=campaign_id,
            insights=self.store.list_typed_insights(campaign_id, limit=100_000),
            experiments=experiments,
            target=target,
        )
        if not belief_set.beliefs:
            return None

        experiments_by_id = {item.spec.experiment_id: item for item in experiments}
        parent_hypotheses_by_strategy: dict[str, tuple[str, ...]] = {}
        source_by_belief_id: dict[str, StoredExperimentV1] = {}
        for belief in belief_set.beliefs:
            source = self._lineage_source_for_belief(belief, experiments_by_id)
            if source is None:
                continue
            source_by_belief_id[belief.belief_id] = source
            parent_hypotheses_by_strategy.setdefault(
                belief.strategy_id,
                (source.spec.hypothesis_id,),
            )

        durable_hypotheses = self.store.list_hypotheses(campaign_id, limit=100_000)
        generated_count = sum(
            1
            for item in durable_hypotheses
            if hypothesis_adaptation_fingerprint_from_hypothesis(item) is not None
        )
        base_adaptation_context = HypothesisAdaptationContextV1(
            contract=context.contract,
            usage=context.usage,
            belief_set=belief_set,
            target_scope=target_scope,
            allowed_strategy_dimensions=dimensions,
            known_hypotheses=durable_hypotheses,
            generated_hypothesis_count=generated_count,
            parent_hypothesis_ids_by_strategy=parent_hypotheses_by_strategy,
        )

        # A dimension can be structurally novel while every configured concrete value
        # is already in the experiment ledger.  Skip such dead directions in-memory
        # without persisting an orphan hypothesis, then ask the generator for the next.
        temporary_hypotheses = list(durable_hypotheses)
        max_attempts = max(1, len(dimensions) * max(1, len(belief_set.beliefs)))
        for _ in range(max_attempts):
            adaptation_context = replace(
                base_adaptation_context,
                known_hypotheses=tuple(temporary_hypotheses),
            )
            hypothesis_proposal = self.hypothesis_generator.propose(adaptation_context)
            if hypothesis_proposal is None:
                return None
            source = next(
                (source_by_belief_id.get(belief_id) for belief_id in hypothesis_proposal.source_belief_ids
                 if source_by_belief_id.get(belief_id) is not None),
                None,
            )
            if source is None:
                raise ResearchCampaignControllerError(
                    "adaptive hypothesis references belief without durable experiment lineage"
                )
            proposal = self.adaptive_experiment_materializer.propose(
                hypothesis_proposal,
                source,
                known_experiment_fingerprints=context.known_experiment_fingerprints,
            )
            if proposal is not None:
                self._validate_adaptive_experiment_proposal(context, source, proposal)
                return proposal
            temporary_hypotheses.append(hypothesis_proposal.hypothesis)
        return None

    @staticmethod
    def _validate_adaptive_experiment_proposal(
        context: ResearchCampaignContextV1,
        source: StoredExperimentV1,
        proposal: AdaptiveExperimentProposalV1,
    ) -> None:
        if proposal.campaign_id != context.state.campaign_id:
            raise ResearchCampaignControllerError("adaptive experiment proposal campaign_id mismatch")
        if proposal.source_experiment_id != source.spec.experiment_id:
            raise ResearchCampaignControllerError("adaptive experiment changed controller-selected source experiment")
        if proposal.source_experiment_fingerprint != source.spec.fingerprint:
            raise ResearchCampaignControllerError("adaptive experiment source fingerprint mismatch")
        spec = proposal.experiment
        if spec.fingerprint in context.known_experiment_fingerprints:
            raise ResearchCampaignControllerError("adaptive experiment duplicates known logical experiment")
        if spec.dataset_fingerprint != source.spec.dataset_fingerprint:
            raise ResearchCampaignControllerError("adaptive experiment must preserve source dataset fingerprint")
        if spec.evaluation_contract_hash != source.spec.evaluation_contract_hash:
            raise ResearchCampaignControllerError("adaptive experiment must preserve source EvaluationContractHash")
        if spec.model_id != source.spec.model_id or spec.provider != source.spec.provider:
            raise ResearchCampaignControllerError("adaptive experiment must preserve source model/provider scope")
        if spec.model_version != source.spec.model_version:
            raise ResearchCampaignControllerError("adaptive experiment must preserve source model version")
        if spec.prompt_hash != source.spec.prompt_hash or spec.tool_policy_hash != source.spec.tool_policy_hash:
            raise ResearchCampaignControllerError("adaptive experiment must preserve prompt/tool policy scope")
        if context.contract.allowed_models and spec.model_id not in context.contract.allowed_models:
            raise ResearchCampaignControllerError("adaptive experiment model is not allowed by ResearchContract")

    def _context(
        self,
        campaign_id: str,
        state: ResearchCampaignStateV1,
        *,
        experiments: tuple[StoredExperimentV1, ...] | None = None,
        typed_insights: tuple[ResearchInsightRecordV1, ...] | None = None,
    ) -> ResearchCampaignContextV1:
        contract = self.store.current_research_contract(campaign_id)
        summary = self.store.campaign_summary(campaign_id)
        if experiments is None:
            experiments = self.store.list_experiments(campaign_id, limit=100_000)
        elif any(item.spec.campaign_id != campaign_id for item in experiments):
            raise ResearchCampaignControllerError(
                "preloaded experiment projection is cross-campaign"
            )
        decisions = self.store.list_decisions(campaign_id, limit=100_000)
        candidate_evaluations = self.store.list_candidate_evaluations(campaign_id, limit=100_000)
        usage = self._usage(experiments, decisions, summary.experiment_count, candidate_evaluations)
        champion = None
        if state.champion_experiment_id is not None:
            champion = next((item for item in experiments if item.spec.experiment_id == state.champion_experiment_id), None)
            if champion is None:
                raise ResearchCampaignControllerError("campaign state references missing champion experiment")
        if typed_insights is None:
            typed_insights = self.store.list_typed_insights(campaign_id, limit=100_000)
        elif any(item.campaign_id != campaign_id for item in typed_insights):
            raise ResearchCampaignControllerError(
                "preloaded typed-insight projection is cross-campaign"
            )
        focus = champion
        if focus is None and summary.best_experiment_id is not None:
            focus = next(
                (item for item in experiments if item.spec.experiment_id == summary.best_experiment_id),
                None,
            )
        if focus is None:
            focus = next(
                (item for item in reversed(experiments) if item.result is not None and item.result.is_terminal),
                None,
            )

        if self.search_context_projector is not None:
            # Compatibility-only explicit override for callers that still require
            # the raw R7.3 typed-insight projector.  Default production uses beliefs.
            preferred_evaluation_contract_hash = (
                None if focus is None else focus.spec.evaluation_contract_hash
            )
            research_search_context = self.search_context_projector.project(
                campaign_id=campaign_id,
                insights=typed_insights,
                preferred_evaluation_contract_hash=preferred_evaluation_contract_hash,
                focus_experiment_id=None if focus is None else focus.spec.experiment_id,
                focus_hypothesis_id=None if focus is None else focus.spec.hypothesis_id,
                focus_strategy_id=None if focus is None else focus.spec.strategy_id,
            )
        else:
            target = None
            if focus is not None:
                target = ResearchBeliefTargetV1(
                    scope=self.belief_consolidator.scope_for_experiment(focus),
                    experiment_id=focus.spec.experiment_id,
                    code_base_commit=focus.spec.code_base_commit,
                    strict_code_base=False,
                )
            belief_set = self.belief_consolidator.consolidate(
                campaign_id=campaign_id,
                insights=typed_insights,
                experiments=experiments,
                target=target,
            )
            research_search_context = self.belief_search_context_projector.project(
                belief_set=belief_set,
                focus_experiment_id=None if focus is None else focus.spec.experiment_id,
                focus_strategy_id=None if focus is None else focus.spec.strategy_id,
            )
        return ResearchCampaignContextV1(
            state=state,
            contract=contract,
            summary=summary,
            usage=usage,
            champion=champion,
            recent_experiments=experiments[-self.recent_limit :],
            known_experiment_fingerprints=frozenset(item.spec.fingerprint for item in experiments),
            research_search_context=research_search_context,
        )

    @staticmethod
    def _telemetry_number(telemetry: Mapping[str, object], key: str, default: float, *, integer: bool = False) -> float:
        raw = telemetry.get(key, default)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw < 0:
            raise ResearchCampaignControllerError(f"terminal telemetry.{key} must be non-negative number")
        value = float(raw)
        if integer and int(value) != value:
            raise ResearchCampaignControllerError(f"terminal telemetry.{key} must be integer-valued")
        return value

    @staticmethod
    def _usage(experiments, decisions, experiment_count: int, candidate_evaluations=()) -> ResearchBudgetUsageV1:
        terminal_count = 0
        metric_calls = 0
        gpu_seconds = 0.0
        api_cost = 0.0
        ordered_terminal = []
        for item in experiments:
            result = item.result
            if result is None or not result.is_terminal:
                continue
            terminal_count += 1
            ordered_terminal.append(item)
            telemetry = result.telemetry
            metric_calls += int(ResearchCampaignControllerV1._telemetry_number(
                telemetry, "metric_calls", 1 if result.primary_metric is not None else 0, integer=True
            ))
            gpu_seconds += ResearchCampaignControllerV1._telemetry_number(telemetry, "gpu_seconds", 0.0)
            api_cost += ResearchCampaignControllerV1._telemetry_number(telemetry, "api_cost", 0.0)
        consecutive_crashes = 0
        for item in reversed(ordered_terminal):
            assert item.result is not None
            if item.result.status is ExperimentStatusV1.CRASHED:
                consecutive_crashes += 1
            else:
                break
        last_champion_id = None
        for decision in decisions:
            if decision.action is SearchDecisionActionV1.UPDATE_CHAMPION:
                last_champion_id = decision.experiment_id
        no_improvement = 0
        if last_champion_id is not None:
            seen = False
            for item in ordered_terminal:
                if item.spec.experiment_id == last_champion_id:
                    seen = True
                    no_improvement = 0
                    continue
                if seen:
                    no_improvement += 1
        elif ordered_terminal:
            no_improvement = len(ordered_terminal)
        for item in candidate_evaluations:
            result = item.result
            if result is None or not result.is_terminal:
                continue
            metric_calls += int(ResearchCampaignControllerV1._telemetry_number(
                result.telemetry, "metric_calls", 1 if result.primary_metric is not None else 0, integer=True
            ))
            gpu_seconds += ResearchCampaignControllerV1._telemetry_number(result.telemetry, "gpu_seconds", 0.0)
            api_cost += ResearchCampaignControllerV1._telemetry_number(result.telemetry, "api_cost", 0.0)
        return ResearchBudgetUsageV1(
            experiment_count=experiment_count,
            terminal_count=terminal_count,
            metric_calls=metric_calls,
            gpu_seconds=gpu_seconds,
            api_cost=api_cost,
            consecutive_crashes=consecutive_crashes,
            no_improvement_count=no_improvement,
        )

    def _current_budget_reason(self, context: ResearchCampaignContextV1) -> str | None:
        budget = context.contract.budget
        usage = context.usage
        now = timestamp_value(timestamp(self.clock(), "clock"))
        if now >= timestamp_value(budget.wall_deadline):
            return "wall time budget exhausted"
        if usage.experiment_count >= budget.max_experiments:
            return "experiment budget exhausted"
        if usage.metric_calls >= budget.max_metric_calls:
            return "metric-call budget exhausted"
        if usage.gpu_seconds > budget.max_gpu_seconds or (
            budget.max_gpu_seconds > 0 and usage.gpu_seconds >= budget.max_gpu_seconds
        ):
            return "GPU budget exhausted"
        if usage.api_cost > budget.max_api_cost or (budget.max_api_cost > 0 and usage.api_cost >= budget.max_api_cost):
            return "API cost budget exhausted"
        return None

    @staticmethod
    def _estimate(spec: ExperimentSpecV1, key: str, *, integer: bool = False) -> float:
        raw = spec.resource_budget.get(key, 0)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw < 0:
            raise ResearchCampaignControllerError(f"resource_budget.{key} must be non-negative number")
        if integer and int(raw) != raw:
            raise ResearchCampaignControllerError(f"resource_budget.{key} must be integer-valued")
        return float(raw)

    def _projected_budget_reason(self, context: ResearchCampaignContextV1, spec: ExperimentSpecV1) -> str | None:
        budget = context.contract.budget
        usage = context.usage
        if usage.experiment_count + 1 > budget.max_experiments:
            return "experiment proposal exceeds experiment budget"
        gpu = self._estimate(spec, "estimated_gpu_seconds")
        cost = self._estimate(spec, "estimated_api_cost")
        metric_calls = int(self._estimate(spec, "estimated_metric_calls", integer=True))
        if usage.gpu_seconds + gpu > budget.max_gpu_seconds:
            return "experiment proposal exceeds GPU budget"
        if usage.api_cost + cost > budget.max_api_cost:
            return "experiment proposal exceeds API cost budget"
        if usage.metric_calls + metric_calls > budget.max_metric_calls:
            return "experiment proposal exceeds metric-call budget"
        return None

    @staticmethod
    def _stagnation_reason(context: ResearchCampaignContextV1) -> str | None:
        budget = context.contract.budget
        if context.usage.consecutive_crashes >= budget.max_consecutive_crashes:
            return "maximum consecutive experiment crashes reached"
        if context.usage.no_improvement_count >= budget.max_no_improvement_window:
            return "maximum no-improvement window reached"
        return None

    def _tick_protected_evaluation(
        self,
        state_before: ResearchCampaignStateV1,
        state: ResearchCampaignStateV1,
    ) -> CampaignTickResultV1:
        assert self.candidate_evaluation_port is not None
        assert self.candidate_evaluation_policy is not None
        context = self._context(state.campaign_id, state)
        if context.champion is None:
            stalled = self._transition(
                state,
                ResearchCampaignStatusV1.STAGNATED,
                reason="protected evaluation requires a DEV champion",
            )
            return CampaignTickResultV1(
                state.campaign_id,
                CampaignTickDispositionV1.STAGNATED,
                state_before,
                stalled,
                reason=stalled.reason,
            )

        experiments = self.store.list_experiments(state.campaign_id, limit=100_000)
        if self.repair_policy is not None:
            retired_source_ids = frozenset(
                item.evidence.source_experiment_id
                for item in self.store.list_repair_proposals(state.campaign_id, limit=100_000)
            )
            experiments = tuple(
                item for item in experiments if item.spec.experiment_id not in retired_source_ids
            )
        comparable_hash = context.champion.spec.evaluation_contract_hash
        top_k = self.candidate_evaluation_policy.top_dev_candidates(
            experiments,
            direction=context.contract.metric_direction,
            evaluation_contract_hash=comparable_hash,
        )
        if not top_k:
            stalled = self._transition(
                state,
                ResearchCampaignStatusV1.STAGNATED,
                reason="no successful immutable DEV candidates are eligible for protected evaluation",
            )
            return CampaignTickResultV1(
                state.campaign_id,
                CampaignTickDispositionV1.STAGNATED,
                state_before,
                stalled,
                reason=stalled.reason,
            )

        if state.status is ResearchCampaignStatusV1.CONFIRMING_TOP_K:
            return self._tick_gate_confirmation(state_before, state, context, top_k)
        if state.status is ResearchCampaignStatusV1.FINAL_EVALUATION:
            return self._tick_final_evaluation(state_before, state, context, top_k)
        raise ResearchCampaignControllerError("protected evaluation tick called outside protected phase")

    def _protected_budget_reason(self, context: ResearchCampaignContextV1, evaluation_contract_hash: str) -> str | None:
        contract = self.store.load_evaluation_contract(evaluation_contract_hash)
        projected_metric_calls = context.usage.metric_calls + contract.repetitions
        if projected_metric_calls > context.contract.budget.max_metric_calls:
            return "protected evaluation proposal exceeds metric-call budget"
        return None

    def _execute_candidate_evaluation(
        self,
        state: ResearchCampaignStateV1,
        context: ResearchCampaignContextV1,
        spec,
    ) -> CandidateEvaluationResultV1:
        assert self.candidate_evaluation_port is not None
        stored = self.store.find_candidate_evaluation_by_fingerprint(state.campaign_id, spec.fingerprint)
        if stored is None:
            self.store.register_candidate_evaluation(spec)
            stored = self.store.find_candidate_evaluation_by_fingerprint(state.campaign_id, spec.fingerprint)
        if stored is None:
            raise ResearchCampaignControllerError("registered candidate evaluation cannot be reloaded")
        # On restart preserve the original immutable spec (including created_at/evaluation_id)
        # instead of reconstructing it with a new clock value.
        spec = stored.spec
        prior = stored.result
        if prior is not None and prior.is_terminal:
            return prior
        budget_reason = self._protected_budget_reason(context, spec.evaluation_contract_hash)
        if budget_reason is not None:
            raise ResearchCampaignControllerError(budget_reason)
        resume = prior is not None and prior.status is CandidateEvaluationStatusV1.RUNNING
        started_at = prior.started_at if resume and prior is not None else timestamp(self.clock(), "clock")
        if prior is None or prior.status is CandidateEvaluationStatusV1.PLANNED:
            self.store.save_candidate_evaluation_result(
                CandidateEvaluationResultV1(
                    evaluation_id=spec.evaluation_id,
                    campaign_id=spec.campaign_id,
                    status=CandidateEvaluationStatusV1.RUNNING,
                    started_at=started_at,
                )
            )
        result = self.candidate_evaluation_port.evaluate(spec, resume=resume)
        if not isinstance(result, CandidateEvaluationResultV1):
            raise ResearchCampaignControllerError("CandidateEvaluationPort must return CandidateEvaluationResultV1")
        if result.evaluation_id != spec.evaluation_id or result.campaign_id != spec.campaign_id:
            raise ResearchCampaignControllerError("candidate evaluation result identity mismatch")
        if not result.is_terminal:
            raise ResearchCampaignControllerError("CandidateEvaluationPort must return terminal result")
        if resume and result.started_at != started_at:
            result = replace(result, started_at=started_at)
        self.store.save_candidate_evaluation_result(result)
        return result

    def _tick_gate_confirmation(self, state_before, state, context, top_k):
        assert self.candidate_evaluation_policy is not None
        top_ids = frozenset(item.spec.experiment_id for item in top_k)
        gate_rows = self.store.list_candidate_evaluations(
            state.campaign_id, surface=CandidateEvaluationSurfaceV1.GATE, limit=100_000
        )
        by_source = {
            item.spec.candidate.source_experiment_id: item
            for item in gate_rows
            if item.spec.candidate.source_experiment_id in top_ids
        }
        for rank, candidate in enumerate(top_k, start=1):
            stored = by_source.get(candidate.spec.experiment_id)
            if stored is not None and stored.result is not None and stored.result.is_terminal:
                continue
            spec = self.candidate_evaluation_policy.gate_spec(
                candidate, rank=rank, created_at=self.clock()
            )
            try:
                result = self._execute_candidate_evaluation(state, context, spec)
            except ResearchCampaignControllerError as exc:
                if "metric-call budget" in str(exc):
                    exhausted = self._transition(
                        state, ResearchCampaignStatusV1.BUDGET_EXHAUSTED, reason=str(exc)
                    )
                    return CampaignTickResultV1(
                        state.campaign_id,
                        CampaignTickDispositionV1.BUDGET_EXHAUSTED,
                        state_before,
                        exhausted,
                        experiment_id=candidate.spec.experiment_id,
                        reason=str(exc),
                    )
                raise
            # Re-read after persistence so the same path is used after restart.
            gate_rows = self.store.list_candidate_evaluations(
                state.campaign_id, surface=CandidateEvaluationSurfaceV1.GATE, limit=100_000
            )
            if any(
                item.spec.candidate.source_experiment_id in top_ids
                and (item.result is None or not item.result.is_terminal)
                for item in gate_rows
            ) or len({
                item.spec.candidate.source_experiment_id
                for item in gate_rows
                if item.spec.candidate.source_experiment_id in top_ids
                and item.result is not None
                and item.result.is_terminal
            }) < len(top_ids):
                return CampaignTickResultV1(
                    state.campaign_id,
                    CampaignTickDispositionV1.EXECUTED,
                    state_before,
                    state,
                    experiment_id=candidate.spec.experiment_id,
                    reused_existing=False,
                    reason=f"GATE evaluation {result.status.value} for immutable candidate",
                )
            break

        gate_rows = self.store.list_candidate_evaluations(
            state.campaign_id, surface=CandidateEvaluationSurfaceV1.GATE, limit=100_000
        )
        winner = self.candidate_evaluation_policy.best_gate_evaluation(
            gate_rows,
            direction=context.contract.metric_direction,
            allowed_source_experiment_ids=top_ids,
        )
        if winner is None:
            repair = self._maybe_schedule_gate_repair(
                state_before, state, context, top_k, gate_rows
            )
            if repair is not None:
                return repair
            stalled = self._transition(
                state,
                ResearchCampaignStatusV1.STAGNATED,
                reason="all Top-K candidates failed protected GATE confirmation",
            )
            return CampaignTickResultV1(
                state.campaign_id,
                CampaignTickDispositionV1.STAGNATED,
                state_before,
                stalled,
                reason=stalled.reason,
            )
        confirmed_id = winner.spec.candidate.source_experiment_id
        advanced = self._transition(
            state,
            ResearchCampaignStatusV1.FINAL_EVALUATION,
            champion_experiment_id=confirmed_id,
            reason=f"Top-K GATE confirmation selected {confirmed_id}",
        )
        return CampaignTickResultV1(
            state.campaign_id,
            CampaignTickDispositionV1.PHASE_ADVANCED,
            state_before,
            advanced,
            experiment_id=confirmed_id,
            reason=advanced.reason,
        )

    def _maybe_schedule_gate_repair(self, state_before, state, context, top_k, gate_rows):
        if self.repair_policy is None or self.repair_planning_port is None:
            return None
        proposals = self.store.list_repair_proposals(state.campaign_id, limit=100_000)
        already_repaired = frozenset(item.evidence.gate_evaluation_id for item in proposals)
        top_ids = frozenset(item.spec.experiment_id for item in top_k)
        evidence = self.repair_policy.select_failure(
            tuple(gate_rows),
            allowed_source_experiment_ids=top_ids,
            already_repaired_evaluation_ids=already_repaired,
        )
        if evidence is None:
            return None
        if len(proposals) >= self.repair_policy.max_total_repairs:
            stalled = self._transition(
                state,
                ResearchCampaignStatusV1.STAGNATED,
                reason="failure-driven repair budget exhausted",
            )
            return CampaignTickResultV1(
                state.campaign_id,
                CampaignTickDispositionV1.STAGNATED,
                state_before,
                stalled,
                experiment_id=evidence.source_experiment_id,
                reason=stalled.reason,
            )
        source = next(
            (item for item in self.store.list_experiments(state.campaign_id, limit=100_000)
             if item.spec.experiment_id == evidence.source_experiment_id),
            None,
        )
        if source is None:
            raise ResearchCampaignControllerError("repair evidence references missing source experiment")
        repair_depth = self.store.repair_depth_for_experiment(
            state.campaign_id, evidence.source_experiment_id
        ) + 1
        if repair_depth > self.repair_policy.max_depth:
            stalled = self._transition(
                state,
                ResearchCampaignStatusV1.STAGNATED,
                reason=f"failure-driven repair depth {repair_depth} exceeds max_depth={self.repair_policy.max_depth}",
            )
            return CampaignTickResultV1(
                state.campaign_id,
                CampaignTickDispositionV1.STAGNATED,
                state_before,
                stalled,
                experiment_id=evidence.source_experiment_id,
                reason=stalled.reason,
            )
        created_at = timestamp(self.clock(), "clock")
        proposal = self.repair_planning_port.propose(
            context,
            evidence,
            source,
            repair_depth=repair_depth,
            created_at=created_at,
        )
        if proposal is None:
            stalled = self._transition(
                state,
                ResearchCampaignStatusV1.STAGNATED,
                reason="repair planner declined repairable GATE failure",
            )
            return CampaignTickResultV1(
                state.campaign_id,
                CampaignTickDispositionV1.STAGNATED,
                state_before,
                stalled,
                experiment_id=evidence.source_experiment_id,
                reason=stalled.reason,
            )
        self._validate_repair_proposal(context, evidence, source, proposal, repair_depth)
        projected_reason = self._projected_budget_reason(context, proposal.experiment)
        if projected_reason is not None:
            exhausted = self._transition(
                state, ResearchCampaignStatusV1.BUDGET_EXHAUSTED, reason=projected_reason
            )
            return CampaignTickResultV1(
                state.campaign_id,
                CampaignTickDispositionV1.BUDGET_EXHAUSTED,
                state_before,
                exhausted,
                experiment_id=evidence.source_experiment_id,
                reason=projected_reason,
            )
        registration = self.store.register_repair_proposal(proposal)
        canonical = self.store.load_repair_proposal(registration.canonical_repair_id)
        advanced = self._transition(
            state,
            ResearchCampaignStatusV1.SEARCHING,
            reason=(
                f"repair {canonical.repair_id} registered from failed GATE "
                f"{canonical.evidence.gate_evaluation_id}"
            ),
        )
        return CampaignTickResultV1(
            state.campaign_id,
            CampaignTickDispositionV1.PHASE_ADVANCED,
            state_before,
            advanced,
            experiment_id=canonical.experiment.experiment_id,
            reused_existing=registration.reused_existing,
            reason=advanced.reason,
        )

    @staticmethod
    def _validate_repair_proposal(context, evidence, source, proposal, repair_depth) -> None:
        if not isinstance(proposal, RepairProposalV1):
            raise ResearchCampaignControllerError("RepairPlanningPort must return RepairProposalV1 or None")
        if proposal.evidence != evidence:
            raise ResearchCampaignControllerError("repair planner must preserve exact durable GATE failure evidence")
        if proposal.repair_depth != repair_depth:
            raise ResearchCampaignControllerError("repair planner changed controller-owned repair_depth")
        if source.spec.hypothesis_id not in proposal.hypothesis.parent_hypothesis_ids:
            raise ResearchCampaignControllerError("repair hypothesis must derive from source hypothesis")
        spec = proposal.experiment
        if spec.parent_experiment_ids.count(source.spec.experiment_id) != 1:
            raise ResearchCampaignControllerError("repair experiment must reference source experiment exactly once")
        if spec.code_base_commit != evidence.source_candidate_commit:
            raise ResearchCampaignControllerError("repair experiment must start from exact failed candidate commit")
        if spec.dataset_fingerprint != source.spec.dataset_fingerprint:
            raise ResearchCampaignControllerError("repair experiment must preserve source dataset fingerprint")
        if spec.evaluation_contract_hash != source.spec.evaluation_contract_hash:
            raise ResearchCampaignControllerError("repair experiment must preserve source EvaluationContractHash")
        if spec.fingerprint == source.spec.fingerprint:
            raise ResearchCampaignControllerError("repair experiment must have new logical fingerprint")
        if spec.fingerprint in context.known_experiment_fingerprints:
            raise ResearchCampaignControllerError("repair experiment duplicates known logical experiment")
        if context.contract.allowed_models and spec.model_id not in context.contract.allowed_models:
            raise ResearchCampaignControllerError("repair experiment model is not allowed by ResearchContract")

    def _tick_final_evaluation(self, state_before, state, context, top_k):
        assert self.candidate_evaluation_policy is not None
        champion_id = state.champion_experiment_id
        if champion_id is None:
            raise ResearchCampaignControllerError("FINAL_EVALUATION lost confirmed champion")
        candidate = next((item for item in top_k if item.spec.experiment_id == champion_id), None)
        if candidate is None:
            # The confirmed champion may have been outside a freshly truncated top_k only if
            # durable state or ranking inputs were corrupted/changed.
            raise ResearchCampaignControllerError("confirmed champion is no longer in immutable DEV Top-K")
        gate_rows = self.store.list_candidate_evaluations(
            state.campaign_id, surface=CandidateEvaluationSurfaceV1.GATE, limit=100_000
        )
        gate = next(
            (
                item for item in gate_rows
                if item.spec.candidate.source_experiment_id == champion_id
                and item.result is not None
                and item.result.status is CandidateEvaluationStatusV1.SCORED
            ),
            None,
        )
        if gate is None:
            raise ResearchCampaignControllerError("FINAL_EVALUATION requires scored GATE confirmation")
        spec = self.candidate_evaluation_policy.test_spec(
            candidate,
            gate_evaluation_id=gate.spec.evaluation_id,
            created_at=self.clock(),
        )
        try:
            result = self._execute_candidate_evaluation(state, context, spec)
        except ResearchCampaignControllerError as exc:
            if "metric-call budget" in str(exc):
                exhausted = self._transition(
                    state, ResearchCampaignStatusV1.BUDGET_EXHAUSTED, reason=str(exc)
                )
                return CampaignTickResultV1(
                    state.campaign_id,
                    CampaignTickDispositionV1.BUDGET_EXHAUSTED,
                    state_before,
                    exhausted,
                    experiment_id=champion_id,
                    reason=str(exc),
                )
            raise
        if result.status is not CandidateEvaluationStatusV1.SCORED:
            stalled = self._transition(
                state,
                ResearchCampaignStatusV1.STAGNATED,
                reason=f"final TEST evaluation ended as {result.status.value}; candidate remains immutable",
            )
            return CampaignTickResultV1(
                state.campaign_id,
                CampaignTickDispositionV1.STAGNATED,
                state_before,
                stalled,
                experiment_id=champion_id,
                reason=stalled.reason,
            )
        completed = self._transition(
            state,
            ResearchCampaignStatusV1.COMPLETED,
            champion_experiment_id=champion_id,
            reason=f"final TEST evaluation scored immutable champion {champion_id}",
        )
        return CampaignTickResultV1(
            state.campaign_id,
            CampaignTickDispositionV1.COMPLETED,
            state_before,
            completed,
            experiment_id=champion_id,
            reason=completed.reason,
        )

    def _persist_campaign_terminal_decision(
        self,
        context: ResearchCampaignContextV1,
        reason: ResearchStopReasonV1,
        explanation: str,
    ) -> ResearchStopDecisionV1 | None:
        if self.stop_policy is None:
            return None
        campaign_id = context.state.campaign_id
        insights = self.store.list_typed_insights(campaign_id, limit=100_000)
        strategy_refs = self.store.list_strategy_decision_fingerprints(
            campaign_id,
            limit=100_000,
        )
        budget = self._strategy_budget_snapshot(context)
        cycle = stable_sha256({
            "schema_version": 1,
            "campaign_id": campaign_id,
            "reason": reason.value,
            "champion": context.state.champion_experiment_id,
            "generation": context.state.generation,
            "budget": budget.fingerprint,
            "strategy_refs": strategy_refs,
        })
        existing = self.store.load_stop_decision_for_cycle(campaign_id, cycle)
        if existing is not None:
            return existing
        evidence_refs = _bounded_stop_decision_refs(
            tuple(f"insight:{item.insight_id}" for item in insights)
        )
        bounded_strategy_refs = _bounded_stop_decision_refs(strategy_refs)
        decision = ResearchStopDecisionV1(
            campaign_id, reason, explanation,
            evidence_refs, budget,
            bounded_strategy_refs, timestamp(self.clock(), "clock"),
        )
        self.store.save_stop_decision(decision, cycle_fingerprint=cycle)
        return decision
    def _campaign_stop_before_planning(
        self,
        state_before: ResearchCampaignStateV1,
        state: ResearchCampaignStateV1,
        context: ResearchCampaignContextV1,
        *,
        experiments: tuple[StoredExperimentV1, ...] | None = None,
        typed_insights: tuple[ResearchInsightRecordV1, ...] | None = None,
    ) -> CampaignTickResultV1 | None:
        if experiments is None:
            experiments = self.store.list_experiments(state.campaign_id, limit=100_000)
        insights = (
            self.store.list_typed_insights(state.campaign_id, limit=100_000)
            if typed_insights is None
            else typed_insights
        )
        strategy_refs = self.store.list_strategy_decision_fingerprints(
            state.campaign_id,
            limit=100_000,
        )
        scientific_metrics = tuple(float(item.result.primary_metric) for item in experiments
            if item.result is not None and item.result.status is ExperimentStatusV1.SUCCEEDED and item.result.primary_metric is not None)
        cycle = stable_sha256({"schema_version": 1, "campaign_id": state.campaign_id,
            "contract": context.contract.contract_hash,
            "champion": state.champion_experiment_id,
            "experiments": [{"spec": item.spec.fingerprint, "result": None if item.result is None else stable_sha256(item.result.to_dict())}
                            for item in experiments],
            "insights": [item.fingerprint for item in insights],
            "budget": self._strategy_budget_snapshot(context).fingerprint})
        decision = self.store.load_stop_decision_for_cycle(state.campaign_id, cycle)
        if decision is None:
            raw_target = context.contract.completion_policy.get("primary_metric_target")
            if raw_target is not None and (isinstance(raw_target, bool) or not isinstance(raw_target, (int, float))):
                raise ResearchCampaignControllerError("completion_policy.primary_metric_target must be numeric")
            assert self.stop_policy is not None
            evidence_refs = _bounded_stop_decision_refs(
                tuple(f"insight:{item.insight_id}" for item in insights)
            )
            bounded_strategy_refs = _bounded_stop_decision_refs(strategy_refs)
            decision = self.stop_policy.decide(campaign_id=state.campaign_id, budget=self._strategy_budget_snapshot(context),
                usage=context.usage, created_at=timestamp(self.clock(), "clock"),
                evidence_refs=evidence_refs, strategy_refs=bounded_strategy_refs,
                valid_dev_metrics=scientific_metrics, metric_direction=context.contract.metric_direction.value,
                objective_target=None if raw_target is None else float(raw_target))
            self.store.save_stop_decision(decision, cycle_fingerprint=cycle)
        if decision.reason is ResearchStopReasonV1.CONTINUE:
            return None
        stopped_state = self._transition(state, ResearchCampaignStatusV1.CONFIRMING_TOP_K,
                                         reason=f"Phase23 autonomous stop: {decision.reason.value}: {decision.explanation}")
        return CampaignTickResultV1(state.campaign_id, CampaignTickDispositionV1.STOPPED, state_before, stopped_state,
                                    reason=decision.explanation)

    def _strategy_planning_applicable(
        self,
        state: ResearchCampaignStateV1,
        context: ResearchCampaignContextV1,
    ) -> bool:
        return (
            self.strategy_planner is not None
            and context.champion is not None
            and state.status
            in {
                ResearchCampaignStatusV1.SEARCHING,
                ResearchCampaignStatusV1.STAGNATED,
            }
        )

    def _strategy_budget_snapshot(
        self,
        context: ResearchCampaignContextV1,
    ) -> ResearchStrategyBudgetV1:
        raw_risk = context.contract.constraints.get(
            "strategy_max_risk_class",
            ResearchRiskClassV1.LOW.value,
        )
        try:
            max_risk = ResearchRiskClassV1(str(raw_risk).upper())
        except ValueError as exc:
            raise ResearchCampaignControllerError(
                "constraints.strategy_max_risk_class must be LOW, MEDIUM, or HIGH"
            ) from exc
        return ResearchStrategyBudgetV1(
            remaining_experiments=max(
                0,
                context.contract.budget.max_experiments
                - context.usage.experiment_count,
            ),
            remaining_model_calls=0,
            remaining_tool_calls=0,
            wall_clock_class="CAMPAIGN_BOUNDED",
            allowed_mutation_scope=context.contract.allowed_mutation_scope,
            protected_surfaces=("GATE", "TEST"),
            max_risk_class=max_risk,
        )

    def _strategy_planning_context(
        self,
        context: ResearchCampaignContextV1,
        *,
        experiments: tuple[StoredExperimentV1, ...] | None = None,
        typed_insights: tuple[ResearchInsightRecordV1, ...] | None = None,
    ) -> ResearchStrategyPlanningContextV1:
        champion = context.champion
        if champion is None:
            raise ResearchCampaignControllerError(
                "strategy planning requires durable champion"
            )
        if experiments is None:
            experiments = self.store.list_experiments(
                context.state.campaign_id,
                limit=100_000,
            )
        target = ResearchBeliefTargetV1(
            scope=self.belief_consolidator.scope_for_experiment(champion),
            experiment_id=champion.spec.experiment_id,
            code_base_commit=champion.spec.code_base_commit,
            strict_code_base=False,
        )
        belief_set = self.belief_consolidator.consolidate(
            campaign_id=context.state.campaign_id,
            insights=(
                self.store.list_typed_insights(
                    context.state.campaign_id,
                    limit=100_000,
                )
                if typed_insights is None
                else typed_insights
            ),
            experiments=experiments,
            target=target,
        )
        historical_proposals = self.store.list_strategy_selected_proposals(
            context.state.campaign_id,
            limit=100_000,
        )
        return ResearchStrategyPlanningContextV1(
            campaign_context=context,
            belief_set=belief_set,
            experiments=experiments,
            historical_proposals=historical_proposals,
            budget_snapshot=self._strategy_budget_snapshot(context),
        )

    def _active_strategy_checkpoints(
        self,
        campaign_id: str,
    ) -> tuple[ResearchStrategyCheckpointV1, ...]:
        # Restart reconciliation only needs checkpoints that can still own
        # unfinished work. Historical terminal checkpoints remain durable but do
        # not need full decode plus a full experiment-ledger reconstruction on
        # every normal search tick.
        return self.store.list_strategy_recovery_checkpoints(
            campaign_id,
            limit=100_000,
        )

    def _reconcile_strategy_checkpoint(
        self,
        state: ResearchCampaignStateV1,
    ) -> tuple[ResearchCampaignStateV1, ResearchStrategyCheckpointV1 | None]:
        if self.strategy_planner is None:
            if state.pending_strategy_decision_fingerprint is not None:
                raise ResearchCampaignControllerError(
                    "campaign has strategy decision reference but no strategy planner"
                )
            return state, None
        if state.status not in {
            ResearchCampaignStatusV1.SEARCHING,
            ResearchCampaignStatusV1.STAGNATED,
        }:
            if state.pending_strategy_decision_fingerprint is not None:
                raise ResearchCampaignControllerError(
                    "strategy decision reference exists outside research search phase"
                )
            return state, None

        if state.pending_strategy_decision_fingerprint is not None:
            checkpoint = self.store.load_strategy_checkpoint_by_fingerprint(
                state.campaign_id,
                state.pending_strategy_decision_fingerprint,
            )
            if checkpoint.stage is ResearchStrategyCheckpointStageV1.EXPERIMENT_PLANNED:
                stored = self.store.find_by_fingerprint(
                    state.campaign_id,
                    checkpoint.planned_experiment.fingerprint,
                )
                if stored is None:
                    raise ResearchCampaignControllerError(
                        "strategy state reference points to missing planned experiment"
                    )
                if stored.result is not None and stored.result.is_terminal:
                    raise ResearchCampaignControllerError(
                        "terminal strategy experiment was not reconciled before next planning cycle"
                    )
            return state, checkpoint

        active = self._active_strategy_checkpoints(state.campaign_id)
        if not active:
            return state, None
        if len(active) > 1:
            raise ResearchCampaignControllerError(
                "multiple active strategy checkpoints make restart ownership ambiguous"
            )
        checkpoint = active[0]
        rebound = self._transition(
            state,
            state.status,
            pending_strategy_decision_fingerprint=checkpoint.decision_fingerprint,
            reason=(
                "reconciled orphan durable R8 strategy decision "
                f"{checkpoint.decision_fingerprint}"
            ),
        )
        return rebound, checkpoint

    def _strategy_reference_matches_experiment(
        self,
        state: ResearchCampaignStateV1,
        experiment_id: str,
    ) -> bool:
        fingerprint = state.pending_strategy_decision_fingerprint
        if fingerprint is None:
            return False
        checkpoint = self.store.load_strategy_checkpoint_by_fingerprint(
            state.campaign_id,
            fingerprint,
        )
        return checkpoint.planned_experiment.experiment_id == experiment_id

    def _advance_without_strategy_selection(
        self,
        state_before: ResearchCampaignStateV1,
        state: ResearchCampaignStateV1,
        context: ResearchCampaignContextV1,
    ) -> CampaignTickResultV1:
        if state.status not in {
            ResearchCampaignStatusV1.SEARCHING,
            ResearchCampaignStatusV1.STAGNATED,
        }:
            raise ResearchCampaignControllerError(
                "strategy exhaustion is only valid during research search phase"
            )
        reason = "R8 strategy planner produced no admissible selected proposal"
        disposition = CampaignTickDispositionV1.PHASE_ADVANCED
        if self.stop_policy is not None:
            self._persist_campaign_terminal_decision(
                context, ResearchStopReasonV1.NO_ADMISSIBLE_PROPOSALS, reason,
            )
            disposition = CampaignTickDispositionV1.STOPPED
        advanced = self._transition(
            state,
            ResearchCampaignStatusV1.CONFIRMING_TOP_K,
            reason=reason,
        )
        return CampaignTickResultV1(
            state.campaign_id,
            disposition,
            state_before,
            advanced,
            reason=reason,
        )
    def _pending_experiment(self, campaign_id: str) -> StoredExperimentV1 | None:
        pending = self.store.list_experiments(
            campaign_id,
            statuses=(ExperimentStatusV1.PLANNED, ExperimentStatusV1.RUNNING),
            limit=2,
        )
        if len(pending) > 1:
            raise ResearchCampaignControllerError("sequential ResearchCampaign has multiple pending experiments")
        return None if not pending else pending[0]

    @staticmethod
    def _validate_proposal(context: ResearchCampaignContextV1, spec: ExperimentSpecV1) -> None:
        if not isinstance(spec, ExperimentSpecV1):
            raise ResearchCampaignControllerError("SearchPolicy must return ExperimentSpecV1 or None")
        if spec.campaign_id != context.state.campaign_id:
            raise ResearchCampaignControllerError("proposal campaign_id mismatch")

    @staticmethod
    def _execution_for_evaluation(
        execution: ExperimentExecutionOutcomeV1,
        context: ResearchCampaignContextV1,
    ) -> ExperimentExecutionOutcomeV1:
        reserved = {"research_evaluation_surface", "research_incumbent_primary_metric"}
        collision = reserved.intersection(execution.telemetry)
        if collision:
            raise ResearchCampaignControllerError(
                f"execution telemetry must not forge controller-owned evaluation hints: {sorted(collision)}"
            )
        if context.state.status is ResearchCampaignStatusV1.CONFIRMING_TOP_K:
            surface = "GATE"
        elif context.state.status is ResearchCampaignStatusV1.FINAL_EVALUATION:
            surface = "TEST"
        else:
            surface = "DEV"
        telemetry = dict(execution.telemetry)
        telemetry["research_evaluation_surface"] = surface
        if context.champion is not None and context.champion.result is not None:
            incumbent = context.champion.result.primary_metric
            if incumbent is not None:
                telemetry["research_incumbent_primary_metric"] = incumbent
        return replace(execution, telemetry=telemetry)

    @staticmethod
    def _validate_decision(
        context: ResearchCampaignContextV1,
        observation: ExperimentObservationV1,
        decision: SearchDecisionV1,
    ) -> None:
        state = context.state
        if decision.campaign_id != observation.spec.campaign_id or decision.experiment_id != observation.spec.experiment_id:
            raise ResearchCampaignControllerError("SearchDecision identity does not match observed experiment")
        if decision.action is SearchDecisionActionV1.UPDATE_CHAMPION:
            if observation.execution.status is not ExperimentExecutionStatusV1.COMPLETED:
                raise ResearchCampaignControllerError("only successfully executed experiment can become champion")
            if observation.evaluation is None or observation.evaluation.status is not ExperimentEvaluationStatusV1.SCORED:
                raise ResearchCampaignControllerError("only scored experiment can become champion")
            if decision.incumbent_experiment_id != state.champion_experiment_id:
                raise ResearchCampaignControllerError("champion update must name the current incumbent exactly")
            if context.champion is not None and (
                context.champion.spec.evaluation_contract_hash != observation.spec.evaluation_contract_hash
            ):
                raise ResearchCampaignControllerError(
                    "champion replacement requires identical EvaluationContractHash"
                )
            require_gate = context.contract.completion_policy.get("require_gate_for_champion", False)
            if not isinstance(require_gate, bool):
                raise ResearchCampaignControllerError(
                    "completion_policy.require_gate_for_champion must be bool"
                )
            if require_gate:
                telemetry = observation.evaluation.telemetry
                if telemetry.get("integrity_validated") is not True:
                    raise ResearchCampaignControllerError(
                        "champion update requires independently validated evaluator integrity"
                    )
                if telemetry.get("admission_passed") is not True or telemetry.get("admission_surface") != "GATE":
                    raise ResearchCampaignControllerError(
                        "champion update requires independent GATE admission"
                    )

    @staticmethod
    def _merge_telemetry(left: Mapping[str, object], right: Mapping[str, object]) -> dict[str, object]:
        merged = dict(left)
        additive = {"gpu_seconds", "api_cost", "metric_calls"}
        for key, value in right.items():
            if key not in merged:
                merged[key] = value
                continue
            if key in additive:
                a, b = merged[key], value
                if isinstance(a, bool) or isinstance(b, bool) or not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
                    raise ResearchCampaignControllerError(f"telemetry.{key} must be numeric")
                merged[key] = a + b
                continue
            if merged[key] != value:
                raise ResearchCampaignControllerError(f"conflicting telemetry value for {key}")
        return merged

    @staticmethod
    def _materialize_result(
        observation: ExperimentObservationV1,
        decision: SearchDecisionV1,
    ) -> ExperimentResultV1:
        execution = observation.execution
        evaluation = observation.evaluation
        status_map = {
            ExperimentExecutionStatusV1.FAILED: ExperimentStatusV1.FAILED,
            ExperimentExecutionStatusV1.CRASHED: ExperimentStatusV1.CRASHED,
            ExperimentExecutionStatusV1.PRUNED: ExperimentStatusV1.PRUNED,
            ExperimentExecutionStatusV1.CANCELLED: ExperimentStatusV1.CANCELLED,
        }
        status = status_map.get(execution.status, ExperimentStatusV1.SUCCEEDED)
        failure_kind = execution.failure_kind
        primary_metric = None
        secondary_metrics: Mapping[str, float] = {}
        variance = None
        telemetry = dict(execution.telemetry)
        if execution.status is ExperimentExecutionStatusV1.COMPLETED:
            assert evaluation is not None
            telemetry = ResearchCampaignControllerV1._merge_telemetry(telemetry, evaluation.telemetry)
            if evaluation.status is ExperimentEvaluationStatusV1.SCORED:
                status = ExperimentStatusV1.SUCCEEDED
                primary_metric = evaluation.primary_metric
                secondary_metrics = evaluation.secondary_metrics
                variance = evaluation.metric_variance
            elif evaluation.status is ExperimentEvaluationStatusV1.INVALID:
                status = ExperimentStatusV1.INVALID
                failure_kind = evaluation.failure_kind or ExperimentFailureKindV1.INVALID_METRIC
                primary_metric = evaluation.primary_metric
                secondary_metrics = evaluation.secondary_metrics
                variance = evaluation.metric_variance
            else:
                status = ExperimentStatusV1.FAILED
                failure_kind = evaluation.failure_kind or ExperimentFailureKindV1.EVALUATION_FAILED
                primary_metric = evaluation.primary_metric
                secondary_metrics = evaluation.secondary_metrics
                variance = evaluation.metric_variance
        return ExperimentResultV1(
            experiment_id=observation.spec.experiment_id,
            campaign_id=observation.spec.campaign_id,
            status=status,
            started_at=execution.started_at,
            ended_at=execution.ended_at,
            primary_metric=primary_metric,
            secondary_metrics=secondary_metrics,
            metric_variance=variance,
            verification_outcome=execution.verification_outcome,
            artifact_refs=execution.artifact_refs,
            stdout_ref=execution.stdout_ref,
            stderr_ref=execution.stderr_ref,
            candidate_commit=execution.candidate_commit,
            telemetry=telemetry,
            failure_kind=failure_kind,
            decision=decision.action,
        )

    @staticmethod
    def _status_after_decision(
        state: ResearchCampaignStateV1,
        action: SearchDecisionActionV1,
        champion_experiment_id: str | None,
    ) -> ResearchCampaignStatusV1:
        if action is SearchDecisionActionV1.STOP:
            if state.status is ResearchCampaignStatusV1.FINAL_EVALUATION:
                return ResearchCampaignStatusV1.COMPLETED
            if state.status is ResearchCampaignStatusV1.CONFIRMING_TOP_K:
                return ResearchCampaignStatusV1.FINAL_EVALUATION
            return ResearchCampaignStatusV1.CONFIRMING_TOP_K
        if state.status is ResearchCampaignStatusV1.CONFIRMING_TOP_K:
            return ResearchCampaignStatusV1.CONFIRMING_TOP_K
        if state.status is ResearchCampaignStatusV1.FINAL_EVALUATION:
            return ResearchCampaignStatusV1.FINAL_EVALUATION
        return (
            ResearchCampaignStatusV1.SEARCHING
            if champion_experiment_id is not None
            else ResearchCampaignStatusV1.BASELINE_REQUIRED
        )

    def _reconcile_durable_outcome(
        self,
        campaign_id: str,
        state: ResearchCampaignStateV1,
    ) -> ResearchCampaignStateV1:
        # SearchDecision is authoritative only while reconciling the DEV search phase.
        # Once protected confirmation starts, campaign_state owns the confirmed champion;
        # replaying an older DEV decision here would silently roll it back after restart.
        search_resume = state.resume_status in {
            ResearchCampaignStatusV1.BASELINE_REQUIRED,
            ResearchCampaignStatusV1.SEARCHING,
        }
        if state.status not in {
            ResearchCampaignStatusV1.BASELINE_REQUIRED,
            ResearchCampaignStatusV1.SEARCHING,
        } and not search_resume:
            return state
        decisions = self.store.list_decisions(campaign_id, limit=100_000)
        if not decisions:
            return state
        latest = decisions[-1]
        durable_champion = self.store.champion_experiment_id(campaign_id)
        clear_strategy = self._strategy_reference_matches_experiment(
            state,
            latest.experiment_id,
        )
        if (
            state.last_experiment_id == latest.experiment_id
            and state.champion_experiment_id == durable_champion
            and not clear_strategy
        ):
            return state
        next_status = self._status_after_decision(state, latest.action, durable_champion)
        return self._transition(
            state,
            next_status,
            champion_experiment_id=durable_champion,
            last_experiment_id=latest.experiment_id,
            reason="reconciled durable terminal experiment outcome",
            clear_pending_strategy_decision=clear_strategy,
        )

    def _reconcile_durable_repair(
        self,
        campaign_id: str,
        state: ResearchCampaignStateV1,
    ) -> ResearchCampaignStateV1:
        if self.repair_policy is None or state.status is not ResearchCampaignStatusV1.CONFIRMING_TOP_K:
            return state
        pending = []
        for proposal in self.store.list_repair_proposals(campaign_id, limit=100_000):
            stored = self.store.find_by_fingerprint(campaign_id, proposal.experiment.fingerprint)
            if stored is None:
                raise ResearchCampaignControllerError("durable repair proposal lost repair experiment")
            if stored.result is None or stored.result.status in {
                ExperimentStatusV1.PLANNED,
                ExperimentStatusV1.RUNNING,
            }:
                pending.append(proposal)
        if not pending:
            return state
        if len(pending) > 1:
            raise ResearchCampaignControllerError("sequential campaign has multiple pending repair experiments")
        proposal = pending[0]
        return self._transition(
            state,
            ResearchCampaignStatusV1.SEARCHING,
            reason=f"reconciled durable repair proposal {proposal.repair_id} after restart",
        )

    def _advance_without_proposal(
        self,
        state_before: ResearchCampaignStateV1,
        state: ResearchCampaignStateV1,
    ) -> CampaignTickResultV1:
        if state.status is ResearchCampaignStatusV1.BASELINE_REQUIRED:
            next_status = ResearchCampaignStatusV1.STAGNATED
            disposition = CampaignTickDispositionV1.STAGNATED
            reason = "search policy produced no baseline experiment"
        elif state.status in {ResearchCampaignStatusV1.SEARCHING, ResearchCampaignStatusV1.STAGNATED}:
            next_status = ResearchCampaignStatusV1.CONFIRMING_TOP_K
            disposition = CampaignTickDispositionV1.PHASE_ADVANCED
            reason = "search policy exhausted search proposals"
        elif state.status is ResearchCampaignStatusV1.CONFIRMING_TOP_K:
            next_status = ResearchCampaignStatusV1.FINAL_EVALUATION
            disposition = CampaignTickDispositionV1.PHASE_ADVANCED
            reason = "top-k confirmation proposals exhausted"
        elif state.status is ResearchCampaignStatusV1.FINAL_EVALUATION:
            next_status = ResearchCampaignStatusV1.COMPLETED
            disposition = CampaignTickDispositionV1.COMPLETED
            reason = "final evaluation proposals exhausted"
        else:
            next_status = ResearchCampaignStatusV1.STAGNATED
            disposition = CampaignTickDispositionV1.STAGNATED
            reason = "search policy produced no actionable experiment"
        transitioned = self._transition(state, next_status, reason=reason)
        return CampaignTickResultV1(
            state.campaign_id,
            disposition,
            state_before,
            transitioned,
            reason=reason,
        )

    def _sync_state_from_terminal(
        self,
        state: ResearchCampaignStateV1,
        stored: StoredExperimentV1,
    ) -> ResearchCampaignStateV1:
        result = stored.result
        assert result is not None and result.is_terminal
        durable = [
            item for item in self.store.list_decisions(state.campaign_id, limit=100_000)
            if item.experiment_id == stored.spec.experiment_id
        ]
        if len(durable) != 1 or durable[0].action is not result.decision:
            raise ResearchCampaignControllerError(
                "terminal experiment cannot be reused without exactly one matching durable SearchDecision"
            )
        champion = state.champion_experiment_id
        if result.decision is SearchDecisionActionV1.UPDATE_CHAMPION:
            champion = stored.spec.experiment_id
        status = self._status_after_decision(state, result.decision or SearchDecisionActionV1.KEEP, champion)
        return self._transition(
            state,
            status,
            champion_experiment_id=champion,
            last_experiment_id=stored.spec.experiment_id,
            reason="reused terminal experiment",
            clear_pending_strategy_decision=(
                self._strategy_reference_matches_experiment(
                    state,
                    stored.spec.experiment_id,
                )
            ),
        )

# Verified compatibility aliases for historical Python interfaces.
ResearchCampaignControllerV1._persist_phase23_terminal_decision = ResearchCampaignControllerV1._persist_campaign_terminal_decision
ResearchCampaignControllerV1._phase23_stop_before_planning = ResearchCampaignControllerV1._campaign_stop_before_planning

"""Phase 22.R8.7 durable strategy decision and restart-safe planning.

R8.7 persists an exact R8.5 decision together with the selected proposal and a
deterministically materialized HypothesisV1/ExperimentSpecV1 before physical
execution. Restart reopens the same decision fingerprint and exact planned
experiment; it never regenerates proposals or re-ranks silently.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Mapping, Protocol, Sequence

from .models import ExperimentSpecV1, HypothesisModeV1, HypothesisStatusV1, HypothesisV1
from .search import StrategyDimensionTargetV1, StrategySpaceV1
from .serialization import canonical_json, plain, safe_id, sha256_hex, stable_sha256, timestamp
from .store import StoredExperimentV1
from .strategy_contracts import ResearchProposalTargetKindV1, ResearchProposalV1
from .strategy_selection import ResearchPortfolioSelectionV1


class DurableStrategyDecisionError(RuntimeError):
    pass


class ResearchStrategyCheckpointStageV1(StrEnum):
    DECISION_PERSISTED = "DECISION_PERSISTED"
    EXPERIMENT_PLANNED = "EXPERIMENT_PLANNED"


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
class ResearchStrategyCheckpointV1:
    selection: ResearchPortfolioSelectionV1
    selected_proposal: ResearchProposalV1
    planned_hypothesis: HypothesisV1
    planned_experiment: ExperimentSpecV1
    stage: ResearchStrategyCheckpointStageV1
    registered_experiment_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.selection, ResearchPortfolioSelectionV1):
            raise ValueError("selection must be ResearchPortfolioSelectionV1")
        if not isinstance(self.selected_proposal, ResearchProposalV1):
            raise ValueError("selected_proposal must be ResearchProposalV1")
        if not isinstance(self.planned_hypothesis, HypothesisV1):
            raise ValueError("planned_hypothesis must be HypothesisV1")
        if not isinstance(self.planned_experiment, ExperimentSpecV1):
            raise ValueError("planned_experiment must be ExperimentSpecV1")
        if not isinstance(self.stage, ResearchStrategyCheckpointStageV1):
            raise ValueError("stage must be ResearchStrategyCheckpointStageV1")

        decision = self.selection.decision
        if decision.selected_proposal_id is None:
            raise ValueError("durable strategy checkpoint requires selected proposal")
        if decision.selected_proposal_id != self.selected_proposal.proposal_id:
            raise ValueError("selected proposal does not match strategy decision")
        if decision.campaign_id != self.selected_proposal.campaign_id:
            raise ValueError("selected proposal campaign mismatch")
        if self.planned_hypothesis.campaign_id != decision.campaign_id:
            raise ValueError("planned hypothesis campaign mismatch")
        if self.planned_experiment.campaign_id != decision.campaign_id:
            raise ValueError("planned experiment campaign mismatch")
        if self.planned_experiment.hypothesis_id != self.planned_hypothesis.hypothesis_id:
            raise ValueError("planned experiment must reference planned hypothesis")
        if tuple(self.planned_experiment.parent_experiment_ids) != tuple(
            self.selected_proposal.parent_experiment_ids
        ):
            raise ValueError("planned experiment parent lineage mismatch")

        registered = self.registered_experiment_id
        if registered is not None:
            registered = safe_id(registered, "registered_experiment_id")
        object.__setattr__(self, "registered_experiment_id", registered)

        if self.stage is ResearchStrategyCheckpointStageV1.DECISION_PERSISTED:
            if registered is not None:
                raise ValueError("DECISION_PERSISTED cannot have registered_experiment_id")
        elif self.stage is ResearchStrategyCheckpointStageV1.EXPERIMENT_PLANNED:
            if registered is None:
                raise ValueError("EXPERIMENT_PLANNED requires registered_experiment_id")
            if registered != self.planned_experiment.experiment_id:
                raise ValueError("registered experiment must equal exact planned experiment_id")

    @property
    def decision_fingerprint(self) -> str:
        return self.selection.decision.decision_fingerprint

    @property
    def planning_fingerprint(self) -> str:
        return stable_sha256(
            {
                "schema_version": 1,
                "decision_fingerprint": self.decision_fingerprint,
                "selection_fingerprint": self.selection.fingerprint,
                "selected_proposal": self.selected_proposal.to_dict(),
                "planned_hypothesis": self.planned_hypothesis.to_dict(),
                "planned_experiment": self.planned_experiment.to_dict(),
            }
        )

    @property
    def fingerprint(self) -> str:
        return stable_sha256(
            {
                "schema_version": 1,
                "planning_fingerprint": self.planning_fingerprint,
                "stage": self.stage.value,
                "registered_experiment_id": self.registered_experiment_id,
            }
        )

    def mark_experiment_planned(self, experiment_id: str) -> "ResearchStrategyCheckpointV1":
        experiment_id = safe_id(experiment_id, "experiment_id")
        if experiment_id != self.planned_experiment.experiment_id:
            raise DurableStrategyDecisionError(
                "cannot bind strategy checkpoint to a different experiment"
            )
        if self.stage is ResearchStrategyCheckpointStageV1.EXPERIMENT_PLANNED:
            if self.registered_experiment_id != experiment_id:
                raise DurableStrategyDecisionError("planned checkpoint binding mismatch")
            return self
        return replace(
            self,
            stage=ResearchStrategyCheckpointStageV1.EXPERIMENT_PLANNED,
            registered_experiment_id=experiment_id,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "selection": self.selection.to_dict(),
            "selected_proposal": self.selected_proposal.to_dict(),
            "planned_hypothesis": self.planned_hypothesis.to_dict(),
            "planned_experiment": self.planned_experiment.to_dict(),
            "stage": self.stage.value,
            "registered_experiment_id": self.registered_experiment_id,
            "decision_fingerprint": self.decision_fingerprint,
            "planning_fingerprint": self.planning_fingerprint,
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ResearchStrategyCheckpointV1":
        if raw.get("schema_version") != 1:
            raise ValueError("ResearchStrategyCheckpointV1 schema_version must be 1")
        result = cls(
            selection=ResearchPortfolioSelectionV1.from_dict(raw["selection"]),
            selected_proposal=ResearchProposalV1.from_dict(raw["selected_proposal"]),
            planned_hypothesis=HypothesisV1.from_dict(raw["planned_hypothesis"]),
            planned_experiment=ExperimentSpecV1.from_dict(raw["planned_experiment"]),
            stage=ResearchStrategyCheckpointStageV1(str(raw["stage"])),
            registered_experiment_id=(
                None
                if raw.get("registered_experiment_id") is None
                else str(raw["registered_experiment_id"])
            ),
        )
        for key, actual in (
            ("decision_fingerprint", result.decision_fingerprint),
            ("planning_fingerprint", result.planning_fingerprint),
            ("fingerprint", result.fingerprint),
        ):
            expected = raw.get(key)
            if expected is not None and expected != actual:
                raise ValueError(f"ResearchStrategyCheckpointV1 {key} mismatch")
        return result


class ResearchStrategyDurableStorePort(Protocol):
    def save_strategy_checkpoint(self, checkpoint: ResearchStrategyCheckpointV1) -> None: ...
    def load_strategy_checkpoint_by_fingerprint(
        self, campaign_id: str, decision_fingerprint: str
    ) -> ResearchStrategyCheckpointV1: ...
    def mark_strategy_checkpoint_planned(
        self, checkpoint: ResearchStrategyCheckpointV1
    ) -> None: ...
    def register_hypothesis_experiment(self, hypothesis: HypothesisV1, spec: ExperimentSpecV1) -> Any: ...
    def load_experiment_spec(self, experiment_id: str) -> ExperimentSpecV1: ...


@dataclass(frozen=True, slots=True)
class DeterministicStrategyExperimentMaterializerV1:
    """Materialize the exact selected R8 proposal into existing R1 experiment models."""

    def prepare(
        self,
        *,
        selection: ResearchPortfolioSelectionV1,
        proposals: Sequence[ResearchProposalV1],
        champion: StoredExperimentV1,
        strategy_space: StrategySpaceV1,
        created_at: str,
    ) -> ResearchStrategyCheckpointV1:
        if not isinstance(selection, ResearchPortfolioSelectionV1):
            raise TypeError("selection must be ResearchPortfolioSelectionV1")
        if not isinstance(champion, StoredExperimentV1):
            raise TypeError("champion must be StoredExperimentV1")
        if not isinstance(strategy_space, StrategySpaceV1):
            raise TypeError("strategy_space must be StrategySpaceV1")
        created_at = timestamp(created_at, "created_at")

        selected_id = selection.decision.selected_proposal_id
        if selected_id is None:
            raise DurableStrategyDecisionError("R8.7 requires a selected proposal")
        proposal_items = tuple(proposals)
        matches = [item for item in proposal_items if item.proposal_id == selected_id]
        if len(matches) != 1:
            raise DurableStrategyDecisionError(
                "selected proposal must be present exactly once in proposal set"
            )
        proposal = matches[0]
        source = champion.spec

        if source.campaign_id != selection.decision.campaign_id:
            raise DurableStrategyDecisionError("champion is cross-campaign")
        if proposal.campaign_id != source.campaign_id:
            raise DurableStrategyDecisionError("selected proposal is cross-campaign")
        if proposal.parent_experiment_ids != (source.experiment_id,):
            raise DurableStrategyDecisionError(
                "selected proposal must have exact champion parent"
            )
        if proposal.source_strategy_id != source.strategy_id:
            raise DurableStrategyDecisionError(
                "selected proposal source_strategy_id mismatch"
            )

        dimension = next(
            (item for item in strategy_space.dimensions if item.name == proposal.target_dimension),
            None,
        )
        if dimension is None:
            raise DurableStrategyDecisionError(
                "selected proposal target_dimension missing from strategy space"
            )
        expected_kind = _TARGET_KIND[dimension.target]
        if proposal.target_kind is not expected_kind:
            raise DurableStrategyDecisionError(
                "selected proposal target_kind does not match strategy dimension"
            )
        expected_key = (
            dimension.key
            if dimension.key is not None
            else dimension.target.value.casefold()
        )
        if proposal.target_key != expected_key:
            raise DurableStrategyDecisionError(
                "selected proposal target_key does not match strategy dimension"
            )
        if not any(
            canonical_json(choice.value) == canonical_json(proposal.target_value)
            for choice in dimension.choices
        ):
            raise DurableStrategyDecisionError(
                "selected proposal target_value is not an allowed strategy choice"
            )

        identity = stable_sha256(
            {
                "schema_version": 1,
                "decision_fingerprint": selection.decision.decision_fingerprint,
                "proposal_fingerprint": proposal.fingerprint,
                "champion_fingerprint": source.fingerprint,
                "target_dimension": proposal.target_dimension,
                "target_value": plain(proposal.target_value),
            }
        )
        hypothesis = HypothesisV1(
            hypothesis_id=f"r87h-{identity[:24]}",
            campaign_id=source.campaign_id,
            statement=proposal.hypothesis_statement,
            mechanism=proposal.mechanism,
            predicted_effect=proposal.predicted_effect,
            falsifier=proposal.falsifier,
            strategy_dimensions={
                proposal.target_dimension: plain(proposal.target_value)
            },
            evidence_refs=(
                f"strategy-decision:{selection.decision.decision_fingerprint}",
                f"proposal:{proposal.fingerprint}",
            ),
            created_by="phase22-r8.7-strategy-decision",
            created_at=created_at,
            mode=HypothesisModeV1.RESEARCH_QUALITY,
            status=HypothesisStatusV1.PROPOSED,
            parent_hypothesis_ids=(source.hypothesis_id,),
        )

        spec = self._mutate_spec(
            source=source,
            champion=champion,
            proposal=proposal,
            dimension=dimension,
            hypothesis=hypothesis,
            identity=identity,
            created_at=created_at,
        )
        if spec.fingerprint == source.fingerprint:
            raise DurableStrategyDecisionError(
                "selected proposal materialized to champion no-op"
            )

        return ResearchStrategyCheckpointV1(
            selection=selection,
            selected_proposal=proposal,
            planned_hypothesis=hypothesis,
            planned_experiment=spec,
            stage=ResearchStrategyCheckpointStageV1.DECISION_PERSISTED,
        )

    @staticmethod
    def _mutate_spec(
        *,
        source: ExperimentSpecV1,
        champion: StoredExperimentV1,
        proposal: ResearchProposalV1,
        dimension: Any,
        hypothesis: HypothesisV1,
        identity: str,
        created_at: str,
    ) -> ExperimentSpecV1:
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
                raise DurableStrategyDecisionError("selected SEED must be non-negative integer")
            seed = value
        elif target is StrategyDimensionTargetV1.RESOURCE_BUDGET:
            resources[key] = value
        elif target is StrategyDimensionTargetV1.EXECUTION:
            execution[key] = value
        elif target is StrategyDimensionTargetV1.STRATEGY_ID:
            strategy_id = safe_id(value, "selected strategy_id")
        else:  # pragma: no cover
            raise DurableStrategyDecisionError(f"unsupported strategy target {target!r}")

        code_base_commit = source.code_base_commit
        if champion.result is not None and champion.result.candidate_commit is not None:
            code_base_commit = champion.result.candidate_commit

        return ExperimentSpecV1(
            experiment_id=f"r87e-{identity[:24]}",
            campaign_id=source.campaign_id,
            hypothesis_id=hypothesis.hypothesis_id,
            parent_experiment_ids=(source.experiment_id,),
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
            created_at=created_at,
        )


@dataclass(frozen=True, slots=True)
class DurableResearchStrategyCoordinatorV1:
    materializer: DeterministicStrategyExperimentMaterializerV1 = (
        DeterministicStrategyExperimentMaterializerV1()
    )

    def persist_decision(
        self,
        *,
        store: ResearchStrategyDurableStorePort,
        selection: ResearchPortfolioSelectionV1,
        proposals: Sequence[ResearchProposalV1],
        champion: StoredExperimentV1,
        strategy_space: StrategySpaceV1,
        created_at: str,
    ) -> ResearchStrategyCheckpointV1:
        checkpoint = self.materializer.prepare(
            selection=selection,
            proposals=proposals,
            champion=champion,
            strategy_space=strategy_space,
            created_at=created_at,
        )
        store.save_strategy_checkpoint(checkpoint)
        loaded = store.load_strategy_checkpoint_by_fingerprint(
            checkpoint.selection.decision.campaign_id,
            checkpoint.decision_fingerprint,
        )
        if loaded.planning_fingerprint != checkpoint.planning_fingerprint:
            raise DurableStrategyDecisionError(
                "persisted strategy decision planning fingerprint mismatch"
            )
        return loaded

    def ensure_selected_experiment_planned(
        self,
        *,
        store: ResearchStrategyDurableStorePort,
        campaign_id: str,
        decision_fingerprint: str,
    ) -> ResearchStrategyCheckpointV1:
        campaign_id = safe_id(campaign_id, "campaign_id")
        decision_fingerprint = sha256_hex(
            decision_fingerprint, "decision_fingerprint"
        )
        checkpoint = store.load_strategy_checkpoint_by_fingerprint(
            campaign_id,
            decision_fingerprint,
        )

        if checkpoint.stage is ResearchStrategyCheckpointStageV1.EXPERIMENT_PLANNED:
            loaded_spec = store.load_experiment_spec(
                checkpoint.registered_experiment_id or ""
            )
            if loaded_spec != checkpoint.planned_experiment:
                raise DurableStrategyDecisionError(
                    "registered planned experiment spec mismatch"
                )
            return checkpoint

        registration = store.register_hypothesis_experiment(
            checkpoint.planned_hypothesis,
            checkpoint.planned_experiment,
        )
        canonical = safe_id(
            registration.canonical_experiment_id,
            "canonical_experiment_id",
        )
        if canonical != checkpoint.planned_experiment.experiment_id:
            raise DurableStrategyDecisionError(
                "selected experiment resolved to a different canonical experiment"
            )
        planned = checkpoint.mark_experiment_planned(canonical)
        store.mark_strategy_checkpoint_planned(planned)

        reopened = store.load_strategy_checkpoint_by_fingerprint(
            campaign_id,
            decision_fingerprint,
        )
        if reopened.fingerprint != planned.fingerprint:
            raise DurableStrategyDecisionError(
                "reopened planned checkpoint fingerprint mismatch"
            )
        loaded_spec = store.load_experiment_spec(canonical)
        if loaded_spec != reopened.planned_experiment:
            raise DurableStrategyDecisionError(
                "reopened experiment does not match exact selected experiment"
            )
        return reopened

    def resume_exact_planned_experiment(
        self,
        *,
        store: ResearchStrategyDurableStorePort,
        campaign_id: str,
        decision_fingerprint: str,
    ) -> ExperimentSpecV1:
        checkpoint = self.ensure_selected_experiment_planned(
            store=store,
            campaign_id=campaign_id,
            decision_fingerprint=decision_fingerprint,
        )
        assert checkpoint.registered_experiment_id is not None
        spec = store.load_experiment_spec(checkpoint.registered_experiment_id)
        if spec.fingerprint != checkpoint.planned_experiment.fingerprint:
            raise DurableStrategyDecisionError(
                "resume exact experiment fingerprint mismatch"
            )
        return spec

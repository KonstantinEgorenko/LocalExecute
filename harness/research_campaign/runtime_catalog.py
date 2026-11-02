'Trusted runtime catalog for research diagnostics research campaign profiles and proposal generators.'
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..integration.single_endpoint import SingleEndpointOrnithRuntime
from .contracts import MetricDirectionV1
from .model_proposals import (
    ModelBackedResearchProposalGeneratorV1,
    OrnithModelResearchProposalGeneratorPortV1,
)
from .profile import (
    CommandMetricEvaluationConfigV1,
    ResearchBudgetPolicyV1,
    ResearchCampaignProfileV1,
)
from .profile_registry import (
    ModelProposalFactoryRegistryV1,
    ProposalGeneratorFactoryV1,
    ResearchCampaignProfileRegistryV1,
)
from .search import (
    StrategyChoiceV1,
    StrategyDimensionTargetV1,
    StrategyDimensionV1,
    StrategySpaceV1,
)
from .strategy_proposals import DeterministicResearchProposalGeneratorV1

UNSEEN_RANKING_PROFILE_ID = "phase24-3-unseen-ranking-v1"
ORNITH_CANONICAL_GENERATOR_ID = "phase24-4b1-ornith-canonical-choice-v1"


def build_unseen_ranking_profile(*, max_experiments: int = 4) -> ResearchCampaignProfileV1:
    strategy_space = StrategySpaceV1(
        dimensions=(
            StrategyDimensionV1(
                name="urgency_weight",
                target=StrategyDimensionTargetV1.PARAMETER,
                key="urgency_weight",
                choices=(StrategyChoiceV1(value=1.0),),
            ),
            StrategyDimensionV1(
                name="impact_weight",
                target=StrategyDimensionTargetV1.PARAMETER,
                key="impact_weight",
                choices=(StrategyChoiceV1(value=0.0), StrategyChoiceV1(value=0.75)),
            ),
            StrategyDimensionV1(
                name="cost_penalty",
                target=StrategyDimensionTargetV1.PARAMETER,
                key="cost_penalty",
                choices=(StrategyChoiceV1(value=0.0), StrategyChoiceV1(value=0.75)),
            ),
            StrategyDimensionV1(
                name="age_weight",
                target=StrategyDimensionTargetV1.PARAMETER,
                key="age_weight",
                choices=(StrategyChoiceV1(value=0.0), StrategyChoiceV1(value=0.25)),
            ),
        ),
        max_candidates=32,
    )
    return ResearchCampaignProfileV1(
        profile_id=UNSEEN_RANKING_PROFILE_ID,
        metric_name="dispatch_accuracy",
        metric_version="1",
        metric_direction=MetricDirectionV1.MAXIMIZE,
        strategy_space=strategy_space,
        baseline_parameters={
            "urgency_weight": 1.0,
            "impact_weight": 0.0,
            "cost_penalty": 0.0,
            "age_weight": 0.0,
        },
        dataset_refs=("data/feature_schema.json",),
        research_split_ref="data/dev_cases.json",
        scorer_refs=("benchmark.py",),
        allowed_mutation_scope=("heuristic_config.json",),
        forbidden_mutation_scope=(
            "benchmark.py",
            "data/feature_schema.json",
            "data/dev_cases.json",
            "tests/test_contract.py",
        ),
        protected_no_feedback_paths=("data/protected_holdout_marker.py",),
        evaluation_config=CommandMetricEvaluationConfigV1(
            verification_argv=("{python}", "-m", "pytest", "-q"),
            evaluation_argv=("{python}", "benchmark.py", "--split", "DEV"),
            metric_json_key="accuracy",
            research_split="DEV",
            timeout_seconds=90,
            expected_config_path="heuristic_config.json",
        ),
        execution_goal_capsule=(
            "Update heuristic_config.json so its committed JSON equals "
            "MissionSpec.global_constraints['research_experiment_parameters'] exactly. "
            "Change no other file. The selected parameter mapping is authoritative; do not invent, "
            "tune, normalize, or omit values. Plan exactly one mutation-owned CODE Todo for this "
            "single-file update; do not create standalone inspect, test, verification, stage, or "
            "commit Todos. Any bounded inspection belongs inside the same mutation Todo. Production "
            "verification owns tests and candidate commit creation. Never request GATE or TEST feedback."
        ),
        execution_global_constraints={
            "selected_parameters_are_authoritative": True,
            "dev_only_research_evaluation": True,
        },
        budget_policy=ResearchBudgetPolicyV1(
            wall_time_seconds=7200,
            max_experiments=max_experiments,
            max_gpu_seconds=3600.0,
            max_api_cost=25.0,
            max_metric_calls=max_experiments + 4,
            max_external_research_rounds=1,
            max_consecutive_crashes=2,
            max_no_improvement_window=max(2, max_experiments - 1),
        ),
        data_split_policy={
            "research_feedback": "DEV_ONLY",
            "gate_test": "EXTERNAL_POSTHOC_ONLY",
        },
    )


def create_ornith_proposal_factory(*, swarm_root: Path) -> ProposalGeneratorFactoryV1:
    model_port = OrnithModelResearchProposalGeneratorPortV1(
        root=Path(swarm_root).resolve(strict=True),
        runtime_factory=SingleEndpointOrnithRuntime,
    )

    def factory(packet: Any) -> ModelBackedResearchProposalGeneratorV1:
        return ModelBackedResearchProposalGeneratorV1(
            model=model_port,
            evidence_packet=packet,
            fallback=DeterministicResearchProposalGeneratorV1(),
            max_proposals=8,
        )

    return factory


def create_default_research_profile_registry() -> ResearchCampaignProfileRegistryV1:
    registry = ResearchCampaignProfileRegistryV1()
    registry.register(build_unseen_ranking_profile())
    return registry


def create_default_model_proposal_factory_registry(*, swarm_root: Path) -> ModelProposalFactoryRegistryV1:
    registry = ModelProposalFactoryRegistryV1()
    registry.register(
        ORNITH_CANONICAL_GENERATOR_ID,
        create_ornith_proposal_factory(swarm_root=swarm_root),
    )
    return registry


@dataclass(slots=True)
class ResearchRuntimeCatalogV1:
    """Trusted catalog bridging public mission request IDs to immutable research profiles/factories."""

    profile_registry: ResearchCampaignProfileRegistryV1
    proposal_factory_registry: ModelProposalFactoryRegistryV1

    @classmethod
    def create_default(cls, *, swarm_root: Path) -> "ResearchRuntimeCatalogV1":
        profiles = create_default_research_profile_registry()
        factories = create_default_model_proposal_factory_registry(swarm_root=swarm_root)
        return cls(profile_registry=profiles, proposal_factory_registry=factories)

    def resolve_profile(self, profile_id: str) -> ResearchCampaignProfileV1:
        try:
            return self.profile_registry.resolve(profile_id)
        except (ValueError, TypeError) as exc:
            raise KeyError(f"unknown trusted research profile: {profile_id}") from exc

    def resolve_proposal_factory(self, generator_identity: str) -> ProposalGeneratorFactoryV1 | None:
        if generator_identity == "deterministic":
            return None
        try:
            return self.proposal_factory_registry.resolve(generator_identity)
        except (ValueError, TypeError) as exc:
            raise KeyError(f"unknown trusted proposal generator identity: {generator_identity}") from exc

    def validate_resolution(
        self, profile_id: str, generator_identity: str = "deterministic"
    ) -> tuple[bool, str | None]:
        try:
            self.profile_registry.resolve(profile_id)
        except (KeyError, ValueError, TypeError):
            return False, f"unknown trusted research profile: {profile_id}"
        if generator_identity != "deterministic":
            try:
                self.proposal_factory_registry.resolve(generator_identity)
            except (KeyError, ValueError, TypeError):
                return False, f"unknown trusted proposal generator identity: {generator_identity}"
        return True, None

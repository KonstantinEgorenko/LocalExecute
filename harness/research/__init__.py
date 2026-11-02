"""Canonical public facade for the autonomous research subsystem.

The authoritative implementation remains under ``harness.research_campaign``.
This facade intentionally exposes a curated operator and integration surface
rather than mirroring the historical package's broad ``__all__``.
"""
from __future__ import annotations

from ..research_campaign.bootstrap import (
    ResearchCampaignBootstrapV1,
    ResearchCampaignBundleV1,
)
from ..research_campaign.campaign import (
    ResearchCampaignStateV1,
    ResearchCampaignStatusV1,
)
from ..research_campaign.contracts import (
    EvaluationContractV1,
    MetricDirectionV1,
    ResearchBudgetV1,
    ResearchContractV1,
)
from ..research_campaign.controller import (
    ResearchCampaignControllerV1,
)
from ..research_campaign.mission_dispatcher import (
    MissionDispatchIdentityError,
    MissionDispatcherV1,
)
from ..research_campaign.mission_models import (
    MissionDispatchResultV1,
    MissionModeV1,
    get_mission_mode,
    get_proposal_generator_identity,
    get_research_profile_id,
)
from ..research_campaign.model_proposals import (
    ModelBackedResearchProposalGeneratorV1,
)
from ..research_campaign.profile import (
    CommandMetricEvaluationConfigV1,
    ResearchBudgetPolicyV1,
    ResearchCampaignProfileV1,
)
from ..research_campaign.profile_registry import (
    DEFAULT_MODEL_PROPOSAL_FACTORY_REGISTRY,
    DEFAULT_RESEARCH_PROFILE_REGISTRY,
    ModelProposalFactoryRegistryV1,
    ProposalGeneratorFactoryV1,
    ResearchCampaignProfileRegistryV1,
)
from ..research_campaign.runner import (
    ResearchCampaignRunnerV1,
    ResearchCampaignRunPolicyV1,
    ResearchCampaignRunResultV1,
)
from ..research_campaign.runtime_catalog import (
    ORNITH_CANONICAL_GENERATOR_ID,
    UNSEEN_RANKING_PROFILE_ID,
    ResearchRuntimeCatalogV1,
    create_default_model_proposal_factory_registry,
    create_default_research_profile_registry,
)
from ..research_campaign.stop_policy import (
    ResearchStopDecisionV1,
    ResearchStopPolicyV1,
    ResearchStopReasonV1,
)
from ..research_campaign.store import ResearchExperimentStore

__all__ = [
    "CommandMetricEvaluationConfigV1",
    "DEFAULT_MODEL_PROPOSAL_FACTORY_REGISTRY",
    "DEFAULT_RESEARCH_PROFILE_REGISTRY",
    "EvaluationContractV1",
    "MetricDirectionV1",
    "MissionDispatchIdentityError",
    "MissionDispatchResultV1",
    "MissionDispatcherV1",
    "MissionModeV1",
    "ModelBackedResearchProposalGeneratorV1",
    "ModelProposalFactoryRegistryV1",
    "ORNITH_CANONICAL_GENERATOR_ID",
    "ProposalGeneratorFactoryV1",
    "ResearchBudgetPolicyV1",
    "ResearchBudgetV1",
    "ResearchCampaignBootstrapV1",
    "ResearchCampaignBundleV1",
    "ResearchCampaignControllerV1",
    "ResearchCampaignProfileRegistryV1",
    "ResearchCampaignProfileV1",
    "ResearchCampaignRunPolicyV1",
    "ResearchCampaignRunResultV1",
    "ResearchCampaignRunnerV1",
    "ResearchCampaignStateV1",
    "ResearchCampaignStatusV1",
    "ResearchContractV1",
    "ResearchExperimentStore",
    "ResearchRuntimeCatalogV1",
    "ResearchStopDecisionV1",
    "ResearchStopPolicyV1",
    "ResearchStopReasonV1",
    "UNSEEN_RANKING_PROFILE_ID",
    "create_default_model_proposal_factory_registry",
    "create_default_research_profile_registry",
    "get_mission_mode",
    "get_proposal_generator_identity",
    "get_research_profile_id",
]

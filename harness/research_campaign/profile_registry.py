'Trusted in-process registries for research diagnostics research profiles and model proposal factories.'
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .evidence import ResearchEvidencePacketV1
from .model_proposals import ModelBackedResearchProposalGeneratorV1
from .profile import ResearchCampaignProfileV1
from .serialization import safe_id


ProposalGeneratorFactoryV1 = Callable[
    [ResearchEvidencePacketV1], ModelBackedResearchProposalGeneratorV1
]


@dataclass(slots=True)
class ResearchCampaignProfileRegistryV1:
    """Trusted profile_id -> immutable profile registry.

    Raw evaluator commands are never accepted from MissionSpec/LLM input; callers
    may only select a profile that trusted application code has registered.
    """

    _profiles: dict[str, ResearchCampaignProfileV1] = field(default_factory=dict)

    def register(self, profile: ResearchCampaignProfileV1) -> None:
        if not isinstance(profile, ResearchCampaignProfileV1):
            raise TypeError("profile must be ResearchCampaignProfileV1")
        if profile.profile_id in self._profiles:
            raise ValueError(f"research profile already registered: {profile.profile_id}")
        self._profiles[profile.profile_id] = profile

    def resolve(self, profile_id: str) -> ResearchCampaignProfileV1:
        profile_id = safe_id(profile_id, "research_profile_id")
        try:
            return self._profiles[profile_id]
        except KeyError as exc:
            raise KeyError(f"unknown trusted research profile: {profile_id}") from exc

    def profile_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._profiles))


@dataclass(slots=True)
class ModelProposalFactoryRegistryV1:
    """Stable generator identity -> trusted model-backed proposal factory."""

    _factories: dict[str, ProposalGeneratorFactoryV1] = field(default_factory=dict)

    def register(self, identity: str, factory: ProposalGeneratorFactoryV1) -> None:
        identity = safe_id(identity, "proposal_generator_identity")
        if identity == "deterministic":
            raise ValueError("'deterministic' is reserved and does not use a model factory")
        if not callable(factory):
            raise TypeError("proposal generator factory must be callable")
        if identity in self._factories:
            raise ValueError(f"proposal generator identity already registered: {identity}")
        self._factories[identity] = factory

    def resolve(self, identity: str) -> ProposalGeneratorFactoryV1:
        identity = safe_id(identity, "proposal_generator_identity")
        if identity == "deterministic":
            raise KeyError("deterministic proposal mode does not resolve through model factory registry")
        try:
            return self._factories[identity]
        except KeyError as exc:
            raise KeyError(f"unknown trusted proposal generator identity: {identity}") from exc

    def identities(self) -> tuple[str, ...]:
        return tuple(sorted(self._factories))


DEFAULT_RESEARCH_PROFILE_REGISTRY = ResearchCampaignProfileRegistryV1()
DEFAULT_MODEL_PROPOSAL_FACTORY_REGISTRY = ModelProposalFactoryRegistryV1()

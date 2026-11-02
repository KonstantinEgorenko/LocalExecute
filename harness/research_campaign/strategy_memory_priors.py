'research campaign.R8.6 deterministic research-memory adaptation into evidence priors.\n\nR8.6 converts CURRENT actionable ResearchBeliefV1 records into proposal-specific\nProposalEvidencePriorV1 values. Matching is typed through StrategySpaceV1 and\nhistorical ExperimentSpecV1 lineage, never inferred from natural-language prose.\n\nTechnical/diagnostic, superseded, stale, or incompatible beliefs do not influence\nsearch. The module does not score utility, rank/select proposals, persist decisions,\nor execute experiments.\n'
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping, Protocol, Sequence

from .beliefs import (
    ResearchBeliefApplicabilityV1,
    ResearchBeliefEvidenceStateV1,
    ResearchBeliefSetV1,
    ResearchBeliefStatusV1,
    ResearchBeliefV1,
)
from .models import ExperimentSpecV1
from .search import (
    StrategyDimensionTargetV1,
    StrategyDimensionV1,
    StrategySpaceV1,
)
from .serialization import (
    canonical_json,
    finite_number,
    non_negative_number,
    plain,
    safe_id,
    sha256_hex,
    stable_sha256,
)
from .store import StoredExperimentV1
from .strategy_contracts import ResearchProposalTargetKindV1, ResearchProposalV1
from .strategy_utility import ProposalEvidencePriorV1



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

def _unit_interval(value: object, name: str) -> float:
    result = non_negative_number(value, name)
    if result > 1.0:
        raise ValueError(f"{name} must be <= 1")
    return result


def _positive_number(value: object, name: str) -> float:
    result = non_negative_number(value, name)
    if result <= 0.0:
        raise ValueError(f"{name} must be > 0")
    return result


def _clamp_unit(value: float) -> float:
    return max(0.0, min(1.0, value))


def _clamp_signed_unit(value: float) -> float:
    return max(-1.0, min(1.0, value))


class ProposalBeliefMatchKindV1(StrEnum):
    EXACT_TARGET = "EXACT_TARGET"
    STRATEGY_FAMILY = "STRATEGY_FAMILY"


@dataclass(frozen=True, slots=True)
class ResearchMemoryPriorPolicyV1:
    exact_target_relevance: float = 1.0
    strategy_family_relevance: float = 0.20
    belief_signal_scale: float = 8.0
    evidence_weight_saturation: float = 2.0
    max_beliefs_per_proposal: int = 128

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "exact_target_relevance",
            _unit_interval(self.exact_target_relevance, "exact_target_relevance"),
        )
        object.__setattr__(
            self,
            "strategy_family_relevance",
            _unit_interval(
                self.strategy_family_relevance,
                "strategy_family_relevance",
            ),
        )
        if self.strategy_family_relevance > self.exact_target_relevance:
            raise ValueError(
                "strategy_family_relevance must not exceed exact_target_relevance"
            )
        object.__setattr__(
            self,
            "belief_signal_scale",
            _positive_number(self.belief_signal_scale, "belief_signal_scale"),
        )
        object.__setattr__(
            self,
            "evidence_weight_saturation",
            _positive_number(
                self.evidence_weight_saturation,
                "evidence_weight_saturation",
            ),
        )
        if (
            isinstance(self.max_beliefs_per_proposal, bool)
            or not isinstance(self.max_beliefs_per_proposal, int)
            or self.max_beliefs_per_proposal <= 0
        ):
            raise ValueError("max_beliefs_per_proposal must be positive integer")
        if self.max_beliefs_per_proposal > 10_000:
            raise ValueError("max_beliefs_per_proposal must be <= 10000")

    @property
    def fingerprint(self) -> str:
        return stable_sha256(
            {
                "schema_version": 1,
                "exact_target_relevance": self.exact_target_relevance,
                "strategy_family_relevance": self.strategy_family_relevance,
                "belief_signal_scale": self.belief_signal_scale,
                "evidence_weight_saturation": self.evidence_weight_saturation,
                "max_beliefs_per_proposal": self.max_beliefs_per_proposal,
                "expected_gain_aggregation": "confidence_normalized_relevance_weighted_v2",
            }
        )


@dataclass(frozen=True, slots=True)
class ProposalBeliefContributionV1:
    proposal_id: str
    belief_id: str
    belief_fingerprint: str
    match_kind: ProposalBeliefMatchKindV1
    relevance_weight: float
    normalized_signal: float
    belief_confidence: float
    effective_weight: float
    weighted_signal: float

    def __post_init__(self) -> None:
        safe_id(self.proposal_id, "proposal_id")
        safe_id(self.belief_id, "belief_id")
        object.__setattr__(
            self,
            "belief_fingerprint",
            sha256_hex(self.belief_fingerprint, "belief_fingerprint"),
        )
        if not isinstance(self.match_kind, ProposalBeliefMatchKindV1):
            raise ValueError("match_kind must be ProposalBeliefMatchKindV1")
        object.__setattr__(
            self,
            "relevance_weight",
            _unit_interval(self.relevance_weight, "relevance_weight"),
        )
        signal = finite_number(self.normalized_signal, "normalized_signal")
        if signal < -1.0 or signal > 1.0:
            raise ValueError("normalized_signal must be within [-1,1]")
        object.__setattr__(self, "normalized_signal", signal)
        object.__setattr__(
            self,
            "belief_confidence",
            _unit_interval(self.belief_confidence, "belief_confidence"),
        )
        object.__setattr__(
            self,
            "effective_weight",
            non_negative_number(self.effective_weight, "effective_weight"),
        )
        object.__setattr__(
            self,
            "weighted_signal",
            finite_number(self.weighted_signal, "weighted_signal"),
        )
        expected_weight = self.relevance_weight * self.belief_confidence
        if abs(expected_weight - self.effective_weight) > 1e-12:
            raise ValueError(
                "effective_weight must equal relevance_weight * belief_confidence"
            )
        expected_signal = self.normalized_signal * self.effective_weight
        if abs(expected_signal - self.weighted_signal) > 1e-12:
            raise ValueError(
                "weighted_signal must equal normalized_signal * effective_weight"
            )

    @property
    def fingerprint(self) -> str:
        return stable_sha256(self._identity_dict())

    def _identity_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "proposal_id": self.proposal_id,
            "belief_id": self.belief_id,
            "belief_fingerprint": self.belief_fingerprint,
            "match_kind": self.match_kind.value,
            "relevance_weight": self.relevance_weight,
            "normalized_signal": self.normalized_signal,
            "belief_confidence": self.belief_confidence,
            "effective_weight": self.effective_weight,
            "weighted_signal": self.weighted_signal,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._identity_dict(), "fingerprint": self.fingerprint}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ProposalBeliefContributionV1":
        if raw.get("schema_version") != 1:
            raise ValueError("ProposalBeliefContributionV1 schema_version must be 1")
        result = cls(
            proposal_id=str(raw["proposal_id"]),
            belief_id=str(raw["belief_id"]),
            belief_fingerprint=str(raw["belief_fingerprint"]),
            match_kind=ProposalBeliefMatchKindV1(str(raw["match_kind"])),
            relevance_weight=float(raw["relevance_weight"]),
            normalized_signal=float(raw["normalized_signal"]),
            belief_confidence=float(raw["belief_confidence"]),
            effective_weight=float(raw["effective_weight"]),
            weighted_signal=float(raw["weighted_signal"]),
        )
        expected = raw.get("fingerprint")
        if expected is not None and expected != result.fingerprint:
            raise ValueError("ProposalBeliefContributionV1 fingerprint mismatch")
        return result


@dataclass(frozen=True, slots=True)
class ResearchMemoryPriorProjectionV1:
    proposal_id: str
    proposal_fingerprint: str
    prior: ProposalEvidencePriorV1
    contributions: tuple[ProposalBeliefContributionV1, ...]
    belief_set_fingerprint: str
    strategy_space_fingerprint: str
    experiment_evidence_fingerprint: str
    policy_fingerprint: str
    excluded_diagnostic_belief_ids: tuple[str, ...]
    excluded_non_current_belief_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        safe_id(self.proposal_id, "proposal_id")
        object.__setattr__(
            self,
            "proposal_fingerprint",
            sha256_hex(self.proposal_fingerprint, "proposal_fingerprint"),
        )
        if not isinstance(self.prior, ProposalEvidencePriorV1):
            raise ValueError("prior must be ProposalEvidencePriorV1")
        if self.prior.proposal_id != self.proposal_id:
            raise ValueError("prior proposal_id mismatch")
        if not isinstance(self.contributions, tuple):
            raise ValueError("contributions must be tuple")
        if any(
            not isinstance(item, ProposalBeliefContributionV1)
            for item in self.contributions
        ):
            raise ValueError(
                "contributions must contain ProposalBeliefContributionV1"
            )
        if any(item.proposal_id != self.proposal_id for item in self.contributions):
            raise ValueError("contribution proposal_id mismatch")
        belief_ids = tuple(item.belief_id for item in self.contributions)
        if len(set(belief_ids)) != len(belief_ids):
            raise ValueError("contributions must not repeat belief_id")
        for name in (
            "belief_set_fingerprint",
            "strategy_space_fingerprint",
            "experiment_evidence_fingerprint",
            "policy_fingerprint",
        ):
            object.__setattr__(
                self,
                name,
                sha256_hex(getattr(self, name), name),
            )
        for name in (
            "excluded_diagnostic_belief_ids",
            "excluded_non_current_belief_ids",
        ):
            values = tuple(safe_id(item, f"{name} item") for item in getattr(self, name))
            if len(set(values)) != len(values):
                raise ValueError(f"{name} must not contain duplicates")
            object.__setattr__(self, name, values)

    @property
    def fingerprint(self) -> str:
        return stable_sha256(
            {
                "schema_version": 1,
                "proposal_id": self.proposal_id,
                "proposal_fingerprint": self.proposal_fingerprint,
                "prior": self.prior.to_dict(),
                "contributions": [item.to_dict() for item in self.contributions],
                "belief_set_fingerprint": self.belief_set_fingerprint,
                "strategy_space_fingerprint": self.strategy_space_fingerprint,
                "experiment_evidence_fingerprint": self.experiment_evidence_fingerprint,
                "policy_fingerprint": self.policy_fingerprint,
                "excluded_diagnostic_belief_ids": list(
                    self.excluded_diagnostic_belief_ids
                ),
                "excluded_non_current_belief_ids": list(
                    self.excluded_non_current_belief_ids
                ),
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "proposal_id": self.proposal_id,
            "proposal_fingerprint": self.proposal_fingerprint,
            "prior": self.prior.to_dict(),
            "contributions": [item.to_dict() for item in self.contributions],
            "belief_set_fingerprint": self.belief_set_fingerprint,
            "strategy_space_fingerprint": self.strategy_space_fingerprint,
            "experiment_evidence_fingerprint": self.experiment_evidence_fingerprint,
            "policy_fingerprint": self.policy_fingerprint,
            "excluded_diagnostic_belief_ids": list(
                self.excluded_diagnostic_belief_ids
            ),
            "excluded_non_current_belief_ids": list(
                self.excluded_non_current_belief_ids
            ),
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ResearchMemoryPriorProjectionV1":
        if raw.get("schema_version") != 1:
            raise ValueError("ResearchMemoryPriorProjectionV1 schema_version must be 1")
        result = cls(
            proposal_id=str(raw["proposal_id"]),
            proposal_fingerprint=str(raw["proposal_fingerprint"]),
            prior=ProposalEvidencePriorV1.from_dict(raw["prior"]),
            contributions=tuple(
                ProposalBeliefContributionV1.from_dict(item)
                for item in raw.get("contributions", ())
            ),
            belief_set_fingerprint=str(raw["belief_set_fingerprint"]),
            strategy_space_fingerprint=str(raw["strategy_space_fingerprint"]),
            experiment_evidence_fingerprint=str(
                raw["experiment_evidence_fingerprint"]
            ),
            policy_fingerprint=str(raw["policy_fingerprint"]),
            excluded_diagnostic_belief_ids=tuple(
                raw.get("excluded_diagnostic_belief_ids", ())
            ),
            excluded_non_current_belief_ids=tuple(
                raw.get("excluded_non_current_belief_ids", ())
            ),
        )
        expected = raw.get("fingerprint")
        if expected is not None and expected != result.fingerprint:
            raise ValueError("ResearchMemoryPriorProjectionV1 fingerprint mismatch")
        return result


class ResearchMemoryPriorProjectorPort(Protocol):
    def project_batch(
        self,
        *,
        proposals: Sequence[ResearchProposalV1],
        belief_set: ResearchBeliefSetV1,
        experiments: Sequence[StoredExperimentV1],
        strategy_space: StrategySpaceV1,
    ) -> tuple[ResearchMemoryPriorProjectionV1, ...]: ...


@dataclass(frozen=True, slots=True)
class DeterministicResearchMemoryPriorProjectorV1:
    policy: ResearchMemoryPriorPolicyV1 = ResearchMemoryPriorPolicyV1()

    def __post_init__(self) -> None:
        if not isinstance(self.policy, ResearchMemoryPriorPolicyV1):
            raise ValueError("policy must be ResearchMemoryPriorPolicyV1")

    def project_batch(
        self,
        *,
        proposals: Sequence[ResearchProposalV1],
        belief_set: ResearchBeliefSetV1,
        experiments: Sequence[StoredExperimentV1],
        strategy_space: StrategySpaceV1,
    ) -> tuple[ResearchMemoryPriorProjectionV1, ...]:
        if not isinstance(belief_set, ResearchBeliefSetV1):
            raise TypeError("belief_set must be ResearchBeliefSetV1")
        if not isinstance(strategy_space, StrategySpaceV1):
            raise TypeError("strategy_space must be StrategySpaceV1")

        proposal_items = _typed_sequence(
            proposals,
            ResearchProposalV1,
            "proposals",
        )
        experiment_items = _typed_sequence(
            experiments,
            StoredExperimentV1,
            "experiments",
        )
        if len({item.proposal_id for item in proposal_items}) != len(proposal_items):
            raise ValueError("proposals must not contain duplicate proposal_id")
        if any(item.campaign_id != belief_set.campaign_id for item in proposal_items):
            raise ValueError("proposals must match belief_set campaign_id")

        by_experiment: dict[str, StoredExperimentV1] = {}
        for stored in experiment_items:
            if stored.spec.campaign_id != belief_set.campaign_id:
                raise ValueError("experiment evidence is cross-campaign")
            if stored.spec.experiment_id in by_experiment:
                raise ValueError("duplicate experiment evidence")
            by_experiment[stored.spec.experiment_id] = stored

        dimensions = {item.name: item for item in strategy_space.dimensions}
        strategy_fp = _strategy_space_fingerprint(strategy_space)
        experiment_fp = _experiment_evidence_fingerprint(experiment_items)

        diagnostic = tuple(
            sorted(
                item.belief_id
                for item in belief_set.beliefs
                if item.status
                in {
                    ResearchBeliefStatusV1.DIAGNOSTIC_ONLY,
                    ResearchBeliefStatusV1.SUPERSEDED,
                }
            )
        )
        non_current = tuple(
            sorted(
                item.belief_id
                for item in belief_set.beliefs
                if item.applicability is not ResearchBeliefApplicabilityV1.CURRENT
            )
        )

        actionable = tuple(
            item
            for item in belief_set.beliefs
            if item.applicability is ResearchBeliefApplicabilityV1.CURRENT
            and item.status
            not in {
                ResearchBeliefStatusV1.DIAGNOSTIC_ONLY,
                ResearchBeliefStatusV1.SUPERSEDED,
            }
        )
        for belief in actionable:
            active_experiment_ids = _active_belief_experiment_ids(belief)
            missing = sorted(set(active_experiment_ids) - set(by_experiment))
            if missing:
                raise ValueError(
                    "actionable belief ACTIVE experiment evidence is missing: "
                    + ",".join(missing)
                )

        belief_set_fp = _belief_set_projection_fingerprint(belief_set)

        return tuple(
            self._project_one(
                proposal=proposal,
                beliefs=actionable,
                experiments=by_experiment,
                dimensions=dimensions,
                belief_set_fingerprint=belief_set_fp,
                strategy_space_fingerprint=strategy_fp,
                experiment_evidence_fingerprint=experiment_fp,
                excluded_diagnostic_belief_ids=diagnostic,
                excluded_non_current_belief_ids=non_current,
            )
            for proposal in sorted(proposal_items, key=lambda item: item.proposal_id)
        )

    def _project_one(
        self,
        *,
        proposal: ResearchProposalV1,
        beliefs: tuple[ResearchBeliefV1, ...],
        experiments: dict[str, StoredExperimentV1],
        dimensions: dict[str, StrategyDimensionV1],
        belief_set_fingerprint: str,
        strategy_space_fingerprint: str,
        experiment_evidence_fingerprint: str,
        excluded_diagnostic_belief_ids: tuple[str, ...],
        excluded_non_current_belief_ids: tuple[str, ...],
    ) -> ResearchMemoryPriorProjectionV1:
        dimension = dimensions.get(proposal.target_dimension)
        if dimension is None:
            raise ValueError(
                f"proposal target_dimension {proposal.target_dimension!r} "
                "is absent from StrategySpaceV1"
            )
        expected_kind = _TARGET_KIND[dimension.target]
        if proposal.target_kind is not expected_kind:
            raise ValueError("proposal target_kind does not match StrategySpaceV1 dimension")
        expected_key = (
            dimension.key
            if dimension.key is not None
            else dimension.target.value.casefold()
        )
        if proposal.target_key != expected_key:
            raise ValueError("proposal target_key does not match StrategySpaceV1 dimension")

        contributions: list[ProposalBeliefContributionV1] = []
        for belief in sorted(beliefs, key=lambda item: item.belief_id):
            if belief.strategy_id != proposal.source_strategy_id:
                continue
            match_kind = self._match_kind(
                proposal=proposal,
                belief=belief,
                dimension=dimension,
                experiments=experiments,
            )
            relevance = (
                self.policy.exact_target_relevance
                if match_kind is ProposalBeliefMatchKindV1.EXACT_TARGET
                else self.policy.strategy_family_relevance
            )
            if relevance <= 0.0:
                continue

            normalized_signal = _clamp_signed_unit(
                belief.net_strength / self.policy.belief_signal_scale
            )
            effective_weight = relevance * belief.confidence
            contributions.append(
                ProposalBeliefContributionV1(
                    proposal_id=proposal.proposal_id,
                    belief_id=belief.belief_id,
                    belief_fingerprint=belief.fingerprint,
                    match_kind=match_kind,
                    relevance_weight=relevance,
                    normalized_signal=normalized_signal,
                    belief_confidence=belief.confidence,
                    effective_weight=effective_weight,
                    weighted_signal=normalized_signal * effective_weight,
                )
            )
            if len(contributions) >= self.policy.max_beliefs_per_proposal:
                break

        contributions_t = tuple(contributions)
        if contributions_t:
            total_weight = sum(item.effective_weight for item in contributions_t)
            total_confidence = sum(
                item.belief_confidence for item in contributions_t
            )
            if total_confidence <= 0.0:
                expected_gain = 0.0
            else:
                # Normalize by scientific belief confidence, not by
                # relevance-weighted confidence.  This preserves the intended
                # attenuation from EXACT_TARGET vs STRATEGY_FAMILY relevance.
                # With one consolidated belief, family relevance therefore
                # scales expected_gain instead of algebraically cancelling out.
                expected_gain = _clamp_signed_unit(
                    sum(item.weighted_signal for item in contributions_t)
                    / total_confidence
                )
            evidence_strength = _clamp_unit(
                total_weight / self.policy.evidence_weight_saturation
            )
            uncertainty = 1.0 - evidence_strength
            exploration_bonus = uncertainty
            provenance = (
                f"belief-set:{belief_set_fingerprint}",
                *(f"belief:{item.belief_id}" for item in contributions_t),
                f"memory-prior-policy:{self.policy.fingerprint}",
            )
        else:
            expected_gain = 0.0
            uncertainty = 1.0
            evidence_strength = 0.0
            exploration_bonus = 1.0
            provenance = (
                f"belief-set:{belief_set_fingerprint}",
                "memory-prior:uninformed",
                f"memory-prior-policy:{self.policy.fingerprint}",
            )

        prior = ProposalEvidencePriorV1(
            proposal_id=proposal.proposal_id,
            expected_gain=expected_gain,
            uncertainty=uncertainty,
            evidence_strength=evidence_strength,
            exploration_bonus=exploration_bonus,
            provenance_ids=provenance,
        )

        return ResearchMemoryPriorProjectionV1(
            proposal_id=proposal.proposal_id,
            proposal_fingerprint=proposal.fingerprint,
            prior=prior,
            contributions=contributions_t,
            belief_set_fingerprint=belief_set_fingerprint,
            strategy_space_fingerprint=strategy_space_fingerprint,
            experiment_evidence_fingerprint=experiment_evidence_fingerprint,
            policy_fingerprint=self.policy.fingerprint,
            excluded_diagnostic_belief_ids=excluded_diagnostic_belief_ids,
            excluded_non_current_belief_ids=excluded_non_current_belief_ids,
        )

    def _match_kind(
        self,
        *,
        proposal: ResearchProposalV1,
        belief: ResearchBeliefV1,
        dimension: StrategyDimensionV1,
        experiments: dict[str, StoredExperimentV1],
    ) -> ProposalBeliefMatchKindV1:
        # Only ACTIVE scientific evidence is allowed to upgrade a belief from
        # strategy-family relevance to exact-target relevance. Diagnostic-only or
        # superseded lineage remains durable audit provenance but has zero search
        # authority, including zero authority over target matching.
        for experiment_id in _active_belief_experiment_ids(belief):
            stored = experiments.get(experiment_id)
            if stored is None:  # validated before projection; defensive only
                continue
            value = _dimension_value(stored.spec, dimension)
            if value is _MISSING:
                continue
            if canonical_json(value) == canonical_json(proposal.target_value):
                return ProposalBeliefMatchKindV1.EXACT_TARGET
        return ProposalBeliefMatchKindV1.STRATEGY_FAMILY


def _active_belief_experiment_ids(belief: ResearchBeliefV1) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                evidence.experiment_id
                for evidence in belief.evidence
                if evidence.state is ResearchBeliefEvidenceStateV1.ACTIVE
            }
        )
    )


class _Missing:
    pass


_MISSING = _Missing()


def _dimension_value(
    spec: ExperimentSpecV1,
    dimension: StrategyDimensionV1,
) -> Any:
    target = dimension.target
    key = dimension.key

    if target is StrategyDimensionTargetV1.PARAMETER:
        return spec.parameters.get(key or "", _MISSING)
    if target is StrategyDimensionTargetV1.MODEL_ID:
        return spec.model_id
    if target is StrategyDimensionTargetV1.PROVIDER:
        return spec.provider
    if target is StrategyDimensionTargetV1.MODEL_VERSION:
        return spec.model_version
    if target is StrategyDimensionTargetV1.PROMPT_HASH:
        return spec.prompt_hash
    if target is StrategyDimensionTargetV1.SAMPLING_CONFIG:
        return spec.sampling_config.get(key or "", _MISSING)
    if target is StrategyDimensionTargetV1.TOOL_POLICY_HASH:
        return spec.tool_policy_hash
    if target is StrategyDimensionTargetV1.SEED:
        return spec.seed
    if target is StrategyDimensionTargetV1.RESOURCE_BUDGET:
        return spec.resource_budget.get(key or "", _MISSING)
    if target is StrategyDimensionTargetV1.EXECUTION:
        return spec.execution.get(key or "", _MISSING)
    if target is StrategyDimensionTargetV1.STRATEGY_ID:
        return spec.strategy_id
    raise ValueError(f"unsupported strategy target {target.value}")


def _belief_set_projection_fingerprint(belief_set: ResearchBeliefSetV1) -> str:
    return stable_sha256(
        {
            "schema_version": 1,
            "campaign_id": belief_set.campaign_id,
            "belief_fingerprints": sorted(
                item.fingerprint for item in belief_set.beliefs
            ),
            "source_insight_count": belief_set.source_insight_count,
            "grouped_belief_count": belief_set.grouped_belief_count,
            "max_beliefs": belief_set.max_beliefs,
            "omitted_by_budget_count": belief_set.omitted_by_budget_count,
            "target_scope_fingerprint": belief_set.target_scope_fingerprint,
        }
    )


def _strategy_space_fingerprint(space: StrategySpaceV1) -> str:
    return stable_sha256(
        {
            "schema_version": 1,
            "dimensions": [
                {
                    "name": dimension.name,
                    "target": dimension.target.value,
                    "key": dimension.key,
                    "choices": sorted(
                        (
                            {
                                "value": plain(choice.value),
                                "priority": choice.priority,
                                "estimated_cost": choice.estimated_cost,
                            }
                            for choice in dimension.choices
                        ),
                        key=lambda item: canonical_json(item),
                    ),
                }
                for dimension in sorted(space.dimensions, key=lambda item: item.name)
            ],
            "exclusions": sorted(
                (
                    {
                        "matches": plain(item.matches),
                        "reason": item.reason,
                    }
                    for item in space.exclusions
                ),
                key=lambda item: canonical_json(item),
            ),
            "max_candidates": space.max_candidates,
        }
    )


def _experiment_evidence_fingerprint(
    experiments: tuple[StoredExperimentV1, ...],
) -> str:
    return stable_sha256(
        {
            "schema_version": 1,
            "experiments": sorted(
                (
                    {
                        "experiment_id": item.spec.experiment_id,
                        "spec_fingerprint": item.spec.fingerprint,
                        "result_fingerprint": (
                            None
                            if item.result is None
                            else stable_sha256(item.result.to_dict())
                        ),
                    }
                    for item in experiments
                ),
                key=lambda item: item["experiment_id"],
            ),
        }
    )


def _typed_sequence(
    values: Sequence[Any],
    expected_type: type,
    name: str,
) -> tuple[Any, ...]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise TypeError(f"{name} must be a sequence")
    result = tuple(values)
    if any(not isinstance(item, expected_type) for item in result):
        raise TypeError(f"{name} contains invalid item type")
    return result

'research campaign.R8.4 deterministic expected-utility assessment.\n\nR8.4 combines hard-admissible proposals, R8.3 structural novelty/diversity, and\nexplicit typed evidence priors. It does not rank/select proposals and does not\nderive priors from research memory; memory-to-prior adaptation remains R8.6.\n'
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence

from .serialization import (
    finite_number,
    non_negative_number,
    safe_id,
    sha256_hex,
    stable_sha256,
    unique_text,
)
from .strategy_admissibility import ProposalAdmissibilityResultV1
from .strategy_contracts import ProposalAssessmentV1, ResearchProposalV1
from .strategy_novelty import ProposalNoveltyAssessmentV1


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


@dataclass(frozen=True, slots=True)
class ProposalEvidencePriorV1:
    """Typed evidence prior consumed by R8.4.

    R8.4 deliberately does not infer these fields from prose or research memory.
    R8.6 will adapt typed beliefs/insights into updated priors.
    """

    proposal_id: str
    expected_gain: float
    uncertainty: float
    evidence_strength: float
    exploration_bonus: float
    provenance_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        safe_id(self.proposal_id, "proposal_id")
        object.__setattr__(
            self,
            "expected_gain",
            finite_number(self.expected_gain, "expected_gain"),
        )
        object.__setattr__(
            self,
            "uncertainty",
            _unit_interval(self.uncertainty, "uncertainty"),
        )
        object.__setattr__(
            self,
            "evidence_strength",
            _unit_interval(self.evidence_strength, "evidence_strength"),
        )
        object.__setattr__(
            self,
            "exploration_bonus",
            _unit_interval(self.exploration_bonus, "exploration_bonus"),
        )
        object.__setattr__(
            self,
            "provenance_ids",
            unique_text(
                self.provenance_ids,
                "provenance_ids",
                maximum=512,
                item_max_length=8_000,
            ),
        )
        if not self.provenance_ids:
            raise ValueError("ProposalEvidencePriorV1 requires provenance_ids")

    @classmethod
    def uninformed(
        cls,
        *,
        proposal_id: str,
        provenance_ids: tuple[str, ...] = ("prior:uninformed",),
    ) -> "ProposalEvidencePriorV1":
        return cls(
            proposal_id=proposal_id,
            expected_gain=0.0,
            uncertainty=1.0,
            evidence_strength=0.0,
            exploration_bonus=1.0,
            provenance_ids=provenance_ids,
        )

    @property
    def fingerprint(self) -> str:
        return stable_sha256(self._identity_dict())

    def _identity_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "proposal_id": self.proposal_id,
            "expected_gain": self.expected_gain,
            "uncertainty": self.uncertainty,
            "evidence_strength": self.evidence_strength,
            "exploration_bonus": self.exploration_bonus,
            "provenance_ids": list(self.provenance_ids),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._identity_dict(), "fingerprint": self.fingerprint}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ProposalEvidencePriorV1":
        if raw.get("schema_version") != 1:
            raise ValueError("ProposalEvidencePriorV1 schema_version must be 1")
        result = cls(
            proposal_id=str(raw["proposal_id"]),
            expected_gain=float(raw["expected_gain"]),
            uncertainty=float(raw["uncertainty"]),
            evidence_strength=float(raw["evidence_strength"]),
            exploration_bonus=float(raw["exploration_bonus"]),
            provenance_ids=tuple(raw.get("provenance_ids", ())),
        )
        expected = raw.get("fingerprint")
        if expected is not None and expected != result.fingerprint:
            raise ValueError("ProposalEvidencePriorV1 fingerprint mismatch")
        return result


@dataclass(frozen=True, slots=True)
class ProposalUtilityPolicyV1:
    expected_gain_weight: float = 1.00
    exploration_weight: float = 0.20
    novelty_weight: float = 0.25
    diversity_weight: float = 0.15
    evidence_strength_weight: float = 0.35
    uncertainty_penalty_weight: float = 0.25
    cost_penalty_weight: float = 0.30
    risk_penalty_weight: float = 0.50
    near_duplicate_penalty: float = 0.25
    expected_gain_scale: float = 1.0
    cost_reference: float = 10.0

    def __post_init__(self) -> None:
        weight_names = (
            "expected_gain_weight",
            "exploration_weight",
            "novelty_weight",
            "diversity_weight",
            "evidence_strength_weight",
            "uncertainty_penalty_weight",
            "cost_penalty_weight",
            "risk_penalty_weight",
            "near_duplicate_penalty",
        )
        total = 0.0
        for name in weight_names:
            value = non_negative_number(getattr(self, name), name)
            object.__setattr__(self, name, value)
            total += value
        if total <= 0.0:
            raise ValueError("at least one utility coefficient must be positive")
        object.__setattr__(
            self,
            "expected_gain_scale",
            _positive_number(self.expected_gain_scale, "expected_gain_scale"),
        )
        object.__setattr__(
            self,
            "cost_reference",
            _positive_number(self.cost_reference, "cost_reference"),
        )

    @property
    def fingerprint(self) -> str:
        return stable_sha256(
            {
                "schema_version": 1,
                "expected_gain_weight": self.expected_gain_weight,
                "exploration_weight": self.exploration_weight,
                "novelty_weight": self.novelty_weight,
                "diversity_weight": self.diversity_weight,
                "evidence_strength_weight": self.evidence_strength_weight,
                "uncertainty_penalty_weight": self.uncertainty_penalty_weight,
                "cost_penalty_weight": self.cost_penalty_weight,
                "risk_penalty_weight": self.risk_penalty_weight,
                "near_duplicate_penalty": self.near_duplicate_penalty,
                "expected_gain_scale": self.expected_gain_scale,
                "cost_reference": self.cost_reference,
            }
        )


@dataclass(frozen=True, slots=True)
class ProposalUtilityAssessmentV1:
    proposal_id: str
    proposal_fingerprint: str
    admissibility_fingerprint: str
    novelty_assessment_fingerprint: str
    evidence_prior_fingerprint: str
    policy_fingerprint: str
    expected_gain: float
    normalized_expected_gain: float
    exploration_bonus: float
    novelty_score: float
    diversity_score: float
    evidence_strength: float
    uncertainty: float
    estimated_cost: float
    normalized_cost: float
    estimated_risk: float
    near_duplicate_penalty_applied: float
    utility_score: float
    provenance_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        safe_id(self.proposal_id, "proposal_id")
        for name in (
            "proposal_fingerprint",
            "admissibility_fingerprint",
            "novelty_assessment_fingerprint",
            "evidence_prior_fingerprint",
            "policy_fingerprint",
        ):
            object.__setattr__(
                self,
                name,
                sha256_hex(getattr(self, name), name),
            )
        object.__setattr__(
            self,
            "expected_gain",
            finite_number(self.expected_gain, "expected_gain"),
        )
        normalized_gain = finite_number(
            self.normalized_expected_gain,
            "normalized_expected_gain",
        )
        if normalized_gain < -1.0 or normalized_gain > 1.0:
            raise ValueError("normalized_expected_gain must be within [-1, 1]")
        object.__setattr__(self, "normalized_expected_gain", normalized_gain)
        for name in (
            "exploration_bonus",
            "novelty_score",
            "diversity_score",
            "evidence_strength",
            "uncertainty",
            "normalized_cost",
            "estimated_risk",
        ):
            object.__setattr__(self, name, _unit_interval(getattr(self, name), name))
        object.__setattr__(
            self,
            "estimated_cost",
            non_negative_number(self.estimated_cost, "estimated_cost"),
        )
        object.__setattr__(
            self,
            "near_duplicate_penalty_applied",
            non_negative_number(
                self.near_duplicate_penalty_applied,
                "near_duplicate_penalty_applied",
            ),
        )
        object.__setattr__(
            self,
            "utility_score",
            finite_number(self.utility_score, "utility_score"),
        )
        object.__setattr__(
            self,
            "provenance_ids",
            unique_text(
                self.provenance_ids,
                "provenance_ids",
                maximum=512,
                item_max_length=8_000,
            ),
        )

    @property
    def fingerprint(self) -> str:
        return stable_sha256(self._identity_dict())

    def _identity_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "proposal_id": self.proposal_id,
            "proposal_fingerprint": self.proposal_fingerprint,
            "admissibility_fingerprint": self.admissibility_fingerprint,
            "novelty_assessment_fingerprint": self.novelty_assessment_fingerprint,
            "evidence_prior_fingerprint": self.evidence_prior_fingerprint,
            "policy_fingerprint": self.policy_fingerprint,
            "expected_gain": self.expected_gain,
            "normalized_expected_gain": self.normalized_expected_gain,
            "exploration_bonus": self.exploration_bonus,
            "novelty_score": self.novelty_score,
            "diversity_score": self.diversity_score,
            "evidence_strength": self.evidence_strength,
            "uncertainty": self.uncertainty,
            "estimated_cost": self.estimated_cost,
            "normalized_cost": self.normalized_cost,
            "estimated_risk": self.estimated_risk,
            "near_duplicate_penalty_applied": self.near_duplicate_penalty_applied,
            "utility_score": self.utility_score,
            "provenance_ids": list(self.provenance_ids),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._identity_dict(), "fingerprint": self.fingerprint}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ProposalUtilityAssessmentV1":
        if raw.get("schema_version") != 1:
            raise ValueError("ProposalUtilityAssessmentV1 schema_version must be 1")
        result = cls(
            proposal_id=str(raw["proposal_id"]),
            proposal_fingerprint=str(raw["proposal_fingerprint"]),
            admissibility_fingerprint=str(raw["admissibility_fingerprint"]),
            novelty_assessment_fingerprint=str(
                raw["novelty_assessment_fingerprint"]
            ),
            evidence_prior_fingerprint=str(raw["evidence_prior_fingerprint"]),
            policy_fingerprint=str(raw["policy_fingerprint"]),
            expected_gain=float(raw["expected_gain"]),
            normalized_expected_gain=float(raw["normalized_expected_gain"]),
            exploration_bonus=float(raw["exploration_bonus"]),
            novelty_score=float(raw["novelty_score"]),
            diversity_score=float(raw["diversity_score"]),
            evidence_strength=float(raw["evidence_strength"]),
            uncertainty=float(raw["uncertainty"]),
            estimated_cost=float(raw["estimated_cost"]),
            normalized_cost=float(raw["normalized_cost"]),
            estimated_risk=float(raw["estimated_risk"]),
            near_duplicate_penalty_applied=float(
                raw["near_duplicate_penalty_applied"]
            ),
            utility_score=float(raw["utility_score"]),
            provenance_ids=tuple(raw.get("provenance_ids", ())),
        )
        expected = raw.get("fingerprint")
        if expected is not None and expected != result.fingerprint:
            raise ValueError("ProposalUtilityAssessmentV1 fingerprint mismatch")
        return result

    def to_proposal_assessment(self) -> ProposalAssessmentV1:
        return ProposalAssessmentV1(
            proposal_id=self.proposal_id,
            admissible=True,
            rejection_reasons=(),
            novelty_score=self.novelty_score,
            expected_gain=self.expected_gain,
            uncertainty=self.uncertainty,
            estimated_cost=self.estimated_cost,
            estimated_risk=self.estimated_risk,
            evidence_strength=self.evidence_strength,
            utility_score=self.utility_score,
            provenance_ids=self.provenance_ids,
        )


class ProposalUtilityScorerPort(Protocol):
    def assess_batch(
        self,
        proposals: Sequence[ResearchProposalV1],
        admissibility_results: Sequence[ProposalAdmissibilityResultV1],
        novelty_assessments: Sequence[ProposalNoveltyAssessmentV1],
        evidence_priors: Sequence[ProposalEvidencePriorV1],
    ) -> tuple[ProposalUtilityAssessmentV1, ...]: ...


@dataclass(frozen=True, slots=True)
class DeterministicProposalUtilityScorerV1:
    policy: ProposalUtilityPolicyV1 = ProposalUtilityPolicyV1()

    def __post_init__(self) -> None:
        if not isinstance(self.policy, ProposalUtilityPolicyV1):
            raise ValueError("policy must be ProposalUtilityPolicyV1")

    def assess_batch(
        self,
        proposals: Sequence[ResearchProposalV1],
        admissibility_results: Sequence[ProposalAdmissibilityResultV1],
        novelty_assessments: Sequence[ProposalNoveltyAssessmentV1],
        evidence_priors: Sequence[ProposalEvidencePriorV1],
    ) -> tuple[ProposalUtilityAssessmentV1, ...]:
        proposals_t = _typed_sequence(
            proposals,
            ResearchProposalV1,
            "proposals",
        )
        admissibility_t = _typed_sequence(
            admissibility_results,
            ProposalAdmissibilityResultV1,
            "admissibility_results",
        )
        novelty_t = _typed_sequence(
            novelty_assessments,
            ProposalNoveltyAssessmentV1,
            "novelty_assessments",
        )
        priors_t = _typed_sequence(
            evidence_priors,
            ProposalEvidencePriorV1,
            "evidence_priors",
        )

        proposal_ids = tuple(item.proposal_id for item in proposals_t)
        if len(set(proposal_ids)) != len(proposal_ids):
            raise ValueError("proposal batch must not contain duplicate proposal_id")

        admissibility_by_id = _one_to_one(admissibility_t, proposal_ids, "admissibility")
        novelty_by_id = _one_to_one(novelty_t, proposal_ids, "novelty")
        prior_by_id = _one_to_one(priors_t, proposal_ids, "evidence prior")

        assessments: list[ProposalUtilityAssessmentV1] = []
        for proposal in sorted(proposals_t, key=lambda item: item.proposal_id):
            admissibility = admissibility_by_id[proposal.proposal_id]
            novelty = novelty_by_id[proposal.proposal_id]
            prior = prior_by_id[proposal.proposal_id]

            if not admissibility.admissible:
                raise ValueError(
                    "R8.4 utility scoring accepts only R8.2-admissible proposals"
                )
            if admissibility.proposal_fingerprint != proposal.fingerprint:
                raise ValueError("admissibility proposal fingerprint mismatch")
            if novelty.proposal_fingerprint != proposal.fingerprint:
                raise ValueError("novelty proposal fingerprint mismatch")
            if novelty.admissibility_fingerprint != admissibility.fingerprint:
                raise ValueError("novelty/admissibility fingerprint mismatch")

            assessments.append(
                self._assess_one(
                    proposal=proposal,
                    admissibility=admissibility,
                    novelty=novelty,
                    prior=prior,
                )
            )

        return tuple(assessments)

    def _assess_one(
        self,
        *,
        proposal: ResearchProposalV1,
        admissibility: ProposalAdmissibilityResultV1,
        novelty: ProposalNoveltyAssessmentV1,
        prior: ProposalEvidencePriorV1,
    ) -> ProposalUtilityAssessmentV1:
        p = self.policy

        normalized_gain = _clamp_signed_unit(
            prior.expected_gain / p.expected_gain_scale
        )
        normalized_cost = _clamp_unit(
            proposal.estimated_cost / p.cost_reference
        )
        near_duplicate_penalty = (
            p.near_duplicate_penalty if novelty.near_duplicate else 0.0
        )

        utility = (
            p.expected_gain_weight * normalized_gain
            + p.exploration_weight * prior.exploration_bonus
            + p.novelty_weight * novelty.novelty_score
            + p.diversity_weight * novelty.diversity_score
            + p.evidence_strength_weight * prior.evidence_strength
            - p.uncertainty_penalty_weight * prior.uncertainty
            - p.cost_penalty_weight * normalized_cost
            - p.risk_penalty_weight * proposal.estimated_risk
            - near_duplicate_penalty
        )

        provenance = tuple(
            dict.fromkeys(
                (
                    *proposal.provenance_ids,
                    *prior.provenance_ids,
                    f"admissibility:{admissibility.fingerprint}",
                    f"novelty:{novelty.fingerprint}",
                    f"utility-policy:{p.fingerprint}",
                )
            )
        )

        return ProposalUtilityAssessmentV1(
            proposal_id=proposal.proposal_id,
            proposal_fingerprint=proposal.fingerprint,
            admissibility_fingerprint=admissibility.fingerprint,
            novelty_assessment_fingerprint=novelty.fingerprint,
            evidence_prior_fingerprint=prior.fingerprint,
            policy_fingerprint=p.fingerprint,
            expected_gain=prior.expected_gain,
            normalized_expected_gain=normalized_gain,
            exploration_bonus=prior.exploration_bonus,
            novelty_score=novelty.novelty_score,
            diversity_score=novelty.diversity_score,
            evidence_strength=prior.evidence_strength,
            uncertainty=prior.uncertainty,
            estimated_cost=proposal.estimated_cost,
            normalized_cost=normalized_cost,
            estimated_risk=proposal.estimated_risk,
            near_duplicate_penalty_applied=near_duplicate_penalty,
            utility_score=utility,
            provenance_ids=provenance,
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


def _one_to_one(
    values: Sequence[Any],
    proposal_ids: tuple[str, ...],
    name: str,
) -> dict[str, Any]:
    by_id = {item.proposal_id: item for item in values}
    if len(by_id) != len(values) or set(by_id) != set(proposal_ids):
        raise ValueError(f"{name} records must map one-to-one to proposal batch")
    return by_id

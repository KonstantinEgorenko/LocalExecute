'research campaign.R8.3 deterministic structural novelty and diversity scoring.\n\nThis layer scores only proposals that already passed R8.2 hard admissibility.\nIt uses typed proposal structure and lineage only: no embeddings, vector database,\nLLM similarity, utility ranking, or execution authority.\n'
from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Mapping, Protocol, Sequence

from .serialization import (
    canonical_json,
    non_negative_number,
    plain,
    safe_id,
    sha256_hex,
    stable_sha256,
)
from .strategy_admissibility import ProposalAdmissibilityResultV1
from .strategy_contracts import ResearchProposalV1


def _unit_interval(value: object, name: str) -> float:
    result = non_negative_number(value, name)
    if result > 1.0:
        raise ValueError(f"{name} must be <= 1")
    return result


@dataclass(frozen=True, slots=True)
class ProposalNoveltyPolicyV1:
    target_kind_weight: float = 0.10
    target_dimension_weight: float = 0.20
    target_key_weight: float = 0.15
    target_value_weight: float = 0.30
    source_strategy_weight: float = 0.10
    parent_lineage_weight: float = 0.15
    near_duplicate_similarity_threshold: float = 0.80
    max_historical_references: int = 10_000

    def __post_init__(self) -> None:
        weight_names = (
            "target_kind_weight",
            "target_dimension_weight",
            "target_key_weight",
            "target_value_weight",
            "source_strategy_weight",
            "parent_lineage_weight",
        )
        total = 0.0
        for name in weight_names:
            value = _unit_interval(getattr(self, name), name)
            object.__setattr__(self, name, value)
            total += value
        if abs(total - 1.0) > 1e-12:
            raise ValueError("novelty similarity weights must sum to 1")
        object.__setattr__(
            self,
            "near_duplicate_similarity_threshold",
            _unit_interval(
                self.near_duplicate_similarity_threshold,
                "near_duplicate_similarity_threshold",
            ),
        )
        if (
            isinstance(self.max_historical_references, bool)
            or not isinstance(self.max_historical_references, int)
            or self.max_historical_references <= 0
        ):
            raise ValueError("max_historical_references must be positive integer")
        if self.max_historical_references > 100_000:
            raise ValueError("max_historical_references must be <= 100000")


@dataclass(frozen=True, slots=True)
class ProposalNoveltyContextV1:
    campaign_id: str
    historical_proposals: tuple[ResearchProposalV1, ...] = ()

    def __post_init__(self) -> None:
        safe_id(self.campaign_id, "campaign_id")
        if not isinstance(self.historical_proposals, tuple):
            raise ValueError("historical_proposals must be tuple")
        if any(not isinstance(item, ResearchProposalV1) for item in self.historical_proposals):
            raise ValueError("historical_proposals must contain ResearchProposalV1")
        if any(item.campaign_id != self.campaign_id for item in self.historical_proposals):
            raise ValueError("historical proposals must belong to context campaign")
        fingerprints = tuple(item.fingerprint for item in self.historical_proposals)
        if len(set(fingerprints)) != len(fingerprints):
            raise ValueError("historical_proposals must not contain duplicate proposal fingerprints")

    @property
    def fingerprint(self) -> str:
        return stable_sha256(
            {
                "schema_version": 1,
                "campaign_id": self.campaign_id,
                "historical_proposal_fingerprints": sorted(
                    item.fingerprint for item in self.historical_proposals
                ),
            }
        )


@dataclass(frozen=True, slots=True)
class ProposalNoveltyAssessmentV1:
    proposal_id: str
    proposal_fingerprint: str
    admissibility_fingerprint: str
    context_fingerprint: str
    novelty_score: float
    diversity_score: float
    nearest_historical_proposal_id: str | None
    nearest_historical_similarity: float
    nearest_cohort_proposal_id: str | None
    nearest_cohort_similarity: float
    exact_duplicate: bool
    near_duplicate: bool

    def __post_init__(self) -> None:
        safe_id(self.proposal_id, "proposal_id")
        object.__setattr__(
            self,
            "proposal_fingerprint",
            sha256_hex(self.proposal_fingerprint, "proposal_fingerprint"),
        )
        object.__setattr__(
            self,
            "admissibility_fingerprint",
            sha256_hex(self.admissibility_fingerprint, "admissibility_fingerprint"),
        )
        object.__setattr__(
            self,
            "context_fingerprint",
            sha256_hex(self.context_fingerprint, "context_fingerprint"),
        )
        object.__setattr__(
            self,
            "novelty_score",
            _unit_interval(self.novelty_score, "novelty_score"),
        )
        object.__setattr__(
            self,
            "diversity_score",
            _unit_interval(self.diversity_score, "diversity_score"),
        )
        if self.nearest_historical_proposal_id is not None:
            safe_id(self.nearest_historical_proposal_id, "nearest_historical_proposal_id")
        object.__setattr__(
            self,
            "nearest_historical_similarity",
            _unit_interval(
                self.nearest_historical_similarity,
                "nearest_historical_similarity",
            ),
        )
        if self.nearest_cohort_proposal_id is not None:
            safe_id(self.nearest_cohort_proposal_id, "nearest_cohort_proposal_id")
        object.__setattr__(
            self,
            "nearest_cohort_similarity",
            _unit_interval(
                self.nearest_cohort_similarity,
                "nearest_cohort_similarity",
            ),
        )
        if not isinstance(self.exact_duplicate, bool):
            raise ValueError("exact_duplicate must be bool")
        if not isinstance(self.near_duplicate, bool):
            raise ValueError("near_duplicate must be bool")

    @property
    def fingerprint(self) -> str:
        return stable_sha256(self._identity_dict())

    def _identity_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "proposal_id": self.proposal_id,
            "proposal_fingerprint": self.proposal_fingerprint,
            "admissibility_fingerprint": self.admissibility_fingerprint,
            "context_fingerprint": self.context_fingerprint,
            "novelty_score": self.novelty_score,
            "diversity_score": self.diversity_score,
            "nearest_historical_proposal_id": self.nearest_historical_proposal_id,
            "nearest_historical_similarity": self.nearest_historical_similarity,
            "nearest_cohort_proposal_id": self.nearest_cohort_proposal_id,
            "nearest_cohort_similarity": self.nearest_cohort_similarity,
            "exact_duplicate": self.exact_duplicate,
            "near_duplicate": self.near_duplicate,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._identity_dict(), "fingerprint": self.fingerprint}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ProposalNoveltyAssessmentV1":
        if raw.get("schema_version") != 1:
            raise ValueError("ProposalNoveltyAssessmentV1 schema_version must be 1")
        result = cls(
            proposal_id=str(raw["proposal_id"]),
            proposal_fingerprint=str(raw["proposal_fingerprint"]),
            admissibility_fingerprint=str(raw["admissibility_fingerprint"]),
            context_fingerprint=str(raw["context_fingerprint"]),
            novelty_score=float(raw["novelty_score"]),
            diversity_score=float(raw["diversity_score"]),
            nearest_historical_proposal_id=(
                None
                if raw.get("nearest_historical_proposal_id") is None
                else str(raw["nearest_historical_proposal_id"])
            ),
            nearest_historical_similarity=float(raw["nearest_historical_similarity"]),
            nearest_cohort_proposal_id=(
                None
                if raw.get("nearest_cohort_proposal_id") is None
                else str(raw["nearest_cohort_proposal_id"])
            ),
            nearest_cohort_similarity=float(raw["nearest_cohort_similarity"]),
            exact_duplicate=raw["exact_duplicate"],
            near_duplicate=raw["near_duplicate"],
        )
        expected = raw.get("fingerprint")
        if expected is not None and expected != result.fingerprint:
            raise ValueError("ProposalNoveltyAssessmentV1 fingerprint mismatch")
        return result


class ProposalNoveltyScorerPort(Protocol):
    def assess_batch(
        self,
        proposals: Sequence[ResearchProposalV1],
        admissibility_results: Sequence[ProposalAdmissibilityResultV1],
        context: ProposalNoveltyContextV1,
    ) -> tuple[ProposalNoveltyAssessmentV1, ...]: ...


@dataclass(frozen=True, slots=True)
class DeterministicProposalNoveltyScorerV1:
    policy: ProposalNoveltyPolicyV1 = ProposalNoveltyPolicyV1()

    def __post_init__(self) -> None:
        if not isinstance(self.policy, ProposalNoveltyPolicyV1):
            raise ValueError("policy must be ProposalNoveltyPolicyV1")

    def assess_batch(
        self,
        proposals: Sequence[ResearchProposalV1],
        admissibility_results: Sequence[ProposalAdmissibilityResultV1],
        context: ProposalNoveltyContextV1,
    ) -> tuple[ProposalNoveltyAssessmentV1, ...]:
        if not isinstance(context, ProposalNoveltyContextV1):
            raise TypeError("context must be ProposalNoveltyContextV1")
        if len(context.historical_proposals) > self.policy.max_historical_references:
            raise ValueError("historical proposal count exceeds novelty policy bound")
        if not isinstance(proposals, Sequence) or isinstance(proposals, (str, bytes)):
            raise TypeError("proposals must be a sequence")
        if not isinstance(admissibility_results, Sequence) or isinstance(
            admissibility_results, (str, bytes)
        ):
            raise TypeError("admissibility_results must be a sequence")

        proposal_items = tuple(proposals)
        result_items = tuple(admissibility_results)
        if any(not isinstance(item, ResearchProposalV1) for item in proposal_items):
            raise TypeError("proposals must contain ResearchProposalV1")
        if any(
            not isinstance(item, ProposalAdmissibilityResultV1)
            for item in result_items
        ):
            raise TypeError(
                "admissibility_results must contain ProposalAdmissibilityResultV1"
            )

        proposal_ids = tuple(item.proposal_id for item in proposal_items)
        if len(set(proposal_ids)) != len(proposal_ids):
            raise ValueError("proposal batch must not contain duplicate proposal_id")
        if any(item.campaign_id != context.campaign_id for item in proposal_items):
            raise ValueError("proposal batch must match novelty context campaign")

        by_id = {item.proposal_id: item for item in result_items}
        if set(by_id) != set(proposal_ids) or len(by_id) != len(result_items):
            raise ValueError(
                "admissibility results must map one-to-one to proposal batch"
            )

        for proposal in proposal_items:
            admissibility = by_id[proposal.proposal_id]
            if admissibility.proposal_fingerprint != proposal.fingerprint:
                raise ValueError("admissibility result proposal fingerprint mismatch")
            if not admissibility.admissible:
                raise ValueError(
                    "R8.3 novelty scoring accepts only R8.2-admissible proposals"
                )

        ordered = tuple(sorted(proposal_items, key=lambda item: item.proposal_id))
        assessments = tuple(
            self._assess_one(
                proposal,
                admissibility=by_id[proposal.proposal_id],
                cohort=ordered,
                context=context,
            )
            for proposal in ordered
        )
        return assessments

    def similarity(
        self,
        left: ResearchProposalV1,
        right: ResearchProposalV1,
    ) -> float:
        if not isinstance(left, ResearchProposalV1) or not isinstance(
            right, ResearchProposalV1
        ):
            raise TypeError("similarity requires ResearchProposalV1")
        if left.novelty_fingerprint == right.novelty_fingerprint:
            return 1.0

        p = self.policy
        score = (
            p.target_kind_weight
            * float(left.target_kind is right.target_kind)
            + p.target_dimension_weight
            * float(left.target_dimension == right.target_dimension)
            + p.target_key_weight
            * float(left.target_key == right.target_key)
            + p.target_value_weight
            * _value_similarity(left.target_value, right.target_value)
            + p.source_strategy_weight
            * float(left.source_strategy_id == right.source_strategy_id)
            + p.parent_lineage_weight
            * _jaccard(
                frozenset(left.parent_experiment_ids),
                frozenset(right.parent_experiment_ids),
            )
        )
        return min(1.0, max(0.0, score))

    def _assess_one(
        self,
        proposal: ResearchProposalV1,
        *,
        admissibility: ProposalAdmissibilityResultV1,
        cohort: tuple[ResearchProposalV1, ...],
        context: ProposalNoveltyContextV1,
    ) -> ProposalNoveltyAssessmentV1:
        historical_id, historical_similarity, exact_historical = self._nearest(
            proposal,
            context.historical_proposals,
        )
        cohort_refs = tuple(
            item for item in cohort if item.proposal_id != proposal.proposal_id
        )
        cohort_id, cohort_similarity, exact_cohort = self._nearest(
            proposal,
            cohort_refs,
        )

        novelty_score = 1.0 - historical_similarity
        diversity_score = 1.0 - cohort_similarity
        exact_duplicate = exact_historical or exact_cohort
        nearest_similarity = max(historical_similarity, cohort_similarity)
        near_duplicate = (
            not exact_duplicate
            and nearest_similarity >= self.policy.near_duplicate_similarity_threshold
        )

        return ProposalNoveltyAssessmentV1(
            proposal_id=proposal.proposal_id,
            proposal_fingerprint=proposal.fingerprint,
            admissibility_fingerprint=admissibility.fingerprint,
            context_fingerprint=context.fingerprint,
            novelty_score=novelty_score,
            diversity_score=diversity_score,
            nearest_historical_proposal_id=historical_id,
            nearest_historical_similarity=historical_similarity,
            nearest_cohort_proposal_id=cohort_id,
            nearest_cohort_similarity=cohort_similarity,
            exact_duplicate=exact_duplicate,
            near_duplicate=near_duplicate,
        )

    def _nearest(
        self,
        proposal: ResearchProposalV1,
        references: Sequence[ResearchProposalV1],
    ) -> tuple[str | None, float, bool]:
        best_id: str | None = None
        best_similarity = 0.0
        exact = False
        for reference in sorted(references, key=lambda item: item.proposal_id):
            similarity = self.similarity(proposal, reference)
            is_exact = proposal.novelty_fingerprint == reference.novelty_fingerprint
            if (
                similarity > best_similarity
                or (
                    similarity == best_similarity
                    and best_id is not None
                    and reference.proposal_id < best_id
                )
                or best_id is None
            ):
                best_id = reference.proposal_id
                best_similarity = similarity
                exact = is_exact
        return best_id, best_similarity, exact


def _value_similarity(left: Any, right: Any) -> float:
    left_plain = plain(left)
    right_plain = plain(right)
    if canonical_json(left_plain) == canonical_json(right_plain):
        return 1.0

    if _is_number(left_plain) and _is_number(right_plain):
        left_number = float(left_plain)
        right_number = float(right_plain)
        denominator = max(abs(left_number), abs(right_number), 1.0)
        return max(0.0, 1.0 - abs(left_number - right_number) / denominator)

    left_text = canonical_json(left_plain).casefold()
    right_text = canonical_json(right_plain).casefold()
    return SequenceMatcher(None, left_text, right_text, autojunk=False).ratio()


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    if not left and not right:
        return 1.0
    union = left | right
    if not union:
        return 1.0
    return len(left & right) / len(union)

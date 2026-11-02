'research campaign.R8.5 deterministic sequential portfolio selection.\n\nR8.5 ranks admissible utility-assessed proposals, preserves hard rejection\nevidence, selects at most one physical next experiment, and emits a durable\nResearchStrategyDecisionV1 plus a complete selection audit wrapper.\n\nThis module does not execute or persist an experiment. R8.7 will make the\ndecision/PLANNED-experiment boundary durable across process restart.\n'
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping, Protocol, Sequence

from .serialization import (
    finite_number,
    non_negative_int,
    safe_id,
    sha256_hex,
    stable_sha256,
    unique_text,
)
from .strategy_admissibility import (
    ProposalAdmissibilityReasonV1,
    ProposalAdmissibilityResultV1,
)
from .strategy_contracts import (
    ResearchProposalV1,
    ResearchStrategyBudgetV1,
    ResearchStrategyDecisionV1,
)
from .strategy_utility import ProposalUtilityAssessmentV1


class ProposalPortfolioStatusV1(StrEnum):
    SELECTED = "SELECTED"
    DEFERRED = "DEFERRED"
    HARD_REJECTED = "HARD_REJECTED"


@dataclass(frozen=True, slots=True)
class SequentialPortfolioPolicyV1:
    max_ranked_proposals: int = 512
    reserve_one_experiment_on_selection: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "max_ranked_proposals",
            non_negative_int(self.max_ranked_proposals, "max_ranked_proposals"),
        )
        if self.max_ranked_proposals <= 0:
            raise ValueError("max_ranked_proposals must be > 0")
        if self.max_ranked_proposals > 10_000:
            raise ValueError("max_ranked_proposals must be <= 10000")
        if not isinstance(self.reserve_one_experiment_on_selection, bool):
            raise ValueError("reserve_one_experiment_on_selection must be bool")

    @property
    def fingerprint(self) -> str:
        return stable_sha256(
            {
                "schema_version": 1,
                "max_ranked_proposals": self.max_ranked_proposals,
                "reserve_one_experiment_on_selection": (
                    self.reserve_one_experiment_on_selection
                ),
                "ranking": (
                    "utility_desc,novelty_desc,diversity_desc,"
                    "uncertainty_asc,cost_asc,risk_asc,proposal_id_asc"
                ),
                "hard_rejected_placement": "after_admissible_by_proposal_id",
            }
        )


@dataclass(frozen=True, slots=True)
class ProposalPortfolioRankEntryV1:
    rank: int
    proposal_id: str
    status: ProposalPortfolioStatusV1
    admissibility_fingerprint: str
    utility_assessment_fingerprint: str | None
    utility_score: float | None
    rejection_reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        if isinstance(self.rank, bool) or not isinstance(self.rank, int) or self.rank <= 0:
            raise ValueError("rank must be positive integer")
        safe_id(self.proposal_id, "proposal_id")
        if not isinstance(self.status, ProposalPortfolioStatusV1):
            raise ValueError("status must be ProposalPortfolioStatusV1")
        object.__setattr__(
            self,
            "admissibility_fingerprint",
            sha256_hex(
                self.admissibility_fingerprint,
                "admissibility_fingerprint",
            ),
        )

        utility_fp = self.utility_assessment_fingerprint
        if utility_fp is not None:
            utility_fp = sha256_hex(
                utility_fp,
                "utility_assessment_fingerprint",
            )
        object.__setattr__(self, "utility_assessment_fingerprint", utility_fp)

        utility = self.utility_score
        if utility is not None:
            utility = finite_number(utility, "utility_score")
        object.__setattr__(self, "utility_score", utility)

        reasons = unique_text(
            self.rejection_reasons,
            "rejection_reasons",
            maximum=128,
            item_max_length=4_000,
        )
        object.__setattr__(self, "rejection_reasons", reasons)

        if self.status is ProposalPortfolioStatusV1.HARD_REJECTED:
            if utility_fp is not None or utility is not None:
                raise ValueError(
                    "HARD_REJECTED entry cannot contain utility assessment"
                )
            if not reasons:
                raise ValueError(
                    "HARD_REJECTED entry requires rejection_reasons"
                )
        else:
            if utility_fp is None or utility is None:
                raise ValueError(
                    "admissible portfolio entry requires utility assessment"
                )
            if reasons:
                raise ValueError(
                    "admissible portfolio entry cannot contain rejection_reasons"
                )

    @property
    def fingerprint(self) -> str:
        return stable_sha256(self._identity_dict())

    def _identity_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "rank": self.rank,
            "proposal_id": self.proposal_id,
            "status": self.status.value,
            "admissibility_fingerprint": self.admissibility_fingerprint,
            "utility_assessment_fingerprint": self.utility_assessment_fingerprint,
            "utility_score": self.utility_score,
            "rejection_reasons": list(self.rejection_reasons),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._identity_dict(), "fingerprint": self.fingerprint}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ProposalPortfolioRankEntryV1":
        if raw.get("schema_version") != 1:
            raise ValueError("ProposalPortfolioRankEntryV1 schema_version must be 1")
        result = cls(
            rank=int(raw["rank"]),
            proposal_id=str(raw["proposal_id"]),
            status=ProposalPortfolioStatusV1(str(raw["status"])),
            admissibility_fingerprint=str(raw["admissibility_fingerprint"]),
            utility_assessment_fingerprint=(
                None
                if raw.get("utility_assessment_fingerprint") is None
                else str(raw["utility_assessment_fingerprint"])
            ),
            utility_score=(
                None
                if raw.get("utility_score") is None
                else float(raw["utility_score"])
            ),
            rejection_reasons=tuple(raw.get("rejection_reasons", ())),
        )
        expected = raw.get("fingerprint")
        if expected is not None and expected != result.fingerprint:
            raise ValueError("ProposalPortfolioRankEntryV1 fingerprint mismatch")
        return result


@dataclass(frozen=True, slots=True)
class ResearchPortfolioSelectionV1:
    decision: ResearchStrategyDecisionV1
    rank_entries: tuple[ProposalPortfolioRankEntryV1, ...]
    budget_before: ResearchStrategyBudgetV1
    budget_after: ResearchStrategyBudgetV1
    policy_fingerprint: str

    def __post_init__(self) -> None:
        if not isinstance(self.decision, ResearchStrategyDecisionV1):
            raise ValueError("decision must be ResearchStrategyDecisionV1")
        if not isinstance(self.rank_entries, tuple) or not self.rank_entries:
            raise ValueError("rank_entries must be non-empty tuple")
        if any(
            not isinstance(item, ProposalPortfolioRankEntryV1)
            for item in self.rank_entries
        ):
            raise ValueError(
                "rank_entries must contain ProposalPortfolioRankEntryV1"
            )
        if not isinstance(self.budget_before, ResearchStrategyBudgetV1):
            raise ValueError("budget_before must be ResearchStrategyBudgetV1")
        if not isinstance(self.budget_after, ResearchStrategyBudgetV1):
            raise ValueError("budget_after must be ResearchStrategyBudgetV1")
        object.__setattr__(
            self,
            "policy_fingerprint",
            sha256_hex(self.policy_fingerprint, "policy_fingerprint"),
        )

        ranks = tuple(item.rank for item in self.rank_entries)
        if ranks != tuple(range(1, len(self.rank_entries) + 1)):
            raise ValueError("rank_entries must have contiguous rank order")

        ids = tuple(item.proposal_id for item in self.rank_entries)
        if len(set(ids)) != len(ids):
            raise ValueError("rank_entries must not contain duplicate proposal_id")
        if ids != self.decision.ranked_proposal_ids:
            raise ValueError(
                "rank_entries order must equal decision.ranked_proposal_ids"
            )
        if self.decision.budget_snapshot != self.budget_before:
            raise ValueError("decision budget_snapshot must equal budget_before")

        selected = tuple(
            item.proposal_id
            for item in self.rank_entries
            if item.status is ProposalPortfolioStatusV1.SELECTED
        )
        if len(selected) > 1:
            raise ValueError("sequential portfolio can select at most one proposal")
        expected_selected = selected[0] if selected else None
        if self.decision.selected_proposal_id != expected_selected:
            raise ValueError("decision selected_proposal_id mismatch")

        rejected = tuple(
            item.proposal_id
            for item in self.rank_entries
            if item.status is ProposalPortfolioStatusV1.HARD_REJECTED
        )
        if set(rejected) != set(self.decision.rejected_proposal_ids):
            raise ValueError("decision rejected_proposal_ids mismatch")

        expected_after = (
            self.budget_before.remaining_experiments - 1
            if expected_selected is not None
            else self.budget_before.remaining_experiments
        )
        if self.budget_after.remaining_experiments != expected_after:
            raise ValueError("budget_after remaining_experiments mismatch")
        for name in (
            "remaining_model_calls",
            "remaining_tool_calls",
            "wall_clock_class",
            "allowed_mutation_scope",
            "protected_surfaces",
            "max_risk_class",
        ):
            if getattr(self.budget_after, name) != getattr(self.budget_before, name):
                raise ValueError(
                    f"selection must not mutate budget field {name}"
                )

    @property
    def fingerprint(self) -> str:
        return stable_sha256(
            {
                "schema_version": 1,
                "decision": self.decision.to_dict(),
                "rank_entries": [item.to_dict() for item in self.rank_entries],
                "budget_before": self.budget_before.to_dict(),
                "budget_after": self.budget_after.to_dict(),
                "policy_fingerprint": self.policy_fingerprint,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "decision": self.decision.to_dict(),
            "rank_entries": [item.to_dict() for item in self.rank_entries],
            "budget_before": self.budget_before.to_dict(),
            "budget_after": self.budget_after.to_dict(),
            "policy_fingerprint": self.policy_fingerprint,
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ResearchPortfolioSelectionV1":
        if raw.get("schema_version") != 1:
            raise ValueError("ResearchPortfolioSelectionV1 schema_version must be 1")
        result = cls(
            decision=ResearchStrategyDecisionV1.from_dict(raw["decision"]),
            rank_entries=tuple(
                ProposalPortfolioRankEntryV1.from_dict(item)
                for item in raw["rank_entries"]
            ),
            budget_before=ResearchStrategyBudgetV1.from_dict(
                raw["budget_before"]
            ),
            budget_after=ResearchStrategyBudgetV1.from_dict(
                raw["budget_after"]
            ),
            policy_fingerprint=str(raw["policy_fingerprint"]),
        )
        expected = raw.get("fingerprint")
        if expected is not None and expected != result.fingerprint:
            raise ValueError("ResearchPortfolioSelectionV1 fingerprint mismatch")
        return result


class SequentialPortfolioSelectorPort(Protocol):
    def select(
        self,
        proposals: Sequence[ResearchProposalV1],
        admissibility_results: Sequence[ProposalAdmissibilityResultV1],
        utility_assessments: Sequence[ProposalUtilityAssessmentV1],
        budget_snapshot: ResearchStrategyBudgetV1,
    ) -> ResearchPortfolioSelectionV1: ...


@dataclass(frozen=True, slots=True)
class DeterministicSequentialPortfolioSelectorV1:
    policy: SequentialPortfolioPolicyV1 = SequentialPortfolioPolicyV1()

    def __post_init__(self) -> None:
        if not isinstance(self.policy, SequentialPortfolioPolicyV1):
            raise ValueError("policy must be SequentialPortfolioPolicyV1")

    def select(
        self,
        proposals: Sequence[ResearchProposalV1],
        admissibility_results: Sequence[ProposalAdmissibilityResultV1],
        utility_assessments: Sequence[ProposalUtilityAssessmentV1],
        budget_snapshot: ResearchStrategyBudgetV1,
    ) -> ResearchPortfolioSelectionV1:
        if not isinstance(budget_snapshot, ResearchStrategyBudgetV1):
            raise TypeError("budget_snapshot must be ResearchStrategyBudgetV1")

        proposal_items = _typed_sequence(
            proposals,
            ResearchProposalV1,
            "proposals",
        )
        admissibility_items = _typed_sequence(
            admissibility_results,
            ProposalAdmissibilityResultV1,
            "admissibility_results",
        )
        utility_items = _typed_sequence(
            utility_assessments,
            ProposalUtilityAssessmentV1,
            "utility_assessments",
        )

        if not proposal_items:
            raise ValueError("portfolio selection requires at least one proposal")
        if len(proposal_items) > self.policy.max_ranked_proposals:
            raise ValueError("proposal count exceeds portfolio policy bound")

        proposal_ids = tuple(item.proposal_id for item in proposal_items)
        if len(set(proposal_ids)) != len(proposal_ids):
            raise ValueError("proposal batch must not contain duplicate proposal_id")

        campaigns = {item.campaign_id for item in proposal_items}
        if len(campaigns) != 1:
            raise ValueError("portfolio proposals must belong to one campaign")
        campaign_id = next(iter(campaigns))

        admissibility_by_id = _one_to_one(
            admissibility_items,
            proposal_ids,
            "admissibility",
        )
        utility_by_id = {item.proposal_id: item for item in utility_items}
        if len(utility_by_id) != len(utility_items):
            raise ValueError(
                "utility assessments must not contain duplicate proposal_id"
            )

        admissible_ids: list[str] = []
        rejected_ids: list[str] = []
        for proposal in proposal_items:
            result = admissibility_by_id[proposal.proposal_id]
            if result.proposal_fingerprint != proposal.fingerprint:
                raise ValueError("admissibility proposal fingerprint mismatch")
            if result.admissible:
                admissible_ids.append(proposal.proposal_id)
            else:
                rejected_ids.append(proposal.proposal_id)

        if set(utility_by_id) != set(admissible_ids):
            raise ValueError(
                "utility assessments must exist exactly for admissible proposals"
            )

        proposal_by_id = {item.proposal_id: item for item in proposal_items}
        for proposal_id in admissible_ids:
            utility = utility_by_id[proposal_id]
            if utility.proposal_fingerprint != proposal_by_id[proposal_id].fingerprint:
                raise ValueError("utility proposal fingerprint mismatch")
            if (
                utility.admissibility_fingerprint
                != admissibility_by_id[proposal_id].fingerprint
            ):
                raise ValueError("utility/admissibility fingerprint mismatch")

        if admissible_ids and budget_snapshot.remaining_experiments <= 0:
            raise ValueError(
                "admissible proposals cannot be selected with exhausted experiment budget"
            )

        ranked_admissible = sorted(
            admissible_ids,
            key=lambda proposal_id: _utility_rank_key(
                utility_by_id[proposal_id]
            ),
        )
        ranked_rejected = sorted(rejected_ids)
        ranked_ids = tuple((*ranked_admissible, *ranked_rejected))
        selected_id = ranked_admissible[0] if ranked_admissible else None

        entries: list[ProposalPortfolioRankEntryV1] = []
        for index, proposal_id in enumerate(ranked_ids, start=1):
            admissibility = admissibility_by_id[proposal_id]
            if proposal_id in utility_by_id:
                utility = utility_by_id[proposal_id]
                status = (
                    ProposalPortfolioStatusV1.SELECTED
                    if proposal_id == selected_id
                    else ProposalPortfolioStatusV1.DEFERRED
                )
                entries.append(
                    ProposalPortfolioRankEntryV1(
                        rank=index,
                        proposal_id=proposal_id,
                        status=status,
                        admissibility_fingerprint=admissibility.fingerprint,
                        utility_assessment_fingerprint=utility.fingerprint,
                        utility_score=utility.utility_score,
                        rejection_reasons=(),
                    )
                )
            else:
                entries.append(
                    ProposalPortfolioRankEntryV1(
                        rank=index,
                        proposal_id=proposal_id,
                        status=ProposalPortfolioStatusV1.HARD_REJECTED,
                        admissibility_fingerprint=admissibility.fingerprint,
                        utility_assessment_fingerprint=None,
                        utility_score=None,
                        rejection_reasons=tuple(
                            item.value for item in admissibility.reasons
                        ),
                    )
                )

        budget_after = _budget_after_selection(
            budget_snapshot,
            selected=selected_id is not None,
            reserve=self.policy.reserve_one_experiment_on_selection,
        )

        provenance = (
            *(f"admissibility:{fingerprint}" for fingerprint in sorted(
                item.fingerprint for item in admissibility_items
            )),
            *(f"utility:{fingerprint}" for fingerprint in sorted(
                item.fingerprint for item in utility_items
            )),
            f"portfolio-policy:{self.policy.fingerprint}",
        )

        decision_seed = stable_sha256(
            {
                "schema_version": 1,
                "campaign_id": campaign_id,
                "ranked_proposal_ids": list(ranked_ids),
                "selected_proposal_id": selected_id,
                "rejected_proposal_ids": sorted(rejected_ids),
                "budget_before": budget_snapshot.to_dict(),
                "budget_after": budget_after.to_dict(),
                "policy_fingerprint": self.policy.fingerprint,
                "admissibility_fingerprints": sorted(
                    item.fingerprint for item in admissibility_items
                ),
                "utility_fingerprints": sorted(
                    item.fingerprint for item in utility_items
                ),
            }
        )
        rationale = _rationale(
            selected_id=selected_id,
            ranked_admissible=ranked_admissible,
            utility_by_id=utility_by_id,
            rejected_count=len(rejected_ids),
        )

        decision = ResearchStrategyDecisionV1(
            decision_id=f"r85d-{decision_seed[:24]}",
            campaign_id=campaign_id,
            ranked_proposal_ids=ranked_ids,
            selected_proposal_id=selected_id,
            rejected_proposal_ids=tuple(sorted(rejected_ids)),
            budget_snapshot=budget_snapshot,
            rationale=rationale,
            provenance_ids=provenance,
        )

        return ResearchPortfolioSelectionV1(
            decision=decision,
            rank_entries=tuple(entries),
            budget_before=budget_snapshot,
            budget_after=budget_after,
            policy_fingerprint=self.policy.fingerprint,
        )


def _utility_rank_key(
    item: ProposalUtilityAssessmentV1,
) -> tuple[float, float, float, float, float, float, str]:
    return (
        -item.utility_score,
        -item.novelty_score,
        -item.diversity_score,
        item.uncertainty,
        item.normalized_cost,
        item.estimated_risk,
        item.proposal_id,
    )


def _budget_after_selection(
    budget: ResearchStrategyBudgetV1,
    *,
    selected: bool,
    reserve: bool,
) -> ResearchStrategyBudgetV1:
    decrement = 1 if selected and reserve else 0
    if decrement and budget.remaining_experiments <= 0:
        raise ValueError("cannot reserve experiment from exhausted budget")
    return ResearchStrategyBudgetV1(
        remaining_experiments=budget.remaining_experiments - decrement,
        remaining_model_calls=budget.remaining_model_calls,
        remaining_tool_calls=budget.remaining_tool_calls,
        wall_clock_class=budget.wall_clock_class,
        allowed_mutation_scope=budget.allowed_mutation_scope,
        protected_surfaces=budget.protected_surfaces,
        max_risk_class=budget.max_risk_class,
    )


def _rationale(
    *,
    selected_id: str | None,
    ranked_admissible: list[str],
    utility_by_id: dict[str, ProposalUtilityAssessmentV1],
    rejected_count: int,
) -> str:
    if selected_id is None:
        return (
            "No admissible proposal is available; no sequential experiment was "
            f"selected. hard_rejected={rejected_count}."
        )
    selected = utility_by_id[selected_id]
    return (
        f"Selected {selected_id} as rank 1 of {len(ranked_admissible)} admissible "
        f"proposals by deterministic utility ordering "
        f"(utility={selected.utility_score:.12g}); "
        f"hard_rejected={rejected_count}. Exactly one experiment budget slot "
        "is reserved; no execution authority is granted by this decision."
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

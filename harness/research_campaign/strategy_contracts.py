'research campaign.R8 immutable research strategy proposal and decision contracts.'
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping

from .serialization import (
    finite_number,
    freeze_json,
    non_negative_int,
    non_negative_number,
    plain,
    safe_id,
    stable_sha256,
    text,
    unique_ids,
    unique_text,
)


class ResearchStrategyAuthorityV1(StrEnum):
    """Authority carried by R8 strategy-layer records.

    R8 chooses/proposes research work. It does not receive a direct workspace
    mutation authority; physical coding remains behind the existing R6/R7
    production execution boundary.
    """

    PROPOSAL_ONLY = "PROPOSAL_ONLY"


class ResearchProposalTargetKindV1(StrEnum):
    """Representable proposal targets.

    DATASET/EVALUATOR are intentionally representable so the later R8.2
    admissibility authority can reject protected-scope proposals explicitly
    instead of losing audit evidence at deserialization time.
    """

    EXECUTION = "EXECUTION"
    MODEL = "MODEL"
    PROMPT = "PROMPT"
    TOOL_POLICY = "TOOL_POLICY"
    DATASET = "DATASET"
    EVALUATOR = "EVALUATOR"


class ResearchRiskClassV1(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


def _unit_interval(value: object, name: str) -> float:
    result = non_negative_number(value, name)
    if result > 1.0:
        raise ValueError(f"{name} must be <= 1")
    return result


@dataclass(frozen=True, slots=True)
class ResearchStrategyBudgetV1:
    """Remaining budget snapshot used by one R8 strategy decision.

    This is deliberately distinct from ``ResearchBudgetV1`` in contracts.py.
    The existing type is the authoritative hard campaign envelope. This type is
    a durable *remaining* snapshot used for proposal ranking/selection.
    """

    remaining_experiments: int
    remaining_model_calls: int
    remaining_tool_calls: int
    wall_clock_class: str
    allowed_mutation_scope: tuple[str, ...]
    protected_surfaces: tuple[str, ...]
    max_risk_class: ResearchRiskClassV1

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "remaining_experiments",
            non_negative_int(self.remaining_experiments, "remaining_experiments"),
        )
        object.__setattr__(
            self,
            "remaining_model_calls",
            non_negative_int(self.remaining_model_calls, "remaining_model_calls"),
        )
        object.__setattr__(
            self,
            "remaining_tool_calls",
            non_negative_int(self.remaining_tool_calls, "remaining_tool_calls"),
        )
        object.__setattr__(
            self,
            "wall_clock_class",
            text(self.wall_clock_class, "wall_clock_class", max_length=256),
        )
        object.__setattr__(
            self,
            "allowed_mutation_scope",
            unique_text(
                self.allowed_mutation_scope,
                "allowed_mutation_scope",
                maximum=512,
                item_max_length=4_000,
            ),
        )
        object.__setattr__(
            self,
            "protected_surfaces",
            unique_text(
                self.protected_surfaces,
                "protected_surfaces",
                maximum=64,
                item_max_length=512,
            ),
        )
        if not isinstance(self.max_risk_class, ResearchRiskClassV1):
            raise ValueError("max_risk_class must be ResearchRiskClassV1")

    @property
    def fingerprint(self) -> str:
        return stable_sha256(self._identity_dict())

    def _identity_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "remaining_experiments": self.remaining_experiments,
            "remaining_model_calls": self.remaining_model_calls,
            "remaining_tool_calls": self.remaining_tool_calls,
            "wall_clock_class": self.wall_clock_class,
            "allowed_mutation_scope": list(self.allowed_mutation_scope),
            "protected_surfaces": list(self.protected_surfaces),
            "max_risk_class": self.max_risk_class.value,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._identity_dict(), "fingerprint": self.fingerprint}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ResearchStrategyBudgetV1":
        if raw.get("schema_version") != 1:
            raise ValueError("ResearchStrategyBudgetV1 schema_version must be 1")
        result = cls(
            remaining_experiments=int(raw["remaining_experiments"]),
            remaining_model_calls=int(raw["remaining_model_calls"]),
            remaining_tool_calls=int(raw["remaining_tool_calls"]),
            wall_clock_class=str(raw["wall_clock_class"]),
            allowed_mutation_scope=tuple(raw.get("allowed_mutation_scope", ())),
            protected_surfaces=tuple(raw.get("protected_surfaces", ())),
            max_risk_class=ResearchRiskClassV1(str(raw["max_risk_class"])),
        )
        expected = raw.get("fingerprint")
        if expected is not None and expected != result.fingerprint:
            raise ValueError("ResearchStrategyBudgetV1 fingerprint mismatch")
        return result


@dataclass(frozen=True, slots=True)
class ResearchProposalV1:
    proposal_id: str
    campaign_id: str
    source_strategy_id: str
    parent_experiment_ids: tuple[str, ...]
    hypothesis_statement: str
    mechanism: str
    falsifier: str
    target_kind: ResearchProposalTargetKindV1
    target_dimension: str
    target_key: str
    target_value: Any
    predicted_effect: str
    estimated_cost: float
    estimated_risk: float
    provenance_ids: tuple[str, ...]
    authority: ResearchStrategyAuthorityV1 = ResearchStrategyAuthorityV1.PROPOSAL_ONLY

    def __post_init__(self) -> None:
        safe_id(self.proposal_id, "proposal_id")
        safe_id(self.campaign_id, "campaign_id")
        safe_id(self.source_strategy_id, "source_strategy_id")

        parents = unique_ids(
            self.parent_experiment_ids,
            "parent_experiment_ids",
            maximum=128,
        )
        object.__setattr__(self, "parent_experiment_ids", parents)

        object.__setattr__(
            self,
            "hypothesis_statement",
            text(self.hypothesis_statement, "hypothesis_statement", max_length=32_000),
        )
        object.__setattr__(
            self,
            "mechanism",
            text(self.mechanism, "mechanism", max_length=32_000),
        )
        object.__setattr__(
            self,
            "falsifier",
            text(self.falsifier, "falsifier", max_length=32_000),
        )

        if not isinstance(self.target_kind, ResearchProposalTargetKindV1):
            raise ValueError("target_kind must be ResearchProposalTargetKindV1")

        object.__setattr__(
            self,
            "target_dimension",
            text(self.target_dimension, "target_dimension", max_length=1_000),
        )
        object.__setattr__(
            self,
            "target_key",
            text(self.target_key, "target_key", max_length=4_000),
        )
        object.__setattr__(
            self,
            "target_value",
            freeze_json(self.target_value, "target_value"),
        )
        object.__setattr__(
            self,
            "predicted_effect",
            text(self.predicted_effect, "predicted_effect", max_length=16_000),
        )
        object.__setattr__(
            self,
            "estimated_cost",
            non_negative_number(self.estimated_cost, "estimated_cost"),
        )
        object.__setattr__(
            self,
            "estimated_risk",
            _unit_interval(self.estimated_risk, "estimated_risk"),
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

        if self.authority is not ResearchStrategyAuthorityV1.PROPOSAL_ONLY:
            raise ValueError("R8 strategy proposal authority must be PROPOSAL_ONLY")

    @property
    def novelty_fingerprint(self) -> str:
        """Semantic identity for exact proposal deduplication.

        Operational estimates, proposal IDs and provenance are excluded so a
        regenerated equivalent hypothesis remains the same research idea.
        """

        return stable_sha256(
            {
                "schema_version": 1,
                "source_strategy_id": self.source_strategy_id,
                "parent_experiment_ids": list(self.parent_experiment_ids),
                "hypothesis_statement": self.hypothesis_statement,
                "mechanism": self.mechanism,
                "falsifier": self.falsifier,
                "target_kind": self.target_kind.value,
                "target_dimension": self.target_dimension,
                "target_key": self.target_key,
                "target_value": plain(self.target_value),
                "predicted_effect": self.predicted_effect,
            }
        )

    @property
    def fingerprint(self) -> str:
        return stable_sha256(self._identity_dict())

    def _identity_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "proposal_id": self.proposal_id,
            "campaign_id": self.campaign_id,
            "source_strategy_id": self.source_strategy_id,
            "parent_experiment_ids": list(self.parent_experiment_ids),
            "hypothesis_statement": self.hypothesis_statement,
            "mechanism": self.mechanism,
            "falsifier": self.falsifier,
            "target_kind": self.target_kind.value,
            "target_dimension": self.target_dimension,
            "target_key": self.target_key,
            "target_value": plain(self.target_value),
            "predicted_effect": self.predicted_effect,
            "estimated_cost": self.estimated_cost,
            "estimated_risk": self.estimated_risk,
            "provenance_ids": list(self.provenance_ids),
            "authority": self.authority.value,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self._identity_dict(),
            "novelty_fingerprint": self.novelty_fingerprint,
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ResearchProposalV1":
        if raw.get("schema_version") != 1:
            raise ValueError("ResearchProposalV1 schema_version must be 1")
        result = cls(
            proposal_id=str(raw["proposal_id"]),
            campaign_id=str(raw["campaign_id"]),
            source_strategy_id=str(raw["source_strategy_id"]),
            parent_experiment_ids=tuple(raw.get("parent_experiment_ids", ())),
            hypothesis_statement=str(raw["hypothesis_statement"]),
            mechanism=str(raw["mechanism"]),
            falsifier=str(raw["falsifier"]),
            target_kind=ResearchProposalTargetKindV1(str(raw["target_kind"])),
            target_dimension=str(raw["target_dimension"]),
            target_key=str(raw["target_key"]),
            target_value=raw["target_value"],
            predicted_effect=str(raw["predicted_effect"]),
            estimated_cost=float(raw["estimated_cost"]),
            estimated_risk=float(raw["estimated_risk"]),
            provenance_ids=tuple(raw.get("provenance_ids", ())),
            authority=ResearchStrategyAuthorityV1(
                str(raw.get("authority", ResearchStrategyAuthorityV1.PROPOSAL_ONLY.value))
            ),
        )
        expected_novelty = raw.get("novelty_fingerprint")
        if expected_novelty is not None and expected_novelty != result.novelty_fingerprint:
            raise ValueError("ResearchProposalV1 novelty_fingerprint mismatch")
        expected = raw.get("fingerprint")
        if expected is not None and expected != result.fingerprint:
            raise ValueError("ResearchProposalV1 fingerprint mismatch")
        return result


@dataclass(frozen=True, slots=True)
class ProposalAssessmentV1:
    proposal_id: str
    admissible: bool
    rejection_reasons: tuple[str, ...]
    novelty_score: float
    expected_gain: float
    uncertainty: float
    estimated_cost: float
    estimated_risk: float
    evidence_strength: float
    utility_score: float
    provenance_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        safe_id(self.proposal_id, "proposal_id")
        if not isinstance(self.admissible, bool):
            raise ValueError("admissible must be bool")

        reasons = unique_text(
            self.rejection_reasons,
            "rejection_reasons",
            maximum=128,
            item_max_length=4_000,
        )
        if self.admissible and reasons:
            raise ValueError("admissible assessment cannot contain rejection_reasons")
        if not self.admissible and not reasons:
            raise ValueError("inadmissible assessment requires rejection_reasons")
        object.__setattr__(self, "rejection_reasons", reasons)

        object.__setattr__(
            self,
            "novelty_score",
            _unit_interval(self.novelty_score, "novelty_score"),
        )
        object.__setattr__(
            self,
            "expected_gain",
            finite_number(self.expected_gain, "expected_gain"),
        )
        object.__setattr__(
            self,
            "uncertainty",
            non_negative_number(self.uncertainty, "uncertainty"),
        )
        object.__setattr__(
            self,
            "estimated_cost",
            non_negative_number(self.estimated_cost, "estimated_cost"),
        )
        object.__setattr__(
            self,
            "estimated_risk",
            _unit_interval(self.estimated_risk, "estimated_risk"),
        )
        object.__setattr__(
            self,
            "evidence_strength",
            _unit_interval(self.evidence_strength, "evidence_strength"),
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
            "admissible": self.admissible,
            "rejection_reasons": list(self.rejection_reasons),
            "novelty_score": self.novelty_score,
            "expected_gain": self.expected_gain,
            "uncertainty": self.uncertainty,
            "estimated_cost": self.estimated_cost,
            "estimated_risk": self.estimated_risk,
            "evidence_strength": self.evidence_strength,
            "utility_score": self.utility_score,
            "provenance_ids": list(self.provenance_ids),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._identity_dict(), "fingerprint": self.fingerprint}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ProposalAssessmentV1":
        if raw.get("schema_version") != 1:
            raise ValueError("ProposalAssessmentV1 schema_version must be 1")
        result = cls(
            proposal_id=str(raw["proposal_id"]),
            admissible=raw["admissible"],
            rejection_reasons=tuple(raw.get("rejection_reasons", ())),
            novelty_score=float(raw["novelty_score"]),
            expected_gain=float(raw["expected_gain"]),
            uncertainty=float(raw["uncertainty"]),
            estimated_cost=float(raw["estimated_cost"]),
            estimated_risk=float(raw["estimated_risk"]),
            evidence_strength=float(raw["evidence_strength"]),
            utility_score=float(raw["utility_score"]),
            provenance_ids=tuple(raw.get("provenance_ids", ())),
        )
        expected = raw.get("fingerprint")
        if expected is not None and expected != result.fingerprint:
            raise ValueError("ProposalAssessmentV1 fingerprint mismatch")
        return result


@dataclass(frozen=True, slots=True)
class ResearchStrategyDecisionV1:
    decision_id: str
    campaign_id: str
    ranked_proposal_ids: tuple[str, ...]
    selected_proposal_id: str | None
    rejected_proposal_ids: tuple[str, ...]
    budget_snapshot: ResearchStrategyBudgetV1
    rationale: str
    provenance_ids: tuple[str, ...]
    authority: ResearchStrategyAuthorityV1 = ResearchStrategyAuthorityV1.PROPOSAL_ONLY

    def __post_init__(self) -> None:
        safe_id(self.decision_id, "decision_id")
        safe_id(self.campaign_id, "campaign_id")

        ranked = unique_ids(
            self.ranked_proposal_ids,
            "ranked_proposal_ids",
            maximum=512,
        )
        if not ranked:
            raise ValueError("ranked_proposal_ids must not be empty")
        object.__setattr__(self, "ranked_proposal_ids", ranked)

        selected = self.selected_proposal_id
        if selected is not None:
            selected = safe_id(selected, "selected_proposal_id")
            if selected not in ranked:
                raise ValueError("selected_proposal_id must be present in ranked_proposal_ids")
        object.__setattr__(self, "selected_proposal_id", selected)

        rejected = unique_ids(
            self.rejected_proposal_ids,
            "rejected_proposal_ids",
            maximum=512,
        )
        if not set(rejected) <= set(ranked):
            raise ValueError("rejected_proposal_ids must be a subset of ranked_proposal_ids")
        if selected is not None and selected in rejected:
            raise ValueError("selected_proposal_id cannot also be rejected")
        object.__setattr__(self, "rejected_proposal_ids", rejected)

        if not isinstance(self.budget_snapshot, ResearchStrategyBudgetV1):
            raise ValueError("budget_snapshot must be ResearchStrategyBudgetV1")

        object.__setattr__(
            self,
            "rationale",
            text(self.rationale, "rationale", max_length=64_000),
        )
        object.__setattr__(
            self,
            "provenance_ids",
            unique_text(
                self.provenance_ids,
                "provenance_ids",
                maximum=1024,
                item_max_length=8_000,
            ),
        )

        if self.authority is not ResearchStrategyAuthorityV1.PROPOSAL_ONLY:
            raise ValueError("R8 strategy decision authority must be PROPOSAL_ONLY")

    @property
    def decision_fingerprint(self) -> str:
        return stable_sha256(
            {
                "schema_version": 1,
                "campaign_id": self.campaign_id,
                "ranked_proposal_ids": list(self.ranked_proposal_ids),
                "selected_proposal_id": self.selected_proposal_id,
                "rejected_proposal_ids": list(self.rejected_proposal_ids),
                "budget_snapshot": self.budget_snapshot.to_dict(),
                "rationale": self.rationale,
                "provenance_ids": list(self.provenance_ids),
                "authority": self.authority.value,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "decision_id": self.decision_id,
            "campaign_id": self.campaign_id,
            "ranked_proposal_ids": list(self.ranked_proposal_ids),
            "selected_proposal_id": self.selected_proposal_id,
            "rejected_proposal_ids": list(self.rejected_proposal_ids),
            "budget_snapshot": self.budget_snapshot.to_dict(),
            "rationale": self.rationale,
            "provenance_ids": list(self.provenance_ids),
            "authority": self.authority.value,
            "decision_fingerprint": self.decision_fingerprint,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ResearchStrategyDecisionV1":
        if raw.get("schema_version") != 1:
            raise ValueError("ResearchStrategyDecisionV1 schema_version must be 1")
        result = cls(
            decision_id=str(raw["decision_id"]),
            campaign_id=str(raw["campaign_id"]),
            ranked_proposal_ids=tuple(raw["ranked_proposal_ids"]),
            selected_proposal_id=(
                None
                if raw.get("selected_proposal_id") is None
                else str(raw["selected_proposal_id"])
            ),
            rejected_proposal_ids=tuple(raw.get("rejected_proposal_ids", ())),
            budget_snapshot=ResearchStrategyBudgetV1.from_dict(raw["budget_snapshot"]),
            rationale=str(raw["rationale"]),
            provenance_ids=tuple(raw.get("provenance_ids", ())),
            authority=ResearchStrategyAuthorityV1(
                str(raw.get("authority", ResearchStrategyAuthorityV1.PROPOSAL_ONLY.value))
            ),
        )
        expected = raw.get("decision_fingerprint")
        if expected is not None and expected != result.decision_fingerprint:
            raise ValueError("ResearchStrategyDecisionV1 decision_fingerprint mismatch")
        return result

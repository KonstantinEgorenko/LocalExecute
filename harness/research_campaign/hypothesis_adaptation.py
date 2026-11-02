'research campaign.R7.6-A-E deterministic hypothesis adaptation contracts.\n\nThis layer does not execute experiments and does not add a second planner.  It turns\nCURRENT consolidated research beliefs into bounded, auditable HypothesisProposalV1\nobjects.  Structural novelty is encoded into HypothesisV1.strategy_dimensions so a\nrestart can deduplicate proposals from the durable hypothesis ledger without a second\npersistence surface.\n'
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Callable, Mapping, Protocol

from .beliefs import (
    ResearchBeliefApplicabilityV1,
    ResearchBeliefScopeV1,
    ResearchBeliefSetV1,
    ResearchBeliefStatusV1,
    ResearchBeliefV1,
)
from .campaign import ResearchBudgetUsageV1
from .contracts import ResearchContractV1
from .models import HypothesisModeV1, HypothesisStatusV1, HypothesisV1
from .serialization import (
    freeze_json,
    non_negative_int,
    optional_safe_id,
    plain,
    positive_int,
    safe_id,
    sha256_hex,
    stable_sha256,
    timestamp,
    unique_ids,
)


_R76_CREATED_BY = "phase22-r7.6-deterministic-adaptation"
_R76_MARKER = "phase22_r76_adaptation"


class HypothesisAdaptationError(RuntimeError):
    pass


class HypothesisAdaptationKindV1(StrEnum):
    EXTEND_SUPPORTED = "EXTEND_SUPPORTED"
    CHALLENGE_CONTRADICTED = "CHALLENGE_CONTRADICTED"
    RESOLVE_MIXED = "RESOLVE_MIXED"


@dataclass(frozen=True, slots=True)
class HypothesisAdaptationPolicyV1:
    max_generated_hypotheses: int = 32
    max_source_beliefs: int = 4
    max_target_dimensions: int = 16
    minimum_belief_confidence: float = 0.20

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "max_generated_hypotheses",
            positive_int(self.max_generated_hypotheses, "max_generated_hypotheses"),
        )
        object.__setattr__(self, "max_source_beliefs", positive_int(self.max_source_beliefs, "max_source_beliefs"))
        object.__setattr__(
            self,
            "max_target_dimensions",
            positive_int(self.max_target_dimensions, "max_target_dimensions"),
        )
        if self.max_generated_hypotheses > 10_000:
            raise ValueError("max_generated_hypotheses must be <= 10000")
        if self.max_source_beliefs > 64:
            raise ValueError("max_source_beliefs must be <= 64")
        if self.max_target_dimensions > 256:
            raise ValueError("max_target_dimensions must be <= 256")
        if isinstance(self.minimum_belief_confidence, bool) or not isinstance(
            self.minimum_belief_confidence, (int, float)
        ):
            raise ValueError("minimum_belief_confidence must be numeric")
        confidence = float(self.minimum_belief_confidence)
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("minimum_belief_confidence must be in [0,1]")
        object.__setattr__(self, "minimum_belief_confidence", confidence)


def _normalize_text(value: str) -> str:
    return " ".join(value.split()).casefold()


def hypothesis_semantic_fingerprint(hypothesis: HypothesisV1) -> str:
    """Normalized hypothesis identity independent of ID, timestamps and evidence refs."""
    if not isinstance(hypothesis, HypothesisV1):
        raise TypeError("hypothesis must be HypothesisV1")
    return stable_sha256(
        {
            "schema_version": 1,
            "statement": _normalize_text(hypothesis.statement),
            "mechanism": _normalize_text(hypothesis.mechanism),
            "predicted_effect": _normalize_text(hypothesis.predicted_effect),
            "falsifier": None if hypothesis.falsifier is None else _normalize_text(hypothesis.falsifier),
            "mode": hypothesis.mode.value,
            "parent_hypothesis_ids": sorted(hypothesis.parent_hypothesis_ids),
            "strategy_dimensions": plain(hypothesis.strategy_dimensions),
        }
    )


def hypothesis_adaptation_novelty_fingerprint(
    *,
    campaign_id: str,
    kind: HypothesisAdaptationKindV1,
    source_strategy_id: str,
    target_dimension: str,
    evaluation_contract_hash: str,
    scope_fingerprint: str,
    parent_hypothesis_ids: tuple[str, ...] = (),
) -> str:
    safe_id(campaign_id, "campaign_id")
    if not isinstance(kind, HypothesisAdaptationKindV1):
        raise ValueError("kind must be HypothesisAdaptationKindV1")
    safe_id(source_strategy_id, "source_strategy_id")
    safe_id(target_dimension, "target_dimension")
    sha256_hex(evaluation_contract_hash, "evaluation_contract_hash")
    sha256_hex(scope_fingerprint, "scope_fingerprint")
    parents = unique_ids(parent_hypothesis_ids, "parent_hypothesis_ids", maximum=64)
    return stable_sha256(
        {
            "schema_version": 1,
            "campaign_id": campaign_id,
            "kind": kind.value,
            "source_strategy_id": source_strategy_id,
            "target_dimension": target_dimension,
            "evaluation_contract_hash": evaluation_contract_hash,
            "scope_fingerprint": scope_fingerprint,
            "parent_hypothesis_ids": sorted(parents),
        }
    )


def hypothesis_adaptation_fingerprint_from_hypothesis(hypothesis: HypothesisV1) -> str | None:
    """Recover the structural R7.6 novelty key from a durable generated hypothesis."""
    if not isinstance(hypothesis, HypothesisV1):
        raise TypeError("hypothesis must be HypothesisV1")
    raw = hypothesis.strategy_dimensions
    if raw.get("adapter") != _R76_MARKER:
        return None
    try:
        return hypothesis_adaptation_novelty_fingerprint(
            campaign_id=hypothesis.campaign_id,
            kind=HypothesisAdaptationKindV1(str(raw["adaptation_kind"])),
            source_strategy_id=str(raw["source_strategy_id"]),
            target_dimension=str(raw["target_dimension"]),
            evaluation_contract_hash=str(raw["evaluation_contract_hash"]),
            scope_fingerprint=str(raw["scope_fingerprint"]),
            parent_hypothesis_ids=hypothesis.parent_hypothesis_ids,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise HypothesisAdaptationError(f"invalid durable R7.6 hypothesis metadata: {exc}") from exc


@dataclass(frozen=True, slots=True)
class HypothesisProposalV1:
    proposal_id: str
    campaign_id: str
    kind: HypothesisAdaptationKindV1
    hypothesis: HypothesisV1
    source_belief_ids: tuple[str, ...]
    source_belief_set_fingerprint: str
    source_strategy_id: str
    target_strategy_dimensions: tuple[str, ...]
    evaluation_contract_hash: str
    scope_fingerprint: str
    created_at: str

    def __post_init__(self) -> None:
        safe_id(self.proposal_id, "proposal_id")
        safe_id(self.campaign_id, "campaign_id")
        if not isinstance(self.kind, HypothesisAdaptationKindV1):
            raise ValueError("kind must be HypothesisAdaptationKindV1")
        if not isinstance(self.hypothesis, HypothesisV1):
            raise ValueError("hypothesis must be HypothesisV1")
        if self.hypothesis.campaign_id != self.campaign_id:
            raise ValueError("proposal hypothesis campaign_id mismatch")
        if self.hypothesis.mode is not HypothesisModeV1.RESEARCH_QUALITY:
            raise ValueError("adaptive hypothesis must use RESEARCH_QUALITY mode")
        if self.hypothesis.status is not HypothesisStatusV1.PROPOSED:
            raise ValueError("adaptive hypothesis must start PROPOSED")
        beliefs = unique_ids(self.source_belief_ids, "source_belief_ids", maximum=64)
        if not beliefs:
            raise ValueError("source_belief_ids must not be empty")
        object.__setattr__(self, "source_belief_ids", beliefs)
        object.__setattr__(
            self,
            "source_belief_set_fingerprint",
            sha256_hex(self.source_belief_set_fingerprint, "source_belief_set_fingerprint"),
        )
        safe_id(self.source_strategy_id, "source_strategy_id")
        dimensions = unique_ids(
            self.target_strategy_dimensions,
            "target_strategy_dimensions",
            maximum=256,
        )
        if not dimensions:
            raise ValueError("target_strategy_dimensions must not be empty")
        object.__setattr__(self, "target_strategy_dimensions", dimensions)
        object.__setattr__(
            self,
            "evaluation_contract_hash",
            sha256_hex(self.evaluation_contract_hash, "evaluation_contract_hash"),
        )
        object.__setattr__(self, "scope_fingerprint", sha256_hex(self.scope_fingerprint, "scope_fingerprint"))
        object.__setattr__(self, "created_at", timestamp(self.created_at, "created_at"))
        if self.hypothesis.created_at != self.created_at:
            raise ValueError("proposal created_at must equal hypothesis created_at")
        if self.hypothesis.evidence_refs != tuple(f"belief:{item}" for item in self.source_belief_ids):
            raise ValueError("hypothesis evidence_refs must exactly reference source beliefs")
        raw = self.hypothesis.strategy_dimensions
        expected = {
            "adapter": _R76_MARKER,
            "adaptation_kind": self.kind.value,
            "source_strategy_id": self.source_strategy_id,
            "target_dimension": self.target_strategy_dimensions[0],
            "evaluation_contract_hash": self.evaluation_contract_hash,
            "scope_fingerprint": self.scope_fingerprint,
        }
        for key, value in expected.items():
            if raw.get(key) != value:
                raise ValueError(f"hypothesis strategy_dimensions.{key} mismatch")

    @property
    def novelty_fingerprint(self) -> str:
        return hypothesis_adaptation_novelty_fingerprint(
            campaign_id=self.campaign_id,
            kind=self.kind,
            source_strategy_id=self.source_strategy_id,
            target_dimension=self.target_strategy_dimensions[0],
            evaluation_contract_hash=self.evaluation_contract_hash,
            scope_fingerprint=self.scope_fingerprint,
            parent_hypothesis_ids=self.hypothesis.parent_hypothesis_ids,
        )

    @property
    def semantic_hypothesis_fingerprint(self) -> str:
        return hypothesis_semantic_fingerprint(self.hypothesis)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "proposal_id": self.proposal_id,
            "campaign_id": self.campaign_id,
            "kind": self.kind.value,
            "hypothesis": self.hypothesis.to_dict(),
            "source_belief_ids": list(self.source_belief_ids),
            "source_belief_set_fingerprint": self.source_belief_set_fingerprint,
            "source_strategy_id": self.source_strategy_id,
            "target_strategy_dimensions": list(self.target_strategy_dimensions),
            "evaluation_contract_hash": self.evaluation_contract_hash,
            "scope_fingerprint": self.scope_fingerprint,
            "created_at": self.created_at,
            "novelty_fingerprint": self.novelty_fingerprint,
            "semantic_hypothesis_fingerprint": self.semantic_hypothesis_fingerprint,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "HypothesisProposalV1":
        if raw.get("schema_version") != 1:
            raise ValueError("HypothesisProposalV1 schema_version must be 1")
        result = cls(
            proposal_id=str(raw["proposal_id"]),
            campaign_id=str(raw["campaign_id"]),
            kind=HypothesisAdaptationKindV1(str(raw["kind"])),
            hypothesis=HypothesisV1.from_dict(raw["hypothesis"]),
            source_belief_ids=tuple(raw.get("source_belief_ids", ())),
            source_belief_set_fingerprint=str(raw["source_belief_set_fingerprint"]),
            source_strategy_id=str(raw["source_strategy_id"]),
            target_strategy_dimensions=tuple(raw.get("target_strategy_dimensions", ())),
            evaluation_contract_hash=str(raw["evaluation_contract_hash"]),
            scope_fingerprint=str(raw["scope_fingerprint"]),
            created_at=str(raw["created_at"]),
        )
        if raw.get("novelty_fingerprint") is not None and raw["novelty_fingerprint"] != result.novelty_fingerprint:
            raise ValueError("HypothesisProposalV1 novelty_fingerprint mismatch")
        if (
            raw.get("semantic_hypothesis_fingerprint") is not None
            and raw["semantic_hypothesis_fingerprint"] != result.semantic_hypothesis_fingerprint
        ):
            raise ValueError("HypothesisProposalV1 semantic_hypothesis_fingerprint mismatch")
        return result


@dataclass(frozen=True, slots=True)
class HypothesisAdaptationContextV1:
    contract: ResearchContractV1
    usage: ResearchBudgetUsageV1
    belief_set: ResearchBeliefSetV1
    target_scope: ResearchBeliefScopeV1
    allowed_strategy_dimensions: tuple[str, ...]
    known_hypotheses: tuple[HypothesisV1, ...] = ()
    generated_hypothesis_count: int = 0
    parent_hypothesis_ids_by_strategy: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.contract, ResearchContractV1):
            raise ValueError("contract must be ResearchContractV1")
        if not isinstance(self.usage, ResearchBudgetUsageV1):
            raise ValueError("usage must be ResearchBudgetUsageV1")
        if not isinstance(self.belief_set, ResearchBeliefSetV1):
            raise ValueError("belief_set must be ResearchBeliefSetV1")
        if not isinstance(self.target_scope, ResearchBeliefScopeV1):
            raise ValueError("target_scope must be ResearchBeliefScopeV1")
        if self.belief_set.campaign_id != self.contract.campaign_id:
            raise ValueError("belief_set campaign_id must match research contract")
        if (
            self.belief_set.target_scope_fingerprint is not None
            and self.belief_set.target_scope_fingerprint != self.target_scope.fingerprint
        ):
            raise ValueError("belief_set target scope does not match adaptation target_scope")
        dimensions = unique_ids(
            self.allowed_strategy_dimensions,
            "allowed_strategy_dimensions",
            maximum=256,
        )
        if not dimensions:
            raise ValueError("allowed_strategy_dimensions must not be empty")
        object.__setattr__(self, "allowed_strategy_dimensions", dimensions)
        if not isinstance(self.known_hypotheses, tuple) or any(
            not isinstance(item, HypothesisV1) for item in self.known_hypotheses
        ):
            raise ValueError("known_hypotheses must be tuple of HypothesisV1")
        if any(item.campaign_id != self.contract.campaign_id for item in self.known_hypotheses):
            raise ValueError("known_hypotheses must be from the same campaign")
        object.__setattr__(
            self,
            "generated_hypothesis_count",
            non_negative_int(self.generated_hypothesis_count, "generated_hypothesis_count"),
        )
        normalized_parents: dict[str, tuple[str, ...]] = {}
        for strategy_id, values in self.parent_hypothesis_ids_by_strategy.items():
            key = safe_id(strategy_id, "parent_hypothesis_ids_by_strategy key")
            normalized_parents[key] = unique_ids(values, f"parent_hypothesis_ids_by_strategy.{key}", maximum=64)
        frozen = freeze_json(normalized_parents, "parent_hypothesis_ids_by_strategy")
        if not isinstance(frozen, Mapping):
            raise ValueError("parent_hypothesis_ids_by_strategy must be mapping")
        object.__setattr__(self, "parent_hypothesis_ids_by_strategy", frozen)

    @property
    def remaining_experiment_budget(self) -> int:
        return max(0, self.contract.budget.max_experiments - self.usage.experiment_count)

    @property
    def known_semantic_fingerprints(self) -> frozenset[str]:
        return frozenset(hypothesis_semantic_fingerprint(item) for item in self.known_hypotheses)

    @property
    def known_adaptation_fingerprints(self) -> frozenset[str]:
        result: set[str] = set()
        for item in self.known_hypotheses:
            fingerprint = hypothesis_adaptation_fingerprint_from_hypothesis(item)
            if fingerprint is not None:
                result.add(fingerprint)
        return frozenset(result)


class HypothesisGenerationPort(Protocol):
    def propose(self, context: HypothesisAdaptationContextV1) -> HypothesisProposalV1 | None: ...


@dataclass(frozen=True, slots=True)
class HypothesisProposalGuardV1:
    policy: HypothesisAdaptationPolicyV1 = HypothesisAdaptationPolicyV1()

    def __post_init__(self) -> None:
        if not isinstance(self.policy, HypothesisAdaptationPolicyV1):
            raise ValueError("policy must be HypothesisAdaptationPolicyV1")

    def validate(self, context: HypothesisAdaptationContextV1, proposal: HypothesisProposalV1) -> None:
        if not isinstance(context, HypothesisAdaptationContextV1):
            raise TypeError("context must be HypothesisAdaptationContextV1")
        if not isinstance(proposal, HypothesisProposalV1):
            raise TypeError("proposal must be HypothesisProposalV1")
        if proposal.campaign_id != context.contract.campaign_id:
            raise HypothesisAdaptationError("proposal campaign_id mismatch")
        if context.remaining_experiment_budget <= 0:
            raise HypothesisAdaptationError("experiment budget exhausted; cannot generate hypothesis")
        if context.generated_hypothesis_count >= self.policy.max_generated_hypotheses:
            raise HypothesisAdaptationError("generated hypothesis budget exhausted")
        if len(proposal.source_belief_ids) > self.policy.max_source_beliefs:
            raise HypothesisAdaptationError("proposal exceeds max_source_beliefs")
        if len(proposal.target_strategy_dimensions) > self.policy.max_target_dimensions:
            raise HypothesisAdaptationError("proposal exceeds max_target_dimensions")
        if proposal.evaluation_contract_hash != context.target_scope.evaluation_contract_hash:
            raise HypothesisAdaptationError("proposal EvaluationContractHash does not match target scope")
        if proposal.scope_fingerprint != context.target_scope.fingerprint:
            raise HypothesisAdaptationError("proposal scope does not match target scope")
        if any(item not in context.allowed_strategy_dimensions for item in proposal.target_strategy_dimensions):
            raise HypothesisAdaptationError("proposal targets strategy dimension outside allowed set")
        by_id = {item.belief_id: item for item in context.belief_set.beliefs}
        for belief_id in proposal.source_belief_ids:
            belief = by_id.get(belief_id)
            if belief is None:
                raise HypothesisAdaptationError("proposal references unknown source belief")
            self._validate_source_belief(context, proposal, belief)
        if proposal.novelty_fingerprint in context.known_adaptation_fingerprints:
            raise HypothesisAdaptationError("structurally duplicate adaptive hypothesis")
        if proposal.semantic_hypothesis_fingerprint in context.known_semantic_fingerprints:
            raise HypothesisAdaptationError("semantically duplicate normalized hypothesis")

    def _validate_source_belief(
        self,
        context: HypothesisAdaptationContextV1,
        proposal: HypothesisProposalV1,
        belief: ResearchBeliefV1,
    ) -> None:
        if belief.applicability is not ResearchBeliefApplicabilityV1.CURRENT:
            raise HypothesisAdaptationError("only CURRENT beliefs may drive hypothesis adaptation")
        if belief.scope.fingerprint != context.target_scope.fingerprint:
            raise HypothesisAdaptationError("source belief scope does not match adaptation target")
        if belief.strategy_id != proposal.source_strategy_id:
            raise HypothesisAdaptationError("source belief strategy does not match proposal strategy")
        if belief.status not in {
            ResearchBeliefStatusV1.SUPPORTED,
            ResearchBeliefStatusV1.CONTRADICTED,
            ResearchBeliefStatusV1.MIXED,
        }:
            raise HypothesisAdaptationError("belief status is not actionable for hypothesis adaptation")
        if belief.confidence < self.policy.minimum_belief_confidence:
            raise HypothesisAdaptationError("belief confidence is below adaptation threshold")


class DeterministicHypothesisGeneratorV1:
    """Model-free generator that proposes the first novel bounded belief adaptation."""

    def __init__(
        self,
        *,
        clock: Callable[[], str],
        policy: HypothesisAdaptationPolicyV1 = HypothesisAdaptationPolicyV1(),
    ) -> None:
        if not callable(clock):
            raise ValueError("clock must be callable")
        if not isinstance(policy, HypothesisAdaptationPolicyV1):
            raise ValueError("policy must be HypothesisAdaptationPolicyV1")
        self.clock = clock
        self.policy = policy
        self.guard = HypothesisProposalGuardV1(policy)

    def propose(self, context: HypothesisAdaptationContextV1) -> HypothesisProposalV1 | None:
        if not isinstance(context, HypothesisAdaptationContextV1):
            raise TypeError("context must be HypothesisAdaptationContextV1")
        if context.remaining_experiment_budget <= 0:
            return None
        if context.generated_hypothesis_count >= self.policy.max_generated_hypotheses:
            return None
        beliefs = self._eligible_beliefs(context)
        if not beliefs:
            return None
        dimensions = tuple(sorted(context.allowed_strategy_dimensions))[: self.policy.max_target_dimensions]
        for belief in beliefs:
            kind = self._kind_for_belief(belief)
            parents = tuple(context.parent_hypothesis_ids_by_strategy.get(belief.strategy_id, ()))
            for dimension in dimensions:
                proposal = self._materialize(context, belief, kind, dimension, parents)
                if proposal.novelty_fingerprint in context.known_adaptation_fingerprints:
                    continue
                if proposal.semantic_hypothesis_fingerprint in context.known_semantic_fingerprints:
                    continue
                self.guard.validate(context, proposal)
                return proposal
        return None

    def _eligible_beliefs(self, context: HypothesisAdaptationContextV1) -> tuple[ResearchBeliefV1, ...]:
        status_rank = {
            ResearchBeliefStatusV1.SUPPORTED: 0,
            ResearchBeliefStatusV1.MIXED: 1,
            ResearchBeliefStatusV1.CONTRADICTED: 2,
        }
        beliefs = [
            item
            for item in context.belief_set.beliefs
            if item.applicability is ResearchBeliefApplicabilityV1.CURRENT
            and item.scope.fingerprint == context.target_scope.fingerprint
            and item.status in status_rank
            and item.confidence >= self.policy.minimum_belief_confidence
        ]
        beliefs.sort(
            key=lambda item: (
                status_rank[item.status],
                -abs(item.net_strength),
                -item.confidence,
                item.strategy_id,
                item.belief_id,
            )
        )
        return tuple(beliefs[: self.policy.max_source_beliefs])

    @staticmethod
    def _kind_for_belief(belief: ResearchBeliefV1) -> HypothesisAdaptationKindV1:
        if belief.status is ResearchBeliefStatusV1.SUPPORTED:
            return HypothesisAdaptationKindV1.EXTEND_SUPPORTED
        if belief.status is ResearchBeliefStatusV1.CONTRADICTED:
            return HypothesisAdaptationKindV1.CHALLENGE_CONTRADICTED
        if belief.status is ResearchBeliefStatusV1.MIXED:
            return HypothesisAdaptationKindV1.RESOLVE_MIXED
        raise HypothesisAdaptationError(f"unsupported belief status {belief.status.value}")

    def _materialize(
        self,
        context: HypothesisAdaptationContextV1,
        belief: ResearchBeliefV1,
        kind: HypothesisAdaptationKindV1,
        dimension: str,
        parents: tuple[str, ...],
    ) -> HypothesisProposalV1:
        created_at = timestamp(self.clock(), "clock")
        novelty = hypothesis_adaptation_novelty_fingerprint(
            campaign_id=context.contract.campaign_id,
            kind=kind,
            source_strategy_id=belief.strategy_id,
            target_dimension=dimension,
            evaluation_contract_hash=context.target_scope.evaluation_contract_hash,
            scope_fingerprint=context.target_scope.fingerprint,
            parent_hypothesis_ids=parents,
        )
        text_parts = self._texts(kind, belief.strategy_id, dimension)
        hypothesis = HypothesisV1(
            hypothesis_id=f"r76h-{novelty[:24]}",
            campaign_id=context.contract.campaign_id,
            statement=text_parts[0],
            mechanism=text_parts[1],
            predicted_effect=text_parts[2],
            falsifier=text_parts[3],
            created_by=_R76_CREATED_BY,
            created_at=created_at,
            mode=HypothesisModeV1.RESEARCH_QUALITY,
            parent_hypothesis_ids=parents,
            strategy_dimensions={
                "adapter": _R76_MARKER,
                "adaptation_kind": kind.value,
                "source_strategy_id": belief.strategy_id,
                "target_dimension": dimension,
                "evaluation_contract_hash": context.target_scope.evaluation_contract_hash,
                "scope_fingerprint": context.target_scope.fingerprint,
            },
            evidence_refs=(f"belief:{belief.belief_id}",),
            status=HypothesisStatusV1.PROPOSED,
        )
        return HypothesisProposalV1(
            proposal_id=f"r76p-{novelty[:24]}",
            campaign_id=context.contract.campaign_id,
            kind=kind,
            hypothesis=hypothesis,
            source_belief_ids=(belief.belief_id,),
            source_belief_set_fingerprint=context.belief_set.fingerprint,
            source_strategy_id=belief.strategy_id,
            target_strategy_dimensions=(dimension,),
            evaluation_contract_hash=context.target_scope.evaluation_contract_hash,
            scope_fingerprint=context.target_scope.fingerprint,
            created_at=created_at,
        )

    @staticmethod
    def _texts(kind: HypothesisAdaptationKindV1, strategy_id: str, dimension: str) -> tuple[str, str, str, str]:
        if kind is HypothesisAdaptationKindV1.EXTEND_SUPPORTED:
            return (
                f"A novel {dimension} variant of supported strategy {strategy_id} can improve or preserve its validated effect.",
                f"Keep the supported mechanism of {strategy_id} fixed while changing one bounded {dimension} choice.",
                "The unseen variant should preserve validity and improve or match the primary metric under the same evaluator.",
                f"All admissible unseen {dimension} variants of {strategy_id} fail to preserve validity or improve the metric.",
            )
        if kind is HypothesisAdaptationKindV1.CHALLENGE_CONTRADICTED:
            return (
                f"Changing {dimension} may remove the candidate-owned weakness observed for strategy {strategy_id}.",
                f"Alter one bounded {dimension} choice while preserving the remaining strategy envelope of {strategy_id}.",
                "A novel variant should eliminate the contradiction without introducing an incompatible evaluation scope.",
                f"Every admissible unseen {dimension} variant remains contradicted under the same protected evaluator.",
            )
        return (
            f"Varying {dimension} can discriminate the mixed evidence currently associated with strategy {strategy_id}.",
            f"Hold the remaining strategy envelope fixed and vary one bounded {dimension} choice to resolve conflicting evidence.",
            "At least one unseen variant should produce a clearer supported or contradicted outcome under the same evaluator.",
            f"Admissible unseen {dimension} variants remain mixed or inconclusive for strategy {strategy_id}.",
        )

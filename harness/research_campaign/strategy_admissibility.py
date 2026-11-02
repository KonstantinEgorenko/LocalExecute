'research campaign.R8.2 deterministic hard proposal admissibility authority.\n\nThis layer performs binary authority checks before novelty/utility scoring.\nIt never ranks proposals and never grants coding authority.\n'
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping, Protocol

from .contracts import ResearchContractV1
from .serialization import (
    non_negative_number,
    plain,
    safe_id,
    sha256_hex,
    stable_sha256,
    unique_text,
)
from .strategy_contracts import (
    ResearchProposalTargetKindV1,
    ResearchProposalV1,
    ResearchRiskClassV1,
    ResearchStrategyAuthorityV1,
    ResearchStrategyBudgetV1,
)


class ProposalAdmissibilityReasonV1(StrEnum):
    PROTECTED_TARGET = "PROTECTED_TARGET"
    DUPLICATE_NOVELTY = "DUPLICATE_NOVELTY"
    EXPERIMENT_BUDGET_EXHAUSTED = "EXPERIMENT_BUDGET_EXHAUSTED"
    RISK_EXCEEDS_BUDGET = "RISK_EXCEEDS_BUDGET"
    MISSING_PROVENANCE = "MISSING_PROVENANCE"
    MISSING_EXECUTION_SCOPE_EVIDENCE = "MISSING_EXECUTION_SCOPE_EVIDENCE"
    MUTATION_SCOPE_NOT_ALLOWED = "MUTATION_SCOPE_NOT_ALLOWED"
    MUTATION_SCOPE_FORBIDDEN = "MUTATION_SCOPE_FORBIDDEN"
    TARGET_KIND_NOT_ALLOWED = "TARGET_KIND_NOT_ALLOWED"
    MODEL_NOT_ALLOWED = "MODEL_NOT_ALLOWED"
    AUTHORITY_VIOLATION = "AUTHORITY_VIOLATION"


@dataclass(frozen=True, slots=True)
class ProposalMutationScopeEvidenceV1:
    proposal_id: str
    requested_mutation_scope: tuple[str, ...]

    def __post_init__(self) -> None:
        safe_id(self.proposal_id, "proposal_id")
        scope = unique_text(
            self.requested_mutation_scope,
            "requested_mutation_scope",
            maximum=512,
            item_max_length=4_000,
        )
        normalized = tuple(_normalize_scope_path(item) for item in scope)
        if len(set(normalized)) != len(normalized):
            raise ValueError("requested_mutation_scope must not contain normalized duplicates")
        object.__setattr__(self, "requested_mutation_scope", normalized)

    @property
    def fingerprint(self) -> str:
        return stable_sha256(
            {
                "schema_version": 1,
                "proposal_id": self.proposal_id,
                "requested_mutation_scope": list(self.requested_mutation_scope),
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "proposal_id": self.proposal_id,
            "requested_mutation_scope": list(self.requested_mutation_scope),
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ProposalMutationScopeEvidenceV1":
        if raw.get("schema_version") != 1:
            raise ValueError("ProposalMutationScopeEvidenceV1 schema_version must be 1")
        result = cls(
            proposal_id=str(raw["proposal_id"]),
            requested_mutation_scope=tuple(raw.get("requested_mutation_scope", ())),
        )
        expected = raw.get("fingerprint")
        if expected is not None and expected != result.fingerprint:
            raise ValueError("ProposalMutationScopeEvidenceV1 fingerprint mismatch")
        return result


@dataclass(frozen=True, slots=True)
class ProposalAdmissibilityPolicyV1:
    allowed_target_kinds: tuple[ResearchProposalTargetKindV1, ...] = (
        ResearchProposalTargetKindV1.EXECUTION,
        ResearchProposalTargetKindV1.MODEL,
        ResearchProposalTargetKindV1.PROMPT,
        ResearchProposalTargetKindV1.TOOL_POLICY,
    )
    require_execution_scope_evidence: bool = True
    risk_limit_low: float = 0.25
    risk_limit_medium: float = 0.50
    risk_limit_high: float = 0.75
    risk_limit_critical: float = 1.00

    def __post_init__(self) -> None:
        if not isinstance(self.allowed_target_kinds, tuple) or not self.allowed_target_kinds:
            raise ValueError("allowed_target_kinds must be non-empty tuple")
        if any(not isinstance(item, ResearchProposalTargetKindV1) for item in self.allowed_target_kinds):
            raise ValueError("allowed_target_kinds must contain ResearchProposalTargetKindV1")
        if len(set(self.allowed_target_kinds)) != len(self.allowed_target_kinds):
            raise ValueError("allowed_target_kinds must not contain duplicates")
        if not isinstance(self.require_execution_scope_evidence, bool):
            raise ValueError("require_execution_scope_evidence must be bool")
        for name in (
            "risk_limit_low",
            "risk_limit_medium",
            "risk_limit_high",
            "risk_limit_critical",
        ):
            value = non_negative_number(getattr(self, name), name)
            if value > 1.0:
                raise ValueError(f"{name} must be <= 1")
            object.__setattr__(self, name, value)
        if not (
            self.risk_limit_low
            <= self.risk_limit_medium
            <= self.risk_limit_high
            <= self.risk_limit_critical
        ):
            raise ValueError("risk limits must be monotonically non-decreasing")

    def risk_limit(self, risk_class: ResearchRiskClassV1) -> float:
        if risk_class is ResearchRiskClassV1.LOW:
            return self.risk_limit_low
        if risk_class is ResearchRiskClassV1.MEDIUM:
            return self.risk_limit_medium
        if risk_class is ResearchRiskClassV1.HIGH:
            return self.risk_limit_high
        if risk_class is ResearchRiskClassV1.CRITICAL:
            return self.risk_limit_critical
        raise ValueError("unknown ResearchRiskClassV1")


@dataclass(frozen=True, slots=True)
class ProposalAdmissibilityContextV1:
    contract: ResearchContractV1
    budget_snapshot: ResearchStrategyBudgetV1
    known_novelty_fingerprints: tuple[str, ...] = ()
    rejected_novelty_fingerprints: tuple[str, ...] = ()
    scope_evidence: tuple[ProposalMutationScopeEvidenceV1, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.contract, ResearchContractV1):
            raise ValueError("contract must be ResearchContractV1")
        if not isinstance(self.budget_snapshot, ResearchStrategyBudgetV1):
            raise ValueError("budget_snapshot must be ResearchStrategyBudgetV1")
        object.__setattr__(
            self,
            "known_novelty_fingerprints",
            _unique_sha256(self.known_novelty_fingerprints, "known_novelty_fingerprints"),
        )
        object.__setattr__(
            self,
            "rejected_novelty_fingerprints",
            _unique_sha256(self.rejected_novelty_fingerprints, "rejected_novelty_fingerprints"),
        )
        if not isinstance(self.scope_evidence, tuple):
            raise ValueError("scope_evidence must be tuple")
        if any(not isinstance(item, ProposalMutationScopeEvidenceV1) for item in self.scope_evidence):
            raise ValueError("scope_evidence must contain ProposalMutationScopeEvidenceV1")
        ids = tuple(item.proposal_id for item in self.scope_evidence)
        if len(set(ids)) != len(ids):
            raise ValueError("scope_evidence must contain at most one item per proposal_id")

    @property
    def excluded_novelty_fingerprints(self) -> frozenset[str]:
        return frozenset(self.known_novelty_fingerprints) | frozenset(
            self.rejected_novelty_fingerprints
        )

    def mutation_scope_for(self, proposal_id: str) -> tuple[str, ...] | None:
        for item in self.scope_evidence:
            if item.proposal_id == proposal_id:
                return item.requested_mutation_scope
        return None

    @property
    def fingerprint(self) -> str:
        return stable_sha256(
            {
                "schema_version": 1,
                "contract_hash": self.contract.contract_hash,
                "budget_snapshot": self.budget_snapshot.to_dict(),
                "known_novelty_fingerprints": sorted(self.known_novelty_fingerprints),
                "rejected_novelty_fingerprints": sorted(self.rejected_novelty_fingerprints),
                "scope_evidence": [item.to_dict() for item in self.scope_evidence],
            }
        )


@dataclass(frozen=True, slots=True)
class ProposalAdmissibilityResultV1:
    proposal_id: str
    proposal_fingerprint: str
    admissible: bool
    reasons: tuple[ProposalAdmissibilityReasonV1, ...]
    context_fingerprint: str

    def __post_init__(self) -> None:
        safe_id(self.proposal_id, "proposal_id")
        object.__setattr__(
            self,
            "proposal_fingerprint",
            sha256_hex(self.proposal_fingerprint, "proposal_fingerprint"),
        )
        if not isinstance(self.admissible, bool):
            raise ValueError("admissible must be bool")
        if not isinstance(self.reasons, tuple):
            raise ValueError("reasons must be tuple")
        if any(not isinstance(item, ProposalAdmissibilityReasonV1) for item in self.reasons):
            raise ValueError("reasons must contain ProposalAdmissibilityReasonV1")
        if len(set(self.reasons)) != len(self.reasons):
            raise ValueError("reasons must not contain duplicates")
        if self.admissible and self.reasons:
            raise ValueError("admissible result cannot contain rejection reasons")
        if not self.admissible and not self.reasons:
            raise ValueError("inadmissible result requires reasons")
        object.__setattr__(
            self,
            "context_fingerprint",
            sha256_hex(self.context_fingerprint, "context_fingerprint"),
        )

    @property
    def fingerprint(self) -> str:
        return stable_sha256(
            {
                "schema_version": 1,
                "proposal_id": self.proposal_id,
                "proposal_fingerprint": self.proposal_fingerprint,
                "admissible": self.admissible,
                "reasons": [item.value for item in self.reasons],
                "context_fingerprint": self.context_fingerprint,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "proposal_id": self.proposal_id,
            "proposal_fingerprint": self.proposal_fingerprint,
            "admissible": self.admissible,
            "reasons": [item.value for item in self.reasons],
            "context_fingerprint": self.context_fingerprint,
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ProposalAdmissibilityResultV1":
        if raw.get("schema_version") != 1:
            raise ValueError("ProposalAdmissibilityResultV1 schema_version must be 1")
        result = cls(
            proposal_id=str(raw["proposal_id"]),
            proposal_fingerprint=str(raw["proposal_fingerprint"]),
            admissible=raw["admissible"],
            reasons=tuple(
                ProposalAdmissibilityReasonV1(str(item))
                for item in raw.get("reasons", ())
            ),
            context_fingerprint=str(raw["context_fingerprint"]),
        )
        expected = raw.get("fingerprint")
        if expected is not None and expected != result.fingerprint:
            raise ValueError("ProposalAdmissibilityResultV1 fingerprint mismatch")
        return result


class ProposalAdmissibilityGatePort(Protocol):
    def assess(
        self,
        proposal: ResearchProposalV1,
        context: ProposalAdmissibilityContextV1,
    ) -> ProposalAdmissibilityResultV1: ...


@dataclass(frozen=True, slots=True)
class DeterministicProposalAdmissibilityGateV1:
    policy: ProposalAdmissibilityPolicyV1 = ProposalAdmissibilityPolicyV1()

    def __post_init__(self) -> None:
        if not isinstance(self.policy, ProposalAdmissibilityPolicyV1):
            raise ValueError("policy must be ProposalAdmissibilityPolicyV1")

    def assess(
        self,
        proposal: ResearchProposalV1,
        context: ProposalAdmissibilityContextV1,
    ) -> ProposalAdmissibilityResultV1:
        if not isinstance(proposal, ResearchProposalV1):
            raise TypeError("proposal must be ResearchProposalV1")
        if not isinstance(context, ProposalAdmissibilityContextV1):
            raise TypeError("context must be ProposalAdmissibilityContextV1")

        reasons: list[ProposalAdmissibilityReasonV1] = []

        if proposal.campaign_id != context.contract.campaign_id:
            reasons.append(ProposalAdmissibilityReasonV1.AUTHORITY_VIOLATION)

        if proposal.authority is not ResearchStrategyAuthorityV1.PROPOSAL_ONLY:
            reasons.append(ProposalAdmissibilityReasonV1.AUTHORITY_VIOLATION)

        if proposal.target_kind in {
            ResearchProposalTargetKindV1.DATASET,
            ResearchProposalTargetKindV1.EVALUATOR,
        }:
            reasons.append(ProposalAdmissibilityReasonV1.PROTECTED_TARGET)
        elif proposal.target_kind not in self.policy.allowed_target_kinds:
            reasons.append(ProposalAdmissibilityReasonV1.TARGET_KIND_NOT_ALLOWED)

        if proposal.novelty_fingerprint in context.excluded_novelty_fingerprints:
            reasons.append(ProposalAdmissibilityReasonV1.DUPLICATE_NOVELTY)

        if context.budget_snapshot.remaining_experiments <= 0:
            reasons.append(ProposalAdmissibilityReasonV1.EXPERIMENT_BUDGET_EXHAUSTED)

        risk_limit = self.policy.risk_limit(context.budget_snapshot.max_risk_class)
        if proposal.estimated_risk > risk_limit:
            reasons.append(ProposalAdmissibilityReasonV1.RISK_EXCEEDS_BUDGET)

        if not proposal.provenance_ids:
            reasons.append(ProposalAdmissibilityReasonV1.MISSING_PROVENANCE)

        if proposal.target_kind is ResearchProposalTargetKindV1.MODEL:
            if context.contract.allowed_models:
                target = plain(proposal.target_value)
                if not isinstance(target, str) or target not in context.contract.allowed_models:
                    reasons.append(ProposalAdmissibilityReasonV1.MODEL_NOT_ALLOWED)

        if proposal.target_kind is ResearchProposalTargetKindV1.EXECUTION:
            requested = context.mutation_scope_for(proposal.proposal_id)
            if requested is None:
                if self.policy.require_execution_scope_evidence:
                    reasons.append(
                        ProposalAdmissibilityReasonV1.MISSING_EXECUTION_SCOPE_EVIDENCE
                    )
            else:
                scope_reasons = _scope_reasons(
                    requested=requested,
                    allowed=context.contract.allowed_mutation_scope,
                    forbidden=context.contract.forbidden_mutation_scope,
                    budget_allowed=context.budget_snapshot.allowed_mutation_scope,
                )
                reasons.extend(scope_reasons)

        ordered = tuple(dict.fromkeys(reasons))
        return ProposalAdmissibilityResultV1(
            proposal_id=proposal.proposal_id,
            proposal_fingerprint=proposal.fingerprint,
            admissible=not ordered,
            reasons=ordered,
            context_fingerprint=context.fingerprint,
        )


def _scope_reasons(
    *,
    requested: tuple[str, ...],
    allowed: tuple[str, ...],
    forbidden: tuple[str, ...],
    budget_allowed: tuple[str, ...],
) -> tuple[ProposalAdmissibilityReasonV1, ...]:
    if not requested:
        return (ProposalAdmissibilityReasonV1.MISSING_EXECUTION_SCOPE_EVIDENCE,)

    allowed_n = tuple(_normalize_scope_path(item) for item in allowed)
    forbidden_n = tuple(_normalize_scope_path(item) for item in forbidden)
    budget_n = tuple(_normalize_scope_path(item) for item in budget_allowed)

    reasons: list[ProposalAdmissibilityReasonV1] = []
    for path in requested:
        if any(_scope_contains(root, path) for root in forbidden_n):
            reasons.append(ProposalAdmissibilityReasonV1.MUTATION_SCOPE_FORBIDDEN)
            continue
        contract_ok = bool(allowed_n) and any(_scope_contains(root, path) for root in allowed_n)
        budget_ok = bool(budget_n) and any(_scope_contains(root, path) for root in budget_n)
        if not contract_ok or not budget_ok:
            reasons.append(ProposalAdmissibilityReasonV1.MUTATION_SCOPE_NOT_ALLOWED)

    return tuple(dict.fromkeys(reasons))


def _scope_contains(root: str, path: str) -> bool:
    return path == root or path.startswith(root + "/")


def _normalize_scope_path(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("mutation scope path must be string")
    raw = value.strip().replace("\\", "/")
    while raw.startswith("./"):
        raw = raw[2:]
    if not raw or raw.startswith("/") or ":" in raw.split("/")[0]:
        raise ValueError("mutation scope path must be repository-relative")
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise ValueError("mutation scope path must not traverse parents")
    normalized = "/".join(parts)
    if not normalized:
        raise ValueError("mutation scope path must not be empty")
    return normalized


def _unique_sha256(values: object, name: str) -> tuple[str, ...]:
    if not isinstance(values, (tuple, list)):
        raise ValueError(f"{name} must be list/tuple")
    result = tuple(sha256_hex(item, name) for item in values)
    if len(set(result)) != len(result):
        raise ValueError(f"{name} must not contain duplicates")
    if len(result) > 10_000:
        raise ValueError(f"{name} exceeds maximum=10000")
    return result

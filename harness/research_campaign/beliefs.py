'research campaign.R7.5 deterministic consolidation of immutable research insights.\n\nTyped insights remain the immutable audit ledger.  R7.5 derives a bounded belief\nprojection from that ledger without rewriting evidence.  The projection is model-free,\nrestart-deterministic, lineage-aware, and scope-aware.  Protected GATE evidence can\nsupersede weaker DEV/repair observations, while successful descendant repair GATE\nconfirmation can retire the candidate-owned rejection that triggered the repair.\n'
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping, Sequence, TYPE_CHECKING

from .insights import (
    ResearchInsightDispositionV1,
    ResearchInsightKindV1,
    ResearchInsightPolarityV1,
    ResearchInsightRecordV1,
    ResearchInsightSurfaceV1,
)
from .serialization import (
    finite_number,
    non_negative_int,
    non_negative_number,
    optional_safe_id,
    optional_sha256_hex,
    optional_text,
    positive_int,
    safe_id,
    sha256_hex,
    stable_sha256,
    timestamp,
    timestamp_value,
)

if TYPE_CHECKING:  # pragma: no cover
    from .store import StoredExperimentV1


class ResearchBeliefStatusV1(StrEnum):
    SUPPORTED = "SUPPORTED"
    CONTRADICTED = "CONTRADICTED"
    MIXED = "MIXED"
    SUPERSEDED = "SUPERSEDED"
    DIAGNOSTIC_ONLY = "DIAGNOSTIC_ONLY"


class ResearchBeliefEvidenceStateV1(StrEnum):
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    DIAGNOSTIC_ONLY = "DIAGNOSTIC_ONLY"


class ResearchBeliefApplicabilityV1(StrEnum):
    UNSCOPED = "UNSCOPED"
    CURRENT = "CURRENT"
    STALE = "STALE"
    INCOMPATIBLE = "INCOMPATIBLE"


@dataclass(frozen=True, slots=True)
class ResearchBeliefScopeV1:
    """Hard execution/evaluation scope for comparing strategy evidence.

    Git commit is intentionally *not* part of this fingerprint.  Repairs and later
    descendants may legitimately move the code base while preserving the researched
    strategy/evaluator/tool envelope.  Code-base drift is tracked separately and can
    be enforced by an opt-in strict target check when lineage is known.
    """

    evaluation_contract_hash: str
    dataset_fingerprint: str
    model_id: str | None = None
    provider: str | None = None
    model_version: str | None = None
    prompt_hash: str | None = None
    tool_policy_hash: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "evaluation_contract_hash",
            sha256_hex(self.evaluation_contract_hash, "evaluation_contract_hash"),
        )
        object.__setattr__(
            self,
            "dataset_fingerprint",
            sha256_hex(self.dataset_fingerprint, "dataset_fingerprint"),
        )
        for name in ("model_id", "provider", "model_version"):
            object.__setattr__(self, name, optional_text(getattr(self, name), name, max_length=4_000))
        object.__setattr__(self, "prompt_hash", optional_sha256_hex(self.prompt_hash, "prompt_hash"))
        object.__setattr__(
            self,
            "tool_policy_hash",
            optional_sha256_hex(self.tool_policy_hash, "tool_policy_hash"),
        )

    @property
    def fingerprint(self) -> str:
        return stable_sha256(self.to_dict(include_fingerprint=False))

    def to_dict(self, *, include_fingerprint: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": 1,
            "evaluation_contract_hash": self.evaluation_contract_hash,
            "dataset_fingerprint": self.dataset_fingerprint,
            "model_id": self.model_id,
            "provider": self.provider,
            "model_version": self.model_version,
            "prompt_hash": self.prompt_hash,
            "tool_policy_hash": self.tool_policy_hash,
        }
        if include_fingerprint:
            payload["fingerprint"] = self.fingerprint
        return payload

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ResearchBeliefScopeV1":
        if raw.get("schema_version") != 1:
            raise ValueError("ResearchBeliefScopeV1 schema_version must be 1")
        result = cls(
            evaluation_contract_hash=str(raw["evaluation_contract_hash"]),
            dataset_fingerprint=str(raw["dataset_fingerprint"]),
            model_id=None if raw.get("model_id") is None else str(raw["model_id"]),
            provider=None if raw.get("provider") is None else str(raw["provider"]),
            model_version=None if raw.get("model_version") is None else str(raw["model_version"]),
            prompt_hash=None if raw.get("prompt_hash") is None else str(raw["prompt_hash"]),
            tool_policy_hash=None if raw.get("tool_policy_hash") is None else str(raw["tool_policy_hash"]),
        )
        expected = raw.get("fingerprint")
        if expected is not None and expected != result.fingerprint:
            raise ValueError("ResearchBeliefScopeV1 fingerprint mismatch")
        return result


@dataclass(frozen=True, slots=True)
class ResearchBeliefTargetV1:
    scope: ResearchBeliefScopeV1
    experiment_id: str | None = None
    code_base_commit: str | None = None
    strict_code_base: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.scope, ResearchBeliefScopeV1):
            raise ValueError("scope must be ResearchBeliefScopeV1")
        object.__setattr__(self, "experiment_id", optional_safe_id(self.experiment_id, "experiment_id"))
        if self.code_base_commit is not None:
            commit = str(self.code_base_commit).strip().lower()
            if len(commit) < 7 or any(ch not in "0123456789abcdef" for ch in commit):
                raise ValueError("code_base_commit must be a hexadecimal Git commit")
            object.__setattr__(self, "code_base_commit", commit)
        if not isinstance(self.strict_code_base, bool):
            raise ValueError("strict_code_base must be bool")


@dataclass(frozen=True, slots=True)
class ResearchBeliefEvidenceV1:
    insight_id: str
    experiment_id: str
    surface: ResearchInsightSurfaceV1
    polarity: ResearchInsightPolarityV1
    confidence: float
    state: ResearchBeliefEvidenceStateV1
    weight: float
    created_at: str
    superseded_by_insight_ids: tuple[str, ...] = ()
    reason: str | None = None

    def __post_init__(self) -> None:
        safe_id(self.insight_id, "insight_id")
        safe_id(self.experiment_id, "experiment_id")
        if not isinstance(self.surface, ResearchInsightSurfaceV1):
            raise ValueError("surface must be ResearchInsightSurfaceV1")
        if not isinstance(self.polarity, ResearchInsightPolarityV1):
            raise ValueError("polarity must be ResearchInsightPolarityV1")
        if not isinstance(self.state, ResearchBeliefEvidenceStateV1):
            raise ValueError("state must be ResearchBeliefEvidenceStateV1")
        confidence = finite_number(self.confidence, "confidence")
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("confidence must be in [0,1]")
        object.__setattr__(self, "confidence", confidence)
        object.__setattr__(self, "weight", non_negative_number(self.weight, "weight"))
        object.__setattr__(self, "created_at", timestamp(self.created_at, "created_at"))
        normalized = tuple(safe_id(item, "superseded_by_insight_ids item") for item in self.superseded_by_insight_ids)
        if len(normalized) != len(set(normalized)):
            raise ValueError("superseded_by_insight_ids must be unique")
        object.__setattr__(self, "superseded_by_insight_ids", normalized)
        object.__setattr__(self, "reason", optional_text(self.reason, "reason", max_length=8_000))
        if self.state is ResearchBeliefEvidenceStateV1.SUPERSEDED and not normalized:
            raise ValueError("SUPERSEDED evidence requires superseded_by_insight_ids")
        if self.state is not ResearchBeliefEvidenceStateV1.SUPERSEDED and normalized:
            raise ValueError("only SUPERSEDED evidence may name superseding insights")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "insight_id": self.insight_id,
            "experiment_id": self.experiment_id,
            "surface": self.surface.value,
            "polarity": self.polarity.value,
            "confidence": self.confidence,
            "state": self.state.value,
            "weight": self.weight,
            "created_at": self.created_at,
            "superseded_by_insight_ids": list(self.superseded_by_insight_ids),
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ResearchBeliefEvidenceV1":
        if raw.get("schema_version") != 1:
            raise ValueError("ResearchBeliefEvidenceV1 schema_version must be 1")
        return cls(
            insight_id=str(raw["insight_id"]),
            experiment_id=str(raw["experiment_id"]),
            surface=ResearchInsightSurfaceV1(str(raw["surface"])),
            polarity=ResearchInsightPolarityV1(str(raw["polarity"])),
            confidence=float(raw["confidence"]),
            state=ResearchBeliefEvidenceStateV1(str(raw["state"])),
            weight=float(raw["weight"]),
            created_at=str(raw["created_at"]),
            superseded_by_insight_ids=tuple(raw.get("superseded_by_insight_ids", ())),
            reason=None if raw.get("reason") is None else str(raw["reason"]),
        )


@dataclass(frozen=True, slots=True)
class ResearchBeliefV1:
    belief_id: str
    campaign_id: str
    subject_key: str
    strategy_id: str
    scope: ResearchBeliefScopeV1
    status: ResearchBeliefStatusV1
    applicability: ResearchBeliefApplicabilityV1
    support_weight: float
    contradiction_weight: float
    net_strength: float
    confidence: float
    latest_evidence_at: str
    evidence: tuple[ResearchBeliefEvidenceV1, ...]
    lineage_experiment_ids: tuple[str, ...]
    observed_code_base_commits: tuple[str, ...]
    stale_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        safe_id(self.belief_id, "belief_id")
        safe_id(self.campaign_id, "campaign_id")
        if not isinstance(self.subject_key, str) or not self.subject_key.strip():
            raise ValueError("subject_key must be non-empty string")
        safe_id(self.strategy_id, "strategy_id")
        if not isinstance(self.scope, ResearchBeliefScopeV1):
            raise ValueError("scope must be ResearchBeliefScopeV1")
        if not isinstance(self.status, ResearchBeliefStatusV1):
            raise ValueError("status must be ResearchBeliefStatusV1")
        if not isinstance(self.applicability, ResearchBeliefApplicabilityV1):
            raise ValueError("applicability must be ResearchBeliefApplicabilityV1")
        object.__setattr__(self, "support_weight", non_negative_number(self.support_weight, "support_weight"))
        object.__setattr__(
            self,
            "contradiction_weight",
            non_negative_number(self.contradiction_weight, "contradiction_weight"),
        )
        object.__setattr__(self, "net_strength", finite_number(self.net_strength, "net_strength"))
        confidence = finite_number(self.confidence, "confidence")
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("confidence must be in [0,1]")
        object.__setattr__(self, "confidence", confidence)
        object.__setattr__(self, "latest_evidence_at", timestamp(self.latest_evidence_at, "latest_evidence_at"))
        if not isinstance(self.evidence, tuple) or not self.evidence:
            raise ValueError("evidence must be non-empty tuple")
        if any(not isinstance(item, ResearchBeliefEvidenceV1) for item in self.evidence):
            raise ValueError("evidence must contain ResearchBeliefEvidenceV1")
        evidence_ids = tuple(item.insight_id for item in self.evidence)
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("belief evidence insight_ids must be unique")
        lineage = tuple(safe_id(item, "lineage_experiment_ids item") for item in self.lineage_experiment_ids)
        if len(lineage) != len(set(lineage)):
            raise ValueError("lineage_experiment_ids must be unique")
        object.__setattr__(self, "lineage_experiment_ids", lineage)
        commits = tuple(str(item).strip().lower() for item in self.observed_code_base_commits)
        if any(len(item) < 7 or any(ch not in "0123456789abcdef" for ch in item) for item in commits):
            raise ValueError("observed_code_base_commits must contain hexadecimal Git commits")
        if len(commits) != len(set(commits)):
            raise ValueError("observed_code_base_commits must be unique")
        object.__setattr__(self, "observed_code_base_commits", commits)
        reasons = tuple(str(item).strip() for item in self.stale_reasons)
        if any(not item for item in reasons) or len(reasons) != len(set(reasons)):
            raise ValueError("stale_reasons must contain unique non-empty strings")
        object.__setattr__(self, "stale_reasons", reasons)
        if self.applicability is ResearchBeliefApplicabilityV1.STALE and not reasons:
            raise ValueError("STALE belief requires stale_reasons")
        if self.applicability is not ResearchBeliefApplicabilityV1.STALE and reasons:
            raise ValueError("only STALE belief may carry stale_reasons")
        expected_net = self.support_weight - self.contradiction_weight
        if abs(expected_net - self.net_strength) > 1e-9:
            raise ValueError("net_strength must equal support_weight - contradiction_weight")

    @property
    def active_insight_ids(self) -> tuple[str, ...]:
        return tuple(item.insight_id for item in self.evidence if item.state is ResearchBeliefEvidenceStateV1.ACTIVE)

    @property
    def superseded_insight_ids(self) -> tuple[str, ...]:
        return tuple(item.insight_id for item in self.evidence if item.state is ResearchBeliefEvidenceStateV1.SUPERSEDED)

    @property
    def diagnostic_insight_ids(self) -> tuple[str, ...]:
        return tuple(item.insight_id for item in self.evidence if item.state is ResearchBeliefEvidenceStateV1.DIAGNOSTIC_ONLY)

    @property
    def fingerprint(self) -> str:
        return stable_sha256(self.to_dict(include_fingerprint=False))

    def to_dict(self, *, include_fingerprint: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": 1,
            "belief_id": self.belief_id,
            "campaign_id": self.campaign_id,
            "subject_key": self.subject_key,
            "strategy_id": self.strategy_id,
            "scope": self.scope.to_dict(),
            "status": self.status.value,
            "applicability": self.applicability.value,
            "support_weight": self.support_weight,
            "contradiction_weight": self.contradiction_weight,
            "net_strength": self.net_strength,
            "confidence": self.confidence,
            "latest_evidence_at": self.latest_evidence_at,
            "evidence": [item.to_dict() for item in self.evidence],
            "lineage_experiment_ids": list(self.lineage_experiment_ids),
            "observed_code_base_commits": list(self.observed_code_base_commits),
            "stale_reasons": list(self.stale_reasons),
            "active_insight_ids": list(self.active_insight_ids),
            "superseded_insight_ids": list(self.superseded_insight_ids),
            "diagnostic_insight_ids": list(self.diagnostic_insight_ids),
        }
        if include_fingerprint:
            payload["fingerprint"] = self.fingerprint
        return payload

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ResearchBeliefV1":
        if raw.get("schema_version") != 1:
            raise ValueError("ResearchBeliefV1 schema_version must be 1")
        result = cls(
            belief_id=str(raw["belief_id"]),
            campaign_id=str(raw["campaign_id"]),
            subject_key=str(raw["subject_key"]),
            strategy_id=str(raw["strategy_id"]),
            scope=ResearchBeliefScopeV1.from_dict(raw["scope"]),
            status=ResearchBeliefStatusV1(str(raw["status"])),
            applicability=ResearchBeliefApplicabilityV1(str(raw["applicability"])),
            support_weight=float(raw["support_weight"]),
            contradiction_weight=float(raw["contradiction_weight"]),
            net_strength=float(raw["net_strength"]),
            confidence=float(raw["confidence"]),
            latest_evidence_at=str(raw["latest_evidence_at"]),
            evidence=tuple(ResearchBeliefEvidenceV1.from_dict(item) for item in raw.get("evidence", ())),
            lineage_experiment_ids=tuple(raw.get("lineage_experiment_ids", ())),
            observed_code_base_commits=tuple(raw.get("observed_code_base_commits", ())),
            stale_reasons=tuple(raw.get("stale_reasons", ())),
        )
        expected = raw.get("fingerprint")
        if expected is not None and expected != result.fingerprint:
            raise ValueError("ResearchBeliefV1 fingerprint mismatch")
        for field_name, actual in (
            ("active_insight_ids", result.active_insight_ids),
            ("superseded_insight_ids", result.superseded_insight_ids),
            ("diagnostic_insight_ids", result.diagnostic_insight_ids),
        ):
            if raw.get(field_name) is not None and tuple(raw[field_name]) != actual:
                raise ValueError(f"ResearchBeliefV1 {field_name} mismatch")
        return result


@dataclass(frozen=True, slots=True)
class ResearchBeliefSetV1:
    campaign_id: str
    beliefs: tuple[ResearchBeliefV1, ...]
    source_insight_count: int
    grouped_belief_count: int
    max_beliefs: int
    omitted_by_budget_count: int
    target_scope_fingerprint: str | None = None

    def __post_init__(self) -> None:
        safe_id(self.campaign_id, "campaign_id")
        if not isinstance(self.beliefs, tuple) or any(not isinstance(item, ResearchBeliefV1) for item in self.beliefs):
            raise ValueError("beliefs must be tuple of ResearchBeliefV1")
        if any(item.campaign_id != self.campaign_id for item in self.beliefs):
            raise ValueError("beliefs must share campaign_id")
        object.__setattr__(self, "source_insight_count", non_negative_int(self.source_insight_count, "source_insight_count"))
        object.__setattr__(self, "grouped_belief_count", non_negative_int(self.grouped_belief_count, "grouped_belief_count"))
        object.__setattr__(self, "max_beliefs", positive_int(self.max_beliefs, "max_beliefs"))
        object.__setattr__(
            self,
            "omitted_by_budget_count",
            non_negative_int(self.omitted_by_budget_count, "omitted_by_budget_count"),
        )
        if len(self.beliefs) > self.max_beliefs:
            raise ValueError("beliefs exceed max_beliefs")
        if len(self.beliefs) + self.omitted_by_budget_count != self.grouped_belief_count:
            raise ValueError("belief set grouped/omitted counts are inconsistent")
        if self.target_scope_fingerprint is not None:
            object.__setattr__(
                self,
                "target_scope_fingerprint",
                sha256_hex(self.target_scope_fingerprint, "target_scope_fingerprint"),
            )

    @property
    def fingerprint(self) -> str:
        return stable_sha256(self.to_dict(include_fingerprint=False))

    def to_dict(self, *, include_fingerprint: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": 1,
            "campaign_id": self.campaign_id,
            "beliefs": [item.to_dict() for item in self.beliefs],
            "source_insight_count": self.source_insight_count,
            "grouped_belief_count": self.grouped_belief_count,
            "max_beliefs": self.max_beliefs,
            "omitted_by_budget_count": self.omitted_by_budget_count,
            "target_scope_fingerprint": self.target_scope_fingerprint,
        }
        if include_fingerprint:
            payload["fingerprint"] = self.fingerprint
        return payload

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ResearchBeliefSetV1":
        if raw.get("schema_version") != 1:
            raise ValueError("ResearchBeliefSetV1 schema_version must be 1")
        result = cls(
            campaign_id=str(raw["campaign_id"]),
            beliefs=tuple(ResearchBeliefV1.from_dict(item) for item in raw.get("beliefs", ())),
            source_insight_count=int(raw["source_insight_count"]),
            grouped_belief_count=int(raw["grouped_belief_count"]),
            max_beliefs=int(raw["max_beliefs"]),
            omitted_by_budget_count=int(raw["omitted_by_budget_count"]),
            target_scope_fingerprint=(
                None if raw.get("target_scope_fingerprint") is None else str(raw["target_scope_fingerprint"])
            ),
        )
        expected = raw.get("fingerprint")
        if expected is not None and expected != result.fingerprint:
            raise ValueError("ResearchBeliefSetV1 fingerprint mismatch")
        return result


@dataclass(frozen=True, slots=True)
class ResearchBeliefConsolidationPolicyV1:
    dev_weight: float = 1.0
    gate_weight: float = 3.0
    repair_weight: float = 2.0
    max_side_weight: float = 8.0
    decision_margin: float = 0.25
    max_beliefs: int = 128

    def __post_init__(self) -> None:
        for name in ("dev_weight", "gate_weight", "repair_weight", "max_side_weight", "decision_margin"):
            object.__setattr__(self, name, non_negative_number(getattr(self, name), name))
        if self.max_side_weight <= 0:
            raise ValueError("max_side_weight must be > 0")
        object.__setattr__(self, "max_beliefs", positive_int(self.max_beliefs, "max_beliefs"))
        if self.max_beliefs > 10_000:
            raise ValueError("max_beliefs must be <= 10000")

    def surface_weight(self, surface: ResearchInsightSurfaceV1) -> float:
        if surface is ResearchInsightSurfaceV1.DEV:
            return self.dev_weight
        if surface is ResearchInsightSurfaceV1.GATE:
            return self.gate_weight
        if surface is ResearchInsightSurfaceV1.REPAIR:
            return self.repair_weight
        raise ValueError(f"unsupported belief surface {surface!r}")


@dataclass(frozen=True, slots=True)
class _EvidenceWork:
    insight: ResearchInsightRecordV1
    experiment_id: str
    semantic_strategy_id: str
    scope: ResearchBeliefScopeV1
    code_base_commit: str


class DeterministicResearchBeliefConsolidatorV1:
    """Derive bounded strategy beliefs from immutable typed research insights."""

    def __init__(self, policy: ResearchBeliefConsolidationPolicyV1 = ResearchBeliefConsolidationPolicyV1()) -> None:
        if not isinstance(policy, ResearchBeliefConsolidationPolicyV1):
            raise ValueError("policy must be ResearchBeliefConsolidationPolicyV1")
        self.policy = policy

    @staticmethod
    def scope_for_experiment(stored: "StoredExperimentV1") -> ResearchBeliefScopeV1:
        spec = stored.spec
        return ResearchBeliefScopeV1(
            evaluation_contract_hash=spec.evaluation_contract_hash,
            dataset_fingerprint=spec.dataset_fingerprint,
            model_id=spec.model_id,
            provider=spec.provider,
            model_version=spec.model_version,
            prompt_hash=spec.prompt_hash,
            tool_policy_hash=spec.tool_policy_hash,
        )

    def consolidate(
        self,
        *,
        campaign_id: str,
        insights: Sequence[ResearchInsightRecordV1],
        experiments: Sequence["StoredExperimentV1"],
        target: ResearchBeliefTargetV1 | None = None,
    ) -> ResearchBeliefSetV1:
        safe_id(campaign_id, "campaign_id")
        if target is not None and not isinstance(target, ResearchBeliefTargetV1):
            raise ValueError("target must be ResearchBeliefTargetV1")
        by_experiment: dict[str, StoredExperimentV1] = {}
        for stored in experiments:
            if stored.spec.campaign_id != campaign_id:
                raise ValueError("belief experiment evidence is cross-campaign")
            if stored.spec.experiment_id in by_experiment:
                raise ValueError("duplicate experiment evidence")
            by_experiment[stored.spec.experiment_id] = stored

        repair_source: dict[str, str] = {}
        for stored in experiments:
            spec = stored.spec
            raw = spec.execution.get("repair_source_experiment_id")
            if raw is None:
                continue
            source_id = safe_id(raw, "execution.repair_source_experiment_id")
            if source_id not in spec.parent_experiment_ids:
                raise ValueError("repair_source_experiment_id must be an explicit parent experiment")
            if source_id not in by_experiment:
                raise ValueError("repair source experiment is missing")
            repair_source[spec.experiment_id] = source_id

        semantic_strategy_cache: dict[str, str] = {}

        def semantic_strategy(experiment_id: str, stack: tuple[str, ...] = ()) -> str:
            if experiment_id in semantic_strategy_cache:
                return semantic_strategy_cache[experiment_id]
            if experiment_id in stack:
                raise ValueError("repair lineage cycle detected")
            stored = by_experiment.get(experiment_id)
            if stored is None:
                raise ValueError(f"insight references missing experiment: {experiment_id}")
            source_id = repair_source.get(experiment_id)
            value = stored.spec.strategy_id if source_id is None else semantic_strategy(source_id, stack + (experiment_id,))
            semantic_strategy_cache[experiment_id] = value
            return value

        work: list[_EvidenceWork] = []
        for insight in insights:
            if not isinstance(insight, ResearchInsightRecordV1):
                raise TypeError("insights must contain ResearchInsightRecordV1")
            if insight.campaign_id != campaign_id:
                raise ValueError("belief insight is cross-campaign")
            experiment_ids = tuple(dict.fromkeys(item.experiment_id for item in insight.evidence))
            if len(experiment_ids) != 1:
                raise ValueError("R7.5 V1 requires each insight to resolve to one experiment")
            experiment_id = experiment_ids[0]
            stored = by_experiment.get(experiment_id)
            if stored is None:
                raise ValueError(f"insight references missing experiment: {experiment_id}")
            if stored.spec.evaluation_contract_hash != insight.evaluation_contract_hash:
                raise ValueError("insight/experiment EvaluationContractHash mismatch")
            work.append(
                _EvidenceWork(
                    insight=insight,
                    experiment_id=experiment_id,
                    semantic_strategy_id=semantic_strategy(experiment_id),
                    scope=self.scope_for_experiment(stored),
                    code_base_commit=stored.spec.code_base_commit,
                )
            )

        by_insight_id = {item.insight.insight_id: item for item in work}
        if len(by_insight_id) != len(work):
            raise ValueError("duplicate insight_id in consolidation input")

        superseded_by: dict[str, set[str]] = {item.insight.insight_id: set() for item in work}
        gate_by_experiment: dict[str, list[_EvidenceWork]] = {}
        for item in work:
            if item.insight.surface is ResearchInsightSurfaceV1.GATE:
                gate_by_experiment.setdefault(item.experiment_id, []).append(item)

        # Protected GATE is authoritative for the exact candidate experiment.  Avoid
        # double-counting weaker DEV/repair success/failure from that same candidate.
        for experiment_id, gate_items in gate_by_experiment.items():
            gate_ids = {item.insight.insight_id for item in gate_items}
            for item in work:
                if item.experiment_id != experiment_id:
                    continue
                if item.insight.surface in {ResearchInsightSurfaceV1.DEV, ResearchInsightSurfaceV1.REPAIR}:
                    superseded_by[item.insight.insight_id].update(gate_ids)

        # A protected confirmation of a repair descendant retires candidate-owned
        # GATE rejections on *all* repair ancestors.  This is the core lineage-aware
        # rule preventing a fixed implementation bug from permanently poisoning the
        # semantic research strategy.
        for child_id, gate_items in gate_by_experiment.items():
            confirming_ids = {
                item.insight.insight_id
                for item in gate_items
                if item.insight.kind is ResearchInsightKindV1.GATE_CONFIRMED
                and item.insight.disposition is ResearchInsightDispositionV1.SEARCH_FEEDBACK
            }
            if not confirming_ids:
                continue
            ancestor = repair_source.get(child_id)
            visited: set[str] = set()
            while ancestor is not None:
                if ancestor in visited:
                    raise ValueError("repair lineage cycle detected")
                visited.add(ancestor)
                for ancestor_gate in gate_by_experiment.get(ancestor, ()):  # usually one durable GATE
                    if ancestor_gate.insight.kind is ResearchInsightKindV1.GATE_REJECTED:
                        superseded_by[ancestor_gate.insight.insight_id].update(confirming_ids)
                ancestor = repair_source.get(ancestor)

        grouped: dict[tuple[str, str], list[_EvidenceWork]] = {}
        for item in work:
            grouped.setdefault((item.semantic_strategy_id, item.scope.fingerprint), []).append(item)

        beliefs: list[ResearchBeliefV1] = []
        for (strategy_id, _scope_fingerprint), items in grouped.items():
            scope = items[0].scope
            if any(item.scope != scope for item in items):  # pragma: no cover - key should prevent this
                raise ValueError("belief group scope mismatch")
            evidence_rows: list[ResearchBeliefEvidenceV1] = []
            support = 0.0
            contradiction = 0.0
            for item in sorted(items, key=lambda row: (row.insight.created_at, row.insight.insight_id)):
                insight = item.insight
                superseders = tuple(sorted(superseded_by[insight.insight_id]))
                if insight.disposition is ResearchInsightDispositionV1.DIAGNOSTIC_ONLY:
                    state = ResearchBeliefEvidenceStateV1.DIAGNOSTIC_ONLY
                    weight = 0.0
                    reason = "diagnostic-only evidence is retained but cannot alter belief strength"
                elif superseders:
                    state = ResearchBeliefEvidenceStateV1.SUPERSEDED
                    weight = 0.0
                    reason = "stronger protected/descendant evidence supersedes this observation"
                else:
                    state = ResearchBeliefEvidenceStateV1.ACTIVE
                    weight = self.policy.surface_weight(insight.surface) * insight.confidence
                    reason = None
                    if insight.polarity is ResearchInsightPolarityV1.SUPPORTING:
                        support += weight
                    elif insight.polarity is ResearchInsightPolarityV1.CONTRADICTING:
                        contradiction += weight
                evidence_rows.append(
                    ResearchBeliefEvidenceV1(
                        insight_id=insight.insight_id,
                        experiment_id=item.experiment_id,
                        surface=insight.surface,
                        polarity=insight.polarity,
                        confidence=insight.confidence,
                        state=state,
                        weight=weight,
                        created_at=insight.created_at,
                        superseded_by_insight_ids=superseders,
                        reason=reason,
                    )
                )
            support = min(self.policy.max_side_weight, support)
            contradiction = min(self.policy.max_side_weight, contradiction)
            net = support - contradiction
            total = support + contradiction
            active_count = sum(1 for item in evidence_rows if item.state is ResearchBeliefEvidenceStateV1.ACTIVE)
            diagnostic_count = sum(
                1 for item in evidence_rows if item.state is ResearchBeliefEvidenceStateV1.DIAGNOSTIC_ONLY
            )
            if active_count == 0:
                status = (
                    ResearchBeliefStatusV1.DIAGNOSTIC_ONLY
                    if diagnostic_count > 0
                    else ResearchBeliefStatusV1.SUPERSEDED
                )
            elif net > self.policy.decision_margin:
                status = ResearchBeliefStatusV1.SUPPORTED
            elif net < -self.policy.decision_margin:
                status = ResearchBeliefStatusV1.CONTRADICTED
            else:
                status = ResearchBeliefStatusV1.MIXED
            confidence = 0.0 if total == 0.0 else min(1.0, abs(net) / self.policy.max_side_weight)
            latest = max((item.insight.created_at for item in items), key=timestamp_value)
            lineage_ids = tuple(sorted({item.experiment_id for item in items}))
            commits = tuple(sorted({item.code_base_commit for item in items}))
            applicability, stale_reasons = self._applicability(
                scope=scope,
                evidence_experiment_ids=lineage_ids,
                observed_commits=commits,
                target=target,
                by_experiment=by_experiment,
            )
            logical = stable_sha256(
                {
                    "campaign_id": campaign_id,
                    "strategy_id": strategy_id,
                    "scope_fingerprint": scope.fingerprint,
                    "evidence_ids": [item.insight.insight_id for item in items],
                }
            )
            beliefs.append(
                ResearchBeliefV1(
                    belief_id=f"r75b-{logical[:24]}",
                    campaign_id=campaign_id,
                    subject_key=f"strategy:{strategy_id}",
                    strategy_id=strategy_id,
                    scope=scope,
                    status=status,
                    applicability=applicability,
                    support_weight=support,
                    contradiction_weight=contradiction,
                    net_strength=net,
                    confidence=confidence,
                    latest_evidence_at=latest,
                    evidence=tuple(evidence_rows),
                    lineage_experiment_ids=lineage_ids,
                    observed_code_base_commits=commits,
                    stale_reasons=stale_reasons,
                )
            )

        applicability_rank = {
            ResearchBeliefApplicabilityV1.CURRENT: 0,
            ResearchBeliefApplicabilityV1.UNSCOPED: 1,
            ResearchBeliefApplicabilityV1.STALE: 2,
            ResearchBeliefApplicabilityV1.INCOMPATIBLE: 3,
        }
        status_rank = {
            ResearchBeliefStatusV1.SUPPORTED: 0,
            ResearchBeliefStatusV1.CONTRADICTED: 1,
            ResearchBeliefStatusV1.MIXED: 2,
            ResearchBeliefStatusV1.DIAGNOSTIC_ONLY: 3,
            ResearchBeliefStatusV1.SUPERSEDED: 4,
        }
        beliefs.sort(
            key=lambda item: (
                applicability_rank[item.applicability],
                status_rank[item.status],
                -item.confidence,
                -timestamp_value(item.latest_evidence_at).timestamp(),
                item.belief_id,
            )
        )
        grouped_count = len(beliefs)
        bounded = tuple(beliefs[: self.policy.max_beliefs])
        return ResearchBeliefSetV1(
            campaign_id=campaign_id,
            beliefs=bounded,
            source_insight_count=len(work),
            grouped_belief_count=grouped_count,
            max_beliefs=self.policy.max_beliefs,
            omitted_by_budget_count=max(0, grouped_count - len(bounded)),
            target_scope_fingerprint=None if target is None else target.scope.fingerprint,
        )

    def _applicability(
        self,
        *,
        scope: ResearchBeliefScopeV1,
        evidence_experiment_ids: tuple[str, ...],
        observed_commits: tuple[str, ...],
        target: ResearchBeliefTargetV1 | None,
        by_experiment: Mapping[str, "StoredExperimentV1"],
    ) -> tuple[ResearchBeliefApplicabilityV1, tuple[str, ...]]:
        if target is None:
            return ResearchBeliefApplicabilityV1.UNSCOPED, ()
        if scope.evaluation_contract_hash != target.scope.evaluation_contract_hash:
            return ResearchBeliefApplicabilityV1.INCOMPATIBLE, ()
        if scope.fingerprint != target.scope.fingerprint:
            reasons = []
            if scope.dataset_fingerprint != target.scope.dataset_fingerprint:
                reasons.append("DATASET_DRIFT")
            if (scope.model_id, scope.provider, scope.model_version) != (
                target.scope.model_id,
                target.scope.provider,
                target.scope.model_version,
            ):
                reasons.append("MODEL_DRIFT")
            if scope.prompt_hash != target.scope.prompt_hash:
                reasons.append("PROMPT_DRIFT")
            if scope.tool_policy_hash != target.scope.tool_policy_hash:
                reasons.append("TOOL_POLICY_DRIFT")
            return ResearchBeliefApplicabilityV1.STALE, tuple(reasons or ("EXECUTION_SCOPE_DRIFT",))
        if target.strict_code_base and target.code_base_commit is not None:
            if target.code_base_commit in observed_commits:
                return ResearchBeliefApplicabilityV1.CURRENT, ()
            if target.experiment_id is not None and target.experiment_id in by_experiment:
                if any(self._is_ancestor(source_id, target.experiment_id, by_experiment) for source_id in evidence_experiment_ids):
                    return ResearchBeliefApplicabilityV1.CURRENT, ()
            return ResearchBeliefApplicabilityV1.STALE, ("CODE_BASE_DRIFT",)
        return ResearchBeliefApplicabilityV1.CURRENT, ()

    @staticmethod
    def _is_ancestor(
        source_experiment_id: str,
        target_experiment_id: str,
        by_experiment: Mapping[str, "StoredExperimentV1"],
    ) -> bool:
        if source_experiment_id == target_experiment_id:
            return True
        pending = [target_experiment_id]
        visited: set[str] = set()
        while pending:
            current = pending.pop()
            if current in visited:
                continue
            visited.add(current)
            stored = by_experiment.get(current)
            if stored is None:
                continue
            for parent in stored.spec.parent_experiment_ids:
                if parent == source_experiment_id:
                    return True
                pending.append(parent)
        return False

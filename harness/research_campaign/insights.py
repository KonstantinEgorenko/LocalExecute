'research campaign.R7.0-R7.2 typed research memory and deterministic insight extraction.\n\nR7 turns durable experiment history into structured research memory.  The extractor\nis deliberately deterministic and model-free.  It consumes DEV experiment results,\nprotected GATE evaluations, and repair outcomes.  Protected TEST evidence is never\naccepted as a research-memory surface and therefore cannot become search feedback.\n'
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Sequence

from .candidate_evaluation import (
    CandidateEvaluationStatusV1,
    CandidateEvaluationSurfaceV1,
    StoredCandidateEvaluationV1,
)
from .models import (
    ExperimentFailureKindV1,
    ExperimentStatusV1,
)
from .repair import RepairProposalV1
from .serialization import (
    finite_number,
    freeze_json,
    optional_safe_id,
    optional_text,
    plain,
    safe_id,
    sha256_hex,
    stable_sha256,
    text,
    timestamp,
)


_EXTRACTOR_VERSION = "phase22-r7.2-deterministic-v1"


class ResearchInsightKindV1(StrEnum):
    DEV_SUCCEEDED = "DEV_SUCCEEDED"
    DEV_CHAMPION_UPDATE = "DEV_CHAMPION_UPDATE"
    DEV_NO_IMPROVEMENT = "DEV_NO_IMPROVEMENT"
    DEV_FAILURE = "DEV_FAILURE"
    GATE_CONFIRMED = "GATE_CONFIRMED"
    GATE_REJECTED = "GATE_REJECTED"
    REPAIR_SUCCEEDED = "REPAIR_SUCCEEDED"
    REPAIR_FAILED = "REPAIR_FAILED"


class ResearchInsightPolarityV1(StrEnum):
    SUPPORTING = "SUPPORTING"
    CONTRADICTING = "CONTRADICTING"
    NEUTRAL = "NEUTRAL"


class ResearchInsightDispositionV1(StrEnum):
    """Whether a memory item may later influence search policy.

    DIAGNOSTIC_ONLY keeps infrastructure/tooling failures in durable memory without
    teaching future hypothesis search that the researched idea itself was bad.
    """

    SEARCH_FEEDBACK = "SEARCH_FEEDBACK"
    DIAGNOSTIC_ONLY = "DIAGNOSTIC_ONLY"


class ResearchInsightSurfaceV1(StrEnum):
    """Research-memory surfaces.  TEST is intentionally absent."""

    DEV = "DEV"
    GATE = "GATE"
    REPAIR = "REPAIR"


class ResearchInsightEvidenceKindV1(StrEnum):
    EXPERIMENT_RESULT = "EXPERIMENT_RESULT"
    GATE_EVALUATION = "GATE_EVALUATION"
    REPAIR_OUTCOME = "REPAIR_OUTCOME"


@dataclass(frozen=True, slots=True)
class ResearchInsightEvidenceV1:
    source_kind: ResearchInsightEvidenceKindV1
    source_id: str
    source_fingerprint: str
    experiment_id: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.source_kind, ResearchInsightEvidenceKindV1):
            raise ValueError("source_kind must be ResearchInsightEvidenceKindV1")
        safe_id(self.source_id, "source_id")
        object.__setattr__(
            self,
            "source_fingerprint",
            sha256_hex(self.source_fingerprint, "source_fingerprint"),
        )
        safe_id(self.experiment_id, "experiment_id")
        metadata = freeze_json(self.metadata, "metadata")
        if not isinstance(metadata, Mapping):
            raise ValueError("metadata must be mapping")
        object.__setattr__(self, "metadata", metadata)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "source_kind": self.source_kind.value,
            "source_id": self.source_id,
            "source_fingerprint": self.source_fingerprint,
            "experiment_id": self.experiment_id,
            "metadata": plain(self.metadata),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ResearchInsightEvidenceV1":
        if raw.get("schema_version") != 1:
            raise ValueError("ResearchInsightEvidenceV1 schema_version must be 1")
        return cls(
            source_kind=ResearchInsightEvidenceKindV1(str(raw["source_kind"])),
            source_id=str(raw["source_id"]),
            source_fingerprint=str(raw["source_fingerprint"]),
            experiment_id=str(raw["experiment_id"]),
            metadata=raw.get("metadata", {}),
        )


@dataclass(frozen=True, slots=True)
class ResearchInsightRecordV1:
    insight_id: str
    campaign_id: str
    kind: ResearchInsightKindV1
    polarity: ResearchInsightPolarityV1
    disposition: ResearchInsightDispositionV1
    surface: ResearchInsightSurfaceV1
    statement: str
    subject_key: str
    evaluation_contract_hash: str
    confidence: float
    created_at: str
    evidence: tuple[ResearchInsightEvidenceV1, ...]
    hypothesis_id: str | None = None
    strategy_id: str | None = None
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        safe_id(self.insight_id, "insight_id")
        safe_id(self.campaign_id, "campaign_id")
        if not isinstance(self.kind, ResearchInsightKindV1):
            raise ValueError("kind must be ResearchInsightKindV1")
        if not isinstance(self.polarity, ResearchInsightPolarityV1):
            raise ValueError("polarity must be ResearchInsightPolarityV1")
        if not isinstance(self.disposition, ResearchInsightDispositionV1):
            raise ValueError("disposition must be ResearchInsightDispositionV1")
        if not isinstance(self.surface, ResearchInsightSurfaceV1):
            raise ValueError("surface must be ResearchInsightSurfaceV1")
        text(self.statement, "statement", max_length=32_000)
        text(self.subject_key, "subject_key", max_length=8_000)
        object.__setattr__(
            self,
            "evaluation_contract_hash",
            sha256_hex(self.evaluation_contract_hash, "evaluation_contract_hash"),
        )
        confidence = finite_number(self.confidence, "confidence")
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("confidence must be in [0,1]")
        object.__setattr__(self, "confidence", confidence)
        object.__setattr__(self, "created_at", timestamp(self.created_at, "created_at"))
        if not isinstance(self.evidence, tuple) or not self.evidence:
            raise ValueError("evidence must be a non-empty tuple")
        if any(not isinstance(item, ResearchInsightEvidenceV1) for item in self.evidence):
            raise ValueError("evidence must contain ResearchInsightEvidenceV1")
        keys = [(item.source_kind.value, item.source_id) for item in self.evidence]
        if len(keys) != len(set(keys)):
            raise ValueError("evidence sources must be unique")
        object.__setattr__(self, "hypothesis_id", optional_safe_id(self.hypothesis_id, "hypothesis_id"))
        object.__setattr__(self, "strategy_id", optional_safe_id(self.strategy_id, "strategy_id"))
        attributes = freeze_json(self.attributes, "attributes")
        if not isinstance(attributes, Mapping):
            raise ValueError("attributes must be mapping")
        object.__setattr__(self, "attributes", attributes)

    @property
    def fingerprint(self) -> str:
        """Logical insight identity, excluding requested ID and creation timestamp."""
        return stable_sha256(
            {
                "schema_version": 1,
                "campaign_id": self.campaign_id,
                "kind": self.kind.value,
                "polarity": self.polarity.value,
                "disposition": self.disposition.value,
                "surface": self.surface.value,
                "statement": self.statement,
                "subject_key": self.subject_key,
                "evaluation_contract_hash": self.evaluation_contract_hash,
                "confidence": self.confidence,
                "evidence": [item.to_dict() for item in self.evidence],
                "hypothesis_id": self.hypothesis_id,
                "strategy_id": self.strategy_id,
                "attributes": plain(self.attributes),
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "insight_id": self.insight_id,
            "campaign_id": self.campaign_id,
            "kind": self.kind.value,
            "polarity": self.polarity.value,
            "disposition": self.disposition.value,
            "surface": self.surface.value,
            "statement": self.statement,
            "subject_key": self.subject_key,
            "evaluation_contract_hash": self.evaluation_contract_hash,
            "confidence": self.confidence,
            "created_at": self.created_at,
            "evidence": [item.to_dict() for item in self.evidence],
            "hypothesis_id": self.hypothesis_id,
            "strategy_id": self.strategy_id,
            "attributes": plain(self.attributes),
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ResearchInsightRecordV1":
        if raw.get("schema_version") != 1:
            raise ValueError("ResearchInsightRecordV1 schema_version must be 1")
        result = cls(
            insight_id=str(raw["insight_id"]),
            campaign_id=str(raw["campaign_id"]),
            kind=ResearchInsightKindV1(str(raw["kind"])),
            polarity=ResearchInsightPolarityV1(str(raw["polarity"])),
            disposition=ResearchInsightDispositionV1(str(raw["disposition"])),
            surface=ResearchInsightSurfaceV1(str(raw["surface"])),
            statement=str(raw["statement"]),
            subject_key=str(raw["subject_key"]),
            evaluation_contract_hash=str(raw["evaluation_contract_hash"]),
            confidence=float(raw["confidence"]),
            created_at=str(raw["created_at"]),
            evidence=tuple(ResearchInsightEvidenceV1.from_dict(item) for item in raw.get("evidence", ())),
            hypothesis_id=None if raw.get("hypothesis_id") is None else str(raw["hypothesis_id"]),
            strategy_id=None if raw.get("strategy_id") is None else str(raw["strategy_id"]),
            attributes=raw.get("attributes", {}),
        )
        expected = raw.get("fingerprint")
        if expected is not None and expected != result.fingerprint:
            raise ValueError("ResearchInsightRecordV1 fingerprint mismatch")
        return result


@dataclass(frozen=True, slots=True)
class ResearchInsightRegistrationV1:
    requested_insight_id: str
    canonical_insight_id: str
    fingerprint: str
    created: bool
    reused_existing: bool


_CANDIDATE_OWNED_FAILURES = {
    ExperimentFailureKindV1.IMPLEMENTATION_BUG,
    ExperimentFailureKindV1.NO_IMPROVEMENT,
}


def experiment_result_evidence_fingerprint(spec: Any, result: Any) -> str:
    return stable_sha256(
        {
            "source_kind": ResearchInsightEvidenceKindV1.EXPERIMENT_RESULT.value,
            "experiment_fingerprint": spec.fingerprint,
            "result": result.to_dict(),
        }
    )


def gate_evaluation_evidence_fingerprint(stored: StoredCandidateEvaluationV1) -> str:
    if stored.spec.surface is not CandidateEvaluationSurfaceV1.GATE:
        raise ValueError("research insight evidence accepts protected GATE only; TEST is excluded")
    if stored.result is None or not stored.result.is_terminal:
        raise ValueError("GATE insight evidence requires terminal result")
    return stable_sha256(
        {
            "source_kind": ResearchInsightEvidenceKindV1.GATE_EVALUATION.value,
            "evaluation_fingerprint": stored.spec.fingerprint,
            "result": stored.result.to_dict(),
        }
    )


def repair_outcome_evidence_fingerprint(proposal: RepairProposalV1, stored_experiment: Any) -> str:
    if stored_experiment.result is None or not stored_experiment.result.is_terminal:
        raise ValueError("repair insight evidence requires terminal repair experiment")
    return stable_sha256(
        {
            "source_kind": ResearchInsightEvidenceKindV1.REPAIR_OUTCOME.value,
            "repair_fingerprint": proposal.fingerprint,
            "repair_experiment_fingerprint": stored_experiment.spec.fingerprint,
            "result": stored_experiment.result.to_dict(),
        }
    )


@dataclass(frozen=True, slots=True)
class DeterministicResearchInsightExtractorV1:
    """Deterministically materialize typed memory from durable research evidence."""

    extractor_version: str = _EXTRACTOR_VERSION

    def extract(
        self,
        *,
        campaign_id: str,
        experiments: Sequence[Any],
        candidate_evaluations: Sequence[StoredCandidateEvaluationV1] = (),
        repair_proposals: Sequence[RepairProposalV1] = (),
        created_at: str,
    ) -> tuple[ResearchInsightRecordV1, ...]:
        safe_id(campaign_id, "campaign_id")
        created_at = timestamp(created_at, "created_at")
        by_experiment: dict[str, Any] = {}
        for stored in experiments:
            spec = stored.spec
            if spec.campaign_id != campaign_id:
                raise ValueError("experiment evidence is cross-campaign")
            by_experiment[spec.experiment_id] = stored

        insights: list[ResearchInsightRecordV1] = []
        for stored in sorted(experiments, key=lambda item: (item.spec.created_at, item.spec.experiment_id)):
            if stored.result is None or not stored.result.is_terminal:
                continue
            insights.append(self._from_dev(stored, created_at))

        for stored in sorted(
            candidate_evaluations,
            key=lambda item: (item.spec.created_at, item.spec.evaluation_id),
        ):
            if stored.spec.campaign_id != campaign_id:
                raise ValueError("candidate evaluation evidence is cross-campaign")
            # TEST must never become research/search memory.
            if stored.spec.surface is not CandidateEvaluationSurfaceV1.GATE:
                continue
            if stored.result is None or not stored.result.is_terminal:
                continue
            if stored.result.status not in {
                CandidateEvaluationStatusV1.SCORED,
                CandidateEvaluationStatusV1.FAILED,
                CandidateEvaluationStatusV1.INVALID,
            }:
                continue
            source = by_experiment.get(stored.spec.candidate.source_experiment_id)
            if source is None:
                raise ValueError("GATE insight evidence source experiment is missing")
            if source.spec.fingerprint != stored.spec.candidate.source_experiment_fingerprint:
                raise ValueError("GATE insight evidence source experiment fingerprint mismatch")
            insights.append(self._from_gate(stored, source, created_at))

        for proposal in sorted(repair_proposals, key=lambda item: (item.created_at, item.repair_id)):
            if proposal.campaign_id != campaign_id:
                raise ValueError("repair evidence is cross-campaign")
            stored = by_experiment.get(proposal.experiment.experiment_id)
            if stored is None or stored.result is None or not stored.result.is_terminal:
                continue
            insights.append(self._from_repair(proposal, stored, created_at))

        # Logical ordering is deterministic and independent of caller input order.
        return tuple(sorted(insights, key=lambda item: (item.surface.value, item.kind.value, item.insight_id)))

    def _record(self, **values: Any) -> ResearchInsightRecordV1:
        logical_id = stable_sha256(
            {
                "extractor_version": self.extractor_version,
                "kind": values["kind"].value,
                "surface": values["surface"].value,
                "subject_key": values["subject_key"],
                "evidence": [item.to_dict() for item in values["evidence"]],
            }
        )
        attributes = dict(values.pop("attributes", {}))
        attributes["extractor_version"] = self.extractor_version
        return ResearchInsightRecordV1(
            insight_id=f"r7i-{logical_id[:24]}",
            attributes=attributes,
            **values,
        )

    def _from_dev(self, stored: Any, created_at: str) -> ResearchInsightRecordV1:
        spec = stored.spec
        result = stored.result
        assert result is not None
        if result.status is ExperimentStatusV1.SUCCEEDED:
            from .decisions import SearchDecisionActionV1
            if result.decision is SearchDecisionActionV1.UPDATE_CHAMPION:
                kind = ResearchInsightKindV1.DEV_CHAMPION_UPDATE
                polarity = ResearchInsightPolarityV1.SUPPORTING
                disposition = ResearchInsightDispositionV1.SEARCH_FEEDBACK
                confidence = 0.65
            elif result.decision in {SearchDecisionActionV1.DROP, SearchDecisionActionV1.PRUNE}:
                kind = ResearchInsightKindV1.DEV_NO_IMPROVEMENT
                polarity = ResearchInsightPolarityV1.CONTRADICTING
                disposition = ResearchInsightDispositionV1.SEARCH_FEEDBACK
                confidence = 0.60
            elif result.decision is SearchDecisionActionV1.RETEST:
                kind = ResearchInsightKindV1.DEV_SUCCEEDED
                polarity = ResearchInsightPolarityV1.NEUTRAL
                disposition = ResearchInsightDispositionV1.DIAGNOSTIC_ONLY
                confidence = 0.50
            else:
                kind = ResearchInsightKindV1.DEV_SUCCEEDED
                polarity = ResearchInsightPolarityV1.SUPPORTING
                disposition = ResearchInsightDispositionV1.SEARCH_FEEDBACK
                confidence = 0.55
            statement = (
                f"DEV experiment {spec.experiment_id} succeeded under strategy {spec.strategy_id} "
                f"with primary metric {result.primary_metric} and decision "
                f"{None if result.decision is None else result.decision.value}."
            )
        else:
            kind = ResearchInsightKindV1.DEV_FAILURE
            polarity = ResearchInsightPolarityV1.CONTRADICTING
            disposition = (
                ResearchInsightDispositionV1.SEARCH_FEEDBACK
                if result.failure_kind in _CANDIDATE_OWNED_FAILURES
                else ResearchInsightDispositionV1.DIAGNOSTIC_ONLY
            )
            failure = "UNKNOWN" if result.failure_kind is None else result.failure_kind.value
            statement = (
                f"DEV experiment {spec.experiment_id} ended {result.status.value} under strategy "
                f"{spec.strategy_id} with failure {failure}."
            )
            confidence = 0.65
        evidence = ResearchInsightEvidenceV1(
            ResearchInsightEvidenceKindV1.EXPERIMENT_RESULT,
            spec.experiment_id,
            experiment_result_evidence_fingerprint(spec, result),
            spec.experiment_id,
            metadata={"status": result.status.value, "failure_kind": None if result.failure_kind is None else result.failure_kind.value},
        )
        return self._record(
            campaign_id=spec.campaign_id,
            kind=kind,
            polarity=polarity,
            disposition=disposition,
            surface=ResearchInsightSurfaceV1.DEV,
            statement=statement,
            subject_key=f"strategy:{spec.strategy_id}",
            evaluation_contract_hash=spec.evaluation_contract_hash,
            confidence=confidence,
            created_at=created_at,
            evidence=(evidence,),
            hypothesis_id=spec.hypothesis_id,
            strategy_id=spec.strategy_id,
            attributes={
                "status": result.status.value,
                "primary_metric": result.primary_metric,
                "failure_kind": None if result.failure_kind is None else result.failure_kind.value,
                "decision": None if result.decision is None else result.decision.value,
                "candidate_commit": result.candidate_commit,
            },
        )

    def _from_gate(self, stored: StoredCandidateEvaluationV1, source: Any, created_at: str) -> ResearchInsightRecordV1:
        spec = stored.spec
        result = stored.result
        assert result is not None
        source_spec = source.spec
        if result.status is CandidateEvaluationStatusV1.SCORED:
            kind = ResearchInsightKindV1.GATE_CONFIRMED
            polarity = ResearchInsightPolarityV1.SUPPORTING
            disposition = ResearchInsightDispositionV1.SEARCH_FEEDBACK
            confidence = 0.85
            statement = (
                f"Protected GATE confirmed candidate from experiment {spec.candidate.source_experiment_id} "
                f"with primary metric {result.primary_metric}."
            )
        else:
            kind = ResearchInsightKindV1.GATE_REJECTED
            polarity = ResearchInsightPolarityV1.CONTRADICTING
            disposition = (
                ResearchInsightDispositionV1.SEARCH_FEEDBACK
                if result.failure_kind in _CANDIDATE_OWNED_FAILURES
                else ResearchInsightDispositionV1.DIAGNOSTIC_ONLY
            )
            confidence = 0.90
            failure = "UNKNOWN" if result.failure_kind is None else result.failure_kind.value
            reason = "" if result.reason is None else f" Reason: {result.reason}"
            statement = (
                f"Protected GATE rejected candidate from experiment {spec.candidate.source_experiment_id} "
                f"with failure {failure}.{reason}"
            )
        evidence = ResearchInsightEvidenceV1(
            ResearchInsightEvidenceKindV1.GATE_EVALUATION,
            spec.evaluation_id,
            gate_evaluation_evidence_fingerprint(stored),
            spec.candidate.source_experiment_id,
            metadata={"status": result.status.value, "candidate_commit": spec.candidate.candidate_commit},
        )
        return self._record(
            campaign_id=spec.campaign_id,
            kind=kind,
            polarity=polarity,
            disposition=disposition,
            surface=ResearchInsightSurfaceV1.GATE,
            statement=statement,
            subject_key=f"candidate:{spec.candidate.source_experiment_id}",
            evaluation_contract_hash=spec.evaluation_contract_hash,
            confidence=confidence,
            created_at=created_at,
            evidence=(evidence,),
            hypothesis_id=source_spec.hypothesis_id,
            strategy_id=source_spec.strategy_id,
            attributes={
                "evaluation_id": spec.evaluation_id,
                "source_hypothesis_id": source_spec.hypothesis_id,
                "source_strategy_id": source_spec.strategy_id,
                "status": result.status.value,
                "primary_metric": result.primary_metric,
                "failure_kind": None if result.failure_kind is None else result.failure_kind.value,
                "candidate_commit": spec.candidate.candidate_commit,
            },
        )

    def _from_repair(self, proposal: RepairProposalV1, stored: Any, created_at: str) -> ResearchInsightRecordV1:
        result = stored.result
        assert result is not None
        if result.status is ExperimentStatusV1.SUCCEEDED:
            kind = ResearchInsightKindV1.REPAIR_SUCCEEDED
            polarity = ResearchInsightPolarityV1.SUPPORTING
            disposition = ResearchInsightDispositionV1.SEARCH_FEEDBACK
            confidence = 0.90
            statement = (
                f"Repair {proposal.repair_id} produced successful descendant experiment "
                f"{proposal.experiment.experiment_id} from failed source {proposal.evidence.source_experiment_id} "
                f"with primary metric {result.primary_metric}."
            )
        else:
            kind = ResearchInsightKindV1.REPAIR_FAILED
            polarity = ResearchInsightPolarityV1.CONTRADICTING
            disposition = (
                ResearchInsightDispositionV1.SEARCH_FEEDBACK
                if result.failure_kind in _CANDIDATE_OWNED_FAILURES
                else ResearchInsightDispositionV1.DIAGNOSTIC_ONLY
            )
            confidence = 0.85
            failure = "UNKNOWN" if result.failure_kind is None else result.failure_kind.value
            statement = (
                f"Repair {proposal.repair_id} ended {result.status.value} for failed source "
                f"{proposal.evidence.source_experiment_id} with failure {failure}."
            )
        evidence = ResearchInsightEvidenceV1(
            ResearchInsightEvidenceKindV1.REPAIR_OUTCOME,
            proposal.repair_id,
            repair_outcome_evidence_fingerprint(proposal, stored),
            proposal.experiment.experiment_id,
            metadata={
                "repair_depth": proposal.repair_depth,
                "source_experiment_id": proposal.evidence.source_experiment_id,
                "source_gate_evaluation_id": proposal.evidence.gate_evaluation_id,
            },
        )
        return self._record(
            campaign_id=proposal.campaign_id,
            kind=kind,
            polarity=polarity,
            disposition=disposition,
            surface=ResearchInsightSurfaceV1.REPAIR,
            statement=statement,
            subject_key=f"repair-source:{proposal.evidence.source_experiment_id}",
            evaluation_contract_hash=proposal.experiment.evaluation_contract_hash,
            confidence=confidence,
            created_at=created_at,
            evidence=(evidence,),
            hypothesis_id=proposal.hypothesis.hypothesis_id,
            strategy_id=proposal.experiment.strategy_id,
            attributes={
                "repair_id": proposal.repair_id,
                "repair_depth": proposal.repair_depth,
                "repair_experiment_id": proposal.experiment.experiment_id,
                "source_experiment_id": proposal.evidence.source_experiment_id,
                "source_gate_evaluation_id": proposal.evidence.gate_evaluation_id,
                "status": result.status.value,
                "primary_metric": result.primary_metric,
                "failure_kind": None if result.failure_kind is None else result.failure_kind.value,
                "candidate_commit": result.candidate_commit,
            },
        )

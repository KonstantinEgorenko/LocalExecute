'Data contracts for research quality research-quality evaluation.'
from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Mapping


class ClaimClass(str, Enum):
    SELF_DESCRIPTION = "SELF_DESCRIPTION"
    ARCHITECTURE = "ARCHITECTURE"
    LICENSE = "LICENSE"
    CURRENT_METRIC = "CURRENT_METRIC"
    INFERENCE = "INFERENCE"
    GENERAL = "GENERAL"


class EntailmentLabel(str, Enum):
    ENTAILED = "ENTAILED"
    PARTIAL = "PARTIAL"
    CONTRADICTED = "CONTRADICTED"
    UNSUPPORTED = "UNSUPPORTED"
    NOT_EVALUATED = "NOT_EVALUATED"


@dataclass(frozen=True, slots=True)
class SourceIdentityV1:
    source_id: str
    canonical_url: str
    document_key: str
    source_family: str
    publisher_key: str
    domain: str
    tier: str
    kind: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class EntailmentResultV1:
    label: EntailmentLabel
    confidence: float
    reason: str

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("entailment confidence must be in [0,1]")

    def to_dict(self) -> dict[str, Any]:
        raw = asdict(self)
        raw["label"] = self.label.value
        return raw


@dataclass(frozen=True, slots=True)
class ClaimQualityV1:
    claim_id: str
    topic: str
    importance: str
    claim_class: ClaimClass
    evidence_refs: int
    supporting_evidence_refs: int
    contradicting_evidence_refs: int
    unique_source_ids: int
    unique_documents: int
    unique_source_families: int
    unique_publishers: int
    unique_domains: int
    primary_documents: int
    evidence_amplification_ratio: float
    fragmented_single_document: bool
    structural_sufficient: bool
    structural_reasons: tuple[str, ...]
    entailment: EntailmentResultV1
    quality_sufficient: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "topic": self.topic,
            "importance": self.importance,
            "claim_class": self.claim_class.value,
            "evidence_refs": self.evidence_refs,
            "supporting_evidence_refs": self.supporting_evidence_refs,
            "contradicting_evidence_refs": self.contradicting_evidence_refs,
            "unique_source_ids": self.unique_source_ids,
            "unique_documents": self.unique_documents,
            "unique_source_families": self.unique_source_families,
            "unique_publishers": self.unique_publishers,
            "unique_domains": self.unique_domains,
            "primary_documents": self.primary_documents,
            "evidence_amplification_ratio": self.evidence_amplification_ratio,
            "fragmented_single_document": self.fragmented_single_document,
            "structural_sufficient": self.structural_sufficient,
            "structural_reasons": list(self.structural_reasons),
            "entailment": self.entailment.to_dict(),
            "quality_sufficient": self.quality_sufficient,
        }


@dataclass(frozen=True, slots=True)
class ResearchQualityReportV1:
    research_id: str
    source_report_fingerprint: str
    source_outcome: str
    source_failure_reason: str
    sources: int
    evidence: int
    claims: int
    unique_source_ids: int
    unique_documents: int
    unique_source_families: int
    unique_publishers: int
    unique_domains: int
    evidence_amplification_ratio: float
    fragmented_claims: int
    important_claims: int
    structurally_insufficient_important_claims: int
    entailment_evaluated_claims: int
    entailment_failed_claims: int
    unsupported_claim_rate: float | None
    quality_sufficient_claims: int
    claim_quality: tuple[ClaimQualityV1, ...]
    diagnostics: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "research_id": self.research_id,
            "source_report_fingerprint": self.source_report_fingerprint,
            "source_outcome": self.source_outcome,
            "source_failure_reason": self.source_failure_reason,
            "metrics": {
                "sources": self.sources,
                "evidence": self.evidence,
                "claims": self.claims,
                "unique_source_ids": self.unique_source_ids,
                "unique_documents": self.unique_documents,
                "unique_source_families": self.unique_source_families,
                "unique_publishers": self.unique_publishers,
                "unique_domains": self.unique_domains,
                "evidence_amplification_ratio": self.evidence_amplification_ratio,
                "fragmented_claims": self.fragmented_claims,
                "important_claims": self.important_claims,
                "structurally_insufficient_important_claims": self.structurally_insufficient_important_claims,
                "entailment_evaluated_claims": self.entailment_evaluated_claims,
                "entailment_failed_claims": self.entailment_failed_claims,
                "unsupported_claim_rate": self.unsupported_claim_rate,
                "quality_sufficient_claims": self.quality_sufficient_claims,
            },
            "claim_quality": [item.to_dict() for item in self.claim_quality],
            "diagnostics": list(self.diagnostics),
        }


def as_mapping(value: object, *, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a mapping")
    return value

'Additive research integrity evidence-integrity contracts.\n\nThese contracts do not modify frozen external evidence/19/19Q models.  They annotate and\nfilter their dictionaries at the research integrity boundary.\n'
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Mapping


class EntityKind(str, Enum):
    GITHUB_REPOSITORY = "GITHUB_REPOSITORY"
    WEB_ENTITY = "WEB_ENTITY"
    UNKNOWN = "UNKNOWN"


class EntityBindingStatus(str, Enum):
    EXACT = "EXACT"
    OFFICIAL_CHILD = "OFFICIAL_CHILD"
    RELATED = "RELATED"
    UNKNOWN = "UNKNOWN"
    MISMATCH = "MISMATCH"


class FreshnessStatus(str, Enum):
    FRESH = "FRESH"
    STALE = "STALE"
    UNAVAILABLE = "UNAVAILABLE"
    INVALID = "INVALID"
    NOT_REQUIRED = "NOT_REQUIRED"


@dataclass(frozen=True, slots=True)
class TargetEntityV1:
    kind: EntityKind
    display_name: str
    canonical_key: str
    github_owner: str = ""
    github_repo: str = ""
    github_repository_id: str = ""
    github_node_id: str = ""
    canonical_urls: tuple[str, ...] = ()

    @property
    def github_full_name(self) -> str:
        if self.github_owner and self.github_repo:
            return f"{self.github_owner}/{self.github_repo}"
        return ""

    def to_dict(self) -> dict[str, Any]:
        raw = asdict(self)
        raw["kind"] = self.kind.value
        raw["github_full_name"] = self.github_full_name
        return raw


@dataclass(frozen=True, slots=True)
class EntityBindingV1:
    status: EntityBindingStatus
    target_key: str
    source_key: str
    reason: str

    @property
    def eligible(self) -> bool:
        return self.status is not EntityBindingStatus.MISMATCH

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "target_key": self.target_key,
            "source_key": self.source_key,
            "reason": self.reason,
            "eligible": self.eligible,
        }


@dataclass(frozen=True, slots=True)
class CanonicalDocumentV2:
    entity_key: str
    document_key: str
    revision_key: str
    artifact_kind: str
    path: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class AcquisitionEnvelopeV1:
    requested_url: str
    final_url: str
    retrieved_at: str
    http_status: int
    content_type: str
    raw_sha256: str
    raw_size: int
    extractor_kind: str
    title: str = ""
    canonical_hint: str = ""
    source_published_at: str = ""
    response_headers: Mapping[str, str] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested_url": self.requested_url,
            "final_url": self.final_url,
            "retrieved_at": self.retrieved_at,
            "http_status": self.http_status,
            "content_type": self.content_type,
            "raw_sha256": self.raw_sha256,
            "raw_size": self.raw_size,
            "extractor_kind": self.extractor_kind,
            "title": self.title,
            "canonical_hint": self.canonical_hint,
            "source_published_at": self.source_published_at,
            "response_headers": dict(self.response_headers),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class RemediationConstraintV2:
    claim_id: str
    target_entity_key: str
    issue_kinds: tuple[str, ...]
    excluded_document_keys: tuple[str, ...]
    required_new_documents: int
    required_total_documents: int
    require_exact_entity: bool
    require_fresh_primary: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class EligibilityDecisionV1:
    evidence_id: str
    eligible: bool
    reasons: tuple[str, ...]
    binding: EntityBindingV1
    document: CanonicalDocumentV2
    freshness: FreshnessStatus
    source_tier: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "eligible": self.eligible,
            "reasons": list(self.reasons),
            "binding": self.binding.to_dict(),
            "document": self.document.to_dict(),
            "freshness": self.freshness.value,
            "source_tier": self.source_tier,
        }


@dataclass(frozen=True, slots=True)
class DefectClosureResultV1:
    closed: bool
    reasons: tuple[str, ...]
    selected_documents: tuple[str, ...]
    new_documents: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

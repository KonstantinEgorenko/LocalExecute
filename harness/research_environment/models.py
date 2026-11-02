from __future__ import annotations
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Mapping

class ResearchCapability(str, Enum):
    WEB_BROWSER_RESEARCH = "WEB_BROWSER_RESEARCH"
    FILE_RESEARCH = "FILE_RESEARCH"
    MEDIA_RESEARCH = "MEDIA_RESEARCH"

@dataclass(frozen=True, slots=True)
class BrowserElement:
    element_id: str
    tag: str
    role: str
    text: str
    name: str
    href: str
    input_type: str
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

@dataclass(frozen=True, slots=True)
class BrowserActionRecord:
    sequence: int
    action: str
    target: str
    detail: str
    url_before: str
    url_after: str
    observed_at: str
    success: bool
    error: str = ""
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

@dataclass(frozen=True, slots=True)
class BrowserObservation:
    url: str
    title: str
    text: str
    elements: tuple[BrowserElement, ...]
    observed_at: str
    action_trail: tuple[BrowserActionRecord, ...]
    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url, "title": self.title, "text": self.text,
            "elements": [x.to_dict() for x in self.elements],
            "observed_at": self.observed_at,
            "action_trail": [x.to_dict() for x in self.action_trail],
        }

@dataclass(frozen=True, slots=True)
class ExtractedFragment:
    fragment_id: str
    text: str
    locator: str
    metadata: Mapping[str, Any]
    def to_dict(self) -> dict[str, Any]:
        return {
            "fragment_id": self.fragment_id, "text": self.text,
            "locator": self.locator, "metadata": dict(self.metadata),
        }

@dataclass(frozen=True, slots=True)
class FileExtractionV1:
    path: str
    artifact_uri: str
    sha256: str
    media_type: str
    size_bytes: int
    extracted_at: str
    fragments: tuple[ExtractedFragment, ...]
    metadata: Mapping[str, Any]

@dataclass(frozen=True, slots=True)
class MediaObservationV1:
    path: str
    artifact_uri: str
    sha256: str
    media_type: str
    observed_at: str
    metadata: Mapping[str, Any]
    semantic_text: str = ""
    semantic_method: str = ""

@dataclass(frozen=True, slots=True)
class NormalizedSourceV1:
    source_id: str
    canonical_url: str
    title: str
    tier: str
    kind: str
    retrieved_at: str
    metadata: Mapping[str, Any]
    def to_research_evidence_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id, "canonical_url": self.canonical_url,
            "title": self.title, "tier": self.tier, "kind": self.kind,
            "retrieved_at": self.retrieved_at, "metadata": dict(self.metadata),
        }

@dataclass(frozen=True, slots=True)
class EvidenceFragmentV1:
    evidence_id: str
    source_id: str
    text: str
    locator: str
    observed_at: str
    metadata: Mapping[str, Any]
    def to_research_evidence_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id, "source_id": self.source_id,
            "text": self.text, "locator": self.locator,
            "observed_at": self.observed_at, "metadata": dict(self.metadata),
        }

# Verified compatibility aliases for historical Python interfaces.
NormalizedSourceV1.to_phase18_dict = NormalizedSourceV1.to_research_evidence_dict
EvidenceFragmentV1.to_phase18_dict = EvidenceFragmentV1.to_research_evidence_dict

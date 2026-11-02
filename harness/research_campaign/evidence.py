'Bounded, typed evidence supplied to research stopping proposal generation.\n\nEvidence is provenance, not authority: this module cannot select an experiment\nor reach the production executor.\n'
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping, Protocol

from .serialization import (
    non_negative_int,
    non_negative_number,
    safe_id,
    stable_sha256,
    text,
    timestamp,
    unique_text,
)


class ResearchEvidenceSourceTypeV1(StrEnum):
    REPOSITORY = "REPOSITORY"
    TEST = "TEST"
    CONFIGURATION = "CONFIGURATION"
    RESEARCH_MEMORY = "RESEARCH_MEMORY"
    EXPERIMENT_HISTORY = "EXPERIMENT_HISTORY"
    CHAMPION = "CHAMPION"
    DIAGNOSTIC = "DIAGNOSTIC"
    EXTERNAL = "EXTERNAL"


class ResearchEvidenceClassificationV1(StrEnum):
    SCIENTIFIC = "SCIENTIFIC"
    DIAGNOSTIC = "DIAGNOSTIC"
    CONTEXT = "CONTEXT"


@dataclass(frozen=True, slots=True)
class ResearchEvidenceItemV1:
    evidence_id: str
    campaign_id: str
    source_type: ResearchEvidenceSourceTypeV1
    source_identity: str
    content: str
    freshness: str
    relevance: float
    provenance_ids: tuple[str, ...]
    confidence: float
    classification: ResearchEvidenceClassificationV1

    def __post_init__(self) -> None:
        safe_id(self.evidence_id, "evidence_id")
        safe_id(self.campaign_id, "campaign_id")
        if not isinstance(self.source_type, ResearchEvidenceSourceTypeV1):
            raise ValueError("source_type must be ResearchEvidenceSourceTypeV1")
        object.__setattr__(self, "source_identity", text(self.source_identity, "source_identity", max_length=8_000))
        object.__setattr__(self, "content", text(self.content, "content", max_length=32_000))
        object.__setattr__(self, "freshness", timestamp(self.freshness, "freshness"))
        relevance = non_negative_number(self.relevance, "relevance")
        confidence = non_negative_number(self.confidence, "confidence")
        if relevance > 1.0 or confidence > 1.0:
            raise ValueError("evidence relevance/confidence must be <= 1")
        object.__setattr__(self, "relevance", relevance)
        object.__setattr__(self, "confidence", confidence)
        object.__setattr__(self, "provenance_ids", unique_text(self.provenance_ids, "provenance_ids", maximum=128, item_max_length=8_000))
        if not isinstance(self.classification, ResearchEvidenceClassificationV1):
            raise ValueError("classification must be ResearchEvidenceClassificationV1")

    @property
    def fingerprint(self) -> str:
        return stable_sha256(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {"schema_version": 1, "evidence_id": self.evidence_id, "campaign_id": self.campaign_id,
                "source_type": self.source_type.value, "source_identity": self.source_identity,
                "content": self.content, "freshness": self.freshness, "relevance": self.relevance,
                "provenance_ids": list(self.provenance_ids), "confidence": self.confidence,
                "classification": self.classification.value}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ResearchEvidenceItemV1":
        if raw.get("schema_version") != 1:
            raise ValueError("ResearchEvidenceItemV1 schema_version must be 1")
        return cls(str(raw["evidence_id"]), str(raw["campaign_id"]), ResearchEvidenceSourceTypeV1(str(raw["source_type"])), str(raw["source_identity"]), str(raw["content"]), str(raw["freshness"]), float(raw["relevance"]), tuple(raw.get("provenance_ids", ())), float(raw["confidence"]), ResearchEvidenceClassificationV1(str(raw["classification"])))



@dataclass(frozen=True, slots=True)
class ResearchEvidenceBudgetV1:
    max_items: int = 32
    max_external_rounds: int = 2
    max_tool_calls: int = 16
    max_context_characters: int = 48_000

    def __post_init__(self) -> None:
        for name in ("max_items", "max_external_rounds", "max_tool_calls", "max_context_characters"):
            value = non_negative_int(getattr(self, name), name)
            if value > 100_000:
                raise ValueError(f"{name} exceeds safe maximum")
            object.__setattr__(self, name, value)

    def to_dict(self) -> dict[str, int]:
        return {"max_items": self.max_items, "max_external_rounds": self.max_external_rounds,
                "max_tool_calls": self.max_tool_calls, "max_context_characters": self.max_context_characters}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ResearchEvidenceBudgetV1":
        return cls(max_items=int(raw["max_items"]), max_external_rounds=int(raw["max_external_rounds"]),
                   max_tool_calls=int(raw["max_tool_calls"]), max_context_characters=int(raw["max_context_characters"]))



@dataclass(frozen=True, slots=True)
class ResearchEvidencePacketV1:
    packet_id: str
    campaign_id: str
    created_at: str
    budget: ResearchEvidenceBudgetV1
    items: tuple[ResearchEvidenceItemV1, ...]
    acquisition_diagnostics: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        safe_id(self.packet_id, "packet_id")
        safe_id(self.campaign_id, "campaign_id")
        object.__setattr__(self, "created_at", timestamp(self.created_at, "created_at"))
        if not isinstance(self.budget, ResearchEvidenceBudgetV1):
            raise ValueError("budget must be ResearchEvidenceBudgetV1")
        if not isinstance(self.items, tuple) or len(self.items) > self.budget.max_items:
            raise ValueError("evidence packet items exceed bounded budget")
        if any(not isinstance(item, ResearchEvidenceItemV1) or item.campaign_id != self.campaign_id for item in self.items):
            raise ValueError("evidence packet contains cross-campaign/non-typed evidence")
        if len({item.evidence_id for item in self.items}) != len(self.items):
            raise ValueError("evidence packet has duplicate evidence_id")
        object.__setattr__(self, "acquisition_diagnostics", unique_text(self.acquisition_diagnostics, "acquisition_diagnostics", maximum=128, item_max_length=8_000))
        if sum(len(item.content) for item in self.items) > self.budget.max_context_characters:
            raise ValueError("evidence packet exceeds context budget")

    @property
    def fingerprint(self) -> str:
        return stable_sha256(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {"schema_version": 1, "packet_id": self.packet_id, "campaign_id": self.campaign_id,
                "created_at": self.created_at, "budget": self.budget.to_dict(), "items": [item.to_dict() for item in self.items],
                "acquisition_diagnostics": list(self.acquisition_diagnostics)}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ResearchEvidencePacketV1":
        if raw.get("schema_version") != 1:
            raise ValueError("ResearchEvidencePacketV1 schema_version must be 1")
        result = cls(str(raw["packet_id"]), str(raw["campaign_id"]), str(raw["created_at"]),
                     ResearchEvidenceBudgetV1.from_dict(raw["budget"]),
                     tuple(ResearchEvidenceItemV1.from_dict(item) for item in raw.get("items", ())),
                     tuple(raw.get("acquisition_diagnostics", ())))
        expected = raw.get("fingerprint")
        if expected is not None and expected != result.fingerprint:
            raise ValueError("ResearchEvidencePacketV1 fingerprint mismatch")
        return result


    @property
    def provenance_ids(self) -> tuple[str, ...]:
        return tuple(item.evidence_id for item in self.items)


class ResearchEvidenceAcquisitionPort(Protocol):
    def acquire(self, *, campaign_id: str, budget: ResearchEvidenceBudgetV1, context: Any = None) -> ResearchEvidencePacketV1: ...

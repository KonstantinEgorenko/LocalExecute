"""Typed contracts for quality-guided research remediation."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any


class QualityIssueKind(str, Enum):
    INSUFFICIENT_DOCUMENT_INDEPENDENCE = "INSUFFICIENT_DOCUMENT_INDEPENDENCE"
    PARTIAL_ENTAILMENT = "PARTIAL_ENTAILMENT"
    CONTRADICTED_CLAIM = "CONTRADICTED_CLAIM"
    UNSUPPORTED_CLAIM = "UNSUPPORTED_CLAIM"
    MISSING_FRESHNESS = "MISSING_FRESHNESS"
    INSUFFICIENT_INFERENCE_CORROBORATION = "INSUFFICIENT_INFERENCE_CORROBORATION"
    PRIMARY_ARTIFACT_GAP = "PRIMARY_ARTIFACT_GAP"


class RemediationAction(str, Enum):
    KEEP = "KEEP"
    REVISE = "REVISE"
    NARROW = "NARROW"
    DROP = "DROP"


@dataclass(frozen=True, slots=True)
class QualityIssueV1:
    issue_id: str
    claim_id: str
    topic: str
    kind: QualityIssueKind
    severity: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        raw = asdict(self)
        raw["kind"] = self.kind.value
        return raw


@dataclass(frozen=True, slots=True)
class RemediationTargetV1:
    claim_id: str
    topic: str
    claim_text: str
    importance: str
    issue_ids: tuple[str, ...]
    issue_kinds: tuple[QualityIssueKind, ...]
    existing_document_keys: tuple[str, ...]
    research_question: str
    required_topics: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "topic": self.topic,
            "claim_text": self.claim_text,
            "importance": self.importance,
            "issue_ids": list(self.issue_ids),
            "issue_kinds": [item.value for item in self.issue_kinds],
            "existing_document_keys": list(self.existing_document_keys),
            "research_question": self.research_question,
            "required_topics": list(self.required_topics),
        }


@dataclass(frozen=True, slots=True)
class RemediationPlanV1:
    research_id: str
    source_report_fingerprint: str
    issue_count: int
    targets: tuple[RemediationTargetV1, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "research_id": self.research_id,
            "source_report_fingerprint": self.source_report_fingerprint,
            "issue_count": self.issue_count,
            "targets": [item.to_dict() for item in self.targets],
        }


@dataclass(frozen=True, slots=True)
class RemediationDecisionV1:
    claim_id: str
    action: RemediationAction
    revised_text: str
    evidence: tuple[dict[str, str], ...]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "action": self.action.value,
            "revised_text": self.revised_text,
            "evidence": [dict(item) for item in self.evidence],
            "reason": self.reason,
        }

'Convert research quality quality diagnostics into typed remediation issues.'
from __future__ import annotations

from typing import Mapping

from .models import QualityIssueKind, QualityIssueV1


_SEVERITY = {
    QualityIssueKind.CONTRADICTED_CLAIM: "CRITICAL",
    QualityIssueKind.UNSUPPORTED_CLAIM: "CRITICAL",
    QualityIssueKind.PARTIAL_ENTAILMENT: "HIGH",
    QualityIssueKind.PRIMARY_ARTIFACT_GAP: "HIGH",
    QualityIssueKind.MISSING_FRESHNESS: "MEDIUM",
    QualityIssueKind.INSUFFICIENT_DOCUMENT_INDEPENDENCE: "MEDIUM",
    QualityIssueKind.INSUFFICIENT_INFERENCE_CORROBORATION: "MEDIUM",
}


class QualityIssueExtractor:
    def extract(self, quality_report: Mapping) -> tuple[QualityIssueV1, ...]:
        issues: list[QualityIssueV1] = []
        seq = 0
        for item in quality_report.get("claim_quality", []):
            if not isinstance(item, Mapping):
                continue
            claim_id = str(item.get("claim_id", "") or "")
            topic = str(item.get("topic", "") or "")
            for raw_reason in item.get("structural_reasons", []):
                reason = str(raw_reason).strip()
                kind = self._structural_kind(reason)
                if kind is None:
                    continue
                seq += 1
                issues.append(self._issue(seq, claim_id, topic, kind, reason))

            entailment = item.get("entailment")
            if isinstance(entailment, Mapping):
                label = str(entailment.get("label", "") or "").upper()
                reason = str(entailment.get("reason", "") or "")
                kind = {
                    "PARTIAL": QualityIssueKind.PARTIAL_ENTAILMENT,
                    "CONTRADICTED": QualityIssueKind.CONTRADICTED_CLAIM,
                    "UNSUPPORTED": QualityIssueKind.UNSUPPORTED_CLAIM,
                }.get(label)
                if kind is not None:
                    seq += 1
                    issues.append(self._issue(seq, claim_id, topic, kind, reason or label))
        return tuple(issues)

    @staticmethod
    def _structural_kind(reason: str) -> QualityIssueKind | None:
        lowered = reason.casefold()
        if "unique_documents=" in lowered:
            return QualityIssueKind.INSUFFICIENT_DOCUMENT_INDEPENDENCE
        if "freshness timestamp unavailable" in lowered:
            return QualityIssueKind.MISSING_FRESHNESS
        if "inference has fewer than two canonical documents" in lowered:
            return QualityIssueKind.INSUFFICIENT_INFERENCE_CORROBORATION
        if "lacks primary document" in lowered:
            return QualityIssueKind.PRIMARY_ARTIFACT_GAP
        return None

    @staticmethod
    def _issue(seq, claim_id, topic, kind, reason) -> QualityIssueV1:
        return QualityIssueV1(
            issue_id=f"qissue:{seq}",
            claim_id=claim_id,
            topic=topic,
            kind=kind,
            severity=_SEVERITY[kind],
            reason=reason,
        )

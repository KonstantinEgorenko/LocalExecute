"""Deterministic quality-defect -> targeted-research planner."""
from __future__ import annotations

from collections import defaultdict
from typing import Mapping

from harness.research_quality import SourceIdentityResolver

from .issues import QualityIssueExtractor
from .models import QualityIssueKind, RemediationPlanV1, RemediationTargetV1
from .policy import ResearchRemediationPolicy


class RemediationPlanner:
    def __init__(self, policy: ResearchRemediationPolicy) -> None:
        self.policy = policy
        self.identity = SourceIdentityResolver()
        self.extractor = QualityIssueExtractor()

    def build(self, source_report: Mapping, quality_report: Mapping) -> RemediationPlanV1:
        issues = self.extractor.extract(quality_report)
        by_claim: dict[str, list] = defaultdict(list)
        for issue in issues:
            by_claim[issue.claim_id].append(issue)

        claims = {
            str(item.get("claim_id", "")): item
            for item in source_report.get("claims", [])
            if isinstance(item, Mapping)
        }
        evidence = {
            str(item.get("evidence_id", "")): item
            for item in source_report.get("evidence", [])
            if isinstance(item, Mapping)
        }
        sources = {
            str(item.get("source_id", "")): item
            for item in source_report.get("sources", [])
            if isinstance(item, Mapping)
        }

        targets = []
        for claim_id, claim_issues in by_claim.items():
            claim = claims.get(claim_id)
            if not isinstance(claim, Mapping):
                continue
            existing_docs = self._existing_documents(claim, evidence, sources)
            kinds = tuple(dict.fromkeys(issue.kind for issue in claim_issues))
            targets.append(
                RemediationTargetV1(
                    claim_id=claim_id,
                    topic=str(claim.get("topic", "")),
                    claim_text=str(claim.get("text", "")),
                    importance=str(claim.get("importance", "")),
                    issue_ids=tuple(issue.issue_id for issue in claim_issues),
                    issue_kinds=kinds,
                    existing_document_keys=existing_docs,
                    research_question=self._question(claim, kinds, existing_docs),
                    required_topics=(str(claim.get("topic", "")),),
                )
            )

        targets.sort(key=lambda item: self._priority(item.issue_kinds))
        targets = targets[: self.policy.max_claims_per_run]
        return RemediationPlanV1(
            research_id=str(source_report.get("research_id", "")),
            source_report_fingerprint=str(source_report.get("report_fingerprint", "")),
            issue_count=len(issues),
            targets=tuple(targets),
        )

    def _existing_documents(self, claim, evidence, sources) -> tuple[str, ...]:
        docs = set()
        for ref in claim.get("evidence", []):
            if not isinstance(ref, Mapping):
                continue
            ev = evidence.get(str(ref.get("evidence_id", "")))
            if not isinstance(ev, Mapping):
                continue
            sid = str(ev.get("source_id") or ev.get("source") or ev.get("source_ref") or "")
            source = sources.get(sid)
            if isinstance(source, Mapping):
                docs.add(self.identity.resolve(dict(source)).document_key)
        return tuple(sorted(docs))

    @staticmethod
    def _priority(kinds: tuple[QualityIssueKind, ...]) -> tuple[int, str]:
        order = {
            QualityIssueKind.CONTRADICTED_CLAIM: 0,
            QualityIssueKind.UNSUPPORTED_CLAIM: 1,
            QualityIssueKind.PARTIAL_ENTAILMENT: 2,
            QualityIssueKind.PRIMARY_ARTIFACT_GAP: 3,
            QualityIssueKind.MISSING_FRESHNESS: 4,
            QualityIssueKind.INSUFFICIENT_INFERENCE_CORROBORATION: 5,
            QualityIssueKind.INSUFFICIENT_DOCUMENT_INDEPENDENCE: 6,
        }
        return min(order[item] for item in kinds), ",".join(item.value for item in kinds)

    @staticmethod
    def _question(claim: Mapping, kinds, existing_docs: tuple[str, ...]) -> str:
        instructions = []
        if QualityIssueKind.INSUFFICIENT_DOCUMENT_INDEPENDENCE in kinds:
            instructions.append(
                "Find corroborating or refuting evidence in distinct canonical documents; "
                "do not count another fragment of an already cited document as independent."
            )
        if QualityIssueKind.PARTIAL_ENTAILMENT in kinds:
            instructions.append(
                "Verify every material clause separately. If only a narrower statement is "
                "supported, collect evidence for that narrower statement rather than preserving unsupported wording."
            )
        if QualityIssueKind.CONTRADICTED_CLAIM in kinds:
            instructions.append("Prioritize primary evidence that resolves the contradiction; do not force the original claim to survive.")
        if QualityIssueKind.UNSUPPORTED_CLAIM in kinds:
            instructions.append("Search for direct primary support; if none exists, preserve the absence of support rather than inferring the claim.")
        if QualityIssueKind.MISSING_FRESHNESS in kinds:
            instructions.append("For changing metrics, capture current values with retrieval/observation time and primary provenance.")
        if QualityIssueKind.INSUFFICIENT_INFERENCE_CORROBORATION in kinds:
            instructions.append("Gather at least two distinct canonical documents relevant to the inference and separate source facts from adaptation analysis.")
        if QualityIssueKind.PRIMARY_ARTIFACT_GAP in kinds:
            instructions.append("Locate the exact primary artifact needed to support the claim, or establish that the asserted artifact was not verified.")
        existing = ", ".join(existing_docs) if existing_docs else "(none)"
        return (
            f"Quality-remediate this existing research claim without assuming it is correct. "
            f"Claim: {claim.get('text', '')} Topic: {claim.get('topic', '')}. "
            + " ".join(instructions)
            + f" Already cited canonical documents: {existing}. Prefer primary sources. "
            "Explicitly record uncertainty and contradictions. The goal is truthful evidence acquisition, not preservation of the original claim."
        )

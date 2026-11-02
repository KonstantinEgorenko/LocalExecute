'Research quality evaluator over durable external evidence reports.'
from __future__ import annotations

from typing import Mapping

from .entailment import EntailmentJudge
from .identity import SourceIdentityResolver
from .models import (
    ClaimClass,
    ClaimQualityV1,
    EntailmentLabel,
    EntailmentResultV1,
    ResearchQualityReportV1,
)
from .policy import ResearchQualityPolicy


def _text(value: object) -> str:
    return str(value or "").strip()


class ResearchQualityEvaluator:
    def __init__(
        self,
        policy: ResearchQualityPolicy,
        *,
        identity: SourceIdentityResolver | None = None,
        entailment_judge: EntailmentJudge | None = None,
    ) -> None:
        self.policy = policy
        self.identity = identity or SourceIdentityResolver()
        self.entailment_judge = entailment_judge

    def evaluate(self, report: Mapping) -> ResearchQualityReportV1:
        sources_raw = [item for item in report.get("sources", []) if isinstance(item, Mapping)]
        evidence_raw = [item for item in report.get("evidence", []) if isinstance(item, Mapping)]
        claims_raw = [item for item in report.get("claims", []) if isinstance(item, Mapping)]

        source_by_id = {
            _text(source.get("source_id")): source
            for source in sources_raw
            if _text(source.get("source_id"))
        }
        identities = {
            item.source_id: item
            for item in (self.identity.resolve(dict(source)) for source in sources_raw)
            if item.source_id
        }
        evidence_by_id = {
            _text(item.get("evidence_id")): item
            for item in evidence_raw
            if _text(item.get("evidence_id"))
        }

        claim_quality = []
        diagnostics: list[str] = []
        for claim in claims_raw:
            quality = self._evaluate_claim(
                claim, evidence_by_id, identities, source_by_id
            )
            claim_quality.append(quality)
            if quality.fragmented_single_document:
                diagnostics.append(
                    f"{quality.claim_id}: evidence multiplicity collapses to one canonical document"
                )
            if quality.importance == "IMPORTANT" and not quality.structural_sufficient:
                diagnostics.append(
                    f"{quality.claim_id}: IMPORTANT structural gate failed: "
                    + "; ".join(quality.structural_reasons)
                )
            if quality.entailment.label in {
                EntailmentLabel.PARTIAL,
                EntailmentLabel.CONTRADICTED,
                EntailmentLabel.UNSUPPORTED,
            }:
                diagnostics.append(
                    f"{quality.claim_id}: entailment={quality.entailment.label.value}"
                )

        all_ids = list(identities.values())
        unique_docs = {item.document_key for item in all_ids}
        unique_families = {item.source_family for item in all_ids}
        unique_publishers = {item.publisher_key for item in all_ids}
        unique_domains = {item.domain for item in all_ids if item.domain}
        evidence_count = len(evidence_raw)
        amplification = evidence_count / max(len(unique_docs), 1)

        important = [item for item in claim_quality if item.importance == "IMPORTANT"]
        structurally_bad = [item for item in important if not item.structural_sufficient]
        evaluated = [
            item for item in claim_quality
            if item.entailment.label != EntailmentLabel.NOT_EVALUATED
        ]
        failed_entailment = [
            item for item in evaluated
            if item.entailment.label in {
                EntailmentLabel.PARTIAL,
                EntailmentLabel.CONTRADICTED,
                EntailmentLabel.UNSUPPORTED,
            }
        ]
        unsupported = [
            item for item in evaluated
            if item.entailment.label in {
                EntailmentLabel.CONTRADICTED,
                EntailmentLabel.UNSUPPORTED,
            }
        ]
        unsupported_rate = None if not evaluated else len(unsupported) / len(evaluated)

        return ResearchQualityReportV1(
            research_id=_text(report.get("research_id")),
            source_report_fingerprint=_text(report.get("report_fingerprint")),
            source_outcome=_text(report.get("outcome")),
            source_failure_reason=_text(report.get("failure_reason")),
            sources=len(sources_raw),
            evidence=evidence_count,
            claims=len(claims_raw),
            unique_source_ids=len(identities),
            unique_documents=len(unique_docs),
            unique_source_families=len(unique_families),
            unique_publishers=len(unique_publishers),
            unique_domains=len(unique_domains),
            evidence_amplification_ratio=amplification,
            fragmented_claims=sum(item.fragmented_single_document for item in claim_quality),
            important_claims=len(important),
            structurally_insufficient_important_claims=len(structurally_bad),
            entailment_evaluated_claims=len(evaluated),
            entailment_failed_claims=len(failed_entailment),
            unsupported_claim_rate=unsupported_rate,
            quality_sufficient_claims=sum(item.quality_sufficient for item in claim_quality),
            claim_quality=tuple(claim_quality),
            diagnostics=tuple(diagnostics),
        )

    def _evaluate_claim(
        self, claim, evidence_by_id, identities, source_by_id
    ) -> ClaimQualityV1:
        refs = [item for item in claim.get("evidence", []) if isinstance(item, Mapping)]
        evidence_items = []
        cited_sources = []
        source_ids = set()
        documents = set()
        families = set()
        publishers = set()
        domains = set()
        primary_documents = set()
        support = contradiction = 0

        for ref in refs:
            relation = _text(ref.get("relation")).upper()
            if relation == "SUPPORTS":
                support += 1
            elif relation == "CONTRADICTS":
                contradiction += 1
            eid = _text(ref.get("evidence_id"))
            evidence = evidence_by_id.get(eid)
            if evidence is None:
                continue
            evidence_items.append(evidence)
            source_id = _text(
                evidence.get("source_id")
                or evidence.get("source")
                or evidence.get("source_ref")
            )
            if not source_id:
                continue
            source_ids.add(source_id)
            source_raw = source_by_id.get(source_id)
            if source_raw is not None:
                cited_sources.append(source_raw)
            identity = identities.get(source_id)
            if identity is None:
                continue
            documents.add(identity.document_key)
            families.add(identity.source_family)
            publishers.add(identity.publisher_key)
            if identity.domain:
                domains.add(identity.domain)
            if identity.tier == "PRIMARY":
                primary_documents.add(identity.document_key)

        importance = _text(claim.get("importance")).upper()
        topic = _text(claim.get("topic"))
        claim_class = self.policy.claim_class_for(topic)
        structural_reasons: list[str] = []

        if not refs:
            structural_reasons.append("claim has no evidence references")
        if support == 0:
            structural_reasons.append("claim has no SUPPORTS evidence")

        # Phase 19 evaluates quality without mutating frozen Phase 18 stopping policy.
        if importance == "IMPORTANT":
            if len(documents) < self.policy.important_min_unique_documents:
                structural_reasons.append(
                    f"unique_documents={len(documents)} < "
                    f"{self.policy.important_min_unique_documents}"
                )
            if len(families) < self.policy.important_min_unique_source_families:
                structural_reasons.append(
                    f"unique_source_families={len(families)} < "
                    f"{self.policy.important_min_unique_source_families}"
                )
        if importance == "CRITICAL" and len(primary_documents) < self.policy.critical_min_primary_documents:
            structural_reasons.append(
                f"primary_documents={len(primary_documents)} < "
                f"{self.policy.critical_min_primary_documents}"
            )

        # Claim-class diagnostics are deliberately evaluation-only.
        if claim_class in {ClaimClass.ARCHITECTURE, ClaimClass.LICENSE, ClaimClass.SELF_DESCRIPTION}:
            if not primary_documents:
                structural_reasons.append(f"{claim_class.value} lacks PRIMARY document")
        if claim_class == ClaimClass.CURRENT_METRIC:
            def freshness(item):
                metadata = item.get("metadata") if isinstance(item, Mapping) else None
                metadata = metadata if isinstance(metadata, Mapping) else {}
                return _text(
                    item.get("retrieved_at")
                    or item.get("fetched_at")
                    or item.get("observed_at")
                    or metadata.get("retrieved_at")
                    or metadata.get("fetched_at")
                    or metadata.get("observed_at")
                )
            has_freshness = any(freshness(item) for item in evidence_items) or any(
                freshness(item) for item in cited_sources
            )
            if not has_freshness:
                structural_reasons.append("CURRENT_METRIC freshness timestamp unavailable")
        if claim_class == ClaimClass.INFERENCE and len(documents) < 2:
            structural_reasons.append("INFERENCE has fewer than two canonical documents")

        entailment = EntailmentResultV1(
            EntailmentLabel.NOT_EVALUATED, 0.0, "entailment judge not configured"
        )
        if self.entailment_judge is not None:
            entailment = self.entailment_judge.judge(
                claim=claim,
                evidence=evidence_items,
            )

        structural_ok = not structural_reasons
        entailment_ok = entailment.label in {
            EntailmentLabel.ENTAILED,
            EntailmentLabel.NOT_EVALUATED,
        }
        quality_ok = structural_ok and entailment_ok
        ratio = len(refs) / max(len(documents), 1)
        return ClaimQualityV1(
            claim_id=_text(claim.get("claim_id")),
            topic=topic,
            importance=importance,
            claim_class=claim_class,
            evidence_refs=len(refs),
            supporting_evidence_refs=support,
            contradicting_evidence_refs=contradiction,
            unique_source_ids=len(source_ids),
            unique_documents=len(documents),
            unique_source_families=len(families),
            unique_publishers=len(publishers),
            unique_domains=len(domains),
            primary_documents=len(primary_documents),
            evidence_amplification_ratio=ratio,
            fragmented_single_document=len(refs) > 1 and len(documents) == 1,
            structural_sufficient=structural_ok,
            structural_reasons=tuple(structural_reasons),
            entailment=entailment,
            quality_sufficient=quality_ok,
        )

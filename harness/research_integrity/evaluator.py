'Supplemental integrity evaluation; frozen research quality remains final authority.'
from __future__ import annotations
from harness.compatibility.configuration import legacy_keywords

from datetime import datetime, timezone
from typing import Mapping

from harness.research_quality import ResearchQualityEvaluator
from harness.research_quality.models import ClaimClass

from .freshness import FreshnessEvaluatorV2
from .identity import CanonicalDocumentResolverV2
from .policy import EvidenceIntegrityPolicy
from .semantic import SemanticClauseIntegrityGuard


class IntegrityQualityEvaluator:
    @legacy_keywords({'phase19_policy': 'quality_policy', 'phase20q_policy': 'integrity_policy'})
    def __init__(self, quality_policy, integrity_policy: EvidenceIntegrityPolicy, *, entailment_judge=None) -> None:
        self.research_quality = ResearchQualityEvaluator(quality_policy, entailment_judge=entailment_judge)
        self.quality_policy = quality_policy
        self.policy = integrity_policy
        self.docs = CanonicalDocumentResolverV2()
        self.freshness = FreshnessEvaluatorV2(integrity_policy.current_metric_freshness_hours)
        self.semantic = SemanticClauseIntegrityGuard()

    def evaluate(self, report: Mapping) -> dict:
        frozen = self.research_quality.evaluate(report).to_dict()
        evidence = {str(x.get("evidence_id", "")): x for x in report.get("evidence", []) if isinstance(x, Mapping)}
        sources = {str(x.get("source_id", "")): x for x in report.get("sources", []) if isinstance(x, Mapping)}
        frozen_claim_quality = {
            str(item.get("claim_id", "")): item
            for item in frozen.get("claim_quality", [])
            if isinstance(item, Mapping)
        }
        integrity = []
        for claim in report.get("claims", []):
            if not isinstance(claim, Mapping):
                continue
            claim_id = str(claim.get("claim_id", ""))
            claim_class = self.quality_policy.claim_class_for(str(claim.get("topic", "")))
            reasons = []
            frozen_item = frozen_claim_quality.get(claim_id, {})
            if isinstance(frozen_item, Mapping) and not bool(frozen_item.get("structural_sufficient", True)):
                for reason in frozen_item.get("structural_reasons", []):
                    reasons.append(f"frozen_phase19_structural: {reason}")
            if claim_class is ClaimClass.CURRENT_METRIC:
                fresh = False
                for ref in claim.get("evidence", []):
                    if not isinstance(ref, Mapping):
                        continue
                    ev = evidence.get(str(ref.get("evidence_id", "")))
                    if not isinstance(ev, Mapping):
                        continue
                    status = self.freshness.evaluate(ev)
                    if status.value == "FRESH":
                        fresh = True
                        break
                    sid = str(ev.get("source_id", ""))
                    source = sources.get(sid)
                    if isinstance(source, Mapping) and self.freshness.evaluate(source).value == "FRESH":
                        fresh = True
                        break
                if not fresh:
                    reasons.append("CURRENT_METRIC has no fresh observation within Phase20Q age policy")
            selected_evidence = []
            for ref in claim.get("evidence", []):
                if not isinstance(ref, Mapping):
                    continue
                ev = evidence.get(str(ref.get("evidence_id", "")))
                if isinstance(ev, Mapping):
                    selected_evidence.append(ev)
            semantic = self.semantic.evaluate(
                claim=claim,
                claim_class=claim_class,
                evidence=selected_evidence,
            )
            for reason in semantic.reasons:
                reasons.append(f"semantic_clause: {reason}")

            integrity.append({
                "claim_id": claim_id,
                "claim_class": claim_class.value,
                "integrity_sufficient": not reasons,
                "reasons": reasons,
            })
        return {
            "schema_version": 1,
            "frozen_phase19": frozen,
            "phase20q_integrity": integrity,
            "integrity_sufficient_claims": sum(x["integrity_sufficient"] for x in integrity),
        }

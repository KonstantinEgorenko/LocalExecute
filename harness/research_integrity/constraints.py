from __future__ import annotations

from typing import Mapping, Sequence

from harness.research_quality.models import ClaimClass
from harness.research_remediation.models import QualityIssueKind

from .freshness import FreshnessEvaluatorV2
from .identity import CanonicalDocumentResolverV2, EntityBindingResolver, quality_document_key
from .models import (
    DefectClosureResultV1,
    EligibilityDecisionV1,
    EntityBindingStatus,
    FreshnessStatus,
    RemediationConstraintV2,
    TargetEntityV1,
)
from .policy import EvidenceIntegrityPolicy



def _normalize_existing_document_key(value: str) -> str:
    text = str(value or "").strip()
    if text.startswith("github:"):
        return "github-repo:" + text[len("github:"):]
    return text

def build_constraint(target, entity: TargetEntityV1, claim_class: ClaimClass, policy: EvidenceIntegrityPolicy) -> RemediationConstraintV2:
    kinds = tuple(x.value if hasattr(x, "value") else str(x) for x in target.issue_kinds)
    need_new = any(x in {
        QualityIssueKind.INSUFFICIENT_DOCUMENT_INDEPENDENCE.value,
        QualityIssueKind.INSUFFICIENT_INFERENCE_CORROBORATION.value,
    } for x in kinds)
    return RemediationConstraintV2(
        claim_id=target.claim_id,
        target_entity_key=entity.canonical_key,
        issue_kinds=kinds,
        excluded_document_keys=tuple(_normalize_existing_document_key(x) for x in target.existing_document_keys),
        required_new_documents=1 if (need_new and policy.require_new_document_for_document_independence) else 0,
        required_total_documents=2 if need_new else 0,
        require_exact_entity=claim_class.value in policy.exact_repository_required_for,
        require_fresh_primary=(claim_class is ClaimClass.CURRENT_METRIC and policy.require_fresh_primary_for_current_metric),
    )


class EvidenceEligibilityGuard:
    def __init__(self, policy: EvidenceIntegrityPolicy) -> None:
        self.policy = policy
        self.binding = EntityBindingResolver()
        self.documents = CanonicalDocumentResolverV2()
        self.freshness = FreshnessEvaluatorV2(policy.current_metric_freshness_hours)

    def filter(self, *, target_entity: TargetEntityV1, claim_class: ClaimClass, evidence: Sequence[Mapping], sources: Mapping[str, Mapping]) -> tuple[list[dict], list[EligibilityDecisionV1]]:
        accepted: list[dict] = []
        decisions: list[EligibilityDecisionV1] = []
        for item in evidence:
            eid = str(item.get("evidence_id", ""))
            sid = str(item.get("source_id") or item.get("source") or item.get("source_ref") or "")
            source = sources.get(sid, {})
            binding = self.binding.bind(source, target_entity)
            document = self.documents.resolve(source, target_entity)
            reasons: list[str] = []
            if self.policy.reject_other_github_repositories and binding.status is EntityBindingStatus.MISMATCH:
                reasons.append("entity_mismatch")
            exact_required = claim_class.value in self.policy.exact_repository_required_for
            if exact_required and binding.status not in {EntityBindingStatus.EXACT, EntityBindingStatus.OFFICIAL_CHILD}:
                reasons.append("exact_target_repository_required")
            if claim_class is ClaimClass.LICENSE and document.artifact_kind not in {"LICENSE", "LICENSE_POLICY", "README", "REPOSITORY_METADATA"}:
                reasons.append("license_primary_artifact_required")
            if claim_class is ClaimClass.CURRENT_METRIC and document.artifact_kind != "REPOSITORY_METADATA":
                reasons.append("live_repository_metadata_required")
            required_fresh = claim_class is ClaimClass.CURRENT_METRIC
            freshness = self.freshness.evaluate(item, required=required_fresh)
            if required_fresh and freshness is not FreshnessStatus.FRESH:
                # Source-level acquisition timestamps can also establish observation time.
                freshness = self.freshness.evaluate(source, required=True)
            if required_fresh and freshness is not FreshnessStatus.FRESH:
                reasons.append(f"freshness_{freshness.value.casefold()}")
            eligible = not reasons
            annotated = dict(item)
            annotated["phase20q_document_key"] = document.document_key
            annotated["phase20q_phase19_document_key"] = quality_document_key(source)
            annotated["phase20q_entity_binding"] = binding.status.value
            annotated["phase20q_freshness"] = freshness.value
            annotated["phase20q_source_tier"] = str(source.get("tier", ""))
            if eligible:
                accepted.append(annotated)
            decisions.append(EligibilityDecisionV1(eid, eligible, tuple(reasons), binding, document, freshness, str(source.get("tier", "")).upper()))
        return accepted, decisions


class DefectClosureGate:
    def __init__(self, policy: EvidenceIntegrityPolicy) -> None:
        self.policy = policy

    def evaluate(
        self,
        *,
        constraint: RemediationConstraintV2,
        selected_evidence_ids: Sequence[str],
        eligibility: Sequence[EligibilityDecisionV1],
        sources: Mapping[str, Mapping] | None = None,
        evidence: Sequence[Mapping] | None = None,
    ) -> DefectClosureResultV1:
        by_id = {x.evidence_id: x for x in eligibility}
        selected = [by_id[eid] for eid in selected_evidence_ids if eid in by_id]
        reasons: list[str] = []
        if any(not x.eligible for x in selected):
            reasons.append("decision selected ineligible evidence")

        # Closure must predict the frozen Phase19 quality judge, not merely
        # Phase20Q's richer document identity.  A source can be distinct in V2
        # (for example @metadata) while frozen Phase19 intentionally collapses
        # it to the repository-root document.  Count the exact namespace that
        # final research quality re-evaluation will use whenever source provenance is
        # available.
        evidence_by_id = {
            str(item.get("evidence_id", "")): item
            for item in (evidence or ()) if isinstance(item, Mapping)
        }
        source_map = sources or {}

        def _closure_document(item: EligibilityDecisionV1) -> str:
            ev = evidence_by_id.get(item.evidence_id, {})
            sid = str(ev.get("source_id") or ev.get("source") or ev.get("source_ref") or "")
            source = source_map.get(sid, {}) if sid else {}
            if isinstance(source, Mapping) and source:
                return quality_document_key(source)
            return item.document.document_key

        docs = {_closure_document(x) for x in selected if x.eligible}
        existing = set(constraint.excluded_document_keys)
        new_docs = docs - existing
        if constraint.required_new_documents and len(new_docs) < constraint.required_new_documents:
            reasons.append(f"new_documents={len(new_docs)} < {constraint.required_new_documents}")
        if constraint.required_total_documents and len(docs) < constraint.required_total_documents:
            reasons.append(f"selected_documents={len(docs)} < {constraint.required_total_documents}")
        if constraint.require_exact_entity and not all(
            x.binding.status in {EntityBindingStatus.EXACT, EntityBindingStatus.OFFICIAL_CHILD}
            for x in selected if x.eligible
        ):
            reasons.append("exact entity binding requirement not satisfied")
        if constraint.require_fresh_primary:
            fresh = [x for x in selected if x.eligible and x.freshness is FreshnessStatus.FRESH and x.source_tier == "PRIMARY"]
            if not fresh:
                reasons.append("fresh primary observation not selected")
        return DefectClosureResultV1(not reasons, tuple(reasons), tuple(sorted(docs)), tuple(sorted(new_docs)))

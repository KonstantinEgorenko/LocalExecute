'research integrity evidence-filtered revision with deterministic defect closure.'
from __future__ import annotations

from dataclasses import replace
from typing import Mapping, Sequence

from harness.research_quality.models import ClaimClass
from harness.research_remediation.models import RemediationAction, RemediationDecisionV1, RemediationTargetV1
from harness.research_remediation.revision import RevisionJudge

from .constraints import DefectClosureGate, EvidenceEligibilityGuard
from .identity import quality_document_key
from .models import RemediationConstraintV2, TargetEntityV1
from .policy import EvidenceIntegrityPolicy
from .relevance import BM25EvidenceSelector


class IntegrityRevisionCoordinator:
    def __init__(self, policy: EvidenceIntegrityPolicy, inner: RevisionJudge) -> None:
        self.policy = policy
        self.inner = inner
        self.guard = EvidenceEligibilityGuard(policy)
        self.closure = DefectClosureGate(policy)
        self.rank = BM25EvidenceSelector(k1=policy.bm25_k1, b=policy.bm25_b)
        self.last_diagnostics: dict = {}

    def decide(self, *, target, target_entity: TargetEntityV1, claim_class: ClaimClass, constraint: RemediationConstraintV2, original_claim: Mapping, evidence: Sequence[Mapping], sources: Mapping[str, Mapping]) -> RemediationDecisionV1:
        # Correctness filtering must see the complete bounded evidence pool.
        # V1/V1.2 truncated before eligibility, while exact research integrity primary
        # artifacts are appended after base + targeted evidence.  On real
        # LoopX runs this starved LICENSE / LICENSE-MIT / licensing.md out of
        # the first 80 candidates and left only REPOSITORY_ROOT evidence.
        accepted, decisions = self.guard.filter(
            target_entity=target_entity,
            claim_class=claim_class,
            evidence=evidence,
            sources=sources,
        )
        query = f"{target.topic} {target.claim_text} {target.research_question}"
        decision_by_id = {item.evidence_id: item for item in decisions if item.eligible}

        def _source_for(item: Mapping) -> Mapping:
            sid = str(item.get("source_id") or item.get("source") or item.get("source_ref") or "")
            return sources.get(sid, {})

        def _quality_document_for(item: Mapping) -> str:
            annotated = str(item.get("phase20q_phase19_document_key") or "")
            if annotated:
                return annotated
            source = _source_for(item)
            if source:
                return quality_document_key(source)
            return str(item.get("phase20q_document_key") or item.get("evidence_id") or "")

        diversity_required = bool(
            constraint.required_total_documents > 1
            or constraint.required_new_documents > 0
        )

        def _document_unique(items: Sequence[Mapping], *, seen: set[str] | None = None) -> list[Mapping]:
            used = set(seen or ())
            output: list[Mapping] = []
            for item in items:
                key = _quality_document_for(item)
                if key in used:
                    continue
                used.add(key)
                output.append(item)
            return output

        def _is_required_primary(item: Mapping) -> bool:
            decision_item = decision_by_id.get(str(item.get("evidence_id", "")))
            if decision_item is None:
                return False
            source = _source_for(item)
            provider = str(source.get("provider_id", ""))
            if provider != "phase20q-github-primary":
                return False
            kind = decision_item.document.artifact_kind
            if claim_class is ClaimClass.LICENSE:
                return kind in {"LICENSE", "LICENSE_POLICY"}
            if claim_class is ClaimClass.CURRENT_METRIC:
                return kind == "REPOSITORY_METADATA"
            return True

        # Reserve model-facing capacity for exact target-bound primary artifacts
        # before BM25 fills the remainder.  For document-independence defects,
        # admission is additionally deduplicated in the *frozen Phase19*
        # canonical-document namespace.  This prevents research integrity-rich identities
        # from predicting closure that research quality later collapses back to one doc.
        required_primary = [item for item in accepted if _is_required_primary(item)]
        if len(required_primary) > self.policy.max_relevance_selected:
            required_primary = self.rank.select(
                query, required_primary, limit=self.policy.max_relevance_selected
            )
        if diversity_required:
            required_primary = _document_unique(required_primary)
        pinned_ids = {str(item.get("evidence_id", "")) for item in required_primary}

        remaining = [item for item in accepted if str(item.get("evidence_id", "")) not in pinned_ids]
        candidate_slots = max(0, self.policy.max_relevance_candidates - len(required_primary))

        if diversity_required and candidate_slots:
            # Pull enough BM25 candidates to discover distinct canonical
            # documents, expanding the window if high-ranked fragments are
            # duplicates of the same document.  The final model-facing set has
            # at most one representative per frozen Phase19 document.
            pool_limit = min(
                len(remaining),
                max(candidate_slots, min(len(remaining), candidate_slots * 2)),
            )
            ranked_pool: list[Mapping] = []
            diverse_remaining: list[Mapping] = []
            seen_docs = {_quality_document_for(item) for item in required_primary}
            while pool_limit > 0:
                ranked_pool = self.rank.select(query, remaining, limit=pool_limit)
                diverse_remaining = _document_unique(ranked_pool, seen=seen_docs)
                if len(diverse_remaining) >= candidate_slots or pool_limit >= len(remaining):
                    break
                pool_limit = min(len(remaining), max(pool_limit + 1, pool_limit * 2))
            ranked_remaining = diverse_remaining[:candidate_slots]
        else:
            ranked_remaining = self.rank.select(query, remaining, limit=candidate_slots) if candidate_slots else []

        candidates = required_primary + ranked_remaining

        if diversity_required:
            # One representative per canonical document means any two cited
            # SUPPORTS evidence references necessarily come from two documents
            # under the same identity semantics frozen Phase19 will later use.
            selected = candidates[: self.policy.max_relevance_selected]
        else:
            selected = list(required_primary)
            selected_slots = max(0, self.policy.max_relevance_selected - len(selected))
            if selected_slots:
                selected.extend(self.rank.select(query, ranked_remaining, limit=selected_slots))

        # Phase19Q's frozen prompt exposes both evidence IDs and existing
        # canonical document keys.  Those are different namespaces, but a model
        # can mistake a document key (for example
        # ``github:owner/repo:@repo-root``) for a citable evidence ID.  research integrity
        # keeps document keys for deterministic defect-closure logic, but removes
        # them from the model-facing revision target.  It also hides source IDs so
        # the only citable identifier visible to the model is ``evidence_id``.
        revision_target = target
        if isinstance(target, RemediationTargetV1):
            revision_target = replace(target, existing_document_keys=())
        model_evidence = []
        document_groups: dict[str, str] = {}
        for item in selected:
            prepared = {**dict(item), "source_id": ""}
            if diversity_required:
                doc_key = _quality_document_for(item)
                label = document_groups.setdefault(doc_key, f"DOC{len(document_groups) + 1}")
                text = str(prepared.get("text") or prepared.get("excerpt") or prepared.get("content") or "")
                prepared["text"] = f"[PHASE20Q_CANONICAL_DOCUMENT_GROUP={label}]\n{text}"
            model_evidence.append(prepared)
        decision = self.inner.decide(
            target=revision_target,
            original_claim=original_claim,
            evidence=model_evidence,
        )
        refs = [x.get("evidence_id", "") for x in decision.evidence if x.get("relation") == "SUPPORTS"]
        closure = self.closure.evaluate(
            constraint=constraint,
            selected_evidence_ids=refs,
            eligibility=decisions,
            sources=sources,
            evidence=evidence,
        )
        self.last_diagnostics = {
            "target_entity": target_entity.to_dict(),
            "constraint": constraint.to_dict(),
            "evidence_pool": len(evidence),
            "eligible_evidence": len(accepted),
            "required_primary_reserved": len(required_primary),
            "relevance_candidates": len(candidates),
            "revision_evidence": len(selected),
            "phase19_document_diversity_required": diversity_required,
            "revision_unique_phase19_documents": len({_quality_document_for(item) for item in selected}),
            "revision_document_groups": dict(document_groups),
            "revision_prompt_identifier_policy": {
                "citable_namespace": "evidence_id_only",
                "existing_document_keys_exposed": False,
                "source_ids_exposed": False,
            },
            "eligibility": [x.to_dict() for x in decisions],
            "closure": closure.to_dict(),
            "model_decision": decision.to_dict(),
        }
        if closure.closed or decision.action is RemediationAction.DROP:
            return decision
        if not self.policy.fail_closed_on_unclosed_defect:
            raise RuntimeError("Phase20Q defect closure failed: " + "; ".join(closure.reasons))
        return RemediationDecisionV1(
            claim_id=target.claim_id,
            action=RemediationAction.DROP,
            revised_text="",
            evidence=(),
            reason="Phase20Q fail-closed: remediation did not close structural defect: " + "; ".join(closure.reasons),
        )

"""Evidence normalization and claim-graph sufficiency gates."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Iterable, Sequence
from urllib.parse import urlsplit

from .models import (
    ClaimImportance,
    EvidenceItemV1,
    EvidenceRelation,
    FetchedSourceV1,
    ResearchWorkflowPolicy,
    ResearchClaimV1,
    ResearchCoverageV1,
    ResearchRequestV1,
    SourceTier,
)
from .provenance import source_domain


def normalize_evidence(
    sources: Sequence[FetchedSourceV1],
    request: ResearchRequestV1,
    policy: ResearchWorkflowPolicy,
) -> tuple[EvidenceItemV1, ...]:
    """Produce bounded, source-addressable evidence excerpts.

    The normalizer is intentionally deterministic. It never paraphrases source
    text; semantic claim creation belongs to the synthesis port.
    """
    items: list[EvidenceItemV1] = []
    for source in sources:
        text = source.text.strip()
        if not text:
            continue
        # Split by paragraphs first; fall back to fixed-size windows for dense text.
        chunks = [chunk.strip() for chunk in text.split("\n\n") if chunk.strip()]
        if len(chunks) <= 1 and len(text) > policy.max_excerpt_chars:
            chunks = [text[i : i + policy.max_excerpt_chars] for i in range(0, len(text), policy.max_excerpt_chars)]
        for index, chunk in enumerate(chunks):
            excerpt = chunk[: policy.max_excerpt_chars]
            topic = _best_topic(excerpt, request.required_topics)
            digest = hashlib.sha256(f"{source.source_id}\0{index}\0{excerpt}".encode("utf-8")).hexdigest()[:24]
            items.append(EvidenceItemV1(f"ev:{digest}", source.source_id, f"chunk:{index}", excerpt, topic))
            if len(items) >= policy.max_evidence_items:
                return tuple(items)
    return tuple(items)


def _best_topic(text: str, topics: Sequence[str]) -> str:
    folded = text.casefold()
    scored = sorted(((folded.count(topic.casefold()), topic) for topic in topics), reverse=True)
    if scored and scored[0][0] > 0:
        return scored[0][1]
    return topics[0]


@dataclass(frozen=True, slots=True)
class ClaimGraph:
    sources: tuple[FetchedSourceV1, ...]
    evidence: tuple[EvidenceItemV1, ...]
    claims: tuple[ResearchClaimV1, ...]

    def __post_init__(self) -> None:
        source_ids = {item.source_id for item in self.sources}
        if len(source_ids) != len(self.sources):
            raise ValueError("source_id values must be unique")
        evidence_ids = {item.evidence_id for item in self.evidence}
        if len(evidence_ids) != len(self.evidence):
            raise ValueError("evidence_id values must be unique")
        if any(item.source_id not in source_ids for item in self.evidence):
            raise ValueError("evidence references unknown source")
        claim_ids = {item.claim_id for item in self.claims}
        if len(claim_ids) != len(self.claims):
            raise ValueError("claim_id values must be unique")
        for claim in self.claims:
            unknown = {ref.evidence_id for ref in claim.evidence} - evidence_ids
            if unknown:
                raise ValueError(f"claim={claim.claim_id} references unknown evidence={sorted(unknown)}")

    def coverage(self, request: ResearchRequestV1, policy: ResearchWorkflowPolicy) -> ResearchCoverageV1:
        evidence_by_id = {item.evidence_id: item for item in self.evidence}
        source_by_id = {item.source_id: item for item in self.sources}
        covered_topics: set[str] = set()
        important = 0
        insufficient_important = 0
        critical_no_primary = 0
        unresolved_critical = 0

        for claim in self.claims:
            support_refs = [ref for ref in claim.evidence if ref.relation is EvidenceRelation.SUPPORTS]
            contradict_refs = [ref for ref in claim.evidence if ref.relation is EvidenceRelation.CONTRADICTS]
            support_source_ids = {evidence_by_id[ref.evidence_id].source_id for ref in support_refs}
            contradict_source_ids = {evidence_by_id[ref.evidence_id].source_id for ref in contradict_refs}
            if support_source_ids:
                covered_topics.add(claim.topic)
            if claim.importance in {ClaimImportance.IMPORTANT, ClaimImportance.CRITICAL}:
                important += 1
                if len(support_source_ids) < policy.min_sources_per_important_claim:
                    insufficient_important += 1
            if claim.importance is ClaimImportance.CRITICAL:
                if policy.require_primary_for_critical_claims and not any(
                    source_by_id[source_id].tier is SourceTier.PRIMARY for source_id in support_source_ids
                ):
                    critical_no_primary += 1
                # A critical contradiction is unresolved unless support includes a
                # primary source and strictly more distinct sources than contradiction.
                if contradict_source_ids:
                    primary_support = any(source_by_id[source_id].tier is SourceTier.PRIMARY for source_id in support_source_ids)
                    if not primary_support or len(support_source_ids) <= len(contradict_source_ids):
                        unresolved_critical += 1

        required_folded = {topic.casefold() for topic in request.required_topics}
        covered_count = len({topic.casefold() for topic in covered_topics} & required_folded)
        domains = {source_domain(item.canonical_url) for item in self.sources if source_domain(item.canonical_url)}
        sufficient = (
            covered_count == len(request.required_topics)
            and len(domains) >= policy.min_source_domains
            and insufficient_important == 0
            and critical_no_primary == 0
            and (
                unresolved_critical == 0
                if policy.require_zero_unresolved_critical_contradictions
                else True
            )
        )
        return ResearchCoverageV1(
            len(request.required_topics), covered_count, len(domains), important,
            insufficient_important, critical_no_primary, unresolved_critical, sufficient,
        )

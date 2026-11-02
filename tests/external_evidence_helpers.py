from __future__ import annotations

from pathlib import Path
from typing import Sequence

from harness.external_evidence.models import (
    ClaimEvidenceRefV1,
    ClaimImportance,
    EvidenceItemV1,
    EvidenceRelation,
    FetchedSourceV1,
    ResearchWorkflowPolicy,
    ResearchChannel,
    ResearchClaimV1,
    ResearchQueryV1,
    ResearchRequestV1,
    SearchHitV1,
    SourceKind,
    SourceTier,
    SynthesisProposalV1,
)


def policy() -> ResearchWorkflowPolicy:
    return ResearchWorkflowPolicy(
        3, 8, 8, 48, 524288, 120000, 128, 6000, 48, 2, 2,
        True, True, True, True, 4, 20, 3, 1.0,
        True, "PHASE18_SEARXNG_BASE_URL", True,
        True, "GITHUB_TOKEN", "https://api.github.com",
    )


def request(research_id: str = "research-1") -> ResearchRequestV1:
    return ResearchRequestV1(
        research_id,
        "Assess concurrency and licensing characteristics.",
        ("concurrency", "licensing"),
        (
            ResearchQueryV1("q-concurrency", "project concurrency official docs", ResearchChannel.WEB),
            ResearchQueryV1("q-licensing", "project licensing repository", ResearchChannel.GITHUB_REPOSITORIES),
        ),
    )


class FixtureProvider:
    def __init__(self, provider_id: str, hits_by_query: dict[str, list[SearchHitV1]], sources_by_url: dict[str, FetchedSourceV1]) -> None:
        self.provider_id = provider_id
        self.hits_by_query = hits_by_query
        self.sources_by_url = sources_by_url
        self.search_calls: list[str] = []
        self.fetch_calls: list[str] = []

    def search(self, query: ResearchQueryV1, *, limit: int):
        self.search_calls.append(query.query_id)
        return self.hits_by_query.get(query.query_id, [])[:limit]

    def fetch(self, hit: SearchHitV1) -> FetchedSourceV1:
        self.fetch_calls.append(hit.url)
        return self.sources_by_url[hit.url]


def fixture_providers() -> tuple[FixtureProvider, FixtureProvider]:
    web_a = "https://docs.python.org/3/library/asyncio.html"
    web_b = "https://example.org/concurrency-analysis"
    gh = "https://github.com/acme/project"
    gh2 = "https://github.com/acme/project-docs"
    web_hits = {
        "q-concurrency": [
            SearchHitV1("web-fixture", "q-concurrency", 1, "Async official", web_a, "", SourceKind.OFFICIAL_DOC),
            SearchHitV1("web-fixture", "q-concurrency", 2, "Independent analysis", web_b, "", SourceKind.WEB_PAGE),
        ],
        "q-followup-licensing": [
            SearchHitV1("web-fixture", "q-followup-licensing", 1, "License docs", gh2, "", SourceKind.GITHUB_REPOSITORY),
        ],
    }
    web_sources = {
        web_a: FetchedSourceV1("web-fixture", web_a, "Async official", SourceKind.OFFICIAL_DOC, SourceTier.PRIMARY, "text/plain", "Concurrency uses cooperative scheduling.\n\nLicensing is documented separately.", 80),
        web_b: FetchedSourceV1("web-fixture", web_b, "Independent analysis", SourceKind.WEB_PAGE, SourceTier.SECONDARY, "text/plain", "Concurrency uses cooperative scheduling according to tests.", 70),
        gh2: FetchedSourceV1("web-fixture", gh2, "Project docs", SourceKind.GITHUB_REPOSITORY, SourceTier.PRIMARY, "text/plain", "Licensing uses the Apache-2.0 license.", 42),
    }
    gh_hits = {
        "q-licensing": [
            SearchHitV1("github-fixture", "q-licensing", 1, "acme/project", gh, "", SourceKind.GITHUB_REPOSITORY),
        ]
    }
    gh_sources = {
        gh: FetchedSourceV1("github-fixture", gh, "acme/project", SourceKind.GITHUB_REPOSITORY, SourceTier.PRIMARY, "text/plain", "Licensing uses the Apache-2.0 license. Concurrency uses cooperative scheduling.", 96),
    }
    return FixtureProvider("web-fixture", web_hits, web_sources), FixtureProvider("github-fixture", gh_hits, gh_sources)


class FixtureSynthesis:
    def __init__(self, *, contradiction: bool = False, require_followup: bool = False) -> None:
        self.contradiction = contradiction
        self.require_followup = require_followup
        self.calls = 0

    def synthesize(self, request: ResearchRequestV1, *, sources: Sequence[FetchedSourceV1], evidence: Sequence[EvidenceItemV1], round_index: int) -> SynthesisProposalV1:
        self.calls += 1
        by_topic: dict[str, list[EvidenceItemV1]] = {topic: [] for topic in request.required_topics}
        for item in evidence:
            by_topic.setdefault(item.topic, []).append(item)
        claims: list[ResearchClaimV1] = []
        concurrency = by_topic.get("concurrency", [])
        if len(concurrency) >= 2:
            refs = [ClaimEvidenceRefV1(item.evidence_id, EvidenceRelation.SUPPORTS) for item in concurrency[:2]]
            if self.contradiction:
                support_ids = {item.evidence_id for item in concurrency[:2]}
                extras = [item for item in evidence if item.evidence_id not in support_ids]
                for item in extras[:2]:
                    refs.append(ClaimEvidenceRefV1(item.evidence_id, EvidenceRelation.CONTRADICTS))
            claims.append(ResearchClaimV1("claim:concurrency", "Concurrency uses cooperative scheduling.", "concurrency", ClaimImportance.CRITICAL, tuple(refs)))
        licensing = by_topic.get("licensing", [])
        if len(licensing) >= 2 and (not self.require_followup or round_index > 0):
            claims.append(ResearchClaimV1(
                "claim:licensing", "The project uses Apache-2.0 licensing.", "licensing", ClaimImportance.IMPORTANT,
                tuple(ClaimEvidenceRefV1(item.evidence_id, EvidenceRelation.SUPPORTS) for item in licensing[:2]),
            ))
        followups = ()
        if self.require_followup and round_index == 0:
            followups = (ResearchQueryV1("q-followup-licensing", "project license official", ResearchChannel.WEB),)
        return SynthesisProposalV1("Evidence-bound fixture synthesis.", tuple(claims), followups)

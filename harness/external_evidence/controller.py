'Bounded external-research orchestration for external evidence.'
from __future__ import annotations

from collections import defaultdict
from typing import Iterable, Mapping, Sequence

from .evidence import ClaimGraph, normalize_evidence
from .models import (
    FetchedSourceV1,
    ResearchWorkflowPolicy,
    ResearchOutcome,
    ResearchQueryV1,
    ResearchReportV1,
    ResearchRequestV1,
    SearchHitV1,
    SynthesisProposalV1,
)
from .ports import ResearchSynthesisPort, SourceDiscoveryPort, SourceFetchPort
from .security import canonicalize_url
from .store import JsonResearchStore


class ResearchController:
    """Research loop that treats external text as evidence, never as authority.

    Search/fetch tools are read-only. The controller owns bounded discovery,
    deduplication, evidence normalization and stopping. It does not mutate a
    repository and does not grant source text instruction-following authority.
    """

    def __init__(
        self,
        *,
        policy: ResearchWorkflowPolicy,
        discovery: Mapping[str, SourceDiscoveryPort],
        fetchers: Mapping[str, SourceFetchPort],
        synthesis: ResearchSynthesisPort,
        store: JsonResearchStore | None = None,
    ) -> None:
        self.policy = policy
        self.discovery = dict(discovery)
        self.fetchers = dict(fetchers)
        self.synthesis = synthesis
        self.store = store
        if not self.discovery:
            raise ValueError("at least one discovery provider is required")

    @staticmethod
    def _provider_key(query: ResearchQueryV1) -> str:
        return query.channel.value

    def _search_round(self, queries: Sequence[ResearchQueryV1]) -> list[SearchHitV1]:
        hits: list[SearchHitV1] = []
        for query in queries[: self.policy.max_queries_per_round]:
            provider = self.discovery.get(self._provider_key(query))
            if provider is None:
                continue
            found = provider.search(query, limit=self.policy.max_results_per_query)
            hits.extend(list(found)[: self.policy.max_results_per_query])
        return hits

    def _fetch_new(self, hits: Iterable[SearchHitV1], seen_urls: set[str]) -> list[FetchedSourceV1]:
        result: list[FetchedSourceV1] = []
        for hit in hits:
            try:
                normalized = canonicalize_url(hit.url)
            except Exception:
                continue
            if normalized in seen_urls:
                continue
            fetcher = self.fetchers.get(hit.provider_id)
            if fetcher is None:
                continue
            try:
                source = fetcher.fetch(hit)
            except Exception:
                continue
            canonical = canonicalize_url(source.canonical_url)
            if canonical in seen_urls:
                continue
            seen_urls.add(canonical)
            result.append(source)
            if len(seen_urls) >= self.policy.max_sources_total:
                break
        return result

    def run(self, request: ResearchRequestV1) -> ResearchReportV1:
        pending = list(request.initial_queries)
        executed: list[ResearchQueryV1] = []
        seen_query_ids: set[str] = set()
        seen_urls: set[str] = set()
        sources: list[FetchedSourceV1] = []
        last_proposal = SynthesisProposalV1("No synthesis produced yet.", ())
        last_graph = ClaimGraph((), (), ())
        coverage = last_graph.coverage(request, self.policy)
        rounds_executed = 0

        for round_index in range(self.policy.max_search_rounds):
            round_queries = [item for item in pending if item.query_id not in seen_query_ids]
            pending = []
            if not round_queries:
                break
            rounds_executed += 1
            for query in round_queries[: self.policy.max_queries_per_round]:
                seen_query_ids.add(query.query_id)
                executed.append(query)
            hits = self._search_round(round_queries)
            new_sources = self._fetch_new(hits, seen_urls)
            sources.extend(new_sources)
            sources = sources[: self.policy.max_sources_total]
            evidence = normalize_evidence(sources, request, self.policy)
            proposal = self.synthesis.synthesize(
                request,
                sources=sources,
                evidence=evidence,
                round_index=round_index,
            )
            if len(proposal.claims) > self.policy.max_claims:
                raise RuntimeError("synthesis exceeds max_claims")
            graph = ClaimGraph(tuple(sources), evidence, proposal.claims)
            coverage = graph.coverage(request, self.policy)
            last_proposal, last_graph = proposal, graph
            if coverage.sufficient:
                report = ResearchReportV1(
                    request.research_id, request.question, ResearchOutcome.SUFFICIENT,
                    proposal.summary, tuple(sources), evidence, proposal.claims, coverage,
                    tuple(executed), rounds_executed,
                )
                self._persist(report)
                return report
            pending.extend(query for query in proposal.followup_queries if query.query_id not in seen_query_ids)

        report = ResearchReportV1(
            request.research_id, request.question, ResearchOutcome.INSUFFICIENT,
            last_proposal.summary, last_graph.sources, last_graph.evidence, last_graph.claims,
            coverage, tuple(executed), rounds_executed,
            "research stopping policy not satisfied within bounded search rounds",
        )
        self._persist(report)
        return report

    def _persist(self, report: ResearchReportV1) -> None:
        if self.store is None:
            return
        self.store.write_evidence_index(report)
        self.store.write_report(report)

'Ownership-preserving ports for external evidence research.'
from __future__ import annotations

from typing import Protocol, Sequence

from .models import EvidenceItemV1, FetchedSourceV1, ResearchQueryV1, ResearchRequestV1, SearchHitV1, SynthesisProposalV1


class SourceDiscoveryPort(Protocol):
    provider_id: str

    def search(self, query: ResearchQueryV1, *, limit: int) -> Sequence[SearchHitV1]: ...


class SourceFetchPort(Protocol):
    provider_id: str

    def fetch(self, hit: SearchHitV1) -> FetchedSourceV1: ...


class ResearchSynthesisPort(Protocol):
    def synthesize(
        self,
        request: ResearchRequestV1,
        *,
        sources: Sequence[FetchedSourceV1],
        evidence: Sequence[EvidenceItemV1],
        round_index: int,
    ) -> SynthesisProposalV1: ...


class ResearchArtifactPort(Protocol):
    def write_report(self, report: object) -> str: ...

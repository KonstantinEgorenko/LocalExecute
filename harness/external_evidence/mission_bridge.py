'mission-facing research bridge.\n\nThe bridge returns a compact evidence-backed summary and artifact references.\nIt never mutates Scheduler, repository, mission journal, or model routing state.\n'
from __future__ import annotations

from dataclasses import dataclass

from .controller import ResearchController
from .models import ResearchOutcome, ResearchRequestV1
from .store import JsonResearchStore


@dataclass(frozen=True, slots=True)
class ResearchMissionObservationV1:
    research_id: str
    sufficient: bool
    summary: str
    report_ref: str
    evidence_graph_ref: str
    source_count: int
    claim_count: int
    fingerprint: str


class MissionResearchBridge:
    def __init__(self, controller: ResearchController, store: JsonResearchStore) -> None:
        self.controller = controller
        self.store = store

    def research(self, request: ResearchRequestV1) -> ResearchMissionObservationV1:
        report = self.controller.run(request)
        report_ref = self.store.write_report(report)
        evidence_ref = self.store.write_evidence_index(report)
        return ResearchMissionObservationV1(
            report.research_id,
            report.outcome is ResearchOutcome.SUFFICIENT,
            report.summary,
            report_ref,
            evidence_ref,
            len(report.sources),
            len(report.claims),
            report.fingerprint,
        )

# Verified legacy import/pickle aliases, not canonical public exports.
Phase17ResearchBridge = MissionResearchBridge

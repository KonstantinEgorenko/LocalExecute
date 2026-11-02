'Read-only durable research stopping campaign report builder.'
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import ExperimentStatusV1
from .serialization import stable_sha256


@dataclass(frozen=True, slots=True)
class ResearchCampaignReportV1:
    campaign_id: str
    payload: dict[str, Any]

    @property
    def fingerprint(self) -> str:
        return stable_sha256(self.payload)


@dataclass(frozen=True, slots=True)
class ResearchCampaignReportBuilderV1:
    store: object

    def build(self, campaign_id: str) -> ResearchCampaignReportV1:
        contract = self.store.current_research_contract(campaign_id)
        state = self.store.load_campaign_state(campaign_id)
        summary = self.store.campaign_summary(campaign_id)
        experiments = self.store.list_experiments(campaign_id, limit=100_000)
        checkpoints = self.store.list_strategy_checkpoints(campaign_id, limit=100_000)
        insights = self.store.list_typed_insights(campaign_id, limit=100_000)
        evidence = self.store.list_evidence_packets(campaign_id)
        cohorts = self.store.list_proposal_cohorts(campaign_id)
        stops = self.store.list_stop_decisions(campaign_id)
        dev = [{"experiment_id": item.spec.experiment_id, "metric": item.result.primary_metric,
                "status": item.result.status.value, "failure_kind": None if item.result.failure_kind is None else item.result.failure_kind.value}
               for item in experiments if item.result is not None]
        scientific = [item for item in dev if item["status"] == ExperimentStatusV1.SUCCEEDED.value]
        diagnostics = [item for item in dev if item["failure_kind"] is not None]
        payload = {
            "schema_version": 1, "campaign_id": campaign_id, "objective": contract.objective,
            "state": state.to_dict(), "summary": summary.__dict__ if hasattr(summary, "__dict__") else {
                name: getattr(summary, name) for name in summary.__slots__},
            "champion_experiment_id": state.champion_experiment_id,
            "experiments": [{"spec": item.spec.to_dict(), "result": None if item.result is None else item.result.to_dict()} for item in experiments],
            "strategy_decisions": [item.to_dict() for item in checkpoints],
            "proposal_cohorts": [item.to_dict() for item in cohorts],
            "evidence_packets": [item.to_dict() for item in evidence],
            "scientific_insights": [item.to_dict() for item in insights],
            "technical_diagnostics": diagnostics, "dev_trajectory": scientific,
            "stop_decisions": [item.to_dict() for item in stops],
        }
        return ResearchCampaignReportV1(campaign_id, payload)

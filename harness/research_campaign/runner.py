"""Stable bounded operator façade for autonomous research campaigns.

It retains all lifecycle ownership in ``ResearchCampaignControllerV1``: the
runner only repeats durable ticks and returns the persisted terminal state.
"""
from __future__ import annotations

from dataclasses import dataclass

from .campaign import ResearchCampaignStateV1
from .controller import CampaignTickResultV1, ResearchCampaignControllerV1
from .reporting import ResearchCampaignReportBuilderV1, ResearchCampaignReportV1
from .stop_policy import ResearchStopDecisionV1


@dataclass(frozen=True, slots=True)
class ResearchCampaignRunPolicyV1:
    max_ticks: int = 128

    def __post_init__(self) -> None:
        if not isinstance(self.max_ticks, int) or isinstance(self.max_ticks, bool) or not 1 <= self.max_ticks <= 100_000:
            raise ValueError("max_ticks must be an integer in [1,100000]")


@dataclass(frozen=True, slots=True)
class ResearchCampaignRunResultV1:
    campaign_id: str
    ticks: tuple[CampaignTickResultV1, ...]
    final_state: ResearchCampaignStateV1
    exhausted_tick_budget: bool
    stop_decision: ResearchStopDecisionV1 | None = None


class ResearchCampaignRunnerV1:
    def __init__(self, controller: ResearchCampaignControllerV1, policy: ResearchCampaignRunPolicyV1 = ResearchCampaignRunPolicyV1()) -> None:
        self.controller = controller
        self.policy = policy

    def run(self, campaign_id: str) -> ResearchCampaignRunResultV1:
        results: list[CampaignTickResultV1] = []
        for _ in range(self.policy.max_ticks):
            result = self.controller.tick(campaign_id)
            results.append(result)
            if result.state_after.is_terminal or result.disposition.value in {"WAITING", "TERMINAL_NOOP", "STOPPED"}:
                stop = self.controller.store.latest_terminal_stop_decision(campaign_id)
                return ResearchCampaignRunResultV1(campaign_id, tuple(results), result.state_after, False, stop)
        final_state = self.controller.store.load_campaign_state(campaign_id)
        return ResearchCampaignRunResultV1(campaign_id, tuple(results), final_state, True,
                                           self.controller.store.latest_terminal_stop_decision(campaign_id))

    resume = run

    def status(self, campaign_id: str) -> ResearchCampaignStateV1:
        return self.controller.store.load_campaign_state(campaign_id)

    def report(self, campaign_id: str) -> ResearchCampaignReportV1:
        return ResearchCampaignReportBuilderV1(self.controller.store).build(campaign_id)

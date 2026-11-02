'research remediation planning and derived-report orchestration.'
from __future__ import annotations
from harness.compatibility.configuration import legacy_keywords

from typing import Mapping

from harness.research_quality import ResearchQualityEvaluator

from .derive import DerivedReportBuilder
from .planner import RemediationPlanner
from .policy import ResearchRemediationPolicy


class RemediationService:
    def __init__(self, policy: ResearchRemediationPolicy) -> None:
        self.policy = policy
        self.planner = RemediationPlanner(policy)
        self.derive = DerivedReportBuilder()

    def plan(self, source_report: Mapping, quality_report: Mapping):
        return self.planner.build(source_report, quality_report)

    def derive_report(self, base_report, targeted_reports, decisions):
        return self.derive.build(
            base_report,
            targeted_reports=targeted_reports,
            decisions=decisions,
        )

    @staticmethod
    @legacy_keywords({'phase19_policy': 'quality_policy'})
    def reevaluate_with_quality_gate(quality_policy, derived_report, entailment_judge=None):
        return ResearchQualityEvaluator(
            quality_policy,
            entailment_judge=entailment_judge,
        ).evaluate(derived_report)

# Verified compatibility aliases for historical Python interfaces.
RemediationService.reevaluate_with_phase19 = RemediationService.reevaluate_with_quality_gate

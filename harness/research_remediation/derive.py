"""Create a derived research report without mutating the frozen baseline."""
from __future__ import annotations

import copy
from typing import Mapping, Sequence

from .models import RemediationAction, RemediationDecisionV1


def _union_by_id(left: list, right: Sequence[Mapping], key: str) -> list:
    output = [copy.deepcopy(item) for item in left if isinstance(item, Mapping)]
    seen = {str(item.get(key, "")) for item in output}
    for item in right:
        if not isinstance(item, Mapping):
            continue
        value = str(item.get(key, ""))
        if not value or value in seen:
            continue
        output.append(copy.deepcopy(dict(item)))
        seen.add(value)
    return output


class DerivedReportBuilder:
    def build(self, base_report: Mapping, *, targeted_reports: Mapping[str, Mapping], decisions: Mapping[str, RemediationDecisionV1]) -> dict:
        derived = copy.deepcopy(dict(base_report))
        derived["derived_from_research_id"] = str(base_report.get("research_id", ""))
        derived["derived_from_report_fingerprint"] = str(base_report.get("report_fingerprint", ""))
        derived["research_id"] = f"{base_report.get('research_id', '')}-19q-derived"
        derived["outcome"] = "DERIVED_FOR_QUALITY_REEVALUATION"
        derived["failure_reason"] = ""
        derived["remediation_decisions"] = [
            decision.to_dict() for _, decision in sorted(decisions.items())
        ]

        all_sources = list(base_report.get("sources", []))
        all_evidence = list(base_report.get("evidence", []))
        for report in targeted_reports.values():
            all_sources = _union_by_id(all_sources, report.get("sources", []), "source_id")
            all_evidence = _union_by_id(all_evidence, report.get("evidence", []), "evidence_id")
        derived["sources"] = all_sources
        derived["evidence"] = all_evidence

        claims = []
        for claim in base_report.get("claims", []):
            if not isinstance(claim, Mapping):
                continue
            claim_id = str(claim.get("claim_id", ""))
            decision = decisions.get(claim_id)
            if decision is None:
                claims.append(copy.deepcopy(dict(claim)))
                continue
            if decision.action == RemediationAction.DROP:
                continue
            updated = copy.deepcopy(dict(claim))
            updated["text"] = decision.revised_text
            updated["evidence"] = [dict(item) for item in decision.evidence]
            updated["remediation_action"] = decision.action.value
            updated["remediation_reason"] = decision.reason
            claims.append(updated)
        derived["claims"] = claims
        derived["coverage"] = {
            "status": "STALE_BASELINE_COVERAGE_NOT_REUSED",
            "derived_quality_requires_phase19": True,
        }
        derived.pop("report_fingerprint", None)
        return derived

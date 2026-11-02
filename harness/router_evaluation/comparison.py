"""Contextual cross-version comparison with explicit non-causal labeling."""
from __future__ import annotations
from harness.compatibility.configuration import legacy_keywords

from typing import Any, Mapping

from .models import EvaluationMetrics


@legacy_keywords({'phase13a_report': 'fault_report'})
def contextual_v1_v2_comparison(
    *,
    fault_report: Mapping[str, Any],
    v2_metrics: EvaluationMetrics,
) -> dict[str, Any]:
    metrics = fault_report.get("metrics")
    if not isinstance(metrics, Mapping):
        raise ValueError("Phase 13A report missing metrics")
    v1_rate = float(metrics["router_selected_verified_rate"])
    v1_count = int(metrics["router_selected_verified_count"])
    v1_runs = int(metrics["router_selected_sample_count"])
    return {
        "comparison_kind": "contextual_non_causal",
        "directly_interchangeable_denominators": False,
        "reason": (
            "Router V1 uses the frozen Tool Protocol V1 baseline and a different "
            "case/repetition denominator; Router V2 uses Tool Protocol V2, "
            "ExecutionProfiles, FINAL grace, and the Phase 13D/14 suite."
        ),
        "router_v1": {
            "selected_verified": v1_count,
            "selected_runs": v1_runs,
            "selected_verified_rate": v1_rate,
        },
        "router_v2": {
            "selected_verified": v2_metrics.selected_verified_runs,
            "selected_runs": v2_metrics.valid_runs,
            "selected_verified_rate": v2_metrics.selected_verified_rate,
            "cascade_verified": v2_metrics.cascade_verified_runs,
            "cascade_verified_rate": v2_metrics.cascade_verified_rate,
        },
        "selected_rate_delta_contextual": (
            v2_metrics.selected_verified_rate - v1_rate
        ),
    }

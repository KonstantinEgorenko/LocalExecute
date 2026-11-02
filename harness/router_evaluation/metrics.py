"""Aggregate Router V2 execution evidence without inventing unobserved attempts."""
from __future__ import annotations

from collections import Counter
import math

from .models import CaseObservation, EvaluationMetrics


def _percentile(values: list[int], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (len(ordered) - 1) * percentile
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return float(ordered[lo])
    fraction = rank - lo
    return float(ordered[lo] + (ordered[hi] - ordered[lo]) * fraction)


def aggregate_metrics(records: tuple[CaseObservation, ...]) -> EvaluationMetrics:
    if not isinstance(records, tuple) or any(
        not isinstance(item, CaseObservation) for item in records
    ):
        raise ValueError("records must be tuple[CaseObservation, ...]")

    valid = [item for item in records if not item.infrastructure_invalid]
    selected_verified = sum(item.selected_verified for item in valid)
    cascade_verified = sum(item.verified for item in valid)
    fallback_runs = sum(item.fallback_used for item in valid)

    attempts = [attempt for item in valid for attempt in item.attempts]
    heavy = sum(attempt.heavy for attempt in attempts)
    success_latencies = [item.latency_ms for item in valid if item.verified]
    wasted = sum(
        attempt.latency_ms
        for item in valid
        for attempt in item.attempts
        if not attempt.verified
    )
    profile_attempts = Counter(attempt.profile_id for attempt in attempts)
    profile_verified = Counter(
        attempt.profile_id for attempt in attempts if attempt.verified
    )

    valid_count = len(valid)
    return EvaluationMetrics(
        raw_runs=len(records),
        valid_runs=valid_count,
        infrastructure_invalid_runs=len(records) - valid_count,
        selected_verified_runs=selected_verified,
        selected_verified_rate=(selected_verified / valid_count) if valid_count else 0.0,
        cascade_verified_runs=cascade_verified,
        cascade_verified_rate=(cascade_verified / valid_count) if valid_count else 0.0,
        fallback_runs=fallback_runs,
        fallback_rate=(fallback_runs / valid_count) if valid_count else 0.0,
        total_attempts=len(attempts),
        heavy_attempts=heavy,
        heavy_attempt_share=(heavy / len(attempts)) if attempts else 0.0,
        model_calls=sum(attempt.model_calls for attempt in attempts),
        tool_calls=sum(attempt.tool_calls for attempt in attempts),
        protocol_failures=sum(attempt.protocol_failures for attempt in attempts),
        tool_failures=sum(attempt.tool_failures for attempt in attempts),
        wasted_failed_latency_ms=wasted,
        mean_time_to_verified_success_ms=(
            sum(success_latencies) / len(success_latencies)
            if success_latencies else None
        ),
        p50_time_to_verified_success_ms=_percentile(success_latencies, 0.50),
        p95_time_to_verified_success_ms=_percentile(success_latencies, 0.95),
        profile_attempt_counts=dict(sorted(profile_attempts.items())),
        profile_verified_counts=dict(sorted(profile_verified.items())),
    )

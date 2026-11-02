'Deterministic evaluation metric aggregation and Router V1 regret analysis.'
from __future__ import annotations

from collections import Counter, defaultdict
from math import ceil
from statistics import mean
from typing import Iterable

from .models import (
    BenchmarkRecord,
    BenchmarkRun,
    BenchmarkSummary,
    GroupMetrics,
    LatencyMetrics,
    RateMetric,
    RouterRegretMetrics,
    stable_id,
)


def _rounded_mean(values: Iterable[int]) -> float | None:
    values = tuple(values)
    return None if not values else round(mean(values), 3)


def _nearest_rank(values: Iterable[int], percentile: int) -> int | None:
    ordered = sorted(values)
    if not ordered:
        return None
    rank = max(1, ceil((percentile / 100.0) * len(ordered)))
    return ordered[rank - 1]


def _group_metrics(records: tuple[BenchmarkRecord, ...]) -> GroupMetrics:
    count = len(records)
    successes = sum(r.measurement.verified_success for r in records)
    first_pass = sum(r.measurement.first_pass_success for r in records)
    retry_records = tuple(r for r in records if r.measurement.retry_count > 0)
    escalation_records = tuple(r for r in records if r.measurement.escalation_count > 0)
    protocol_failure_records = sum(r.measurement.protocol_failures > 0 for r in records)
    latencies = tuple(r.measurement.latency_ms for r in records)
    return GroupMetrics(
        sample_count=count,
        verified_success=RateMetric.make(successes, count),
        first_pass_success=RateMetric.make(first_pass, count),
        retry_success=RateMetric.make(sum(r.measurement.verified_success for r in retry_records), len(retry_records)),
        escalation_success=RateMetric.make(sum(r.measurement.verified_success for r in escalation_records), len(escalation_records)),
        protocol_failure_rate=RateMetric.make(protocol_failure_records, count),
        latency=LatencyMetrics(count, _rounded_mean(latencies), _nearest_rank(latencies, 50), _nearest_rank(latencies, 95)),
        mean_model_calls=_rounded_mean(r.measurement.model_calls for r in records),
        mean_tool_calls=_rounded_mean(r.measurement.tool_calls for r in records),
        mean_prompt_tokens=_rounded_mean(r.measurement.prompt_tokens for r in records),
        mean_output_tokens=_rounded_mean(r.measurement.output_tokens for r in records),
    )


def _router_regret(records: tuple[BenchmarkRecord, ...]) -> RouterRegretMetrics:
    groups: dict[tuple[str, int], list[BenchmarkRecord]] = defaultdict(list)
    for record in records:
        groups[(record.case_id, record.repetition)].append(record)

    comparable = 0
    oracle_success_groups = 0
    avoidable_failures = 0
    wins = 0
    latency_regrets: list[int] = []

    for group in groups.values():
        selected = [record for record in group if record.router_selected]
        if len(selected) != 1 or len(group) < 2:
            continue
        comparable += 1
        chosen = selected[0]
        successes = [record for record in group if record.measurement.verified_success]
        if not successes:
            continue
        oracle_success_groups += 1
        oracle = min(successes, key=lambda r: (r.measurement.latency_ms, r.model_id.casefold(), r.model_id))
        if not chosen.measurement.verified_success:
            avoidable_failures += 1
            continue
        latency_regrets.append(max(0, chosen.measurement.latency_ms - oracle.measurement.latency_ms))
        if chosen.model_id == oracle.model_id:
            wins += 1

    avoidable_rate = None if oracle_success_groups == 0 else round(avoidable_failures / oracle_success_groups, 6)
    win_rate = None if oracle_success_groups == 0 else round(wins / oracle_success_groups, 6)
    latency_mean = None if not latency_regrets else round(mean(latency_regrets), 3)
    return RouterRegretMetrics(
        comparable_groups=comparable,
        oracle_success_groups=oracle_success_groups,
        avoidable_failures=avoidable_failures,
        avoidable_failure_rate=avoidable_rate,
        router_oracle_wins=wins,
        router_oracle_win_rate=win_rate,
        mean_latency_regret_ms=latency_mean,
    )


class BenchmarkAggregator:
    """Produce answer-first reproducible summaries from a completed benchmark run."""

    def summarize(self, run: BenchmarkRun) -> BenchmarkSummary:
        records = tuple(sorted(run.records, key=lambda r: (r.case_id.casefold(), r.repetition, r.model_id.casefold())))
        by_model_groups: dict[str, list[BenchmarkRecord]] = defaultdict(list)
        by_task_groups: dict[str, list[BenchmarkRecord]] = defaultdict(list)
        for record in records:
            by_model_groups[record.model_id].append(record)
            by_task_groups[record.task_class.value].append(record)

        failure_classes = Counter(
            record.measurement.critic_failure_class.value
            for record in records
            if record.measurement.critic_failure_class.value != "NONE"
        )
        judge_decisions = Counter(
            record.measurement.judge_decision.value
            for record in records
            if record.measurement.judge_decision is not None
        )
        final_statuses = Counter(record.measurement.final_status.value for record in records)
        run_fingerprint = run.fingerprint
        summary_id = stable_id("bench-summary", {"run": run_fingerprint})
        return BenchmarkSummary(
            summary_id=summary_id,
            run_id=run.run_id,
            run_fingerprint=run_fingerprint,
            overall=_group_metrics(records),
            by_model={model: _group_metrics(tuple(items)) for model, items in sorted(by_model_groups.items())},
            by_task_class={task: _group_metrics(tuple(items)) for task, items in sorted(by_task_groups.items())},
            failure_classes=dict(failure_classes),
            judge_decisions=dict(judge_decisions),
            final_statuses=dict(final_statuses),
            router_regret=_router_regret(records),
        )

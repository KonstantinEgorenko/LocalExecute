'execution evidence metrics that preserve evaluation while adding fault-aware views.'
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from statistics import mean
from types import MappingProxyType
from typing import Mapping

from harness.observability.fault_attribution import FaultAnalysis, SampleValidity


def _rate(numerator: int, denominator: int) -> float:
    return 0.0 if denominator == 0 else round(numerator / denominator, 6)


@dataclass(frozen=True, slots=True)
class FaultMetrics:
    raw_sample_count: int
    valid_sample_count: int
    invalid_infrastructure_count: int

    raw_verified_success_rate: float
    valid_verified_success_rate: float

    router_selected_sample_count: int
    router_selected_verified_count: int
    router_selected_verified_rate: float

    faults_by_domain: Mapping[str, int]
    faults_by_code: Mapping[str, int]
    faults_by_attribution: Mapping[str, int]

    finalization_candidate_count: int

    wasted_failed_latency_ms: int
    avoidable_failure_wasted_latency_ms: int

    comparable_groups: int
    oracle_success_groups: int
    avoidable_failures: int
    router_oracle_wins: int
    conditional_latency_regret_ms: float | None

    def __post_init__(self) -> None:
        for field_name in (
            "raw_sample_count",
            "valid_sample_count",
            "invalid_infrastructure_count",
            "router_selected_sample_count",
            "router_selected_verified_count",
            "finalization_candidate_count",
            "wasted_failed_latency_ms",
            "avoidable_failure_wasted_latency_ms",
            "comparable_groups",
            "oracle_success_groups",
            "avoidable_failures",
            "router_oracle_wins",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{field_name} must be integer >= 0")

        for field_name in (
            "raw_verified_success_rate",
            "valid_verified_success_rate",
            "router_selected_verified_rate",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, float) or not 0.0 <= value <= 1.0:
                raise ValueError(f"{field_name} must be float in [0, 1]")

        if self.conditional_latency_regret_ms is not None and (
            not isinstance(self.conditional_latency_regret_ms, (int, float))
            or self.conditional_latency_regret_ms < 0
        ):
            raise ValueError("conditional_latency_regret_ms must be numeric >= 0|None")

        for field_name in (
            "faults_by_domain",
            "faults_by_code",
            "faults_by_attribution",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, Mapping):
                raise ValueError(f"{field_name} must be mapping")
            if any(
                not isinstance(k, str)
                or not isinstance(v, int)
                or isinstance(v, bool)
                or v < 0
                for k, v in value.items()
            ):
                raise ValueError(f"{field_name} must be mapping[str, int >= 0]")
            object.__setattr__(self, field_name, MappingProxyType(dict(value)))

    def to_dict(self) -> dict[str, object]:
        return {
            "raw_sample_count": self.raw_sample_count,
            "valid_sample_count": self.valid_sample_count,
            "invalid_infrastructure_count": self.invalid_infrastructure_count,
            "raw_verified_success_rate": self.raw_verified_success_rate,
            "valid_verified_success_rate": self.valid_verified_success_rate,
            "router_selected_sample_count": self.router_selected_sample_count,
            "router_selected_verified_count": self.router_selected_verified_count,
            "router_selected_verified_rate": self.router_selected_verified_rate,
            "faults_by_domain": dict(self.faults_by_domain),
            "faults_by_code": dict(self.faults_by_code),
            "faults_by_attribution": dict(self.faults_by_attribution),
            "finalization_candidate_count": self.finalization_candidate_count,
            "wasted_failed_latency_ms": self.wasted_failed_latency_ms,
            "avoidable_failure_wasted_latency_ms": self.avoidable_failure_wasted_latency_ms,
            "comparable_groups": self.comparable_groups,
            "oracle_success_groups": self.oracle_success_groups,
            "avoidable_failures": self.avoidable_failures,
            "router_oracle_wins": self.router_oracle_wins,
            "conditional_latency_regret_ms": self.conditional_latency_regret_ms,
        }


def aggregate_fault_metrics(
    analyses: tuple[FaultAnalysis, ...],
) -> FaultMetrics:
    if not isinstance(analyses, tuple) or any(
        not isinstance(item, FaultAnalysis) for item in analyses
    ):
        raise ValueError("analyses must be tuple[FaultAnalysis, ...]")
    if len({item.invocation_id for item in analyses}) != len(analyses):
        raise ValueError("analyses cannot contain duplicate invocation IDs")

    raw_count = len(analyses)
    raw_successes = sum(item.verified_success for item in analyses)
    valid = tuple(
        item
        for item in analyses
        if item.sample_validity is SampleValidity.VALID
    )
    valid_successes = sum(item.verified_success for item in valid)

    selected = tuple(item for item in valid if item.router_selected)
    selected_successes = sum(item.verified_success for item in selected)

    domain_counts = Counter(item.primary_domain.value for item in analyses)
    code_counts = Counter(item.primary_code.value for item in analyses)
    attribution_counts = Counter(item.attribution.value for item in analyses)

    wasted_failed_latency = sum(
        item.latency_ms
        for item in valid
        if not item.verified_success
    )

    groups: dict[tuple[str, int], list[FaultAnalysis]] = defaultdict(list)
    for item in analyses:
        groups[(item.case_id, item.repetition)].append(item)

    comparable = 0
    oracle_success_groups = 0
    avoidable_failures = 0
    router_wins = 0
    conditional_regrets: list[int] = []
    avoidable_wasted = 0

    for group in groups.values():
        chosen = [item for item in group if item.router_selected]
        if len(chosen) != 1 or len(group) < 2:
            continue

        comparable += 1
        selected_item = chosen[0]

        # Infrastructure-invalid alternatives cannot become oracle winners.
        successful_valid = [
            item
            for item in group
            if item.sample_validity is SampleValidity.VALID
            and item.verified_success
        ]
        if not successful_valid:
            continue

        oracle_success_groups += 1
        oracle = min(
            successful_valid,
            key=lambda item: (
                item.latency_ms,
                item.model_id.casefold(),
                item.model_id,
            ),
        )

        if (
            selected_item.sample_validity is not SampleValidity.VALID
            or not selected_item.verified_success
        ):
            avoidable_failures += 1
            avoidable_wasted += selected_item.latency_ms
            continue

        regret = max(0, selected_item.latency_ms - oracle.latency_ms)
        conditional_regrets.append(regret)
        if selected_item.model_id == oracle.model_id:
            router_wins += 1

    return FaultMetrics(
        raw_sample_count=raw_count,
        valid_sample_count=len(valid),
        invalid_infrastructure_count=raw_count - len(valid),
        raw_verified_success_rate=_rate(raw_successes, raw_count),
        valid_verified_success_rate=_rate(valid_successes, len(valid)),
        router_selected_sample_count=len(selected),
        router_selected_verified_count=selected_successes,
        router_selected_verified_rate=_rate(selected_successes, len(selected)),
        faults_by_domain=dict(sorted(domain_counts.items())),
        faults_by_code=dict(sorted(code_counts.items())),
        faults_by_attribution=dict(sorted(attribution_counts.items())),
        finalization_candidate_count=sum(
            item.finalization_candidate for item in analyses
        ),
        wasted_failed_latency_ms=wasted_failed_latency,
        avoidable_failure_wasted_latency_ms=avoidable_wasted,
        comparable_groups=comparable,
        oracle_success_groups=oracle_success_groups,
        avoidable_failures=avoidable_failures,
        router_oracle_wins=router_wins,
        conditional_latency_regret_ms=(
            None
            if not conditional_regrets
            else round(mean(conditional_regrets), 3)
        ),
    )

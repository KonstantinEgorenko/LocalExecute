"""Actual cascade/time-to-verified-success metrics for Router V2."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RouterV2RunRecord:
    task_id: str
    selected_profile_id: str | None
    attempted_profile_ids: tuple[str, ...]
    verified: bool
    latency_ms: int
    infrastructure_invalid: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, str) or not self.task_id.strip():
            raise ValueError("task_id must be non-empty string")
        if self.selected_profile_id is not None and (not isinstance(self.selected_profile_id, str) or not self.selected_profile_id.strip()):
            raise ValueError("selected_profile_id must be non-empty str|None")
        if not isinstance(self.attempted_profile_ids, tuple) or any(
            not isinstance(item, str) or not item.strip() for item in self.attempted_profile_ids
        ):
            raise ValueError("attempted_profile_ids must be tuple[str, ...]")
        if not isinstance(self.verified, bool) or not isinstance(self.infrastructure_invalid, bool):
            raise ValueError("verified/infrastructure_invalid must be bool")
        if not isinstance(self.latency_ms, int) or isinstance(self.latency_ms, bool) or self.latency_ms < 0:
            raise ValueError("latency_ms must be integer >= 0")


@dataclass(frozen=True, slots=True)
class RouterV2Metrics:
    raw_runs: int
    valid_runs: int
    invalid_infrastructure_runs: int
    verified_runs: int
    verified_success_rate: float
    first_profile_verified_runs: int
    first_profile_verified_rate: float
    fallback_runs: int
    fallback_rate: float
    total_time_to_verified_success_ms: int
    mean_time_to_verified_success_ms: float | None

    def to_dict(self) -> dict[str, object]:
        return {
            "raw_runs": self.raw_runs,
            "valid_runs": self.valid_runs,
            "invalid_infrastructure_runs": self.invalid_infrastructure_runs,
            "verified_runs": self.verified_runs,
            "verified_success_rate": self.verified_success_rate,
            "first_profile_verified_runs": self.first_profile_verified_runs,
            "first_profile_verified_rate": self.first_profile_verified_rate,
            "fallback_runs": self.fallback_runs,
            "fallback_rate": self.fallback_rate,
            "total_time_to_verified_success_ms": self.total_time_to_verified_success_ms,
            "mean_time_to_verified_success_ms": self.mean_time_to_verified_success_ms,
        }


def aggregate_router_v2_metrics(records: tuple[RouterV2RunRecord, ...]) -> RouterV2Metrics:
    if not isinstance(records, tuple) or any(not isinstance(item, RouterV2RunRecord) for item in records):
        raise ValueError("records must be tuple[RouterV2RunRecord, ...]")
    valid = tuple(item for item in records if not item.infrastructure_invalid)
    verified = tuple(item for item in valid if item.verified)
    first_verified = tuple(item for item in verified if len(item.attempted_profile_ids) <= 1)
    fallback = tuple(item for item in valid if len(item.attempted_profile_ids) > 1)
    total_success_latency = sum(item.latency_ms for item in verified)
    return RouterV2Metrics(
        raw_runs=len(records),
        valid_runs=len(valid),
        invalid_infrastructure_runs=len(records) - len(valid),
        verified_runs=len(verified),
        verified_success_rate=(len(verified) / len(valid)) if valid else 0.0,
        first_profile_verified_runs=len(first_verified),
        first_profile_verified_rate=(len(first_verified) / len(valid)) if valid else 0.0,
        fallback_runs=len(fallback),
        fallback_rate=(len(fallback) / len(valid)) if valid else 0.0,
        total_time_to_verified_success_ms=total_success_latency,
        mean_time_to_verified_success_ms=(total_success_latency / len(verified)) if verified else None,
    )

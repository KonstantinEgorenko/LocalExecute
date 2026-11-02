"""Immutable evidence contracts for one controlled worker calibration run."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


class CalibrationOutcome(StrEnum):
    SUCCESS = "SUCCESS"
    SAFETY_STOP = "SAFETY_STOP"


@dataclass(frozen=True, slots=True)
class CalibrationObservation:
    logical_model_id: str
    model_tag: str
    requested_context_length: int
    requested_num_gpu: int | None
    observed_context_length: int | None
    outcome: CalibrationOutcome
    stop_reason: str | None
    generation_executed: bool
    before_gpu_used_gb: float | None
    before_gpu_free_gb: float | None
    loaded_model_vram_gb: float | None
    after_load_gpu_used_gb: float | None
    after_load_gpu_free_gb: float | None
    system_gpu_delta_gb: float | None
    before_ram_used_gb: float | None
    after_load_ram_used_gb: float | None
    system_ram_delta_gb: float | None
    load_duration_ns: int | None
    prompt_eval_count: int | None
    eval_count: int | None
    eval_duration_ns: int | None
    total_duration_ns: int | None
    tokens_per_second: float | None
    cleanup_verified: bool
    timestamp: str

    @classmethod
    def timestamp_now(cls, **values: Any) -> "CalibrationObservation":
        return cls(timestamp=datetime.now(timezone.utc).isoformat(), **values)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CalibrationRequest:
    logical_model_id: str
    context_length: int
    num_gpu: int | None = None

    def __post_init__(self) -> None:
        if not self.logical_model_id:
            raise ValueError("logical_model_id is required")
        if not isinstance(self.context_length, int) or isinstance(self.context_length, bool) or self.context_length <= 0:
            raise ValueError("context_length must be a positive integer")
        if self.num_gpu is not None and (not isinstance(self.num_gpu, int) or isinstance(self.num_gpu, bool) or self.num_gpu < 0):
            raise ValueError("num_gpu must be a non-negative integer or None")
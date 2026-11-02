"""Model residency admission, independent of other device users.

Ollama size_vram measures resident allocation, not a hard peak-allocation cap.
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from typing import Any

MODEL_VRAM_BUDGET_BYTES = 11 * 1024**3
MODEL_VRAM_BUDGET_MIB = 11264
MEMORY_POLICY = "MODEL_ONLY_HYBRID_GPU_RAM"
LEGACY_MEMORY_POLICY = "LEGACY_FREE_RESERVE"


def require_model_capacity(actual_free_mib: float | None, *, resident_bytes: int = 0) -> None:
    if (actual_free_mib is None or not math.isfinite(actual_free_mib)
            or actual_free_mib < 0 or type(resident_bytes) is not int or resident_bytes < 0):
        raise ValueError("VRAM_TELEMETRY_UNAVAILABLE")
    # Warm reuse charges only capacity not already consumed by Swarm residency.
    required = max(0, MODEL_VRAM_BUDGET_BYTES - resident_bytes) / 1024**2
    if actual_free_mib < required:
        raise ValueError("INSUFFICIENT_FREE_VRAM_FOR_MODEL_BUDGET")


def resident_budget(rows: Iterable[Mapping[str, Any]]) -> int:
    total = 0
    for row in rows:
        size = row.get("size_vram")
        if type(size) is not int or size < 0:
            raise ValueError("MODEL_VRAM_TELEMETRY_UNAVAILABLE")
        total += size
    if total > MODEL_VRAM_BUDGET_BYTES:
        raise ValueError("MODEL_VRAM_BUDGET_EXCEEDED")
    return total

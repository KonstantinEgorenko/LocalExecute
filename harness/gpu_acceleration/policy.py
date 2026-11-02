'gpu acceleration additive policy for opportunistic Ornith GPU bursts.'
from __future__ import annotations
from harness.compatibility.configuration import resolve_config_path, normalize_configuration_document

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from harness.model_vram import MEMORY_POLICY, LEGACY_MEMORY_POLICY


@dataclass(frozen=True, slots=True)
class GpuAccelerationPolicy:
    enabled_env: str
    root_env: str
    lead_base_url: str
    worker_base_url: str
    external_base_urls: tuple[str, ...]
    model_tag: str
    gpu_num_gpu: int
    cpu_num_gpu: int
    num_ctx: int
    min_free_vram_mib: int
    opportunistic_acquire_timeout_seconds: float
    placement_timeout_seconds: float
    status_poll_interval_seconds: float
    reclaim_known_idle_workers: bool
    fail_closed_cleanup: bool

    memory_policy: str = LEGACY_MEMORY_POLICY

    @classmethod
    def from_project_root(cls, root: str | Path) -> "GpuAccelerationPolicy":
        root = Path(root)
        raw = normalize_configuration_document(yaml.safe_load((resolve_config_path(root / "config" / "phase20u.yaml")).read_text(encoding="utf-8")))
        if not isinstance(raw, dict) or raw.get("schema_version") != 1:
            raise ValueError("config/phase20u.yaml requires schema_version: 1")
        data: Any = raw.get("ornith_gpu_burst_v1")
        if not isinstance(data, dict):
            raise ValueError("config/phase20u.yaml requires ornith_gpu_burst_v1 mapping")
        policy = cls(
            enabled_env=str(data["enabled_env"]),
            root_env=str(data["root_env"]),
            lead_base_url=str(data["lead_base_url"]).rstrip("/"),
            worker_base_url=str(data["worker_base_url"]).rstrip("/"),
            external_base_urls=tuple(str(x).rstrip("/") for x in data.get("external_base_urls", ())),
            model_tag=str(data["model_tag"]),
            gpu_num_gpu=int(data["gpu_num_gpu"]),
            cpu_num_gpu=int(data["cpu_num_gpu"]),
            num_ctx=int(data["num_ctx"]),
            min_free_vram_mib=int(data["min_free_vram_mib"]),
            memory_policy=str(data.get("memory_policy", LEGACY_MEMORY_POLICY)),
            opportunistic_acquire_timeout_seconds=float(data["opportunistic_acquire_timeout_seconds"]),
            placement_timeout_seconds=float(data["placement_timeout_seconds"]),
            status_poll_interval_seconds=float(data["status_poll_interval_seconds"]),
            reclaim_known_idle_workers=bool(data.get("reclaim_known_idle_workers", True)),
            fail_closed_cleanup=bool(data.get("fail_closed_cleanup", True)),
        )
        policy.validate()
        return policy

    def validate(self) -> None:
        if self.memory_policy not in (MEMORY_POLICY, LEGACY_MEMORY_POLICY):
            raise ValueError("Unknown model memory policy")
        if self.memory_policy == MEMORY_POLICY and self.min_free_vram_mib != 0:
            raise ValueError("Model-only policy has no post-placement free reserve")
        if self.gpu_num_gpu != 20:
            raise ValueError("Phase20U production Ornith profile is fixed at num_gpu=20")
        if self.cpu_num_gpu != 0:
            raise ValueError("Phase20U CPU fallback must be num_gpu=0")
        # Historical explicit callers retain G16/G17 reserve semantics.
        # Current model-only policy does not reserve other processes' memory.
        if self.memory_policy == LEGACY_MEMORY_POLICY and (type(self.min_free_vram_mib) is not int or self.min_free_vram_mib not in (3584, 3084)):
            raise ValueError("Phase20U Ornith reserve must be historical 3584 or G17 3084 MiB")
        if self.num_ctx != 65536:
            raise ValueError("Phase20U benchmarked Ornith context is fixed at 65536")
        if not self.model_tag.strip():
            raise ValueError("model_tag must be non-empty")
        urls = (self.lead_base_url, self.worker_base_url, *self.external_base_urls)
        if any(not value.startswith("http://127.0.0.1:") for value in urls):
            raise ValueError("Phase20U endpoints must remain loopback")
        if self.opportunistic_acquire_timeout_seconds < 0:
            raise ValueError("opportunistic acquire timeout must be >= 0")
        if self.placement_timeout_seconds <= 0 or self.status_poll_interval_seconds <= 0:
            raise ValueError("placement timeout and poll interval must be positive")

    @property
    def min_free_vram_gb(self) -> float:
        return self.min_free_vram_mib / 1024.0

    def enabled(self) -> bool:
        return os.getenv(self.enabled_env, "").strip().casefold() in {"1", "true", "yes", "on"}

# Verified legacy import/pickle aliases, not canonical public exports.
Phase20UPolicy = GpuAccelerationPolicy

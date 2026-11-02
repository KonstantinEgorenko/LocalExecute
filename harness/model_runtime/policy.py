'model runtime policy: universal model-runtime orchestration and observability.'
from __future__ import annotations
from harness.compatibility.configuration import resolve_config_path, normalize_configuration_document

import os
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

import yaml
from harness.model_vram import MEMORY_POLICY, LEGACY_MEMORY_POLICY


@dataclass(frozen=True, slots=True)
class ModelLifecyclePolicy:
    enabled_env: str
    root_env: str
    lock_path_env: str
    default_lock_path: str
    acquire_timeout_seconds: float
    poll_interval_seconds: float
    min_free_vram_mib: int
    min_available_ram_mib: int
    worker_keep_alive: str
    status_poll_interval_seconds: float
    lifecycle_timeout_seconds: float
    fail_closed: bool
    final_cleanup: bool
    external_observe_urls: tuple[str, ...]
    runtime_profile_cache: str
    calibration_enabled: bool
    calibration_min_num_gpu: int
    calibration_max_attempts: int
    calibration_safety_margin_mib: int
    max_worker_vram_mib: int
    todo_tag_resolution: Mapping[str, Mapping[str, tuple[str, ...]]]

    memory_policy: str = LEGACY_MEMORY_POLICY

    @classmethod
    def from_project_root(cls, root: str | Path) -> "ModelLifecyclePolicy":
        root = Path(root)
        raw = normalize_configuration_document(yaml.safe_load((resolve_config_path(root / "config" / "phase20v.yaml")).read_text(encoding="utf-8")))
        if not isinstance(raw, dict) or raw.get("schema_version") != 1:
            raise ValueError("config/phase20v.yaml requires schema_version: 1")
        data: Any = raw.get("model_runtime_v1")
        if not isinstance(data, dict):
            raise ValueError("config/phase20v.yaml requires model_runtime_v1 mapping")
        calibration = data.get("placement_calibration")
        if not isinstance(calibration, dict):
            raise ValueError("Phase20V placement_calibration mapping is required")
        resolution = data.get("todo_tag_resolution", {})
        if not isinstance(resolution, dict):
            raise ValueError("Phase20V todo_tag_resolution must be a mapping")
        normalized: dict[str, Mapping[str, tuple[str, ...]]] = {}
        for logical_id, rule in resolution.items():
            if not isinstance(logical_id, str) or not logical_id.strip() or not isinstance(rule, dict):
                raise ValueError("Phase20V TODO tag resolution entries must be mappings")
            all_tokens = rule.get("all_tokens", ())
            any_tokens = rule.get("any_tokens", ())
            if not isinstance(all_tokens, list) or not isinstance(any_tokens, list):
                raise ValueError("Phase20V tag matcher tokens must be lists")
            if any(not isinstance(x, str) or not x.strip() for x in [*all_tokens, *any_tokens]):
                raise ValueError("Phase20V tag matcher tokens must be non-empty strings")
            normalized[logical_id] = MappingProxyType({
                "all_tokens": tuple(x.strip().casefold() for x in all_tokens),
                "any_tokens": tuple(x.strip().casefold() for x in any_tokens),
            })
        p = cls(
            enabled_env=str(data["enabled_env"]),
            root_env=str(data["root_env"]),
            lock_path_env=str(data["lock_path_env"]),
            default_lock_path=str(data["default_lock_path"]),
            acquire_timeout_seconds=float(data["acquire_timeout_seconds"]),
            poll_interval_seconds=float(data["poll_interval_seconds"]),
            min_free_vram_mib=int(data["min_free_vram_mib"]),
            memory_policy=str(data.get("memory_policy", LEGACY_MEMORY_POLICY)),
            min_available_ram_mib=int(data["min_available_ram_mib"]),
            worker_keep_alive=str(data["worker_keep_alive"]),
            status_poll_interval_seconds=float(data["status_poll_interval_seconds"]),
            lifecycle_timeout_seconds=float(data["lifecycle_timeout_seconds"]),
            fail_closed=bool(data.get("fail_closed", True)),
            final_cleanup=bool(data.get("final_cleanup", True)),
            external_observe_urls=tuple(str(x).rstrip("/") for x in data.get("external_observe_urls", ())),
            runtime_profile_cache=str(data["runtime_profile_cache"]),
            calibration_enabled=bool(calibration.get("enabled", True)),
            calibration_min_num_gpu=int(calibration["min_num_gpu"]),
            calibration_max_attempts=int(calibration["max_attempts"]),
            calibration_safety_margin_mib=int(calibration["safety_margin_mib"]),
            max_worker_vram_mib=int(calibration["max_worker_vram_mib"]),
            todo_tag_resolution=MappingProxyType(normalized),
        )
        p.validate()
        return p

    def validate(self) -> None:
        if self.memory_policy not in (MEMORY_POLICY, LEGACY_MEMORY_POLICY):
            raise ValueError("Unknown model memory policy")
        if self.memory_policy == MEMORY_POLICY and self.min_free_vram_mib != 0:
            raise ValueError("Model-only policy has no post-placement free reserve")
        if self.memory_policy == LEGACY_MEMORY_POLICY and self.min_free_vram_mib != 3584:
            raise ValueError("Phase20V common GPU-heavy reserve must be 3584 MiB")
        if self.min_available_ram_mib != 16384:
            raise ValueError("Phase20V host-RAM reserve must be 16384 MiB")
        if self.worker_keep_alive != "10m":
            raise ValueError("Phase20V worker warm residency is fixed at 10m")
        if self.acquire_timeout_seconds <= 0 or self.poll_interval_seconds <= 0:
            raise ValueError("Phase20V lock timings must be positive")
        if self.lifecycle_timeout_seconds <= 0 or self.status_poll_interval_seconds <= 0:
            raise ValueError("Phase20V lifecycle timings must be positive")
        if any(not url.startswith("http://127.0.0.1:") for url in self.external_observe_urls):
            raise ValueError("Phase20V external observed Ollama endpoints must be loopback")
        cache = Path(self.runtime_profile_cache)
        if cache.is_absolute() or ".." in cache.parts or str(cache) in ("", "."):
            raise ValueError("Phase20V runtime profile cache must be a project-relative path")
        if not self.calibration_enabled:
            raise ValueError("Phase20V adaptive placement calibration must remain enabled")
        if self.calibration_min_num_gpu < 1 or self.calibration_max_attempts < 1:
            raise ValueError("Phase20V calibration layer/attempt limits must be positive")
        if not 0 < self.calibration_safety_margin_mib <= 512:
            raise ValueError("Phase20V calibration safety margin must be in (0, 512] MiB")
        if self.max_worker_vram_mib != 11264:
            raise ValueError("Phase20V worker VRAM hard limit must remain 11264 MiB (11 GiB)")

    def enabled(self) -> bool:
        return os.getenv(self.enabled_env, "").strip().casefold() in {"1", "true", "yes", "on"}

    def lock_path(self) -> Path:
        return Path(os.getenv(self.lock_path_env, "").strip() or self.default_lock_path)

    def cache_path(self, root: str | Path) -> Path:
        return Path(root).resolve() / self.runtime_profile_cache

# Verified legacy import/pickle aliases, not canonical public exports.
Phase20VPolicy = ModelLifecyclePolicy

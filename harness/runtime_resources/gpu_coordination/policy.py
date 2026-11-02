'Configuration for the additive gpu coordination GPU_HEAVY guard.'
from __future__ import annotations
from harness.compatibility.configuration import resolve_config_path, normalize_configuration_document

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True, slots=True)
class GpuCoordinationPolicy:
    enabled_env: str
    root_env: str
    lock_path_env: str
    default_lock_path: str
    acquire_timeout_seconds: float
    poll_interval_seconds: float
    worker_server_urls: tuple[str, ...]
    guarded_paths: tuple[str, ...]
    status_paths: tuple[str, ...]
    fail_closed: bool

    @classmethod
    def from_project_root(cls, root: str | Path) -> "GpuCoordinationPolicy":
        root = Path(root)
        path = root / "config" / "gpu_coordination.yaml"
        if not path.exists():
            path = resolve_config_path(root / "config" / "phase20g.yaml")  # Legacy configuration reader.
        raw = normalize_configuration_document(yaml.safe_load(path.read_text(encoding="utf-8")))
        if not isinstance(raw, dict) or not isinstance(raw.get("gpu_heavy_guard_v1"), dict):
            raise ValueError("config/phase20g.yaml requires gpu_heavy_guard_v1 mapping")
        data: dict[str, Any] = raw["gpu_heavy_guard_v1"]
        policy = cls(
            enabled_env=str(data["enabled_env"]),
            root_env=str(data["root_env"]),
            lock_path_env=str(data["lock_path_env"]),
            default_lock_path=str(data["default_lock_path"]),
            acquire_timeout_seconds=float(data["acquire_timeout_seconds"]),
            poll_interval_seconds=float(data["poll_interval_seconds"]),
            worker_server_urls=tuple(str(x).rstrip("/") for x in data["worker_server_urls"]),
            guarded_paths=tuple(str(x) for x in data["guarded_paths"]),
            status_paths=tuple(str(x) for x in data["status_paths"]),
            fail_closed=bool(data.get("fail_closed", True)),
        )
        policy.validate()
        return policy

    def validate(self) -> None:
        for name in ("enabled_env", "root_env", "lock_path_env", "default_lock_path"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be non-empty")
        if self.acquire_timeout_seconds < 0:
            raise ValueError("acquire_timeout_seconds must be >= 0")
        if self.poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be > 0")
        if not self.worker_server_urls:
            raise ValueError("at least one worker_server_url is required")
        if any(not url.startswith("http://127.0.0.1:") for url in self.worker_server_urls):
            raise ValueError("Phase20G worker endpoints must remain loopback")
        if "/api/chat" not in self.guarded_paths or "/api/generate" not in self.guarded_paths:
            raise ValueError("Phase20G must guard /api/chat and /api/generate")
        if any(path in self.guarded_paths for path in self.status_paths):
            raise ValueError("status paths must not be GPU-heavy paths")

    def enabled(self) -> bool:
        value = os.getenv(self.enabled_env, "").strip().casefold()
        return value in {"1", "true", "yes", "on"}

    def lock_path(self) -> Path:
        value = os.getenv(self.lock_path_env, "").strip() or self.default_lock_path
        return Path(value)

# Historical import/pickle compatibility; new execution uses GpuCoordinationPolicy.
Phase20GPolicy = GpuCoordinationPolicy

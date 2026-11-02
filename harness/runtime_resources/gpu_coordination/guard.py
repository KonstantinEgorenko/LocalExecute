'Runtime monkeypatch layer for the additive gpu coordination GPU_HEAVY lease.\n\nFrozen runtime files remain untouched. The guard is installed by the gpu coordination\nlauncher (or optional sitecustomize autostart) before real Swarm work begins.\n'
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from harness.resource_manager.locks import LockState

from .lease import GpuHeavyLease
from .policy import GpuCoordinationPolicy

_INSTALLED = False
_POLICY: GpuCoordinationPolicy | None = None
_LEASE: GpuHeavyLease | None = None
_ORIGINALS: dict[str, Any] = {}


def runtime_guard_installed() -> bool:
    return _INSTALLED


def current_lease() -> GpuHeavyLease | None:
    return _LEASE


def _canonical_base(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".rstrip("/")


def _request_is_guarded(policy: GpuCoordinationPolicy, base_url: str, path: str) -> bool:
    if path in policy.status_paths:
        return False
    return _canonical_base(base_url) in policy.worker_server_urls and path in policy.guarded_paths


def _owner_from_payload(prefix: str, payload: dict[str, Any] | None, fallback: str) -> str:
    model = payload.get("model") if isinstance(payload, dict) else None
    if isinstance(model, str) and model.strip():
        return f"{prefix}:{model.strip()}"
    return f"{prefix}:{fallback}"


def _install_native_client_guard(policy: GpuCoordinationPolicy, lease: GpuHeavyLease) -> None:
    from harness.executor.client import OllamaNativeClient

    original = OllamaNativeClient._request
    _ORIGINALS["OllamaNativeClient._request"] = original

    def guarded_request(self, base_url: str, path: str, *, timeout_seconds: float, payload=None):
        if not _request_is_guarded(policy, base_url, path):
            return original(self, base_url, path, timeout_seconds=timeout_seconds, payload=payload)
        owner = _owner_from_payload("swarm-http", payload, path)
        with lease.held(owner, timeout_seconds=policy.acquire_timeout_seconds):
            return original(self, base_url, path, timeout_seconds=timeout_seconds, payload=payload)

    OllamaNativeClient._request = guarded_request


def _install_worker_transaction_guard(policy: GpuCoordinationPolicy, lease: GpuHeavyLease) -> None:
    from harness.executor.lifecycle import WorkerLifecycleManager

    original = WorkerLifecycleManager.prepared
    _ORIGINALS["WorkerLifecycleManager.prepared"] = original

    @contextmanager
    def guarded_prepared(self, decision):
        selected = getattr(decision, "selected_model", None)
        owner = f"swarm-worker:{selected or 'unknown'}"
        with lease.held(owner, timeout_seconds=policy.acquire_timeout_seconds):
            with original(self, decision) as prepared:
                yield prepared

    WorkerLifecycleManager.prepared = guarded_prepared


def _install_calibration_guard(policy: GpuCoordinationPolicy, lease: GpuHeavyLease) -> None:
    from harness.calibration.runner import CalibrationRunner

    original = CalibrationRunner.run
    _ORIGINALS["CalibrationRunner.run"] = original

    def guarded_run(self, request):
        model_id = getattr(request, "logical_model_id", "unknown")
        with lease.held(f"swarm-calibration:{model_id}", timeout_seconds=policy.acquire_timeout_seconds):
            return original(self, request)

    CalibrationRunner.run = guarded_run


def _install_profile_residency_guard(policy: GpuCoordinationPolicy, lease: GpuHeavyLease) -> None:
    'Hold the machine lease from explicit profile preload through unload.\n\n    execution evidence+ live workflows intentionally preload a profile, perform several\n    chat turns, then unload it. Per-request locking alone would leave a gap\n    between those operations, so an explicit profile reservation is retained.\n    '
    from harness.executor.profile_client import ProfileOllamaClient

    original_preload = ProfileOllamaClient.preload_profile
    original_unload = ProfileOllamaClient.unload_profile
    _ORIGINALS["ProfileOllamaClient.preload_profile"] = original_preload
    _ORIGINALS["ProfileOllamaClient.unload_profile"] = original_unload

    def guarded_preload(self, base_url, profile):
        current = getattr(self, "_gpu_profile_reservation", None)
        tag = getattr(profile, "model_tag", "unknown")
        if current is not None:
            if current[0] != tag:
                raise RuntimeError(
                    f"Phase20G profile reservation already active for {current[0]!r}; cannot preload {tag!r}"
                )
            return original_preload(self, base_url, profile)
        cm = lease.held(f"swarm-profile:{tag}", timeout_seconds=policy.acquire_timeout_seconds)
        cm.__enter__()
        try:
            result = original_preload(self, base_url, profile)
        except BaseException:
            cm.__exit__(*__import__("sys").exc_info())
            raise
        self._gpu_profile_reservation = (tag, cm)
        return result

    def guarded_unload(self, base_url, profile):
        reservation = getattr(self, "_gpu_profile_reservation", None)
        try:
            return original_unload(self, base_url, profile)
        finally:
            if reservation is not None and reservation[0] == getattr(profile, "model_tag", None):
                self._gpu_profile_reservation = None
                reservation[1].__exit__(None, None, None)

    ProfileOllamaClient.preload_profile = guarded_preload
    ProfileOllamaClient.unload_profile = guarded_unload


def _install_snapshot_overlay(lease: GpuHeavyLease) -> None:
    from harness.resource_manager.manager import ResourceManager

    original = ResourceManager.get_snapshot
    _ORIGINALS["ResourceManager.get_snapshot"] = original

    def guarded_snapshot(self, observations):
        snapshot = original(self, observations)
        if lease.held_by_current_thread:
            return snapshot
        status = lease.status()
        if not status.locked:
            return snapshot
        owner = status.owner or (f"pid:{status.owner_pid}" if status.owner_pid is not None else "external")
        # Preserve the frozen ResourceSnapshot shape while making Router/admission
        # observe a machine-wide owner as if the GPU worker mutex were busy.
        from dataclasses import replace

        return replace(snapshot, gpu_worker_lock=LockState(True, f"GPU_HEAVY:{owner}"))

    ResourceManager.get_snapshot = guarded_snapshot


def install_runtime_guard(root: str | Path, *, policy: GpuCoordinationPolicy | None = None, lease: GpuHeavyLease | None = None) -> GpuHeavyLease:
    global _INSTALLED, _POLICY, _LEASE
    if _INSTALLED:
        assert _LEASE is not None
        return _LEASE
    root = Path(root).resolve()
    policy = policy or GpuCoordinationPolicy.from_project_root(root)
    lease = lease or GpuHeavyLease(policy.lock_path(), poll_interval_seconds=policy.poll_interval_seconds)
    _install_native_client_guard(policy, lease)
    _install_worker_transaction_guard(policy, lease)
    _install_calibration_guard(policy, lease)
    _install_profile_residency_guard(policy, lease)
    _install_snapshot_overlay(lease)
    _POLICY, _LEASE, _INSTALLED = policy, lease, True
    return lease


def install_from_environment() -> GpuHeavyLease | None:
    root_text = os.getenv("SWARM_GPU_HEAVY_ROOT", "").strip()
    if not root_text:
        return None
    policy = GpuCoordinationPolicy.from_project_root(root_text)
    if not policy.enabled():
        return None
    return install_runtime_guard(root_text, policy=policy)

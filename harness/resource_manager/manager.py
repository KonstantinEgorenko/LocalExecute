'Read-only configuration runtime resource coordination facade.'
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from ..errors import ResourceDiscoveryError
from ..model_vram import MEMORY_POLICY, MODEL_VRAM_BUDGET_MIB
from ..models import ModelRegistry
from .locks import ResourceLocks
from .snapshot import ResourceSnapshot, discover_gpu, discover_ram, now_utc


@dataclass(frozen=True, slots=True)
class AdmissionResult:
    allowed: bool
    reason: str
    requested_vram_gb: float
    remaining_vram_gb: float | None
    soft_target_exceeded: bool = False


class ResourceManager:
    def __init__(self, registry: ModelRegistry, resource_config: dict[str, Any], locks: ResourceLocks | None = None) -> None:
        self.registry = registry
        self._config = resource_config
        self.locks = locks or ResourceLocks()

    @property
    def worker_vram_policy(self) -> MappingProxyType:
        """Read-only worker VRAM policy for deterministic consumers."""
        return MappingProxyType(dict(self._config["worker_vram"]))

    def get_snapshot(self, observations: dict[str, Any]) -> ResourceSnapshot:
        errors: list[str] = [f"{item.server_id}: {item.error}" for item in observations.values() if item.error]
        try:
            ram = discover_ram()
        except ResourceDiscoveryError as exc:
            ram = None
            errors.append(f"RAM: {exc}")
        try:
            gpu = discover_gpu()
            gpu_probe = gpu.probe
        except ResourceDiscoveryError as exc:
            gpu = None
            gpu_probe = getattr(exc, 'evidence', None)
            errors.append(f"GPU: {exc}")
        statuses = []
        for logical_id in self.registry.logical_ids:
            definition = self.registry.get(logical_id)
            observed = observations.get(definition.server)
            if observed is None:
                statuses.append(self.registry.status_for(logical_id, False, False, ()))
            else:
                statuses.append(self.registry.status_for(logical_id, observed.online, observed.discovery_ok, observed.loaded_models))
        lead = observations.get("lead")
        worker = observations.get("worker")
        return ResourceSnapshot(now_utc(), ram, gpu, bool(lead and lead.online), bool(worker and worker.online), tuple(statuses),
                                self.locks.gpu_worker_lock.state, self.locks.cpu_bandwidth_lock.state, tuple(errors), gpu_probe)

    def can_use_gpu_worker(self, snapshot: ResourceSnapshot, requested_vram_gb: float) -> AdmissionResult:
        """Evaluate an explicit VRAM estimate; this method never predicts model usage."""
        if requested_vram_gb < 0:
            return AdmissionResult(False, "requested_vram_invalid", requested_vram_gb, None)
        if snapshot.gpu is None:
            return AdmissionResult(False, "gpu_unavailable", requested_vram_gb, None)
        if snapshot.gpu_worker_lock.locked:
            return AdmissionResult(False, "gpu_worker_lock_busy", requested_vram_gb, snapshot.gpu.vram_free_gb)
        limits = self.worker_vram_policy
        hard_limit = float(limits["hard_limit_gb"])
        if requested_vram_gb > hard_limit:
            return AdmissionResult(False, "requested_vram_exceeds_hard_limit", requested_vram_gb, snapshot.gpu.vram_free_gb)
        if limits.get("memory_policy") == MEMORY_POLICY and snapshot.gpu.vram_free_gb * 1024 < MODEL_VRAM_BUDGET_MIB:
            return AdmissionResult(False, "INSUFFICIENT_FREE_VRAM_FOR_MODEL_BUDGET", requested_vram_gb, snapshot.gpu.vram_free_gb)
        remaining = snapshot.gpu.vram_free_gb - requested_vram_gb
        if remaining < float(limits["min_free_after_load_gb"]):
            return AdmissionResult(False, "insufficient_free_vram_after_allocation", requested_vram_gb, remaining)
        return AdmissionResult(True, "admitted", requested_vram_gb, remaining, requested_vram_gb > float(limits["soft_target_gb"]))

    def can_start_heavy_inference(self, snapshot: ResourceSnapshot, requested_vram_gb: float) -> AdmissionResult:
        if snapshot.cpu_bandwidth_lock.locked:
            return AdmissionResult(False, "cpu_bandwidth_lock_busy", requested_vram_gb, snapshot.gpu.vram_free_gb if snapshot.gpu else None)
        return self.can_use_gpu_worker(snapshot, requested_vram_gb)

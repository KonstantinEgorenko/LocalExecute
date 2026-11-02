"""Phase20U opportunistic Ornith GPU acceleration over frozen Phase20G/20R.

The LLM never selects a resource profile. Runtime policy exposes exactly two
execution modes:

* ``num_gpu=20`` while the machine-wide GPU_HEAVY lease is held and the
  model-only resident budget is met (legacy policies retain their free reserve);
* ``num_gpu=0`` CPU fallback when the GPU cannot be admitted immediately.

Residency is burst-scoped. At the end of *both* modes Ornith is explicitly
unloaded with ``keep_alive=0`` and must disappear from ``/api/ps``.  There is no
persistent CPU-resident restore step.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterator
from urllib.request import Request, urlopen

from harness.runtime_resources.gpu_coordination.lease import GpuHeavyLease, GpuHeavyLeaseTimeout
from harness.runtime_resources.gpu_coordination.policy import GpuCoordinationPolicy

from .policy import GpuAccelerationPolicy
from harness.model_vram import MEMORY_POLICY, require_model_capacity, resident_budget

_INSTALLED = False
_MANAGER: "OrnithGpuBurstManager | None" = None
_ORIGINALS: dict[str, Any] = {}


class OrnithGpuBurstError(RuntimeError):
    pass


class OrnithGpuBurstCleanupError(OrnithGpuBurstError):
    pass


@dataclass(frozen=True, slots=True)
class GpuMemory:
    used_mib: float
    free_mib: float
    total_mib: float


@dataclass(frozen=True, slots=True)
class BurstState:
    mode: str
    num_gpu: int
    reason: str
    free_vram_mib: float | None = None
    size_vram_mib: float | None = None
    reclaimed_worker_models: tuple[str, ...] = ()


class NvidiaSmiMemoryProbe:
    """Strict VRAM telemetry; optional power/temp fields are deliberately absent."""

    def sample(self) -> GpuMemory:
        cp = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.used,memory.total,memory.free",
                "--format=csv,noheader,nounits",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if cp.returncode:
            raise OrnithGpuBurstError(cp.stderr.strip() or "nvidia-smi failed")
        rows = [line.strip() for line in cp.stdout.splitlines() if line.strip()]
        if not rows:
            raise OrnithGpuBurstError("nvidia-smi returned no GPU rows")
        values = [item.strip() for item in rows[0].split(",")]
        if len(values) < 3:
            raise OrnithGpuBurstError(f"unexpected nvidia-smi memory row: {rows[0]!r}")
        try:
            used, total, free = (float(x) for x in values[:3])
        except ValueError as exc:
            raise OrnithGpuBurstError(f"non-numeric nvidia-smi memory row: {rows[0]!r}") from exc
        import math
        if not all(math.isfinite(x) for x in (used, total, free)) or not 0 <= free <= total:
            raise OrnithGpuBurstError("invalid driver-aware memory.free")
        return GpuMemory(used, free, total)


class OllamaPlacementBackend:
    'Small exact Ollama placement/status adapter used only by gpu acceleration.'

    def __init__(self, policy: GpuAccelerationPolicy) -> None:
        self.policy = policy

    @staticmethod
    def _json(
        base_url: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float,
    ) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(
            base_url.rstrip("/") + path,
            data=data,
            method="GET" if data is None else "POST",
            headers={} if data is None else {"Content-Type": "application/json"},
        )
        with urlopen(request, timeout=timeout) as response:
            raw = json.loads(response.read().decode("utf-8"))
        if not isinstance(raw, dict):
            raise OrnithGpuBurstError(f"non-object Ollama response from {base_url}{path}")
        return raw

    def models(self, base_url: str) -> tuple[dict[str, Any], ...]:
        try:
            raw = self._json(base_url, "/api/ps", timeout=5.0)
        except Exception as exc:
            raise OrnithGpuBurstError(f"Ollama status unavailable at {base_url}: {exc}") from exc
        return tuple(item for item in raw.get("models", ()) if isinstance(item, dict))

    def resident(self, base_url: str, model_tag: str) -> dict[str, Any] | None:
        for item in self.models(base_url):
            name = str(item.get("name") or item.get("model") or "")
            if name == model_tag:
                return item
        return None

    def _wait(self, base_url: str, model_tag: str, *, present: bool) -> dict[str, Any] | None:
        deadline = time.monotonic() + self.policy.placement_timeout_seconds
        while True:
            item = self.resident(base_url, model_tag)
            if (item is not None) == present:
                return item
            if time.monotonic() >= deadline:
                raise OrnithGpuBurstError(
                    f"Ollama placement verification timeout: model={model_tag} present={present}"
                )
            time.sleep(self.policy.status_poll_interval_seconds)

    def place_lead(self, num_gpu: int) -> dict[str, Any]:
        'Pin the selected placement only for the surrounding gpu acceleration burst.'
        payload = {
            "model": self.policy.model_tag,
            "messages": [],
            "stream": False,
            "keep_alive": -1,
            "options": {"num_gpu": int(num_gpu), "num_ctx": self.policy.num_ctx},
        }
        self._json(
            self.policy.lead_base_url,
            "/api/chat",
            payload,
            timeout=self.policy.placement_timeout_seconds,
        )
        item = self._wait(self.policy.lead_base_url, self.policy.model_tag, present=True)
        assert item is not None
        return item

    def unload(self, base_url: str, model_tag: str) -> None:
        """Fully release Ollama residency (VRAM and host RAM) and verify absence."""
        # Avoid a needless control request when no runner is resident.
        if self.resident(base_url, model_tag) is None:
            return
        payload = {
            "model": model_tag,
            "messages": [],
            "stream": False,
            "keep_alive": 0,
        }
        self._json(base_url, "/api/chat", payload, timeout=self.policy.placement_timeout_seconds)
        self._wait(base_url, model_tag, present=False)


class OrnithGpuBurstManager:
    def __init__(
        self,
        root: str | Path,
        *,
        policy: GpuAccelerationPolicy | None = None,
        lease: GpuHeavyLease | None = None,
        backend: OllamaPlacementBackend | None = None,
        memory_probe: NvidiaSmiMemoryProbe | None = None,
        known_worker_tags: tuple[str, ...] = (),
    ) -> None:
        self.root = Path(root).resolve()
        self.policy = policy or GpuAccelerationPolicy.from_project_root(self.root)
        if lease is None:
            coordination_policy = GpuCoordinationPolicy.from_project_root(self.root)
            from harness.runtime_resources.gpu_coordination.guard import current_lease
            installed_lease = current_lease()
            lease = installed_lease if (
                installed_lease is not None
                and installed_lease.path.resolve() == coordination_policy.lock_path().resolve()
            ) else GpuHeavyLease(
                coordination_policy.lock_path(), poll_interval_seconds=coordination_policy.poll_interval_seconds
            )
        self.lease = lease
        self.backend = backend or OllamaPlacementBackend(self.policy)
        self.memory_probe = memory_probe or NvidiaSmiMemoryProbe()
        self.known_worker_tags = tuple(known_worker_tags)
        self._local = threading.local()
        self._poisoned = False
        self._metrics_lock = threading.Lock()
        self._metrics = {
            "gpu_bursts": 0,
            "cpu_bursts": 0,
            "cpu_fallbacks": 0,
            "lease_busy_fallbacks": 0,
            "foreign_gpu_fallbacks": 0,
            "floor_fallbacks": 0,
            "placement_failures": 0,
            "reclaimed_worker_models": 0,
            "full_unloads": 0,
            "cleanup_failures": 0,
        }

    @property
    def gpu_active(self) -> bool:
        return bool(getattr(self._local, "gpu_active", False))

    @property
    def current_num_gpu(self) -> int:
        return self.policy.gpu_num_gpu if self.gpu_active else self.policy.cpu_num_gpu

    @contextmanager
    def require_gpu(self) -> Iterator[None]:
        """Scoped diagnostic contract; ordinary production fallback is unchanged."""
        previous = bool(getattr(self._local, "gpu_required", False))
        if int(getattr(self._local, "depth", 0)) and not self.gpu_active:
            raise OrnithGpuBurstError("GPU_REQUIRED: active CPU burst")
        self._local.gpu_required = True
        try:
            yield
        finally:
            self._local.gpu_required = previous

    def metrics(self) -> dict[str, int]:
        with self._metrics_lock:
            return dict(self._metrics)

    def _record(self, key: str, amount: int = 1) -> None:
        with self._metrics_lock:
            self._metrics[key] += amount

    @staticmethod
    def _size_vram_mib(item: dict[str, Any] | None) -> float:
        return float((item or {}).get("size_vram") or 0) / (1024.0 * 1024.0)

    @staticmethod
    def _gpu_residents(items: tuple[dict[str, Any], ...]) -> tuple[dict[str, Any], ...]:
        return tuple(item for item in items if int(item.get("size_vram") or 0) > 0)

    def _reclaim_idle_workers(self) -> tuple[str, ...]:
        try:
            residents = self._gpu_residents(self.backend.models(self.policy.worker_base_url))
        except OrnithGpuBurstError:
            # An offline worker endpoint cannot own resident VRAM.
            return ()
        if not residents:
            return ()
        known = set(self.known_worker_tags)
        unknown = [
            str(item.get("name") or item.get("model") or "")
            for item in residents
            if str(item.get("name") or item.get("model") or "") not in known
        ]
        if unknown or not self.policy.reclaim_known_idle_workers:
            raise OrnithGpuBurstError(
                "worker GPU has unknown/non-reclaimable resident model(s): "
                + ", ".join(unknown or ["configured worker"])
            )
        reclaimed: list[str] = []
        for item in residents:
            tag = str(item.get("name") or item.get("model") or "")
            self.backend.unload(self.policy.worker_base_url, tag)
            reclaimed.append(tag)
        if reclaimed:
            self._record("reclaimed_worker_models", len(reclaimed))
        return tuple(reclaimed)

    def _foreign_gpu_is_resident(self) -> bool:
        for base_url in self.policy.external_base_urls:
            try:
                residents = self._gpu_residents(self.backend.models(base_url))
            except OrnithGpuBurstError:
                # Offline external Ollama means no resident model on that endpoint.
                continue
            if residents:
                return True
        return False

    def _full_unload_lead(self) -> bool:
        """Enforce the post-burst invariant: Ornith must be absent from /api/ps."""
        try:
            was_resident = self.backend.resident(self.policy.lead_base_url, self.policy.model_tag)
            self.backend.unload(self.policy.lead_base_url, self.policy.model_tag)
            absent = self.backend.resident(self.policy.lead_base_url, self.policy.model_tag) is None
            if absent and was_resident is not None:
                self._record("full_unloads")
            return absent
        except Exception:
            return False

    def _cleanup_or_raise(self, *, hold_gpu_lease: bool, context: str) -> bool:
        'Return True when caller may release the GPU lease.\n\n        A GPU cleanup failure is fail-closed: the lease is retained and the manager\n        is poisoned so no subsequent operation can incorrectly assume the GPU is\n        free. A CPU cleanup failure cannot retain a lease that was never held, but\n        still poisons the manager and raises immediately because host-RAM cleanup is\n        part of the gpu acceleration contract.\n        '
        if self._full_unload_lead():
            return True
        self._record("cleanup_failures")
        self._poisoned = True
        if hold_gpu_lease and self.policy.fail_closed_cleanup:
            raise OrnithGpuBurstCleanupError(
                f"{context}: Ornith did not fully unload; GPU_HEAVY lease retained fail-closed"
            )
        raise OrnithGpuBurstCleanupError(
            f"{context}: Ornith did not fully unload from Ollama host RAM"
        )

    @contextmanager
    def _cpu_burst(self, reason: str) -> Iterator[BurstState]:
        """CPU fallback with bounded residency and mandatory full unload."""
        self._record("cpu_bursts")
        state = BurstState("cpu", self.policy.cpu_num_gpu, reason)
        self._local.depth, self._local.gpu_active, self._local.state = 1, False, state
        try:
            yield state
        finally:
            self._local.depth = 0
            self._local.gpu_active = False
            self._cleanup_or_raise(hold_gpu_lease=False, context="CPU burst cleanup")

    @contextmanager
    def burst(self, owner: str = "ornith") -> Iterator[BurstState]:
        if self._poisoned:
            raise OrnithGpuBurstCleanupError(
                "Ornith GPU burst manager is poisoned by a prior cleanup failure"
            )

        # Nested calls (for example ReliableJsonRuntime retry -> OrnithRuntime)
        # share one residency transaction and therefore do not reload the model.
        depth = int(getattr(self._local, "depth", 0))
        if depth > 0:
            self._local.depth = depth + 1
            try:
                yield getattr(self._local, "state")
            finally:
                self._local.depth -= 1
            return

        lease_cm = self.lease.held(
            f"swarm-ornith:{owner}",
            timeout_seconds=self.policy.opportunistic_acquire_timeout_seconds,
        )
        try:
            lease_cm.__enter__()
        except GpuHeavyLeaseTimeout:
            if getattr(self._local, "gpu_required", False):
                raise OrnithGpuBurstError("GPU_REQUIRED: GPU lease unavailable")
            self._record("cpu_fallbacks")
            self._record("lease_busy_fallbacks")
            with self._cpu_burst("gpu_lease_busy") as state:
                yield state
            return

        release_lease = True
        try:
            try:
                if self._foreign_gpu_is_resident():
                    raise OrnithGpuBurstError("external GPU model resident")
                reclaimed = self._reclaim_idle_workers()
                if self.policy.memory_policy == MEMORY_POLICY:
                    require_model_capacity(self.memory_probe.sample().free_mib)
                item = self.backend.place_lead(self.policy.gpu_num_gpu)
                size_vram = self._size_vram_mib(item)
                memory = self.memory_probe.sample()
                if size_vram <= 0:
                    raise OrnithGpuBurstError("num_gpu=20 placement reported size_vram=0")
                if self.policy.memory_policy == MEMORY_POLICY:
                    rows = []
                    for endpoint in dict.fromkeys((self.policy.lead_base_url, self.policy.worker_base_url)):
                        rows.extend(self.backend.models(endpoint))
                    resident_budget(rows)
                if self.policy.memory_policy != MEMORY_POLICY and memory.free_mib < self.policy.min_free_vram_mib:
                    self._record("floor_fallbacks")
                    raise OrnithGpuBurstError(
                        f"post-placement free VRAM {memory.free_mib:.0f} MiB < "
                        f"{self.policy.min_free_vram_mib} MiB"
                    )
            except Exception as exc:
                self._record("cpu_fallbacks")
                if "external GPU model resident" in str(exc) or "unknown/non-reclaimable" in str(exc):
                    self._record("foreign_gpu_fallbacks")
                else:
                    self._record("placement_failures")

                # If a partial GPU placement happened, it must be fully unloaded
                # before the machine-wide lease is released and CPU fallback begins.
                if not self._full_unload_lead():
                    self._record("cleanup_failures")
                    self._poisoned = True
                    if self.policy.fail_closed_cleanup:
                        release_lease = False
                        raise OrnithGpuBurstCleanupError(
                            f"failed GPU admission and could not fully unload Ornith: {exc}; "
                            "GPU_HEAVY lease retained fail-closed"
                        ) from exc
                    raise OrnithGpuBurstCleanupError(
                        f"failed GPU admission and could not fully unload Ornith: {exc}"
                    ) from exc

                lease_cm.__exit__(None, None, None)
                release_lease = False
                if getattr(self._local, "gpu_required", False):
                    raise OrnithGpuBurstError(f"GPU_REQUIRED: GPU admission failed: {exc}") from exc
                with self._cpu_burst(f"gpu_unavailable:{exc}") as state:
                    yield state
                return

            self._record("gpu_bursts")
            state = BurstState(
                "gpu",
                self.policy.gpu_num_gpu,
                "gpu_lease_acquired",
                free_vram_mib=memory.free_mib,
                size_vram_mib=size_vram,
                reclaimed_worker_models=reclaimed,
            )
            self._local.depth, self._local.gpu_active, self._local.state = 1, True, state
            try:
                yield state
            finally:
                self._local.depth = 0
                self._local.gpu_active = False
                if not self._full_unload_lead():
                    self._record("cleanup_failures")
                    self._poisoned = True
                    if self.policy.fail_closed_cleanup:
                        release_lease = False
                        raise OrnithGpuBurstCleanupError(
                            "Ornith GPU burst cleanup did not fully unload the model; "
                            "GPU_HEAVY lease retained fail-closed"
                        )
                    raise OrnithGpuBurstCleanupError(
                        "Ornith GPU burst cleanup did not fully unload the model"
                    )
        finally:
            if release_lease:
                lease_cm.__exit__(None, None, None)


def _rewrite_request_num_gpu(request: Request, num_gpu: int, lead_base_url: str) -> Request:
    url = getattr(request, "full_url", "")
    if (
        not url.startswith(lead_base_url.rstrip("/") + "/")
        or not url.endswith("/api/chat")
        or request.data is None
    ):
        return request
    payload = json.loads(request.data.decode("utf-8"))
    if not isinstance(payload, dict):
        return request
    options = dict(payload.get("options")) if isinstance(payload.get("options"), dict) else {}
    options["num_gpu"] = int(num_gpu)
    payload["options"] = options
    return Request(
        url,
        data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        headers=dict(request.header_items()),
        method=request.get_method(),
    )


def _known_worker_tags(root: Path) -> tuple[str, ...]:
    from harness.config import ConfigLoader
    from harness.models import ModelRegistry

    registry = ModelRegistry.from_loader(ConfigLoader(root))
    tags: list[str] = []
    for logical_id in registry.logical_ids:
        definition = registry.get(logical_id)
        if (
            definition.server == "worker"
            and definition.runtime.get("execution") == "gpu_worker"
            and definition.model_tag_known
        ):
            tags.append(definition.ollama_model)
    return tuple(sorted(set(tags)))


def _install_adaptive_gpu_layers(manager: OrnithGpuBurstManager) -> None:
    from harness.runtime_hardening import transport

    original = transport._harden_request
    _ORIGINALS["phase20r._harden_request"] = original

    def adaptive(request, policy):
        hardened = original(request, policy)
        return _rewrite_request_num_gpu(
            hardened, manager.current_num_gpu, manager.policy.lead_base_url
        )

    transport._harden_request = adaptive


def _install_call_boundaries(manager: OrnithGpuBurstManager) -> None:
    from harness.lead.runtime import OrnithRuntime
    from harness.research_reasoning.runtime import ReliableJsonRuntime

    original_ornith = OrnithRuntime.complete
    original_json = ReliableJsonRuntime.complete
    _ORIGINALS["OrnithRuntime.complete"] = original_ornith
    _ORIGINALS["ReliableJsonRuntime.complete"] = original_json

    def ornith_complete(self, *args, **kwargs):
        with manager.burst("runtime-complete"):
            return original_ornith(self, *args, **kwargs)

    def reliable_complete(self, *args, **kwargs):
        # Retries performed inside ReliableJsonRuntime share this outer burst;
        # nested OrnithRuntime.complete calls therefore do not reload the model.
        with manager.burst("structured-complete"):
            return original_json(self, *args, **kwargs)

    OrnithRuntime.complete = ornith_complete
    ReliableJsonRuntime.complete = reliable_complete


def _install_global_vram_reserve_overlay(policy: GpuAccelerationPolicy) -> None:
    """Overlay the effective machine GPU-heavy reserve without editing frozen YAML."""
    from harness.resource_manager.manager import ResourceManager
    from harness.router.advanced_router import RouterV2Policy

    original_property = ResourceManager.worker_vram_policy
    original_router_loader = RouterV2Policy.from_project_root.__func__
    _ORIGINALS["ResourceManager.worker_vram_policy"] = original_property
    _ORIGINALS["RouterV2Policy.from_project_root"] = original_router_loader

    def worker_policy(self):
        current = dict(original_property.__get__(self, type(self)))
        if policy.memory_policy == MEMORY_POLICY:
            current["min_free_after_load_gb"] = 0.0
            current["memory_policy"] = MEMORY_POLICY
            return MappingProxyType(current)
        current["min_free_after_load_gb"] = max(
            float(current.get("min_free_after_load_gb", 0.0)), policy.min_free_vram_gb
        )
        return MappingProxyType(current)

    @classmethod
    def router_loader(cls, root):
        current = original_router_loader(cls, root)
        if policy.memory_policy == MEMORY_POLICY:
            return replace(current, minimum_free_vram_reserve_gb=0.0)
        return replace(
            current,
            minimum_free_vram_reserve_gb=max(
                float(current.minimum_free_vram_reserve_gb), policy.min_free_vram_gb
            ),
        )

    ResourceManager.worker_vram_policy = property(worker_policy)
    RouterV2Policy.from_project_root = router_loader


def install_runtime_acceleration(
    root: str | Path,
    *,
    policy: GpuAccelerationPolicy | None = None,
    manager: OrnithGpuBurstManager | None = None,
) -> OrnithGpuBurstManager:
    global _INSTALLED, _MANAGER
    if _INSTALLED:
        assert _MANAGER is not None
        return _MANAGER
    root = Path(root).resolve()
    policy = policy or GpuAccelerationPolicy.from_project_root(root)

    # Phase20R remains byte-for-byte frozen. Install its wrapper first, then add
    # a dynamic resource-only num_gpu overlay for the active gpu acceleration burst.
    from harness.runtime_hardening.transport import install_runtime_hardening

    install_runtime_hardening(root)

    manager = manager or OrnithGpuBurstManager(
        root,
        policy=policy,
        known_worker_tags=_known_worker_tags(root),
    )
    _install_adaptive_gpu_layers(manager)
    _install_call_boundaries(manager)
    _install_global_vram_reserve_overlay(policy)
    _MANAGER = manager
    _INSTALLED = True
    return manager


def current_manager() -> OrnithGpuBurstManager | None:
    return _MANAGER


def install_from_environment() -> OrnithGpuBurstManager | None:
    root_text = os.getenv("SWARM_ORNITH_GPU_ROOT", "").strip()
    if not root_text:
        return None
    policy = GpuAccelerationPolicy.from_project_root(root_text)
    if not policy.enabled():
        return None
    return install_runtime_acceleration(root_text, policy=policy)

# Verified compatibility aliases for historical Python interfaces.
_install_phase20r_adaptive_num_gpu = _install_adaptive_gpu_layers

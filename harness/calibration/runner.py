"""Controlled one-model calibration; deliberately independent from Router."""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timezone
from typing import Callable

from ..errors import ExecutionError, LockBusyError
from ..executor.client import OllamaNativeClient
from ..executor.lifecycle import LifecycleVerificationPolicy, WorkerLifecycleManager
from ..executor.models import ExecutionMessage
from ..models import LoadedModel, ModelDefinition, ModelRegistry
from ..resource_manager.locks import LockState
from ..resource_manager.manager import ResourceManager
from ..resource_manager.snapshot import ResourceSnapshot
from .models import CalibrationObservation, CalibrationOutcome, CalibrationRequest


class CalibrationPrecheckError(ExecutionError):
    pass


class CalibrationCleanupError(ExecutionError):
    """Preserves both the failed calibration operation and its cleanup failure."""

    def __init__(self, primary_error: Exception | None, cleanup_error: Exception) -> None:
        message = "calibration cleanup failed" if primary_error is None else "calibration failed and target cleanup also failed"
        super().__init__(message, failure_context={"primary_error": primary_error, "cleanup_error": cleanup_error})
        self.primary_error = primary_error
        self.cleanup_error = cleanup_error


class CalibrationRunner:
    """Run one explicit calibration point; never route, substitute, or tune config."""

    def __init__(self, registry: ModelRegistry, resources: ResourceManager, client: OllamaNativeClient, snapshot_provider: Callable[[], ResourceSnapshot], policies: dict, verification: LifecycleVerificationPolicy | None = None) -> None:
        self.registry, self.resources, self.client, self.snapshot_provider = registry, resources, client, snapshot_provider
        self.policies = policies.get("calibration", {})
        self.lifecycle = WorkerLifecycleManager(registry, resources, client, snapshot_provider, verification or LifecycleVerificationPolicy.from_policies(policies))

    def run(self, request: CalibrationRequest) -> CalibrationObservation:
        definition, _preflight = self._precheck(request)
        server = self.registry.get_server(definition.server)
        try:
            with ExitStack() as locks:
                locks.enter_context(self.resources.locks.gpu_worker_lock.held(definition.logical_id))
                if definition.resources.get("heavy_cpu_offload"):
                    locks.enter_context(self.resources.locks.cpu_bandwidth_lock.held(definition.logical_id))
                if self.policies.get("require_clean_worker", True) and self._worker_is_dirty(server.server_id, server.ollama_base_url):
                    raise CalibrationPrecheckError("calibration_requires_clean_worker")
                before = self.snapshot_provider()
                self._validate_snapshot(before)
                self._admit(before, definition)
                return self._run_preloaded_calibration(definition, request, before)
        except LockBusyError as exc:
            reason = "gpu_worker_lock_busy" if self.resources.locks.gpu_worker_lock.state.locked else "cpu_bandwidth_lock_busy"
            raise CalibrationPrecheckError(reason) from exc

    def _run_preloaded_calibration(self, definition: ModelDefinition, request: CalibrationRequest, before: ResourceSnapshot) -> CalibrationObservation:
        """Run post-preload work and clean up only the exact calibration target."""
        server = self.registry.get_server(definition.server)
        preload_attempted = False
        preload_returned = False
        primary_error: Exception | None = None
        cleanup_error: Exception | None = None
        cleanup_verified = False
        safety_stop_reason: str | None = None
        observed_context: int | None = None
        after_load: ResourceSnapshot | None = None
        runtime: LoadedModel | None = None
        preload_response: dict | None = None
        response: dict | None = None
        try:
            preload_attempted = True
            preload_response = self.client.preload_model(server.ollama_base_url, definition.ollama_model, request.context_length, num_gpu=request.num_gpu)
            preload_returned = True
            observed_context = self.lifecycle._verify_loaded(server.server_id, server.ollama_base_url, definition, request.context_length)
            after_load = self.snapshot_provider()
            runtime = self._find_loaded(server.server_id, server.ollama_base_url, definition.ollama_model)
            if runtime is None:
                raise CalibrationPrecheckError("calibration target disappeared after verified load")
            safety_stop_reason = self._post_load_stop_reason(runtime, after_load)
            if safety_stop_reason is None:
                response = self.client.chat(
                    server.ollama_base_url,
                    definition.ollama_model,
                    (ExecutionMessage("user", str(self.policies.get("generation_prompt", "Reply with exactly: CALIBRATION_OK"))),),
                    -1,
                    request.context_length,
                    num_gpu=request.num_gpu,
                )
        except Exception as exc:
            primary_error = exc
        finally:
            cleanup_required = preload_returned or (preload_attempted and self._target_is_observed_resident(server.server_id, server.ollama_base_url, definition.ollama_model))
            if cleanup_required and self.policies.get("unload_after_run", True):
                try:
                    self.client.unload_model(server.ollama_base_url, definition.ollama_model)
                    self.lifecycle._verify_unloaded(server.server_id, server.ollama_base_url, definition)
                    cleanup_verified = True
                except Exception as exc:
                    cleanup_error = exc
        if primary_error is not None:
            if cleanup_error is not None:
                raise CalibrationCleanupError(primary_error, cleanup_error) from primary_error
            raise primary_error
        if cleanup_error is not None:
            raise CalibrationCleanupError(None, cleanup_error) from cleanup_error
        assert observed_context is not None and after_load is not None and runtime is not None and preload_response is not None
        after_cleanup = self.snapshot_provider()
        if safety_stop_reason is not None:
            return self._observation(definition, request, observed_context, before, after_load, after_cleanup, runtime, preload_response, None, cleanup_verified, CalibrationOutcome.SAFETY_STOP, safety_stop_reason)
        assert response is not None
        return self._observation(definition, request, observed_context, before, after_load, after_cleanup, runtime, preload_response, response, cleanup_verified, CalibrationOutcome.SUCCESS, None)
    def _post_load_stop_reason(self, runtime: LoadedModel, after_load: ResourceSnapshot) -> str | None:
        """Return one deterministic post-load safety reason before generation."""
        policy = self.resources.worker_vram_policy
        if runtime.vram_gb is not None and runtime.vram_gb > float(policy["hard_limit_gb"]):
            return "observed_vram_exceeds_hard_limit"
        if after_load.gpu is not None and after_load.gpu.vram_free_gb < float(policy["min_free_after_load_gb"]):
            return "post_load_gpu_reserve_breached"
        if after_load.ram is not None and after_load.ram.available_gb < float(self.policies.get("minimum_available_ram_gb", 20)):
            return "post_load_ram_floor_breached"
        if after_load.gpu is None or after_load.ram is None:
            return "post_load_resource_state_unavailable"
        return None
    def _precheck(self, request: CalibrationRequest) -> tuple[ModelDefinition, ResourceSnapshot]:
        if request.logical_model_id == "ornith_lead":
            raise CalibrationPrecheckError("Ornith is not a calibration worker")
        try:
            definition = self.registry.get(request.logical_model_id)
        except KeyError as exc:
            raise CalibrationPrecheckError("unknown calibration model") from exc
        if definition.runtime.get("execution") != "gpu_worker" or not definition.model_tag_known:
            raise CalibrationPrecheckError("calibration target must be a configured GPU worker with known tag")
        supported = definition.context.get("supported_max")
        if isinstance(supported, int) and not isinstance(supported, bool) and request.context_length > supported:
            raise CalibrationPrecheckError("requested context exceeds known supported limit")
        snapshot = self.snapshot_provider()
        self._validate_snapshot(snapshot)
        if self.resources.locks.gpu_worker_lock.state.locked:
            raise CalibrationPrecheckError("gpu_worker_lock_busy")
        if definition.resources.get("heavy_cpu_offload") and self.resources.locks.cpu_bandwidth_lock.state.locked:
            raise CalibrationPrecheckError("cpu_bandwidth_lock_busy")
        server = self.registry.get_server(definition.server)
        if self.policies.get("require_clean_worker", True) and self._worker_is_dirty(server.server_id, server.ollama_base_url):
            raise CalibrationPrecheckError("calibration_requires_clean_worker")
        return definition, snapshot

    def _validate_snapshot(self, snapshot: ResourceSnapshot) -> None:
        if not snapshot.worker_online:
            raise CalibrationPrecheckError("worker_server_offline")
        if snapshot.gpu is None or snapshot.ram is None:
            raise CalibrationPrecheckError("calibration requires GPU and RAM snapshots")
        if snapshot.ram.available_gb < float(self.policies.get("minimum_available_ram_gb", 20)):
            raise CalibrationPrecheckError("calibration_insufficient_available_ram")

    def _admit(self, snapshot: ResourceSnapshot, definition: ModelDefinition) -> None:
        estimate = definition.resources.get("estimated_vram_gb")
        if isinstance(estimate, (int, float)) and not isinstance(estimate, bool):
            admission_snapshot = replace(snapshot, gpu_worker_lock=LockState(False, None), cpu_bandwidth_lock=LockState(False, None))
            admitted = self.resources.can_start_heavy_inference(admission_snapshot, float(estimate)) if definition.resources.get("heavy_cpu_offload") else self.resources.can_use_gpu_worker(admission_snapshot, float(estimate))
            if not admitted.allowed:
                raise CalibrationPrecheckError(f"calibration_resource_admission_failed:{admitted.reason}")

    def _worker_is_dirty(self, server_id: str, base_url: str) -> bool:
        """A calibration baseline is clean only when worker /api/ps is literally empty."""
        return bool(self.client.get_loaded_models(server_id, base_url))

    def _target_is_observed_resident(self, server_id: str, base_url: str, tag: str) -> bool:
        try:
            return self._find_loaded(server_id, base_url, tag) is not None
        except Exception:
            return False

    def _find_loaded(self, server_id: str, base_url: str, tag: str) -> LoadedModel | None:
        return next((item for item in self.client.get_loaded_models(server_id, base_url) if tag in (item.name, item.model)), None)

    @staticmethod
    def _delta(after: float | None, before: float | None) -> float | None:
        return None if after is None or before is None else round(after - before, 6)

    @staticmethod
    def _integer(response: dict, key: str) -> int | None:
        value = response.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    def _observation(self, definition: ModelDefinition, request: CalibrationRequest, observed_context: int, before: ResourceSnapshot, after_load: ResourceSnapshot, after_cleanup: ResourceSnapshot, runtime: LoadedModel, preload_response: dict, response: dict | None, cleanup_verified: bool, outcome: CalibrationOutcome, stop_reason: str | None) -> CalibrationObservation:
        before_gpu_used, after_gpu_used = (before.gpu.vram_used_gb if before.gpu else None), (after_load.gpu.vram_used_gb if after_load.gpu else None)
        before_gpu_free, after_gpu_free = (before.gpu.vram_free_gb if before.gpu else None), (after_load.gpu.vram_free_gb if after_load.gpu else None)
        before_ram, after_ram = (before.ram.used_gb if before.ram else None), (after_load.ram.used_gb if after_load.ram else None)
        eval_count = self._integer(response, "eval_count") if response is not None else None
        eval_duration = self._integer(response, "eval_duration") if response is not None else None
        tokens_per_second = None if eval_count is None or eval_duration is None or eval_duration <= 0 else eval_count / (eval_duration / 1_000_000_000)
        return CalibrationObservation(definition.logical_id, definition.ollama_model, request.context_length, request.num_gpu, observed_context, outcome, stop_reason, response is not None, before_gpu_used, before_gpu_free, runtime.vram_gb, after_gpu_used, after_gpu_free, self._delta(after_gpu_used, before_gpu_used), before_ram, after_ram, self._delta(after_ram, before_ram), self._integer(preload_response, "load_duration"), self._integer(response, "prompt_eval_count") if response is not None else None, eval_count, eval_duration, self._integer(response, "total_duration") if response is not None else None, tokens_per_second, cleanup_verified, datetime.now(timezone.utc).isoformat())
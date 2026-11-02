"""TOCTOU-safe worker preparation; routing is deliberately absent."""
from __future__ import annotations

import math
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from itertools import combinations
from typing import Callable, Iterator

from ..errors import ExecutionError, LifecycleVerificationError, LockBusyError, StaleRoutingDecisionError
from ..models import LoadedModel, ModelDefinition, ModelRegistry
from ..resource_manager.locks import LockState
from ..resource_manager.manager import ResourceManager
from ..resource_manager.snapshot import ResourceSnapshot
from ..router.scoring import DecisionType, ResourceFeasibilityMode, RoutingDecision
from .client import OllamaNativeClient
from .models import ExecutionFailureContext, ExecutionStage, LifecycleAction, LifecycleActionKind


@dataclass(frozen=True, slots=True)
class LifecycleVerificationPolicy:
    timeout_seconds: float = 10.0
    poll_interval_seconds: float = 0.2

    def __post_init__(self) -> None:
        if any(not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) for value in (self.timeout_seconds, self.poll_interval_seconds)) or self.timeout_seconds <= 0 or self.poll_interval_seconds <= 0 or self.poll_interval_seconds > self.timeout_seconds:
            raise ValueError("Lifecycle verification timeout/interval must be finite positive values with interval <= timeout")

    @classmethod
    def from_policies(cls, policies: dict) -> "LifecycleVerificationPolicy":
        values = policies.get("executor", {})
        return cls(float(values.get("lifecycle_verification_timeout_seconds", 10.0)), float(values.get("lifecycle_verification_poll_interval_seconds", 0.2)))


@dataclass(frozen=True, slots=True)
class PreparedWorker:
    model_id: str
    model_tag: str
    base_url: str
    actions: tuple[LifecycleAction, ...]
    before: ResourceSnapshot
    requested_context_length: int
    observed_context_length: int
    configured_num_gpu: int | None


class WorkerLifecycleManager:
    """Prepare exactly the selected GPU worker; never route or substitute."""

    def __init__(self, registry: ModelRegistry, resources: ResourceManager, client: OllamaNativeClient, snapshot_provider: Callable[[], ResourceSnapshot], verification: LifecycleVerificationPolicy | None = None, *, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> None:
        self.registry, self.resources, self.client, self.snapshot_provider = registry, resources, client, snapshot_provider
        self.verification, self._clock, self._sleep = verification or LifecycleVerificationPolicy(), clock, sleep

    @contextmanager
    def prepared(self, decision: RoutingDecision) -> Iterator[PreparedWorker]:
        model_id, mode = self._validate_decision(decision)
        definition, actions, before = self.registry.get(model_id), [], self.snapshot_provider()
        try:
            with self.resources.locks.gpu_worker_lock.held(model_id):
                if definition.resources.get("heavy_cpu_offload"):
                    with self.resources.locks.cpu_bandwidth_lock.held(model_id):
                        yield self._prepare_locked(decision, definition, self.snapshot_provider(), mode, actions)
                else:
                    yield self._prepare_locked(decision, definition, self.snapshot_provider(), mode, actions)
        except LockBusyError as exc:
            raise StaleRoutingDecisionError(f"REPLAN_REQUIRED: worker lock unavailable: {exc}") from exc
        except ExecutionError as exc:
            if not isinstance(exc, StaleRoutingDecisionError):
                self._attach_failure_context(exc, getattr(exc, "_execution_stage", ExecutionStage.PREFLIGHT), definition.logical_id, definition.ollama_model, actions)
            raise

    def mark_failure(self, error: ExecutionError, stage: ExecutionStage, prepared: PreparedWorker) -> None:
        error._execution_stage, error._execution_model_id, error._execution_model_tag = stage, prepared.model_id, prepared.model_tag

    def _attach_failure_context(self, error: ExecutionError, stage: ExecutionStage, model_id: str | None, model_tag: str | None, actions: list[LifecycleAction]) -> None:
        if error.failure_context is None:
            error.failure_context = ExecutionFailureContext(tuple(actions), self.snapshot_provider(), getattr(error, "_execution_model_id", model_id), getattr(error, "_execution_model_tag", model_tag), stage)

    def _validate_decision(self, decision: RoutingDecision) -> tuple[str, ResourceFeasibilityMode]:
        if decision.decision_type is not DecisionType.WORKER or not decision.selected_model:
            raise StaleRoutingDecisionError("REPLAN_REQUIRED: decision is not a worker selection")
        if decision.selected_model == "ornith_lead":
            raise StaleRoutingDecisionError("REPLAN_REQUIRED: Ornith is not a lifecycle worker")
        try: definition = self.registry.get(decision.selected_model)
        except KeyError as exc: raise StaleRoutingDecisionError("REPLAN_REQUIRED: selected worker is unknown") from exc
        if definition.runtime.get("execution") != "gpu_worker": raise StaleRoutingDecisionError("REPLAN_REQUIRED: selected model is not a GPU worker")
        if not definition.model_tag_known: raise StaleRoutingDecisionError("REPLAN_REQUIRED: selected worker has TODO model tag")
        try: return definition.logical_id, ResourceFeasibilityMode((decision.resource_admission or {}).get("mode"))
        except ValueError as exc: raise StaleRoutingDecisionError("REPLAN_REQUIRED: missing or invalid resource feasibility mode") from exc

    def _prepare_locked(self, decision: RoutingDecision, definition: ModelDefinition, before: ResourceSnapshot, mode: ResourceFeasibilityMode, actions: list[LifecycleAction]) -> PreparedWorker:
        server, required_context, stage = self.registry.get_server(definition.server), self._required_context(definition), ExecutionStage.PREFLIGHT
        try:
            loaded = self.client.get_loaded_models(definition.server, server.ollama_base_url)
            target = self._target_loaded_model(loaded, definition.ollama_model)
            if mode is ResourceFeasibilityMode.REUSE_LOADED:
                if target is None: raise StaleRoutingDecisionError("REPLAN_REQUIRED: target is no longer loaded")
                requirement = self._current_requirement(definition, target)
                self._revalidate_reuse(before, requirement)
                self._ensure_decision_requirement(decision, requirement)
                if target.context_length != required_context: raise StaleRoutingDecisionError("REPLAN_REQUIRED: resident_context_mismatch")
                observed_context = target.context_length
            elif mode is ResourceFeasibilityMode.LOAD_DIRECT:
                if target is not None: raise StaleRoutingDecisionError("REPLAN_REQUIRED: target became resident; direct-load plan is stale")
                requirement = self._configured_requirement(definition)
                self._ensure_decision_requirement(decision, requirement)
                self._require_direct_admission(before, requirement, definition)
                stage = ExecutionStage.PRELOAD
                actions.append(LifecycleAction(LifecycleActionKind.PRELOAD, definition.logical_id, definition.ollama_model))
                self.client.preload_model(server.ollama_base_url, definition.ollama_model, required_context, num_gpu=self._configured_num_gpu(definition))
                stage = ExecutionStage.PRELOAD_VERIFICATION
                observed_context = self._verify_loaded(server.server_id, server.ollama_base_url, definition, required_context)
            elif mode is ResourceFeasibilityMode.SWITCH_REQUIRED:
                if target is not None: raise StaleRoutingDecisionError("REPLAN_REQUIRED: target is already resident; switch is stale")
                requirement = self._configured_requirement(definition)
                self._ensure_decision_requirement(decision, requirement)
                victims = self._select_victims(loaded, definition, self._switch_required_vram(before, requirement))
                if not victims: raise StaleRoutingDecisionError("REPLAN_REQUIRED: no configured idle workers can safely satisfy switch")
                for victim in victims:
                    stage = ExecutionStage.UNLOAD
                    actions.append(LifecycleAction(LifecycleActionKind.UNLOAD, victim.logical_id, victim.ollama_model))
                    self.client.unload_model(server.ollama_base_url, victim.ollama_model)
                    stage = ExecutionStage.UNLOAD_VERIFICATION
                    self._verify_unloaded(server.server_id, server.ollama_base_url, victim)
                stage = ExecutionStage.PRELOAD
                actions.append(LifecycleAction(LifecycleActionKind.PRELOAD, definition.logical_id, definition.ollama_model))
                self.client.preload_model(server.ollama_base_url, definition.ollama_model, required_context, num_gpu=self._configured_num_gpu(definition))
                stage = ExecutionStage.PRELOAD_VERIFICATION
                observed_context = self._verify_loaded(server.server_id, server.ollama_base_url, definition, required_context)
            else: raise StaleRoutingDecisionError("REPLAN_REQUIRED: decision is not feasible")
        except ExecutionError as exc:
            exc._execution_stage = stage
            raise
        return PreparedWorker(definition.logical_id, definition.ollama_model, server.ollama_base_url, tuple(actions), before, required_context, observed_context, self._configured_num_gpu(definition))

    @staticmethod
    def _configured_num_gpu(definition: ModelDefinition) -> int | None:
        value = definition.runtime.get("num_gpu")
        if value is None:
            return None
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise StaleRoutingDecisionError("REPLAN_REQUIRED: invalid configured num_gpu")
        return value
    @staticmethod
    def _required_context(definition: ModelDefinition) -> int:
        value, maximum = definition.context.get("default"), definition.context.get("operational_max")
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0 or not isinstance(maximum, int) or isinstance(maximum, bool) or value > maximum:
            raise StaleRoutingDecisionError("REPLAN_REQUIRED: invalid configured execution context")
        return value

    @staticmethod
    def _target_loaded_model(models: tuple[LoadedModel, ...], tag: str) -> LoadedModel | None:
        return next((item for item in models if tag in (item.name, item.model)), None)

    @staticmethod
    def _configured_requirement(definition: ModelDefinition) -> float:
        value = definition.resources.get("estimated_vram_gb")
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0: raise StaleRoutingDecisionError("REPLAN_REQUIRED: current resource estimate is unknown")
        return float(value)

    def _current_requirement(self, definition: ModelDefinition, runtime_model: LoadedModel) -> float:
        return runtime_model.vram_gb if runtime_model.vram_gb is not None else self._configured_requirement(definition)

    @staticmethod
    def _ensure_decision_requirement(decision: RoutingDecision, current_requirement: float) -> None:
        planned = (decision.resource_admission or {}).get("requested_vram_gb")
        if not isinstance(planned, (int, float)) or isinstance(planned, bool) or abs(float(planned) - current_requirement) > 1e-9: raise StaleRoutingDecisionError("REPLAN_REQUIRED: stale_resource_estimate")

    @staticmethod
    def _admission_snapshot(snapshot: ResourceSnapshot) -> ResourceSnapshot:
        return replace(snapshot, gpu_worker_lock=LockState(False, None), cpu_bandwidth_lock=LockState(False, None))

    def _revalidate_reuse(self, snapshot: ResourceSnapshot, requirement: float) -> None:
        policy = self.resources.worker_vram_policy
        if snapshot.gpu is None: raise StaleRoutingDecisionError("REPLAN_REQUIRED: gpu_unavailable")
        if requirement > float(policy["hard_limit_gb"]): raise StaleRoutingDecisionError("REPLAN_REQUIRED: observed_vram_exceeds_hard_limit")
        if snapshot.gpu.vram_free_gb < float(policy["min_free_after_load_gb"]): raise StaleRoutingDecisionError("REPLAN_REQUIRED: insufficient_free_vram_reserve")

    def _require_direct_admission(self, snapshot: ResourceSnapshot, requirement: float, definition: ModelDefinition) -> None:
        snapshot = self._admission_snapshot(snapshot)
        result = self.resources.can_start_heavy_inference(snapshot, requirement) if definition.resources.get("heavy_cpu_offload") else self.resources.can_use_gpu_worker(snapshot, requirement)
        if not result.allowed: raise StaleRoutingDecisionError(f"REPLAN_REQUIRED: {result.reason}")

    def _switch_required_vram(self, snapshot: ResourceSnapshot, requirement: float) -> float:
        if snapshot.gpu is None: raise StaleRoutingDecisionError("REPLAN_REQUIRED: switch state unavailable")
        policy = self.resources.worker_vram_policy
        if requirement > float(policy["hard_limit_gb"]): raise StaleRoutingDecisionError("REPLAN_REQUIRED: requested_vram_exceeds_hard_limit")
        return max(0.0, float(policy["min_free_after_load_gb"]) + requirement - snapshot.gpu.vram_free_gb)

    def _verify_loaded(self, server_id: str, base_url: str, definition: ModelDefinition, required_context: int) -> int:
        model = self._poll_for_model(server_id, base_url, definition.ollama_model, present=True, required_context=required_context)
        assert model is not None and model.context_length is not None
        return model.context_length

    def _verify_unloaded(self, server_id: str, base_url: str, definition: ModelDefinition) -> None:
        self._poll_for_model(server_id, base_url, definition.ollama_model, present=False, required_context=None)

    def _poll_for_model(self, server_id: str, base_url: str, tag: str, *, present: bool, required_context: int | None) -> LoadedModel | None:
        deadline = self._clock() + self.verification.timeout_seconds
        while True:
            model = self._target_loaded_model(self.client.get_loaded_models(server_id, base_url), tag)
            context_ok = required_context is None or (model is not None and model.context_length == required_context)
            if (present and model is not None and context_ok) or (not present and model is None): return model
            if self._clock() >= deadline: break
            self._sleep(min(self.verification.poll_interval_seconds, deadline - self._clock()))
        verb = "Preload" if present else "Unload"
        raise LifecycleVerificationError(f"{verb} verification timed out for {tag}")

    def _select_victims(self, loaded: tuple[LoadedModel, ...], target: ModelDefinition, required_vram_gb: float) -> tuple[ModelDefinition, ...]:
        candidates = [(definition, item.vram_gb) for item in loaded if (definition := self._configured_gpu_worker_for_loaded(item, target)) is not None and item.vram_gb is not None]
        choices = []
        for count in range(1, len(candidates) + 1):
            for combination in combinations(candidates, count):
                total = sum(item[1] for item in combination)
                if total >= required_vram_gb: choices.append((count, total, tuple(item[0] for item in combination)))
            if choices: break
        return () if not choices else min(choices, key=lambda choice: (choice[0], choice[1], tuple(item.logical_id for item in choice[2])))[2]

    def _configured_gpu_worker_for_loaded(self, loaded: LoadedModel, target: ModelDefinition) -> ModelDefinition | None:
        for logical_id in self.registry.logical_ids:
            definition = self.registry.get(logical_id)
            if definition.logical_id != target.logical_id and definition.logical_id != "ornith_lead" and definition.server == target.server and definition.runtime.get("execution") == "gpu_worker" and self._target_loaded_model((loaded,), definition.ollama_model): return definition
        return None
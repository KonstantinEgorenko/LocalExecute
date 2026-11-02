"""Pure deterministic resource-aware Router V1; no discovery, network, or model calls."""
from __future__ import annotations
from collections import Counter
from dataclasses import dataclass
from typing import Any
from ..errors import ConfigurationError
from ..models import ModelRegistry, ModelStatus
from ..resource_manager.manager import ResourceManager
from .profile import AttemptFailure, Complexity, NeedLevel, TaskProfile, TaskType
from .scoring import CandidateScore, DecisionType, ResourceFeasibility, ResourceFeasibilityMode, RoutingDecision

TERMINAL_ESCALATION_VALUES = frozenset({"stop", "ornith_judgement", "failure_report"})

@dataclass(frozen=True, slots=True)
class _CandidateContext:
    feasibility: ResourceFeasibility | None
    rejection_reasons: tuple[str, ...]

class ResourceAwareRouter:
    """Pure scoring over supplied TaskProfile and ResourceSnapshot only.

    V1 does not predict additional runtime KV-cache growth. It only reuses known
    resident allocations or projects declared model-weight allocation/switching.
    """
    def __init__(self, registry: ModelRegistry, resource_manager: ResourceManager, routing_config: dict[str, Any], policies: dict[str, Any]) -> None:
        self.registry, self.resource_manager = registry, resource_manager
        self.config, self.branches = routing_config["router_v1"], routing_config["branches"]
        self.escalation_graph, self.policies = routing_config["escalation_graph"], policies["retry_and_anti_loop"]
        self._validate_router_config()

    def route(self, profile: TaskProfile) -> RoutingDecision:
        branch = self._branch_for(profile.task_type.value)
        exhausted = self._exhausted_models(profile.previous_failures)
        escalation_from = self._escalation_source(exhausted & set(self.branches[branch]["workers"]))
        if escalation_from == "qwen27_heavy_quality":
            return RoutingDecision(DecisionType.ORCHESTRATOR, None, None, 0.0, "qwen27 escalation path exhausted", (), None, escalation_from,
                                   ("qwen27_heavy_quality has reached its retry limit in the coding branch",))
        candidates = [self._score(model_id, profile, branch, exhausted, escalation_from) for model_id in self.config["candidate_order"]]
        eligible = [candidate for candidate in candidates if candidate.eligible]
        if not eligible:
            return RoutingDecision(DecisionType.NO_ELIGIBLE_MODEL, None, None, 0.0, "no executable worker candidate", tuple(candidates), None,
                                   escalation_from, ("Review hard rejection reasons or configure model tags/resource estimates.",))
        ranked = sorted(eligible, key=lambda item: (-item.total_score, self.config["candidate_order"].index(item.model_id)))
        winner, second = ranked[0], (ranked[1].total_score if len(ranked) > 1 else None)
        confidence = self._confidence(profile.router_confidence, winner.total_score, second)
        if confidence < float(self.config["confidence_threshold"]):
            return RoutingDecision(DecisionType.ORCHESTRATOR, None, winner.total_score, confidence, "routing confidence below configured threshold",
                                   tuple(candidates), None, escalation_from, (f"best deterministic candidate is {winner.model_id}",))
        context = self._eligibility(winner.model_id, profile, branch, exhausted, escalation_from)
        warnings = list(self._warnings(winner))
        if context.feasibility and context.feasibility.mode is ResourceFeasibilityMode.SWITCH_REQUIRED:
            warnings.append("Selected worker requires replacing an idle resident GPU worker.")
        return RoutingDecision(DecisionType.WORKER, winner.model_id, winner.total_score, confidence,
                               f"{winner.model_id} scored highest among eligible {branch} workers", tuple(candidates),
                               context.feasibility.to_dict() if context.feasibility else None, escalation_from, tuple(warnings))

    def _score(self, model_id: str, profile: TaskProfile, branch: str, exhausted: set[str], escalation_from: str | None) -> CandidateScore:
        context, weights = self._eligibility(model_id, profile, branch, exhausted, escalation_from), self.config["weights"]
        base, capability, task_fit = int(weights["base"]), self._capability_score(model_id, profile), self._task_fit_score(model_id, profile)
        resource_score = int(weights["resource_ready"]) if not context.rejection_reasons else 0
        penalty = self._failure_count(model_id, profile.previous_failures) * int(weights["failure_penalty"])
        escalation = int(weights["escalation_bonus"]) if escalation_from and model_id in self._successors(escalation_from) else 0
        total = base + capability + task_fit + resource_score + escalation - penalty
        return CandidateScore(model_id, base, capability, task_fit, resource_score, penalty, escalation, total, not context.rejection_reasons, context.rejection_reasons)

    def _eligibility(self, model_id: str, profile: TaskProfile, branch: str, exhausted: set[str], escalation_from: str | None) -> _CandidateContext:
        definition, reasons = self.registry.get(model_id), []
        if model_id not in self.branches[branch]["workers"]: reasons.append("wrong_routing_branch")
        if model_id in exhausted: reasons.append("retry_limit_reached")
        if escalation_from and model_id not in self._successors(escalation_from): reasons.append("not_in_escalation_path")
        estimated_tokens, operational_max = profile.context_estimate.estimated_tokens, definition.context.get("operational_max")
        if estimated_tokens is not None and isinstance(operational_max, int) and estimated_tokens > operational_max: reasons.append("context_exceeds_operational_max")
        status = next((item for item in profile.resource_state.model_statuses if item.logical_id == model_id), None)
        if status is None: reasons.append("runtime_status_missing")
        elif status.status is ModelStatus.TAG_TODO: reasons.append("tag_todo")
        elif status.status is ModelStatus.SERVER_OFFLINE: reasons.append("server_offline")
        elif status.status is ModelStatus.DISCOVERY_ERROR: reasons.append("discovery_error")
        elif status.status not in (ModelStatus.LOADED, ModelStatus.UNLOADED): reasons.append(f"runtime_status_{status.status.value.lower()}")
        feasibility = self._feasibility(model_id, profile, status)
        if feasibility and not feasibility.allowed: reasons.append(feasibility.reason)
        return _CandidateContext(feasibility, tuple(reasons))

    def _feasibility(self, model_id: str, profile: TaskProfile, status: Any) -> ResourceFeasibility | None:
        definition = self.registry.get(model_id)
        if definition.runtime.get("execution") != "gpu_worker": return None
        gpu = profile.resource_state.gpu
        loaded = status is not None and status.status is ModelStatus.LOADED
        observed = status.runtime_model.vram_gb if loaded and status.runtime_model else None
        configured = definition.resources.get("estimated_vram_gb")
        if loaded and observed is not None:
            requested, source = observed, "runtime_observed"
        elif isinstance(configured, (int, float)):
            requested, source = float(configured), "configured_estimate"
        elif loaded:
            return ResourceFeasibility(False, ResourceFeasibilityMode.NOT_FEASIBLE, None, None, gpu.vram_free_gb if gpu else None, 0, None, False, "loaded_vram_unknown")
        else:
            return ResourceFeasibility(False, ResourceFeasibilityMode.NOT_FEASIBLE, None, None, gpu.vram_free_gb if gpu else None, 0, None, False, "resource_estimate_unknown")
        if gpu is None: return ResourceFeasibility(False, ResourceFeasibilityMode.NOT_FEASIBLE, requested, 0 if loaded else requested, None, 0, None, False, "gpu_unavailable", source, observed)
        if profile.resource_state.gpu_worker_lock.locked: return ResourceFeasibility(False, ResourceFeasibilityMode.NOT_FEASIBLE, requested, 0 if loaded else requested, gpu.vram_free_gb, 0, None, False, "gpu_worker_lock_busy", source, observed)
        if definition.runtime.get("cpu_ram_offload_allowed") and profile.resource_state.cpu_bandwidth_lock.locked:
            return ResourceFeasibility(False, ResourceFeasibilityMode.NOT_FEASIBLE, requested, 0 if loaded else requested, gpu.vram_free_gb, 0, None, False, "cpu_bandwidth_lock_busy", source, observed)
        limits = self.resource_manager.worker_vram_policy
        reserve = float(limits["min_free_after_load_gb"])
        hard, soft = float(limits["hard_limit_gb"]), float(limits["soft_target_gb"])
        if requested > hard:
            reason = "observed_vram_exceeds_hard_limit" if source == "runtime_observed" else "requested_vram_exceeds_hard_limit"
            return ResourceFeasibility(False, ResourceFeasibilityMode.NOT_FEASIBLE, requested, 0 if loaded else requested, gpu.vram_free_gb, 0, None, requested > soft, reason, source, observed)
        if loaded:
            if gpu.vram_free_gb < reserve: return ResourceFeasibility(False, ResourceFeasibilityMode.NOT_FEASIBLE, requested, 0, gpu.vram_free_gb, 0, gpu.vram_free_gb, requested > soft, "insufficient_free_vram_reserve", source, observed)
            return ResourceFeasibility(True, ResourceFeasibilityMode.REUSE_LOADED, requested, 0, gpu.vram_free_gb, 0, gpu.vram_free_gb, requested > soft, "reuse_loaded", source, observed)
        projected = gpu.vram_free_gb - requested
        if projected >= reserve: return ResourceFeasibility(True, ResourceFeasibilityMode.LOAD_DIRECT, requested, requested, gpu.vram_free_gb, 0, projected, requested > soft, "load_direct", source, observed)
        reclaimable = self._reclaimable_idle_worker_vram(profile, excluded_model=model_id)
        projected = gpu.vram_free_gb + reclaimable - requested
        if reclaimable > 0 and projected >= reserve:
            return ResourceFeasibility(True, ResourceFeasibilityMode.SWITCH_REQUIRED, requested, requested, gpu.vram_free_gb, reclaimable, projected, requested > soft, "switch_required", source, observed)
        return ResourceFeasibility(False, ResourceFeasibilityMode.NOT_FEASIBLE, requested, requested, gpu.vram_free_gb, reclaimable, projected, requested > soft, "insufficient_free_vram_after_allocation", source, observed)
    def _reclaimable_idle_worker_vram(self, profile: TaskProfile, excluded_model: str) -> float:
        if profile.resource_state.gpu_worker_lock.locked: return 0.0
        reclaimable = 0.0
        for status in profile.resource_state.model_statuses:
            if status.logical_id == excluded_model or status.status is not ModelStatus.LOADED or not status.runtime_model: continue
            definition = self.registry.get(status.logical_id)
            if definition.runtime.get("execution") == "gpu_worker" and status.runtime_model.vram_gb is not None:
                reclaimable += status.runtime_model.vram_gb
        return round(reclaimable, 3)

    def _branch_for(self, task_type: str) -> str:
        for name, branch in self.branches.items():
            if task_type in branch["task_types"]: return name
        raise ConfigurationError(f"No routing branch configured for task type {task_type!r}")
    def _successors(self, model_id: str) -> tuple[str, ...]: return tuple(item for item in self.escalation_graph.get(model_id, []) if item not in TERMINAL_ESCALATION_VALUES)
    def _exhausted_models(self, failures: tuple[AttemptFailure, ...]) -> set[str]:
        counts = Counter(item.model_id for item in failures)
        return {model_id for model_id, count in counts.items() if model_id in self.registry.logical_ids and count >= self._retry_limit(model_id)}
    def _retry_limit(self, model_id: str) -> int:
        definition = self.registry.get(model_id)
        return int(self.policies["max_attempts_same_heavy_worker"] if definition.resources.get("heavy_cpu_offload") else self.policies["max_attempts_same_fast_worker"])
    @staticmethod
    def _escalation_source(exhausted: set[str]) -> str | None:
        for model_id in ("qwen27_heavy_quality", "qwen35_heavy_fast", "iquest_repo_coder", "deepseek_fast_coder", "ministral_reasoner"):
            if model_id in exhausted: return model_id
        return None
    def _task_fit_score(self, model_id: str, profile: TaskProfile) -> int:
        rules, weights, score = self.config["model_signals"].get(model_id, {}), self.config["weights"], 0
        for field, key in (("task_type","task_type_match"),("complexity","complexity_match"),("scope","scope_match")):
            if getattr(profile,field).value in rules.get(field,[]): score += int(weights[key])
        if profile.reasoning_need in (NeedLevel.HIGH,NeedLevel.CRITICAL) and model_id == "ministral_reasoner": score += int(weights["reasoning_need_match"])
        if profile.repo_exploration_need in (NeedLevel.HIGH,NeedLevel.CRITICAL) and model_id == "iquest_repo_coder": score += int(weights["repo_need_match"])
        return score
    def _capability_score(self, model_id: str, profile: TaskProfile) -> int:
        weights,score=self.config["weights"],0
        if profile.risk in (NeedLevel.HIGH,NeedLevel.CRITICAL):
            if model_id in ("qwen35_heavy_fast","qwen27_heavy_quality"):score+=int(weights["high_risk_heavy_bonus"])
            if model_id=="deepseek_fast_coder":score-=int(weights["high_risk_fast_penalty"])
        if profile.risk is NeedLevel.CRITICAL and model_id=="qwen27_heavy_quality":score+=int(weights["critical_quality_bonus"])
        if profile.complexity is Complexity.HARD and model_id=="qwen35_heavy_fast":score+=int(weights["hard_complexity_heavy_bonus"])
        if profile.complexity in (Complexity.VERY_HARD,Complexity.CRITICAL) and model_id=="qwen27_heavy_quality":score+=int(weights["very_hard_quality_bonus"])
        return score
    @staticmethod
    def _failure_count(model_id: str, failures: tuple[AttemptFailure,...]) -> int:return sum(1 for item in failures if item.model_id==model_id)
    @staticmethod
    def _confidence(profile_confidence: float,winner:int,second:int|None)->float:
        margin=0.0 if second is None else min(1.0,max(0.0,(winner-second)/max(1,abs(winner))))
        return round(min(1.0,max(0.0,(profile_confidence+margin)/2)),3)
    @staticmethod
    def _warnings(winner: CandidateScore)->tuple[str,...]: return ("Selection follows configured escalation graph.",) if winner.escalation_adjustment else ()
    def _validate_router_config(self) -> None:
        threshold,order=self.config.get("confidence_threshold"),self.config.get("candidate_order")
        if not isinstance(threshold,(int,float)) or not 0<=threshold<=1:raise ConfigurationError("router_v1.confidence_threshold must be numeric in [0, 1]")
        if not isinstance(order,list) or len(order)!=len(set(order)):raise ConfigurationError("router_v1.candidate_order must be a duplicate-free list")
        if any(item not in self.registry.logical_ids for item in order):raise ConfigurationError("router_v1.candidate_order contains unknown model IDs")
        worker_sets=[set(branch.get("workers",[])) for branch in self.branches.values()]
        all_workers=set().union(*worker_sets)
        if any(item not in all_workers for item in order):raise ConfigurationError("router_v1.candidate_order contains non-branch worker IDs")
        if any(worker_sets[i]&worker_sets[j] for i in range(len(worker_sets)) for j in range(i)):raise ConfigurationError("routing branches may not share workers")
        task_types=[item for branch in self.branches.values() for item in branch.get("task_types",[])]
        if len(task_types)!=len(set(task_types)) or set(task_types)!={item.value for item in TaskType}:raise ConfigurationError("routing branches must cover each TaskType exactly once")
        if any(worker not in order for worker in all_workers):raise ConfigurationError("every branch worker must appear in candidate_order")
        required=("base","resource_ready","task_type_match","complexity_match","scope_match","reasoning_need_match","repo_need_match","high_risk_heavy_bonus","high_risk_fast_penalty","critical_quality_bonus","hard_complexity_heavy_bonus","very_hard_quality_bonus","failure_penalty","escalation_bonus")
        if any(not isinstance(self.config.get("weights",{}).get(key),(int,float)) for key in required):raise ConfigurationError("router_v1 weights must contain required numeric values")
        for source,successors in self.escalation_graph.items():
            if source not in self.registry.logical_ids or not isinstance(successors,list):raise ConfigurationError("escalation_graph must map known model IDs to lists")
            if any(item not in self.registry.logical_ids and item not in TERMINAL_ESCALATION_VALUES for item in successors):raise ConfigurationError("escalation_graph contains unknown successor")
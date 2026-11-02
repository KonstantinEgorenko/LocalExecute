"""Immutable orchestration-layer contracts; no Router policy lives here."""
from __future__ import annotations
import json, math
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any
from ..executor.models import WorkerResult
from ..router.profile import Complexity, ContextBucket, ContextEstimate, NeedLevel, Scope, TaskType, ValidationLevel
from ..router.scoring import RoutingDecision
from ..control_plane.models import QuotaUsage

@dataclass(frozen=True, slots=True)
class RoutingTaskSpec:
    task_type: TaskType
    complexity: Complexity
    scope: Scope
    reasoning_need: NeedLevel
    repo_exploration_need: NeedLevel
    tool_depth: NeedLevel
    context_estimate: ContextEstimate
    testability: ValidationLevel
    risk: NeedLevel
    router_confidence: float
    def __post_init__(self) -> None:
        expected=((self.task_type,TaskType,"task_type"),(self.complexity,Complexity,"complexity"),(self.scope,Scope,"scope"),(self.reasoning_need,NeedLevel,"reasoning_need"),(self.repo_exploration_need,NeedLevel,"repo_exploration_need"),(self.tool_depth,NeedLevel,"tool_depth"),(self.context_estimate,ContextEstimate,"context_estimate"),(self.testability,ValidationLevel,"testability"),(self.risk,NeedLevel,"risk"))
        for value,kind,name in expected:
            if not isinstance(value,kind): raise ValueError(f"{name} must be a {kind.__name__}")
        if not isinstance(self.context_estimate.bucket,ContextBucket): raise ValueError("context_estimate.bucket must be a ContextBucket")
        tokens=self.context_estimate.estimated_tokens
        if tokens is not None and (not isinstance(tokens,int) or isinstance(tokens,bool) or tokens<0): raise ValueError("context_estimate.estimated_tokens must be None or a non-boolean integer >= 0")
        if not isinstance(self.router_confidence,(int,float)) or isinstance(self.router_confidence,bool) or not math.isfinite(self.router_confidence) or not 0<=self.router_confidence<=1: raise ValueError("router_confidence must be finite numeric in [0, 1]")

class OrchestrationOutcome(StrEnum):
    SUCCESS="SUCCESS"; WAITING_EVIDENCE="WAITING_EVIDENCE"; NEEDS_ATTENTION="NEEDS_ATTENTION"; FAILED="FAILED"; QUOTA_EXHAUSTED="QUOTA_EXHAUSTED"

@dataclass(frozen=True, slots=True)
class OrchestrationResult:
    outcome: OrchestrationOutcome
    run_id: str
    todo_id: str
    routing_decision: RoutingDecision | None
    worker_result: WorkerResult | None
    attempts: tuple[dict[str,Any], ...]
    reason: str | None
    quota_usage: QuotaUsage
    def __post_init__(self)->None:
        copied=json.loads(json.dumps(self.attempts,allow_nan=False))
        def freeze(value: Any) -> Any:
            if isinstance(value,dict): return MappingProxyType({key:freeze(item) for key,item in value.items()})
            if isinstance(value,list): return tuple(freeze(item) for item in value)
            return value
        object.__setattr__(self,"attempts",tuple(freeze(item) for item in copied))
    def to_dict(self)->dict[str,Any]:
        return {"outcome":self.outcome.value,"run_id":self.run_id,"todo_id":self.todo_id,"routing_decision":self.routing_decision.to_dict() if self.routing_decision else None,"worker_result":self.worker_result.to_dict() if self.worker_result else None,"attempts":json.loads(json.dumps(self.attempts,default=lambda value: dict(value) if isinstance(value,MappingProxyType) else list(value))),"reason":self.reason,"quota_usage":self.quota_usage.to_dict()}
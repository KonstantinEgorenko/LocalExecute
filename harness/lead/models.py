"""Immutable, JSON-safe contracts for Ornith-only decomposition."""
from __future__ import annotations
import math
from dataclasses import dataclass,field
from enum import StrEnum
from types import MappingProxyType
from typing import Any,Mapping
from ..control_plane import Goal,Todo,WorkContract
from ..orchestrator.models import RoutingTaskSpec
from ..router.profile import Complexity,ContextBucket,ContextEstimate,NeedLevel,Scope,TaskType,ValidationLevel
class LeadDisposition(StrEnum): PLAN="PLAN";NEEDS_ATTENTION="NEEDS_ATTENTION"
class LeadOutcome(StrEnum): PLAN_READY="PLAN_READY";NEEDS_ATTENTION="NEEDS_ATTENTION";MODEL_FAILURE="MODEL_FAILURE";INVALID_PLAN="INVALID_PLAN"
def _text(v,n,optional=False):
 if v is None and optional:return None
 if not isinstance(v,str) or not v.strip():raise ValueError(f"{n} must be a non-empty string")
 return v
def _ids(v,n):
 if not isinstance(v,(list,tuple)):raise ValueError(f"{n} must be list or tuple")
 r=tuple(_text(x,n) for x in v)
 if len(set(r))!=len(r):raise ValueError(f"{n} must not contain duplicates")
 return r
def _freeze(v,n="value"):
 if v is None or isinstance(v,(str,bool,int)):return v
 if isinstance(v,float):
  if not math.isfinite(v):raise ValueError(f"{n} must be finite")
  return v
 if isinstance(v,Mapping):
  if any(not isinstance(k,str) for k in v):raise ValueError(f"{n} keys must be strings")
  return MappingProxyType({k:_freeze(x,f"{n}.{k}") for k,x in v.items()})
 if isinstance(v,(tuple,list)):return tuple(_freeze(x,n) for x in v)
 raise ValueError(f"{n} must be JSON-safe")
def _plain(v):
 if isinstance(v,Mapping):return {k:_plain(x) for k,x in v.items()}
 if isinstance(v,tuple):return [_plain(x) for x in v]
 return v
@dataclass(frozen=True,slots=True)
class LeadRequest:
 objective:str;constraints:Mapping[str,Any]=field(default_factory=dict);allowed_scope:tuple[str,...]=();forbidden_scope:tuple[str,...]=();metadata:Mapping[str,Any]=field(default_factory=dict)
 def __post_init__(self):
  _text(self.objective,"objective");object.__setattr__(self,"constraints",_freeze(self.constraints,"constraints"));object.__setattr__(self,"allowed_scope",_ids(self.allowed_scope,"allowed_scope"));object.__setattr__(self,"forbidden_scope",_ids(self.forbidden_scope,"forbidden_scope"));object.__setattr__(self,"metadata",_freeze(self.metadata,"metadata"))
  if set(self.allowed_scope)&set(self.forbidden_scope):raise ValueError("request scope overlap")
 def to_dict(self):return {"objective":self.objective,"constraints":_plain(self.constraints),"allowed_scope":list(self.allowed_scope),"forbidden_scope":list(self.forbidden_scope),"metadata":_plain(self.metadata)}
@dataclass(frozen=True,slots=True)
class LeadRoutingSpec:
 task_type:TaskType;complexity:Complexity;scope:Scope;reasoning_need:NeedLevel;repo_exploration_need:NeedLevel;tool_depth:NeedLevel;context_estimate:ContextEstimate;testability:ValidationLevel;risk:NeedLevel;router_confidence:float
 def __post_init__(self):
  for v,t,n in ((self.task_type,TaskType,"task_type"),(self.complexity,Complexity,"complexity"),(self.scope,Scope,"scope"),(self.reasoning_need,NeedLevel,"reasoning_need"),(self.repo_exploration_need,NeedLevel,"repo_exploration_need"),(self.tool_depth,NeedLevel,"tool_depth"),(self.testability,ValidationLevel,"testability"),(self.risk,NeedLevel,"risk")):
   if not isinstance(v,t):raise ValueError(f"{n} must be {t.__name__}")
  if not isinstance(self.context_estimate,ContextEstimate) or not isinstance(self.context_estimate.bucket,ContextBucket):raise ValueError("context_estimate must be ContextEstimate")
  tokens=self.context_estimate.estimated_tokens
  if tokens is not None and (not isinstance(tokens,int) or isinstance(tokens,bool) or tokens<0):raise ValueError("estimated_tokens invalid")
  if isinstance(self.router_confidence,bool) or not isinstance(self.router_confidence,(int,float)) or not math.isfinite(self.router_confidence) or not 0<=self.router_confidence<=1:raise ValueError("router_confidence invalid")
 def to_dict(self):return {"task_type":self.task_type.value,"complexity":self.complexity.value,"scope":self.scope.value,"reasoning_need":self.reasoning_need.value,"repo_exploration_need":self.repo_exploration_need.value,"tool_depth":self.tool_depth.value,"context_estimate":{"estimated_tokens":self.context_estimate.estimated_tokens,"bucket":self.context_estimate.bucket.value},"testability":self.testability.value,"risk":self.risk.value,"router_confidence":self.router_confidence}
@dataclass(frozen=True,slots=True)
class LeadTodoSpec:
 key:str;objective:str;dependencies:tuple[str,...];priority:int;constraints:Mapping[str,Any];allowed_scope:tuple[str,...];forbidden_scope:tuple[str,...];required_evidence:tuple[str,...];routing_spec:LeadRoutingSpec
 def __post_init__(self):
  _text(self.key,"key");_text(self.objective,"objective");object.__setattr__(self,"dependencies",_ids(self.dependencies,"dependencies"));object.__setattr__(self,"constraints",_freeze(self.constraints,"constraints"));object.__setattr__(self,"allowed_scope",_ids(self.allowed_scope,"allowed_scope"));object.__setattr__(self,"forbidden_scope",_ids(self.forbidden_scope,"forbidden_scope"));object.__setattr__(self,"required_evidence",_ids(self.required_evidence,"required_evidence"))
  if not isinstance(self.priority,int) or isinstance(self.priority,bool) or self.priority<0:raise ValueError("priority invalid")
  if set(self.allowed_scope)&set(self.forbidden_scope):raise ValueError("todo scope overlap")
  if not isinstance(self.routing_spec,LeadRoutingSpec):raise ValueError("routing_spec invalid")
 def to_dict(self):return {"key":self.key,"objective":self.objective,"dependencies":list(self.dependencies),"priority":self.priority,"constraints":_plain(self.constraints),"allowed_scope":list(self.allowed_scope),"forbidden_scope":list(self.forbidden_scope),"required_evidence":list(self.required_evidence),"routing":self.routing_spec.to_dict()}
@dataclass(frozen=True,slots=True)
class LeadPlan:
 schema_version:int;disposition:LeadDisposition;goal_summary:str;assumptions:tuple[str,...];todos:tuple[LeadTodoSpec,...];attention_reason:str|None=None
 def __post_init__(self):
  if not isinstance(self.schema_version,int) or isinstance(self.schema_version,bool) or self.schema_version<1:raise ValueError("schema_version invalid")
  if not isinstance(self.disposition,LeadDisposition):raise ValueError("disposition invalid")
  _text(self.goal_summary,"goal_summary");object.__setattr__(self,"assumptions",_ids(self.assumptions,"assumptions"));object.__setattr__(self,"todos",tuple(self.todos));_text(self.attention_reason,"attention_reason",True)
  if any(not isinstance(x,LeadTodoSpec) for x in self.todos):raise ValueError("todos invalid")
  if self.disposition is LeadDisposition.PLAN and not self.todos:raise ValueError("PLAN requires todos")
  if self.disposition is LeadDisposition.NEEDS_ATTENTION and (self.todos or self.attention_reason is None):raise ValueError("NEEDS_ATTENTION requires reason and no todos")
 def to_dict(self):return {"schema_version":self.schema_version,"disposition":self.disposition.value,"goal_summary":self.goal_summary,"assumptions":list(self.assumptions),"attention_reason":self.attention_reason,"todos":[x.to_dict() for x in self.todos]}
@dataclass(frozen=True,slots=True)
class CompiledWorkItem:
 todo:Todo;work_contract:WorkContract;routing_task_spec:RoutingTaskSpec
 def __post_init__(self):
  if not isinstance(self.todo,Todo) or not isinstance(self.work_contract,WorkContract) or not isinstance(self.routing_task_spec,RoutingTaskSpec):raise ValueError("CompiledWorkItem requires Todo, WorkContract, RoutingTaskSpec")
 def to_dict(self):return {"todo":self.todo.to_dict(),"work_contract":self.work_contract.to_dict(),"routing_task_spec":LeadRoutingSpec(self.routing_task_spec.task_type,self.routing_task_spec.complexity,self.routing_task_spec.scope,self.routing_task_spec.reasoning_need,self.routing_task_spec.repo_exploration_need,self.routing_task_spec.tool_depth,self.routing_task_spec.context_estimate,self.routing_task_spec.testability,self.routing_task_spec.risk,self.routing_task_spec.router_confidence).to_dict()}
@dataclass(frozen=True,slots=True)
class LeadResult:
 outcome:LeadOutcome;goal:Goal|None;work_items:tuple[CompiledWorkItem,...]=();lead_call_count:int=0;model_telemetry:Mapping[str,Any]=field(default_factory=dict);warnings:tuple[str,...]=();attention_reason:str|None=None
 def __post_init__(self):
  if not isinstance(self.outcome,LeadOutcome):raise ValueError("outcome invalid")
  if self.goal is not None and not isinstance(self.goal,Goal):raise ValueError("goal must be Goal or None")
  object.__setattr__(self,"work_items",tuple(self.work_items));object.__setattr__(self,"model_telemetry",_freeze(self.model_telemetry,"model_telemetry"));object.__setattr__(self,"warnings",_ids(self.warnings,"warnings"));_text(self.attention_reason,"attention_reason",True)
  if any(not isinstance(x,CompiledWorkItem) for x in self.work_items):raise ValueError("work_items invalid")
  if not isinstance(self.lead_call_count,int) or isinstance(self.lead_call_count,bool) or self.lead_call_count<0:raise ValueError("lead_call_count invalid")
  if self.outcome is LeadOutcome.PLAN_READY and (self.goal is None or not self.work_items):raise ValueError("PLAN_READY requires goal and work items")
  if self.outcome is LeadOutcome.NEEDS_ATTENTION and (self.goal is None or self.work_items or self.attention_reason is None):raise ValueError("NEEDS_ATTENTION invalid")
  if self.outcome in (LeadOutcome.MODEL_FAILURE,LeadOutcome.INVALID_PLAN) and (self.goal is not None or self.work_items):raise ValueError("failure result must not expose persisted work")
 def to_dict(self):return {"outcome":self.outcome.value,"goal":self.goal.to_dict() if self.goal else None,"work_items":[x.to_dict() for x in self.work_items],"lead_call_count":self.lead_call_count,"model_telemetry":_plain(self.model_telemetry),"warnings":list(self.warnings),"attention_reason":self.attention_reason}

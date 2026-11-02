"""Fail-closed JSON parser for the public Ornith plan schema. Markdown fences are rejected."""
from __future__ import annotations
import json
from .models import LeadDisposition,LeadPlan,LeadRoutingSpec,LeadTodoSpec
from ..router.profile import Complexity, ContextBucket, ContextEstimate, NeedLevel, Scope, TaskType, ValidationLevel
class LeadValidationError(ValueError): pass
def parse_plan(text:str,expected_schema_version:int)->LeadPlan:
 if not isinstance(text,str):raise LeadValidationError("response must be text")
 try:value=json.loads(text)
 except json.JSONDecodeError as exc:raise LeadValidationError(f"invalid JSON: {exc.msg}") from exc
 if not isinstance(value,dict):raise LeadValidationError("response must be one JSON object")
 required={"schema_version","disposition","goal_summary","assumptions","attention_reason","todos"}
 if set(value)!=required:raise LeadValidationError("plan keys must match schema exactly")
 if value["schema_version"]!=expected_schema_version:raise LeadValidationError("unsupported schema_version")
 try:
  todos=[]
  if not isinstance(value["todos"],list):raise ValueError("todos must be list")
  for raw in value["todos"]:
   keys={"key","objective","dependencies","priority","constraints","allowed_scope","forbidden_scope","required_evidence","routing"}
   if not isinstance(raw,dict) or set(raw)!=keys:raise ValueError("todo keys must match schema exactly")
   route=raw["routing"]
   route_keys={"task_type","complexity","scope","reasoning_need","repo_exploration_need","tool_depth","context_estimate","testability","risk","router_confidence"}
   if not isinstance(route,dict) or set(route)!=route_keys:raise ValueError("routing keys must match schema exactly")
   from ..router.profile import Complexity, ContextBucket, NeedLevel, Scope, TaskType, ValidationLevel
   TaskType(route["task_type"]);Complexity(route["complexity"]);Scope(route["scope"]);NeedLevel(route["reasoning_need"]);NeedLevel(route["repo_exploration_need"]);NeedLevel(route["tool_depth"]);ValidationLevel(route["testability"]);NeedLevel(route["risk"])
   if not isinstance(route["context_estimate"],dict) or set(route["context_estimate"])!={"estimated_tokens","bucket"}:raise ValueError("context_estimate keys must match schema exactly")
   tokens=route["context_estimate"]["estimated_tokens"]
   if tokens is not None and (not isinstance(tokens,int) or isinstance(tokens,bool) or tokens<0):raise ValueError("estimated_tokens invalid")
   routing=LeadRoutingSpec(TaskType(route["task_type"]),Complexity(route["complexity"]),Scope(route["scope"]),NeedLevel(route["reasoning_need"]),NeedLevel(route["repo_exploration_need"]),NeedLevel(route["tool_depth"]),ContextEstimate(tokens,ContextBucket(route["context_estimate"]["bucket"])),ValidationLevel(route["testability"]),NeedLevel(route["risk"]),route["router_confidence"])
   todos.append(LeadTodoSpec(raw["key"],raw["objective"],raw["dependencies"],raw["priority"],raw["constraints"],raw["allowed_scope"],raw["forbidden_scope"],raw["required_evidence"],routing))
  return LeadPlan(value["schema_version"],LeadDisposition(value["disposition"]),value["goal_summary"],value["assumptions"],tuple(todos),value["attention_reason"])
 except (KeyError,TypeError,ValueError) as exc:raise LeadValidationError(str(exc)) from exc

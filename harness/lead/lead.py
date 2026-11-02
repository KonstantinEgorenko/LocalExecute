"""Lead service: decompose, validate fully, compile, then persist. Never executes Todos."""
from __future__ import annotations
from .compiler import compile_plan
from .models import LeadDisposition,LeadOutcome,LeadResult
from .parser import LeadValidationError,parse_plan
from .prompt import build_prompt,build_repair_prompt,build_response_schema
from .runtime import LeadModelError
from .validator import LeadDependencyValidationError
from ..control_plane import StoreIntegrityError
class OrnithLead:
 def __init__(self,store,runtime,config,policies):self.store,self.runtime,self.config,self.policies=store,runtime,config,policies
 def decompose(self,request,goal_id):
  lead=self.config["lead"];calls=0;invalid="";errors=[];telemetry={};first_validation_error=None
  for attempt in range(lead["max_lead_calls"]):
   prompt=build_prompt(request,self.config) if attempt==0 else build_repair_prompt(request,invalid,errors,self.config)
   calls+=1
   try:response=self.runtime.complete("You are the Lead decomposition service. Return only canonical schema-conformant JSON.",prompt,build_response_schema(self.config))
   except LeadModelError as exc:return LeadResult(LeadOutcome.MODEL_FAILURE,None,(),calls,telemetry,(str(exc),),str(exc))
   telemetry={"model_id":response.model_id,"model_tag":response.model_tag,"context_length":response.context_length}
   try:
    plan=parse_plan(response.content,lead["schema_version"])
    if plan.disposition is LeadDisposition.NEEDS_ATTENTION:
     from ..control_plane import Goal,GoalStatus
     goal=Goal(goal_id,request.objective,GoalStatus.NEEDS_ATTENTION,"lead");self.store.create_goal(goal)
     return LeadResult(LeadOutcome.NEEDS_ATTENTION,goal,(),calls,telemetry,(),plan.attention_reason)
    goal,items=compile_plan(plan,request,goal_id,self.policies,lead["max_todos"],lead["max_dependencies_per_todo"])
   except (LeadValidationError,ValueError,KeyError) as exc:
    telemetry["planning_error_category"]="PLANNING_INVALID_DEPENDENCY_DAG" if isinstance(exc,LeadDependencyValidationError) else "PLANNING_INVALID_PLAN"
    if first_validation_error is None:first_validation_error=(telemetry["planning_error_category"],str(exc))
    invalid=response.content;errors=[str(exc)];continue
   try:
    try:self.store.get_goal(goal.goal_id)
    except StoreIntegrityError:pass
    else:raise ValueError("goal_id already exists")
    for item in items:
     try:self.store.get_todo(item.todo.todo_id)
     except StoreIntegrityError:pass
     else:raise ValueError("todo_id already exists")
   except ValueError as exc:
    return LeadResult(LeadOutcome.INVALID_PLAN,None,(),calls,telemetry,(str(exc),),str(exc))
   self.store.create_goal(goal)
   for item in items:self.store.create_todo(item.todo)
   return LeadResult(LeadOutcome.PLAN_READY,goal,items,calls,telemetry)
  if first_validation_error is not None:
   telemetry["planning_error_category"],telemetry["planning_first_error_reason"]=first_validation_error
  return LeadResult(LeadOutcome.INVALID_PLAN,None,(),calls,telemetry,tuple(errors),errors[0] if errors else "invalid plan")

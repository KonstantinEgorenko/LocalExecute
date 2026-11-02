"""Pure conversion of a valid LeadPlan to frozen Control Plane contracts."""
from __future__ import annotations
from .models import CompiledWorkItem,LeadPlan
from .validator import topological_keys
from ..control_plane import Goal,GoalStatus,Quota,Todo,TodoStatus,WorkContract
from ..control_plane.todo_identity import make_todo_id
from ..orchestrator.models import RoutingTaskSpec
from ..router.profile import Complexity,ContextBucket,ContextEstimate,NeedLevel,Scope,TaskType,ValidationLevel
def _quota(policies):
 p=policies["retry_and_anti_loop"]
 return Quota(int(p["max_total_worker_calls_per_task"]),int(p["max_attempts_same_fast_worker"]),int(p["max_attempts_same_heavy_worker"]),int(p["max_replans_per_task"]),int(p["max_escalations_per_task"]))
def _routing(value):
 return RoutingTaskSpec(value.task_type,value.complexity,value.scope,value.reasoning_need,value.repo_exploration_need,value.tool_depth,value.context_estimate,value.testability,value.risk,value.router_confidence)
def compile_plan(plan:LeadPlan,request,goal_id:str,policies:dict,max_todos:int,max_dependencies:int):
 order=topological_keys(plan,max_todos,max_dependencies);by_key={todo.key:todo for todo in plan.todos};ids={key:make_todo_id(goal_id,index) for index,key in enumerate(order,1)}
 goal=Goal(goal_id,request.objective,GoalStatus.ACTIVE,"lead")
 items=[];quota=_quota(policies)
 for key in order:
  spec=by_key[key];deps=tuple(ids[x] for x in spec.dependencies);todo=Todo(ids[key],goal_id,spec.objective,TodoStatus.PENDING if deps else TodoStatus.READY,deps,(),spec.priority)
  constraints=dict(request.constraints)
  from ..verification.callable_authority import AUTHORITY,INGRESS
  if any(k in spec.constraints and k not in request.constraints for k in (AUTHORITY,INGRESS)):
   raise ValueError("model cannot activate trusted public callable authority")
  for constraint,value in spec.constraints.items():
   if constraint in constraints and constraints[constraint]!=value:raise ValueError("todo constraint conflicts with parent constraint")
   constraints[constraint]=value
  allowed=request.allowed_scope if request.allowed_scope else spec.allowed_scope
  forbidden=tuple(dict.fromkeys((*request.forbidden_scope,*spec.forbidden_scope)))
  if set(allowed)&set(forbidden):raise ValueError("inherited scope overlap")
  evidence=spec.required_evidence if "WORKER_RESULT" in spec.required_evidence else (*spec.required_evidence,"WORKER_RESULT")
  contract=WorkContract(goal_id,todo.todo_id,todo.objective,constraints,allowed,forbidden,evidence,quota)
  items.append(CompiledWorkItem(todo,contract,_routing(spec.routing_spec)))
 return goal,tuple(items)

"""Deterministic LeadPlan structural and DAG validation."""
from __future__ import annotations
from .models import LeadDisposition,LeadPlan
from .parser import LeadValidationError
class LeadDependencyValidationError(LeadValidationError):
 """Known model DAG error; not a transport or persistence failure."""
def topological_keys(plan:LeadPlan,max_todos:int,max_dependencies:int)->tuple[str,...]:
 if plan.disposition is LeadDisposition.NEEDS_ATTENTION:return ()
 if len(plan.todos)>max_todos:raise LeadValidationError("todo count exceeds limit")
 by_key={todo.key:todo for todo in plan.todos}
 if len(by_key)!=len(plan.todos):raise LeadDependencyValidationError("duplicate todo key")
 for todo in plan.todos:
  if len(todo.dependencies)>max_dependencies:raise LeadValidationError("dependency count exceeds limit")
  if todo.key in todo.dependencies:raise LeadDependencyValidationError("self dependency")
  if any(key not in by_key for key in todo.dependencies):raise LeadDependencyValidationError("unknown dependency")
 pending={key:set(todo.dependencies) for key,todo in by_key.items()};ordered=[]
 while pending:
  ready=sorted(key for key,deps in pending.items() if not deps)
  if not ready:raise LeadDependencyValidationError("dependency cycle")
  ordered.extend(ready)
  for key in ready:del pending[key]
  for deps in pending.values():deps.difference_update(ready)
 return tuple(ordered)

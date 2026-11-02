"""Bounded deterministic orchestration over frozen Router and Executor APIs."""
from __future__ import annotations
from typing import Any, Callable
from ..control_plane import AttentionItem, AttentionSeverity, AttentionStatus, AttemptRecord, Evidence, EvidenceStatus, Handoff, QuotaUsage, Run, RunStatus
from ..control_plane.store import RunStore
from ..errors import ExecutionError, StaleRoutingDecisionError
from ..executor.models import ExecutionOutcome, ExecutionRequest, WorkerResult
from ..router.profile import AttemptFailure, FailureReason
from ..router.scoring import DecisionType, RoutingDecision
from .models import OrchestrationOutcome as Outcome, OrchestrationResult, RoutingTaskSpec
from .profile_factory import TaskProfileFactory

class ExecutionOrchestrator:
 def __init__(self,store:RunStore,router:Any,executor:Any,snapshot_provider:Callable[[],Any],policy:dict[str,Any],profile_factory:TaskProfileFactory|None=None)->None:
  self.store,self.router,self.executor,self.snapshot_provider=store,router,executor,snapshot_provider;self.policy=policy.get("retry_and_anti_loop",policy);self.factory=profile_factory or TaskProfileFactory()
 def execute(self,run_id:str,contract:Any,spec:RoutingTaskSpec,messages:tuple[Any,...],*,request_id:str|None=None,task_id:str|None=None,keep_alive:int|str=-1)->OrchestrationResult:
  run=self.store.get_run(run_id);todo=self.store.get_todo(contract.todo_id)
  if run.status is not RunStatus.RUNNING: raise ValueError("Run must be RUNNING to execute")
  if contract.goal_id!=run.goal_id or todo.goal_id!=run.goal_id or (run.active_todo_id is not None and run.active_todo_id!=contract.todo_id): raise ValueError("WorkContract does not match active Run/Todo")
  quota=run.quota_usage;attempts=[];failures=self._history(run_id,contract.todo_id)
  handoffs={(p["todo_id"],p["from_model"],p["to_model"]) for e in self.store.list_events(run_id) if e.event_type.value=="HANDOFF_RECORDED" for p in [e.to_dict()["payload"]] if p.get("todo_id")==contract.todo_id}
  if self._success_evidence(run_id,contract.todo_id):
   outcome=self._evaluate(contract,run_id)
   if outcome is Outcome.SUCCESS: run=self.store.update_run(Run(run.run_id,run.goal_id,run.active_todo_id,RunStatus.COMPLETED,quota))
   return self._finish(outcome,run,contract.todo_id,None,None,attempts,None,quota)
  while True:
   limits=self._limits(contract.quota)
   if quota.worker_calls>=limits["calls"]: return self._quota(run,contract.todo_id,None,attempts,"worker call quota exhausted",quota)
   decision=self.router.route(self.factory.build(spec,self.snapshot_provider(),tuple(failures)))
   if decision.decision_type is not DecisionType.WORKER:
    run=self.store.update_run(Run(run.run_id,run.goal_id,run.active_todo_id,RunStatus.NEEDS_ATTENTION,quota));self.store.record_attention(run_id,AttentionItem(f"{run_id}:attention:{len(self.store.list_events(run_id))}",decision.routing_reason,AttentionSeverity.HIGH,AttentionStatus.OPEN,todo_id=contract.todo_id,run_id=run_id,category="router"))
    return self._finish(Outcome.NEEDS_ATTENTION,run,contract.todo_id,decision,None,attempts,decision.routing_reason,quota)
   if decision.escalation_from and decision.selected_model:
    transition=(contract.todo_id,decision.escalation_from,decision.selected_model)
    if transition not in handoffs:
     if quota.escalations>=limits["escalations"]: return self._quota(run,contract.todo_id,decision,attempts,"escalation quota exhausted",quota)
     self.store.record_handoff(run_id,Handoff(contract.todo_id,decision.escalation_from,decision.selected_model,"router escalation",quota.worker_calls));handoffs.add(transition);quota=self._usage(quota,escalations=1);run=self._persist(run,quota)
   if quota.worker_attempts.get(decision.selected_model or "",0)>=self._model_limit(decision.selected_model or "",contract.quota): return self._quota(run,contract.todo_id,decision,attempts,"same-worker retry quota exhausted",quota)
   quota=self._usage(quota,worker_calls=1,model=decision.selected_model);run=self._persist(run,quota)
   try: result:WorkerResult=self.executor.execute(ExecutionRequest(decision,tuple(messages),keep_alive,request_id,task_id))
   except StaleRoutingDecisionError as exc:
    record=AttemptRecord(quota.worker_calls-1,contract.todo_id,decision,decision.selected_model,ExecutionOutcome.REPLAN_REQUIRED,None,str(exc));self.store.record_attempt(run_id,record);attempts.append(record.to_dict())
    if quota.replans>=limits["replans"]: return self._quota(run,contract.todo_id,decision,attempts,"replan quota exhausted",quota)
    quota=self._usage(quota,replans=1);run=self._persist(run,quota);continue
   except ExecutionError as exc:
    stage=getattr(getattr(exc,"failure_context",None),"stage",None);record=AttemptRecord(quota.worker_calls-1,contract.todo_id,decision,decision.selected_model,ExecutionOutcome.EXECUTION_FAILURE,stage,f"{type(exc).__name__}: {exc}")
    self.store.record_attempt(run_id,record);attempts.append(record.to_dict());failures.append(AttemptFailure(decision.selected_model or "",FailureReason.REPEATED_SAME_FAILURE,quota.worker_attempts.get(decision.selected_model or "",0)));continue
   record=AttemptRecord(quota.worker_calls-1,contract.todo_id,decision,decision.selected_model,ExecutionOutcome.SUCCESS);self.store.record_attempt(run_id,record);attempts.append(record.to_dict());self.store.record_evidence(run_id,Evidence(f"{run_id}:worker:{quota.worker_calls}",contract.todo_id,"WORKER_RESULT","WorkerExecutor",EvidenceStatus.PASSED,result.to_dict()))
   outcome=self._evaluate(contract,run_id)
   if outcome is Outcome.SUCCESS: run=self.store.update_run(Run(run.run_id,run.goal_id,run.active_todo_id,RunStatus.COMPLETED,quota))
   return self._finish(outcome,run,contract.todo_id,decision,result,attempts,None,quota)
 def _history(self,run_id:str,todo_id:str)->list[AttemptFailure]:
  return [AttemptFailure(p["selected_model"],FailureReason.REPEATED_SAME_FAILURE,int(p["attempt_index"])) for e in self.store.list_events(run_id) if e.event_type.value=="ATTEMPT_RECORDED" for p in [e.to_dict()["payload"]] if p.get("todo_id")==todo_id and p.get("execution_outcome")=="EXECUTION_FAILURE" and isinstance(p.get("selected_model"),str)]
 def _success_evidence(self,run_id:str,todo_id:str)->bool:
  return any((p:=e.to_dict()["payload"]).get("todo_id")==todo_id and p.get("evidence_type")=="WORKER_RESULT" and p.get("status")=="PASSED" for e in self.store.list_events(run_id) if e.event_type.value=="EVIDENCE_RECORDED")
 def _limits(self,q:Any)->dict[str,int]: return {"calls":min(int(self.policy["max_total_worker_calls_per_task"]),q.max_worker_calls),"replans":min(int(self.policy["max_replans_per_task"]),q.max_replans),"escalations":min(int(self.policy["max_escalations_per_task"]),q.max_escalations)}
 def _model_limit(self,m:str,q:Any)->int:
  heavy=m in ("qwen35_heavy_fast","qwen27_heavy_quality");return min(int(self.policy["max_attempts_same_heavy_worker" if heavy else "max_attempts_same_fast_worker"]),q.max_same_heavy_worker_attempts if heavy else q.max_same_fast_worker_attempts)
 @staticmethod
 def _usage(q:QuotaUsage,worker_calls=0,replans=0,escalations=0,model=None)->QuotaUsage:
  values=dict(q.worker_attempts)
  if model: values[model]=values.get(model,0)+1
  return QuotaUsage(q.worker_calls+worker_calls,q.replans+replans,q.escalations+escalations,values)
 def _persist(self,run:Run,q:QuotaUsage)->Run:return self.store.update_run(Run(run.run_id,run.goal_id,run.active_todo_id,run.status,q))
 def _evaluate(self,c:Any,run_id:str)->Outcome:
  latest={};passed=set()
  for e in self.store.list_events(run_id):
   p=e.to_dict()["payload"]
   if e.event_type.value=="GATE_RECORDED" and p.get("todo_id")==c.todo_id: latest[p["gate"]["gate_id"]]=p["gate"]["status"]
   if e.event_type.value=="EVIDENCE_RECORDED" and p.get("todo_id")==c.todo_id and p.get("status")=="PASSED":passed.add(p.get("evidence_type"))
  return Outcome.SUCCESS if all(latest.get(x)=="SATISFIED" for x in self.store.get_todo(c.todo_id).required_gates) and all(x in passed for x in c.required_evidence) else Outcome.WAITING_EVIDENCE
 def _quota(self,run:Run,todo:str,d:RoutingDecision|None,a:list[dict[str,Any]],reason:str,q:QuotaUsage)->OrchestrationResult:
  run=self.store.update_run(Run(run.run_id,run.goal_id,run.active_todo_id,RunStatus.NEEDS_ATTENTION,q));self.store.record_attention(run.run_id,AttentionItem(f"{run.run_id}:quota:{len(self.store.list_events(run.run_id))}",reason,AttentionSeverity.HIGH,AttentionStatus.OPEN,todo_id=todo,run_id=run.run_id,category="quota"));return self._finish(Outcome.QUOTA_EXHAUSTED,run,todo,d,None,a,reason,q)
 @staticmethod
 def _finish(o:Outcome,run:Run,todo:str,d:RoutingDecision|None,result:WorkerResult|None,a:list[dict[str,Any]],reason:str|None,q:QuotaUsage)->OrchestrationResult:return OrchestrationResult(o,run.run_id,todo,d,result,tuple(a),reason,q)
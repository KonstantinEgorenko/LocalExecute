"""Deterministic in-memory and durable JSON Control Plane stores."""
from __future__ import annotations
import json, math, os, tempfile
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from .errors import StoreCorruptionError, StoreIntegrityError
from .events import ControlPlaneEntityType as ET, ControlPlaneEvent, ControlPlaneEventType as EV
from .models import AttentionItem, AttemptRecord, Evidence, Gate, Goal, Handoff, Run, Todo

def _now(): return datetime.now(timezone.utc).isoformat()
def _copy(x):
    try: return json.loads(json.dumps(x,ensure_ascii=False,allow_nan=False))
    except (TypeError,ValueError) as e: raise StoreIntegrityError(str(e)) from e

class RunStore(ABC):
 @abstractmethod
 def create_goal(self,x:Goal)->Goal: ...
 @abstractmethod
 def get_goal(self,x:str)->Goal: ...
 @abstractmethod
 def create_todo(self,x:Todo)->Todo: ...
 @abstractmethod
 def get_todo(self,x:str)->Todo: ...
 @abstractmethod
 def create_run(self,x:Run)->Run: ...
 @abstractmethod
 def get_run(self,x:str)->Run: ...
 @abstractmethod
 def update_run(self,x:Run)->Run: ...
 @abstractmethod
 def record_attempt(self,r:str,x:AttemptRecord)->ControlPlaneEvent: ...
 @abstractmethod
 def record_evidence(self,r:str,x:Evidence)->ControlPlaneEvent: ...
 @abstractmethod
 def record_gate(self,r:str,t:str,x:Gate)->ControlPlaneEvent: ...
 @abstractmethod
 def record_handoff(self,r:str,x:Handoff)->ControlPlaneEvent: ...
 @abstractmethod
 def record_attention(self,r:str,x:AttentionItem)->ControlPlaneEvent: ...
 @abstractmethod
 def list_events(self,r:str)->tuple[ControlPlaneEvent,...]: ...
 def list_runs(self)->tuple[Run,...]:
  """Enumerate existing durable runs for explicitly scoped continuation."""
  raise NotImplementedError

class InMemoryRunStore(RunStore):
 def __init__(self):
  self._goals={};self._todos={};self._runs={};self._events={};self._records={};self._ids={}
 def create_goal(self,x):
  if not isinstance(x,Goal): raise StoreIntegrityError("goal must be a Goal")
  if x.goal_id in self._goals: raise StoreIntegrityError("duplicate goal_id")
  self._goals[x.goal_id]=x;return x
 def get_goal(self,x):
  if x not in self._goals: raise StoreIntegrityError(f"missing goal_id: {x}")
  return self._goals[x]
 def create_todo(self,x):
  if not isinstance(x,Todo): raise StoreIntegrityError("todo must be a Todo")
  if x.todo_id in self._todos: raise StoreIntegrityError("duplicate todo_id")
  self.get_goal(x.goal_id);self._todos[x.todo_id]=x;return x
 def get_todo(self,x):
  if x not in self._todos: raise StoreIntegrityError(f"missing todo_id: {x}")
  return self._todos[x]
 def _valid_run(self,x):
  self.get_goal(x.goal_id)
  if x.active_todo_id is not None and self.get_todo(x.active_todo_id).goal_id!=x.goal_id: raise StoreIntegrityError("active_todo_id must belong to the run goal")
 def create_run(self,x):
  if not isinstance(x,Run): raise StoreIntegrityError("run must be a Run")
  if x.run_id in self._runs: raise StoreIntegrityError("duplicate run_id")
  self._valid_run(x);self._runs[x.run_id]=x;self._events[x.run_id]=[];self._records[x.run_id]={k:[] for k in ("attempts","evidence","gates","handoffs","attention")};self._ids[x.run_id]={k:set() for k in ("attempts","evidence","handoffs","attention")}
  self._append(x.run_id,EV.RUN_CREATED,ET.RUN,x.run_id,x.to_dict());return x
 def get_run(self,x):
  if x not in self._runs: raise StoreIntegrityError(f"missing run_id: {x}")
  return self._runs[x]
 def update_run(self,x):
  old=self.get_run(x.run_id)
  if not isinstance(x,Run): raise StoreIntegrityError("run must be a Run")
  self._valid_run(x)
  if old.active_todo_id!=x.active_todo_id: raise StoreIntegrityError("Run active_todo_id is immutable after creation")
  self._runs[x.run_id]=x;self._append(x.run_id,EV.RUN_UPDATED,ET.RUN,x.run_id,x.to_dict());return x
 def _todo(self,r,t):
  run,todo=self.get_run(r),self.get_todo(t)
  if todo.goal_id!=run.goal_id: raise StoreIntegrityError("referenced todo must belong to the run goal")
  return todo
 def _unique(self,r,k,i):
  if i in self._ids[r][k]: raise StoreIntegrityError(f"duplicate {k} identity: {i}")
  self._ids[r][k].add(i)
 def _append(self,r,k,e,i,p):
  event=ControlPlaneEvent(f"{r}:{len(self._events[r])}",r,len(self._events[r]),k,_now(),e,i,_copy(p));self._events[r].append(event);return event
 def record_attempt(self,r,x):
  if not isinstance(x,AttemptRecord): raise StoreIntegrityError("attempt must be an AttemptRecord")
  self._todo(r,x.todo_id);self._unique(r,"attempts",f"{x.todo_id}:{x.attempt_index}");p=_copy(x.to_dict());self._records[r]["attempts"].append(p);return self._append(r,EV.ATTEMPT_RECORDED,ET.ATTEMPT,f"{x.todo_id}:{x.attempt_index}",p)
 def record_evidence(self,r,x):
  if not isinstance(x,Evidence): raise StoreIntegrityError("evidence must be an Evidence")
  self._todo(r,x.todo_id);self._unique(r,"evidence",x.evidence_id);p=_copy(x.to_dict());self._records[r]["evidence"].append(p);return self._append(r,EV.EVIDENCE_RECORDED,ET.EVIDENCE,x.evidence_id,p)
 def record_gate(self,r,t,x):
  if not isinstance(x,Gate): raise StoreIntegrityError("gate must be a Gate")
  self._todo(r,t);p={"todo_id":t,"gate":_copy(x.to_dict())};self._records[r]["gates"].append(_copy(p));return self._append(r,EV.GATE_RECORDED,ET.GATE,x.gate_id,p)
 def record_handoff(self,r,x):
  if not isinstance(x,Handoff): raise StoreIntegrityError("handoff must be a Handoff")
  self._todo(r,x.todo_id);self._unique(r,"handoffs",f"{x.todo_id}:{x.attempt_index}");p=_copy(x.to_dict());self._records[r]["handoffs"].append(p);return self._append(r,EV.HANDOFF_RECORDED,ET.HANDOFF,f"{x.todo_id}:{x.attempt_index}",p)
 def record_attention(self,r,x):
  if not isinstance(x,AttentionItem): raise StoreIntegrityError("attention item must be an AttentionItem")
  run=self.get_run(r)
  if x.run_id not in (None,r): raise StoreIntegrityError("attention run_id must match the event run")
  if x.todo_id is not None: self._todo(r,x.todo_id)
  self._unique(r,"attention",x.attention_id);p=_copy(x.to_dict());self._records[r]["attention"].append(p);return self._append(r,EV.ATTENTION_RECORDED,ET.ATTENTION,x.attention_id,p)
 def list_events(self,r): self.get_run(r);return tuple(self._events[r])
 def list_runs(self): return tuple(self._runs.values())

class FileRunStore(InMemoryRunStore):
 def __init__(self,root):
  self.root=Path(root);self.runs_root=self.root/"runs";super().__init__();self._load()
 def _after(self,r): self._state(r)
 def create_goal(self,x): y=super().create_goal(x);self._index();return y
 def create_todo(self,x): y=super().create_todo(x);self._index();return y
 def create_run(self,x): y=super().create_run(x);self._state(x.run_id);return y
 def update_run(self,x): y=super().update_run(x);self._state(x.run_id);return y
 def record_attempt(self,r,x): y=super().record_attempt(r,x);self._state(r);return y
 def record_evidence(self,r,x): y=super().record_evidence(r,x);self._state(r);return y
 def record_gate(self,r,t,x): y=super().record_gate(r,t,x);self._state(r);return y
 def record_handoff(self,r,x): y=super().record_handoff(r,x);self._state(r);return y
 def record_attention(self,r,x): y=super().record_attention(r,x);self._state(r);return y
 def _append(self,*a,**kw):
  event=super()._append(*a,**kw);d=self.runs_root/event.run_id;d.mkdir(parents=True,exist_ok=True)
  with (d/"events.jsonl").open("a",encoding="utf-8",newline="\n") as f:f.write(json.dumps(event.to_dict(),sort_keys=True)+"\n");f.flush();os.fsync(f.fileno())
  return event
 def _atomic(self,p,d):
  p.parent.mkdir(parents=True,exist_ok=True);tmp=None
  try:
   with tempfile.NamedTemporaryFile("w",encoding="utf-8",dir=p.parent,delete=False) as f:tmp=f.name;json.dump(d,f,sort_keys=True);f.flush();os.fsync(f.fileno())
   os.replace(tmp,p)
  finally:
   if tmp and os.path.exists(tmp):os.unlink(tmp)
 def _index(self):self._atomic(self.root/"index.json",{"goals":[x.to_dict() for x in self._goals.values()],"todos":[x.to_dict() for x in self._todos.values()]})
 def _state(self,r):self._atomic(self.runs_root/r/"state.json",{"run":self._runs[r].to_dict(),"records":_copy(self._records[r]),"last_event_sequence":len(self._events[r])-1})
 def _load(self):
  index=self.root/"index.json"
  if index.exists():
   try:data=json.loads(index.read_text(encoding="utf-8"))
   except Exception as e:raise StoreCorruptionError(f"malformed index: {e}") from e
   try:
    from .models import GoalStatus,TodoStatus
    for p in data["goals"]:InMemoryRunStore.create_goal(self,Goal(p["goal_id"],p["objective"],GoalStatus(p["status"]),p["created_at"],p.get("metadata",{})))
    for p in data["todos"]:InMemoryRunStore.create_todo(self,Todo(p["todo_id"],p["goal_id"],p["objective"],TodoStatus(p["status"]),p.get("dependencies",[]),p.get("required_gates",[]),p.get("priority",0)))
   except Exception as e:raise StoreCorruptionError(f"invalid index: {e}") from e
  if self.runs_root.exists():
   for d in sorted(x for x in self.runs_root.iterdir() if x.is_dir()):self._load_run(d)
 def _load_run(self,d):
  try:
   try: state=json.loads((d/"state.json").read_text(encoding="utf-8"))
   except json.JSONDecodeError as e: raise StoreCorruptionError(f"malformed JSON: {e}") from e
   lines=(d/"events.jsonl").read_text(encoding="utf-8").splitlines()
   from .models import RunStatus,QuotaUsage
   p=state["run"];run=Run(p["run_id"],p["goal_id"],p.get("active_todo_id"),RunStatus(p["status"]),QuotaUsage(**p["quota_usage"]))
   if run.run_id!=d.name:raise ValueError("run identity")
   self._valid_run(run);events=[]
   for n,line in enumerate(lines):
    try: raw=json.loads(line)
    except json.JSONDecodeError as e: raise StoreCorruptionError(f"malformed JSONL: {e}") from e
    event=ControlPlaneEvent(raw["event_id"],raw["run_id"],raw["sequence"],EV(raw["event_type"]),raw["occurred_at"],ET(raw["entity_type"]),raw["entity_id"],raw["payload"])
    if event.run_id!=run.run_id or event.sequence!=n or event.event_id!=f"{run.run_id}:{n}":raise StoreCorruptionError("invalid event sequence/order")
    self._check(event,run);events.append(event)
   if not events or events[0].event_type is not EV.RUN_CREATED or state["last_event_sequence"]!=len(events)-1:raise ValueError("checkpoint")
   latest=next(e.to_dict()["payload"] for e in reversed(events) if e.event_type in (EV.RUN_CREATED,EV.RUN_UPDATED))
   if latest!=run.to_dict():raise ValueError("run state mismatch")
   expected={k:[e.to_dict()["payload"] for e in events if e.event_type.value==v] for k,v in {"attempts":"ATTEMPT_RECORDED","evidence":"EVIDENCE_RECORDED","gates":"GATE_RECORDED","handoffs":"HANDOFF_RECORDED","attention":"ATTENTION_RECORDED"}.items()}
   if state["records"]!=expected:raise ValueError("records mismatch")
   self._runs[run.run_id]=run;self._events[run.run_id]=events;self._records[run.run_id]=_copy(expected);self._ids[run.run_id]={k:set() for k in ("attempts","evidence","handoffs","attention")}
   for k,fn in (("attempts",lambda p:f'{p["todo_id"]}:{p["attempt_index"]}'),("evidence",lambda p:p["evidence_id"]),("handoffs",lambda p:f'{p["todo_id"]}:{p["attempt_index"]}'),("attention",lambda p:p["attention_id"])):
    for p in expected[k]:self._unique(run.run_id,k,fn(p))
  except Exception as e:
   if isinstance(e,StoreCorruptionError):raise
   raise StoreCorruptionError(f"invalid persisted run {d.name}: {e}") from e
 def _check(self,e,run):
  from collections.abc import Mapping
  from .models import AttentionSeverity, AttentionStatus, EvidenceStatus, GateStatus, QuotaUsage, RunStatus
  from ..executor.models import ExecutionOutcome, ExecutionStage
  from ..router.scoring import CandidateScore, DecisionType, RoutingDecision
  p=e.to_dict()["payload"]
  if not isinstance(p,dict): raise ValueError("event payload must be a mapping")
  def todo(x):
   if not isinstance(x,str) or x not in self._todos or self._todos[x].goal_id!=run.goal_id:raise ValueError("todo reference")
  def decision(value):
   if not isinstance(value,Mapping): raise ValueError("routing_decision must be a mapping")
   kind=DecisionType(value["decision_type"]);selected=value.get("selected_model")
   if kind is DecisionType.WORKER:
    if not isinstance(selected,str) or not selected.strip():raise ValueError("WORKER routing_decision.selected_model")
   elif selected is not None:raise ValueError("non-worker routing_decision.selected_model must be None")
   score=value.get("score")
   if score is not None and (not isinstance(score,int) or isinstance(score,bool)):raise ValueError("routing_decision.score")
   confidence=value.get("router_confidence")
   if isinstance(confidence,bool) or not isinstance(confidence,(int,float)) or not math.isfinite(confidence) or not 0<=confidence<=1:raise ValueError("routing_decision.router_confidence")
   reason=value.get("routing_reason")
   if not isinstance(reason,str) or not reason.strip():raise ValueError("routing_decision.routing_reason")
   raw_alternatives=value.get("alternatives")
   if not isinstance(raw_alternatives,list) or any(not isinstance(item,Mapping) for item in raw_alternatives):raise ValueError("routing_decision.alternatives")
   alternatives=tuple(CandidateScore(**dict(item)) for item in raw_alternatives);admission=value.get("resource_admission")
   if admission is not None and not isinstance(admission,Mapping):raise ValueError("routing_decision.resource_admission")
   escalation=value.get("escalation_from")
   if escalation is not None and (not isinstance(escalation,str) or not escalation.strip()):raise ValueError("routing_decision.escalation_from")
   warnings=value.get("warnings")
   if not isinstance(warnings,list) or any(not isinstance(item,str) for item in warnings):raise ValueError("routing_decision.warnings")
   return RoutingDecision(kind,selected,score,float(confidence),reason,alternatives,dict(admission) if admission is not None else None,escalation,tuple(warnings))
  if e.event_type in (EV.RUN_CREATED,EV.RUN_UPDATED):
   if e.entity_type is not ET.RUN or e.entity_id!=run.run_id or p.get("run_id")!=run.run_id:raise ValueError("run event")
   candidate=Run(p["run_id"],p["goal_id"],p.get("active_todo_id"),RunStatus(p["status"]),QuotaUsage(**p["quota_usage"]));self._valid_run(candidate)
   if candidate.goal_id!=run.goal_id or candidate.active_todo_id!=run.active_todo_id:raise ValueError("persisted Run goal/todo history mismatch")
  elif e.event_type is EV.ATTEMPT_RECORDED:
   if e.entity_type is not ET.ATTEMPT or e.entity_id!="{}:{}".format(p.get("todo_id"),p.get("attempt_index")):raise ValueError("attempt event")
   todo(p.get("todo_id"));outcome=p.get("execution_outcome");stage=p.get("failure_stage")
   AttemptRecord(p["attempt_index"],p["todo_id"],decision(p.get("routing_decision")),p.get("selected_model"),ExecutionOutcome(outcome) if outcome is not None else None,ExecutionStage(stage) if stage is not None else None,p.get("failure_reason"))
  elif e.event_type is EV.EVIDENCE_RECORDED:
   if e.entity_type is not ET.EVIDENCE or e.entity_id!=p.get("evidence_id") or not isinstance(p.get("evidence_id"),str):raise ValueError("evidence event")
   todo(p.get("todo_id"));Evidence(p["evidence_id"],p["todo_id"],p["evidence_type"],p["source"],EvidenceStatus(p["status"]),p.get("payload",{}),p.get("reference"))
  elif e.event_type is EV.GATE_RECORDED:
   if e.entity_type is not ET.GATE or not isinstance(p.get("gate"),dict) or e.entity_id!=p["gate"].get("gate_id"):raise ValueError("gate event")
   todo(p.get("todo_id"));gate=p["gate"];Gate(gate["gate_id"],gate["gate_type"],gate["required"],GateStatus(gate["status"]))
  elif e.event_type is EV.HANDOFF_RECORDED:
   if e.entity_type is not ET.HANDOFF or e.entity_id!="{}:{}".format(p.get("todo_id"),p.get("attempt_index")):raise ValueError("handoff event")
   todo(p.get("todo_id"));Handoff(p["todo_id"],p["from_model"],p["to_model"],p["reason"],p["attempt_index"])
  elif e.event_type is EV.ATTENTION_RECORDED:
   if e.entity_type is not ET.ATTENTION or e.entity_id!=p.get("attention_id") or p.get("run_id") not in (None,run.run_id):raise ValueError("attention event")
   if p.get("todo_id") is not None:todo(p["todo_id"])
   AttentionItem(p["attention_id"],p["reason"],AttentionSeverity(p["severity"]),AttentionStatus(p["status"]),p.get("todo_id"),p.get("run_id"),p.get("category"))
  else:raise ValueError("event type")

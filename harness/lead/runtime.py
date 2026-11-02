"""Ornith-only runtime adapter; it never addresses the worker server."""
from __future__ import annotations
import json
from dataclasses import dataclass
from urllib.error import HTTPError,URLError
from urllib.request import Request,urlopen
from ..errors import LockBusyError

@dataclass(frozen=True,slots=True)
class LeadRuntimeResponse:
 content:str
 model_id:str
 model_tag:str
 context_length:int

class LeadModelError(RuntimeError):pass

class OrnithRuntime:
 def __init__(self,registry,locks,timeout_seconds=120,output_tokens=8192,model_id="ornith_lead"):
  self.registry,self.locks,self.timeout_seconds,self.output_tokens,self.model_id=registry,locks,timeout_seconds,output_tokens,model_id
 def _definition(self):
  try:definition=self.registry.get(self.model_id);server=self.registry.get_server(definition.server);worker_server=self.registry.get_server("worker")
  except KeyError as exc:raise LeadModelError("configured Lead model/server is unavailable") from exc
  if definition.logical_id!=self.model_id or server.server_id!="lead" or definition.server=="worker" or definition.runtime.get("execution")!="cpu":raise LeadModelError("configured Lead must be CPU model on lead server")
  if server.ollama_base_url.rstrip("/")==worker_server.ollama_base_url.rstrip("/"):raise LeadModelError("lead and worker endpoints must differ")
  if not definition.model_tag_known:raise LeadModelError("Ornith model tag is TODO")
  return definition,server
 def complete(self,system_prompt,user_prompt,response_schema=None):
  definition,server=self._definition();context=definition.context["default"]
  payload={"model":definition.ollama_model,"messages":[{"role":"system","content":system_prompt},{"role":"user","content":user_prompt}],"stream":False,"options":{"num_ctx":context,"num_predict":self.output_tokens,"temperature":0}}
  if response_schema is not None:payload["format"]=response_schema
  request=Request(server.ollama_base_url.rstrip("/")+"/api/chat",data=json.dumps(payload).encode("utf-8"),method="POST",headers={"Content-Type":"application/json"})
  try:
   with self.locks.cpu_bandwidth_lock.held(definition.logical_id):
    with urlopen(request,timeout=self.timeout_seconds) as response:raw=response.read()
   data=json.loads(raw.decode("utf-8"));content=data.get("message",{}).get("content")
   if not isinstance(content,str):raise ValueError("response lacks message.content")
   return LeadRuntimeResponse(content,definition.logical_id,definition.ollama_model,context)
  except LeadModelError:raise
  except (HTTPError,URLError,TimeoutError,OSError,UnicodeDecodeError,json.JSONDecodeError,ValueError,LockBusyError) as exc:raise LeadModelError(f"Ornith runtime failure: {exc}") from exc

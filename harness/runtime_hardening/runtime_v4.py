'runtime hardening V4 CPU inference watchdog layered over existing hardening.'
from __future__ import annotations
import threading
from pathlib import Path
_MIN_CPU_CHAT_TIMEOUT_SECONDS=1800
_LOCK=threading.Lock(); _INSTALLED=False; _ORIGINAL=None

def install_v4_cpu_watchdog(root:Path)->None:
 del root
 global _INSTALLED,_ORIGINAL
 with _LOCK:
  if _INSTALLED:return
  from harness.lead import runtime as lead_runtime
  original=lead_runtime.urlopen
  def v4_urlopen(request,timeout=None):
   url=getattr(request,'full_url','')
   if url.endswith('/api/chat'):
    current=0 if timeout is None else float(timeout); return original(request,timeout=max(current,float(_MIN_CPU_CHAT_TIMEOUT_SECONDS)))
   return original(request,timeout=timeout)
  _ORIGINAL=original; lead_runtime.urlopen=v4_urlopen; _INSTALLED=True

def uninstall_v4_cpu_watchdog()->None:
 global _INSTALLED,_ORIGINAL
 with _LOCK:
  if not _INSTALLED:return
  from harness.lead import runtime as lead_runtime
  lead_runtime.urlopen=_ORIGINAL; _ORIGINAL=None; _INSTALLED=False

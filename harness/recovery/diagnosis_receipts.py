"""Protocol receipts, not physical invocation manifests; immutable attempt checkpoints."""
from datetime import datetime, timezone
import hashlib
import json
from ..agent_loop.protocol import decode_strict_json
from ..context_manager.models import Artifact, ArtifactKind, ArtifactOrigin

class DiagnosisExhaustedError(RuntimeError):
    stage = "diagnosis"
    error_code = "MODEL_PROTOCOL_EXHAUSTED"
    def __init__(self, message, *, attempts, receipt_ids=()):
        super().__init__(message)
        self.attempts = attempts
        self.receipt_ids = tuple(receipt_ids)

class DiagnosisInterruptedError(RuntimeError):
    stage = "diagnosis"
    error_code = "DISPATCH_UNRESOLVED"

def fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(",",":"),allow_nan=False).encode()).hexdigest()

def bounded(text, limit):
    value = text.encode("utf-8")
    return value[:limit].decode("utf-8", "ignore"), len(value)>limit

class DiagnosisReceipts:
    def __init__(self, store, bundle, model_id, model_tag, max_attempts, runtime_options=None):
        self.store=store
        self.identity={"schema_version":1,"run_id":bundle.source_metadata.get("run_id"),
            "todo_id":bundle.task_id,"bundle_fingerprint":bundle.fingerprint,
            "model_id":model_id,"model_tag":model_tag,"max_attempts":max_attempts,"runtime_options":runtime_options}
        self.request_id="diagnosis:"+fingerprint({k:self.identity[k] for k in ("run_id","todo_id","bundle_fingerprint")})
        self.ids=[]
        self.cycle=bundle.source_metadata.get("recovery_cycle")
    def key(self, attempt, phase):
        return f"{self.request_id}:attempt:{attempt}:{phase}"
    def read(self, attempt, phase):
        key=self.key(attempt,phase)
        if self.store is None or not self.store.has_artifact(key):return None
        artifact=self.store.get_artifact(key)
        row=decode_strict_json(artifact.content)
        if artifact.source!="DiagnosisProtocolReceipt" or row.get("identity")!=self.identity or row.get("attempt_index")!=attempt:
            raise RuntimeError("diagnosis receipt identity mismatch")
        allowed = {"DISPATCHED_NO_TERMINAL"} if phase=="dispatch" else {"PROTOCOL_VALID","PROTOCOL_REJECTED","FAILED_AFTER_DISPATCH","FAILED_BEFORE_DISPATCH"}
        if row.get("status") not in allowed or type(row.get("attempt_index")) is not int:
            raise RuntimeError("invalid diagnosis receipt status/index")
        before=self.identity["max_attempts"]-attempt
        after=before if row["status"]=="FAILED_BEFORE_DISPATCH" else before-1
        if row.get("budget_before")!=before or row.get("budget_after")!=after:
            raise RuntimeError("diagnosis receipt budget mismatch")
        self.ids.append(key)
        return row
    def write(self, attempt, phase, **fields):
        key=self.key(attempt,phase)
        row={"identity":self.identity,"request_id":self.request_id,"attempt_index":attempt,
            "accounting_kind":"PROTOCOL_RECEIPT_NOT_PHYSICAL_CALL",
            "physical_execution_status":"UNKNOWN",
            "runtime_request_id":None,"physical_linkage_status":"NOT_OBSERVED",
            "budget_before":self.identity['max_attempts']-attempt,
            "budget_after":self.identity['max_attempts']-attempt-1,**fields}
        content=json.dumps(row,ensure_ascii=False,sort_keys=True,allow_nan=False)
        if self.store is not None:
            if self.store.has_artifact(key):
                if self.store.get_artifact(key).content!=content:raise RuntimeError("diagnosis receipt conflicts")
            else:
                self.store.create_artifact(Artifact.from_text(artifact_id=key,kind=ArtifactKind.OTHER,
                    origin=ArtifactOrigin.CONTROL_PLANE,content=content,media_type="application/json",
                    source="DiagnosisProtocolReceipt",created_at=datetime.now(timezone.utc).isoformat(),
                    metadata={"run_id":self.identity["run_id"],"todo_id":self.identity["todo_id"],"attempt_index":attempt}))
        self.ids.append(key)
        return row

'Bounded no-tool model adapters for recovery diagnosis and replan.'
from __future__ import annotations

import time
import sys
from dataclasses import dataclass
from pathlib import Path

from ..config import ConfigLoader
from ..executor.client import OllamaNativeClient, OllamaTimeouts
from ..lead.runtime import OrnithRuntime
from ..models import ModelRegistry
from ..resource_manager.locks import ResourceLocks
from .diagnosis_receipts import DiagnosisReceipts, DiagnosisExhaustedError, DiagnosisInterruptedError, bounded, fingerprint
from .models import FailureDiagnosisV2, FailureEvidenceV2, RecoveryPolicy, ReplanContractV2
from .prompt import diagnosis_messages, replan_prompts
from .protocol import RecoveryProtocolError, parse_diagnosis_response, parse_replan_response


@dataclass(frozen=True, slots=True)
class DiagnosisCallResult:
    diagnosis: FailureDiagnosisV2
    model_id: str
    model_tag: str
    model_calls: int
    latency_ms: int


@dataclass(frozen=True, slots=True)
class ReplanCallResult:
    plan: ReplanContractV2
    model_id: str
    model_tag: str
    model_calls: int
    latency_ms: int


class MinistralDiagnosisClient:
    def __init__(self, root: Path, policy: RecoveryPolicy, *, artifact_store=None) -> None:
        self.artifact_store = artifact_store
        self.root = Path(root)
        self.policy = policy
        self.loader = ConfigLoader(self.root)
        self.registry = ModelRegistry.from_loader(self.loader)
        self.native = OllamaNativeClient(OllamaTimeouts.from_policies(self.loader.load_policies()))

    def diagnose(self, bundle: FailureEvidenceV2) -> DiagnosisCallResult:
        definition = self.registry.get(self.policy.diagnosis_model_id)
        if definition.logical_id != "ministral_reasoner" or definition.server != "worker":
            raise RuntimeError("Phase 15 diagnosis must use worker Ministral reasoner")
        if not definition.model_tag_known:
            raise RuntimeError("Ministral model tag is not configured")
        server = self.registry.get_server(definition.server)
        context = min(int(definition.context["default"]), self.policy.max_context_tokens)
        num_gpu = definition.runtime.get("num_gpu")
        if num_gpu is not None:
            num_gpu = int(num_gpu)
        started = time.monotonic()
        calls = 0
        prior_error: str | None = None
        prior_response: str | None = None
        receipts = DiagnosisReceipts(getattr(self,"artifact_store",None),bundle,definition.logical_id,
            definition.ollama_model,self.policy.max_diagnosis_model_attempts,
            runtime_options={"endpoint":server.ollama_base_url,"context":context,"num_gpu":num_gpu,"max_response_bytes":self.policy.max_response_bytes})
        loaded = False
        try:
            last_error = "diagnosis produced no valid response"
            for attempt in range(self.policy.max_diagnosis_model_attempts):
                terminal=receipts.read(attempt,"result")
                if terminal is not None:
                    if terminal["status"] == "FAILED_AFTER_DISPATCH":
                        raise DiagnosisInterruptedError("saved diagnosis dispatch failure; no implicit replay")
                    content=terminal.get("response")
                    if terminal.get("response_truncated"):
                        raise DiagnosisInterruptedError("saved diagnosis response truncated; no implicit replay")
                else:
                    pending=receipts.read(attempt,"dispatch")
                    if pending is not None:
                        raise DiagnosisInterruptedError("diagnosis dispatched without terminal receipt; no implicit replay")
                    messages = diagnosis_messages(bundle, self.policy, prior_error=prior_error, prior_response=prior_response)
                    request_hash=fingerprint({"messages":[{"role":m.role,"content":m.content} for m in messages],
                        "endpoint":server.ollama_base_url,"model":definition.ollama_model,
                        "context":context,"num_gpu":num_gpu})
                    if not loaded:
                        try:
                            self.native.preload_model(server.ollama_base_url, definition.ollama_model, context, num_gpu=num_gpu)
                        except Exception as exc:
                            receipts.write(attempt,"result",status="FAILED_BEFORE_DISPATCH",error_code="PRELOAD_FAILED",
                                exception_type=type(exc).__name__,error=bounded(str(exc),2048)[0],request_fingerprint=request_hash,
                                budget_after=self.policy.max_diagnosis_model_attempts-attempt)
                            raise
                        loaded=True
                    receipts.write(attempt,"dispatch",status="DISPATCHED_NO_TERMINAL",request_fingerprint=request_hash)
                    try:
                        from ..supervision.transport import protocol_attempt
                        with protocol_attempt(request_id=receipts.request_id, attempt_index=attempt,
                                run_id=bundle.source_metadata.get('run_id'), todo_id=bundle.task_id,
                                role='RECOVERY_DIAGNOSIS', logical_model_id=definition.logical_id,
                                dispatch_receipt_id=receipts.key(attempt,'dispatch')):
                            payload = self.native.chat(server.ollama_base_url,definition.ollama_model,messages,-1,context,
                                num_gpu=num_gpu,format_schema="json",temperature=0.0)
                    except Exception as exc:
                        receipts.write(attempt,"result",status="FAILED_AFTER_DISPATCH",error_code="TRANSPORT_FAILED",
                            exception_type=type(exc).__name__,error=bounded(str(exc),2048)[0],request_fingerprint=request_hash)
                        raise
                    calls += 1
                    message=payload.get("message") if isinstance(payload,dict) else None
                    content=message.get("content") if isinstance(message,dict) else None
                error_code=None
                if not isinstance(content,str) or len(content.encode("utf-8"))>self.policy.max_response_bytes:
                    prior_error,prior_response="diagnosis content missing or too large",None
                    error_code="INVALID_RESPONSE_SIZE"
                else:
                    try:
                        diagnosis=parse_diagnosis_response(content,bundle)
                    except RecoveryProtocolError as exc:
                        prior_error,prior_response=str(exc),content
                        error_code=exc.code
                if terminal is None:
                    response,truncated=bounded(content,min(65536,self.policy.max_response_bytes)) if isinstance(content,str) else (None,False)
                    receipts.write(attempt,"result",status="PROTOCOL_REJECTED" if error_code else "PROTOCOL_VALID",
                        dispatch_status="CLIENT_RETURNED",error_code=error_code,error=bounded(prior_error or "",2048)[0] if error_code else None,
                        response=response,response_truncated=truncated,request_fingerprint=request_hash)
                elif terminal["status"]=="FAILED_BEFORE_DISPATCH":
                    raise DiagnosisInterruptedError("saved diagnosis pre-dispatch failure; no implicit replay")
                if error_code is not None:
                    last_error=prior_error
                    continue
                return DiagnosisCallResult(diagnosis,definition.logical_id,definition.ollama_model,calls,
                    int(round((time.monotonic()-started)*1000)))
            raise DiagnosisExhaustedError(
                f"Ministral diagnosis exhausted {self.policy.max_diagnosis_model_attempts} attempts: {last_error}",
                attempts=self.policy.max_diagnosis_model_attempts,receipt_ids=tuple(dict.fromkeys(receipts.ids)))
        finally:
            if loaded:
                primary=sys.exc_info()[1]
                try:self.native.unload_model(server.ollama_base_url,definition.ollama_model)
                except Exception as cleanup:
                    if primary is None:raise
                    primary.add_note("Diagnosis cleanup failed: "+type(cleanup).__name__)



class OrnithReplanClient:
    def __init__(self, root: Path, policy: RecoveryPolicy) -> None:
        self.root = Path(root)
        self.policy = policy
        loader = ConfigLoader(self.root)
        self.registry = ModelRegistry.from_loader(loader)
        self.runtime = OrnithRuntime(
            self.registry,
            ResourceLocks(),
            timeout_seconds=180,
            output_tokens=4096,
            model_id=self.policy.replan_model_id,
        )

    def replan(self, bundle: FailureEvidenceV2, diagnosis: FailureDiagnosisV2) -> ReplanCallResult:
        if self.policy.replan_model_id != "ornith_lead":
            raise RuntimeError("Phase 15 replan must use Ornith lead")
        started = time.monotonic()
        calls = 0
        prior_error: str | None = None
        prior_response: str | None = None
        last_error = "replan produced no valid response"
        for _ in range(self.policy.max_replan_model_attempts):
            system, user = replan_prompts(
                bundle,
                diagnosis,
                self.policy,
                prior_error=prior_error,
                prior_response=prior_response,
            )
            response = self.runtime.complete(system, user, "json")
            calls += 1
            content = response.content
            if len(content.encode("utf-8")) > self.policy.max_response_bytes:
                prior_error, prior_response = "replan content too large", None
                last_error = prior_error
                continue
            try:
                plan = parse_replan_response(
                    content,
                    bundle,
                    diagnosis,
                    max_context_tokens=self.policy.max_context_tokens,
                    max_target_files=self.policy.max_target_files,
                )
            except RecoveryProtocolError as exc:
                last_error = str(exc)
                prior_error, prior_response = last_error, content
                continue
            return ReplanCallResult(
                plan,
                response.model_id,
                response.model_tag,
                calls,
                int(round((time.monotonic() - started) * 1000)),
            )
        raise RuntimeError(
            f"Ornith replan exhausted {self.policy.max_replan_model_attempts} attempts: {last_error}"
        )

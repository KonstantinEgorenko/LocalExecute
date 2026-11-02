'Integration-only tracing for Critic, Judge, and recovery physical model calls.\n\nThe wrapped coordinators and clients remain authoritative.  This module only\nprojects successful/failed physical inference attempts into the existing\nMissionFlightRecorder through ModelInvocationManifestV1.\n'
from __future__ import annotations

import time
import uuid
from contextlib import contextmanager
from typing import Any, Mapping

from .model_invocation_lineage import (
    ModelInvocationKindV1,
    ModelInvocationManifestV1,
    ModelInvocationStatusV1,
    runtime_fingerprint,
    stable_fingerprint,
)


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _non_negative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _compact_diagnostic(error: Exception) -> str:
    """Content-minimized durable failure identity."""
    diagnostic_text = str(error)
    diagnostic_fingerprint = stable_fingerprint({
        "exception_type": type(error).__name__,
        "message": diagnostic_text,
    })
    return (
        f"{type(error).__name__}: "
        f"fingerprint={diagnostic_fingerprint}; "
        f"bytes={len(diagnostic_text.encode('utf-8'))}"
    )


def _message_projection(messages: Any) -> list[dict[str, str | None]]:
    result: list[dict[str, str | None]] = []
    for item in messages or ():
        if isinstance(item, Mapping):
            role, content = item.get("role"), item.get("content")
        else:
            role, content = getattr(item, "role", None), getattr(item, "content", None)
        result.append({
            "role": role if isinstance(role, str) else None,
            "content": content if isinstance(content, str) else None,
        })
    return result


def _decision_projection(decision: Any) -> Any:
    if hasattr(decision, "to_dict") and callable(decision.to_dict):
        return decision.to_dict()
    return {
        "selected_model": getattr(decision, "selected_model", None),
        "decision_type": str(getattr(decision, "decision_type", "")),
    }


def _output_fingerprint(value: Any) -> str:
    if isinstance(value, str):
        return stable_fingerprint(value)
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return stable_fingerprint(value.to_dict())
    if isinstance(value, Mapping):
        return stable_fingerprint(value)
    return stable_fingerprint({
        "type": type(value).__name__,
        "model_id": getattr(value, "model_id", None),
        "model_tag": getattr(value, "model_tag", None),
        "outcome": str(getattr(value, "outcome", "")),
        "error": getattr(value, "error", None),
    })


def _runtime_metadata(runtime: Any, response: Any, logical_default: str) -> tuple[str, str | None, str, str]:
    logical_model_id = (
        _text(getattr(response, "model_id", None))
        or _text(getattr(runtime, "model_id", None))
        or logical_default
    )
    model_tag = _text(getattr(response, "model_tag", None))
    values: dict[str, Any] = {
        "runtime_kind": type(runtime).__name__,
        "logical_model_id": logical_model_id,
        "model_tag": model_tag,
        "context_length": getattr(response, "context_length", None),
        "output_tokens": getattr(runtime, "output_tokens", None),
        "timeout_seconds": getattr(runtime, "timeout_seconds", None),
    }
    registry = getattr(runtime, "registry", None)
    if registry is not None:
        try:
            definition = registry.get(logical_model_id)
            server = registry.get_server(definition.server)
            model_tag = model_tag or definition.ollama_model
            values.update({
                "model_tag": model_tag,
                "server_id": server.server_id,
                "execution": definition.runtime.get("execution"),
                "configured_num_gpu": definition.runtime.get("num_gpu"),
                "configured_context": definition.context.get("default"),
            })
        except (AttributeError, KeyError, TypeError, ValueError):
            pass
    return logical_model_id, model_tag, type(runtime).__name__, runtime_fingerprint(values)


class _TraceSink:
    def __init__(self, registry: Any) -> None:
        self.registry = registry
        self.last_error: str | None = None

    def correlation(self, run_id: str) -> Any | None:
        try:
            return self.registry.correlation_for_run(run_id)
        except Exception as exc:
            self.last_error = f"model invocation correlation failed: {type(exc).__name__}: {exc}"
            return None

    def record(self, run_id: str, manifest: ModelInvocationManifestV1) -> None:
        try:
            self.registry.resolve_for_run(run_id).record_model_invocation(manifest)
        except Exception as exc:
            self.last_error = f"model invocation trace write failed: {type(exc).__name__}: {exc}"


@contextmanager
def _temporary_innermost(owner: Any, attribute: str, wrapper_factory):
    """Wrap the deepest `.inner` transport and restore it after the call."""
    root = getattr(owner, attribute)
    parent = None
    current = root
    seen: set[int] = set()
    while hasattr(current, "inner") and id(current) not in seen:
        seen.add(id(current))
        parent = current
        current = current.inner
    wrapped = wrapper_factory(current)
    if parent is None:
        setattr(owner, attribute, wrapped)
        try:
            yield
        finally:
            setattr(owner, attribute, root)
    else:
        original = parent.inner
        parent.inner = wrapped
        try:
            yield
        finally:
            parent.inner = original


class _CriticExecutorProxy:
    def __init__(
        self,
        inner: Any,
        sink: _TraceSink,
        *,
        run_id: str,
        todo_id: str,
        max_attempts: int,
    ) -> None:
        self.inner = inner
        self.sink = sink
        self.run_id = run_id
        self.todo_id = todo_id
        self.max_attempts = max(1, int(max_attempts))
        self.ordinal = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def execute(self, request: Any) -> Any:
        self.ordinal += 1
        attempt = self.ordinal - 1
        correlation = self.sink.correlation(self.run_id)
        started = time.monotonic()
        decision = request.routing_decision
        logical_model_id = _text(getattr(decision, "selected_model", None)) or "critic"
        input_hash = stable_fingerprint({
            "request_id": getattr(request, "request_id", None),
            "task_id": getattr(request, "task_id", None),
            "routing_decision": _decision_projection(decision),
            "messages": _message_projection(getattr(request, "messages", ())),
            "response_format": getattr(request, "response_format", None),
            "temperature": getattr(request, "temperature", None),
            "keep_alive": getattr(request, "keep_alive", None),
        })
        invocation_id = f"{self.run_id}:critic:{attempt}:{uuid.uuid4().hex}"
        request_id = _text(getattr(request, "request_id", None))
        before = max(0, self.max_attempts - attempt)
        after = max(0, before - 1)
        try:
            result = self.inner.execute(request)
        except Exception as exc:
            if correlation is not None:
                self.sink.record(self.run_id, ModelInvocationManifestV1(
                    schema_version=1,
                    invocation_id=invocation_id,
                    request_kind=ModelInvocationKindV1.CRITIC_REVIEW,
                    mission_id=correlation.mission_id,
                    goal_id=correlation.goal_id,
                    todo_id=self.todo_id,
                    run_id=self.run_id,
                    request_id=request_id,
                    attempt_index=attempt,
                    slice_index=correlation.slice_index,
                    logical_model_id=logical_model_id,
                    runtime_kind="critic_worker_executor",
                    runtime_fingerprint=runtime_fingerprint({
                        "runtime_kind": "critic_worker_executor",
                        "logical_model_id": logical_model_id,
                    }),
                    input_fingerprint=input_hash,
                    budget_before={"critic_attempts_remaining": before},
                    budget_after={"critic_attempts_remaining": after},
                    duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                    status=ModelInvocationStatusV1.FAILED,
                    diagnostic=_compact_diagnostic(exc),
                ))
            raise

        if correlation is not None:
            returned_model = _text(getattr(result, "model_id", None)) or logical_model_id
            model_tag = _text(getattr(result, "model_tag", None))
            self.sink.record(self.run_id, ModelInvocationManifestV1(
                schema_version=1,
                invocation_id=invocation_id,
                request_kind=ModelInvocationKindV1.CRITIC_REVIEW,
                mission_id=correlation.mission_id,
                goal_id=correlation.goal_id,
                todo_id=self.todo_id,
                run_id=self.run_id,
                request_id=request_id,
                attempt_index=attempt,
                slice_index=correlation.slice_index,
                logical_model_id=returned_model,
                model_tag=model_tag,
                runtime_kind="critic_worker_executor",
                runtime_fingerprint=runtime_fingerprint({
                    "runtime_kind": "critic_worker_executor",
                    "logical_model_id": returned_model,
                    "model_tag": model_tag,
                    "requested_context_length": getattr(result, "requested_context_length", None),
                    "configured_num_gpu": getattr(result, "configured_num_gpu", None),
                }),
                input_fingerprint=input_hash,
                output_fingerprint=_output_fingerprint(
                    getattr(result, "content", None)
                    if getattr(result, "content", None) is not None
                    else result
                ),
                prompt_tokens=_non_negative_int(getattr(result, "prompt_eval_count", None)),
                completion_tokens=_non_negative_int(getattr(result, "eval_count", None)),
                requested_context_length=_non_negative_int(getattr(result, "requested_context_length", None)),
                observed_context_length=_non_negative_int(getattr(result, "observed_context_length", None)),
                budget_before={"critic_attempts_remaining": before},
                budget_after={"critic_attempts_remaining": after},
                duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                status=ModelInvocationStatusV1.SUCCESS,
            ))
        return result


class TracingCriticCoordinator:
    """Observe physical Critic executor calls; reused Critic artifacts emit nothing."""

    def __init__(self, inner: Any, registry: Any) -> None:
        self.inner = inner
        self.sink = _TraceSink(registry)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def review(self, *, run_id: str, contract: Any, lease_id: str) -> Any:
        original = self.inner.executor
        max_attempts = getattr(getattr(self.inner, "policy", None), "max_model_attempts", 1)
        self.inner.executor = _CriticExecutorProxy(
            original,
            self.sink,
            run_id=run_id,
            todo_id=contract.todo_id,
            max_attempts=max_attempts,
        )
        try:
            return self.inner.review(run_id=run_id, contract=contract, lease_id=lease_id)
        finally:
            self.inner.executor = original


class _OrnithRuntimeProxy:
    def __init__(
        self,
        inner: Any,
        sink: _TraceSink,
        *,
        run_id: str,
        todo_id: str,
        kind: ModelInvocationKindV1,
        logical_model_id: str,
        stage: str,
        max_attempts: int,
        recovery_cycle: int | None = None,
    ) -> None:
        self.inner = inner
        self.sink = sink
        self.run_id = run_id
        self.todo_id = todo_id
        self.kind = kind
        self.logical_model_id = logical_model_id
        self.stage = stage
        self.max_attempts = max(1, int(max_attempts))
        self.recovery_cycle = recovery_cycle
        self.ordinal = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def complete(self, system_prompt: str, user_prompt: str, response_schema: Any = None) -> Any:
        self.ordinal += 1
        attempt = self.ordinal - 1
        correlation = self.sink.correlation(self.run_id)
        started = time.monotonic()
        input_hash = stable_fingerprint({
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "response_schema": response_schema,
        })
        cycle = "" if self.recovery_cycle is None else f":{self.recovery_cycle}"
        invocation_id = f"{self.run_id}:{self.stage}{cycle}:{attempt}:{uuid.uuid4().hex}"
        request_id = f"{self.run_id}:{self.stage}{cycle}:attempt:{attempt}"
        before = max(0, self.max_attempts - attempt)
        after = max(0, before - 1)
        budget_key = f"{self.stage}_attempts_remaining"
        try:
            response = self.inner.complete(system_prompt, user_prompt, response_schema)
        except Exception as exc:
            if correlation is not None:
                logical_id, model_tag, runtime_kind, runtime_hash = _runtime_metadata(
                    self.inner, None, self.logical_model_id
                )
                self.sink.record(self.run_id, ModelInvocationManifestV1(
                    schema_version=1,
                    invocation_id=invocation_id,
                    request_kind=self.kind,
                    mission_id=correlation.mission_id,
                    goal_id=correlation.goal_id,
                    todo_id=self.todo_id,
                    run_id=self.run_id,
                    request_id=request_id,
                    attempt_index=attempt,
                    slice_index=correlation.slice_index,
                    logical_model_id=logical_id,
                    model_tag=model_tag,
                    runtime_kind=runtime_kind,
                    runtime_fingerprint=runtime_hash,
                    input_fingerprint=input_hash,
                    budget_before={budget_key: before},
                    budget_after={budget_key: after},
                    duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                    status=ModelInvocationStatusV1.FAILED,
                    diagnostic=_compact_diagnostic(exc),
                ))
            raise

        if correlation is not None:
            logical_id, model_tag, runtime_kind, runtime_hash = _runtime_metadata(
                self.inner, response, self.logical_model_id
            )
            content = getattr(response, "content", response)
            self.sink.record(self.run_id, ModelInvocationManifestV1(
                schema_version=1,
                invocation_id=invocation_id,
                request_kind=self.kind,
                mission_id=correlation.mission_id,
                goal_id=correlation.goal_id,
                todo_id=self.todo_id,
                run_id=self.run_id,
                request_id=request_id,
                attempt_index=attempt,
                slice_index=correlation.slice_index,
                logical_model_id=logical_id,
                model_tag=model_tag,
                runtime_kind=runtime_kind,
                runtime_fingerprint=runtime_hash,
                input_fingerprint=input_hash,
                output_fingerprint=_output_fingerprint(content),
                requested_context_length=_non_negative_int(getattr(response, "context_length", None)),
                observed_context_length=_non_negative_int(getattr(response, "context_length", None)),
                budget_before={budget_key: before},
                budget_after={budget_key: after},
                duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                status=ModelInvocationStatusV1.SUCCESS,
            ))
        return response


class TracingJudgeCoordinator:
    """Observe physical Judge runtime calls without emitting events for reused turns."""

    def __init__(self, inner: Any, registry: Any) -> None:
        self.inner = inner
        self.sink = _TraceSink(registry)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def interpret_public_requirements(self, frozen):
        with _temporary_innermost(self.inner, 'runtime', lambda physical: _OrnithRuntimeProxy(
            physical, self.sink, run_id=frozen.run_id, todo_id=frozen.todo_id,
            kind=ModelInvocationKindV1.JUDGE_REVIEW, logical_model_id=self.inner.policy.model_id,
            stage='public_acceptance_baseline', max_attempts=1)):
            return self.inner.interpret_public_requirements(frozen)

    def review(self, *, run_id: str, contract: Any, lease_id: str) -> Any:
        policy = getattr(self.inner, "policy", None)
        with _temporary_innermost(
            self.inner,
            "runtime",
            lambda physical: _OrnithRuntimeProxy(
                physical,
                self.sink,
                run_id=run_id,
                todo_id=contract.todo_id,
                kind=ModelInvocationKindV1.JUDGE_REVIEW,
                logical_model_id=getattr(policy, "model_id", "ornith_lead"),
                  stage="judge",
                  # Physical telemetry includes the separately bounded public
                  # comparison; final Judge retry policy itself is unchanged.
                  max_attempts=getattr(policy, "max_model_attempts", 1),
            ),
        ):
            return self.inner.review(run_id=run_id, contract=contract, lease_id=lease_id)


class PublicInterpreterTraceObserver:
    """Role-separated completed stage telemetry; durable artifacts own recovery."""
    def __init__(self, registry):
        self.sink = _TraceSink(registry)

    def __call__(self, identity, result):
        run_id = identity['run_id']
        correlation = self.sink.correlation(run_id)
        if correlation is None:
            return
        success = result['disposition'] == 'COMPLETE'
        model = identity['model']
        request_id = run_id + ':public-interpreter:' + stable_fingerprint(identity)
        recorder = self.sink.registry.resolve_for_run(run_id)
        if not any(event.get('event_type') == 'PUBLIC_INTERPRETER_STAGE_COMPLETE'
                   and event.get('model_call_id') == request_id for event in recorder.events()):
            recorder.record('PUBLIC_INTERPRETER_STAGE_COMPLETE',run_id=run_id,
                todo_id=identity['todo_id'],actor=model['model_id'],model_call_id=request_id,
                duration_ms=max(0,int(result['wall_seconds']*1000)),
                payload={'role':'PUBLIC_INTERPRETER','identity':identity,
                    'disposition':result['disposition'],
                    'generation_dispatched':result.get('generation_dispatched',True),
                    'error_code':result.get('error_code'),
                    'generation_metadata':result.get('generation_metadata',{})})
        if result.get('generation_dispatched') is False:
            return
        existing = [event for event in recorder.events()
            if event.get('event_type') == 'MODEL_INVOCATION_MANIFEST'
            and event.get('model_call_id') == request_id]
        if existing:
            if any(event['payload']['input_fingerprint'] != identity['request_fingerprint']
                   or event['payload']['runtime_fingerprint'] != stable_fingerprint(model) for event in existing):
                raise ValueError('public interpreter invocation trace identity conflict')
            return
        self.sink.record(run_id, ModelInvocationManifestV1(
            schema_version=1, invocation_id=request_id,
            request_kind=ModelInvocationKindV1(identity['stage']),
            mission_id=correlation.mission_id, goal_id=correlation.goal_id,
            run_id=run_id, todo_id=identity['todo_id'], request_id=request_id,
            attempt_index=identity['attempt_index'], slice_index=correlation.slice_index,
            logical_model_id=model['model_id'], model_tag=model['model_tag'],
            runtime_kind='PUBLIC_INTERPRETER', runtime_fingerprint=stable_fingerprint(model),
            input_fingerprint=identity['request_fingerprint'],
            output_fingerprint=stable_fingerprint(result['result']) if success else None,
            duration_ms=max(0,int(result['wall_seconds']*1000)),
            status=ModelInvocationStatusV1.SUCCESS if success else ModelInvocationStatusV1.FAILED,
            requested_context_length=32768,
            diagnostic=None if success else result['error_code']))


class _DiagnosisNativeProxy:
    def __init__(
        self,
        inner: Any,
        sink: _TraceSink,
        *,
        run_id: str,
        todo_id: str,
        logical_model_id: str,
        max_attempts: int,
        recovery_cycle: int,
    ) -> None:
        self.inner = inner
        self.sink = sink
        self.run_id = run_id
        self.todo_id = todo_id
        self.logical_model_id = logical_model_id
        self.max_attempts = max(1, int(max_attempts))
        self.recovery_cycle = recovery_cycle
        self.ordinal = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def chat(
        self,
        base_url: str,
        model_tag: str,
        messages: Any,
        keep_alive: int | str,
        num_ctx: int,
        *,
        num_gpu: int | None = None,
        format_schema: Any = None,
        temperature: float | None = None,
    ) -> dict[str, Any]:
        self.ordinal += 1
        attempt = self.ordinal - 1
        correlation = self.sink.correlation(self.run_id)
        started = time.monotonic()
        input_hash = stable_fingerprint({
            "model_tag": model_tag,
            "messages": _message_projection(messages),
            "keep_alive": keep_alive,
            "num_ctx": num_ctx,
            "num_gpu": num_gpu,
            "format_schema": format_schema,
            "temperature": temperature,
        })
        invocation_id = (
            f"{self.run_id}:recovery:{self.recovery_cycle}:diagnosis:"
            f"{attempt}:{uuid.uuid4().hex}"
        )
        request_id = (
            f"{self.run_id}:recovery:{self.recovery_cycle}:diagnosis:attempt:{attempt}"
        )
        before = max(0, self.max_attempts - attempt)
        after = max(0, before - 1)
        runtime_hash = runtime_fingerprint({
            "runtime_kind": "ollama_native_diagnosis",
            "logical_model_id": self.logical_model_id,
            "model_tag": model_tag,
            "base_url": base_url.rstrip("/"),
            "num_ctx": num_ctx,
            "num_gpu": num_gpu,
        })
        try:
            payload = self.inner.chat(
                base_url,
                model_tag,
                messages,
                keep_alive,
                num_ctx,
                num_gpu=num_gpu,
                format_schema=format_schema,
                temperature=temperature,
            )
        except Exception as exc:
            if correlation is not None:
                self.sink.record(self.run_id, ModelInvocationManifestV1(
                    schema_version=1,
                    invocation_id=invocation_id,
                    request_kind=ModelInvocationKindV1.RECOVERY_DIAGNOSIS,
                    mission_id=correlation.mission_id,
                    goal_id=correlation.goal_id,
                    todo_id=self.todo_id,
                    run_id=self.run_id,
                    request_id=request_id,
                    attempt_index=attempt,
                    slice_index=correlation.slice_index,
                    logical_model_id=self.logical_model_id,
                    model_tag=model_tag,
                    runtime_kind="ollama_native_diagnosis",
                    runtime_fingerprint=runtime_hash,
                    input_fingerprint=input_hash,
                    requested_context_length=_non_negative_int(num_ctx),
                    budget_before={"diagnosis_attempts_remaining": before},
                    budget_after={"diagnosis_attempts_remaining": after},
                    duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                    status=ModelInvocationStatusV1.FAILED,
                    diagnostic=_compact_diagnostic(exc),
                ))
            raise

        if correlation is not None:
            message = payload.get("message") if isinstance(payload, Mapping) else None
            content = message.get("content") if isinstance(message, Mapping) else None
            self.sink.record(self.run_id, ModelInvocationManifestV1(
                schema_version=1,
                invocation_id=invocation_id,
                request_kind=ModelInvocationKindV1.RECOVERY_DIAGNOSIS,
                mission_id=correlation.mission_id,
                goal_id=correlation.goal_id,
                todo_id=self.todo_id,
                run_id=self.run_id,
                request_id=request_id,
                attempt_index=attempt,
                slice_index=correlation.slice_index,
                logical_model_id=self.logical_model_id,
                model_tag=model_tag,
                runtime_kind="ollama_native_diagnosis",
                runtime_fingerprint=runtime_hash,
                input_fingerprint=input_hash,
                output_fingerprint=_output_fingerprint(
                    content if isinstance(content, str) else payload
                ),
                prompt_tokens=_non_negative_int(payload.get("prompt_eval_count")) if isinstance(payload, Mapping) else None,
                completion_tokens=_non_negative_int(payload.get("eval_count")) if isinstance(payload, Mapping) else None,
                requested_context_length=_non_negative_int(num_ctx),
                observed_context_length=_non_negative_int(num_ctx),
                budget_before={"diagnosis_attempts_remaining": before},
                budget_after={"diagnosis_attempts_remaining": after},
                duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                status=ModelInvocationStatusV1.SUCCESS,
            ))
        return payload


class TracingRecoveryBridge:
    'Trace diagnosis/replan transports while leaving recovery recovery authoritative.'

    def __init__(self, inner: Any, registry: Any) -> None:
        self.inner = inner
        self.sink = _TraceSink(registry)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def recover(self, todo: Any, **kwargs: Any) -> Any:
        run_id = _text(getattr(todo, "run_id", None))
        if run_id is None:
            return self.inner.recover(todo, **kwargs)
        policy = getattr(self.inner, "policy", None)
        cycle = int(kwargs.get("recovery_cycle", 0))
        diagnosis_client = self.inner.diagnosis_client
        replan_client = self.inner.replan_client
        with _temporary_innermost(
            diagnosis_client,
            "native",
            lambda physical: _DiagnosisNativeProxy(
                physical,
                self.sink,
                run_id=run_id,
                todo_id=todo.todo_id,
                logical_model_id=getattr(policy, "diagnosis_model_id", "ministral_reasoner"),
                max_attempts=getattr(policy, "max_diagnosis_model_attempts", 1),
                recovery_cycle=cycle,
            ),
        ):
            with _temporary_innermost(
                replan_client,
                "runtime",
                lambda physical: _OrnithRuntimeProxy(
                    physical,
                    self.sink,
                    run_id=run_id,
                    todo_id=todo.todo_id,
                    kind=ModelInvocationKindV1.RECOVERY_REPLAN,
                    logical_model_id=getattr(policy, "replan_model_id", "ornith_lead"),
                    stage="replan",
                    max_attempts=getattr(policy, "max_replan_model_attempts", 1),
                    recovery_cycle=cycle,
                ),
            ):
                return self.inner.recover(todo, **kwargs)


__all__ = [
    "TracingCriticCoordinator",
    "TracingJudgeCoordinator",
    'TracingRecoveryBridge',
]

# Verified compatibility aliases for historical Python interfaces.
TracingPhase15RecoveryBridge = TracingRecoveryBridge

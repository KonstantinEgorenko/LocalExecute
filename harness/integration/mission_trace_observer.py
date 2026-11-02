"""Mission-scoped trace registry and one-way projections of neutral ports."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .mission_flight_recorder import MissionFlightRecorder
from .model_invocation_lineage import (
    ModelInvocationKindV1,
    ModelInvocationManifestV1,
    ModelInvocationStatusV1,
    runtime_fingerprint,
    stable_fingerprint,
)


def _serialized_bytes(value: Any) -> int:
    """UTF-8 size of canonical JSON telemetry input without persisting it."""
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return len(encoded)


def _enum_trace_value(value: Any) -> Any:
    """Project enums to their stable wire value without expanding content."""
    return getattr(value, "value", value)


def _artifact_ref_trace_payload(artifact_ref: Any | None) -> dict[str, Any] | None:
    """Content-free ArtifactRef projection for durable tracing."""
    if artifact_ref is None:
        return None
    return {
        "artifact_id": artifact_ref.artifact_id,
        "kind": _enum_trace_value(artifact_ref.kind),
        "sha256": artifact_ref.sha256,
        "size_bytes": artifact_ref.size_bytes,
    }


def _context_item_trace_payload(item: Any | None) -> dict[str, Any] | None:
    """Content-minimized ContextItem projection for durable tracing."""
    if item is None:
        return None

    content = item.content
    provenance = item.provenance

    return {
        "item_id": item.item_id,
        "source": _enum_trace_value(item.source),
        "required": item.required,
        "priority": item.priority,
        "token_estimate": item.token_estimate,
        "content_fingerprint": stable_fingerprint(content),
        "content_bytes": len(content.encode("utf-8")),
        "provenance_fingerprint": stable_fingerprint(provenance),
        "provenance_bytes": len(provenance.encode("utf-8")),
        "artifact_ref": _artifact_ref_trace_payload(item.artifact_ref),
    }


class MissionTraceCorrelationError(RuntimeError):
    """An autonomous production boundary lacks its authoritative mission binding."""


@dataclass(frozen=True, slots=True)
class MissionTraceCorrelationV1:
    mission_id: str
    goal_id: str
    todo_id: str | None = None
    run_id: str | None = None
    attempt_index: int | None = None
    slice_index: int | None = None
    candidate_lease_id: str | None = None


class MissionTraceRegistry:
    def __init__(self, state_root: Path) -> None:
        self.state_root = Path(state_root)
        self._recorders: dict[str, MissionFlightRecorder] = {}
        self._missions_by_goal: dict[str, str] = {}
        self._missions_by_todo: dict[str, str] = {}
        self._missions_by_run: dict[str, str] = {}
        self._correlations_by_run: dict[str, MissionTraceCorrelationV1] = {}

    def get(self, mission_id: str, goal_id: str) -> MissionFlightRecorder:
        recorder = self._recorders.get(mission_id)
        if recorder is None:
            recorder = MissionFlightRecorder(self.state_root, mission_id=mission_id, goal_id=goal_id)
            self._recorders[mission_id] = recorder
        elif recorder.goal_id != goal_id:
            raise MissionTraceCorrelationError("conflicting mission/goal trace binding")
        prior = self._missions_by_goal.get(goal_id)
        if prior not in (None, mission_id):
            raise MissionTraceCorrelationError("conflicting goal trace binding")
        self._missions_by_goal[goal_id] = mission_id
        return recorder

    def bind_todo(self, correlation: MissionTraceCorrelationV1) -> None:
        if correlation.todo_id is None:
            raise ValueError("todo_id is required")
        self.get(correlation.mission_id, correlation.goal_id)
        prior = self._missions_by_todo.get(correlation.todo_id)
        if prior not in (None, correlation.mission_id):
            raise MissionTraceCorrelationError("conflicting todo trace binding")
        self._missions_by_todo[correlation.todo_id] = correlation.mission_id

    def bind_run(self, correlation: MissionTraceCorrelationV1) -> None:
        if correlation.run_id is None or correlation.todo_id is None:
            raise MissionTraceCorrelationError("run/todo trace binding requires both identities")
        self.bind_todo(correlation)
        prior = self._correlations_by_run.get(correlation.run_id)
        if prior not in (None, correlation):
            raise MissionTraceCorrelationError("conflicting run trace binding")
        self._missions_by_run[correlation.run_id] = correlation.mission_id
        self._correlations_by_run[correlation.run_id] = correlation

    def bind_slice(self, run_id: str, slice_index: int) -> None:
        prior = self.correlation_for_run(run_id)
        self._correlations_by_run[run_id] = MissionTraceCorrelationV1(prior.mission_id, prior.goal_id, prior.todo_id, run_id, prior.attempt_index, slice_index, prior.candidate_lease_id)

    def bind_candidate(self, run_id: str, candidate_lease_id: str) -> None:
        prior = self.correlation_for_run(run_id)
        self._correlations_by_run[run_id] = MissionTraceCorrelationV1(prior.mission_id, prior.goal_id, prior.todo_id, run_id, prior.attempt_index, prior.slice_index, candidate_lease_id)

    def correlation_for_run(self, run_id: str) -> MissionTraceCorrelationV1:
        value = self._correlations_by_run.get(run_id)
        if value is None:
            raise MissionTraceCorrelationError("missing run trace binding")
        return value
    def correlation_for_goal(self, goal_id: str) -> MissionTraceCorrelationV1:
        mission_id = self._missions_by_goal.get(goal_id)
        if mission_id is None:
            raise MissionTraceCorrelationError("missing goal trace binding")
        return MissionTraceCorrelationV1(mission_id, goal_id)

    def resolve_for_goal(self, goal_id: str) -> MissionFlightRecorder:
        mission_id = self._missions_by_goal.get(goal_id)
        if mission_id is None:
            raise MissionTraceCorrelationError("missing goal trace binding")
        return self._recorders[mission_id]

    def resolve_for_todo(self, todo_id: str) -> MissionFlightRecorder:
        mission_id = self._missions_by_todo.get(todo_id)
        if mission_id is None:
            raise MissionTraceCorrelationError("missing todo trace binding")
        return self._recorders[mission_id]

    def resolve_for_run(self, run_id: str) -> MissionFlightRecorder:
        mission_id = self._missions_by_run.get(run_id)
        if mission_id is None:
            raise MissionTraceCorrelationError("missing run trace binding")
        return self._recorders[mission_id]


class MissionTraceObserver:
    def __init__(self, registry: MissionTraceRegistry, *, strict: bool = False) -> None:
        self.registry, self.strict = registry, strict
        self._active_model_calls: dict[str, tuple[str, float, str, str | None, str | None]] = {}
        self._last_model_call_ids: dict[str, str] = {}
        self._model_call_ordinals: dict[str, int] = {}
        self._tool_call_ordinals: dict[str, int] = {}
        self._active_tool_calls: dict[tuple[str, str], tuple[str, str | None]] = {}
        self._context_bundle_ids: dict[tuple[str, str], str] = {}

    def _goal(self, goal_id: str):
        try:
            return self.registry.resolve_for_goal(goal_id)
        except MissionTraceCorrelationError:
            if self.strict:
                raise
            return None

    def _run(self, run_id: str):
        try:
            return self.registry.resolve_for_run(run_id)
        except MissionTraceCorrelationError:
            if self.strict:
                raise
            return None

    def build_started(self, *, request, budget: Mapping[str, int]) -> None:
        recorder = self._goal(request.goal_id)
        if recorder: recorder.record("AGENT_CONTEXT_BUILD_STARTED", todo_id=request.todo_id, payload={"budget": dict(budget)})
    def item_considered(self, *, request, item, required, selected, omission_reason=None, artifact_ref=None) -> None:
        recorder = self._goal(request.goal_id)
        if recorder:
            recorder.record(
                "AGENT_CONTEXT_ITEM_CONSIDERED",
                todo_id=request.todo_id,
                payload={
                    "required": required,
                    "selected": selected,
                    "omission_reason": omission_reason,
                    "item": _context_item_trace_payload(item),
                    "artifact_ref": _artifact_ref_trace_payload(artifact_ref),
                },
            )
    def budget_decision(self, *, request, required_items, optional_items, selected_items, omitted_items, required_total, selected_total, overflow, top_required_contributors, status) -> None:
        recorder = self._goal(request.goal_id)
        if recorder:
            recorder.record(
                "AGENT_CONTEXT_BUDGET_DECISION",
                todo_id=request.todo_id,
                payload={
                    "status": status,
                    "budget": request.budget.to_dict(),
                    "required_total": dict(required_total),
                    "selected_total": dict(selected_total),
                    "overflow": dict(overflow),
                    "top_required_contributors": [
                        _context_item_trace_payload(item)
                        for item in top_required_contributors
                    ],
                },
            )
    def build_finished(self, *, request, bundle, required_total, selected_total, omitted_items) -> None:
        self._context_bundle_ids[(request.goal_id, request.todo_id)] = bundle.bundle_id
        recorder = self._goal(request.goal_id)
        if recorder: recorder.record("AGENT_CONTEXT_BUILD_FINISHED", todo_id=request.todo_id, payload={"bundle_id": bundle.bundle_id})

    def routing_started(self, *, run_id, todo_id, attempt_index) -> None:
        recorder = self._run(run_id)
        if recorder:
            recorder.record("MODEL_ROUTING_STARTED", run_id=run_id, todo_id=todo_id, attempt_index=attempt_index)

    def routing_decision(self, *, run_id, todo_id, attempt_index, decision) -> None:
        recorder = self._run(run_id)
        if recorder: recorder.record("MODEL_ROUTING_DECISION", run_id=run_id, todo_id=todo_id, attempt_index=attempt_index, payload=decision.to_dict())
    def _active_model_call_id_for_run(self, run_id: str) -> str | None:
        active = self._active_model_calls.get(run_id)
        return active[0] if active is not None else self._last_model_call_ids.get(run_id)

    def _next_model_call_ordinal(self, run_id: str, recorder: MissionFlightRecorder) -> int:
        current = self._model_call_ordinals.get(run_id)
        if current is None:
            prefix = f"{run_id}:model:"
            current = 0
            for event in recorder.events():
                call_id = event.get("model_call_id")
                if isinstance(call_id, str) and call_id.startswith(prefix):
                    suffix = call_id[len(prefix):]
                    if suffix.isdigit():
                        current = max(current, int(suffix))
        ordinal = current + 1
        self._model_call_ordinals[run_id] = ordinal
        return ordinal

    @staticmethod
    def _worker_request_fingerprint(decision, request) -> str:
        return stable_fingerprint({
            "request_id": getattr(request, "request_id", None),
            "task_id": getattr(request, "task_id", None),
            "routing_decision": decision.to_dict(),
            "messages": [
                {"role": getattr(message, "role", None), "content": getattr(message, "content", "")}
                for message in request.messages
            ],
            "response_format": getattr(request, "response_format", None),
            "temperature": getattr(request, "temperature", None),
            "keep_alive": getattr(request, "keep_alive", None),
        })

    def model_call_started(self, *, run_id, todo_id, attempt_index, decision, request) -> None:
        recorder = self._run(run_id)
        if run_id in self._active_model_calls:
            raise MissionTraceCorrelationError("overlapping model trace calls for run")
        correlation = self.registry.correlation_for_run(run_id)
        ordinal = self._next_model_call_ordinal(run_id, recorder)
        model_call_id = f"{run_id}:model:{ordinal}"
        input_fingerprint = self._worker_request_fingerprint(decision, request)
        message_bytes = sum(
            len(str(getattr(message, "content", "")).encode("utf-8"))
            for message in request.messages
        )
        self._active_model_calls[run_id] = (
            model_call_id,
            time.monotonic(),
            input_fingerprint,
            getattr(request, "request_id", None),
            self._context_bundle_ids.get((correlation.goal_id, todo_id)),
        )
        if recorder:
            recorder.record(
                "MODEL_CALL_STARTED",
                run_id=run_id,
                todo_id=todo_id,
                attempt_index=attempt_index,
                actor=decision.selected_model,
                model_call_id=model_call_id,
                payload={
                    "input_fingerprint": input_fingerprint,
                    "message_count": len(request.messages),
                    "message_bytes": message_bytes,
                },
            )

    def model_call_finished(self, *, run_id, todo_id, attempt_index, decision, result) -> None:
        active = self._active_model_calls.pop(run_id, None)
        if active is None:
            raise MissionTraceCorrelationError("model trace finish has no active model call")
        model_call_id, started, input_fingerprint, request_id, context_bundle_id = active
        self._last_model_call_ids[run_id] = model_call_id
        recorder = self._run(run_id)
        correlation = self.registry.correlation_for_run(run_id)
        content = result.content or ""
        duration_ms = max(0, int((time.monotonic() - started) * 1000))
        logical_model_id = getattr(result, "model_id", None) or decision.selected_model
        model_tag = getattr(result, "model_tag", None)
        runtime_values = {
            "runtime_kind": "worker_executor",
            "logical_model_id": logical_model_id,
            "model_tag": model_tag,
            "requested_context_length": getattr(result, "requested_context_length", None),
            "configured_num_gpu": getattr(result, "configured_num_gpu", None),
        }
        if recorder:
            recorder.record(
                "MODEL_CALL_FINISHED",
                run_id=run_id,
                todo_id=todo_id,
                attempt_index=attempt_index,
                actor=decision.selected_model,
                model_call_id=model_call_id,
                duration_ms=duration_ms,
                payload={
                    "response_fingerprint": recorder.fingerprint(content),
                    "response_bytes": len(content.encode("utf-8")),
                },
            )
            recorder.record_model_invocation(ModelInvocationManifestV1(
                schema_version=1,
                invocation_id=model_call_id,
                request_kind=ModelInvocationKindV1.WORKER_EXECUTION,
                mission_id=correlation.mission_id,
                goal_id=correlation.goal_id,
                todo_id=todo_id,
                run_id=run_id,
                request_id=request_id,
                attempt_index=attempt_index,
                slice_index=correlation.slice_index,
                logical_model_id=logical_model_id,
                model_tag=model_tag,
                runtime_kind="worker_executor",
                runtime_fingerprint=runtime_fingerprint(runtime_values),
                context_bundle_id=context_bundle_id,
                input_fingerprint=input_fingerprint,
                output_fingerprint=stable_fingerprint(content),
                prompt_tokens=getattr(result, "prompt_eval_count", None),
                completion_tokens=getattr(result, "eval_count", None),
                requested_context_length=getattr(result, "requested_context_length", None),
                observed_context_length=getattr(result, "observed_context_length", None),
                duration_ms=duration_ms,
                status=ModelInvocationStatusV1.SUCCESS,
            ))

    def model_call_failed(self, *, run_id, todo_id, attempt_index, decision, error) -> None:
        active = self._active_model_calls.pop(run_id, None)
        if active is None:
            raise MissionTraceCorrelationError("model trace failure has no active model call")
        model_call_id, started, input_fingerprint, request_id, context_bundle_id = active
        self._last_model_call_ids[run_id] = model_call_id
        recorder = self._run(run_id)
        correlation = self.registry.correlation_for_run(run_id)
        duration_ms = max(0, int((time.monotonic() - started) * 1000))
        diagnostic_text = str(error)
        diagnostic_fingerprint = stable_fingerprint({
            "exception_type": type(error).__name__,
            "message": diagnostic_text,
        })
        diagnostic = (
            f"{type(error).__name__}: "
            f"fingerprint={diagnostic_fingerprint}; "
            f"bytes={len(diagnostic_text.encode('utf-8'))}"
        )
        if recorder:
            from harness.transport_diagnostics import error_projection
            recorder.record(
                "MODEL_CALL_FAILED",
                run_id=run_id,
                todo_id=todo_id,
                attempt_index=attempt_index,
                actor=decision.selected_model,
                model_call_id=model_call_id,
                duration_ms=duration_ms,
                payload={
                    "exception_type": type(error).__name__,
                    "diagnostic_fingerprint": diagnostic_fingerprint,
                    "diagnostic_bytes": len(diagnostic_text.encode("utf-8")),
                    **error_projection(error),
                },
            )
            recorder.record_model_invocation(ModelInvocationManifestV1(
                schema_version=1,
                invocation_id=model_call_id,
                request_kind=ModelInvocationKindV1.WORKER_EXECUTION,
                mission_id=correlation.mission_id,
                goal_id=correlation.goal_id,
                todo_id=todo_id,
                run_id=run_id,
                request_id=request_id,
                attempt_index=attempt_index,
                slice_index=correlation.slice_index,
                logical_model_id=decision.selected_model,
                runtime_kind="worker_executor",
                runtime_fingerprint=runtime_fingerprint({
                    "runtime_kind": "worker_executor",
                    "logical_model_id": decision.selected_model,
                }),
                context_bundle_id=context_bundle_id,
                input_fingerprint=input_fingerprint,
                duration_ms=duration_ms,
                status=ModelInvocationStatusV1.FAILED,
                diagnostic=diagnostic,
            ))

    def tool_call_started(self, *, run_id, todo_id, attempt_index, request) -> None:
        recorder = self._run(run_id)
        ordinal = self._tool_call_ordinals.get(run_id, 0) + 1
        self._tool_call_ordinals[run_id] = ordinal
        tool_call_id = f"{run_id}:tool:{ordinal}"
        model_call_id = self._active_model_call_id_for_run(run_id)
        self._active_tool_calls[(run_id, request.call_id)] = (tool_call_id, model_call_id)

        arguments = request.to_dict().get("arguments", {})
        if recorder:
            recorder.record(
                "TOOL_CALL_STARTED",
                run_id=run_id,
                todo_id=todo_id,
                attempt_index=attempt_index,
                model_call_id=model_call_id,
                tool_call_id=tool_call_id,
                payload={
                    "request_call_id": request.call_id,
                    "tool": request.tool.value,
                    "arguments_fingerprint": stable_fingerprint(arguments),
                    "arguments_bytes": _serialized_bytes(arguments),
                    "argument_count": len(arguments),
                },
            )

    def tool_call_finished(self, *, run_id, todo_id, attempt_index, request, result) -> None:
        active = self._active_tool_calls.pop((run_id, request.call_id), None)
        if active is None:
            raise MissionTraceCorrelationError("tool trace finish has no active tool call")
        tool_call_id, model_call_id = active
        recorder = self._run(run_id)

        result_payload = result.to_dict()
        output = result_payload.get("output", "")
        metadata = result_payload.get("metadata", {})
        changed_paths = result_payload.get("changed_paths", [])

        if recorder:
            recorder.record(
                "TOOL_CALL_FINISHED",
                run_id=run_id,
                todo_id=todo_id,
                attempt_index=attempt_index,
                model_call_id=model_call_id,
                tool_call_id=tool_call_id,
                payload={
                    "request_call_id": request.call_id,
                    "tool": result.tool.value,
                    "status": result.status.value,
                    "output_fingerprint": stable_fingerprint(output),
                    "output_bytes": len(output.encode("utf-8")),
                    "metadata_fingerprint": stable_fingerprint(metadata),
                    "metadata_bytes": _serialized_bytes(metadata),
                    "metadata_count": len(metadata),
                    "changed_paths_fingerprint": stable_fingerprint(changed_paths),
                    "changed_path_count": len(changed_paths),
                },
            )

    def tool_call_failed(self, *, run_id, todo_id, attempt_index, request, error) -> None:
        active = self._active_tool_calls.pop((run_id, request.call_id), None)
        if active is None:
            raise MissionTraceCorrelationError("tool trace failure has no active tool call")
        tool_call_id, model_call_id = active
        recorder = self._run(run_id)

        diagnostic_text = str(error)
        diagnostic_fingerprint = stable_fingerprint({
            "exception_type": type(error).__name__,
            "message": diagnostic_text,
        })

        if recorder:
            recorder.record(
                "TOOL_CALL_FAILED",
                run_id=run_id,
                todo_id=todo_id,
                attempt_index=attempt_index,
                model_call_id=model_call_id,
                tool_call_id=tool_call_id,
                payload={
                    "request_call_id": request.call_id,
                    "tool": request.tool.value,
                    "exception_type": type(error).__name__,
                    "diagnostic_fingerprint": diagnostic_fingerprint,
                    "diagnostic_bytes": len(diagnostic_text.encode("utf-8")),
                },
            )

class TracingContextResolver:
    def __init__(self, inner: Any, observer: MissionTraceObserver) -> None:
        self.inner, self.observer = inner, observer
    def assemble(self, *args, **kwargs):
        kwargs["observer"] = self.observer
        return self.inner.assemble(*args, **kwargs)

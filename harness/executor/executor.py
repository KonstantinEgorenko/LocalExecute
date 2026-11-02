"""One-shot executor: prepare a selected worker and issue one native chat call."""
from __future__ import annotations

from typing import Any, Callable

from ..errors import ExecutionError, WorkerGenerationError, WorkerProtocolError
from ..resource_manager.snapshot import ResourceSnapshot
from .client import OllamaNativeClient
from .lifecycle import WorkerLifecycleManager
from .models import ExecutionFailureContext, ExecutionOutcome, ExecutionRequest, ExecutionStage, LifecycleAction, WorkerResult


class WorkerExecutor:
    def __init__(self, lifecycle: WorkerLifecycleManager, client: OllamaNativeClient, snapshot_provider: Callable[[], ResourceSnapshot]) -> None:
        self.lifecycle, self.client, self.snapshot_provider = lifecycle, client, snapshot_provider

    def execute(self, request: ExecutionRequest) -> WorkerResult:
        prepared = None
        try:
            with self.lifecycle.prepared(request.routing_decision) as prepared:
                try:
                    kwargs = {"num_gpu": prepared.configured_num_gpu}
                    if request.response_format is not None:
                        kwargs["format_schema"] = (
                            dict(request.response_format)
                            if hasattr(request.response_format, "items")
                            else request.response_format
                        )
                    if request.temperature is not None:
                        kwargs["temperature"] = request.temperature
                    response = self.client.chat(
                        prepared.base_url,
                        prepared.model_tag,
                        request.messages,
                        request.keep_alive,
                        prepared.requested_context_length,
                        **kwargs,
                    )
                except ExecutionError as exc:
                    self.lifecycle.mark_failure(exc, ExecutionStage.GENERATION, prepared)
                    raise
            after = self.snapshot_provider()
            try: return self._parse_response(response, prepared, after)
            except ExecutionError as exc:
                self.lifecycle.mark_failure(exc, ExecutionStage.RESPONSE_VALIDATION, prepared)
                exc.failure_context = ExecutionFailureContext(prepared.actions, after, prepared.model_id, prepared.model_tag, ExecutionStage.RESPONSE_VALIDATION)
                raise
        except ExecutionError: raise

    @staticmethod
    def _parse_response(response: dict[str, Any], prepared, after: ResourceSnapshot) -> WorkerResult:
        if not isinstance(response, dict): raise WorkerProtocolError("Malformed /api/chat response: expected object")
        response_model, message, done = response.get("model"), response.get("message"), response.get("done")
        if not isinstance(response_model, str) or not response_model.strip(): raise WorkerProtocolError("Malformed /api/chat response: model is required")
        if response_model != prepared.model_tag: raise WorkerProtocolError("Malformed /api/chat response: model tag differs from request")
        if not isinstance(done, bool) or not isinstance(message, dict): raise WorkerProtocolError("Malformed /api/chat response envelope")
        if not done: raise WorkerGenerationError("Worker response did not complete (done is false)")
        content, thinking, tool_calls = message.get("content"), message.get("thinking"), message.get("tool_calls", [])
        if not isinstance(content, str): raise WorkerProtocolError("Malformed /api/chat message content")
        if thinking is not None and not isinstance(thinking, str): raise WorkerProtocolError("Malformed /api/chat thinking")
        if not isinstance(tool_calls, list) or any(not isinstance(item, dict) for item in tool_calls): raise WorkerProtocolError("Malformed /api/chat tool_calls")
        numeric = ("prompt_eval_count", "eval_count", "load_duration", "prompt_eval_duration", "eval_duration", "total_duration")
        if any(key in response and (not isinstance(response[key], int) or isinstance(response[key], bool)) for key in numeric): raise WorkerProtocolError("Malformed /api/chat metric")
        done_reason = response.get("done_reason")
        if done_reason is not None and not isinstance(done_reason, str): raise WorkerProtocolError("Malformed /api/chat done_reason")
        return WorkerResult(ExecutionOutcome.SUCCESS, prepared.model_id, prepared.model_tag, content, thinking, tuple(tool_calls), *(response.get(key) for key in numeric), done_reason, prepared.actions, prepared.requested_context_length, prepared.observed_context_length, prepared.configured_num_gpu, prepared.before, after)
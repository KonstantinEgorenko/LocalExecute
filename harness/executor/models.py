"""Immutable contracts for worker lifecycle, execution, and failure reporting."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
import math
from typing import Any, Mapping

from ..resource_manager.snapshot import ResourceSnapshot
from ..router.scoring import RoutingDecision


class LifecycleActionKind(StrEnum):
    PRELOAD = "PRELOAD"
    UNLOAD = "UNLOAD"


class ExecutionOutcome(StrEnum):
    """SUCCESS is returned; failures deliberately travel as domain exceptions."""

    SUCCESS = "SUCCESS"
    REPLAN_REQUIRED = "REPLAN_REQUIRED"
    EXECUTION_FAILURE = "EXECUTION_FAILURE"


class ExecutionStage(StrEnum):
    PREFLIGHT = "preflight"
    UNLOAD = "unload"
    UNLOAD_VERIFICATION = "unload_verification"
    PRELOAD = "preload"
    PRELOAD_VERIFICATION = "preload_verification"
    GENERATION = "generation"
    RESPONSE_VALIDATION = "response_validation"


@dataclass(frozen=True, slots=True)
class ExecutionMessage:
    role: str
    content: str

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass(frozen=True, slots=True)
class LifecycleAction:
    kind: LifecycleActionKind
    model_id: str
    model_tag: str

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind.value, "model_id": self.model_id, "model_tag": self.model_tag}


@dataclass(frozen=True, slots=True)
class ExecutionFailureContext:
    lifecycle_actions: tuple[LifecycleAction, ...]
    runtime_state: ResourceSnapshot | None
    model_id: str | None
    model_tag: str | None
    stage: ExecutionStage

    def to_dict(self) -> dict[str, Any]:
        return {
            "lifecycle_actions": [item.to_dict() for item in self.lifecycle_actions],
            "runtime_state": self.runtime_state.to_dict() if self.runtime_state else None,
            "model_id": self.model_id,
            "model_tag": self.model_tag,
            "stage": self.stage.value,
        }


@dataclass(frozen=True, slots=True)
class ExecutionRequest:
    routing_decision: RoutingDecision
    messages: tuple[ExecutionMessage | Mapping[str, str], ...]
    keep_alive: int | str = -1
    request_id: str | None = None
    task_id: str | None = None
    response_format: Mapping[str, Any] | str | None = None
    temperature: float | None = None

    def __post_init__(self) -> None:
        normalized: list[ExecutionMessage] = []
        for message in self.messages:
            if isinstance(message, ExecutionMessage):
                normalized.append(message)
            elif isinstance(message, Mapping) and isinstance(message.get("role"), str) and isinstance(message.get("content"), str):
                normalized.append(ExecutionMessage(message["role"], message["content"]))
            else:
                raise ValueError("messages must contain text role/content mappings")
        object.__setattr__(self, "messages", tuple(normalized))
        if self.response_format is not None:
            if isinstance(self.response_format, Mapping):
                object.__setattr__(self, "response_format", MappingProxyType(dict(self.response_format)))
            elif self.response_format != "json":
                raise ValueError("response_format must be a JSON schema mapping, 'json', or None")
        if self.temperature is not None:
            if isinstance(self.temperature, bool) or not isinstance(self.temperature, (int, float)) or not math.isfinite(self.temperature):
                raise ValueError("temperature must be a finite number or None")
            if not 0.0 <= float(self.temperature) <= 2.0:
                raise ValueError("temperature must be between 0 and 2")
            object.__setattr__(self, "temperature", float(self.temperature))


@dataclass(frozen=True, slots=True)
class WorkerResult:
    outcome: ExecutionOutcome
    model_id: str | None
    model_tag: str | None
    content: str | None
    thinking: str | None
    tool_calls: tuple[Mapping[str, Any], ...]
    prompt_eval_count: int | None
    eval_count: int | None
    load_duration: int | None
    prompt_eval_duration: int | None
    eval_duration: int | None
    total_duration: int | None
    done_reason: str | None
    lifecycle_actions: tuple[LifecycleAction, ...]
    requested_context_length: int | None
    observed_context_length: int | None
    configured_num_gpu: int | None
    before_runtime_state: ResourceSnapshot | None
    after_runtime_state: ResourceSnapshot | None
    error: str | None = None

    def __post_init__(self) -> None:
        if any(not isinstance(item, Mapping) for item in self.tool_calls):
            raise ValueError("tool_calls must contain mappings")
        object.__setattr__(self, "tool_calls", tuple(MappingProxyType(dict(item)) for item in self.tool_calls))

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value, "model_id": self.model_id, "model_tag": self.model_tag,
            "content": self.content, "thinking": self.thinking, "tool_calls": [dict(item) for item in self.tool_calls],
            "prompt_eval_count": self.prompt_eval_count, "eval_count": self.eval_count,
            "load_duration": self.load_duration, "prompt_eval_duration": self.prompt_eval_duration,
            "eval_duration": self.eval_duration, "total_duration": self.total_duration,
            "done_reason": self.done_reason, "requested_context_length": self.requested_context_length, "observed_context_length": self.observed_context_length, "configured_num_gpu": self.configured_num_gpu, "lifecycle_actions": [item.to_dict() for item in self.lifecycle_actions],
            "before_runtime_state": self.before_runtime_state.to_dict() if self.before_runtime_state else None,
            "after_runtime_state": self.after_runtime_state.to_dict() if self.after_runtime_state else None,
            "error": self.error,
        }
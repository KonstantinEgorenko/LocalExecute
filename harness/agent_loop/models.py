'Immutable contracts for the worker agent coding-agent loop.'
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping

from ..workspace.models import JsonValue, ToolName


class AgentActionKind(StrEnum):
    TOOL = "TOOL"
    FINAL = "FINAL"


class AgentOutcome(StrEnum):
    SUCCESS = "SUCCESS"
    WAITING_EVIDENCE = "WAITING_EVIDENCE"
    NEEDS_ATTENTION = "NEEDS_ATTENTION"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    LOOP_DETECTED = "LOOP_DETECTED"
    ALREADY_FINALIZED = "ALREADY_FINALIZED"


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _non_negative_int(value: object, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{field_name} must be a non-boolean integer >= 0")
    return value


def _positive_int(value: object, field_name: str) -> int:
    value = _non_negative_int(value, field_name)
    if value == 0:
        raise ValueError(f"{field_name} must be > 0")
    return value


def _freeze_json(value: Any, field_name: str) -> JsonValue:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{field_name} must not contain non-finite floats")
        return value
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        result: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{field_name} mapping keys must be strings")
            result[key] = _freeze_json(item, f"{field_name}.{key}")
        return MappingProxyType(result)
    if isinstance(value, (tuple, list)):
        return tuple(_freeze_json(item, field_name) for item in value)
    raise ValueError(f"{field_name} must be JSON-safe")


def _plain(value: JsonValue) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class AgentToolCall:
    tool: ToolName
    arguments: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.tool, ToolName):
            raise ValueError("tool must be a ToolName")
        frozen = _freeze_json(self.arguments, "arguments")
        if not isinstance(frozen, Mapping):
            raise ValueError("arguments must be a mapping")
        object.__setattr__(self, "arguments", frozen)

    def fingerprint(self) -> str:
        return json.dumps(
            {"tool": self.tool.value, "arguments": _plain(self.arguments)},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )

    def to_dict(self) -> dict[str, Any]:
        return {"tool": self.tool.value, "arguments": _plain(self.arguments)}


@dataclass(frozen=True, slots=True)
class AgentFinal:
    summary: str

    def __post_init__(self) -> None:
        _required_text(self.summary, "summary")

    def to_dict(self) -> dict[str, str]:
        return {"summary": self.summary}


@dataclass(frozen=True, slots=True)
class AgentAction:
    schema_version: int
    action: AgentActionKind
    tool_call: AgentToolCall | None
    final: AgentFinal | None

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("schema_version must be exactly 1")
        if not isinstance(self.action, AgentActionKind):
            raise ValueError("action must be an AgentActionKind")
        if self.action is AgentActionKind.TOOL:
            if not isinstance(self.tool_call, AgentToolCall) or self.final is not None:
                raise ValueError("TOOL requires tool_call and final=null")
        elif self.action is AgentActionKind.FINAL:
            if self.tool_call is not None or not isinstance(self.final, AgentFinal):
                raise ValueError("FINAL requires tool_call=null and final")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "action": self.action.value,
            "tool_call": self.tool_call.to_dict() if self.tool_call else None,
            "final": self.final.to_dict() if self.final else None,
        }


@dataclass(frozen=True, slots=True)
class AgentLoopPolicy:
    max_tool_calls: int = 12
    max_same_tool_call: int = 2
    max_tool_failures: int = 3
    max_protocol_failures: int = 2
    max_message_bytes: int = 120 * 1024
    max_tool_feedback_bytes: int = 64 * 1024

    def __post_init__(self) -> None:
        for field_name in (
            "max_tool_calls",
            "max_same_tool_call",
            "max_tool_failures",
            "max_protocol_failures",
            "max_message_bytes",
            "max_tool_feedback_bytes",
        ):
            _positive_int(getattr(self, field_name), field_name)

    @classmethod
    def from_policies(cls, policies: Mapping[str, Any]) -> "AgentLoopPolicy":
        raw = policies.get("agent_loop", {}) if isinstance(policies, Mapping) else {}
        return cls(
            max_tool_calls=int(raw.get("max_tool_calls", 12)),
            max_same_tool_call=int(raw.get("max_same_tool_call", 2)),
            max_tool_failures=int(raw.get("max_tool_failures", 3)),
            max_protocol_failures=int(raw.get("max_protocol_failures", 2)),
            max_message_bytes=int(raw.get("max_message_bytes", 120 * 1024)),
            max_tool_feedback_bytes=int(raw.get("max_tool_feedback_bytes", 64 * 1024)),
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "max_tool_calls": self.max_tool_calls,
            "max_same_tool_call": self.max_same_tool_call,
            "max_tool_failures": self.max_tool_failures,
            "max_protocol_failures": self.max_protocol_failures,
            "max_message_bytes": self.max_message_bytes,
            "max_tool_feedback_bytes": self.max_tool_feedback_bytes,
        }


@dataclass(frozen=True, slots=True)
class AgentLoopResult:
    outcome: AgentOutcome
    run_id: str
    todo_id: str
    context_bundle_id: str
    final_summary: str | None
    model_turns: int
    tool_calls: int
    changed_paths: tuple[str, ...]
    last_model_id: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, AgentOutcome):
            raise ValueError("outcome must be an AgentOutcome")
        for field_name in ("run_id", "todo_id", "context_bundle_id"):
            _required_text(getattr(self, field_name), field_name)
        if self.final_summary is not None:
            _required_text(self.final_summary, "final_summary")
        _non_negative_int(self.model_turns, "model_turns")
        _non_negative_int(self.tool_calls, "tool_calls")
        if not isinstance(self.changed_paths, tuple) or any(
            not isinstance(item, str) or not item.strip() for item in self.changed_paths
        ):
            raise ValueError("changed_paths must be a tuple of non-empty strings")
        if len(set(self.changed_paths)) != len(self.changed_paths):
            raise ValueError("changed_paths must not contain duplicates")
        if self.last_model_id is not None:
            _required_text(self.last_model_id, "last_model_id")
        if self.reason is not None:
            _required_text(self.reason, "reason")

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "run_id": self.run_id,
            "todo_id": self.todo_id,
            "context_bundle_id": self.context_bundle_id,
            "final_summary": self.final_summary,
            "model_turns": self.model_turns,
            "tool_calls": self.tool_calls,
            "changed_paths": list(self.changed_paths),
            "last_model_id": self.last_model_id,
            "reason": self.reason,
        }

'Immutable model-facing contracts for profile tools Tool Protocol V2.'
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping

from harness.workspace.models import JsonValue, ToolStatus


class V2ActionKind(StrEnum):
    TOOL = "TOOL"
    FINAL = "FINAL"


class V2ToolName(StrEnum):
    LIST_FILES = "LIST_FILES"
    READ_TEXT = "READ_TEXT"
    SEARCH_TEXT = "SEARCH_TEXT"
    CREATE_TEXT = "CREATE_TEXT"
    WRITE_TEXT = "WRITE_TEXT"
    REPLACE_TEXT = "REPLACE_TEXT"
    DELETE_FILE = "DELETE_FILE"
    RUN_COMMAND = "RUN_COMMAND"


class V2GuardKind(StrEnum):
    LAST_READ = "LAST_READ"


class V2ToolFaultCode(StrEnum):
    NONE = "NONE"
    TOOL_NOT_AVAILABLE = "TOOL_NOT_AVAILABLE"
    LAST_READ_REQUIRED = "LAST_READ_REQUIRED"
    CREATE_TARGET_EXISTS = "CREATE_TARGET_EXISTS"
    WORKSPACE_POLICY_DENIED = "WORKSPACE_POLICY_DENIED"
    STALE_OR_CONFLICTING_REVISION = "STALE_OR_CONFLICTING_REVISION"
    INVALID_TOOL_REQUEST = "INVALID_TOOL_REQUEST"
    NOT_FOUND = "NOT_FOUND"
    TIMEOUT = "TIMEOUT"
    EXECUTION_ERROR = "EXECUTION_ERROR"


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _non_negative_int(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be integer >= 0")
    return value


def _freeze_json(value: Any, name: str) -> JsonValue:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{name} must not contain non-finite floats")
        return value
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        result: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{name} keys must be strings")
            result[key] = _freeze_json(item, f"{name}.{key}")
        return MappingProxyType(result)
    if isinstance(value, (tuple, list)):
        return tuple(_freeze_json(item, name) for item in value)
    raise ValueError(f"{name} must be JSON-safe")


def _plain(value: JsonValue) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


def _freeze_mapping(value: Mapping[str, Any], name: str) -> Mapping[str, JsonValue]:
    frozen = _freeze_json(value, name)
    if not isinstance(frozen, Mapping):
        raise ValueError(f"{name} must be mapping")
    return frozen


def _texts(values: object, name: str) -> tuple[str, ...]:
    if not isinstance(values, (tuple, list)):
        raise ValueError(f"{name} must be tuple/list")
    result = tuple(_required_text(value, name) for value in values)
    if len(result) != len(set(result)):
        raise ValueError(f"{name} must not contain duplicates")
    return result


@dataclass(frozen=True, slots=True)
class V2Guard:
    kind: V2GuardKind = V2GuardKind.LAST_READ

    def __post_init__(self) -> None:
        if not isinstance(self.kind, V2GuardKind):
            raise ValueError("kind must be V2GuardKind")

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind.value}


@dataclass(frozen=True, slots=True)
class V2ToolCall:
    tool: V2ToolName
    arguments: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.tool, V2ToolName):
            raise ValueError("tool must be V2ToolName")
        object.__setattr__(self, "arguments", _freeze_mapping(self.arguments, "arguments"))

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
class V2Final:
    summary: str

    def __post_init__(self) -> None:
        _required_text(self.summary, "summary")

    def to_dict(self) -> dict[str, str]:
        return {"summary": self.summary}


@dataclass(frozen=True, slots=True)
class V2AgentAction:
    schema_version: int
    action: V2ActionKind
    tool_call: V2ToolCall | None
    final: V2Final | None

    def __post_init__(self) -> None:
        if self.schema_version != 2:
            raise ValueError("schema_version must be exactly 2")
        if not isinstance(self.action, V2ActionKind):
            raise ValueError("action must be V2ActionKind")
        if self.action is V2ActionKind.TOOL:
            if not isinstance(self.tool_call, V2ToolCall) or self.final is not None:
                raise ValueError("TOOL requires tool_call and final=null")
        else:
            if self.tool_call is not None or not isinstance(self.final, V2Final):
                raise ValueError("FINAL requires tool_call=null and final")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "action": self.action.value,
            "tool_call": None if self.tool_call is None else self.tool_call.to_dict(),
            "final": None if self.final is None else self.final.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class V2ToolFeedback:
    call_id: str
    tool: V2ToolName
    underlying_tool: str | None
    status: ToolStatus
    fault_code: V2ToolFaultCode
    recoverable: bool
    output: str
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)
    changed_paths: tuple[str, ...] = ()
    required_next_action: str | None = None

    def __post_init__(self) -> None:
        _required_text(self.call_id, "call_id")
        if not isinstance(self.tool, V2ToolName):
            raise ValueError("tool must be V2ToolName")
        if self.underlying_tool is not None:
            _required_text(self.underlying_tool, "underlying_tool")
        if not isinstance(self.status, ToolStatus):
            raise ValueError("status must be ToolStatus")
        if not isinstance(self.fault_code, V2ToolFaultCode):
            raise ValueError("fault_code must be V2ToolFaultCode")
        if not isinstance(self.recoverable, bool):
            raise ValueError("recoverable must be bool")
        if not isinstance(self.output, str):
            raise ValueError("output must be string")
        object.__setattr__(self, "metadata", _freeze_mapping(self.metadata, "metadata"))
        object.__setattr__(self, "changed_paths", _texts(self.changed_paths, "changed_paths"))
        if self.required_next_action is not None:
            _required_text(self.required_next_action, "required_next_action")
        if self.status is ToolStatus.SUCCESS and self.fault_code is not V2ToolFaultCode.NONE:
            raise ValueError("successful feedback requires fault_code NONE")
        if self.status is not ToolStatus.SUCCESS and self.changed_paths:
            raise ValueError("failed feedback cannot report changed_paths")

    def to_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "tool": self.tool.value,
            "underlying_tool": self.underlying_tool,
            "status": self.status.value,
            "fault_code": self.fault_code.value,
            "recoverable": self.recoverable,
            "output": self.output,
            "metadata": _plain(self.metadata),
            "changed_paths": list(self.changed_paths),
            "required_next_action": self.required_next_action,
        }

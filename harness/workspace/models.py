'Immutable workspace workspace and tool-dispatch contracts.'
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping, TypeAlias

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | tuple["JsonValue", ...] | Mapping[str, "JsonValue"]


class ToolName(StrEnum):
    LIST_FILES = "LIST_FILES"
    READ_TEXT = "READ_TEXT"
    SEARCH_TEXT = "SEARCH_TEXT"
    WRITE_TEXT = "WRITE_TEXT"
    REPLACE_TEXT = "REPLACE_TEXT"
    DELETE_FILE = "DELETE_FILE"
    RUN_COMMAND = "RUN_COMMAND"


class ToolStatus(StrEnum):
    SUCCESS = "SUCCESS"
    DENIED = "DENIED"
    INVALID = "INVALID"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    TIMEOUT = "TIMEOUT"
    ERROR = "ERROR"


class WorkspaceAccess(StrEnum):
    READ_ONLY = "READ_ONLY"
    READ_WRITE = "READ_WRITE"


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
        frozen: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{field_name} mapping keys must be strings")
            frozen[key] = _freeze_json(item, f"{field_name}.{key}")
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item, field_name) for item in value)
    raise ValueError(f"{field_name} must be JSON-safe")


def _freeze_mapping(value: Mapping[str, Any] | None, field_name: str) -> Mapping[str, JsonValue]:
    if value is None:
        return MappingProxyType({})
    frozen = _freeze_json(value, field_name)
    if not isinstance(frozen, Mapping):
        raise ValueError(f"{field_name} must be a mapping")
    return frozen


def _plain(value: JsonValue) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


def _texts(values: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(values, (tuple, list)):
        raise ValueError(f"{field_name} must be a tuple/list of strings")
    normalized = tuple(_required_text(value, field_name) for value in values)
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{field_name} must not contain duplicates")
    return normalized


def _command_prefixes(values: object) -> tuple[tuple[str, ...], ...]:
    if not isinstance(values, (tuple, list)):
        raise ValueError("command_prefixes must be a tuple/list")
    result: list[tuple[str, ...]] = []
    for index, prefix in enumerate(values):
        if not isinstance(prefix, (tuple, list)) or not prefix:
            raise ValueError(f"command_prefixes[{index}] must be a non-empty tuple/list")
        normalized = tuple(_required_text(value, f"command_prefixes[{index}]") for value in prefix)
        result.append(normalized)
    if len(set(result)) != len(result):
        raise ValueError("command_prefixes must not contain duplicates")
    return tuple(result)


@dataclass(frozen=True, slots=True)
class WorkspacePolicy:
    access: WorkspaceAccess = WorkspaceAccess.READ_WRITE
    allowed_paths: tuple[str, ...] = ()
    forbidden_paths: tuple[str, ...] = (".git",)
    protected_paths: tuple[str, ...] = ("state", "scripts/swarm_monitor.ps1")
    command_prefixes: tuple[tuple[str, ...], ...] = ()
    max_read_bytes: int = 512 * 1024
    max_write_bytes: int = 2 * 1024 * 1024
    max_list_entries: int = 2000
    max_search_files: int = 1000
    max_search_matches: int = 500
    max_command_seconds: int = 120
    max_command_output_bytes: int = 512 * 1024

    def __post_init__(self) -> None:
        if not isinstance(self.access, WorkspaceAccess):
            raise ValueError("access must be a WorkspaceAccess")
        object.__setattr__(self, "allowed_paths", _texts(self.allowed_paths, "allowed_paths"))
        object.__setattr__(self, "forbidden_paths", _texts(self.forbidden_paths, "forbidden_paths"))
        object.__setattr__(self, "protected_paths", _texts(self.protected_paths, "protected_paths"))
        object.__setattr__(self, "command_prefixes", _command_prefixes(self.command_prefixes))
        for field_name in (
            "max_read_bytes", "max_write_bytes", "max_list_entries", "max_search_files",
            "max_search_matches", "max_command_seconds", "max_command_output_bytes",
        ):
            _positive_int(getattr(self, field_name), field_name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "access": self.access.value,
            "allowed_paths": list(self.allowed_paths),
            "forbidden_paths": list(self.forbidden_paths),
            "protected_paths": list(self.protected_paths),
            "command_prefixes": [list(value) for value in self.command_prefixes],
            "max_read_bytes": self.max_read_bytes,
            "max_write_bytes": self.max_write_bytes,
            "max_list_entries": self.max_list_entries,
            "max_search_files": self.max_search_files,
            "max_search_matches": self.max_search_matches,
            "max_command_seconds": self.max_command_seconds,
            "max_command_output_bytes": self.max_command_output_bytes,
        }


@dataclass(frozen=True, slots=True)
class WorkspaceSession:
    session_id: str
    run_id: str
    todo_id: str
    root: str
    policy: WorkspacePolicy

    def __post_init__(self) -> None:
        for field_name in ("session_id", "run_id", "todo_id", "root"):
            _required_text(getattr(self, field_name), field_name)
        if not isinstance(self.policy, WorkspacePolicy):
            raise ValueError("policy must be a WorkspacePolicy")

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "run_id": self.run_id,
            "todo_id": self.todo_id,
            "root": self.root,
            "policy": self.policy.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class ToolRequest:
    call_id: str
    run_id: str
    todo_id: str
    tool: ToolName
    arguments: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name in ("call_id", "run_id", "todo_id"):
            _required_text(getattr(self, field_name), field_name)
        if not isinstance(self.tool, ToolName):
            raise ValueError("tool must be a ToolName")
        object.__setattr__(self, "arguments", _freeze_mapping(self.arguments, "arguments"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "run_id": self.run_id,
            "todo_id": self.todo_id,
            "tool": self.tool.value,
            "arguments": _plain(self.arguments),
        }


@dataclass(frozen=True, slots=True)
class ToolResult:
    call_id: str
    run_id: str
    todo_id: str
    tool: ToolName
    status: ToolStatus
    output: str = ""
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)
    changed_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field_name in ("call_id", "run_id", "todo_id"):
            _required_text(getattr(self, field_name), field_name)
        if not isinstance(self.tool, ToolName):
            raise ValueError("tool must be a ToolName")
        if not isinstance(self.status, ToolStatus):
            raise ValueError("status must be a ToolStatus")
        if not isinstance(self.output, str):
            raise ValueError("output must be a string")
        object.__setattr__(self, "metadata", _freeze_mapping(self.metadata, "metadata"))
        object.__setattr__(self, "changed_paths", _texts(self.changed_paths, "changed_paths"))
        if self.status is not ToolStatus.SUCCESS and self.changed_paths:
            raise ValueError("non-successful tool results must not report changed_paths")

    def to_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "run_id": self.run_id,
            "todo_id": self.todo_id,
            "tool": self.tool.value,
            "status": self.status.value,
            "output": self.output,
            "metadata": _plain(self.metadata),
            "changed_paths": list(self.changed_paths),
        }

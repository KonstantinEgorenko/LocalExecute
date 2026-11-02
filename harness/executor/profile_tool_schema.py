"""Tool-specific structured-output schema for Tool Protocol V2."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from harness.executor.profile_tool_capabilities import V2ToolCapabilities
from harness.executor.profile_tool_models import V2ToolName


_PATH = {"type": "string", "minLength": 1}
_OPTIONAL_PATH = {"type": "string"}
_GUARD = {
    "type": "object",
    "additionalProperties": False,
    "required": ["kind"],
    "properties": {"kind": {"type": "string", "enum": ["LAST_READ"]}},
}


def _object_schema(
    properties: dict[str, Any],
    required: tuple[str, ...],
) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(required),
        "properties": properties,
    }


_ARGUMENT_SCHEMAS: dict[V2ToolName, dict[str, Any]] = {
    V2ToolName.LIST_FILES: _object_schema(
        {
            "path": deepcopy(_OPTIONAL_PATH),
            "recursive": {"type": "boolean"},
        },
        ("path",),
    ),
    V2ToolName.READ_TEXT: _object_schema(
        {"path": deepcopy(_PATH)},
        ("path",),
    ),
    V2ToolName.SEARCH_TEXT: _object_schema(
        {
            "query": {"type": "string", "minLength": 1},
            "path": deepcopy(_OPTIONAL_PATH),
            "case_sensitive": {"type": "boolean"},
        },
        ("query",),
    ),
    V2ToolName.CREATE_TEXT: _object_schema(
        {
            "path": deepcopy(_PATH),
            "content": {"type": "string"},
        },
        ("path", "content"),
    ),
    V2ToolName.WRITE_TEXT: _object_schema(
        {
            "path": deepcopy(_PATH),
            "content": {"type": "string"},
            "guard": deepcopy(_GUARD),
        },
        ("path", "content", "guard"),
    ),
    V2ToolName.REPLACE_TEXT: _object_schema(
        {
            "path": deepcopy(_PATH),
            "old_text": {"type": "string", "minLength": 1},
            "new_text": {"type": "string"},
            "guard": deepcopy(_GUARD),
            "expected_occurrences": {"type": "integer", "minimum": 1},
        },
        ("path", "old_text", "new_text", "guard"),
    ),
    V2ToolName.DELETE_FILE: _object_schema(
        {
            "path": deepcopy(_PATH),
            "guard": deepcopy(_GUARD),
        },
        ("path", "guard"),
    ),
    V2ToolName.RUN_COMMAND: _object_schema(
        {
            "argv": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string", "minLength": 1},
            },
            "cwd": deepcopy(_OPTIONAL_PATH),
            "timeout_seconds": {"type": "integer", "minimum": 1},
        },
        ("argv",),
    ),
}


def tool_arguments_json_schema_v2(tool: V2ToolName) -> dict[str, Any]:
    if not isinstance(tool, V2ToolName):
        raise ValueError("tool must be V2ToolName")
    return deepcopy(_ARGUMENT_SCHEMAS[tool])


def agent_action_json_schema_v2(
    capabilities: V2ToolCapabilities,
) -> dict[str, Any]:
    if not isinstance(capabilities, V2ToolCapabilities):
        raise ValueError("capabilities must be V2ToolCapabilities")

    tool_branches = []
    for tool in capabilities.tools:
        tool_branches.append(
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["tool", "arguments"],
                "properties": {
                    "tool": {"type": "string", "enum": [tool.value]},
                    "arguments": tool_arguments_json_schema_v2(tool),
                },
            }
        )

    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["schema_version", "action", "tool_call", "final"],
        "properties": {
            "schema_version": {"type": "integer", "enum": [2]},
            "action": {"type": "string", "enum": ["TOOL", "FINAL"]},
            "tool_call": {
                "anyOf": [
                    {"type": "null"},
                    *tool_branches,
                ]
            },
            "final": {
                "anyOf": [
                    {"type": "null"},
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["summary"],
                        "properties": {
                            "summary": {"type": "string", "minLength": 1}
                        },
                    },
                ]
            },
        },
    }

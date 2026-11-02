'Strict canonical JSON protocol for worker agent worker actions.'
from __future__ import annotations

import json
from typing import Any

from ..workspace.models import ToolName
from .errors import AgentProtocolError
from .models import AgentAction, AgentActionKind, AgentFinal, AgentToolCall

_TOP_KEYS = {"schema_version", "action", "tool_call", "final"}
_TOOL_KEYS = {"tool", "arguments"}
_FINAL_KEYS = {"summary"}


def decode_strict_json(raw: str) -> Any:
    """Decode JSON without duplicate keys or non-standard numeric constants."""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result
    def constant(value):
        raise ValueError(f"non-finite JSON literal: {value}")
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def _object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AgentProtocolError(f"{name} must be a JSON object")
    return value


def parse_agent_action(raw: str) -> AgentAction:
    if not isinstance(raw, str) or not raw.strip():
        raise AgentProtocolError("worker response must be a non-empty JSON string")
    stripped = raw.strip()
    if stripped.startswith("```") or stripped.endswith("```"):
        raise AgentProtocolError("Markdown fences are forbidden")
    try:
        value = decode_strict_json(stripped)
    except ValueError as exc:
        raise AgentProtocolError(f"worker response is not valid JSON: {exc}") from exc
    payload = _object(value, "response")
    if set(payload) != _TOP_KEYS:
        raise AgentProtocolError("response keys must match canonical schema exactly")
    if type(payload.get("schema_version")) is not int or payload["schema_version"] != 1:
        raise AgentProtocolError("schema_version must be an integer exactly 1")
    try:
        action = AgentActionKind(payload.get("action"))
    except (TypeError, ValueError) as exc:
        raise AgentProtocolError("action must be TOOL or FINAL") from exc

    if action is AgentActionKind.TOOL:
        if payload.get("final") is not None:
            raise AgentProtocolError("TOOL requires final=null")
        call = _object(payload.get("tool_call"), "tool_call")
        if set(call) != _TOOL_KEYS:
            raise AgentProtocolError("tool_call keys must match schema exactly")
        try:
            tool = ToolName(call.get("tool"))
        except (TypeError, ValueError) as exc:
            raise AgentProtocolError("tool_call.tool is not a supported ToolName") from exc
        arguments = call.get("arguments")
        if not isinstance(arguments, dict):
            raise AgentProtocolError("tool_call.arguments must be a JSON object")
        try:
            return AgentAction(1, action, AgentToolCall(tool, arguments), None)
        except ValueError as exc:
            raise AgentProtocolError(str(exc)) from exc

    if payload.get("tool_call") is not None:
        raise AgentProtocolError("FINAL requires tool_call=null")
    final = _object(payload.get("final"), "final")
    if set(final) != _FINAL_KEYS:
        raise AgentProtocolError("final keys must match schema exactly")
    try:
        return AgentAction(1, action, None, AgentFinal(final.get("summary")))
    except ValueError as exc:
        raise AgentProtocolError(str(exc)) from exc



def agent_action_json_schema(
    *,
    allow_file_delete: bool = False,
    allowed_tools: frozenset[ToolName] | None = None,
    allow_final: bool = True,
) -> dict[str, Any]:
    """Return the strict structured-output schema for one agent action.

    ``allowed_tools`` narrows the model-visible TOOL action space without
    changing parser authority or dispatcher policy. ``allow_final=False`` is
    used while a mutation-required contract still has no confirmed mutation,
    because FINAL cannot be accepted in that state.
    """
    if not isinstance(allow_file_delete, bool):
        raise ValueError("allow_file_delete must be a bool")
    if not isinstance(allow_final, bool):
        raise ValueError("allow_final must be a bool")
    if allowed_tools is not None:
        if not isinstance(allowed_tools, frozenset):
            raise ValueError("allowed_tools must be a frozenset[ToolName] or None")
        if not allowed_tools:
            raise ValueError("allowed_tools must not be empty")
        if any(not isinstance(item, ToolName) for item in allowed_tools):
            raise ValueError("allowed_tools must contain only ToolName values")

    text = {"type": "string"}
    non_empty_text = {"type": "string", "minLength": 1}
    sha256 = {"type": "string", "pattern": "^[0-9a-fA-F]{64}$"}

    def arguments(
        properties: dict[str, Any],
        *,
        required: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "required": list(required),
            "properties": properties,
        }

    tool_variants = [
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["tool", "arguments"],
            "properties": {
                "tool": {"type": "string", "enum": [ToolName.LIST_FILES.value]},
                "arguments": arguments(
                    {"path": text, "recursive": {"type": "boolean"}}
                ),
            },
        },
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["tool", "arguments"],
            "properties": {
                "tool": {"type": "string", "enum": [ToolName.READ_TEXT.value]},
                "arguments": arguments({"path": non_empty_text}, required=("path",)),
            },
        },
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["tool", "arguments"],
            "properties": {
                "tool": {"type": "string", "enum": [ToolName.SEARCH_TEXT.value]},
                "arguments": arguments(
                    {
                        "query": non_empty_text,
                        "path": text,
                        "case_sensitive": {"type": "boolean"},
                    },
                    required=("query",),
                ),
            },
        },
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["tool", "arguments"],
            "properties": {
                "tool": {"type": "string", "enum": [ToolName.WRITE_TEXT.value]},
                "arguments": arguments(
                    {
                        "path": non_empty_text,
                        "content": text,
                        "expected_sha256": sha256,
                    },
                    required=("path", "content"),
                ),
            },
        },
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["tool", "arguments"],
            "properties": {
                "tool": {"type": "string", "enum": [ToolName.REPLACE_TEXT.value]},
                "arguments": arguments(
                    {
                        "path": non_empty_text,
                        "old_text": non_empty_text,
                        "new_text": text,
                        "expected_sha256": sha256,
                        "expected_occurrences": {"type": "integer", "minimum": 1},
                    },
                    required=("path", "old_text", "new_text"),
                ),
            },
        },
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["tool", "arguments"],
            "properties": {
                "tool": {"type": "string", "enum": [ToolName.DELETE_FILE.value]},
                "arguments": arguments(
                    {"path": non_empty_text, "expected_sha256": sha256},
                    required=("path",),
                ),
            },
        },
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["tool", "arguments"],
            "properties": {
                "tool": {"type": "string", "enum": [ToolName.RUN_COMMAND.value]},
                "arguments": arguments(
                    {
                        "argv": {
                            "type": "array",
                            "minItems": 1,
                            "items": non_empty_text,
                        },
                        "cwd": text,
                        "timeout_seconds": {"type": "integer", "minimum": 1},
                    },
                    required=("argv",),
                ),
            },
        },
    ]

    visible_tools = set(ToolName) if allowed_tools is None else set(allowed_tools)
    if not allow_file_delete:
        visible_tools.discard(ToolName.DELETE_FILE)

    tool_variants = [
        variant
        for variant in tool_variants
        if ToolName(variant["properties"]["tool"]["enum"][0]) in visible_tools
    ]
    if not tool_variants:
        raise ValueError("schema would expose no TOOL variants")

    common = {"schema_version": {"type": "integer", "enum": [1]}}
    variants: list[dict[str, Any]] = [
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["schema_version", "action", "tool_call", "final"],
            "properties": {
                **common,
                "action": {"type": "string", "enum": ["TOOL"]},
                "tool_call": {"oneOf": tool_variants},
                "final": {"type": "null"},
            },
        }
    ]

    if allow_final:
        variants.append(
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["schema_version", "action", "tool_call", "final"],
                "properties": {
                    **common,
                    "action": {"type": "string", "enum": ["FINAL"]},
                    "tool_call": {"type": "null"},
                    "final": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["summary"],
                        "properties": {
                            "summary": {"type": "string", "minLength": 1},
                        },
                    },
                },
            }
        )

    return {"oneOf": variants}

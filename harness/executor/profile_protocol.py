'Strict parser for profile tools Tool Protocol V2.'
from __future__ import annotations

import json
from typing import Any, Mapping

from harness.agent_loop.errors import AgentProtocolError
from harness.executor.profile_tool_capabilities import V2ToolCapabilities
from harness.executor.profile_tool_models import V2ActionKind, V2AgentAction, V2Final, V2GuardKind, V2ToolCall, V2ToolName

_TOP_KEYS = {"schema_version", "action", "tool_call", "final"}
_TOOL_KEYS = {"tool", "arguments"}
_FINAL_KEYS = {"summary"}


def _object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AgentProtocolError(f"{name} must be a JSON object")
    return value


def _exact_keys(
    value: Mapping[str, Any],
    *,
    required: set[str],
    optional: set[str] = frozenset(),
    name: str,
) -> None:
    keys = set(value)
    allowed = required | optional
    missing = required - keys
    extra = keys - allowed
    if missing:
        raise AgentProtocolError(
            f"{name} missing required keys: {sorted(missing)}"
        )
    if extra:
        raise AgentProtocolError(f"{name} has unsupported keys: {sorted(extra)}")


def _text(value: object, name: str, *, non_empty: bool = True) -> str:
    if not isinstance(value, str) or (non_empty and not value.strip()):
        requirement = "non-empty string" if non_empty else "string"
        raise AgentProtocolError(f"{name} must be {requirement}")
    return value


def _bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise AgentProtocolError(f"{name} must be bool")
    return value


def _positive_int(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise AgentProtocolError(f"{name} must be integer > 0")
    return value


def _guard(value: object) -> dict[str, str]:
    item = _object(value, "guard")
    if set(item) != {"kind"} or item.get("kind") != V2GuardKind.LAST_READ.value:
        raise AgentProtocolError('guard must be exactly {"kind":"LAST_READ"}')
    return {"kind": V2GuardKind.LAST_READ.value}


def _validate_arguments(tool: V2ToolName, raw: object) -> dict[str, Any]:
    args = _object(raw, "tool_call.arguments")

    if tool is V2ToolName.LIST_FILES:
        _exact_keys(args, required={"path"}, optional={"recursive"}, name="LIST_FILES arguments")
        return {
            "path": _text(args["path"], "path", non_empty=False),
            "recursive": _bool(args.get("recursive", True), "recursive"),
        }

    if tool is V2ToolName.READ_TEXT:
        _exact_keys(args, required={"path"}, name="READ_TEXT arguments")
        return {"path": _text(args["path"], "path")}

    if tool is V2ToolName.SEARCH_TEXT:
        _exact_keys(
            args,
            required={"query"},
            optional={"path", "case_sensitive"},
            name="SEARCH_TEXT arguments",
        )
        return {
            "query": _text(args["query"], "query"),
            "path": _text(args.get("path", ""), "path", non_empty=False),
            "case_sensitive": _bool(
                args.get("case_sensitive", True), "case_sensitive"
            ),
        }

    if tool is V2ToolName.CREATE_TEXT:
        _exact_keys(
            args,
            required={"path", "content"},
            name="CREATE_TEXT arguments",
        )
        return {
            "path": _text(args["path"], "path"),
            "content": _text(args["content"], "content", non_empty=False),
        }

    if tool is V2ToolName.WRITE_TEXT:
        _exact_keys(
            args,
            required={"path", "content", "guard"},
            name="WRITE_TEXT arguments",
        )
        return {
            "path": _text(args["path"], "path"),
            "content": _text(args["content"], "content", non_empty=False),
            "guard": _guard(args["guard"]),
        }

    if tool is V2ToolName.REPLACE_TEXT:
        _exact_keys(
            args,
            required={"path", "old_text", "new_text", "guard"},
            optional={"expected_occurrences"},
            name="REPLACE_TEXT arguments",
        )
        return {
            "path": _text(args["path"], "path"),
            "old_text": _text(args["old_text"], "old_text"),
            "new_text": _text(args["new_text"], "new_text", non_empty=False),
            "guard": _guard(args["guard"]),
            "expected_occurrences": _positive_int(
                args.get("expected_occurrences", 1),
                "expected_occurrences",
            ),
        }

    if tool is V2ToolName.DELETE_FILE:
        _exact_keys(
            args,
            required={"path", "guard"},
            name="DELETE_FILE arguments",
        )
        return {
            "path": _text(args["path"], "path"),
            "guard": _guard(args["guard"]),
        }

    if tool is V2ToolName.RUN_COMMAND:
        _exact_keys(
            args,
            required={"argv"},
            optional={"cwd", "timeout_seconds"},
            name="RUN_COMMAND arguments",
        )
        argv = args["argv"]
        if not isinstance(argv, list) or not argv:
            raise AgentProtocolError("argv must be non-empty JSON array")
        normalized_argv = [_text(item, "argv item") for item in argv]
        result: dict[str, Any] = {
            "argv": normalized_argv,
            "cwd": _text(args.get("cwd", ""), "cwd", non_empty=False),
        }
        if "timeout_seconds" in args:
            result["timeout_seconds"] = _positive_int(
                args["timeout_seconds"], "timeout_seconds"
            )
        return result

    raise AgentProtocolError(f"unsupported V2 tool: {tool.value}")


def parse_agent_action_v2(
    raw: str,
    capabilities: V2ToolCapabilities,
) -> V2AgentAction:
    if not isinstance(capabilities, V2ToolCapabilities):
        raise ValueError("capabilities must be V2ToolCapabilities")
    if not isinstance(raw, str) or not raw.strip():
        raise AgentProtocolError("worker response must be a non-empty JSON string")
    stripped = raw.strip()
    if stripped.startswith("```") or stripped.endswith("```"):
        raise AgentProtocolError("Markdown fences are forbidden")

    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise AgentProtocolError(
            f"worker response is not valid JSON: {exc}"
        ) from exc
    payload = _object(payload, "response")
    if set(payload) != _TOP_KEYS:
        raise AgentProtocolError("response keys must match V2 schema exactly")
    if payload.get("schema_version") != 2:
        raise AgentProtocolError("schema_version must be exactly 2")

    try:
        action = V2ActionKind(payload.get("action"))
    except (TypeError, ValueError) as exc:
        raise AgentProtocolError("action must be TOOL or FINAL") from exc

    if action is V2ActionKind.FINAL:
        if payload.get("tool_call") is not None:
            raise AgentProtocolError("FINAL requires tool_call=null")
        final = _object(payload.get("final"), "final")
        if set(final) != _FINAL_KEYS:
            raise AgentProtocolError("final keys must match schema exactly")
        try:
            return V2AgentAction(
                2,
                action,
                None,
                V2Final(_text(final.get("summary"), "summary")),
            )
        except ValueError as exc:
            raise AgentProtocolError(str(exc)) from exc

    if payload.get("final") is not None:
        raise AgentProtocolError("TOOL requires final=null")
    tool_call = _object(payload.get("tool_call"), "tool_call")
    if set(tool_call) != _TOOL_KEYS:
        raise AgentProtocolError("tool_call keys must match schema exactly")
    try:
        tool = V2ToolName(tool_call.get("tool"))
    except (TypeError, ValueError) as exc:
        raise AgentProtocolError("tool_call.tool is not a V2 tool") from exc
    if not capabilities.contains(tool):
        raise AgentProtocolError(
            f"tool {tool.value} is not available in this workspace session"
        )

    normalized = _validate_arguments(tool, tool_call.get("arguments"))
    try:
        return V2AgentAction(
            2,
            action,
            V2ToolCall(tool, normalized),
            None,
        )
    except ValueError as exc:
        raise AgentProtocolError(str(exc)) from exc

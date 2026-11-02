"""Strict structured-output protocol for SimpleWorkerV2."""

from __future__ import annotations

from typing import Any
from io import StringIO
import tokenize

from harness.agent_loop.protocol import decode_strict_json

from harness.workspace_aci_v2 import AciActionKindV2, AciActionV2

from .models import SimpleWorkerActionKindV2, SimpleWorkerActionV2


class SimpleWorkerProtocolError(ValueError):
    def __init__(self, message: str, *, error_code: str | None = None,
                 path: str | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.path = path


_TOP_KEYS = {"schema_version", "action", "arguments", "summary"}

_ARG_KEYS = {
    SimpleWorkerActionKindV2.VIEW: {"path"},
    SimpleWorkerActionKindV2.SEARCH: {"query", "path", "case_sensitive"},
    SimpleWorkerActionKindV2.EDIT: {
        "match",
        "new_text",
    },
    SimpleWorkerActionKindV2.RUN: {"argv", "cwd", "timeout_seconds"},
    SimpleWorkerActionKindV2.FINISH: set(),
}


def _object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SimpleWorkerProtocolError(f"{name} must be a JSON object")
    return value


def parse_simple_worker_action(raw: str, *, viewed_files: dict[str, str] | None = None) -> SimpleWorkerActionV2:
    if not isinstance(raw, str) or not raw.strip():
        raise SimpleWorkerProtocolError(
            "worker response must be a non-empty JSON string"
        )
    stripped = raw.strip()
    if stripped.startswith("```") or stripped.endswith("```"):
        raise SimpleWorkerProtocolError("Markdown fences are forbidden")
    try:
        value = decode_strict_json(stripped)
    except ValueError as exc:
        raise SimpleWorkerProtocolError(
            f"worker response is not valid JSON: {exc}"
        ) from exc

    payload = _object(value, "response")
    if set(payload) != _TOP_KEYS:
        raise SimpleWorkerProtocolError(
            "response keys must match canonical schema exactly"
        )
    if type(payload.get("schema_version")) is not int or payload["schema_version"] != 1:
        raise SimpleWorkerProtocolError("schema_version must be exactly 1")
    try:
        action = SimpleWorkerActionKindV2(payload.get("action"))
    except (TypeError, ValueError) as exc:
        raise SimpleWorkerProtocolError(
            "action must be VIEW, SEARCH, EDIT, RUN, or FINISH"
        ) from exc

    arguments = _object(payload.get("arguments"), "arguments")
    compact_range = action is SimpleWorkerActionKindV2.EDIT and set(arguments) == {"file", "lines", "replacement"}
    compact_create = action is SimpleWorkerActionKindV2.EDIT and set(arguments) == {"file", "replacement"}
    compact_fragment = action is SimpleWorkerActionKindV2.EDIT and set(arguments) == {"file", "old_text", "replacement"}
    range_edit = action is SimpleWorkerActionKindV2.EDIT and set(arguments) == {"match", "replacement_lines"}
    if set(arguments) != _ARG_KEYS[action] and not (range_edit or compact_range or compact_create or compact_fragment):
        raise SimpleWorkerProtocolError(
            f"{action.value} arguments must match canonical schema exactly"
        )

    summary = payload.get("summary")
    if action is SimpleWorkerActionKindV2.FINISH:
        if not isinstance(summary, str) or not summary.strip():
            raise SimpleWorkerProtocolError(
                "FINISH requires a non-empty summary"
            )
        return SimpleWorkerActionV2(1, action, None, summary)

    if summary is not None:
        raise SimpleWorkerProtocolError(
            f"{action.value} requires summary=null"
        )

    try:
        if action is SimpleWorkerActionKindV2.VIEW:
            aci = AciActionV2(
                AciActionKindV2.VIEW,
                path=arguments["path"],
            )
        elif action is SimpleWorkerActionKindV2.SEARCH:
            aci = AciActionV2(
                AciActionKindV2.SEARCH,
                path=arguments["path"],
                query=arguments["query"],
                case_sensitive=arguments["case_sensitive"],
            )
        elif action is SimpleWorkerActionKindV2.EDIT:
            if compact_range or compact_create or compact_fragment:
                # A declared compact wire shape, not response repair. Copy the
                # literal replacement unchanged into the existing ACI boundary.
                if not isinstance(arguments["file"], str) or not isinstance(arguments["replacement"], str):
                    raise ValueError("EDIT file and replacement must be exact strings")
                if compact_fragment:
                    if not isinstance(arguments["old_text"], str) or not arguments["old_text"]:
                        raise ValueError("EDIT old_text must be a non-empty exact string")
                    aci = AciActionV2(AciActionKindV2.EDIT, path=arguments["file"],
                        old_text=arguments["old_text"], new_text=arguments["replacement"],
                        expected_occurrences=1)
                    return SimpleWorkerActionV2(1, action, aci, None)
                if compact_create:
                    aci = AciActionV2(AciActionKindV2.EDIT, path=arguments["file"],
                        old_text=None, new_text=arguments["replacement"], expected_occurrences=1)
                    return SimpleWorkerActionV2(1, action, aci, None)
                bounds = _object(arguments["lines"], "EDIT lines")
                if set(bounds) != {"first", "last"}:
                    raise ValueError("EDIT lines requires first and last exactly")
                arguments = {"match": {"kind": "RANGE", "file": {"path": arguments["file"]},
                    "start_line": bounds["first"], "end_line": bounds["last"]},
                    "new_text": arguments["replacement"]}
            match = _object(arguments["match"], "EDIT match")
            if range_edit or compact_range:
                if set(match) != {"kind", "file", "start_line", "end_line"} or match["kind"] != "RANGE":
                    raise ValueError("RANGE match requires kind, file, start_line and end_line exactly")
                target = _object(match["file"], "RANGE file")
                if set(target) != {"path"} or not isinstance(target["path"], str):
                    raise ValueError("RANGE file requires exact string path")
                source = (viewed_files or {}).get(target["path"])
                if source is None:
                    raise ValueError("RANGE requires a current bounded VIEW of the complete file")
                lines = source.splitlines(keepends=True)
                start, end = match["start_line"], match["end_line"]
                if type(start) is not int or type(end) is not int or not 1 <= start <= end <= len(lines):
                    raise ValueError("RANGE requires valid inclusive one-based integer line bounds")
                replacement = arguments.get("replacement_lines")
                if not compact_range:
                    if (not isinstance(replacement, list) or any(not isinstance(line, str)
                            or "\n" in line or "\r" in line for line in replacement)):
                        raise SimpleWorkerProtocolError(
                            "replacement_lines must be a JSON array of source lines without embedded newlines",
                            error_code="INVALID_SOURCE_LINE_ARRAY", path=target["path"])
                # Line separators are part of the declared range-edit contract;
                # source strings are never unescaped, stripped or repaired.
                new = arguments["new_text"] if compact_range else "\n".join(replacement)
                if new and not new.endswith("\n") and end > 0 and lines[end - 1].endswith("\n"):
                    new += "\n"
                updated = "".join(lines[:start - 1]) + new + "".join(lines[end:])
                aci = AciActionV2(AciActionKindV2.EDIT, path=target["path"],
                                 old_text=source, new_text=updated, expected_occurrences=1,
                                 expected_observed_content=source)
                return SimpleWorkerActionV2(1, action, aci, None)
            if set(match) != {"kind", "file", "old_text", "expected_occurrences"}:
                raise ValueError("EDIT match keys must match canonical schema exactly")
            target = _object(match["file"], "EDIT match file")
            if set(target) != {"path"}:
                raise ValueError("EDIT match file requires exactly path")
            kind = match["kind"]
            if kind not in ("CREATE", "LINE", "FILE", "FRAGMENT"):
                raise ValueError("EDIT match kind must be CREATE, LINE, FILE or FRAGMENT")
            if (kind == "CREATE") != (match["old_text"] is None):
                raise ValueError("only CREATE permits old_text=null")
            if kind == "LINE" and (not isinstance(arguments["new_text"], str)
                    or "\n" in arguments["new_text"] or "\r" in arguments["new_text"]):
                raise ValueError("LINE replacement requires one source line")
            aci = AciActionV2(
                AciActionKindV2.EDIT,
                path=target["path"],
                old_text=match["old_text"],
                new_text=arguments["new_text"],
                expected_occurrences=match["expected_occurrences"],
                expected_observed_content=((viewed_files or {}).get(target["path"])
                                           if kind == "FILE" else None),
            )
        else:
            argv = arguments["argv"]
            if not isinstance(argv, list):
                raise ValueError("RUN argv must be a JSON array")
            aci = AciActionV2(
                AciActionKindV2.RUN,
                argv=tuple(argv),
                cwd=arguments["cwd"],
                timeout_seconds=arguments["timeout_seconds"],
            )
    except SimpleWorkerProtocolError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise SimpleWorkerProtocolError(str(exc)) from exc

    return SimpleWorkerActionV2(1, action, aci, None)


def observed_edit_fragments(content: str) -> tuple[str, ...]:
    """Bounded exact lexical source units, including syntactically broken files.

    This inventories text already observed; it never evaluates code or infers
    the desired mutation. Tokenization can fail after yielding useful units.
    """
    lines = content.splitlines(keepends=True)
    starts = {0, len(lines)}
    try:
        for token in tokenize.generate_tokens(StringIO(content).readline):
            if token.start[1] == 0 and token.type in (tokenize.NAME, tokenize.STRING):
                starts.add(token.start[0] - 1)
    except (tokenize.TokenError, SyntaxError):
        pass
    ordered = sorted(starts)
    result = []
    used = 0
    for begin, end in zip(ordered, ordered[1:]):
        fragment = "".join(lines[begin:end]).rstrip("\r\n")
        if not fragment.strip():
            continue
        before, _, after = content.partition(fragment)
        if not (before + after).strip():
            continue  # Whole-file replacement remains explicitly FILE.
        for value in dict.fromkeys((fragment, fragment + "\n")):
            size = len(value.encode("utf-8"))
            if value in content and value not in result and len(result) < 32 and used + size <= 16384:
                result.append(value)
                used += size
    return tuple(result)


def simple_worker_action_json_schema(*, viewed_files: dict[str, str] | None = None,
                                    excluded_actions: frozenset[str] = frozenset(),
                                    disabled_file_rewrites: frozenset[str] = frozenset(),
                                    line_only_edit_paths: frozenset[str] = frozenset(),
                                    range_edits: bool = False,
                                    available_view_paths: tuple[str, ...] | None = None,
                                    compact_edits: bool = False,
                                    compact_fragments: bool = False) -> dict[str, Any]:
    """Self-contained branches for Ollama's JSON-schema grammar compiler.

    Root union branches must each include the entire canonical envelope:
    grammar backends need not combine union branches with sibling properties.
    The authoritative parser still validates every returned action.
    """
    arguments = {
        "VIEW": {"path": {"type": "string"}},
        "SEARCH": {"query": {"type": "string", "minLength": 1}, "path": {"type": "string"}, "case_sensitive": {"type": "boolean"}},
        "EDIT": {
            "match": {"type": "object", "additionalProperties": False,
                "required": ["kind", "file", "old_text", "expected_occurrences"],
                "properties": {"kind": {"type": "string", "enum": ["CREATE", "FRAGMENT"]},
                    "file": {"type": "object", "additionalProperties": False, "required": ["path"],
                             "properties": {"path": {"type": "string", "minLength": 1}}},
                    "old_text": {"anyOf": [{"type": "string", "minLength": 1}, {"type": "null"}]},
                    "expected_occurrences": {"type": "integer", "minimum": 1}}},
            "new_text": {"type": "string"}},
        "RUN": {"argv": {"type": "array", "items": {"type": "string"}, "minItems": 1}, "cwd": {"type": "string"}, "timeout_seconds": {"type": "integer", "minimum": 1}},
        "FINISH": {},
    }
    schema = {
        "oneOf": [
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["schema_version", "action", "arguments", "summary"],
                "properties": {
                    "schema_version": {"type": "integer", "enum": [1]},
                    "action": {"const": name},
                    "arguments": {"type": "object", "additionalProperties": False, "properties": fields, "required": list(fields)},
                    "summary": {"type": "string", "minLength": 1} if name == "FINISH" else {"type": "null"},
                },
            }
            for name, fields in arguments.items()
        ],
    }
    if viewed_files is not None and not compact_edits:
        edit_branch = next(branch for branch in schema["oneOf"] if branch["properties"]["action"]["const"] == "EDIT")
        schema["oneOf"].remove(edit_branch)
        from copy import deepcopy
        create_branch = deepcopy(edit_branch)
        create_branch["properties"]["arguments"]["properties"]["match"]["properties"]["old_text"] = {"type": "null"}
        create_branch["properties"]["arguments"]["properties"]["match"]["properties"]["kind"] = {"type": "string", "enum": ["CREATE"]}
        schema["oneOf"].append(create_branch)
        for path, content in viewed_files.items():
            if range_edits and content.splitlines():
                range_branch = deepcopy(edit_branch)
                range_branch["properties"]["arguments"] = {
                    "type": "object", "additionalProperties": False,
                    "required": ["match", "replacement_lines"], "properties": {
                        "match": {"type": "object", "additionalProperties": False,
                            "required": ["kind", "file", "start_line", "end_line"], "properties": {
                                "kind": {"type": "string", "enum": ["RANGE"]},
                                "file": {"type": "object", "additionalProperties": False, "required": ["path"],
                                         "properties": {"path": {"type": "string", "enum": [path]}}},
                                "start_line": {"type": "integer", "enum": list(range(1, len(content.splitlines()) + 1))},
                                "end_line": {"type": "integer", "enum": list(range(1, len(content.splitlines()) + 1))}}},
                        "replacement_lines": {"type": "array", "items": {"type": "string"}}}}
                range_branch["description"] = "Replace inclusive one-based observed source lines; each replacement array item is one literal source line."
                if path in line_only_edit_paths:
                    range_branch["properties"]["arguments"]["properties"]["replacement_lines"]["maxItems"] = 1
                    # The offered grammar must enforce the same single-line
                    # relation as the parser, not two independent line enums.
                    for line_number in range(1, len(content.splitlines()) + 1):
                        single = deepcopy(range_branch)
                        bounds = single["properties"]["arguments"]["properties"]["match"]["properties"]
                        bounds["start_line"] = {"type": "integer", "enum": [line_number]}
                        bounds["end_line"] = {"type": "integer", "enum": [line_number]}
                        schema["oneOf"].append(single)
                else:
                    schema["oneOf"].append(range_branch)
                if path not in disabled_file_rewrites and path not in line_only_edit_paths:
                    whole = deepcopy(edit_branch)
                    fields = whole["properties"]["arguments"]["properties"]["match"]["properties"]
                    fields["kind"] = {"type": "string", "enum": ["FILE"]}
                    fields["file"]["properties"]["path"] = {"type": "string", "enum": [path]}
                    fields["old_text"] = {"type": "string", "enum": [content]}
                    schema["oneOf"].append(whole)
                continue
            branch = deepcopy(edit_branch)
            fields = branch["properties"]["arguments"]["properties"]
            match_fields = fields["match"]["properties"]
            match_fields["kind"] = {"type": "string", "enum": ["LINE"]}
            match_fields["file"]["properties"]["path"] = {"type": "string", "enum": [path]}
            # Exact existing lines and the full observed source are available.
            # This constrains copying, not what the implementation should do.
            fragments = list(dict.fromkeys(line for line in content.splitlines() if line.strip()))
            match_fields["old_text"] = {"type": "string", "enum": list(dict.fromkeys(fragments))}
            # Source may contain quotes/backslashes. Keep the native JSON string
            # grammar: backend regex grammars do not reliably JSON-escape source
            # characters. The worker enforces the observed line/full-file shape.
            fields["new_text"] = {"type": "string"}
            branch["description"] = "Replace one complete observed source line with one source line."
            schema["oneOf"].append(branch)
            fragment = deepcopy(branch)
            fragment["description"] = "Replace an exact proper fragment of the observed file; never the entire file."
            fragment["properties"]["arguments"]["properties"]["match"]["properties"]["kind"] = {"type": "string", "enum": ["FRAGMENT"]}
            observed_fragments = observed_edit_fragments(content)
            fragment["properties"]["arguments"]["properties"]["match"]["properties"]["old_text"] = {"type": "string", "enum": list(observed_fragments)}
            if observed_fragments and path not in line_only_edit_paths:
                schema["oneOf"].append(fragment)
            whole = deepcopy(branch)
            whole["description"] = "Explicit complete file replacement using the full observed source."
            whole["properties"]["arguments"]["properties"]["match"]["properties"]["kind"] = {"type": "string", "enum": ["FILE"]}
            whole["properties"]["arguments"]["properties"]["match"]["properties"]["old_text"] = {"type": "string", "enum": [content]}
            whole["properties"]["arguments"]["properties"]["new_text"] = {"type": "string"}
            if path not in disabled_file_rewrites and path not in line_only_edit_paths:
                schema["oneOf"].append(whole)
    if compact_edits:
        # Keep target -> bounds -> source order without copying full source into
        # the grammar or forcing a model to split text into a line array.
        from copy import deepcopy
        template = next(b for b in schema["oneOf"] if b["properties"]["action"]["const"] == "EDIT")
        schema["oneOf"] = [b for b in schema["oneOf"] if b["properties"]["action"]["const"] != "EDIT"]
        def append_edit(fields):
            branch = deepcopy(template)
            branch.pop("description", None)
            branch["properties"]["arguments"] = {"type": "object", "additionalProperties": False,
                "required": list(fields), "properties": fields}
            schema["oneOf"].append(branch)
        append_edit({"file": {"type": "string", "minLength": 1}, "replacement": {"type": "string"}})
        if compact_fragments:
            append_edit({"file": {"type": "string", "minLength": 1},
                "old_text": {"type": "string", "minLength": 1},
                "replacement": {"type": "string"}})
        for path, content in (viewed_files or {}).items():
            numbers = list(range(1, len(content.splitlines()) + 1))
            for singleton in numbers if path in line_only_edit_paths else [None]:
                if not numbers:
                    continue
                allowed = [singleton] if singleton is not None else numbers
                append_edit({"file": {"type": "string", "enum": [path]},
                    "lines": {"type": "object", "additionalProperties": False,
                        "required": ["first", "last"], "properties": {
                            "first": {"type": "integer", "enum": allowed},
                            "last": {"type": "integer", "enum": allowed}}},
                    "replacement": {"type": "string"}})
    if available_view_paths is not None:
        if available_view_paths:
            view = next(branch for branch in schema["oneOf"]
                        if branch["properties"]["action"]["const"] == "VIEW")
            view["properties"]["arguments"]["properties"]["path"] = {
                "type": "string", "enum": list(available_view_paths)}
        else:
            excluded_actions = excluded_actions | {"VIEW"}
    schema["oneOf"] = [branch for branch in schema["oneOf"]
                       if branch["properties"]["action"]["const"] not in excluded_actions]
    return schema

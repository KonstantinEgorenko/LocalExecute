'Deterministic workspace tool dispatcher; one request -> one bounded operation.'
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Mapping

from .errors import (
    ToolDeniedError,
    ToolRequestError,
    WorkspaceConflictError,
    WorkspaceMutationError,
    WorkspacePathError,
    WorkspacePathIsFileError,
    WorkspaceReadError,
    WorkspaceScopeError,
)
from .models import ToolName, ToolRequest, ToolResult, ToolStatus, WorkspaceSession
from .workspace import Workspace, content_sha256, normalize_relative_path


def _arg(arguments: Mapping[str, Any], name: str, default: Any = None, *, required: bool = False) -> Any:
    if name not in arguments:
        if required:
            raise ToolRequestError(f"missing required argument: {name}")
        return default
    return arguments[name]


def _bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ToolRequestError(f"{name} must be a bool")
    return value


def _string(value: object, name: str, *, non_empty: bool = True) -> str:
    if not isinstance(value, str) or (non_empty and not value.strip()):
        requirement = "a non-empty string" if non_empty else "a string"
        raise ToolRequestError(f"{name} must be {requirement}")
    return value


def _positive_int(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ToolRequestError(f"{name} must be an integer > 0")
    return value


def _argv(value: object) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)) or not value:
        raise ToolRequestError("argv must be a non-empty list/tuple")
    result = tuple(_string(item, "argv item") for item in value)
    if any("\x00" in item for item in result):
        raise ToolRequestError("argv must not contain NUL bytes")
    return result


def _command_token(value: str) -> str:
    return Path(value).name.casefold() if os.name == "nt" else Path(value).name


def _prefix_allowed(argv: tuple[str, ...], prefixes: tuple[tuple[str, ...], ...]) -> bool:
    if not prefixes:
        return False
    actual = (_command_token(argv[0]), *argv[1:])
    for prefix in prefixes:
        normalized = (_command_token(prefix[0]), *prefix[1:])
        if len(actual) >= len(normalized) and tuple(actual[: len(normalized)]) == tuple(normalized):
            return True
    return False


class WorkspaceToolDispatcher:
    """Dispatch only explicit, typed tools inside a pre-validated WorkspaceSession."""

    def execute(self, session: WorkspaceSession, request: ToolRequest) -> ToolResult:
        if not isinstance(session, WorkspaceSession):
            raise ValueError("session must be a WorkspaceSession")
        if not isinstance(request, ToolRequest):
            raise ValueError("request must be a ToolRequest")
        if request.run_id != session.run_id or request.todo_id != session.todo_id:
            raise ToolRequestError("ToolRequest identity must match WorkspaceSession run_id/todo_id")
        workspace = Workspace(session)
        try:
            return self._execute(workspace, request)
        except WorkspaceConflictError as exc:
            return self._result(request, ToolStatus.CONFLICT, str(exc))
        except WorkspaceScopeError as exc:
            return self._result(request, ToolStatus.DENIED, str(exc))
        except ToolDeniedError as exc:
            return self._result(request, ToolStatus.DENIED, str(exc))
        except FileNotFoundError as exc:
            return self._result(request, ToolStatus.NOT_FOUND, str(exc))
        except WorkspacePathIsFileError as exc:
            return self._result(
                request, ToolStatus.INVALID, str(exc),
                {
                    "error_code": "PATH_IS_FILE",
                    "path": exc.path,
                    "requested_tool": ToolName.LIST_FILES.value,
                    "suggested_tool": ToolName.READ_TEXT.value,
                    "suggested_arguments": {"path": exc.path},
                },
            )
        except (ToolRequestError, WorkspacePathError, WorkspaceReadError, WorkspaceMutationError) as exc:
            return self._result(request, ToolStatus.INVALID, str(exc))
        except subprocess.TimeoutExpired:
            return self._result(request, ToolStatus.TIMEOUT, "command timed out")
        except OSError as exc:
            return self._result(request, ToolStatus.ERROR, f"OS error: {exc}")

    def _execute(self, workspace: Workspace, request: ToolRequest) -> ToolResult:
        args = request.arguments
        if request.tool is ToolName.LIST_FILES:
            path = _arg(args, "path", "")
            recursive = _bool(_arg(args, "recursive", True), "recursive")
            entries = workspace.list_files(path, recursive=recursive)
            return self._result(
                request,
                ToolStatus.SUCCESS,
                json.dumps(entries, ensure_ascii=False, sort_keys=True),
                {"entry_count": len(entries)},
            )

        if request.tool is ToolName.READ_TEXT:
            path = _arg(args, "path", required=True)
            logical, content = workspace.read_text(path)
            return self._result(
                request,
                ToolStatus.SUCCESS,
                content,
                {
                    "path": logical,
                    "sha256": content_sha256(content),
                    "size_bytes": len(content.encode("utf-8")),
                },
            )

        if request.tool is ToolName.SEARCH_TEXT:
            query = _arg(args, "query", required=True)
            path = _arg(args, "path", "")
            case_sensitive = _bool(_arg(args, "case_sensitive", True), "case_sensitive")
            matches = workspace.search_text(query, path, case_sensitive=case_sensitive)
            return self._result(
                request,
                ToolStatus.SUCCESS,
                json.dumps(matches, ensure_ascii=False, sort_keys=True),
                {"match_count": len(matches)},
            )

        if request.tool is ToolName.WRITE_TEXT:
            path = _arg(args, "path", required=True)
            content = _string(_arg(args, "content", required=True), "content", non_empty=False)
            expected = _arg(args, "expected_sha256", None)
            logical, digest = workspace.write_text(path, content, expected_sha256=expected)
            return self._result(
                request,
                ToolStatus.SUCCESS,
                f"wrote {logical}",
                {"path": logical, "sha256": digest, "size_bytes": len(content.encode("utf-8"))},
                (logical,),
            )

        if request.tool is ToolName.REPLACE_TEXT:
            path = _arg(args, "path", required=True)
            old_text = _string(_arg(args, "old_text", required=True), "old_text")
            new_text = _string(_arg(args, "new_text", required=True), "new_text", non_empty=False)
            expected = _arg(args, "expected_sha256", required=True)
            occurrences = _positive_int(_arg(args, "expected_occurrences", 1), "expected_occurrences")
            logical, digest = workspace.replace_text(
                path,
                old_text,
                new_text,
                expected_sha256=expected,
                expected_occurrences=occurrences,
            )
            return self._result(
                request,
                ToolStatus.SUCCESS,
                f"updated {logical}",
                {"path": logical, "sha256": digest},
                (logical,),
            )

        if request.tool is ToolName.DELETE_FILE:
            path = _arg(args, "path", required=True)
            expected = _arg(args, "expected_sha256", required=True)
            logical = workspace.delete_file(path, expected_sha256=expected)
            return self._result(request, ToolStatus.SUCCESS, f"deleted {logical}", {"path": logical}, (logical,))

        if request.tool is ToolName.RUN_COMMAND:
            return self._run_command(workspace, request)

        raise ToolRequestError(f"unsupported tool: {request.tool}")

    def _run_command(self, workspace: Workspace, request: ToolRequest) -> ToolResult:
        argv = _argv(_arg(request.arguments, "argv", required=True))
        if not _prefix_allowed(argv, workspace.session.policy.command_prefixes):
            raise ToolDeniedError("command is not allowed by command_prefixes")
        cwd_value = _arg(request.arguments, "cwd", "")
        logical, cwd = workspace._candidate(cwd_value, allow_root=True)  # centralized scope/containment guard
        if not cwd.is_dir():
            raise ToolRequestError("command cwd must be a directory")
        timeout = _arg(request.arguments, "timeout_seconds", workspace.session.policy.max_command_seconds)
        timeout = _positive_int(timeout, "timeout_seconds")
        timeout = min(timeout, workspace.session.policy.max_command_seconds)
        executable = shutil.which(argv[0])
        if executable is None:
            raise ToolRequestError(f"command executable not found: {argv[0]}")
        command = (executable, *argv[1:])
        env = self._minimal_env()
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=False,
            shell=False,
            timeout=timeout,
            check=False,
        )
        raw = completed.stdout + completed.stderr
        limit = workspace.session.policy.max_command_output_bytes
        truncated = len(raw) > limit
        raw = raw[:limit]
        output = raw.decode("utf-8", errors="replace")
        status = ToolStatus.SUCCESS if completed.returncode == 0 else ToolStatus.ERROR
        return self._result(
            request,
            status,
            output,
            {
                "cwd": logical,
                "returncode": completed.returncode,
                "truncated": truncated,
                "output_bytes": len(raw),
            },
        )

    @staticmethod
    def _minimal_env() -> dict[str, str]:
        allowed = (
            "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "TEMP", "TMP",
            "HOME", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "VIRTUAL_ENV",
        )
        return {key: value for key, value in os.environ.items() if key.upper() in allowed}

    @staticmethod
    def _result(
        request: ToolRequest,
        status: ToolStatus,
        output: str,
        metadata: Mapping[str, Any] | None = None,
        changed_paths: tuple[str, ...] = (),
    ) -> ToolResult:
        return ToolResult(
            call_id=request.call_id,
            run_id=request.run_id,
            todo_id=request.todo_id,
            tool=request.tool,
            status=status,
            output=output,
            metadata={} if metadata is None else metadata,
            changed_paths=changed_paths,
        )

"""High-level ACI adapter over the existing bounded Workspace dispatcher."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from harness.workspace import (
    ToolName,
    ToolRequest,
    ToolStatus,
    Workspace,
    WorkspaceToolDispatcher,
    WorkspaceSession,
    content_sha256,
)
from harness.workspace.errors import (
    WorkspaceMutationError,
    WorkspacePathError,
    WorkspaceReadError,
    WorkspaceScopeError,
)

from .models import (
    AciActionKindV2,
    AciActionV2,
    AciErrorCodeV2,
    AciObservationV2,
)


_STATUS_ERROR = {
    ToolStatus.DENIED: AciErrorCodeV2.LOW_LEVEL_DENIED,
    ToolStatus.CONFLICT: AciErrorCodeV2.LOW_LEVEL_CONFLICT,
    ToolStatus.INVALID: AciErrorCodeV2.LOW_LEVEL_INVALID,
    ToolStatus.NOT_FOUND: AciErrorCodeV2.PATH_NOT_FOUND,
    ToolStatus.TIMEOUT: AciErrorCodeV2.LOW_LEVEL_TIMEOUT,
    ToolStatus.ERROR: AciErrorCodeV2.LOW_LEVEL_ERROR,
}


class WorkspaceAciV2:
    """Reduce model action entropy without replacing workspace authority."""

    def __init__(self, dispatcher: WorkspaceToolDispatcher | None = None) -> None:
        self.dispatcher = dispatcher or WorkspaceToolDispatcher()

    def execute(
        self,
        *,
        session: WorkspaceSession,
        call_id: str,
        action: AciActionV2,
    ) -> AciObservationV2:
        if not isinstance(session, WorkspaceSession):
            raise TypeError("session must be WorkspaceSession")
        if not isinstance(call_id, str) or not call_id.strip():
            raise ValueError("call_id must be a non-empty string")
        if not isinstance(action, AciActionV2):
            raise TypeError("action must be AciActionV2")

        if action.kind is AciActionKindV2.VIEW:
            return self._view(session, call_id, action)
        if action.kind is AciActionKindV2.SEARCH:
            return self._search(session, call_id, action)
        if action.kind is AciActionKindV2.EDIT:
            return self._edit(session, call_id, action)
        if action.kind is AciActionKindV2.RUN:
            return self._run(session, call_id, action)
        return self._error(
            action.kind,
            AciErrorCodeV2.INVALID_ACTION,
            "unsupported ACI action",
        )

    def _request(
        self,
        session: WorkspaceSession,
        call_id: str,
        tool: ToolName,
        arguments: dict[str, Any],
    ):
        return self.dispatcher.execute(
            session,
            ToolRequest(
                call_id=call_id,
                run_id=session.run_id,
                todo_id=session.todo_id,
                tool=tool,
                arguments=arguments,
            ),
        )

    def _from_tool(self, action: AciActionKindV2, result) -> AciObservationV2:
        metadata = dict(result.metadata)
        metadata["low_level_tool"] = result.tool.value
        metadata["low_level_status"] = result.status.value
        if result.status is ToolStatus.SUCCESS:
            return AciObservationV2(
                action=action,
                ok=True,
                output=result.output,
                metadata=metadata,
                changed_paths=result.changed_paths,
            )
        return AciObservationV2(
            action=action,
            ok=False,
            output=result.output,
            metadata=metadata,
            changed_paths=(),
            error_code=_STATUS_ERROR.get(
                result.status,
                AciErrorCodeV2.LOW_LEVEL_ERROR,
            ),
        )

    def _view(
        self,
        session: WorkspaceSession,
        call_id: str,
        action: AciActionV2,
    ) -> AciObservationV2:
        workspace = Workspace(session)
        try:
            logical, candidate = workspace._candidate(action.path, allow_root=True)
        except FileNotFoundError:
            return self._error(
                action.kind,
                AciErrorCodeV2.PATH_NOT_FOUND,
                f"path not found: {action.path}",
            )
        except (WorkspacePathError, WorkspaceScopeError, WorkspaceReadError) as exc:
            return self._error(
                action.kind,
                AciErrorCodeV2.PATH_NOT_VIEWABLE,
                str(exc),
            )

        if candidate.is_file():
            result = self._request(
                session,
                f"{call_id}:read",
                ToolName.READ_TEXT,
                {"path": logical},
            )
            observation = self._from_tool(action.kind, result)
            # Advisory source observation only. verification still owns acceptance.
            if observation.ok and len(observation.output.encode("utf-8")) <= 16384:
                diagnostic = self._python_preflight(logical, observation.output)
                if diagnostic is not None:
                    return AciObservationV2(action=observation.action, ok=True,
                        output=observation.output, changed_paths=(),
                        metadata={**observation.metadata, "advisory_python_syntax_error": {
                            **diagnostic, "version": "workspace_read", "advisory_only": True}})
            return observation

        if candidate.is_dir():
            result = self._request(
                session,
                f"{call_id}:list",
                ToolName.LIST_FILES,
                {"path": logical, "recursive": False},
            )
            return self._from_tool(action.kind, result)

        return self._error(
            action.kind,
            AciErrorCodeV2.PATH_NOT_VIEWABLE,
            "path is neither a regular file nor a directory",
        )

    def _search(
        self,
        session: WorkspaceSession,
        call_id: str,
        action: AciActionV2,
    ) -> AciObservationV2:
        result = self._request(
            session,
            f"{call_id}:search",
            ToolName.SEARCH_TEXT,
            {
                "query": action.query,
                "path": action.path,
                "case_sensitive": action.case_sensitive,
            },
        )
        observation = self._from_tool(action.kind, result)
        if not observation.ok:
            return observation
        # SEARCH_TEXT owns authorization and bounds. Decode its trusted wire
        # representation once; source snippets must reach the model literally,
        # like VIEW, rather than as JSON-escaped strings to copy into an edit.
        rows = json.loads(observation.output)
        locations = [{key: row[key] for key in ("path", "line", "column")}
                     for row in rows]
        output = "\n\n".join(
            f"MATCH {index}\n{row['text']}" for index, row in enumerate(rows, 1))
        return AciObservationV2(action=observation.action, ok=True, output=output,
            metadata={**observation.metadata, "match_locations": locations,
                      "snippet_format": "literal_source"},
            changed_paths=observation.changed_paths)

    def _edit(
        self,
        session: WorkspaceSession,
        call_id: str,
        action: AciActionV2,
    ) -> AciObservationV2:
        workspace = Workspace(session)
        try:
            logical, candidate = workspace._candidate(action.path, mutation=True)
        except FileNotFoundError:
            return self._error(
                action.kind,
                AciErrorCodeV2.PATH_NOT_FOUND,
                f"parent path not found for edit target: {action.path}",
            )
        except (WorkspacePathError, WorkspaceScopeError, WorkspaceMutationError) as exc:
            return self._error(
                action.kind,
                AciErrorCodeV2.LOW_LEVEL_DENIED
                if isinstance(exc, WorkspaceScopeError)
                else AciErrorCodeV2.LOW_LEVEL_INVALID,
                str(exc),
            )

        if candidate.exists():
            if not candidate.is_file():
                return self._error(
                    action.kind,
                    AciErrorCodeV2.LOW_LEVEL_INVALID,
                    "edit target must be a regular file",
                )
            try:
                _, current = workspace.read_text(logical)
            except (WorkspaceReadError, WorkspacePathError, WorkspaceScopeError) as exc:
                return self._error(
                    action.kind,
                    AciErrorCodeV2.LOW_LEVEL_INVALID,
                    str(exc),
                )

            if (action.expected_observed_content is not None
                    and current != action.expected_observed_content):
                return self._error(
                    action.kind,
                    AciErrorCodeV2.LOW_LEVEL_CONFLICT,
                    "observed file content changed; VIEW the target again before EDIT",
                    {"path": logical, "workspace_mutated": False, "required_action": "VIEW"},
                )

            if action.old_text is None:
                return self._error(
                    action.kind,
                    AciErrorCodeV2.EXACT_REPLACEMENT_REQUIRED,
                    "existing files require exact old_text; full-file rewrite is not exposed by ACI V2",
                    {
                        "path": logical,
                        "required_action": "EDIT",
                        "required_fields": ["old_text", "new_text"],
                    },
                )
            if action.old_text == action.new_text:
                return self._error(
                    action.kind,
                    AciErrorCodeV2.NO_EFFECT,
                    "old_text and new_text must differ",
                    {"path": logical},
                )

            observed = current.count(action.old_text)
            if observed != action.expected_occurrences:
                return self._error(
                    action.kind,
                    AciErrorCodeV2.OCCURRENCE_MISMATCH,
                    (
                        "old_text occurrence count mismatch: "
                        f"expected {action.expected_occurrences}, observed {observed}"
                    ),
                    {
                        "path": logical,
                        "expected_occurrences": action.expected_occurrences,
                        "observed_occurrences": observed,
                    },
                )

            updated = current.replace(action.old_text, action.new_text)
            syntax_error = self._python_preflight(logical, updated)
            if syntax_error is not None:
                return self._error(
                    action.kind,
                    AciErrorCodeV2.SYNTAX_PREFLIGHT_FAILED,
                    syntax_error["message"],
                    {
                        "path": logical,
                        "workspace_mutated": False,
                        "preflight": "python_compile",
                        "syntax_error": {**syntax_error, "version": "proposed_candidate"},
                    },
                )

            result = self._request(
                session,
                f"{call_id}:replace",
                ToolName.REPLACE_TEXT,
                {
                    "path": logical,
                    "old_text": action.old_text,
                    "new_text": action.new_text,
                    "expected_sha256": content_sha256(current),
                    "expected_occurrences": action.expected_occurrences,
                },
            )
            observation = self._from_tool(action.kind, result)
            if observation.ok:
                metadata = dict(observation.metadata)
                metadata.update(
                    {
                        "preflight": "python_compile" if logical.endswith(".py") else "none",
                        "cas_bound_by_harness": True,
                        "edit_mode": "exact_replace",
                    }
                )
                return AciObservationV2(
                    action=observation.action,
                    ok=True,
                    output=observation.output,
                    metadata=metadata,
                    changed_paths=observation.changed_paths,
                )
            return observation

        if action.old_text is not None:
            return self._error(
                action.kind,
                AciErrorCodeV2.LOW_LEVEL_INVALID,
                "new file creation requires old_text=null",
                {"path": logical},
            )

        syntax_error = self._python_preflight(logical, action.new_text or "")
        if syntax_error is not None:
            return self._error(
                action.kind,
                AciErrorCodeV2.SYNTAX_PREFLIGHT_FAILED,
                syntax_error["message"],
                {
                    "path": logical,
                    "workspace_mutated": False,
                    "preflight": "python_compile",
                    "syntax_error": {**syntax_error, "version": "proposed_candidate"},
                },
            )

        result = self._request(
            session,
            f"{call_id}:create",
            ToolName.WRITE_TEXT,
            {"path": logical, "content": action.new_text or ""},
        )
        observation = self._from_tool(action.kind, result)
        if observation.ok:
            metadata = dict(observation.metadata)
            metadata.update(
                {
                    "preflight": "python_compile" if logical.endswith(".py") else "none",
                    "cas_bound_by_harness": True,
                    "edit_mode": "create",
                }
            )
            return AciObservationV2(
                action=observation.action,
                ok=True,
                output=observation.output,
                metadata=metadata,
                changed_paths=observation.changed_paths,
            )
        return observation

    def _run(
        self,
        session: WorkspaceSession,
        call_id: str,
        action: AciActionV2,
    ) -> AciObservationV2:
        result = self._request(
            session,
            f"{call_id}:run",
            ToolName.RUN_COMMAND,
            {
                "argv": list(action.argv),
                "cwd": action.cwd,
                "timeout_seconds": action.timeout_seconds,
            },
        )
        return self._from_tool(action.kind, result)

    @staticmethod
    def _python_preflight(path: str, content: str) -> dict[str, Any] | None:
        if Path(path).suffix.casefold() != ".py":
            return None
        try:
            # Match Phase7's compile-time syntax rules, including invalid
            # statement placement that ast.parse alone accepts. Never execute.
            compile(content, path, "exec", dont_inherit=True)
        except SyntaxError as exc:
            source_line = exc.text
            if source_line is None and exc.lineno is not None:
                lines = content.splitlines()
                source_line = lines[exc.lineno - 1] if 0 < exc.lineno <= len(lines) else ""
            location = ""
            if exc.lineno is not None:
                location = f" at line {exc.lineno}"
                if exc.offset is not None:
                    location += f", column {exc.offset}"
            return {"message": f"Python syntax preflight failed{location}: {exc.msg}",
                    "line": exc.lineno, "column": exc.offset,
                    "source_line": (source_line or "").rstrip("\r\n").encode("utf-8")[:1024].decode("utf-8", errors="ignore")}
        return None

    @staticmethod
    def _error(
        action: AciActionKindV2,
        code: AciErrorCodeV2,
        output: str,
        metadata: dict[str, Any] | None = None,
    ) -> AciObservationV2:
        return AciObservationV2(
            action=action,
            ok=False,
            output=output,
            metadata={} if metadata is None else metadata,
            changed_paths=(),
            error_code=code,
        )

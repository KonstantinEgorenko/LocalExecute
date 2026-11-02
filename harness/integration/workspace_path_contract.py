'Production-only guard for ambiguous placeholder-like workspace paths.\n\nFrozen coding prompts historically document repository-relative path arguments with\nexamples such as ``relative/file``. Some local models interpret the word\n``relative`` as a literal top-level directory and emit ``relative/foo.py`` even\nwhen ``foo.py`` exists at repository root. The workspace dispatcher correctly\nfails those requests, but repeated NOT_FOUND results can waste the entire bounded\nexecution budget.\n\nThis additive guard never rewrites or executes a mutation. It only detects the\nspecific unambiguous case where:\n\n* the request uses ``relative/<suffix>``;\n* the workspace does not contain a real top-level ``relative`` entry; and\n* the stripped repository-relative target already exists (or, for RUN_COMMAND,\n  the stripped cwd exists as a directory).\n\nThe request is rejected as INVALID with a deterministic suggested path. The\nworker must explicitly issue a corrected tool call on the next turn.\n'
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..workspace.models import ToolName, ToolRequest, ToolResult, ToolStatus, WorkspaceSession


WORKSPACE_PATH_GUIDANCE = (
    "WORKSPACE PATH CONTRACT: tool paths are already repository-relative. "
    "Do not prepend the literal directory name 'relative/'. For example use "
    "path='candidate.py', not path='relative/candidate.py'. Re-issue the tool "
    "call with the suggested repository-relative path."
)

_PATH_TOOLS = {
    ToolName.LIST_FILES,
    ToolName.READ_TEXT,
    ToolName.SEARCH_TEXT,
    ToolName.WRITE_TEXT,
    ToolName.REPLACE_TEXT,
    ToolName.DELETE_FILE,
}
_PREFIX = "relative/"


def _suggested_path(session: WorkspaceSession, request: ToolRequest) -> str | None:
    if not isinstance(session, WorkspaceSession):
        return None
    if request.tool not in _PATH_TOOLS and request.tool is not ToolName.RUN_COMMAND:
        return None

    key = "cwd" if request.tool is ToolName.RUN_COMMAND else "path"
    raw = request.arguments.get(key)
    if not isinstance(raw, str):
        return None
    normalized = raw.strip().replace("\\", "/")
    if not normalized.casefold().startswith(_PREFIX):
        return None

    suffix = normalized[len(_PREFIX):].strip("/")
    if not suffix:
        return None

    root = Path(session.root).resolve(strict=True)
    # A real repository directory named ``relative`` makes the intent
    # ambiguous. In that case preserve frozen dispatcher semantics exactly.
    if root.joinpath("relative").exists():
        return None

    candidate = root.joinpath(*suffix.split("/"))
    if request.tool is ToolName.RUN_COMMAND:
        return suffix if candidate.is_dir() else None

    # Existing-file operations are the high-confidence case observed in live
    # acceptance. LIST_FILES/SEARCH_TEXT may target either a file or directory.
    # WRITE_TEXT is guarded only when the stripped target already exists; new
    # file creation is never redirected by this adapter.
    return suffix if candidate.exists() else None


@dataclass(slots=True)
class ProductionWorkspacePathContractDispatcher:
    """Fail fast on unambiguous literal-placeholder path misuse."""

    inner: Any

    def execute(self, session, request):
        suggestion = _suggested_path(session, request)
        if suggestion is None:
            return self.inner.execute(session, request)
        key = "cwd" if request.tool is ToolName.RUN_COMMAND else "path"
        return ToolResult(
            request.call_id,
            request.run_id,
            request.todo_id,
            request.tool,
            ToolStatus.INVALID,
            f"{WORKSPACE_PATH_GUIDANCE} Suggested {key}='{suggestion}'.",
            {
                "workspace_path_contract": True,
                "placeholder_prefix": "relative/",
                "suggested_argument": key,
                "suggested_path": suggestion,
            },
            (),
        )

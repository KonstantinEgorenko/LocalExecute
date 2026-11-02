"""Policy-derived model-facing Tool Protocol V2 capability set."""
from __future__ import annotations

from dataclasses import dataclass

from harness.workspace.models import WorkspaceAccess, WorkspaceSession
from harness.executor.profile_tool_models import V2ToolName

_BASE_READ = (
    V2ToolName.LIST_FILES,
    V2ToolName.READ_TEXT,
    V2ToolName.SEARCH_TEXT,
)
_MUTATIONS = (
    V2ToolName.CREATE_TEXT,
    V2ToolName.WRITE_TEXT,
    V2ToolName.REPLACE_TEXT,
    V2ToolName.DELETE_FILE,
)


@dataclass(frozen=True, slots=True)
class V2ToolCapabilities:
    tools: tuple[V2ToolName, ...]
    can_mutate: bool
    can_run_commands: bool

    def __post_init__(self) -> None:
        if (
            not isinstance(self.tools, tuple)
            or any(not isinstance(item, V2ToolName) for item in self.tools)
        ):
            raise ValueError("tools must be tuple[V2ToolName, ...]")
        if len(self.tools) != len(set(self.tools)):
            raise ValueError("tools must not contain duplicates")
        if not isinstance(self.can_mutate, bool) or not isinstance(
            self.can_run_commands, bool
        ):
            raise ValueError("capability flags must be bool")

    def contains(self, tool: V2ToolName) -> bool:
        return tool in self.tools


def capabilities_for_session(session: WorkspaceSession) -> V2ToolCapabilities:
    if not isinstance(session, WorkspaceSession):
        raise ValueError("session must be WorkspaceSession")
    can_mutate = session.policy.access is WorkspaceAccess.READ_WRITE
    can_run_commands = bool(session.policy.command_prefixes)
    tools = list(_BASE_READ)
    if can_mutate:
        tools.extend(_MUTATIONS)
    if can_run_commands:
        tools.append(V2ToolName.RUN_COMMAND)
    return V2ToolCapabilities(tuple(tools), can_mutate, can_run_commands)

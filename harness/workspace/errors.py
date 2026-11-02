'workspace workspace/tool-dispatch errors.'

class WorkspaceError(RuntimeError):
    """Base workspace boundary error."""


class WorkspacePathError(WorkspaceError):
    """A requested path is invalid or escapes the workspace."""


class WorkspaceScopeError(WorkspaceError):
    """A requested path is outside the active WorkContract scope."""


class WorkspaceReadError(WorkspaceError):
    """A file cannot be read safely as bounded UTF-8 text."""


class WorkspacePathIsFileError(WorkspaceReadError):
    """LIST_FILES was requested for an authorized regular file."""

    def __init__(self, path: str) -> None:
        super().__init__("list path is not a directory")
        self.path = path


class WorkspaceMutationError(WorkspaceError):
    """A requested mutation is invalid or unsafe."""


class WorkspaceConflictError(WorkspaceMutationError):
    """Optimistic concurrency guard failed."""


class ToolRequestError(WorkspaceError):
    """A tool request is malformed or violates dispatcher invariants."""


class ToolDeniedError(WorkspaceError):
    """A valid tool request is denied by policy."""

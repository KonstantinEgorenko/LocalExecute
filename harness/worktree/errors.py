'worker agent.5 isolated-worktree errors.'


class WorktreeError(RuntimeError):
    """Base isolated-worktree error."""


class GitRepositoryError(WorktreeError):
    """A repository cannot be discovered or does not satisfy Git invariants."""


class ProtectedRepositoryError(WorktreeError):
    """The requested repository is protected and cannot be an agent target."""


class WorktreePolicyError(WorktreeError):
    """The requested worktree operation violates isolation policy."""


class WorktreeConflictError(WorktreeError):
    """Durable lease state conflicts with the filesystem/Git state."""


class WorktreeDirtyError(WorktreeError):
    """A destructive cleanup was requested for a dirty worktree without force."""

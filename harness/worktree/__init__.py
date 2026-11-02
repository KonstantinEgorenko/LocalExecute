'worker agent.5 isolated Git worktrees and protected control-repository boundary.'
from .errors import (
    GitRepositoryError,
    ProtectedRepositoryError,
    WorktreeConflictError,
    WorktreeDirtyError,
    WorktreeError,
    WorktreePolicyError,
)
from .git import GitClient
from .guard import RepositoryGuard
from .integration import ControlPlaneIsolatedWorkspaceResolver
from .manager import GitWorktreeManager
from .models import (
    RepositoryIdentity,
    WorktreeLease,
    WorktreeLeaseStatus,
    WorktreePolicy,
    WorktreeSnapshot,
)
from .store import FileWorktreeLeaseStore

__all__ = [
    "ControlPlaneIsolatedWorkspaceResolver",
    "FileWorktreeLeaseStore",
    "GitClient",
    "GitRepositoryError",
    "GitWorktreeManager",
    "ProtectedRepositoryError",
    "RepositoryGuard",
    "RepositoryIdentity",
    "WorktreeConflictError",
    "WorktreeDirtyError",
    "WorktreeError",
    "WorktreeLease",
    "WorktreeLeaseStatus",
    "WorktreePolicy",
    "WorktreePolicyError",
    "WorktreeSnapshot",
]

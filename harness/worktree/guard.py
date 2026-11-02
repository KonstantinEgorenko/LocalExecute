"""Repository identity discovery and protected-control-repository checks."""
from __future__ import annotations

import os
from pathlib import Path

from .errors import GitRepositoryError, ProtectedRepositoryError, WorktreePolicyError
from .git import GitClient
from .models import RepositoryIdentity


def _canonical(path: str | Path, *, strict: bool = True) -> Path:
    candidate = Path(path).expanduser().resolve(strict=strict)
    return candidate


def _same_or_nested(candidate: Path, parent: Path) -> bool:
    try:
        candidate.relative_to(parent)
        return True
    except ValueError:
        return False


class RepositoryGuard:
    def __init__(
        self,
        *,
        protected_roots: tuple[str | Path, ...],
        managed_root: str | Path,
        git: GitClient | None = None,
    ) -> None:
        self.git = git or GitClient()
        self.protected_roots = tuple(_canonical(value) for value in protected_roots)
        self.managed_root = _canonical(managed_root, strict=False)
        for protected in self.protected_roots:
            if _same_or_nested(self.managed_root, protected):
                raise WorktreePolicyError("managed worktree root must not be inside a protected repository root")
        self._protected_identities = tuple(self.discover(value) for value in self.protected_roots)

    def discover(self, repo_root: str | Path) -> RepositoryIdentity:
        requested = _canonical(repo_root)
        top = self.git.text(requested, "rev-parse", "--show-toplevel")
        if not top:
            raise GitRepositoryError("git repository top-level is empty")
        root = _canonical(top)
        common = self.git.text(root, "rev-parse", "--git-common-dir")
        common_path = Path(common)
        if not common_path.is_absolute():
            common_path = root / common_path
        common_path = common_path.resolve(strict=True)
        head = self.git.text(root, "rev-parse", "HEAD")
        if len(head) != 40:
            raise GitRepositoryError("HEAD must resolve to a full 40-character commit id")
        return RepositoryIdentity(root=str(root), git_common_dir=str(common_path), head_commit=head)

    def assert_target_allowed(self, repo_root: str | Path, *, protect_same_git_common_dir: bool = True) -> RepositoryIdentity:
        identity = self.discover(repo_root)
        root = Path(identity.root)
        common = Path(identity.git_common_dir)
        for protected_root, protected_identity in zip(self.protected_roots, self._protected_identities, strict=True):
            if _same_or_nested(root, protected_root) or _same_or_nested(protected_root, root):
                raise ProtectedRepositoryError(f"target repository overlaps protected control repository: {protected_root}")
            if protect_same_git_common_dir and os.path.normcase(str(common)) == os.path.normcase(protected_identity.git_common_dir):
                raise ProtectedRepositoryError("target repository shares Git common-dir with protected control repository")
        if _same_or_nested(self.managed_root, root) or _same_or_nested(root, self.managed_root):
            raise WorktreePolicyError("managed worktree root and source repository must not overlap")
        return identity

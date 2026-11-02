'Fail-closed repository-local filesystem operations for workspace.'
from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path, PurePosixPath

from .errors import (
    WorkspaceConflictError,
    WorkspaceMutationError,
    WorkspacePathError,
    WorkspacePathIsFileError,
    WorkspaceReadError,
    WorkspaceScopeError,
)
from .models import WorkspaceAccess, WorkspaceSession


def normalize_relative_path(value: object, *, allow_root: bool = False) -> str:
    if not isinstance(value, str):
        raise WorkspacePathError("path must be a string")
    raw = value.strip().replace("\\", "/")
    if raw in ("", "."):
        if allow_root:
            return ""
        raise WorkspacePathError("path must name a workspace entry")
    if raw.startswith("/") or raw.startswith("//"):
        raise WorkspacePathError("absolute paths are forbidden")
    if len(raw) >= 2 and raw[1] == ":":
        raise WorkspacePathError("drive-qualified paths are forbidden")
    raw = raw.rstrip("/")
    raw_parts = raw.split("/")
    if any(part in ("", ".", "..") for part in raw_parts):
        raise WorkspacePathError("path traversal and dot components are forbidden")
    path = PurePosixPath(raw)
    parts = path.parts
    normalized = "/".join(parts)
    if normalized.startswith("../") or normalized == "..":
        raise WorkspacePathError("path traversal is forbidden")
    return normalized


def content_sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _scope_comparison_key(value: str) -> str:
    """Normalize logical workspace paths using host filesystem case semantics."""
    normalized = normalize_relative_path(value, allow_root=True)
    return os.path.normcase(normalized).replace("\\", "/")


def _prefix_match(path: str, prefix: str) -> bool:
    normalized_path = _scope_comparison_key(path)
    normalized_prefix = _scope_comparison_key(prefix)
    if normalized_prefix == "":
        return True
    return (
        normalized_path == normalized_prefix
        or normalized_path.startswith(normalized_prefix + "/")
    )


class Workspace:
    def __init__(self, session: WorkspaceSession) -> None:
        if not isinstance(session, WorkspaceSession):
            raise ValueError("session must be a WorkspaceSession")
        self.session = session
        self.root = Path(session.root).resolve(strict=True)
        if not self.root.is_dir():
            raise WorkspacePathError("workspace root must be a directory")

    def _logical_scope(self, path: str, *, mutation: bool) -> None:
        policy = self.session.policy
        if any(_prefix_match(path, value) for value in policy.forbidden_paths):
            raise WorkspaceScopeError(f"path is forbidden by WorkContract/policy: {path}")
        if policy.allowed_paths and not any(_prefix_match(path, value) for value in policy.allowed_paths):
            raise WorkspaceScopeError(f"path is outside allowed WorkContract scope: {path}")
        if mutation:
            if policy.access is WorkspaceAccess.READ_ONLY:
                raise WorkspaceScopeError("workspace session is read-only")
            if any(_prefix_match(path, value) for value in policy.protected_paths):
                raise WorkspaceScopeError(f"path is protected from mutation: {path}")

    def _candidate(self, path: object, *, mutation: bool = False, allow_root: bool = False) -> tuple[str, Path]:
        logical = normalize_relative_path(path, allow_root=allow_root)
        self._logical_scope(logical, mutation=mutation)
        candidate = self.root if logical == "" else self.root.joinpath(*logical.split("/"))
        if mutation:
            parent = candidate.parent.resolve(strict=True)
            self._require_contained(parent)
            resolved = parent / candidate.name
            if candidate.exists() or candidate.is_symlink():
                resolved = candidate.resolve(strict=True)
        else:
            resolved = candidate.resolve(strict=True)
        self._canonical_scope(resolved, mutation=mutation)
        if mutation and candidate.is_symlink():
            raise WorkspaceMutationError("mutating symlink paths is forbidden")
        return logical, candidate

    def _canonical_scope(self, resolved: Path, *, mutation: bool) -> None:
        """Apply the same policy to the effective workspace-relative destination."""
        self._require_contained(resolved)
        self._logical_scope(resolved.relative_to(self.root).as_posix(), mutation=mutation)

    def _require_contained(self, value: Path) -> None:
        try:
            value.relative_to(self.root)
        except ValueError as exc:
            raise WorkspacePathError("resolved path escapes workspace root") from exc

    def read_text(self, path: object) -> tuple[str, str]:
        logical, candidate = self._candidate(path)
        if not candidate.is_file():
            raise WorkspaceReadError("path is not a regular file")
        size = candidate.stat().st_size
        if size > self.session.policy.max_read_bytes:
            raise WorkspaceReadError("file exceeds max_read_bytes")
        try:
            content = candidate.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise WorkspaceReadError("file is not valid UTF-8 text") from exc
        return logical, content

    def list_files(self, path: object = "", *, recursive: bool = True) -> tuple[dict[str, object], ...]:
        logical, candidate = self._candidate(path, allow_root=True)
        if candidate.is_file():
            raise WorkspacePathIsFileError(logical)
        if not candidate.is_dir():
            raise WorkspaceReadError("list path is not a directory")
        entries: list[dict[str, object]] = []
        stack = [candidate]
        while stack:
            directory = stack.pop()
            children = sorted(directory.iterdir(), key=lambda item: item.name.casefold())
            for child in children:
                rel = child.relative_to(self.root).as_posix()
                try:
                    self._logical_scope(rel, mutation=False)
                except WorkspaceScopeError:
                    continue
                try:
                    self._canonical_scope(child.resolve(strict=False), mutation=False)
                except WorkspaceScopeError:
                    continue
                except WorkspacePathError:
                    pass  # External links remain diagnostic entries, never traversed.
                if child.is_symlink():
                    entry_type = "symlink"
                    size = 0
                else:
                    try:
                        self._require_contained(child.resolve(strict=True))
                    except WorkspacePathError:
                        entry_type = "external-link"
                        size = 0
                    else:
                        if child.is_dir():
                            entry_type = "dir"
                            size = 0
                        elif child.is_file():
                            entry_type = "file"
                            size = child.stat().st_size
                        else:
                            entry_type = "other"
                            size = 0
                entries.append({"path": rel, "type": entry_type, "size_bytes": size})
                if len(entries) > self.session.policy.max_list_entries:
                    raise WorkspaceReadError("directory listing exceeds max_list_entries")
                if recursive and entry_type == "dir":
                    stack.append(child)
        entries.sort(key=lambda item: str(item["path"]).casefold())
        return tuple(entries)

    def search_text(
        self,
        query: object,
        path: object = "",
        *,
        case_sensitive: bool = True,
    ) -> tuple[dict[str, object], ...]:
        if not isinstance(query, str) or not query:
            raise WorkspaceReadError("search query must be a non-empty string")
        logical, candidate = self._candidate(path, allow_root=True)
        if candidate.is_file():
            files = [candidate]
        elif candidate.is_dir():
            files = list(self._search_files(candidate))
        else:
            raise WorkspaceReadError("search path must be a file or directory")
        files = sorted(files, key=lambda value: value.relative_to(self.root).as_posix().casefold())
        matches: list[dict[str, object]] = []
        searched = 0
        needle = query if case_sensitive else query.casefold()
        for file_path in files:
            rel = file_path.relative_to(self.root).as_posix()
            try:
                self._candidate(rel)
            except (WorkspaceScopeError, WorkspacePathError):
                continue
            searched += 1
            if searched > self.session.policy.max_search_files:
                raise WorkspaceReadError("search exceeds max_search_files")
            if file_path.stat().st_size > self.session.policy.max_read_bytes:
                continue
            try:
                content = file_path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for line_number, line in enumerate(content.splitlines(), 1):
                haystack = line if case_sensitive else line.casefold()
                start = 0
                while True:
                    index = haystack.find(needle, start)
                    if index < 0:
                        break
                    matches.append({
                        "path": rel,
                        "line": line_number,
                        "column": index + 1,
                        "text": line,
                    })
                    if len(matches) >= self.session.policy.max_search_matches:
                        return tuple(matches)
                    start = index + max(1, len(needle))
        return tuple(matches)


    def _search_files(self, root: Path):
        stack = [root]
        while stack:
            directory = stack.pop()
            children = sorted(directory.iterdir(), key=lambda item: item.name.casefold(), reverse=True)
            for child in children:
                rel = child.relative_to(self.root).as_posix()
                try:
                    self._logical_scope(rel, mutation=False)
                except WorkspaceScopeError:
                    continue
                if child.is_symlink():
                    continue
                try:
                    self._canonical_scope(child.resolve(strict=True), mutation=False)
                except (WorkspaceScopeError, WorkspacePathError):
                    continue
                if child.is_dir():
                    stack.append(child)
                elif child.is_file():
                    yield child

    def write_text(self, path: object, content: object, *, expected_sha256: object = None) -> tuple[str, str]:
        if not isinstance(content, str):
            raise WorkspaceMutationError("content must be a string")
        if len(content.encode("utf-8")) > self.session.policy.max_write_bytes:
            raise WorkspaceMutationError("content exceeds max_write_bytes")
        logical, candidate = self._candidate(path, mutation=True)
        exists = candidate.exists()
        if exists and not candidate.is_file():
            raise WorkspaceMutationError("write target must be a regular file")
        if exists:
            if not isinstance(expected_sha256, str) or len(expected_sha256) != 64:
                raise WorkspaceConflictError("overwriting an existing file requires expected_sha256")
            try:
                current = candidate.read_text(encoding="utf-8")
            except UnicodeDecodeError as exc:
                raise WorkspaceMutationError("existing file is not valid UTF-8") from exc
            if content_sha256(current) != expected_sha256.lower():
                raise WorkspaceConflictError("expected_sha256 does not match current file")
        elif expected_sha256 is not None:
            raise WorkspaceConflictError("expected_sha256 must be omitted when creating a new file")
        candidate.parent.mkdir(parents=False, exist_ok=True)
        self._atomic_write(candidate, content)
        return logical, content_sha256(content)

    def replace_text(
        self,
        path: object,
        old_text: object,
        new_text: object,
        *,
        expected_sha256: object,
        expected_occurrences: object = 1,
    ) -> tuple[str, str]:
        if not isinstance(old_text, str) or not old_text:
            raise WorkspaceMutationError("old_text must be a non-empty string")
        if not isinstance(new_text, str):
            raise WorkspaceMutationError("new_text must be a string")
        if not isinstance(expected_occurrences, int) or isinstance(expected_occurrences, bool) or expected_occurrences <= 0:
            raise WorkspaceMutationError("expected_occurrences must be an integer > 0")
        logical, candidate = self._candidate(path, mutation=True)
        if not candidate.is_file():
            raise WorkspaceMutationError("replace target must be an existing regular file")
        try:
            current = candidate.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise WorkspaceMutationError("replace target is not valid UTF-8") from exc
        if not isinstance(expected_sha256, str) or content_sha256(current) != expected_sha256.lower():
            raise WorkspaceConflictError("expected_sha256 does not match current file")
        occurrences = current.count(old_text)
        if occurrences != expected_occurrences:
            raise WorkspaceConflictError(
                f"old_text occurrence count mismatch: expected {expected_occurrences}, observed {occurrences}"
            )
        updated = current.replace(old_text, new_text)
        if len(updated.encode("utf-8")) > self.session.policy.max_write_bytes:
            raise WorkspaceMutationError("updated content exceeds max_write_bytes")
        self._atomic_write(candidate, updated)
        return logical, content_sha256(updated)

    def delete_file(self, path: object, *, expected_sha256: object) -> str:
        logical, candidate = self._candidate(path, mutation=True)
        if not candidate.is_file():
            raise WorkspaceMutationError("delete target must be an existing regular file")
        try:
            current = candidate.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise WorkspaceMutationError("delete target is not valid UTF-8") from exc
        if not isinstance(expected_sha256, str) or content_sha256(current) != expected_sha256.lower():
            raise WorkspaceConflictError("expected_sha256 does not match current file")
        candidate.unlink()
        return logical

    def _atomic_write(self, path: Path, content: str) -> None:
        # On Windows, tempfile.mkstemp can spin inside _mkstemp_inner when
        # dir is addressed through a junction/reparse-point path. Perform
        # atomic I/O through the already-authorized canonical parent instead.
        canonical_parent = path.parent.resolve(strict=True)
        self._require_contained(canonical_parent)

        if path.exists() or path.is_symlink():
            canonical_target = path.resolve(strict=True)
            self._require_contained(canonical_target)
            if path.is_symlink():
                raise WorkspaceMutationError("mutating symlink paths is forbidden")
        else:
            canonical_target = canonical_parent / path.name
            self._require_contained(canonical_target)

        fd, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=str(canonical_parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, canonical_target)
        except Exception:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
            raise

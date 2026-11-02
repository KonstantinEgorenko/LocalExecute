from __future__ import annotations

import os
from pathlib import Path

import pytest

from harness.workspace import (
    Workspace,
    WorkspacePathError,
    WorkspacePolicy,
    WorkspaceReadError,
    WorkspaceScopeError,
    WorkspaceSession,
    normalize_relative_path,
)


def session(root: Path, **policy_kwargs) -> WorkspaceSession:
    return WorkspaceSession("s", "r", "t", str(root), WorkspacePolicy(**policy_kwargs))


@pytest.mark.parametrize("value", ["../x", "a/../x", "/tmp/x", "C:/temp/x", "a/./b"])
def test_normalize_relative_path_rejects_escape_and_dot_components(value):
    with pytest.raises(WorkspacePathError):
        normalize_relative_path(value)


def test_normalize_relative_path_accepts_windows_separators():
    assert normalize_relative_path(r"harness\router\router.py") == "harness/router/router.py"


def test_allowed_and_forbidden_scope_are_fail_closed(tmp_path):
    (tmp_path / "src").mkdir(); (tmp_path / "src" / "a.py").write_text("ok", encoding="utf-8")
    (tmp_path / "secret").mkdir(); (tmp_path / "secret" / "x.txt").write_text("no", encoding="utf-8")
    ws = Workspace(session(tmp_path, allowed_paths=("src/",), forbidden_paths=("secret/", ".git")))
    assert ws.read_text("src/a.py")[1] == "ok"
    with pytest.raises(WorkspaceScopeError):
        ws.read_text("secret/x.txt")
    with pytest.raises(WorkspaceScopeError):
        ws.read_text("README.md")


def test_listing_is_deterministic_and_does_not_traverse_symlinks(tmp_path):
    (tmp_path / "b.txt").write_text("b", encoding="utf-8")
    (tmp_path / "a").mkdir(); (tmp_path / "a" / "c.txt").write_text("c", encoding="utf-8")
    ws = Workspace(session(tmp_path))
    paths = [item["path"] for item in ws.list_files()]
    assert paths == ["a", "a/c.txt", "b.txt"]


def test_external_symlink_read_is_rejected(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable on this platform")
    ws = Workspace(session(tmp_path))
    with pytest.raises(WorkspacePathError, match="escapes"):
        ws.read_text("link.txt")


def test_read_rejects_non_utf8_and_oversize(tmp_path):
    (tmp_path / "bad.bin").write_bytes(b"\xff\xfe")
    (tmp_path / "large.txt").write_text("12345", encoding="utf-8")
    ws = Workspace(session(tmp_path, max_read_bytes=4))
    with pytest.raises(WorkspaceReadError):
        ws.read_text("bad.bin")
    with pytest.raises(WorkspaceReadError, match="max_read_bytes"):
        ws.read_text("large.txt")


def test_search_does_not_traverse_external_symlink_directory(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside-dir"
    outside.mkdir(exist_ok=True)
    (outside / "secret.txt").write_text("needle", encoding="utf-8")
    link = tmp_path / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlink creation unavailable on this platform")
    ws = Workspace(session(tmp_path))
    assert ws.search_text("needle") == ()
    entries = {item["path"]: item["type"] for item in ws.list_files()}
    assert entries["linked"] in {"symlink", "external-link"}

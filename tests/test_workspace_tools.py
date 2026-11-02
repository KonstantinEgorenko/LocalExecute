from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from harness.workspace import (
    ToolName,
    ToolRequest,
    ToolRequestError,
    ToolStatus,
    WorkspaceAccess,
    WorkspacePolicy,
    WorkspaceSession,
    WorkspaceToolDispatcher,
    content_sha256,
)


def make_session(root: Path, **kwargs) -> WorkspaceSession:
    return WorkspaceSession("s", "run", "todo", str(root), WorkspacePolicy(**kwargs))


def call(dispatcher, session, index, tool, args):
    return dispatcher.execute(session, ToolRequest(f"c{index}", "run", "todo", tool, args))


def test_read_list_search_dispatch(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "b.py").write_text("Alpha\nBeta\n", encoding="utf-8")
    (tmp_path / "src" / "a.py").write_text("alpha alpha\n", encoding="utf-8")
    d = WorkspaceToolDispatcher(); s = make_session(tmp_path, allowed_paths=("src/",))
    listed = call(d, s, 1, ToolName.LIST_FILES, {"path": "src", "recursive": True})
    assert listed.status is ToolStatus.SUCCESS
    assert [x["path"] for x in json.loads(listed.output)] == ["src/a.py", "src/b.py"]
    read = call(d, s, 2, ToolName.READ_TEXT, {"path": "src/a.py"})
    assert read.output == "alpha alpha\n" and read.metadata["sha256"] == content_sha256(read.output)
    search = call(d, s, 3, ToolName.SEARCH_TEXT, {"path": "src", "query": "alpha", "case_sensitive": False})
    assert search.status is ToolStatus.SUCCESS and search.metadata["match_count"] == 3


def test_dispatcher_identity_mismatch_is_programming_error(tmp_path):
    d = WorkspaceToolDispatcher(); s = make_session(tmp_path)
    with pytest.raises(ToolRequestError, match="identity"):
        d.execute(s, ToolRequest("c", "other", "todo", ToolName.LIST_FILES, {}))


def test_path_escape_and_forbidden_path_become_structured_denial_or_invalid(tmp_path):
    (tmp_path / ".git").mkdir(); (tmp_path / ".git" / "config").write_text("x", encoding="utf-8")
    d = WorkspaceToolDispatcher(); s = make_session(tmp_path)
    denied = call(d, s, 1, ToolName.READ_TEXT, {"path": ".git/config"})
    invalid = call(d, s, 2, ToolName.READ_TEXT, {"path": "../outside"})
    assert denied.status is ToolStatus.DENIED
    assert invalid.status is ToolStatus.INVALID


def test_create_overwrite_replace_and_delete_with_sha_guards(tmp_path):
    (tmp_path / "src").mkdir()
    d = WorkspaceToolDispatcher(); s = make_session(tmp_path, allowed_paths=("src/",))
    created = call(d, s, 1, ToolName.WRITE_TEXT, {"path": "src/a.py", "content": "x = 1\n"})
    assert created.status is ToolStatus.SUCCESS and created.changed_paths == ("src/a.py",)
    digest1 = created.metadata["sha256"]
    conflict = call(d, s, 2, ToolName.WRITE_TEXT, {"path": "src/a.py", "content": "x=2\n"})
    assert conflict.status is ToolStatus.CONFLICT
    replaced = call(d, s, 3, ToolName.REPLACE_TEXT, {
        "path": "src/a.py", "old_text": "1", "new_text": "2", "expected_sha256": digest1,
    })
    assert replaced.status is ToolStatus.SUCCESS
    digest2 = replaced.metadata["sha256"]
    stale = call(d, s, 4, ToolName.DELETE_FILE, {"path": "src/a.py", "expected_sha256": digest1})
    assert stale.status is ToolStatus.CONFLICT
    deleted = call(d, s, 5, ToolName.DELETE_FILE, {"path": "src/a.py", "expected_sha256": digest2})
    assert deleted.status is ToolStatus.SUCCESS and not (tmp_path / "src" / "a.py").exists()


def test_replace_requires_exact_occurrence_count(tmp_path):
    (tmp_path / "a.txt").write_text("x x", encoding="utf-8")
    d = WorkspaceToolDispatcher(); s = make_session(tmp_path)
    digest = content_sha256("x x")
    result = call(d, s, 1, ToolName.REPLACE_TEXT, {
        "path": "a.txt", "old_text": "x", "new_text": "y", "expected_sha256": digest,
    })
    assert result.status is ToolStatus.CONFLICT
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "x x"


def test_read_only_and_protected_paths_deny_mutation(tmp_path):
    (tmp_path / "src").mkdir(); (tmp_path / "src" / "a.py").write_text("x", encoding="utf-8")
    d = WorkspaceToolDispatcher()
    read_only = make_session(tmp_path, access=WorkspaceAccess.READ_ONLY)
    denied = call(d, read_only, 1, ToolName.WRITE_TEXT, {
        "path": "src/a.py", "content": "y", "expected_sha256": content_sha256("x")
    })
    assert denied.status is ToolStatus.DENIED
    protected = make_session(tmp_path, protected_paths=("src/a.py",))
    denied2 = call(d, protected, 2, ToolName.DELETE_FILE, {
        "path": "src/a.py", "expected_sha256": content_sha256("x")
    })
    assert denied2.status is ToolStatus.DENIED


def test_command_is_disabled_without_allowlist(tmp_path):
    d = WorkspaceToolDispatcher(); s = make_session(tmp_path)
    result = call(d, s, 1, ToolName.RUN_COMMAND, {"argv": [Path(sys.executable).name, "-c", "print('x')"]})
    assert result.status is ToolStatus.DENIED


def test_allowlisted_command_runs_without_shell_and_reports_returncode(tmp_path):
    name = Path(sys.executable).name
    prefix = ((name, "-c"),)
    d = WorkspaceToolDispatcher(); s = make_session(tmp_path, command_prefixes=prefix)
    ok = call(d, s, 1, ToolName.RUN_COMMAND, {"argv": [name, "-c", "print('ok')"]})
    assert ok.status is ToolStatus.SUCCESS and "ok" in ok.output and ok.metadata["returncode"] == 0
    bad = call(d, s, 2, ToolName.RUN_COMMAND, {"argv": [name, "-c", "import sys;sys.exit(3)"]})
    assert bad.status is ToolStatus.ERROR and bad.metadata["returncode"] == 3


def test_command_output_is_bounded(tmp_path):
    name = Path(sys.executable).name
    d = WorkspaceToolDispatcher(); s = make_session(
        tmp_path, command_prefixes=((name, "-c"),), max_command_output_bytes=16
    )
    result = call(d, s, 1, ToolName.RUN_COMMAND, {"argv": [name, "-c", "print('x'*100)"]})
    assert result.status is ToolStatus.SUCCESS
    assert result.metadata["truncated"] is True and result.metadata["output_bytes"] == 16


def test_malformed_tool_arguments_return_invalid(tmp_path):
    d = WorkspaceToolDispatcher(); s = make_session(tmp_path)
    result = call(d, s, 1, ToolName.READ_TEXT, {"path": 123})
    assert result.status is ToolStatus.INVALID


def test_overwrite_non_utf8_file_returns_invalid_without_mutation(tmp_path):
    (tmp_path / "bad.bin").write_bytes(b"\xff\xfe")
    d = WorkspaceToolDispatcher(); s = make_session(tmp_path)
    result = call(d, s, 1, ToolName.WRITE_TEXT, {
        "path": "bad.bin", "content": "safe", "expected_sha256": "0" * 64,
    })
    assert result.status is ToolStatus.INVALID
    assert (tmp_path / "bad.bin").read_bytes() == b"\xff\xfe"

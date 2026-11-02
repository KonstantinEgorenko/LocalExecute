"""Internal directory aliases must not change workspace authority."""
import os
import subprocess

import pytest

from harness.workspace import (
    ToolName, ToolRequest, ToolStatus, WorkspacePolicy, WorkspaceSession,
    WorkspaceToolDispatcher, content_sha256,
)


def link_directory(link, target):
    if os.name == "nt":
        # Environment arguments avoid PowerShell quoting/path interpolation.
        subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "New-Item -ItemType Junction -Path $env:SWARM_TEST_LINK -Target $env:SWARM_TEST_TARGET"],
            env={**os.environ, "SWARM_TEST_LINK": str(link), "SWARM_TEST_TARGET": str(target)},
            check=True, capture_output=True,
        )
    else:
        link.symlink_to(target, target_is_directory=True)


@pytest.fixture
def alias_workspace(tmp_path):
    for name in (".git", "state", "allowed"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "record.txt").write_text("needle", encoding="utf-8")
        link_directory(tmp_path / ("alias-" + name), tmp_path / name)
    return tmp_path


def invoke(root, tool, path, **policy):
    args = {"path": path}
    if tool == ToolName.WRITE_TEXT:
        args.update(content="changed")
        if not path.endswith("new.txt"):
            args["expected_sha256"] = content_sha256("needle")
    elif tool == ToolName.REPLACE_TEXT:
        args.update(old_text="needle", new_text="changed", expected_sha256=content_sha256("needle"))
    elif tool == ToolName.DELETE_FILE:
        args["expected_sha256"] = content_sha256("needle")
    elif tool == ToolName.SEARCH_TEXT:
        args["query"] = "needle"
    return WorkspaceToolDispatcher().execute(
        WorkspaceSession("s", "r", "t", str(root), WorkspacePolicy(**policy)),
        ToolRequest("c", "r", "t", tool, args),
    )


@pytest.mark.parametrize("tool", [ToolName.READ_TEXT, ToolName.WRITE_TEXT,
    ToolName.REPLACE_TEXT, ToolName.DELETE_FILE, ToolName.LIST_FILES, ToolName.SEARCH_TEXT])
def test_forbidden_target_denied_through_internal_alias(alias_workspace, tool):
    path = "alias-.git" if tool in (ToolName.LIST_FILES, ToolName.SEARCH_TEXT) else "alias-.git/record.txt"
    assert invoke(alias_workspace, tool, path).status is ToolStatus.DENIED
    assert (alias_workspace / ".git/record.txt").read_text() == "needle"


@pytest.mark.parametrize("tool", [ToolName.WRITE_TEXT, ToolName.REPLACE_TEXT, ToolName.DELETE_FILE])
def test_protected_target_denied_through_internal_alias(alias_workspace, tool):
    assert invoke(alias_workspace, tool, "alias-state/record.txt").status is ToolStatus.DENIED
    assert (alias_workspace / "state/record.txt").read_text() == "needle"


@pytest.mark.parametrize("parent", ["alias-.git", "alias-state"])
def test_create_authorizes_canonical_parent(alias_workspace, parent):
    assert invoke(alias_workspace, ToolName.WRITE_TEXT, parent + "/new.txt").status is ToolStatus.DENIED
    assert not (alias_workspace / parent / "new.txt").exists()


@pytest.mark.parametrize("tool", [ToolName.READ_TEXT, ToolName.WRITE_TEXT,
    ToolName.REPLACE_TEXT, ToolName.DELETE_FILE, ToolName.LIST_FILES, ToolName.SEARCH_TEXT])
def test_allowed_internal_alias_still_works(alias_workspace, tool):
    path = "alias-allowed" if tool in (ToolName.LIST_FILES, ToolName.SEARCH_TEXT) else "alias-allowed/record.txt"
    assert invoke(alias_workspace, tool, path).status is ToolStatus.SUCCESS


def test_allowed_create_and_both_names_require_authority(alias_workspace):
    assert invoke(alias_workspace, ToolName.WRITE_TEXT, "alias-allowed/new.txt").status is ToolStatus.SUCCESS
    assert (alias_workspace / "allowed/new.txt").read_text() == "changed"
    for allowed in (("alias-allowed",), ("allowed",)):
        assert invoke(alias_workspace, ToolName.READ_TEXT, "alias-allowed/record.txt",
                      allowed_paths=allowed).status is ToolStatus.DENIED
    assert invoke(alias_workspace, ToolName.READ_TEXT, "alias-allowed/record.txt",
                  forbidden_paths=("alias-allowed",)).status is ToolStatus.DENIED
    assert invoke(alias_workspace, ToolName.WRITE_TEXT, "alias-allowed/record.txt",
                  protected_paths=("alias-allowed",)).status is ToolStatus.DENIED


@pytest.mark.parametrize("tool", [ToolName.LIST_FILES, ToolName.SEARCH_TEXT])
def test_recursive_enumeration_hides_forbidden_alias(alias_workspace, tool):
    result = invoke(alias_workspace, tool, "")
    assert result.status is ToolStatus.SUCCESS
    assert "alias-.git" not in result.output
    assert "record.txt" in result.output

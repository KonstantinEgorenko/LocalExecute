'Bridge bounded workspace/tool output into the context management ArtifactStore.'
from __future__ import annotations

from pathlib import PurePosixPath

from ..context_manager.models import Artifact, ArtifactKind, ArtifactOrigin, ArtifactRef
from ..context_manager.store import ArtifactStore
from .models import ToolResult, WorkspaceSession
from .workspace import Workspace


def _infer_kind(path: str) -> ArtifactKind:
    pure = PurePosixPath(path)
    suffix = pure.suffix.lower()
    if path.startswith("tests/") or pure.name.startswith("test_"):
        return ArtifactKind.TEST_FILE
    if path.startswith("config/") or suffix in {".yaml", ".yml", ".toml", ".ini"}:
        return ArtifactKind.CONFIG
    if path.startswith("docs/") or suffix in {".md", ".rst", ".txt"}:
        return ArtifactKind.DOCUMENTATION
    return ArtifactKind.SOURCE_FILE


class WorkspaceArtifactBridge:
    def __init__(self, artifact_store: ArtifactStore) -> None:
        self.artifact_store = artifact_store

    def snapshot_file(
        self,
        session: WorkspaceSession,
        *,
        path: str,
        artifact_id: str,
        kind: ArtifactKind | None = None,
        created_at: str = "workspace",
    ) -> ArtifactRef:
        workspace = Workspace(session)
        logical, content = workspace.read_text(path)
        matching_scopes = tuple(
            value
            for value in session.policy.allowed_paths
            if logical == value.rstrip("/\\") or logical.startswith(value.rstrip("/\\") + "/")
        )
        scope_tags = matching_scopes or (logical,)
        artifact = Artifact.from_text(
            artifact_id=artifact_id,
            kind=_infer_kind(logical) if kind is None else kind,
            origin=ArtifactOrigin.REPOSITORY,
            content=content,
            media_type="text/plain; charset=utf-8",
            source=f"workspace:{logical}",
            created_at=created_at,
            scope_tags=scope_tags,
            metadata={"path": logical, "session_id": session.session_id},
        )
        self.artifact_store.create_artifact(artifact)
        return artifact.ref

    def capture_tool_result(
        self,
        result: ToolResult,
        *,
        artifact_id: str | None = None,
        created_at: str = "tool",
    ) -> ArtifactRef:
        artifact = Artifact.from_text(
            artifact_id=artifact_id or f"tool:{result.call_id}",
            kind=ArtifactKind.LOG,
            origin=ArtifactOrigin.TOOL,
            content=result.output,
            media_type="text/plain; charset=utf-8",
            source=f"tool:{result.tool.value}",
            created_at=created_at,
            scope_tags=result.changed_paths,
            metadata={
                "call_id": result.call_id,
                "run_id": result.run_id,
                "todo_id": result.todo_id,
                "tool": result.tool.value,
                "status": result.status.value,
            },
        )
        self.artifact_store.create_artifact(artifact)
        return artifact.ref

'Repository intelligence -> ArtifactStore -> context management Context Manager bridge.'
from __future__ import annotations

import hashlib
from pathlib import Path

from ..context_manager import Artifact, ArtifactKind, ArtifactOrigin, ArtifactRef, ContextBudget, ContextBundle, ControlPlaneContextResolver
from ..context_manager.store import ArtifactStore
from ..control_plane import WorkContract
from ..worktree.manager import GitWorktreeManager
from .errors import RepositoryIntegrityError, RepositorySnapshotMismatchError
from .models import RepositoryFileKind, RepositoryIndex, RepositoryIntelligencePolicy, RepositoryRetrievalResult, canonical_json


def _scope_tags(path: str) -> tuple[str, ...]:
    parts = path.split("/")
    scopes = ["/".join(parts[:index]) for index in range(1, len(parts) + 1)]
    scopes.append("repository_intelligence")
    return tuple(dict.fromkeys(scopes))


def _artifact_kind(kind: RepositoryFileKind) -> ArtifactKind:
    return {
        RepositoryFileKind.SOURCE: ArtifactKind.SOURCE_FILE,
        RepositoryFileKind.TEST: ArtifactKind.TEST_FILE,
        RepositoryFileKind.CONFIG: ArtifactKind.CONFIG,
        RepositoryFileKind.DOCUMENTATION: ArtifactKind.DOCUMENTATION,
        RepositoryFileKind.OTHER: ArtifactKind.OTHER,
    }[kind]


class RepositoryIndexPublisher:
    'Persist a deterministic RepositoryIndex as a content-addressed context management Artifact.'
    def __init__(self, artifact_store: ArtifactStore) -> None: self.artifact_store = artifact_store

    @staticmethod
    def artifact_id(index: RepositoryIndex) -> str: return f"{index.index_id}:artifact"

    def publish(self, index: RepositoryIndex) -> ArtifactRef:
        artifact_id = self.artifact_id(index)
        content = canonical_json(index.to_dict())
        artifact = Artifact.from_text(
            artifact_id=artifact_id,
            kind=ArtifactKind.OTHER,
            origin=ArtifactOrigin.TOOL,
            content=content,
            media_type="application/json",
            source="RepositoryIntelligence",
            created_at="1970-01-01T00:00:00+00:00",
            metadata={"index_id": index.index_id, "lease_id": index.lease_id, "head_commit": index.head_commit, "diff_sha256": index.diff_sha256, "file_count": len(index.files)},
        )
        if self.artifact_store.has_artifact(artifact_id):
            existing = self.artifact_store.get_artifact(artifact_id)
            if existing.content != artifact.content or existing.sha256 != artifact.sha256:
                raise RepositoryIntegrityError("repository index artifact identity conflict")
            return existing.ref
        self.artifact_store.create_artifact(artifact)
        return artifact.ref


class RepositoryContextBridge:
    def __init__(self, *, manager: GitWorktreeManager, artifact_store: ArtifactStore, policy: RepositoryIntelligencePolicy | None = None) -> None:
        self.manager = manager; self.artifact_store = artifact_store; self.policy = policy or RepositoryIntelligencePolicy()

    def assert_current(self, index: RepositoryIndex) -> None:
        lease = self.manager.get_active(index.lease_id)
        if str(Path(lease.worktree_root)) != str(Path(index.repo_root)):
            raise RepositorySnapshotMismatchError("repository index worktree root no longer matches active lease")
        snapshot = self.manager.snapshot(index.lease_id)
        if snapshot.head_commit != index.head_commit or snapshot.diff_sha256 != index.diff_sha256 or snapshot.dirty_paths != index.dirty_paths:
            raise RepositorySnapshotMismatchError("repository index is stale for current candidate snapshot")

    def materialize(self, index: RepositoryIndex, retrieval: RepositoryRetrievalResult) -> tuple[ArtifactRef, ...]:
        if retrieval.index_id != index.index_id:
            raise RepositorySnapshotMismatchError("retrieval result does not bind to repository index")
        self.assert_current(index)
        root = Path(index.repo_root)
        file_map = index.file_map
        refs: list[ArtifactRef] = []
        before = self.manager.snapshot(index.lease_id)
        for hit in retrieval.hits:
            record = file_map.get(hit.path)
            if record is None or record.sha256 != hit.sha256 or record.size_bytes != hit.size_bytes:
                raise RepositoryIntegrityError("retrieval hit does not match indexed file metadata")
            path = root / Path(hit.path)
            if path.is_symlink() or not path.is_file():
                raise RepositorySnapshotMismatchError(f"retrieved file is missing or symlink: {hit.path}")
            try: payload = path.read_bytes()
            except OSError as exc: raise RepositoryIntegrityError(f"cannot read retrieved file {hit.path}: {exc}") from exc
            if len(payload) != record.size_bytes or hashlib.sha256(payload).hexdigest() != record.sha256:
                raise RepositorySnapshotMismatchError(f"retrieved file changed after indexing: {hit.path}")
            try: content = payload.decode("utf-8")
            except UnicodeDecodeError as exc: raise RepositoryIntegrityError(f"retrieved file is no longer UTF-8: {hit.path}") from exc
            path_hash = hashlib.sha256(hit.path.encode("utf-8")).hexdigest()[:16]
            artifact_id = f"repo-context:{index.index_id}:{path_hash}:{record.sha256[:16]}"
            artifact = Artifact.from_text(
                artifact_id=artifact_id,
                kind=_artifact_kind(record.kind),
                origin=ArtifactOrigin.REPOSITORY,
                content=content,
                media_type="text/plain",
                source=hit.path,
                created_at="1970-01-01T00:00:00+00:00",
                scope_tags=_scope_tags(hit.path),
                metadata={"index_id": index.index_id, "retrieval_id": retrieval.retrieval_id, "score": hit.score, "reasons": list(hit.reasons), "sha256": record.sha256},
            )
            if self.artifact_store.has_artifact(artifact_id):
                existing = self.artifact_store.get_artifact(artifact_id)
                if existing.content != content or existing.sha256 != artifact.sha256:
                    raise RepositoryIntegrityError("repository context artifact identity conflict")
                refs.append(existing.ref)
            else:
                refs.append(self.artifact_store.create_artifact(artifact).ref)
        after = self.manager.snapshot(index.lease_id)
        if (before.head_commit, before.diff_sha256, before.dirty_paths) != (after.head_commit, after.diff_sha256, after.dirty_paths):
            raise RepositorySnapshotMismatchError("candidate changed while repository context was materialized")
        return tuple(refs)

    def assemble_context(
        self,
        *,
        resolver: ControlPlaneContextResolver,
        run_id: str,
        contract: WorkContract,
        budget: ContextBudget,
        index: RepositoryIndex,
        retrieval: RepositoryRetrievalResult,
        dependency_run_ids: dict[str, str] | None = None,
        required_artifacts: tuple[ArtifactRef, ...] = (),
        optional_artifacts: tuple[ArtifactRef, ...] = (),
    ) -> ContextBundle:
        repo_refs = self.materialize(index, retrieval)
        merged_optional = tuple(optional_artifacts) + repo_refs
        return resolver.assemble(
            run_id, contract, budget,
            dependency_run_ids=dependency_run_ids,
            required_artifacts=required_artifacts,
            optional_artifacts=merged_optional,
            request_id=f"{run_id}:{contract.todo_id}:repo-context:{retrieval.retrieval_id}",
        )

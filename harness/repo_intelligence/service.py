'repository intelligence orchestration facade: index, persist, retrieve, and bind to current candidate.'
from __future__ import annotations

from ..context_manager.store import ArtifactStore
from ..worktree.manager import GitWorktreeManager
from .errors import RepositorySnapshotMismatchError
from .integration import RepositoryIndexPublisher
from .models import RepositoryIndex, RepositoryIntelligencePolicy, RepositoryRetrievalResult
from .retrieval import RepositoryRetriever
from .scanner import RepositoryIndexer
from .store import RepositoryIndexStore


class RepositoryIntelligenceService:
    def __init__(self, *, manager: GitWorktreeManager, index_store: RepositoryIndexStore, artifact_store: ArtifactStore, policy: RepositoryIntelligencePolicy | None = None) -> None:
        self.manager = manager
        self.policy = policy or RepositoryIntelligencePolicy()
        self.index_store = index_store
        self.artifact_store = artifact_store
        self.indexer = RepositoryIndexer(manager=manager, policy=self.policy)
        self.retriever = RepositoryRetriever(policy=self.policy)
        self.publisher = RepositoryIndexPublisher(artifact_store)

    def build_index(self, lease_id: str) -> RepositoryIndex:
        index = self.indexer.build(lease_id)
        if self.index_store.has(index.index_id):
            existing = self.index_store.get(index.index_id)
            if existing != index:
                raise RepositorySnapshotMismatchError("persisted repository index conflicts with current deterministic rebuild")
            index = existing
        else:
            self.index_store.put(index)
        self.publisher.publish(index)
        return index

    def retrieve(self, index: RepositoryIndex, query: str, *, explicit_paths: tuple[str, ...] = ()) -> RepositoryRetrievalResult:
        self.assert_current(index)
        return self.retriever.retrieve(index, query, explicit_paths=explicit_paths)

    def assert_current(self, index: RepositoryIndex) -> None:
        snapshot = self.manager.snapshot(index.lease_id)
        if snapshot.head_commit != index.head_commit or snapshot.diff_sha256 != index.diff_sha256 or snapshot.dirty_paths != index.dirty_paths:
            raise RepositorySnapshotMismatchError("repository index is stale for current candidate snapshot")

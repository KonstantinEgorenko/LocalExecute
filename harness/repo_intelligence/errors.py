'repository intelligence repository intelligence errors.'
from __future__ import annotations


class RepositoryIntelligenceError(RuntimeError):
    'Base repository intelligence error.'


class RepositoryPolicyError(RepositoryIntelligenceError):
    """Harness-owned repository-intelligence policy is invalid or violated."""


class RepositoryBudgetError(RepositoryIntelligenceError):
    """A deterministic repository scan/retrieval budget was exceeded."""


class RepositoryIntegrityError(RepositoryIntelligenceError):
    """Persisted or live repository-intelligence state failed integrity checks."""


class RepositorySnapshotMismatchError(RepositoryIntegrityError):
    """A repository index/retrieval result is stale for the current candidate."""


class RepositoryIndexNotFoundError(RepositoryIntelligenceError):
    """Requested repository index does not exist."""

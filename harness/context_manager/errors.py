"""Errors for deterministic context and artifact handling."""


class ContextManagerError(RuntimeError):
    'Base error for context management context/artifact operations.'


class ArtifactIntegrityError(ContextManagerError):
    """Artifact content or metadata failed integrity validation."""


class ArtifactStoreError(ContextManagerError):
    """Base error for artifact-store persistence and identity operations."""


class ArtifactNotFoundError(ArtifactStoreError):
    """Requested artifact identity does not exist."""


class ArtifactIdentityError(ArtifactStoreError):
    """Artifact identity violates create-only uniqueness semantics."""


class ArtifactStoreCorruptionError(ArtifactStoreError):
    """Durable artifact store cannot be hydrated safely."""


class ContextBudgetError(ContextManagerError):
    """A context request cannot be satisfied within its hard budget."""


class ContextResolutionError(ContextManagerError):
    """Required context or artifact provenance cannot be resolved safely."""

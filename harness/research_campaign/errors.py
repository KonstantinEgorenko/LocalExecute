'Errors raised by the research campaign ResearchExperimentStore.'


class ResearchStoreError(RuntimeError):
    """Base durable research-store error."""


class ResearchStoreIntegrityError(ResearchStoreError):
    """Persisted research state is corrupt or violates an immutable identity."""


class ResearchStoreConflictError(ResearchStoreError):
    """A caller attempted to rewrite immutable or incompatible research state."""


class ResearchStoreNotFoundError(ResearchStoreError):
    """Requested research state does not exist."""


class ResearchStoreSchemaError(ResearchStoreError):
    """SQLite schema version is unsupported or could not be initialized."""

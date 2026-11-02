'scheduler deterministic scheduler errors.'

class SchedulerError(RuntimeError):
    """Base scheduler failure."""


class SchedulerPolicyError(SchedulerError):
    """Scheduler policy or DAG invariant was violated."""


class SchedulerConflictError(SchedulerError):
    """A Todo/claim/resource is already owned or conflicts with another active item."""


class SchedulerStateError(SchedulerError):
    """Requested scheduler state transition is invalid."""


class SchedulerStoreError(SchedulerError):
    """Durable scheduler state is unavailable or corrupt."""


class SchedulerSnapshotError(SchedulerError):
    'A repository intelligence working-set prediction is stale or inconsistent.'

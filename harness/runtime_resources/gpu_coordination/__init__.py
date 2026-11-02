'gpu coordination additive machine-wide GPU_HEAVY coordination guard.'
from .guard import install_runtime_guard, runtime_guard_installed
from .lease import GpuHeavyLease, GpuHeavyLeaseError, GpuHeavyLeaseTimeout, GpuHeavyStatus
from .policy import GpuCoordinationPolicy

__all__ = [
    "GpuHeavyLease",
    "GpuHeavyLeaseError",
    "GpuHeavyLeaseTimeout",
    "GpuHeavyStatus",
    "GpuCoordinationPolicy",
    "install_runtime_guard",
    "runtime_guard_installed",
]

Phase20GPolicy = GpuCoordinationPolicy

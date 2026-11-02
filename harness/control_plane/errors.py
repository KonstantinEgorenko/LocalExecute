"""Narrow exceptions for Control Plane storage integrity."""
class RunStoreError(ValueError):
    """Base error for durable Control Plane storage."""
class StoreIntegrityError(RunStoreError):
    """Raised when a store operation violates domain references or identity."""
class StoreCorruptionError(RunStoreError):
    """Raised when persisted Control Plane data is malformed or inconsistent."""
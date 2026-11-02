"""Domain errors for configuration, runtime discovery, and worker execution."""
from __future__ import annotations

from typing import Any


class HarnessError(RuntimeError):
    pass


class ConfigurationError(HarnessError):
    pass


class ServerUnavailableError(HarnessError):
    pass


class RuntimeDiscoveryError(HarnessError):
    pass


class ResourceDiscoveryError(HarnessError):
    pass


class LockOwnershipError(HarnessError):
    pass


class LockBusyError(HarnessError):
    pass


class ExecutionError(HarnessError):
    """Execution failure; post-mutation failures may expose failure_context."""

    def __init__(self, message: str, *, failure_context: Any = None) -> None:
        super().__init__(message)
        self.failure_context = failure_context


class ExecutionServerUnavailableError(ExecutionError):
    pass


class ExecutionHttpError(ExecutionError):
    def __init__(self, message: str, *, failure_context: Any = None,
                 status_code: int | None = None, response_error_kind: str | None = None,
                 protocol_error_reason: str | None = None, response_body_sha256: str | None = None,
                 response_body_bytes: int | None = None, response_body_truncated: bool = False,
                 transport_error: Any = None) -> None:
        super().__init__(message, failure_context=failure_context)
        self.status_code = status_code
        self.response_error_kind = response_error_kind
        self.protocol_error_reason = protocol_error_reason
        self.response_body_sha256 = response_body_sha256
        self.response_body_bytes = response_body_bytes
        self.response_body_truncated = response_body_truncated
        self.transport_error = transport_error


class LifecycleVerificationError(ExecutionError):
    pass


class WorkerGenerationError(ExecutionError):
    pass


class WorkerProtocolError(ExecutionError):
    pass


class StaleRoutingDecisionError(ExecutionError):
    pass

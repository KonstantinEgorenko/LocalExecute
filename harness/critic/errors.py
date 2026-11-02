'critic critic-specific domain errors.'

class CriticError(Exception):
    'Base class for critic critic failures.'


class CriticPolicyError(CriticError):
    pass


class CriticProtocolError(CriticError):
    pass


class CriticConformanceError(CriticProtocolError):
    """Terminal failure of an explicitly bounded alias invocation."""

    def __init__(self, message, diagnostic=None):
        super().__init__(message)
        self.failure_diagnostic = {"category": "CRITIC_EVIDENCE_REFERENCE_CONFORMANCE_FAILURE",
                                   **(diagnostic or {}), "message": message}


class CriticStateError(CriticError):
    pass


class CriticIntegrityError(CriticError):
    pass


class CriticRoutingError(CriticError):
    pass


class CriticExhaustedError(CriticError):
    pass

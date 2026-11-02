"""Fatal evidence gaps must bypass ordinary model/transport retry handlers."""


class SupervisionObservationHalt(BaseException):
    """Abort the supervised invocation; never redispatch to repair telemetry.

    Deliberately outside Exception: production recovery catches Exception for
    model failures. Cleanup handlers may catch BaseException but must re-raise.
    This is not a transport failure or a successful Mission outcome.
    """

    def __init__(self, stage, cause, *, identity=None, response_received=False,
                 response_fingerprint=None, transport_error_type=None):
        self.stage = stage
        self.identity = identity
        self.response_received = response_received
        self.response_fingerprint = response_fingerprint
        self.transport_error_type = transport_error_type
        self.persistence_error_type = type(cause).__name__
        super().__init__(f'SUPERVISION_OBSERVATION_HALT stage={stage} '
            f'response_received={response_received} identity={identity} '
            f'cause={type(cause).__name__}')

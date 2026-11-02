'judge Judge-specific fail-closed exceptions.'

class JudgeError(RuntimeError):
    pass


class JudgeStateError(JudgeError):
    pass


class JudgeIntegrityError(JudgeError):
    pass


class JudgeProtocolError(JudgeError):
    pass


class JudgeExhaustedError(JudgeError):
    def __init__(self, message, *, failure_kind="UNKNOWN", request_id=None, attempts=None):
        super().__init__(message)
        self.failure_kind = failure_kind
        self.request_id = request_id
        self.attempts = attempts

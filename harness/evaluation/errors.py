'evaluation evaluation domain errors.'

class EvaluationError(RuntimeError):
    pass

class EvaluationIntegrityError(EvaluationError):
    pass

class EvaluationStoreError(EvaluationError):
    pass

class BenchmarkExecutionError(EvaluationError):
    pass

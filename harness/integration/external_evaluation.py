"""Optional trusted evaluation boundary; ordinary missions need no private oracle.

The V1 private acceptance contract remains mandatory when declared, including
in original admission evidence. Missing evaluation infrastructure never grants
acceptance. Candidate data cannot select an evaluator implementation.
"""
import importlib
import json

from harness.context_manager.errors import ContextResolutionError
from harness.verification.global_contract import GlobalVerificationError

INDEPENDENT_ACCEPTANCE = 'INDEPENDENT_ACCEPTANCE_V1'


def admitted_independent_requirement(spine, spec):
    if not hasattr(spine, 'artifact_store'):
        return False
    try:
        ref = spine.root_context(spec.goal_id).source
        artifact = spine.artifact_store.get_artifact(ref.artifact_id)
        return INDEPENDENT_ACCEPTANCE in json.loads(artifact.content)['constraints']
    except ContextResolutionError:
        return False
    except Exception:
        # Corrupt admission evidence cannot enable legacy completion.
        return True


def independent_acceptance_required(spine, spec):
    return (INDEPENDENT_ACCEPTANCE in getattr(spec, 'global_constraints', {})
            or admitted_independent_requirement(spine, spec))


def independent_verifier_type():
    """Resolve the trusted legacy V1 backend only for mandatory evaluation.

    Public distributions intentionally omit this benchmark-specific backend.
    Broken dependencies also fail closed; no fallback evaluator is substituted.
    """
    try:
        module = importlib.import_module('harness.integration.independent_acceptance')
    except ImportError as exc:
        raise GlobalVerificationError(
            'INDEPENDENT_EVALUATOR_UNAVAILABLE',
            'The declared mandatory independent acceptance backend is not installed',
        ) from exc
    return module.IndependentAcceptanceVerifier

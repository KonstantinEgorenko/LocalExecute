'verification deterministic verification errors.'
from __future__ import annotations


class VerificationError(RuntimeError):
    """Base verification error."""


class VerificationPolicyError(VerificationError):
    """Trusted verification policy or plan is invalid."""


class VerificationStateError(VerificationError):
    """Run/worktree/artifact state cannot be safely verified."""


class VerificationIntegrityError(VerificationError):
    """Persisted or reconstructed verification data is inconsistent."""

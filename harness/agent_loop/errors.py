'worker agent coding-agent loop errors.'
from __future__ import annotations


class AgentLoopError(RuntimeError):
    """Base error for deterministic coding-agent loop failures."""


class AgentProtocolError(AgentLoopError):
    'Worker response does not match the canonical worker agent action schema.'


class AgentLoopStateError(AgentLoopError):
    """Run/Todo/context/workspace identities are inconsistent."""


class AgentResumeError(AgentLoopError):
    """Durable transcript evidence cannot be reconstructed safely."""

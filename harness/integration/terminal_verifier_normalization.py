"""Deterministic, fail-closed normalization for eligible terminal verifiers.

The Lead schema intentionally has no per-Todo production-gate field.  This
module runs before the integration adapter projects applicable gates onto
Todos, so it may remove only a verifier whose semantics are already guaranteed
by its unique final mutation sink.
"""

from __future__ import annotations

from dataclasses import replace
import re
from typing import Any

from harness.lead.models import CompiledWorkItem

from .plan_feasibility import has_unconditional_mutation_deliverable, is_verification_only_objective


_SPACE_RE = re.compile(r"\s+")
_TOKEN_RE = re.compile(r"[a-z0-9_]+")
_CONFIGURED_GATES_RE = re.compile(r"\b(?:all\s+)?configured\s+(?:verification\s+)?gates?\b", re.IGNORECASE)
_FOLDED_VALIDATION_KEY = "terminal_configured_validation"

_VERIFIER_FRAMING_TOKENS = frozenset({
    "all", "and", "confirm", "configured", "deliverable", "execute", "gates",
    "implementation", "mutation", "on", "pass", "passes", "passing", "run",
    "successfully", "the", "to", "validate", "verification", "verify",
})
_EVIDENCE_FRAMING_TOKENS = frozenset({
    "all", "and", "configured", "four", "from", "gates", "of", "output",
    "pass", "passes", "passed", "passing", "successfully", "the", "verification",
})


def _canonical_objective(value: str) -> str:
    return _SPACE_RE.sub(" ", value).strip().casefold()


def _gate_identifier(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value).casefold()).strip("_")


def _tokens(value: str) -> tuple[str, ...]:
    return tuple(_TOKEN_RE.findall(str(value).casefold().replace("-", "_")))


def _required_gate_set(required_gates: tuple[str, ...]) -> frozenset[str]:
    return frozenset(_gate_identifier(gate) for gate in required_gates if _gate_identifier(gate))


def is_configured_gate_only_verifier(objective: str, required_gates: tuple[str, ...]) -> bool:
    """Recognize only a complete, bounded configured-gate validation leaf."""
    required = _required_gate_set(required_gates)
    if not required or not is_verification_only_objective(objective):
        return False
    canonical = _canonical_objective(objective)
    if not _CONFIGURED_GATES_RE.search(canonical):
        return False
    tokens = _tokens(canonical)
    if required.intersection(tokens) != required:
        return False
    return all(token in required or token in _VERIFIER_FRAMING_TOKENS for token in tokens)


def is_configured_gate_evidence_only(required_evidence: tuple[str, ...], required_gates: tuple[str, ...]) -> bool:
    """Accept only standard or gate-result evidence with no independent output."""
    required = _required_gate_set(required_gates)
    if not required:
        return False
    for evidence in required_evidence:
        canonical = _canonical_objective(str(evidence))
        if not canonical or canonical == "worker_result":
            continue
        tokens = _tokens(canonical)
        mentioned = required.intersection(tokens)
        if "gate" not in tokens and "gates" not in tokens and mentioned != required:
            return False
        if not {"pass", "passes", "passed", "passing", "output"}.intersection(tokens):
            return False
        if mentioned and mentioned != required:
            return False
        if any(token not in required and token not in _EVIDENCE_FRAMING_TOKENS for token in tokens):
            return False
    return True


def _ancestor_ids(by_id: dict[str, CompiledWorkItem], todo_id: str) -> set[str]:
    ancestors: set[str] = set()
    pending = list(by_id[todo_id].todo.dependencies)
    while pending:
        current = pending.pop()
        if current in ancestors:
            continue
        ancestors.add(current)
        pending.extend(by_id[current].todo.dependencies)
    return ancestors


class TerminalVerifierNormalizer:
    """Fold only an unambiguous configured-verification leaf into its final sink."""

    def __init__(self, required_gates: tuple[str, ...]) -> None:
        self.required_gates = tuple(dict.fromkeys(str(gate) for gate in required_gates))

    def _eligible(
        self,
        item: CompiledWorkItem,
        *,
        by_id: dict[str, CompiledWorkItem],
        dependents: dict[str, set[str]],
    ) -> str | None:
        """Return the unique mutation predecessor id only when every proof holds."""
        if not self.required_gates:
            return None
        if not is_verification_only_objective(item.todo.objective):
            return None
        if not is_configured_gate_only_verifier(item.todo.objective, self.required_gates):
            return None
        if dependents[item.todo.todo_id]:
            return None
        if len(item.todo.dependencies) != 1:
            return None

        predecessor_id = item.todo.dependencies[0]
        predecessor = by_id.get(predecessor_id)
        if predecessor is None or not has_unconditional_mutation_deliverable(predecessor.todo.objective):
            return None

        # Scope, ownership, and constraints must be exactly inherited. Verifier
        # evidence may be a narrow restatement of the configured gate results.
        if (
            tuple(item.work_contract.allowed_scope) != tuple(predecessor.work_contract.allowed_scope)
            or tuple(item.work_contract.forbidden_scope) != tuple(predecessor.work_contract.forbidden_scope)
            or dict(item.work_contract.constraints) != dict(predecessor.work_contract.constraints)
        ):
            return None
        if not is_configured_gate_evidence_only(
            tuple(item.work_contract.required_evidence), self.required_gates
        ):
            return None

        # Conservatively treat every other executable Todo as potentially
        # mutating.  This proves that the predecessor is the unique final sink
        # and that its gates observe the cumulative candidate state.
        ancestors = _ancestor_ids(by_id, predecessor_id)
        if any(other_id not in ancestors and other_id != predecessor_id for other_id in by_id if other_id != item.todo.todo_id):
            return None
        return predecessor_id

    def normalize(self, work_items: tuple[Any, ...]) -> tuple[Any, ...]:
        items = tuple(work_items)
        if not self.required_gates or not all(isinstance(item, CompiledWorkItem) for item in items):
            return items
        by_id = {item.todo.todo_id: item for item in items}
        if len(by_id) != len(items):
            return items
        dependents = {todo_id: set() for todo_id in by_id}
        for item in items:
            for dependency in item.todo.dependencies:
                if dependency not in by_id:
                    return items
                dependents[dependency].add(item.todo.todo_id)

        removals: set[str] = set()
        replacements: dict[str, CompiledWorkItem] = {}
        for item in items:
            predecessor_id = self._eligible(item, by_id=by_id, dependents=dependents)
            if predecessor_id is None or predecessor_id in replacements:
                continue
            predecessor = by_id[predecessor_id]
            constraints = dict(predecessor.work_contract.constraints)
            constraints[_FOLDED_VALIDATION_KEY] = {
                "folded_todo_id": item.todo.todo_id,
                "objective": item.todo.objective,
                "configured_gates": list(self.required_gates),
                "verifier_required_evidence": list(item.work_contract.required_evidence),
            }
            replacements[predecessor_id] = replace(
                predecessor,
                work_contract=replace(predecessor.work_contract, constraints=constraints),
            )
            removals.add(item.todo.todo_id)

        if not removals:
            return items
        return tuple(
            replacements.get(item.todo.todo_id, item)
            for item in items
            if item.todo.todo_id not in removals
        )


__all__ = [
    "TerminalVerifierNormalizer",
    "is_configured_gate_evidence_only",
    "is_configured_gate_only_verifier",
]

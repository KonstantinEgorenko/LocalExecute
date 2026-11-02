"Deterministic plan-shape normalization for bounded production missions.\n\nThis module is intentionally narrow.  It does not synthesize arbitrary plans,\nchange mission objectives, or weaken :mod:`plan_feasibility`.  It only closes a\nspecific refinement nondeterminism discovered by research campaign R6 live acceptance:\na Lead may repeat a standalone reconnaissance Todo after the feasibility gate\nhas already told it to inline discovery into one explicitly requested\nimplementation Todo.\n\nWhen the Mission/Lead request carries the machine-readable\n``PLAN_SHAPE_SINGLE_IMPLEMENTATION_TODO`` contract and the request is already a\nreconnaissance refinement attempt, the normalizer may:\n\n* fold an unambiguous reconnaissance-only Todo into its unique mutation\n  consumer; or\n* when the repeated candidate contains exactly one reconnaissance-only Todo,\n  replace that Todo's objective with the *existing root request objective* if\n  that root objective itself exposes an unconditional mutation deliverable.\n\nNo new semantic work is invented.  Ambiguous shapes remain unchanged and are\nrejected by the normal feasibility gate.\n"

from __future__ import annotations

from dataclasses import replace
import re
from typing import Any, Mapping

from harness.control_plane import TodoStatus
from harness.lead.models import CompiledWorkItem, LeadRequest
from harness.router.profile import TaskType

from .plan_feasibility import (
    DeliveryExecutionResponsibility,
    classify_execution_responsibility,
    detect_non_deliverable_reconnaissance,
    has_unconditional_mutation_deliverable,
    is_mutation_consumer as is_structural_mutation_consumer,
    is_mutation_consumer as _authoritative_is_mutation_consumer,
    is_verification_only_objective,
)


SINGLE_IMPLEMENTATION_TODO_CONSTRAINT = "PLAN_SHAPE_SINGLE_IMPLEMENTATION_TODO"
_PLANNER_REFINEMENT_KEY = "plan_refinement_guidance"
_PLANNER_ONLY_CONSTRAINT_KEYS = frozenset(
    {
        _PLANNER_REFINEMENT_KEY,
        SINGLE_IMPLEMENTATION_TODO_CONSTRAINT,
    }
)
_RECONNAISSANCE_ISSUE_KINDS = frozenset(
    {
        "NON_DELIVERABLE_RECONNAISSANCE",
        "RECONNAISSANCE_CANDIDATE_DIFF_INCOMPATIBLE",
        # Some Lead reconnaissance objectives (for example ``Locate the existing
        # candidate.py ...``) are conservatively classified by the generic
        # candidate-diff gate as MUTATION_REQUIRED even though they own no
        # durable mutation.  Under the explicit single-implementation contract
        # we still need deterministic refinement closure for that exact shape.
        "CANDIDATE_DIFF_INCOMPATIBLE",
    }
)


def _plain_constraints(item: CompiledWorkItem) -> dict[str, Any]:
    return {
        key: value
        for key, value in dict(item.work_contract.constraints).items()
        if key not in _PLANNER_ONLY_CONSTRAINT_KEYS
    }


def _strip_planner_only_constraints(item: CompiledWorkItem) -> CompiledWorkItem:
    constraints = _plain_constraints(item)
    if constraints == dict(item.work_contract.constraints):
        return item
    return replace(
        item,
        work_contract=replace(item.work_contract, constraints=constraints),
    )


def _is_reconnaissance_refinement(request: LeadRequest) -> bool:
    guidance = request.constraints.get(_PLANNER_REFINEMENT_KEY)
    if not isinstance(guidance, Mapping):
        return False
    return str(guidance.get("issue_kind", "")) in _RECONNAISSANCE_ISSUE_KINDS


def _is_candidate_diff_refinement(request: LeadRequest) -> bool:
    guidance = request.constraints.get(_PLANNER_REFINEMENT_KEY)
    if not isinstance(guidance, Mapping):
        return False
    return str(guidance.get("issue_kind", "")) in {
        "CANDIDATE_DIFF_INCOMPATIBLE",
        "RECONNAISSANCE_CANDIDATE_DIFF_INCOMPATIBLE",
    }


def _requires_single_implementation_todo(request: LeadRequest) -> bool:
    return request.constraints.get(SINGLE_IMPLEMENTATION_TODO_CONSTRAINT) is True


def _is_reconnaissance_only(item: CompiledWorkItem) -> bool:
    if detect_non_deliverable_reconnaissance(item) is not None:
        return True
    return (
        classify_execution_responsibility(item.todo.objective)
        is DeliveryExecutionResponsibility.RECONNAISSANCE_ONLY
    )


def _is_mutation_consumer(item: CompiledWorkItem) -> bool:
    return _authoritative_is_mutation_consumer(item)



def _same_execution_envelope(left: CompiledWorkItem, right: CompiledWorkItem) -> bool:
    """Require exact inherited execution restrictions before folding.

    Planner-only controls are ignored because they are transport metadata and
    are stripped before persistence.  Todo-specific scope/constraints are not
    merged: if they differ, normalization refuses to guess.
    """

    return (
        _plain_constraints(left) == _plain_constraints(right)
        and tuple(left.work_contract.allowed_scope) == tuple(right.work_contract.allowed_scope)
        and tuple(left.work_contract.forbidden_scope) == tuple(right.work_contract.forbidden_scope)
        and left.work_contract.quota == right.work_contract.quota
        and set(left.work_contract.required_evidence).issubset({"WORKER_RESULT"})
    )


def _rewire_consumer(
    reconnaissance: CompiledWorkItem,
    consumer: CompiledWorkItem,
) -> CompiledWorkItem:
    dependencies: list[str] = []
    for dependency in consumer.todo.dependencies:
        if dependency == reconnaissance.todo.todo_id:
            for inherited in reconnaissance.todo.dependencies:
                if inherited not in dependencies:
                    dependencies.append(inherited)
            continue
        if dependency not in dependencies:
            dependencies.append(dependency)

    todo = replace(
        consumer.todo,
        dependencies=tuple(dependencies),
        status=TodoStatus.PENDING if dependencies else TodoStatus.READY,
    )
    return replace(consumer, todo=todo)


def _fold_unique_reconnaissance_consumers(
    work_items: tuple[CompiledWorkItem, ...],
) -> tuple[CompiledWorkItem, ...]:
    """Fold only a reconnaissance node with one direct mutation consumer."""

    items = tuple(work_items)
    changed = True
    while changed:
        changed = False
        by_id = {item.todo.todo_id: item for item in items}
        if len(by_id) != len(items):
            return items
        dependents: dict[str, list[str]] = {todo_id: [] for todo_id in by_id}
        for item in items:
            for dependency in item.todo.dependencies:
                if dependency not in by_id:
                    return items
                dependents[dependency].append(item.todo.todo_id)

        for reconnaissance in items:
            recon_id = reconnaissance.todo.todo_id
            if not _is_reconnaissance_only(reconnaissance):
                continue
            direct = dependents.get(recon_id, [])
            if len(direct) != 1:
                continue
            consumer = by_id[direct[0]]
            if not _is_mutation_consumer(consumer):
                continue
            if not _same_execution_envelope(reconnaissance, consumer):
                continue

            replacement = _rewire_consumer(reconnaissance, consumer)
            items = tuple(
                replacement if item.todo.todo_id == consumer.todo.todo_id else item
                for item in items
                if item.todo.todo_id != recon_id
            )
            changed = True
            break

    return items

_TERMINAL_TEST_SUITE_RE = re.compile(
    r"^\s*(?:run|execute)\s+(?:the\s+)?(?:(?:full|complete|entire)\s+)?"
    r"(?:pytest|test\s+suite|tests?)\b",
    re.IGNORECASE,
)


def _is_terminal_test_suite_verifier(item: CompiledWorkItem) -> bool:
    objective = " ".join(str(item.todo.objective).split())
    task_type = getattr(item.routing_task_spec, "task_type", None)
    return (
        (
            bool(_TERMINAL_TEST_SUITE_RE.match(objective))
            or (
                task_type is TaskType.TESTS
                and objective.casefold().startswith(("run ", "execute "))
            )
        )
        and is_verification_only_objective(objective)
        and not has_unconditional_mutation_deliverable(objective)
    )


def _shape_ancestor_ids(by_id: dict[str, CompiledWorkItem], todo_id: str) -> set[str]:
    ancestors: set[str] = set()
    pending = list(by_id[todo_id].todo.dependencies)
    while pending:
        current = pending.pop()
        if current in ancestors or current not in by_id:
            continue
        ancestors.add(current)
        pending.extend(by_id[current].todo.dependencies)
    return ancestors


def _fold_unique_terminal_test_suite_verifier(
    work_items: tuple[CompiledWorkItem, ...],
) -> tuple[CompiledWorkItem, ...]:
    items = tuple(work_items)
    by_id = {item.todo.todo_id: item for item in items}
    if len(by_id) != len(items):
        return items

    dependents: dict[str, list[str]] = {todo_id: [] for todo_id in by_id}
    for item in items:
        for dependency in item.todo.dependencies:
            if dependency not in by_id:
                return items
            dependents[dependency].append(item.todo.todo_id)

    for verifier in items:
        verifier_id = verifier.todo.todo_id
        if not _is_terminal_test_suite_verifier(verifier):
            continue
        if dependents[verifier_id]:
            continue
        if len(verifier.todo.dependencies) != 1:
            continue

        predecessor_id = verifier.todo.dependencies[0]
        predecessor = by_id.get(predecessor_id)
        if predecessor is None or not _is_mutation_consumer(predecessor):
            continue
        if not _same_execution_envelope(verifier, predecessor):
            continue

        ancestors = _shape_ancestor_ids(by_id, predecessor_id)
        if any(
            other_id not in ancestors and other_id != predecessor_id
            for other_id in by_id
            if other_id != verifier_id
        ):
            continue

        return tuple(item for item in items if item.todo.todo_id != verifier_id)

    return items




_ANALYSIS_PRELUDE_TASK_TYPES = frozenset(
    {
        TaskType.REASONING,
        TaskType.FAILURE_ANALYSIS,
        TaskType.REPO_EXPLORATION,
        TaskType.ARCHITECTURE,
    }
)

_IMPLEMENTATION_TASK_TYPES = frozenset(
    {
        TaskType.CODE,
        TaskType.BUGFIX,
        TaskType.REFACTOR,
        TaskType.MULTI_FILE_IMPLEMENTATION,
    }
)


def _merge_analysis_prelude_contracts(
    prefix: tuple[CompiledWorkItem, ...],
    implementation: CompiledWorkItem,
):
    """Merge analysis contracts without weakening execution restrictions."""
    implementation_contract = implementation.work_contract
    merged_constraints = _plain_constraints(implementation)
    merged_evidence = list(implementation_contract.required_evidence)

    for item in prefix:
        contract = item.work_contract

        if tuple(contract.allowed_scope) != tuple(implementation_contract.allowed_scope):
            return None
        if tuple(contract.forbidden_scope) != tuple(implementation_contract.forbidden_scope):
            return None
        if contract.quota != implementation_contract.quota:
            return None

        for key, value in _plain_constraints(item).items():
            if key in merged_constraints and merged_constraints[key] != value:
                return None
            merged_constraints[key] = value

        for evidence_type in contract.required_evidence:
            if evidence_type not in merged_evidence:
                merged_evidence.append(evidence_type)

    return replace(
        implementation_contract,
        constraints=merged_constraints,
        required_evidence=tuple(merged_evidence),
    )



def _is_foldable_initial_analysis_prelude_item(
    request: LeadRequest,
    item: CompiledWorkItem,
) -> bool:
    # V10 dedicated analysis types remain foldable.
    task_type = getattr(item.routing_task_spec, "task_type", None)
    if task_type in _ANALYSIS_PRELUDE_TASK_TYPES:
        return True

    # V10.2: a CODE Todo is foldable only when both deterministic semantic
    # classifiers agree that it is reconnaissance-only and has no durable
    # standalone deliverable. Generic CODE remains fail-closed.
    if task_type is not TaskType.CODE:
        return False

    # Preserve bounded refinement semantics: standalone CODE reconnaissance
    # must be rejected on the initial lead-call, then may be folded only
    # after feasibility explicitly requested reconnaissance refinement.
    if not _is_reconnaissance_refinement(request):
        return False

    if (
        classify_execution_responsibility(item.todo.objective)
        is not DeliveryExecutionResponsibility.RECONNAISSANCE_ONLY
    ):
        return False

    return detect_non_deliverable_reconnaissance(item) is not None



def _fold_initial_analysis_prelude(
    request: LeadRequest,
    work_items: tuple[CompiledWorkItem, ...],
) -> tuple[CompiledWorkItem, ...]:
    # Fold a unique leading analysis chain into its first implementation Todo.
    if not _requires_single_implementation_todo(request) or len(work_items) < 2:
        return work_items

    by_id = {item.todo.todo_id: item for item in work_items}
    if len(by_id) != len(work_items):
        return work_items

    dependents: dict[str, list[str]] = {todo_id: [] for todo_id in by_id}
    roots: list[str] = []
    for item in work_items:
        dependencies = tuple(item.todo.dependencies)
        if not dependencies:
            roots.append(item.todo.todo_id)
        for dependency in dependencies:
            if dependency not in by_id:
                return work_items
            dependents[dependency].append(item.todo.todo_id)

    if len(roots) != 1:
        return work_items

    prefix: list[CompiledWorkItem] = []
    current = by_id[roots[0]]

    while True:
        task_type = getattr(current.routing_task_spec, 'task_type', None)
        if _is_foldable_initial_analysis_prelude_item(request, current):
            if is_structural_mutation_consumer(current):
                return work_items
            prefix.append(current)
            children = dependents[current.todo.todo_id]
            if len(children) != 1:
                return work_items
            current = by_id[children[0]]
            continue
        break

    if not prefix:
        return work_items

    implementation = current
    implementation_type = getattr(
        implementation.routing_task_spec,
        'task_type',
        None,
    )
    if implementation_type not in _IMPLEMENTATION_TASK_TYPES:
        return work_items
    if not is_structural_mutation_consumer(implementation):
        return work_items

    if tuple(implementation.todo.dependencies) != (prefix[-1].todo.todo_id,):
        return work_items

    merged_contract = _merge_analysis_prelude_contracts(
        tuple(prefix),
        implementation,
    )
    if merged_contract is None:
        return work_items

    combined_objective = '\n\n'.join(
        item.todo.objective.strip()
        for item in (*prefix, implementation)
        if item.todo.objective.strip()
    )

    inherited_dependencies = tuple(prefix[0].todo.dependencies)
    replacement = replace(
        implementation,
        todo=replace(
            implementation.todo,
            objective=combined_objective,
            dependencies=inherited_dependencies,
            status=TodoStatus.PENDING if inherited_dependencies else TodoStatus.READY,
        ),
        work_contract=replace(
            merged_contract,
            objective=combined_objective,
        ),
    )

    removed = {item.todo.todo_id for item in prefix}
    return tuple(
        replacement if item.todo.todo_id == implementation.todo.todo_id else item
        for item in work_items
        if item.todo.todo_id not in removed
    )


def _fold_refinement_test_first_prelude(
    request: LeadRequest,
    work_items: tuple[CompiledWorkItem, ...],
) -> tuple[CompiledWorkItem, ...]:
    """Fold ``recon -> write tests -> implement`` into one implementation Todo.

    A bounded single-implementation refinement may still put a test-authoring
    mutation between discovery and the actual source implementation.  That
    leaves two candidate-diff owners and prevents the generic linear collapse.
    Keep this rewrite narrower than the initial-prelude fold: it requires the
    explicit contract, a reconnaissance refinement, one linear chain, exactly
    one TESTS node immediately before one implementation node, and the existing
    fail-closed contract merge.
    """

    if (
        not _requires_single_implementation_todo(request)
        or not _is_reconnaissance_refinement(request)
        or len(work_items) < 2
    ):
        return work_items

    by_id = {item.todo.todo_id: item for item in work_items}
    if len(by_id) != len(work_items):
        return work_items

    dependents: dict[str, list[str]] = {todo_id: [] for todo_id in by_id}
    roots: list[str] = []
    for item in work_items:
        dependencies = tuple(item.todo.dependencies)
        if not dependencies:
            roots.append(item.todo.todo_id)
        if len(dependencies) > 1:
            return work_items
        for dependency in dependencies:
            if dependency not in by_id:
                return work_items
            dependents[dependency].append(item.todo.todo_id)
    if len(roots) != 1 or any(len(children) > 1 for children in dependents.values()):
        return work_items

    prefix: list[CompiledWorkItem] = []
    current = by_id[roots[0]]
    while _is_foldable_initial_analysis_prelude_item(request, current):
        if is_structural_mutation_consumer(current):
            return work_items
        prefix.append(current)
        children = dependents[current.todo.todo_id]
        if len(children) != 1:
            return work_items
        current = by_id[children[0]]

    if getattr(current.routing_task_spec, "task_type", None) is not TaskType.TESTS:
        return work_items
    if not is_structural_mutation_consumer(current):
        return work_items
    test_item = current
    children = dependents[test_item.todo.todo_id]
    if len(children) != 1:
        return work_items

    implementation = by_id[children[0]]
    if getattr(implementation.routing_task_spec, "task_type", None) not in _IMPLEMENTATION_TASK_TYPES:
        return work_items
    if not is_structural_mutation_consumer(implementation):
        return work_items
    if dependents[implementation.todo.todo_id]:
        return work_items

    folded = (*prefix, test_item)
    merged_contract = _merge_analysis_prelude_contracts(folded, implementation)
    if merged_contract is None:
        return work_items

    combined_objective = "\n\n".join(
        item.todo.objective.strip()
        for item in (*folded, implementation)
        if item.todo.objective.strip()
    )
    inherited_dependencies = tuple(folded[0].todo.dependencies)
    replacement = replace(
        implementation,
        todo=replace(
            implementation.todo,
            objective=combined_objective,
            dependencies=inherited_dependencies,
            status=TodoStatus.PENDING if inherited_dependencies else TodoStatus.READY,
        ),
        work_contract=replace(
            merged_contract,
            objective=combined_objective,
        ),
    )
    removed = {item.todo.todo_id for item in folded}
    return tuple(
        replacement if item.todo.todo_id == implementation.todo.todo_id else item
        for item in work_items
        if item.todo.todo_id not in removed
    )

def _collapse_linear_single_implementation_candidate(
    request: LeadRequest,
    work_items: tuple[CompiledWorkItem, ...],
) -> tuple[CompiledWorkItem, ...]:
    """Collapse ``recon -> mutate -> verify`` into its unique mutation Todo.

    This is intentionally available only on a refinement request carrying the
    explicit single-implementation contract.  The root objective must itself
    own an unconditional mutation, the candidate must contain exactly one
    mutation Todo, every other Todo must be reconnaissance-only or
    verification-only, and the DAG must be a single connected chain.  That
    makes the rewrite deterministic and preserves fail-closed behavior for
    ambiguous or genuinely multi-deliverable plans.
    """

    if len(work_items) <= 1:
        return work_items
    if not has_unconditional_mutation_deliverable(request.objective):
        return work_items
    if (
        classify_execution_responsibility(request.objective)
        is not DeliveryExecutionResponsibility.MUTATION_REQUIRED
    ):
        return work_items

    mutation_items = tuple(item for item in work_items if _is_mutation_consumer(item))
    implementation_items = tuple(
        item for item in mutation_items
        if getattr(item.routing_task_spec, "task_type", None) in _IMPLEMENTATION_TASK_TYPES
    )
    test_mutations = tuple(
        item for item in mutation_items
        if getattr(item.routing_task_spec, "task_type", None) is TaskType.TESTS
    )
    if len(implementation_items) != 1:
        return work_items
    if len(mutation_items) > 1 and (
        not _is_candidate_diff_refinement(request)
        or len(test_mutations) != len(mutation_items) - 1
        or len(test_mutations) > 1
    ):
        return work_items
    mutation = implementation_items[0]

    for item in work_items:
        if item.todo.todo_id == mutation.todo.todo_id:
            continue
        if _is_reconnaissance_only(item):
            pass
        elif item in test_mutations:
            pass
        elif is_verification_only_objective(item.todo.objective):
            pass
        else:
            return work_items

    by_id = {item.todo.todo_id: item for item in work_items}
    if len(by_id) != len(work_items):
        return work_items

    dependents: dict[str, list[str]] = {todo_id: [] for todo_id in by_id}
    roots: list[str] = []
    for item in work_items:
        dependencies = tuple(item.todo.dependencies)
        if len(dependencies) > 1:
            return work_items
        if not dependencies:
            roots.append(item.todo.todo_id)
        for dependency in dependencies:
            if dependency not in by_id:
                return work_items
            dependents[dependency].append(item.todo.todo_id)
    if len(roots) != 1 or any(len(children) > 1 for children in dependents.values()):
        return work_items

    visited: list[str] = []
    current = roots[0]
    while current not in visited:
        visited.append(current)
        children = dependents[current]
        if not children:
            break
        current = children[0]
    if len(visited) != len(work_items):
        return work_items

    merged_contract = _merge_analysis_prelude_contracts(
        tuple(item for item in work_items if item.todo.todo_id != mutation.todo.todo_id),
        mutation,
    )
    if merged_contract is None:
        return work_items

    combined_objective = "\n\n".join(
        by_id[todo_id].todo.objective.strip()
        for todo_id in visited
        if by_id[todo_id].todo.objective.strip()
    )

    collapsed_todo = replace(
        mutation.todo,
        objective=combined_objective,
        dependencies=(),
        status=TodoStatus.READY,
    )
    return (
        replace(
            mutation,
            todo=collapsed_todo,
            work_contract=replace(merged_contract, objective=combined_objective),
        ),
    )

def _promote_repeated_single_reconnaissance(
    request: LeadRequest,
    work_items: tuple[CompiledWorkItem, ...],
) -> tuple[CompiledWorkItem, ...]:
    """Replace one repeated recon Todo with the exact bounded root objective.

    This fallback is available only after a reconnaissance feasibility
    rejection and only under an explicit single-implementation plan contract.
    It therefore cannot turn a generic repository-audit request into code.
    """

    if len(work_items) != 1:
        return work_items
    item = work_items[0]
    if not _is_reconnaissance_only(item):
        return work_items
    if not has_unconditional_mutation_deliverable(request.objective):
        return work_items
    if (
        classify_execution_responsibility(request.objective)
        is not DeliveryExecutionResponsibility.MUTATION_REQUIRED
    ):
        return work_items

    todo = replace(item.todo, objective=request.objective)
    contract = replace(item.work_contract, objective=request.objective)
    routing = replace(item.routing_task_spec, task_type=TaskType.CODE)
    promoted = replace(item, todo=todo, work_contract=contract, routing_task_spec=routing)
    return (promoted,)


class PlanShapeNormalizer:
    """Normalize only explicitly contracted bounded refinement candidates."""

    def normalize(
        self,
        request: LeadRequest,
        work_items: tuple[Any, ...],
    ) -> tuple[Any, ...]:
        items = tuple(work_items)
        if not all(isinstance(item, CompiledWorkItem) for item in items):
            return items

        # Planner-control keys are never worker execution constraints.  Strip
        # them whether or not a semantic rewrite is needed.
        normalized = tuple(_strip_planner_only_constraints(item) for item in items)

        if not _requires_single_implementation_todo(request):
            return normalized

        normalized = _fold_initial_analysis_prelude(request, normalized)

        if not _is_reconnaissance_refinement(request):
            return normalized

        normalized = _fold_refinement_test_first_prelude(request, normalized)
        normalized = _collapse_linear_single_implementation_candidate(request, normalized)
        normalized = _fold_unique_reconnaissance_consumers(normalized)
        normalized = _fold_unique_terminal_test_suite_verifier(normalized)
        normalized = _promote_repeated_single_reconnaissance(request, normalized)
        return normalized


__all__ = [
    "PlanShapeNormalizer",
    "SINGLE_IMPLEMENTATION_TODO_CONSTRAINT",
]

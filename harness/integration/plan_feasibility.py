"""RC-2 Plan Feasibility, Granularity Policy, and Bounded Refinement.

Enforces precommit validation of Lead-generated plans so oversized or compound
Todos are rejected and refined before durable persistence, Scheduler registration,
worktree creation, or worker activation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import re
from types import MappingProxyType
from typing import Any, Mapping

from harness.lead.models import LeadOutcome, LeadRequest, LeadResult
from harness.mission.models import MissionSpecV1


class FeasibilityDecision(StrEnum):
    ACCEPT = "ACCEPT"
    SPLIT_REQUIRED = "SPLIT_REQUIRED"
    INVALID_PLAN = "INVALID_PLAN"


class DeliveryResponsibility(StrEnum):
    """The deliverable a Todo owns, distinct from subjects it mentions."""

    CODE_IMPLEMENTATION = "CODE_IMPLEMENTATION"
    TEST_MATRIX = "TEST_MATRIX"
    DOCUMENTATION = "DOCUMENTATION"


class DeliveryExecutionResponsibility(StrEnum):
    """Whether a Todo must produce a workspace mutation to complete."""

    MUTATION_REQUIRED = "MUTATION_REQUIRED"
    VERIFICATION_ONLY = "VERIFICATION_ONLY"
    DOCUMENTATION_UPDATE = "DOCUMENTATION_UPDATE"
    RECONNAISSANCE_ONLY = "RECONNAISSANCE_ONLY"


@dataclass(frozen=True, slots=True)
class PlanFeasibilityObservationV1:
    schema_version: int = 1
    decision: FeasibilityDecision = FeasibilityDecision.ACCEPT
    offending_todo_keys: tuple[str, ...] = ()
    independent_clusters: tuple[str, ...] = ()
    reason: str = ""
    evidence: Mapping[str, Any] = field(default_factory=dict)
    guidance: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.decision, FeasibilityDecision):
            raise ValueError(f"decision must be FeasibilityDecision, got {type(self.decision)}")
        object.__setattr__(self, "offending_todo_keys", tuple(self.offending_todo_keys))
        object.__setattr__(self, "independent_clusters", tuple(self.independent_clusters))
        if not isinstance(self.evidence, Mapping):
            raise ValueError("evidence must be a mapping")
        object.__setattr__(self, "evidence", MappingProxyType(dict(self.evidence)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "decision": self.decision.value,
            "offending_todo_keys": list(self.offending_todo_keys),
            "independent_clusters": list(self.independent_clusters),
            "reason": self.reason,
            "evidence": dict(self.evidence),
            "guidance": self.guidance,
        }


@dataclass(slots=True)
class PlanningCallBudget:
    """Bounded call counter shared across generation, repair, and refinement."""

    max_calls: int
    calls_used: int = 0

    def has_remaining(self) -> bool:
        return self.calls_used < self.max_calls

    @property
    def calls_remaining(self) -> int:
        return max(0, self.max_calls - self.calls_used)

    def consume(self, count: int = 1) -> int:
        self.calls_used += count
        return self.calls_used


_CONCERN_TAXONOMY: dict[str, tuple[str, ...]] = {
    "SCHEMA_MODEL": (
        "расширить модель",
        "добавить поля",
        "модель task",
        "модель задачи",
        "task model",
        "task schema",
        "schema model",
        "dataclass task",
        "model definition",
        "field definitions",
        "add fields",
        "expand model",
    ),
    "STORE_LIFECYCLE": (
        "taskstore",
        "store class",
        "in-memory store",
        "хранилище задач",
        "обратная совместимость",
        "backward compatibility",
        "add_task",
        "store lifecycle",
    ),
    "FILTER_QUERY": (
        "фильтрац",
        "детерминированный порядок",
        "детерминированн",
        "filter engine",
        "query engine",
        "sorting engine",
        "сортировк",
    ),
    "PERSISTENCE_IO": (
        "persistence",
        "json persistence",
        "сериализац",
        "файловое хранилище",
        "atomic replace",
        "round-trip",
        "save/load",
    ),
    "CLI_INTERFACE": (
        "cli",
        "command line",
        "argparse",
        "команды add",
        "exit codes",
        "add, list, complete",
    ),
    "TEST_MATRIX": (
        "pytest",
        "test suite",
        "тесты pytest",
        "unit tests",
        "integration tests",
    ),
    "DOCUMENTATION": (
        "readme",
        "документац",
        "documentation",
        "docs",
    ),
}


_RECONNAISSANCE_PREFIXES: tuple[str, ...] = (
    "locate ",
    "inspect ",
    "understand ",
    "explore ",
    "discover ",
    "identify ",
    "find ",
    "review ",
    "analyze ",
    "analyse ",
    "examine ",
    "read ",
    "audit ",
)

_DELIVERY_ACTIONS: tuple[str, ...] = (
    "implement",
    "add",
    "update",
    "modify",
    "change",
    "set",
    "refactor",
    "fix",
    "create",
    "write",
    "produce",
    "generate",
    "remove",
    "delete",
    "migrate",
    "persist",
    "document",
    "verify",
    "validate",
    "test",
    "run",
)

_DELIVERY_TRANSITION_RE = re.compile(
    r"\b(?:and\s+then|then|followed\s+by)\s+(?:"
    + "|".join(re.escape(action) for action in _DELIVERY_ACTIONS)
    + r")\b",
    re.IGNORECASE,
)


_VERIFICATION_ONLY_RE = re.compile(r"^\s*(?:verify|validate|check|inspect|confirm|ensure|run|execute)\b", re.IGNORECASE)
_VERIFICATION_AFTER_RECON_PRELUDE_RE = re.compile(
    r"^\s*(?:inspect|read|review|locate|analyze|analyse|examine|explore)\b"
    r"[^.;]*?\bthen\s+(?:run|execute|verify|validate|check|confirm)\b",
    re.IGNORECASE,
)
_UNCONDITIONAL_FIX_AFTER_RECON_PRELUDE_RE = re.compile(
    r"^\s*(?:inspect|read|review|locate|analyze|analyse|examine|explore)\b"
    r"[^.;]*?\bthen\s+fix\s+(?!if\b|any\b|failures?\b)(?:the\s+)?\w+",
    re.IGNORECASE,
)
_GUARANTEED_MUTATION_ACTION_RE = re.compile(
    r"\b(?:implement|extend|add|update|modify|change|set|refactor|correct|create|write|produce|generate|remove|delete|migrate|persist|document)\b",
    re.IGNORECASE,
)

_CANDIDATE_DIFF_EVIDENCE_ACTION_RE = re.compile(
    r"\b(?:produce|generate|create|write)\s+(?:the\s+)?candidate[_ -]?diff(?:\s+evidence)?\b",
    re.IGNORECASE,
)



_CANDIDATE_DIFF_REFINEMENT_GUIDANCE = (
    "Do NOT emit a standalone verification-only executable Todo. "
    "Fold configured verification into an existing mutation Todo while preserving dependencies and scope. "
    "The mutation deliverable must be unconditional. "
    "'fix failures if any' is conditional and does NOT satisfy candidate_diff. "
    "Do NOT recreate the same semantic validator under a different Todo ID. "
    "Do NOT manufacture a mutation. "
    "All configured gates will execute on the mutation Todo. "
    "If validation has independent semantic output, restructure the plan so a real durable deliverable exists; "
    "do not pretend candidate_diff is satisfied. "
    "Preserve valid dependencies and constraints for unaffected Todos. "
    "Produce a complete replacement DAG."
)

_RECONNAISSANCE_CANDIDATE_DIFF_GUIDANCE = (
    "Fold repository discovery into the first implementation Todo. "
    "Do not create standalone reconnaissance Todos. "
    "Do NOT create standalone repository exploration Todos. "
    "Fold repository inspection into the first mutation Todo. "
    "The mutation Todo must inspect existing implementation, identify integration "
    "points, modify durable files, and produce candidate_diff evidence. "
    "Do not create analysis-only execution steps."
)

_VERIFICATION_EXECUTION_PREFIXES: tuple[str, ...] = (
    "write comprehensive pytest test suite",
    "write comprehensive pytest tests",
    "write pytest test suite",
    "write pytest tests",
    "write tests",
    "run tests",
    "run pytest",
    "run the full test suite",
    "run existing test suite",
    "run existing and new test suite",
    "run the existing test suite",
    "run the existing and new test suite",
    "run the full pytest suite",
    "run verification",
    "validate",
    "verify",
    "audit",
    "benchmark",
    "check",
)

_DOCUMENTATION_EXECUTION_PREFIXES: tuple[str, ...] = (
    "update readme",
    "write readme",
    "add readme",
    "create readme",
    "update documentation",
    "write documentation",
    "add documentation",
    "create documentation",
    "document",
    "update usage guide",
    "write usage guide",
    "add usage guide",
    "create usage guide",
)

_RECONNAISSANCE_EXECUTION_SIGNALS: tuple[str, ...] = (
    "analyze existing",
    "analyse existing",
    "inspect existing",
    "inspect repository",
    "explore repository",
    "review codebase",
    "identify integration points",
    "understand current structure",
    "map existing architecture",
    "inspect current implementation",
)


def classify_execution_responsibility(
    objective: str,
) -> DeliveryExecutionResponsibility:
    normalized = " ".join(str(objective).casefold().split())
    if _VERIFICATION_AFTER_RECON_PRELUDE_RE.match(normalized) and not has_unconditional_mutation_deliverable(normalized):
        return DeliveryExecutionResponsibility.VERIFICATION_ONLY
    # Writing test files is a mutation even when their purpose is verification.
    if normalized.startswith(("write tests", "write pytest tests", "write pytest test suite", "write comprehensive pytest")):
        return DeliveryExecutionResponsibility.MUTATION_REQUIRED
    if normalized.startswith(_VERIFICATION_EXECUTION_PREFIXES):
        return DeliveryExecutionResponsibility.VERIFICATION_ONLY
    if normalized.startswith(_DOCUMENTATION_EXECUTION_PREFIXES):
        return DeliveryExecutionResponsibility.DOCUMENTATION_UPDATE
    if (
        any(signal in normalized for signal in _RECONNAISSANCE_EXECUTION_SIGNALS)
        and not has_unconditional_mutation_deliverable(normalized)
    ):
        return DeliveryExecutionResponsibility.RECONNAISSANCE_ONLY
    if (
        any(normalized.startswith(prefix) for prefix in _RECONNAISSANCE_PREFIXES)
        and not has_unconditional_mutation_deliverable(normalized)
    ):
        return DeliveryExecutionResponsibility.RECONNAISSANCE_ONLY
    return DeliveryExecutionResponsibility.MUTATION_REQUIRED

def is_verification_only_objective(objective: str) -> bool:
    """Return whether an objective is led by verification rather than delivery."""
    normalized = " ".join(str(objective).split())
    if _VERIFICATION_AFTER_RECON_PRELUDE_RE.match(normalized):
        return not has_unconditional_mutation_deliverable(normalized)
    return bool(_VERIFICATION_ONLY_RE.match(normalized))


def has_unconditional_mutation_deliverable(objective: str) -> bool:
    # D4-C mutation vocabulary, excluding candidate_diff meta evidence.
    normalized = " ".join(str(objective).split())
    semantic = _CANDIDATE_DIFF_EVIDENCE_ACTION_RE.sub(" ", normalized)
    semantic = " ".join(semantic.split())
    return bool(
        _GUARANTEED_MUTATION_ACTION_RE.search(semantic)
        or (
            _UNCONDITIONAL_FIX_AFTER_RECON_PRELUDE_RE.match(semantic)
            and not re.search(
                r"\b(?:if|when|as\s+needed)\b",
                re.split(r"\bthen\s+fix\b", semantic, maxsplit=1, flags=re.IGNORECASE)[-1],
                re.IGNORECASE,
            )
        )
    )

_MUTATION_CAPABLE_TASK_TYPE_VALUES = frozenset(
    {
        "code",
        "bugfix",
        "refactor",
        "tests",
        "multi_file_implementation",
    }
)


def is_mutation_capable_task_type(task_type: Any) -> bool:
    value = getattr(task_type, "value", task_type)
    return str(value or "").casefold() in _MUTATION_CAPABLE_TASK_TYPE_VALUES


def is_mutation_consumer(item: Any) -> bool:
    routing = getattr(item, "routing_task_spec", None)
    task_type = getattr(routing, "task_type", None)
    if not is_mutation_capable_task_type(task_type):
        return False
    objective = getattr(getattr(item, "todo", None), "objective", "")
    return has_unconditional_mutation_deliverable(objective)


def detect_candidate_diff_incompatible_todo(item: Any) -> str | None:
    # Validate candidate_diff against the WorkItem's actual execution role.
    gates = tuple(getattr(item.todo, "required_gates", ()) or ())
    if "candidate_diff" not in gates:
        return None

    objective = " ".join(str(item.todo.objective).split())
    responsibility = classify_execution_responsibility(objective)

    routing = getattr(item, "routing_task_spec", None)
    task_type = getattr(routing, "task_type", None)
    task_type_value = str(getattr(task_type, "value", task_type) or "").casefold()

    # Verification and critique consume/evaluate an existing candidate diff;
    # they do not have to create a fresh workspace mutation.
    if responsibility is DeliveryExecutionResponsibility.VERIFICATION_ONLY:
        return "verification-only Todo cannot require candidate_diff mutation evidence"
    if task_type_value == "critique":
        return None

    # Mutation authority is structural: task type and objective deliverable
    # must agree.
    if is_mutation_consumer(item):
        return None

    if (
        responsibility is DeliveryExecutionResponsibility.RECONNAISSANCE_ONLY
        or task_type_value in {
            "reasoning",
            "failure_analysis",
            "repo_exploration",
            "architecture",
        }
    ):
        return (
            "standalone analysis/reconnaissance Todo cannot produce "
            "candidate_diff mutation evidence"
        )

    if has_unconditional_mutation_deliverable(objective):
        return (
            "candidate_diff is mandatory but mutation-like objective wording "
            f"is incompatible with non-mutation task type {task_type_value!r}"
        )

    return (
        "candidate_diff is mandatory but the Todo has no unconditional "
        f"mutation deliverable ({responsibility.value})"
    )

def detect_non_deliverable_reconnaissance(
    item: Any,
) -> str | None:
    """Reject standalone repository discovery encoded as an executable CODE Todo.

    Repository discovery is valid inside an implementation Todo. It is not a
    standalone executable deliverable because successful inspection alone
    produces no candidate mutation, terminal verification result, or explicit
    durable artifact for the coding pipeline to accept.
    """

    routing = getattr(item, "routing_task_spec", None)
    task_type = getattr(routing, "task_type", None)
    task_type_value = getattr(task_type, "value", None)

    # This guard is deliberately limited to coding Todos. Dedicated research
    # or other non-code task types retain their own completion semantics.
    if str(task_type_value or "").casefold() != "code":
        return None

    objective = " ".join(
        str(item.todo.objective).casefold().split()
    )

    if not objective:
        return "empty executable Todo objective"

    starts_with_reconnaissance = any(
        objective.startswith(prefix)
        for prefix in _RECONNAISSANCE_PREFIXES
    )

    if not starts_with_reconnaissance:
        return None

    # Discovery is valid when the same Todo explicitly implements a durable
    # deliverable. Keep ambiguous "add/update" reconnaissance wording subject
    # to the established D2 explicit-transition rule below.
    if re.search(r"\b(?:implement|extend|set)\b", objective) and has_unconditional_mutation_deliverable(objective):
        return None

    # Retain the existing explicit transition allowance for bounded terminal
    # verification and other non-mutation outcomes.
    if _DELIVERY_TRANSITION_RE.search(objective):
        return None

    return (
        "standalone reconnaissance CODE Todo has no explicit durable "
        "deliverable; repository discovery must be folded into the nearest "
        "implementation, artifact-production, or terminal verification Todo"
    )


def detect_concern_clusters(text: str) -> tuple[str, ...]:
    """Identify distinct architectural concern clusters present in objective text."""
    lowered = text.casefold()
    detected = []
    for cluster_name, keywords in _CONCERN_TAXONOMY.items():
        if any(keyword in lowered for keyword in keywords):
            detected.append(cluster_name)
    return tuple(detected)



_META_DELIVERABLE_MUTATION_RE = re.compile(
    r"\b(?:and\s+then|then|followed\s+by|and)\s+(?:"
    r"implement|add|update|modify|change|refactor|fix|create|"
    r"remove|delete|migrate|persist"
    r")\b",
    re.IGNORECASE,
)


_TEST_MATRIX_DELIVERY_PREFIXES: tuple[str, ...] = (
    "write comprehensive pytest test suite",
    "write comprehensive pytest tests",
    "write pytest test suite",
    "write pytest tests",
    "write unit tests",
    "write integration tests",
    "write tests",
    "add pytest tests",
    "add unit tests",
    "add integration tests",
    "add tests",
    "create pytest tests",
    "create unit tests",
    "create integration tests",
    "create tests",
    "implement pytest tests",
    "implement unit tests",
    "implement integration tests",
    "implement tests",
    "update pytest tests",
    "update unit tests",
    "update integration tests",
    "update tests",
    "pytest test suite",
)

_DOCUMENTATION_DELIVERY_PREFIXES: tuple[str, ...] = (
    "update readme",
    "write readme",
    "add readme",
    "create readme",
    "update documentation",
    "write documentation",
    "add documentation",
    "create documentation",
    "document",
)


def classify_delivery_responsibility(objective: str) -> DeliveryResponsibility:
    """Classify the work a Todo delivers rather than its coverage subjects."""
    normalized = " ".join(str(objective).casefold().split())
    if normalized.startswith(_TEST_MATRIX_DELIVERY_PREFIXES):
        return DeliveryResponsibility.TEST_MATRIX
    if normalized.startswith(_DOCUMENTATION_DELIVERY_PREFIXES):
        return DeliveryResponsibility.DOCUMENTATION
    return DeliveryResponsibility.CODE_IMPLEMENTATION


def project_delivery_responsibility_clusters(text: str) -> tuple[str, ...]:
    """Project delivery responsibility without treating coverage as delivery."""
    responsibility = classify_delivery_responsibility(text)
    clusters = detect_concern_clusters(text)
    if responsibility is DeliveryResponsibility.CODE_IMPLEMENTATION:
        return clusters
    projected = [responsibility.value]
    has_implementation_subject = any(
        cluster not in {DeliveryResponsibility.TEST_MATRIX.value, DeliveryResponsibility.DOCUMENTATION.value}
        for cluster in clusters
    )
    if has_implementation_subject and _META_DELIVERABLE_MUTATION_RE.search(text):
        projected.append(DeliveryResponsibility.CODE_IMPLEMENTATION.value)
    return tuple(projected)


def build_refinement_request(
    request: LeadRequest,
    result: LeadResult,
    observation: PlanFeasibilityObservationV1,
) -> LeadRequest:
    """Construct a structured refined LeadRequest for the next OrnithLead.decompose attempt."""
    refinement_constraints = dict(request.constraints)
    issue_kind = str(observation.evidence.get("issue_kind", ""))

    mandatory_replacement_shape: dict[str, Any] | None = None
    if issue_kind == "NON_DELIVERABLE_RECONNAISSANCE":
        instructions = (
            "Refine non-deliverable executable Todo(s) into independently "
            "verifiable deliverables. "
            "Do NOT emit repository reconnaissance, locating, inspection, or understanding as a standalone CODE Todo. "
            "The replacement DAG MUST NOT contain a CODE Todo whose only "
            "responsibility is inspect/locate/find/review/explore/discover/read/analyze/understand. "
            "If repository inspection is required, put that inspection inside the SAME Todo "
            "that also owns an unconditional durable mutation or artifact deliverable. "
            "A valid implementation Todo should read like 'Inspect the existing implementation "
            "as needed, then update/implement <durable target> and verify the result', not like "
            "'Inspect repository structure'. "
            "Fold bounded repository discovery into the nearest implementation "
            "Todo, explicit durable-artifact Todo, or terminal verification Todo. "
            "Preserve valid dependencies and constraints for unaffected Todos. "
            "Produce a complete replacement DAG. "
            "Do NOT micro-split trivial single-file changes into unnecessary "
            "individual steps. Standalone non-CODE reconnaissance in a bounded "
            "implementation mission must also be folded into its consuming Todo."
        )
        mandatory_replacement_shape = {
            "standalone_reconnaissance_code_todo": "FORBIDDEN",
            "repository_discovery": "INLINE_WITH_CONSUMING_DELIVERABLE_TODO",
            "first_code_todo_requires_durable_deliverable": True,
            "prohibited_reconnaissance_prefixes": list(_RECONNAISSANCE_PREFIXES),
        }
    elif issue_kind == "RECONNAISSANCE_CANDIDATE_DIFF_INCOMPATIBLE":
        instructions = (
            _RECONNAISSANCE_CANDIDATE_DIFF_GUIDANCE
            + " The replacement DAG MUST NOT preserve the reconnaissance step under a new Todo ID. "
            "The first CODE Todo must itself own the concrete mutation deliverable while performing "
            "any required inspection inline."
        )
        mandatory_replacement_shape = {
            "standalone_reconnaissance_code_todo": "FORBIDDEN",
            "repository_discovery": "INLINE_WITH_FIRST_MUTATION_TODO",
            "first_code_todo_requires_durable_deliverable": True,
            "prohibited_reconnaissance_prefixes": list(_RECONNAISSANCE_PREFIXES),
        }
    elif issue_kind == "CANDIDATE_DIFF_INCOMPATIBLE":
        instructions = _CANDIDATE_DIFF_REFINEMENT_GUIDANCE
    else:
        instructions = (
            "Decompose compound Todo(s) into sequential, independently verifiable sub-Todos. "
            "Preserve valid dependencies and constraints for unaffected Todos. "
            "Produce a complete replacement DAG. "
            "Do NOT micro-split trivial single-file changes into unnecessary individual steps; "
            "keep each sub-Todo coherent and focused on a single responsibility."
        )

    if observation.guidance and issue_kind != "CANDIDATE_DIFF_INCOMPATIBLE":
        instructions = f"{instructions} {observation.guidance}"

    refinement_payload = {
        "offending_todo_keys": list(observation.offending_todo_keys),
        "detected_clusters": list(observation.independent_clusters),
        "reason": observation.reason,
        "issue_kind": issue_kind,
        "instructions": instructions,
    }
    if mandatory_replacement_shape is not None:
        refinement_payload["mandatory_replacement_shape"] = mandatory_replacement_shape
    refinement_constraints["plan_refinement_guidance"] = refinement_payload

    refinement_meta = dict(request.metadata)
    refinement_meta["feasibility_rejection_reason"] = observation.reason

    return LeadRequest(
        objective=request.objective,
        constraints=refinement_constraints,
        allowed_scope=request.allowed_scope,
        forbidden_scope=request.forbidden_scope,
        metadata=refinement_meta,
    )


class PlanFeasibilityGate:
    """Evaluates whether a compiled planning candidate is feasible for single-slice execution."""

    def __init__(
        self,
        *,
        max_clusters_per_todo: int = 2,
        compound_pair_rules: tuple[tuple[str, str], ...] = (
            ("SCHEMA_MODEL", "CLI_INTERFACE"),
            ("SCHEMA_MODEL", "PERSISTENCE_IO"),
            ("PERSISTENCE_IO", "CLI_INTERFACE"),
        ),
    ) -> None:
        self.max_clusters_per_todo = max_clusters_per_todo
        self.compound_pair_rules = compound_pair_rules

    def evaluate(
        self,
        spec: MissionSpecV1,
        result: LeadResult,
    ) -> PlanFeasibilityObservationV1:
        if result.outcome is not LeadOutcome.PLAN_READY or result.goal is None or not result.work_items:
            return PlanFeasibilityObservationV1(
                decision=FeasibilityDecision.INVALID_PLAN,
                reason=f"Lead outcome is not PLAN_READY: {result.outcome.value if result else 'None'}",
                evidence={"outcome": result.outcome.value if result else None},
            )

        # Check DAG topological integrity and cycle freedom
        all_todo_ids = {item.todo.todo_id for item in result.work_items}
        for item in result.work_items:
            for dep in item.todo.dependencies:
                if dep not in all_todo_ids:
                    return PlanFeasibilityObservationV1(
                        decision=FeasibilityDecision.INVALID_PLAN,
                        offending_todo_keys=(item.todo.todo_id,),
                        reason=f"Todo {item.todo.todo_id} references non-existent dependency: {dep}",
                        evidence={"missing_dependency": dep},
                    )

        # Executability invariant:
        # every CODE Todo entering the production scheduler must have an
        # independently acceptable outcome. Pure discovery belongs inside
        # implementation/context acquisition, not as a standalone DAG node.
        non_deliverable_keys: list[str] = []
        non_deliverable_reasons: list[str] = []

        for item in result.work_items:
            deliverability_reason = detect_non_deliverable_reconnaissance(item)
            # A bounded implementation mission cannot execute a standalone
            # analysis prelude: its candidate verifier requires a mutation.
            # Dedicated research missions retain their own completion rules.
            if (
                deliverability_reason is None
                and spec.global_constraints.get("PLAN_SHAPE_SINGLE_IMPLEMENTATION_TODO") is True
                and str(getattr(getattr(item.routing_task_spec, "task_type", None), "value", "")).casefold()
                in {"reasoning", "failure_analysis", "repo_exploration", "architecture"}
                and classify_execution_responsibility(item.todo.objective)
                is DeliveryExecutionResponsibility.RECONNAISSANCE_ONLY
            ):
                deliverability_reason = (
                    "standalone analysis Todo has no durable implementation deliverable; "
                    "fold it into the consuming implementation Todo"
                )
            if deliverability_reason is None:
                continue

            non_deliverable_keys.append(item.todo.todo_id)
            non_deliverable_reasons.append(
                f"Todo '{item.todo.todo_id}': {deliverability_reason}"
            )

        if non_deliverable_keys:
            reason_summary = "; ".join(non_deliverable_reasons)

            return PlanFeasibilityObservationV1(
                decision=FeasibilityDecision.SPLIT_REQUIRED,
                offending_todo_keys=tuple(non_deliverable_keys),
                reason=reason_summary,
                evidence={
                    "issue_kind": "NON_DELIVERABLE_RECONNAISSANCE",
                    "total_work_items": len(result.work_items),
                    "offending_count": len(non_deliverable_keys),
                },
                guidance=(
                    "Each executable CODE Todo must expose an independently "
                    "verifiable deliverable. Fold repository discovery into "
                    "the implementation Todo that consumes the discovered "
                    "context rather than persisting discovery as its own Todo."
                ),
            )

        incompatible_keys: list[str] = []
        incompatible_reasons: list[str] = []
        has_reconnaissance_incompatibility = False
        for item in result.work_items:
            reason = detect_candidate_diff_incompatible_todo(item)
            if reason is not None:
                incompatible_keys.append(item.todo.todo_id)
                incompatible_reasons.append(f"Todo '{item.todo.todo_id}': {reason}")
                if (
                    classify_execution_responsibility(item.todo.objective)
                    is DeliveryExecutionResponsibility.RECONNAISSANCE_ONLY
                ):
                    has_reconnaissance_incompatibility = True
        if incompatible_keys:
            issue_kind = (
                "RECONNAISSANCE_CANDIDATE_DIFF_INCOMPATIBLE"
                if has_reconnaissance_incompatibility
                else "CANDIDATE_DIFF_INCOMPATIBLE"
            )
            guidance = (
                _RECONNAISSANCE_CANDIDATE_DIFF_GUIDANCE
                if has_reconnaissance_incompatibility
                else _CANDIDATE_DIFF_REFINEMENT_GUIDANCE
            )
            return PlanFeasibilityObservationV1(
                decision=FeasibilityDecision.SPLIT_REQUIRED,
                offending_todo_keys=tuple(incompatible_keys),
                reason="; ".join(incompatible_reasons),
                evidence={
                    "issue_kind": issue_kind,
                    "total_work_items": len(result.work_items),
                    "offending_count": len(incompatible_keys),
                },
                guidance=guidance,
            )

        offending_keys = []
        all_clusters: list[str] = []
        detailed_reasons: list[str] = []

        for item in result.work_items:
            objective = item.todo.objective
            delivery = classify_delivery_responsibility(objective)
            clusters = project_delivery_responsibility_clusters(objective)
            for c in clusters:
                if c not in all_clusters:
                    all_clusters.append(c)

            is_compound = False
            reasons = []
            if (
                delivery is not DeliveryResponsibility.CODE_IMPLEMENTATION
                and len(clusters) > 1
            ):
                is_compound = True
                reasons.append(
                    "combines delivery responsibilities "
                    f"({', '.join(clusters)})"
                )

            if delivery is DeliveryResponsibility.CODE_IMPLEMENTATION:
                # Concern-cluster compound detection applies only to real code
                # implementation, never to test/documentation coverage subjects.
                if len(clusters) > self.max_clusters_per_todo:
                    is_compound = True
                    reasons.append(
                        f"combines {len(clusters)} independent concerns ({', '.join(clusters)})"
                    )
                for pair in self.compound_pair_rules:
                    if pair[0] in clusters and pair[1] in clusters:
                        is_compound = True
                        reasons.append(
                            f"combines incompatible architectural domains {pair[0]} + {pair[1]}"
                        )

            if is_compound:
                key = item.todo.todo_id
                offending_keys.append(key)
                detailed_reasons.append(f"Todo '{key}': {'; '.join(reasons)}")

        if offending_keys:
            reason_summary = "; ".join(detailed_reasons)
            guidance = (
                f"Decompose compound Todo(s) [{', '.join(offending_keys)}] into distinct, "
                "sequentially verifiable sub-tasks representing single architectural concerns."
            )
            return PlanFeasibilityObservationV1(
                decision=FeasibilityDecision.SPLIT_REQUIRED,
                offending_todo_keys=tuple(offending_keys),
                independent_clusters=tuple(all_clusters),
                reason=reason_summary,
                evidence={
                    "total_work_items": len(result.work_items),
                    "offending_count": len(offending_keys),
                    "detected_clusters": list(all_clusters),
                },
                guidance=guidance,
            )

        return PlanFeasibilityObservationV1(
            decision=FeasibilityDecision.ACCEPT,
            reason="Plan granularity is feasible and within single-slice execution capacity.",
            evidence={
                "total_work_items": len(result.work_items),
                "clusters": list(all_clusters),
            },
        )

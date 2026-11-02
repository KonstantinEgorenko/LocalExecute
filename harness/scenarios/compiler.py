'Deterministic compiler and validator for research diagnostics typed scenarios.'

from __future__ import annotations

from .models import (
    CompiledScenarioV1,
    DeliverableTypeV1,
    ScenarioNodeKindV1,
    ScenarioNodeV1,
    ScenarioSelectionV1,
    ScenarioTypeV1,
    ToolProfileV1,
)


class ScenarioGraphValidatorV1:
    """Validate graph structure and execution-responsibility invariants."""

    def validate(self, scenario: CompiledScenarioV1) -> CompiledScenarioV1:
        if not isinstance(scenario, CompiledScenarioV1):
            raise TypeError("scenario must be CompiledScenarioV1")

        by_id = {node.node_id: node for node in scenario.nodes}
        if len(by_id) != len(scenario.nodes):
            raise ValueError("compiled scenario node ids must be unique")

        for node in scenario.nodes:
            for dependency in node.dependencies:
                if dependency not in by_id:
                    raise ValueError(
                        f"node {node.node_id!r} depends on missing node "
                        f"{dependency!r}"
                    )

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node_id: str) -> None:
            if node_id in visited:
                return
            if node_id in visiting:
                raise ValueError("compiled scenario graph must be acyclic")
            visiting.add(node_id)
            for dependency in by_id[node_id].dependencies:
                visit(dependency)
            visiting.remove(node_id)
            visited.add(node_id)

        for node_id in by_id:
            visit(node_id)

        for node in scenario.nodes:
            if node.kind is ScenarioNodeKindV1.IMPLEMENTATION:
                if not node.mutation_required:
                    raise ValueError(
                        "implementation node must require workspace mutation"
                    )
                if node.deliverable is not DeliverableTypeV1.CANDIDATE_DIFF:
                    raise ValueError(
                        "implementation node must deliver CANDIDATE_DIFF"
                    )
                if node.tool_profile is not ToolProfileV1.CODE_EDIT:
                    raise ValueError(
                        "implementation node must use CODE_EDIT tool profile"
                    )
            elif node.kind is ScenarioNodeKindV1.RESEARCH:
                if node.mutation_required:
                    raise ValueError("research node cannot mutate workspace")
                if node.deliverable is not DeliverableTypeV1.RESEARCH_EVIDENCE:
                    raise ValueError(
                        "research node must deliver RESEARCH_EVIDENCE"
                    )
            elif node.kind is ScenarioNodeKindV1.DETERMINISTIC_VERIFICATION:
                if node.deliverable is not DeliverableTypeV1.VERIFICATION_REPORT:
                    raise ValueError(
                        "verification node must deliver VERIFICATION_REPORT"
                    )
                if node.tool_profile is not ToolProfileV1.NONE:
                    raise ValueError(
                        "deterministic verification is not a model tool profile"
                    )
            elif node.kind is ScenarioNodeKindV1.JUDGE:
                if node.deliverable is not DeliverableTypeV1.JUDGE_DECISION:
                    raise ValueError("judge node must deliver JUDGE_DECISION")
                if node.tool_profile is not ToolProfileV1.NONE:
                    raise ValueError("judge is not a coding-worker tool profile")

        roots = tuple(node for node in scenario.nodes if not node.dependencies)
        if len(roots) != 1:
            raise ValueError("Phase 24A scenarios require exactly one graph root")

        judges = tuple(
            node
            for node in scenario.nodes
            if node.kind is ScenarioNodeKindV1.JUDGE
        )
        if len(judges) != 1:
            raise ValueError("compiled scenario requires exactly one Judge node")

        judge = judges[0]
        if len(judge.dependencies) != 1:
            raise ValueError("Judge must depend on exactly one node")
        judge_parent = by_id[judge.dependencies[0]]
        if judge_parent.kind is not ScenarioNodeKindV1.DETERMINISTIC_VERIFICATION:
            raise ValueError("Judge must depend on deterministic verification")

        return scenario


class ScenarioCompilerV1:
    """Compile a typed selection into deterministic control flow.

    Phase 24D supports SIMPLE_MUTATION, BUGFIX_WITH_REGRESSION,
    MULTI_FILE_CHANGE, and VERIFICATION_ONLY. Other enum values fail closed
    until their invariants are implemented.
    """

    def __init__(
        self,
        validator: ScenarioGraphValidatorV1 | None = None,
    ) -> None:
        self.validator = validator or ScenarioGraphValidatorV1()

    def compile(self, selection: ScenarioSelectionV1) -> CompiledScenarioV1:
        if not isinstance(selection, ScenarioSelectionV1):
            raise TypeError("selection must be ScenarioSelectionV1")
        if selection.requires_research:
            raise NotImplementedError(
                f"ScenarioCompilerV1 does not yet implement required research for {selection.scenario.value}")

        if selection.scenario in {
            ScenarioTypeV1.SIMPLE_MUTATION,
            ScenarioTypeV1.BUGFIX_WITH_REGRESSION,
            ScenarioTypeV1.MULTI_FILE_CHANGE,
        }:
            nodes = (
                ScenarioNodeV1(
                    node_id="implementation",
                    kind=ScenarioNodeKindV1.IMPLEMENTATION,
                    dependencies=(),
                    deliverable=DeliverableTypeV1.CANDIDATE_DIFF,
                    mutation_required=True,
                    tool_profile=ToolProfileV1.CODE_EDIT,
                ),
                ScenarioNodeV1(
                    node_id="verify",
                    kind=ScenarioNodeKindV1.DETERMINISTIC_VERIFICATION,
                    dependencies=("implementation",),
                    deliverable=DeliverableTypeV1.VERIFICATION_REPORT,
                    mutation_required=False,
                    tool_profile=ToolProfileV1.NONE,
                ),
                ScenarioNodeV1(
                    node_id="judge",
                    kind=ScenarioNodeKindV1.JUDGE,
                    dependencies=("verify",),
                    deliverable=DeliverableTypeV1.JUDGE_DECISION,
                    mutation_required=False,
                    tool_profile=ToolProfileV1.NONE,
                ),
            )
        elif selection.scenario is ScenarioTypeV1.VERIFICATION_ONLY:
            nodes = (
                ScenarioNodeV1(
                    node_id="verify",
                    kind=ScenarioNodeKindV1.DETERMINISTIC_VERIFICATION,
                    dependencies=(),
                    deliverable=DeliverableTypeV1.VERIFICATION_REPORT,
                    mutation_required=False,
                    tool_profile=ToolProfileV1.NONE,
                ),
                ScenarioNodeV1(
                    node_id="judge",
                    kind=ScenarioNodeKindV1.JUDGE,
                    dependencies=("verify",),
                    deliverable=DeliverableTypeV1.JUDGE_DECISION,
                    mutation_required=False,
                    tool_profile=ToolProfileV1.NONE,
                ),
            )
        else:
            raise NotImplementedError(
                f"ScenarioCompilerV1 does not yet implement "
                f"{selection.scenario.value}"
            )

        return self.validator.validate(
            CompiledScenarioV1(
                schema_version=1,
                selection=selection,
                nodes=nodes,
            )
        )

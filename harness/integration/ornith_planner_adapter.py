'Contract-checked mission planning adapter backed by the existing Lead with precommit feasibility validation.'

from __future__ import annotations

from dataclasses import replace
from typing import Any

from harness.control_plane import InMemoryRunStore
from harness.control_plane.errors import StoreIntegrityError
from harness.lead.models import LeadOutcome, LeadRequest, LeadResult
from harness.mission.models import MissionSpecV1, PlanObservationV1, stable_sha256

from .plan_feasibility import (
    FeasibilityDecision,
    PlanFeasibilityGate,
    PlanningCallBudget,
    is_mutation_consumer,
    classify_execution_responsibility,
    DeliveryExecutionResponsibility,
    build_refinement_request,
)


from .planning_trace import PlanningTraceRecorder
from .plan_shape_normalization import PlanShapeNormalizer
from .terminal_verifier_normalization import TerminalVerifierNormalizer
from harness.lead.required_graph import REQUIRED_GRAPH, RequiredDependencyGraphV1
from harness.scenarios.scope_evidence import PlanningContractError
from .planning_evidence import PlanningEvidenceRuntime, retain

class OrnithPlanningAdapter:
    """Implement MissionPlanningPort with transient precommit feasibility validation using existing Lead."""

    def __init__(
        self,
        ornith_lead: Any,
        request_factory: Any,
        plan_registry: Any = None,
        required_gates: tuple[str, ...] = (),
        feasibility_gate: PlanFeasibilityGate | None = None,
        planning_trace: PlanningTraceRecorder | None = None,
        plan_shape_normalizer: PlanShapeNormalizer | None = None,
    ) -> None:
        self.ornith_lead = ornith_lead
        self.request_factory = request_factory
        self.plan_registry = plan_registry
        self.required_gates = tuple(dict.fromkeys(str(gate) for gate in required_gates))
        self.feasibility_gate = feasibility_gate or PlanFeasibilityGate()
        self.planning_trace = planning_trace
        self.plan_shape_normalizer = plan_shape_normalizer or PlanShapeNormalizer()

    def _normalize_work_items(self, work_items: tuple[Any, ...]) -> tuple[Any, ...]:
        work_items = TerminalVerifierNormalizer(self.required_gates).normalize(work_items)

        analysis_only_task_types = {
            "reasoning",
            "critique",
            "failure_analysis",
            "repo_exploration",
            "architecture",
        }

        normalized = []
        for item in work_items:
            routing = getattr(item, "routing_task_spec", None)
            task_type = getattr(routing, "task_type", None)
            task_type_value = str(
                getattr(task_type, "value", task_type) or ""
            ).casefold()
            verification_only = (
                classify_execution_responsibility(item.todo.objective)
                is DeliveryExecutionResponsibility.VERIFICATION_ONLY
            )

            projected_gates = tuple(
                gate
                for gate in self.required_gates
                if not (
                    gate == "candidate_diff"
                    and (task_type_value in analysis_only_task_types or verification_only)
                )
            )

            todo = replace(
                item.todo,
                required_gates=tuple(
                    dict.fromkeys((*item.todo.required_gates, *projected_gates))
                ),
            )

            constraints = dict(item.work_contract.constraints)
            if is_mutation_consumer(item):
                constraints["REQUIRES_WORKSPACE_MUTATION"] = True

            normalized.append(
                replace(
                    item,
                    todo=todo,
                    work_contract=replace(
                        item.work_contract,
                        constraints=constraints,
                    ),
                )
            )
        return tuple(normalized)


    def _commit_and_persist(
        self,
        spec: MissionSpecV1,
        result: LeadResult,
        run_store: Any | None,
    ) -> PlanObservationV1:
        if run_store is not None and result.goal is not None:
            try:
                run_store.create_goal(result.goal)
            except StoreIntegrityError:
                if REQUIRED_GRAPH in spec.global_constraints:
                    raise
                pass
            for item in result.work_items:
                try:
                    run_store.create_todo(item.todo)
                except StoreIntegrityError:
                    if REQUIRED_GRAPH in spec.global_constraints:
                        raise
                    pass

        todo_ids = tuple(item.todo.todo_id for item in result.work_items)
        fingerprint = stable_sha256({
            "goal_id": spec.goal_id,
            "root_task_sha256": spec.root_task_sha256,
            "work_items": [item.to_dict() for item in result.work_items],
        })
        observation = PlanObservationV1(
            plan_id=spec.goal_id,
            todo_ids=todo_ids,
            fingerprint=fingerprint,
            summary=result.goal.objective if result.goal else spec.raw_objective,
        )
        if self.plan_registry is not None:
            self.plan_registry.save_plan(spec, result, observation)
        return observation

    def ensure_initial_plan(self, spec: MissionSpecV1) -> PlanObservationV1:
        if not isinstance(spec, MissionSpecV1):
            raise TypeError("spec must be MissionSpecV1")
        from harness.scenarios.scope_evidence import SCOPE_REQUIREMENTS
        from harness.verification.callable_authority import PublicCallableAuthorityV1, INGRESS
        graph = RequiredDependencyGraphV1.admit(spec)
        authority = PublicCallableAuthorityV1.admit(spec, graph)
        initial_request = (self.request_factory(spec)
                           if SCOPE_REQUIREMENTS in spec.global_constraints else None)
        if self.plan_registry is not None:
            existing = self.plan_registry.load_plan(spec)
            if existing is not None:
                if initial_request is not None:
                    from harness.scenarios.scope_evidence import SCOPE_EVIDENCE, canonical, digest
                    registry = getattr(self.plan_registry, "registry", self.plan_registry)
                    store = getattr(registry, "artifact_store", None)
                    key = "planning-scope-" + digest(spec.mission_id + ":" + spec.goal_id) + "-dispatch"
                    if (store is None or not store.has_artifact(key)
                            or store.get_artifact(key).content != canonical(initial_request.constraints[SCOPE_EVIDENCE])):
                        raise PlanningContractError("PLANNER_SCOPE_EVIDENCE_INVALID", reason="accepted plan scope provenance mismatch")
                return existing

        initial_request = initial_request or self.request_factory(spec)
        if authority is not None:
            initial_request = replace(initial_request,constraints={**initial_request.constraints,
                INGRESS: authority.to_dict()['fingerprint']})
        if graph is not None:
            from harness.scenarios.scope_evidence import canonical
            if canonical(initial_request.constraints.get(REQUIRED_GRAPH)) != graph.document:
                raise PlanningContractError("PLANNING_REQUIRED_GRAPH_CONTRACT_INVALID", reason="request differs from trusted Mission contract")
        typed = spec.global_constraints.get("TYPED_SCENARIO_EXECUTION", False)
        if type(typed) is not bool:
            raise ValueError("TYPED_SCENARIO_EXECUTION must be bool")
        if SCOPE_REQUIREMENTS in spec.global_constraints and not typed:
            raise PlanningContractError("PLANNER_SCOPE_EVIDENCE_INVALID", reason="scope contract requires typed classification")
        if initial_request.constraints.get('NATIVE_MULTIFILE_SOURCE_LIMIT') == 2 and not typed:
            raise PlanningContractError('QUALIFIED_EXECUTION_SHAPE_INVALID', reason='native multifile requires typed selection and proof')
        if typed:
            if graph is not None:
                raise PlanningContractError("PLANNING_REQUIRED_GRAPH_CONTRACT_INVALID", reason="required logical graph needs legacy DAG planner")
            return self._typed_initial_plan(spec, initial_request)
        current_request = initial_request

        # Determine original max_lead_calls
        config = getattr(self.ornith_lead, "config", None)
        lead_cfg = config.get("lead", {}) if isinstance(config, dict) else {}
        max_lead_calls = int(lead_cfg.get("max_lead_calls", 3))
        budget = PlanningCallBudget(max_calls=max_lead_calls)

        runtime = getattr(self.ornith_lead, "runtime", None)
        policies = getattr(self.ornith_lead, "policies", None)
        production_store = getattr(self.ornith_lead, "store", None)
        registry = getattr(self.plan_registry, "registry", self.plan_registry)
        evidence_store = getattr(registry, "artifact_store", None)
        evidence_runtime = None
        if runtime is not None:
            evidence_runtime = PlanningEvidenceRuntime(runtime, evidence_store, spec, graph, lead_cfg)
            runtime = evidence_runtime

        last_decision = None
        last_result = None

        while budget.has_remaining():
            # Create a FRESH transient InMemoryRunStore for each semantic candidate attempt
            transient_store = InMemoryRunStore()

            trace_attempt = None
            attempt_runtime = runtime
            if self.planning_trace is not None and runtime is not None:
                trace_attempt = self.planning_trace.begin_attempt(spec, current_request, budget.calls_remaining, runtime)
                attempt_runtime = trace_attempt

            if attempt_runtime is not None and config is not None and policies is not None:
                attempt_config = {
                    **config,
                    "lead": {
                        **lead_cfg,
                        "max_lead_calls": budget.calls_remaining,
                    },
                }
                lead_instance = type(self.ornith_lead)(
                    store=transient_store,
                    runtime=attempt_runtime,
                    config=attempt_config,
                    policies=policies,
                )
            else:
                lead_instance = self.ornith_lead

            result = lead_instance.decompose(request=current_request, goal_id=spec.goal_id)
            last_result = result

            calls_made = result.lead_call_count if result.lead_call_count > 0 else 1
            budget.consume(calls_made)

            if result.outcome is not LeadOutcome.PLAN_READY or result.goal is None or not result.work_items:
                if trace_attempt is not None:
                    trace_attempt.finalize(result=result, budget_after=budget.calls_remaining)
                reason = result.attention_reason or "; ".join(result.warnings) or result.outcome.value
                if result.outcome is LeadOutcome.INVALID_PLAN:
                    error = PlanningContractError(result.model_telemetry.get("planning_error_category", "PLANNING_INVALID_PLAN"),
                        reason=result.model_telemetry.get("planning_first_error_reason", reason), final_reason=reason)
                    if evidence_runtime is not None:
                        evidence_runtime.retain_invalid(error.failure_diagnostic)
                    raise error
                raise RuntimeError(f"Lead did not produce a runnable plan: {reason}")
            if result.goal.goal_id != spec.goal_id:
                raise RuntimeError("Lead Goal identity does not match MissionSpecV1.goal_id")

            compiled_items = result.work_items
            shape_normalized_items = self.plan_shape_normalizer.normalize(current_request, compiled_items)
            normalized_items = self._normalize_work_items(shape_normalized_items)
            result = replace(result, work_items=normalized_items)

            # Precommit feasibility evaluation
            decision = self.feasibility_gate.evaluate(spec, result)
            last_decision = decision
            if trace_attempt is not None:
                trace_attempt.finalize(result=result, budget_after=budget.calls_remaining, compiled_items=compiled_items, normalized_items=normalized_items, decision=decision)

            if decision.decision is FeasibilityDecision.ACCEPT:
                # Commit accepted plan to production store & artifact store exactly once
                if graph is not None:
                    if evidence_runtime is None:
                        raise PlanningContractError("PLANNING_REQUIRED_GRAPH_CONTRACT_INVALID", reason="raw production runtime required")
                    evidence_runtime.qualify(result)
                try:
                    observation = self._commit_and_persist(spec, result, production_store)
                    if graph is not None:
                        retain(evidence_store, spec, "-committed", {"plan_fingerprint": observation.fingerprint})
                    return observation
                except (OSError, StoreIntegrityError) as exc:
                    # Preserve the actual persistence cause, never call it an invalid model DAG.
                    raise PlanningContractError("PLANNING_PERSISTENCE_FAILED", exception_type=type(exc).__name__, reason=str(exc)) from exc

            if decision.decision is FeasibilityDecision.SPLIT_REQUIRED:
                # Candidate is discarded (transient_store will be garbage collected)
                if budget.has_remaining():
                    current_request = build_refinement_request(current_request, result, decision)
                    continue
                break

            # Hard INVALID_PLAN -> fail closed
            raise RuntimeError(f"Plan feasibility rejected: {decision.reason}")

        # Budget exhausted without ACCEPT -> fail closed
        reason = (
            f"Planning call budget exhausted ({budget.max_calls} calls). "
            f"Last decision: {last_decision.decision.value if last_decision else 'UNKNOWN'} - "
            f"{last_decision.reason if last_decision else 'no valid candidate'}"
        )
        raise RuntimeError(f"Lead plan failed feasibility validation: {reason}")

    def validate_committed_plan(self, spec, expected):
        """Restart inspection only: never dispatch planning or rebuild a lost plan."""
        RequiredDependencyGraphV1.admit(spec)
        actual = self.plan_registry.load_plan(spec) if self.plan_registry is not None else None
        if actual is None or actual != expected:
            raise PlanningContractError("PLANNING_REQUIRED_GRAPH_MISMATCH", reason="committed Mission/registry plan mismatch")

    def _typed_initial_plan(self, spec, request):
        """Explicit opt-in; reuse the durable plan and feasibility authorities."""
        from harness.scenarios.selector import OrnithScenarioSelectorV1
        from harness.scenarios.projector import ScenarioPlanProjectorV1
        from harness.scenarios.scope_evidence import (
            SCOPE_REQUIREMENTS, SCOPE_EVIDENCE, PlanningScopeEvidenceV1,
            PlanningContractError, canonical, digest,
        )

        evidence = None
        record_response = None
        native_multifile = request.constraints.get("NATIVE_MULTIFILE_SOURCE_LIMIT") == 2
        if native_multifile and SCOPE_REQUIREMENTS not in spec.global_constraints:
            raise PlanningContractError("PLANNER_SCOPE_EVIDENCE_INVALID", reason="native multifile scope manifest missing")
        if SCOPE_REQUIREMENTS in spec.global_constraints:
            evidence = PlanningScopeEvidenceV1(canonical(request.constraints.get(SCOPE_EVIDENCE)))
            evidence.validate_binding(spec)
            registry = getattr(self.plan_registry, "registry", self.plan_registry)
            store = getattr(registry, "artifact_store", None)
            if store is None:
                raise PlanningContractError("PLANNER_SCOPE_EVIDENCE_INVALID", reason="durable planning evidence store required")
            key = "planning-scope-" + digest(spec.mission_id + ":" + spec.goal_id)
            if store.has_artifact(key + "-dispatch"):
                raise PlanningContractError("PLANNER_DISPATCH_AMBIGUOUS", reason="reserved planning dispatch cannot be replayed")
            from harness.context_manager import Artifact, ArtifactKind, ArtifactOrigin

            def retain(suffix, payload):
                store.create_artifact(Artifact.from_text(
                    artifact_id=key + suffix, kind=ArtifactKind.CONTROL_PLANE_EVIDENCE,
                    origin=ArtifactOrigin.CONTROL_PLANE, content=canonical(payload),
                    media_type="application/json", source="ProductionPlanningScope",
                    created_at="1970-01-01T00:00:00+00:00",
                    scope_tags=(spec.mission_id, spec.goal_id),
                ))

            retain("-dispatch", evidence.to_dict())
            recorded = []

            def record_response(content):
                if not isinstance(content, str):
                    raise PlanningContractError("PLANNER_RESPONSE_INVALID", reason="missing response text")
                raw = content.encode("utf-8")
                retain("-response", {"raw_response": raw[:65536].decode("utf-8", errors="ignore"),
                       "sha256": digest(content), "bytes": len(raw), "truncated": len(raw) > 65536,
                       "scope_fingerprint": evidence.to_dict()["fingerprint"]})
                if len(raw) > 65536:
                    raise PlanningContractError("PLANNER_RESPONSE_INVALID", reason="response exceeds bound")
                recorded.append(content)

        try:
            selected = OrnithScenarioSelectorV1(self.ornith_lead.runtime).select(
                request.objective, research_required=request.constraints.get("research_required"),
                scope_evidence=evidence, record_response=record_response)
        except PlanningContractError:
            raise
        except Exception as exc:
            if evidence is not None:
                raise PlanningContractError("PLANNER_SELECTION_FAILED", reason=str(exc)) from exc
            raise
        if evidence is not None:
            import json
            if (len(recorded) != 1 or selected.selection.to_dict() != json.loads(recorded[0])
                    or selected.compiled.selection != selected.selection):
                raise PlanningContractError("PLANNER_RAW_SELECTION_CHANGED", reason="classification differs from retained raw response")
            evidence.validate_selection(selected.selection)
        shape = None
        if native_multifile:
            from harness.scenarios.qualified_shape import qualify, SHAPE
            fresh = PlanningScopeEvidenceV1.collect(spec, request.metadata["scope_workspace"])
            if fresh.document != evidence.document:
                raise PlanningContractError("PLANNER_SCOPE_EVIDENCE_INVALID", reason="scope changed during PLAN")
            shape = qualify(selected.selection, evidence, request)
            request = replace(request, constraints={**dict(request.constraints), SHAPE: shape.to_dict()})
        projection = ScenarioPlanProjectorV1().project(
            scenario=selected.compiled, request=request, goal_id=spec.goal_id,
            policies=self.ornith_lead.policies, required_gates=self.required_gates,
        )
        if shape is not None:
            for item in projection.work_items:
                shape.validate_item(item)
            retain("-qualification", {"status": "PASS", **shape.to_dict()})
        result = LeadResult(
            LeadOutcome.PLAN_READY, projection.goal, projection.work_items, 1,
            model_telemetry={"typed_scenario": selected.to_dict(),
                             "projection_fingerprint": projection.fingerprint},
        )
        decision = self.feasibility_gate.evaluate(spec, result)
        if decision.decision is not FeasibilityDecision.ACCEPT:
            raise RuntimeError(f"Typed scenario feasibility rejected: {decision.reason}")
        return self._commit_and_persist(spec, result, self.ornith_lead.store)

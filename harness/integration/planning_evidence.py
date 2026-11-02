"""Bounded raw PLAN receipts, separate from privacy-minimized PlanningTrace."""
from __future__ import annotations

import json
from harness.context_manager import Artifact, ArtifactKind, ArtifactOrigin
from harness.lead.parser import parse_plan, LeadValidationError
from harness.lead.required_graph import RequiredDependencyGraphV1
from harness.scenarios.scope_evidence import PlanningContractError, canonical, digest
from harness.mission.models import stable_sha256


def evidence_key(spec):
    return "planning-graph-" + digest(spec.mission_id + ":" + spec.goal_id)


def retain(store, spec, suffix, value):
    store.create_artifact(Artifact.from_text(
        artifact_id=evidence_key(spec) + suffix, kind=ArtifactKind.CONTROL_PLANE_EVIDENCE,
        origin=ArtifactOrigin.CONTROL_PLANE, content=canonical(value), media_type="application/json",
        source="ProductionPlanningGraphEvidence", created_at="1970-01-01T00:00:00+00:00",
        scope_tags=(spec.mission_id, spec.goal_id)))


class PlanningEvidenceRuntime:
    def __init__(self, inner, store, spec, graph, config):
        self.inner, self.store, self.spec, self.graph, self.config = inner, store, spec, graph, config
        self.calls = 0
        self.responses = []
        self.plan = None
        if graph and store is not None:
            if store.has_artifact(evidence_key(spec) + "-dispatch"):
                raise PlanningContractError("PLANNING_DISPATCH_AMBIGUOUS", reason="uncommitted planning dispatch cannot be replayed")
            retain(store, spec, "-dispatch", {"contract": graph.to_dict() if graph else None,
                    "root_task_sha256": spec.root_task_sha256})
        elif graph:
            raise PlanningContractError("PLANNING_REQUIRED_GRAPH_CONTRACT_INVALID", reason="durable evidence store required")

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def complete(self, system, user, schema=None):
        response = self.inner.complete(system, user, schema)
        self.calls += 1
        raw = response.content
        data = raw.encode("utf-8")
        receipt = {"raw_response": data[:65536].decode("utf-8", errors="ignore"),
                   "sha256": digest(raw), "bytes": len(data), "truncated": len(data) > 65536}
        if self.graph:
            retain(self.store, self.spec, f"-response-{self.calls}", receipt)
        else:
            # Legacy semantic candidates retain their zero-precommit-write contract.
            # Publish these bounded receipts only on final typed INVALID_PLAN.
            self.responses.append(receipt)
        if self.graph:
            if len(raw.encode("utf-8")) > 65536:
                raise PlanningContractError("PLANNING_INVALID_PLAN", reason="raw PLAN exceeds retained evidence bound")
            try:
                self.plan = parse_plan(raw, self.config.get("schema_version", 1))
                order = self.graph.validate_plan(self.plan, self.config.get("max_todos", 12),
                                                 self.config.get("max_dependencies_per_todo", 6))
            except LeadValidationError as exc:
                raise PlanningContractError("PLANNING_INVALID_PLAN", reason=str(exc)) from exc
            except PlanningContractError as exc:
                retain(self.store, self.spec, f"-decision-{self.calls}", exc.failure_diagnostic)
                raise
            retain(self.store, self.spec, f"-decision-{self.calls}", {"decision": "VALID_LOGICAL_GRAPH", "order": order})
        return response

    def retain_invalid(self, diagnostic):
        if self.graph is None and self.store is not None:
            retain(self.store, self.spec, "-dispatch", {"contract": None, "root_task_sha256": self.spec.root_task_sha256})
            for i, response in enumerate(self.responses, 1):
                retain(self.store, self.spec, f"-response-{i}", response)
            retain(self.store, self.spec, "-decision", diagnostic)

    def qualify(self, result):
        mapping = self.graph.validate_compiled(self.plan, result.work_items, self.spec.goal_id)
        retain(self.store, self.spec, "-qualification", {
            "contract": self.graph.to_dict(), "raw_call": self.calls, "mapping": mapping,
            "work_items_fingerprint": stable_sha256([item.to_dict() for item in result.work_items])})


def verify_restored_graph(registry, spec, observation):
    """Validate before any caller can obtain a usable committed graph."""
    graph = RequiredDependencyGraphV1.admit(spec)
    if graph is None:
        return
    store = registry.artifact_store
    key = evidence_key(spec)
    for suffix in ("-dispatch", "-qualification", "-committed"):
        if not store.has_artifact(key + suffix):
            raise PlanningContractError("PLANNING_DISPATCH_AMBIGUOUS", reason="incomplete required graph commit", missing=suffix)
    proof = json.loads(store.get_artifact(key + "-qualification").content)
    dispatch = json.loads(store.get_artifact(key + "-dispatch").content)
    committed = json.loads(store.get_artifact(key + "-committed").content)
    if (proof["contract"] != graph.to_dict() or dispatch["contract"] != graph.to_dict()
            or committed["plan_fingerprint"] != observation.fingerprint):
        raise PlanningContractError("PLANNING_REQUIRED_GRAPH_CONTRACT_INVALID", reason="restored graph provenance mismatch")
    receipt = json.loads(store.get_artifact(key + f"-response-{proof['raw_call']}").content)
    if receipt["truncated"] or digest(receipt["raw_response"]) != receipt["sha256"]:
        raise PlanningContractError("PLANNING_REQUIRED_GRAPH_CONTRACT_INVALID", reason="raw PLAN integrity mismatch")
    from .work_item_registry import _work_item, _work_item_artifact_id
    rows = [json.loads(store.get_artifact(_work_item_artifact_id(spec.goal_id, todo)).content) for todo in observation.todo_ids]
    if stable_sha256(rows) != proof["work_items_fingerprint"]:
        raise PlanningContractError("PLANNING_REQUIRED_GRAPH_MISMATCH", reason="restored work-item fingerprint mismatch")
    parsed = parse_plan(receipt["raw_response"], 1)
    mapping = graph.validate_compiled(parsed, tuple(_work_item(row) for row in rows), spec.goal_id)
    if mapping != proof["mapping"]:
        raise PlanningContractError("PLANNING_REQUIRED_GRAPH_MISMATCH", reason="restored logical mapping mismatch")

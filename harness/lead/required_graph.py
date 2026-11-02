"""Caller-admitted dependency requirements, never inferred from model text."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping

from harness.scenarios.scope_evidence import PlanningContractError, canonical, digest
from harness.control_plane.todo_identity import make_todo_id
from .validator import topological_keys
from .parser import LeadValidationError

REQUIRED_GRAPH = "REQUIRED_DEPENDENCY_GRAPH_V1"


def fail(reason, **details):
    raise PlanningContractError("PLANNING_REQUIRED_GRAPH_MISMATCH", reason=reason, **details)


def paths(values):
    if not isinstance(values, (tuple, list)):
        raise ValueError("scope must be a list")
    result = tuple(values)
    for p in result:
        if (not isinstance(p, str) or not p or p != p.strip() or "\\" in p
                or p.startswith("/") or any(x in {"", ".", ".."} for x in p.split("/"))
                or any(c in p for c in ":*?[") or p.split("/")[0] == ".git"):
            raise ValueError("scope must contain canonical relative paths")
    if len(set(result)) != len(result):
        raise ValueError("duplicate scope path")
    return result


def covers(parent, child):
    # The supported native Windows boundary cannot assign case aliases to owners.
    parent, child = parent.casefold(), child.casefold()
    return child == parent or child.startswith(parent + "/")


@dataclass(frozen=True, slots=True)
class RequiredTodoV1:
    logical_id: str
    dependencies: tuple[str, ...]
    source_scope: tuple[str, ...]
    test_scope: tuple[str, ...]
    additional_scope: tuple[str, ...]
    mutation_required: bool


@dataclass(frozen=True, slots=True)
class RequiredDependencyGraphV1:
    """Immutable canonical document and typed nodes, bound to admitted Mission."""
    document: str
    todos: tuple[RequiredTodoV1, ...]
    minimum_todo_count: int
    exact_todo_count: int | None
    exact_topology: bool
    scope_widening: str

    @classmethod
    def admit(cls, spec):
        if REQUIRED_GRAPH not in spec.global_constraints:
            return None
        try:
            raw = spec.global_constraints[REQUIRED_GRAPH]
            keys = {"schema_version", "mission_id", "goal_id", "root_task_sha256", "source",
                    "source_fingerprint", "fingerprint", "todos", "minimum_todo_count",
                    "exact_todo_count", "exact_topology", "scope_widening"}
            if not isinstance(raw, Mapping) or set(raw) != keys:
                raise ValueError("contract fields mismatch")
            if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
                raise ValueError("schema_version must be integer 1")
            if (raw["mission_id"], raw["goal_id"], raw["root_task_sha256"]) != (spec.mission_id, spec.goal_id, spec.root_task_sha256):
                raise ValueError("mission binding mismatch")
            if not isinstance(raw["source"], str) or not raw["source"].strip():
                raise ValueError("trusted source descriptor required")
            for name in ("source_fingerprint", "fingerprint"):
                if not isinstance(raw[name], str) or not re.fullmatch(r"[a-f0-9]{64}", raw[name]):
                    raise ValueError("invalid " + name)
            body = {k: v for k, v in raw.items() if k != "fingerprint"}
            if digest(canonical(body)) != raw["fingerprint"]:
                raise ValueError("contract fingerprint mismatch")
            minimum, exact = raw["minimum_todo_count"], raw["exact_todo_count"]
            if type(minimum) is not int or not 1 <= minimum <= 12:
                raise ValueError("invalid minimum Todo count")
            if exact is not None and (type(exact) is not int or not minimum <= exact <= 12):
                raise ValueError("invalid exact Todo count")
            if type(raw["exact_topology"]) is not bool or raw["scope_widening"] not in {"FORBID", "DECLARED_ONLY"}:
                raise ValueError("invalid topology/scope policy")
            rows = raw["todos"]
            if not isinstance(rows, (tuple, list)) or not 1 <= len(rows) <= minimum:
                raise ValueError("invalid required Todos")
            nodes = []
            for row in rows:
                if set(row) != {"logical_id", "dependencies", "source_scope", "test_scope", "additional_scope", "mutation_required"}:
                    raise ValueError("required Todo fields mismatch")
                key = row["logical_id"]
                if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", key):
                    raise ValueError("invalid logical identity")
                deps = row["dependencies"]
                if not isinstance(deps, (list, tuple)) or any(not isinstance(x, str) for x in deps) or len(set(deps)) != len(deps):
                    raise ValueError("invalid dependency set")
                if type(row["mutation_required"]) is not bool:
                    raise ValueError("mutation_required must be bool")
                node = RequiredTodoV1(key, tuple(deps), paths(row["source_scope"]), paths(row["test_scope"]),
                                      paths(row["additional_scope"]), row["mutation_required"])
                if not node.source_scope or not node.test_scope:
                    raise ValueError("separate source/test responsibility required")
                if raw["scope_widening"] == "FORBID" and node.additional_scope:
                    raise ValueError("additional scope forbidden")
                nodes.append(node)
            by_id = {n.logical_id: n for n in nodes}
            if len(by_id) != len(nodes):
                raise ValueError("duplicate required identity")
            if raw["exact_topology"] and (minimum != len(nodes) or exact != len(nodes)):
                raise ValueError("exact topology requires exact required identity count")
            pending = {n.logical_id: set(n.dependencies) for n in nodes}
            if any(k in deps or not deps <= by_id.keys() for k, deps in pending.items()):
                raise ValueError("invalid required edge")
            while pending:
                ready = {k for k, deps in pending.items() if not deps}
                if not ready:
                    raise ValueError("required graph cycle")
                pending = {k: deps - ready for k, deps in pending.items() if k not in ready}
            owned = [(n.logical_id, p) for n in nodes for p in (*n.source_scope, *n.test_scope, *n.additional_scope)]
            for i, (key, p) in enumerate(owned):
                for other, q in owned[i + 1:]:
                    if covers(p, q) or covers(q, p):
                        raise ValueError("overlapping mutation responsibilities")
            return cls(canonical(raw), tuple(nodes), minimum, exact, raw["exact_topology"], raw["scope_widening"])
        except (TypeError, ValueError, KeyError) as exc:
            raise PlanningContractError("PLANNING_REQUIRED_GRAPH_CONTRACT_INVALID", reason=str(exc)) from exc

    def to_dict(self):
        import json
        return json.loads(self.document)

    def validate_plan(self, plan, max_todos=12, max_dependencies=6):
        try:
            order = topological_keys(plan, max_todos, max_dependencies)
        except LeadValidationError as exc:
            raise PlanningContractError("PLANNING_INVALID_DEPENDENCY_DAG", reason=str(exc)) from exc
        by_key = {t.key: t for t in plan.todos}
        required = {t.logical_id for t in self.todos}
        if (not required <= by_key.keys() or len(by_key) < self.minimum_todo_count
                or self.exact_todo_count is not None and len(by_key) != self.exact_todo_count
                or self.exact_topology and set(by_key) != required):
            fail("required Todo identity/count mismatch", expected=sorted(required), observed=sorted(by_key))
        for node in self.todos:
            todo = by_key[node.logical_id]
            expected = set(node.dependencies)
            actual = set(todo.dependencies)
            if not expected <= actual or self.exact_topology and actual != expected:
                fail("required dependency edge mismatch", todo=node.logical_id, expected=sorted(expected), observed=sorted(actual))
            self.validate_scope(node, todo.allowed_scope)
        for todo in plan.todos:
            if todo.key not in required:
                # Extra units may not take over required units' paths, even by ancestor scope.
                try:
                    extra_scope = paths(todo.allowed_scope)
                except ValueError as exc:
                    fail("extra Todo scope is not canonical", todo=todo.key, detail=str(exc))
                for p in extra_scope:
                    for node in self.todos:
                        if any(covers(p, q) or covers(q, p) for q in (*node.source_scope, *node.test_scope, *node.additional_scope)):
                            fail("extra Todo claims required mutation responsibility", todo=todo.key)
        return order

    def validate_scope(self, node, allowed):
        required = set((*node.source_scope, *node.test_scope))
        maximum = required | set(node.additional_scope)
        if not required <= set(allowed) or not set(allowed) <= maximum:
            fail("source/test scope ownership mismatch", todo=node.logical_id, expected=sorted(required), maximum=sorted(maximum), observed=sorted(allowed))

    def validate_compiled(self, plan, items, goal_id):
        order = self.validate_plan(plan)
        mapping = {key: make_todo_id(goal_id, i) for i, key in enumerate(order, 1)}
        if tuple(item.todo_id if hasattr(item, "todo_id") else item.todo.todo_id for item in items) != tuple(mapping[k] for k in order):
            fail("compiled logical/physical identity mismatch")
        required = {n.logical_id: n for n in self.todos}
        raw = {t.key: t for t in plan.todos}
        for key, item in zip(order, items):
            todo = getattr(item, "todo", item)
            contract = item.work_contract
            if todo.objective != raw[key].objective or contract.objective != raw[key].objective:
                fail("compiled objective differs from raw PLAN", todo=key)
            if set(todo.dependencies) != {mapping[k] for k in raw[key].dependencies}:
                fail("compiled dependency mismatch", todo=key)
            if contract.goal_id != goal_id or contract.todo_id != mapping[key]:
                fail("compiled contract identity mismatch", todo=key)
            if tuple(contract.allowed_scope) != tuple(raw[key].allowed_scope):
                fail("compilation widened or replaced declared scope", todo=key)
            if key in required:
                node = required[key]
                self.validate_scope(node, contract.allowed_scope)
                if node.mutation_required and contract.constraints.get("REQUIRES_WORKSPACE_MUTATION") is not True:
                    fail("required mutation responsibility missing", todo=key)
        return mapping

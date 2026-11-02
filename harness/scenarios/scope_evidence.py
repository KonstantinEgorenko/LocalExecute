"""Bounded, caller-authorized scope facts; never infer mutation requirements.

Declarations originate in MissionSpec, not a model response. Source facts are
read through Workspace authorization and bound to the mission and original
bytes. Neither allowed paths nor API/CAS counts establish required mutations.
"""
from __future__ import annotations

import ast
import hashlib
import json
from dataclasses import dataclass
from typing import Mapping

from harness.workspace import Workspace
from harness.workspace.models import WorkspaceAccess, WorkspacePolicy, WorkspaceSession
from harness.workspace.workspace import normalize_relative_path

SCOPE_REQUIREMENTS = "PLANNING_SCOPE_REQUIREMENTS_V1"
SCOPE_EVIDENCE = "PLANNING_SCOPE_EVIDENCE_V1"
MAX_CONTEXT_BYTES = 32768


def plain(value):
    if isinstance(value, Mapping):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    return value


def canonical(value):
    return json.dumps(plain(value), sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class PlanningContractError(RuntimeError):
    def __init__(self, category, **details):
        self.failure_diagnostic = {"category": category, "decision": "FAIL_CLOSED", **details}
        super().__init__(canonical(self.failure_diagnostic))


@dataclass(frozen=True, slots=True)
class PlanningScopeEvidenceV1:
    """Canonical immutable document, validated against current read-only facts."""
    document: str

    def to_dict(self):
        return json.loads(self.document)

    @classmethod
    def collect(cls, spec, workspace):
        declaration = plain(spec.global_constraints[SCOPE_REQUIREMENTS])
        expected_keys = {"objective", "allowed_paths", "required_source_paths",
                         "regression_paths", "source_hashes", "authority"}
        try:
            if set(declaration) != expected_keys or declaration["objective"] != spec.raw_objective:
                raise ValueError("scope declaration fields/objective mismatch")
            authority = declaration["authority"]
            if not isinstance(authority, str) or not authority.strip() or len(authority.encode()) > 8192:
                raise ValueError("explicit authority required")
            paths = {}
            for key in ("allowed_paths", "required_source_paths", "regression_paths"):
                values = declaration[key]
                if not isinstance(values, list) or len(values) > 16:
                    raise ValueError("scope path bound")
                normalized = tuple(normalize_relative_path(p) for p in values)
                if list(normalized) != values or len(set(normalized)) != len(values):
                    raise ValueError("noncanonical/duplicate scope paths")
                paths[key] = normalized
            allowed = paths["allowed_paths"]
            if not set((*paths["required_source_paths"], *paths["regression_paths"])) <= set(allowed):
                raise ValueError("required paths outside declared allowance")
            if any(not p.endswith(".py") or p.startswith("tests/") for p in paths["required_source_paths"]):
                raise ValueError("required source paths must identify source modules")
            hashes = declaration["source_hashes"]
            if not isinstance(hashes, dict) or set(hashes) != set(allowed):
                raise ValueError("source provenance coverage mismatch")
            session = WorkspaceSession("planning-scope", spec.mission_id, spec.goal_id, str(workspace),
                WorkspacePolicy(access=WorkspaceAccess.READ_ONLY, allowed_paths=allowed))
            reader = Workspace(session)
            trees = {}; total = 0; canonical_paths = {}
            for path in allowed:
                _, text = reader.read_text(path)
                _, target = reader._candidate(path)
                canonical_paths[path] = target.relative_to(reader.root).as_posix()
                source_bytes = target.read_bytes()
                total += len(source_bytes)
                if total > MAX_CONTEXT_BYTES or hashlib.sha256(source_bytes).hexdigest() != hashes[path]:
                    raise ValueError("stale source hash or source bound exceeded: " + path)
                if path.endswith(".py"):
                    trees[path] = ast.parse(text)
            bindings = [(p, node.name) for p, tree in trees.items() for node in tree.body
                        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
            edges = []
            for path, tree in trees.items():
                for node in tree.body:
                    if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                        dependency = node.module.replace(".", "/") + ".py"
                        for alias in node.names:
                            if (dependency, alias.name) not in bindings:
                                continue
                            for function in tree.body:
                                if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                                    local = {n.id for n in ast.walk(function)
                                             if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
                                    local.update(n.arg for n in ast.walk(function) if isinstance(n, ast.arg))
                                    if (alias.asname or alias.name) in local:
                                        continue
                                    if any(isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                                           and call.func.id == (alias.asname or alias.name)
                                           for call in ast.walk(function)):
                                        edges.append([path, function.name, dependency, alias.name])
            body = {"version": 1, "mission_id": spec.mission_id, "goal_id": spec.goal_id,
                    "root_task_sha256": spec.root_task_sha256, "objective": spec.raw_objective,
                    **{k: list(v) for k, v in paths.items()}, "source_hashes": hashes,
                    "authority": authority, "authority_sha256": digest(authority),
                    "declaration_sha256": digest(canonical(declaration)),
                    "canonical_paths": canonical_paths, "source_bytes": total,
                    "callable_bindings": bindings, "verified_dependency_edges": edges}
            body["fingerprint"] = digest(canonical(body))
            document = canonical(body)
            if len(document.encode("utf-8")) > MAX_CONTEXT_BYTES:
                raise ValueError("scope evidence exceeds bound")
            return cls(document)
        except Exception as exc:
            raise PlanningContractError("PLANNER_SCOPE_EVIDENCE_INVALID", reason=str(exc)) from exc

    def validate_binding(self, spec):
        body = self.to_dict()
        if not isinstance(body, dict):
            raise PlanningContractError("PLANNER_SCOPE_EVIDENCE_INVALID", reason="missing scope manifest")
        fp = body.pop("fingerprint", None)
        if (fp != digest(canonical(body)) or body.get("mission_id") != spec.mission_id
                or body.get("goal_id") != spec.goal_id or body.get("root_task_sha256") != spec.root_task_sha256
                or body.get("declaration_sha256") != digest(canonical(spec.global_constraints[SCOPE_REQUIREMENTS]))):
            raise PlanningContractError("PLANNER_SCOPE_EVIDENCE_INVALID", reason="stale/cross-mission manifest")

    def validate_selection(self, selection):
        body = self.to_dict()
        # Only explicitly required, verified source mutations establish this contract.
        if len(body["required_source_paths"]) >= 2 and (
                selection.scenario.value not in ("MULTI_FILE_CHANGE", "BUGFIX_WITH_REGRESSION") or selection.scope.value != "MULTI_FILE"):
            raise PlanningContractError("PLANNER_SCENARIO_SCOPE_INCOMPATIBLE",
                expected={"scenario": "MULTI_FILE_CHANGE", "scope": "MULTI_FILE"},
                observed={"scenario": selection.scenario.value, "scope": selection.scope.value},
                scope_fingerprint=body["fingerprint"])
        if body["regression_paths"] and not selection.requires_regression_change:
            raise PlanningContractError("PLANNER_SCENARIO_SCOPE_INCOMPATIBLE",
                expected={"requires_regression_change": True},
                observed={"requires_regression_change": selection.requires_regression_change})

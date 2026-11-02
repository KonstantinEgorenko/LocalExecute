"""Trusted verification policy and deterministic plan compilation."""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from ..control_plane.models import Run, Todo, WorkContract
from ..workspace.workspace import normalize_relative_path
from ..worktree.models import WorktreeLease
from .errors import VerificationPolicyError
from .models import (
    BuiltinGate,
    CommandCatalogEntry,
    FileAssertionMode,
    VerificationGateKind,
    VerificationGateSpec,
    VerificationGateType,
    VerificationPlan,
)


_BUILTINS: dict[str, tuple[BuiltinGate, VerificationGateType]] = {
    BuiltinGate.CANDIDATE_DIFF.value: (BuiltinGate.CANDIDATE_DIFF, VerificationGateType.DIFF),
    BuiltinGate.SCOPE_GUARD.value: (BuiltinGate.SCOPE_GUARD, VerificationGateType.SCOPE),
    BuiltinGate.PYTHON_SYNTAX.value: (BuiltinGate.PYTHON_SYNTAX, VerificationGateType.SYNTAX),
    BuiltinGate.REGRESSION_CHANGE.value: (BuiltinGate.REGRESSION_CHANGE, VerificationGateType.TESTS),
    **{g.value: (g, VerificationGateType.TESTS) for g in (
        BuiltinGate.REGRESSION_PRESERVATION, BuiltinGate.IMMUTABLE_BASELINE_TESTS, BuiltinGate.REGRESSION_ORACLE)},
}

_GATE_ORDER = {
    VerificationGateType.DIFF: 10,
    VerificationGateType.SCOPE: 20,
    VerificationGateType.SYNTAX: 30,
    VerificationGateType.POSTCONDITION: 40,
    VerificationGateType.TESTS: 50,
    VerificationGateType.LINT: 60,
    VerificationGateType.TYPE_CHECK: 70,
    VerificationGateType.BUILD: 80,
    VerificationGateType.CUSTOM: 90,
}


@dataclass(frozen=True, slots=True)
class VerificationPolicy:
    trusted_worktrees_only: bool = True
    require_candidate_dirty: bool = True
    fail_fast: bool = False
    max_gates: int = 16
    max_gate_timeout_seconds: int = 600
    max_total_timeout_seconds: int = 1800
    max_output_bytes: int = 256 * 1024
    max_source_bytes: int = 2 * 1024 * 1024
    trusted_python_executable: str | None = None
    command_catalog: Mapping[str, CommandCatalogEntry] = field(
        default_factory=lambda: MappingProxyType({})
    )

    def __post_init__(self) -> None:
        if self.trusted_worktrees_only is not True:
            raise ValueError("trusted_worktrees_only must remain true in Phase 7")
        for name in ("require_candidate_dirty", "fail_fast"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be bool")
        for name in (
            "max_gates", "max_gate_timeout_seconds", "max_total_timeout_seconds", "max_output_bytes", "max_source_bytes"
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be positive integer")
        if self.max_gate_timeout_seconds > self.max_total_timeout_seconds:
            raise ValueError("max_gate_timeout_seconds must not exceed max_total_timeout_seconds")
        if self.trusted_python_executable is not None:
            if not isinstance(self.trusted_python_executable, str) or not self.trusted_python_executable.strip():
                raise ValueError("trusted_python_executable must be absolute path string or None")
            trusted_python = Path(self.trusted_python_executable).expanduser()
            if not trusted_python.is_absolute():
                raise ValueError("trusted_python_executable must be absolute")
            object.__setattr__(self, "trusted_python_executable", str(trusted_python))
        if not isinstance(self.command_catalog, Mapping):
            raise ValueError("command_catalog must be mapping")
        normalized: dict[str, CommandCatalogEntry] = {}
        for key, entry in self.command_catalog.items():
            if not isinstance(key, str) or not key.strip() or not isinstance(entry, CommandCatalogEntry):
                raise ValueError("command_catalog must map non-empty IDs to CommandCatalogEntry")
            if key != entry.catalog_id:
                raise ValueError("command_catalog key must equal entry.catalog_id")
            if entry.timeout_seconds > self.max_gate_timeout_seconds:
                raise ValueError("catalog timeout exceeds max_gate_timeout_seconds")
            first = entry.argv[0]
            if first not in ("{python}", "{git}") and not Path(first).is_absolute():
                raise ValueError("catalog executable must be {python}, {git}, or an absolute path")
            normalized[key] = entry
        object.__setattr__(self, "command_catalog", MappingProxyType(normalized))

    @classmethod
    def from_policies(
        cls, policies: Mapping[str, Any], *, project_root: str | Path | None = None
    ) -> "VerificationPolicy":
        verification = policies.get("verification", {})
        if not isinstance(verification, Mapping):
            raise VerificationPolicyError("verification policy must be mapping")
        runtime = verification.get("runtime", {})
        if not isinstance(runtime, Mapping):
            raise VerificationPolicyError("verification.runtime must be mapping")
        raw_catalog = verification.get("command_catalog", {})
        if not isinstance(raw_catalog, Mapping):
            raise VerificationPolicyError("verification.command_catalog must be mapping")
        catalog: dict[str, CommandCatalogEntry] = {}
        try:
            for catalog_id, raw in raw_catalog.items():
                if not isinstance(catalog_id, str) or not isinstance(raw, Mapping):
                    raise ValueError("catalog entries must be mappings")
                argv = raw.get("argv")
                codes = raw.get("expected_exit_codes", [0])
                catalog[catalog_id] = CommandCatalogEntry(
                    catalog_id=catalog_id,
                    gate_type=VerificationGateType(str(raw["gate_type"]).upper()),
                    argv=tuple(str(item) for item in argv),
                    timeout_seconds=int(raw["timeout_seconds"]),
                    expected_exit_codes=tuple(int(item) for item in codes),
                )
            trusted = runtime.get("trusted_worktrees_only", True)
            dirty = runtime.get("require_candidate_dirty", True)
            fail_fast = runtime.get("fail_fast", False)
            if not isinstance(trusted, bool) or not isinstance(dirty, bool) or not isinstance(fail_fast, bool):
                raise ValueError("verification runtime boolean fields must be bool")
            raw_venv = runtime.get("trusted_python_venv")
            trusted_python_executable: str | None = None
            if raw_venv is not None:
                if not isinstance(raw_venv, str) or not raw_venv.strip():
                    raise ValueError("trusted_python_venv must be non-empty relative path")
                venv_rel = Path(raw_venv.strip())
                if venv_rel.is_absolute() or ".." in venv_rel.parts or str(venv_rel) in ("", "."):
                    raise ValueError("trusted_python_venv must be project-relative and must not escape project root")
                if project_root is None:
                    raise ValueError("project_root is required when trusted_python_venv is configured")
                # Keep the configured project-local venv path lexical.  In
                # particular, do not realpath/resolve the Windows venv launcher:
                # Store-derived venvs may redirect CreateFile/realpath to
                # WindowsApps even though CreateProcess must receive the literal
                # .venv\Scripts\python.exe path.
                root = Path(os.path.abspath(os.path.normpath(str(Path(project_root).expanduser()))))
                venv_root = Path(os.path.abspath(os.path.normpath(str(root / venv_rel))))
                try:
                    venv_root.relative_to(root)
                except ValueError as exc:
                    raise ValueError("trusted_python_venv escapes project root") from exc
                python_rel = Path("Scripts") / "python.exe" if os.name == "nt" else Path("bin") / "python"
                trusted_python_executable = str(Path(os.path.abspath(os.path.normpath(str(venv_root / python_rel)))))

            integer_fields = {
                "max_gates": runtime.get("max_gates", 16),
                "max_gate_timeout_seconds": runtime.get("max_gate_timeout_seconds", 600),
                "max_total_timeout_seconds": runtime.get("max_total_timeout_seconds", 1800),
                "max_output_bytes": runtime.get("max_output_bytes", 256 * 1024),
                "max_source_bytes": runtime.get("max_source_bytes", 2 * 1024 * 1024),
            }
            if any(not isinstance(value, int) or isinstance(value, bool) for value in integer_fields.values()):
                raise ValueError("verification runtime numeric fields must be integers")
            if any(entry.argv[0] == "{python}" for entry in catalog.values()) and trusted_python_executable is None:
                raise ValueError("trusted_python_venv is required when command catalog uses {python}")
            return cls(
                trusted_worktrees_only=trusted,
                require_candidate_dirty=dirty,
                fail_fast=fail_fast,
                max_gates=integer_fields["max_gates"],
                max_gate_timeout_seconds=integer_fields["max_gate_timeout_seconds"],
                max_total_timeout_seconds=integer_fields["max_total_timeout_seconds"],
                max_output_bytes=integer_fields["max_output_bytes"],
                max_source_bytes=integer_fields["max_source_bytes"],
                trusted_python_executable=trusted_python_executable,
                command_catalog=catalog,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise VerificationPolicyError(f"invalid verification policy: {exc}") from exc


class VerificationPlanCompiler:
    """Compile only named Control Plane gates into trusted deterministic specs.

    Command argv comes exclusively from the harness-owned command catalog.
    WorkContract may supply non-executable file assertions, but never commands.
    """

    def __init__(self, policy: VerificationPolicy) -> None:
        self.policy = policy

    def compile(
        self,
        *,
        run: Run,
        todo: Todo,
        contract: WorkContract,
        lease: WorktreeLease,
    ) -> VerificationPlan:
        if run.run_id != lease.run_id or todo.todo_id != lease.todo_id:
            raise VerificationPolicyError("Run/Todo and worktree lease identity mismatch")
        if run.active_todo_id != todo.todo_id or todo.goal_id != run.goal_id:
            raise VerificationPolicyError("Run active Todo identity mismatch")
        if contract.goal_id != run.goal_id or contract.todo_id != todo.todo_id:
            raise VerificationPolicyError("WorkContract identity mismatch")
        required = tuple(todo.required_gates)
        from .test_contract import policy as test_contract_policy
        if test_contract_policy(contract.constraints) is not None:
            required += tuple(g for g in ('regression_preservation', 'immutable_baseline_tests', 'regression_oracle') if g not in required)
        optional_raw = contract.constraints.get("verification_optional_gates", ())
        if optional_raw is None:
            optional_raw = ()
        if not isinstance(optional_raw, (tuple, list)) or any(not isinstance(item, str) or not item.strip() for item in optional_raw):
            raise VerificationPolicyError("verification_optional_gates must be list/tuple of non-empty strings")
        optional = tuple(optional_raw)
        if set(required) & set(optional):
            raise VerificationPolicyError("required and optional verification gates overlap")
        gate_ids = required + optional
        if not gate_ids:
            raise VerificationPolicyError("Todo/WorkContract defines no verification gates")
        if len(gate_ids) > self.policy.max_gates:
            raise VerificationPolicyError("verification gate count exceeds max_gates")
        if len(set(gate_ids)) != len(gate_ids):
            raise VerificationPolicyError("verification gate IDs must be unique")
        assertions = self._assertions(contract)
        gates = [self._compile_gate(gate_id, gate_id in required, assertions) for gate_id in gate_ids]
        gates.sort(key=lambda gate: (_GATE_ORDER[gate.gate_type], gate.gate_id.casefold()))
        return VerificationPlan(
            plan_id=f"{run.run_id}:{todo.todo_id}:verification",
            run_id=run.run_id,
            todo_id=todo.todo_id,
            lease_id=lease.lease_id,
            gates=tuple(gates),
        )

    def _assertions(self, contract: WorkContract) -> dict[str, Mapping[str, Any]]:
        raw = contract.constraints.get("verification_file_assertions", {})
        if raw is None:
            return {}
        if not isinstance(raw, Mapping):
            raise VerificationPolicyError("verification_file_assertions must be mapping")
        result: dict[str, Mapping[str, Any]] = {}
        for gate_id, value in raw.items():
            if not isinstance(gate_id, str) or not gate_id.strip() or not isinstance(value, Mapping):
                raise VerificationPolicyError("verification_file_assertions entries must be mappings")
            result[gate_id] = value
        return result

    def _compile_gate(
        self,
        gate_id: str,
        required: bool,
        assertions: Mapping[str, Mapping[str, Any]],
    ) -> VerificationGateSpec:
        if gate_id in _BUILTINS:
            builtin, gate_type = _BUILTINS[gate_id]
            return VerificationGateSpec(gate_id, VerificationGateKind.BUILTIN, gate_type, required, builtin=builtin)
        if gate_id in assertions:
            raw = assertions[gate_id]
            try:
                mode = FileAssertionMode(str(raw["mode"]).upper())
                path = normalize_relative_path(raw["path"])
                expected = raw.get("expected")
                if expected is not None and not isinstance(expected, str):
                    raise ValueError("expected must be string")
                return VerificationGateSpec(
                    gate_id=gate_id,
                    kind=VerificationGateKind.FILE_ASSERTION,
                    gate_type=VerificationGateType.POSTCONDITION,
                    required=required,
                    assertion_mode=mode,
                    path=path,
                    expected=expected,
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise VerificationPolicyError(f"invalid file assertion {gate_id}: {exc}") from exc
        entry = self.policy.command_catalog.get(gate_id)
        if entry is not None:
            return VerificationGateSpec(
                gate_id=gate_id,
                kind=VerificationGateKind.COMMAND,
                gate_type=entry.gate_type,
                required=required,
                catalog_id=entry.catalog_id,
            )
        raise VerificationPolicyError(f"unknown verification gate_id: {gate_id}")

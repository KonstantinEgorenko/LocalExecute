'Immutable contracts for verification deterministic verification gates.'
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping, TypeAlias

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | tuple["JsonValue", ...] | Mapping[str, "JsonValue"]


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _non_negative_int(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be a non-boolean integer >= 0")
    return value


def _positive_int(value: object, name: str) -> int:
    value = _non_negative_int(value, name)
    if value == 0:
        raise ValueError(f"{name} must be > 0")
    return value


def _freeze(value: Any, name: str) -> JsonValue:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{name} must not contain non-finite floats")
        return value
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        frozen: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{name} mapping keys must be strings")
            frozen[key] = _freeze(item, f"{name}.{key}")
        return MappingProxyType(frozen)
    if isinstance(value, (tuple, list)):
        return tuple(_freeze(item, name) for item in value)
    raise ValueError(f"{name} must be JSON-safe")


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    if isinstance(value, StrEnum):
        return value.value
    return value


def canonical_json(value: object) -> str:
    return json.dumps(_plain(value), sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class VerificationGateKind(StrEnum):
    BUILTIN = "BUILTIN"
    FILE_ASSERTION = "FILE_ASSERTION"
    COMMAND = "COMMAND"


class VerificationGateType(StrEnum):
    DIFF = "DIFF"
    SCOPE = "SCOPE"
    SYNTAX = "SYNTAX"
    TESTS = "TESTS"
    LINT = "LINT"
    TYPE_CHECK = "TYPE_CHECK"
    BUILD = "BUILD"
    POSTCONDITION = "POSTCONDITION"
    CUSTOM = "CUSTOM"


class BuiltinGate(StrEnum):
    CANDIDATE_DIFF = "candidate_diff"
    SCOPE_GUARD = "scope_guard"
    PYTHON_SYNTAX = "python_syntax"
    REGRESSION_CHANGE = "regression_change"
    REGRESSION_PRESERVATION = "regression_preservation"
    IMMUTABLE_BASELINE_TESTS = "immutable_baseline_tests"
    REGRESSION_ORACLE = "regression_oracle"


class FileAssertionMode(StrEnum):
    EXISTS = "EXISTS"
    ABSENT = "ABSENT"
    EXACT_TEXT = "EXACT_TEXT"
    CONTAINS_TEXT = "CONTAINS_TEXT"
    SHA256 = "SHA256"


class GateOutcome(StrEnum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    ERROR = "ERROR"
    TIMEOUT = "TIMEOUT"
    NOT_RUN = "NOT_RUN"


class VerificationOutcome(StrEnum):
    PASSED = "PASSED"
    FAILED = "FAILED"


class VerificationSubject(StrEnum):
    CANDIDATE = "CANDIDATE"
    ORIGINAL_SOURCE = "ORIGINAL_SOURCE"


class ExecutionPurpose(StrEnum):
    CANDIDATE_VALIDATION = "CANDIDATE_VALIDATION"
    IMMUTABLE_BASELINE = "IMMUTABLE_BASELINE"
    REGRESSION_NEGATIVE_CONTROL = "REGRESSION_NEGATIVE_CONTROL"


def source_revision_identity(head: str, diff_sha256: str) -> str:
    return canonical_sha256({"head_commit": head, "diff_sha256": diff_sha256})


@dataclass(frozen=True, slots=True)
class GateProvenance:
    subject_revision: VerificationSubject
    execution_purpose: ExecutionPurpose
    raw_exit_code: int | None
    gate_outcome: GateOutcome
    expected_failure: bool
    verification_report_id: str
    source_revision_identity: str

    def __post_init__(self) -> None:
        if not isinstance(self.subject_revision, VerificationSubject) or not isinstance(self.execution_purpose, ExecutionPurpose):
            raise ValueError("invalid verification provenance subject/purpose")
        if not isinstance(self.gate_outcome, GateOutcome):
            raise ValueError("invalid provenance gate outcome")
        if self.raw_exit_code is not None and type(self.raw_exit_code) is not int:
            raise ValueError("raw_exit_code must be integer or None")
        negative = self.execution_purpose is ExecutionPurpose.REGRESSION_NEGATIVE_CONTROL
        if type(self.expected_failure) is not bool or self.expected_failure != negative:
            raise ValueError("expected_failure must match execution purpose")
        if (self.subject_revision is VerificationSubject.ORIGINAL_SOURCE) != negative:
            raise ValueError("original SOURCE is only a regression negative control")
        if negative and self.gate_outcome is GateOutcome.PASSED and self.raw_exit_code != 1:
            raise ValueError("passing negative control requires assertion-failure exit code 1")
        _text(self.verification_report_id, "provenance report_id")
        value = self.source_revision_identity
        if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError("source_revision_identity must be a SHA-256 identity")

    def to_dict(self) -> dict[str, Any]:
        return {"subject_revision": self.subject_revision.value,
                "execution_purpose": self.execution_purpose.value,
                "raw_exit_code": self.raw_exit_code, "gate_outcome": self.gate_outcome.value,
                "expected_failure": self.expected_failure,
                "verification_report_id": self.verification_report_id,
                "source_revision_identity": self.source_revision_identity}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "GateProvenance":
        if not isinstance(payload, Mapping) or set(payload) != set(cls.__dataclass_fields__):
            raise ValueError("provenance keys must match contract exactly")
        return cls(VerificationSubject(payload["subject_revision"]), ExecutionPurpose(payload["execution_purpose"]),
                   payload["raw_exit_code"], GateOutcome(payload["gate_outcome"]), payload["expected_failure"],
                   payload["verification_report_id"], payload["source_revision_identity"])


@dataclass(frozen=True, slots=True)
class CommandCatalogEntry:
    catalog_id: str
    gate_type: VerificationGateType
    argv: tuple[str, ...]
    timeout_seconds: int
    expected_exit_codes: tuple[int, ...] = (0,)

    def __post_init__(self) -> None:
        _text(self.catalog_id, "catalog_id")
        if not isinstance(self.gate_type, VerificationGateType):
            raise ValueError("gate_type must be VerificationGateType")
        if self.gate_type not in (
            VerificationGateType.TESTS, VerificationGateType.LINT, VerificationGateType.TYPE_CHECK,
            VerificationGateType.BUILD, VerificationGateType.CUSTOM,
        ):
            raise ValueError("command gate_type must be TESTS/LINT/TYPE_CHECK/BUILD/CUSTOM")
        if not isinstance(self.argv, tuple) or not self.argv:
            raise ValueError("argv must be a non-empty tuple")
        for item in self.argv:
            _text(item, "argv")
        _positive_int(self.timeout_seconds, "timeout_seconds")
        if not isinstance(self.expected_exit_codes, tuple) or not self.expected_exit_codes:
            raise ValueError("expected_exit_codes must be a non-empty tuple")
        if any(not isinstance(code, int) or isinstance(code, bool) for code in self.expected_exit_codes):
            raise ValueError("expected_exit_codes must contain integers")
        if len(set(self.expected_exit_codes)) != len(self.expected_exit_codes):
            raise ValueError("expected_exit_codes must not contain duplicates")

    def to_dict(self) -> dict[str, Any]:
        return {
            "catalog_id": self.catalog_id,
            "gate_type": self.gate_type.value,
            "argv": list(self.argv),
            "timeout_seconds": self.timeout_seconds,
            "expected_exit_codes": list(self.expected_exit_codes),
        }


@dataclass(frozen=True, slots=True)
class VerificationGateSpec:
    gate_id: str
    kind: VerificationGateKind
    gate_type: VerificationGateType
    required: bool
    builtin: BuiltinGate | None = None
    catalog_id: str | None = None
    assertion_mode: FileAssertionMode | None = None
    path: str | None = None
    expected: str | None = None

    def __post_init__(self) -> None:
        _text(self.gate_id, "gate_id")
        if not isinstance(self.kind, VerificationGateKind):
            raise ValueError("kind must be VerificationGateKind")
        if not isinstance(self.gate_type, VerificationGateType):
            raise ValueError("gate_type must be VerificationGateType")
        if not isinstance(self.required, bool):
            raise ValueError("required must be bool")
        if self.kind is VerificationGateKind.BUILTIN:
            if not isinstance(self.builtin, BuiltinGate):
                raise ValueError("BUILTIN gate requires builtin")
            if any(value is not None for value in (self.catalog_id, self.assertion_mode, self.path, self.expected)):
                raise ValueError("BUILTIN gate has incompatible fields")
        elif self.kind is VerificationGateKind.COMMAND:
            _text(self.catalog_id, "catalog_id")
            if any(value is not None for value in (self.builtin, self.assertion_mode, self.path, self.expected)):
                raise ValueError("COMMAND gate has incompatible fields")
        elif self.kind is VerificationGateKind.FILE_ASSERTION:
            if not isinstance(self.assertion_mode, FileAssertionMode):
                raise ValueError("FILE_ASSERTION gate requires assertion_mode")
            _text(self.path, "path")
            if self.builtin is not None or self.catalog_id is not None:
                raise ValueError("FILE_ASSERTION gate has incompatible fields")
            if self.assertion_mode in (FileAssertionMode.EXACT_TEXT, FileAssertionMode.CONTAINS_TEXT, FileAssertionMode.SHA256):
                if not isinstance(self.expected, str):
                    raise ValueError("selected assertion_mode requires expected string")
                if self.assertion_mode is FileAssertionMode.SHA256:
                    value = self.expected.lower()
                    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
                        raise ValueError("SHA256 assertion expected must be 64-character hex digest")
                    object.__setattr__(self, "expected", value)
            elif self.expected is not None:
                raise ValueError("EXISTS/ABSENT assertions must not define expected")

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate_id": self.gate_id,
            "kind": self.kind.value,
            "gate_type": self.gate_type.value,
            "required": self.required,
            "builtin": self.builtin.value if self.builtin else None,
            "catalog_id": self.catalog_id,
            "assertion_mode": self.assertion_mode.value if self.assertion_mode else None,
            "path": self.path,
            "expected": self.expected,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "VerificationGateSpec":
        required = payload["required"]
        if not isinstance(required, bool):
            raise ValueError("required must be bool")
        return cls(
            gate_id=str(payload["gate_id"]),
            kind=VerificationGateKind(str(payload["kind"])),
            gate_type=VerificationGateType(str(payload["gate_type"])),
            required=required,
            builtin=None if payload.get("builtin") is None else BuiltinGate(str(payload["builtin"])),
            catalog_id=None if payload.get("catalog_id") is None else str(payload["catalog_id"]),
            assertion_mode=None if payload.get("assertion_mode") is None else FileAssertionMode(str(payload["assertion_mode"])),
            path=None if payload.get("path") is None else str(payload["path"]),
            expected=None if payload.get("expected") is None else str(payload["expected"]),
        )


@dataclass(frozen=True, slots=True)
class VerificationPlan:
    plan_id: str
    run_id: str
    todo_id: str
    lease_id: str
    gates: tuple[VerificationGateSpec, ...]

    def __post_init__(self) -> None:
        for name in ("plan_id", "run_id", "todo_id", "lease_id"):
            _text(getattr(self, name), name)
        if not isinstance(self.gates, tuple) or not self.gates:
            raise ValueError("gates must be a non-empty tuple")
        if any(not isinstance(gate, VerificationGateSpec) for gate in self.gates):
            raise ValueError("gates must contain VerificationGateSpec")
        ids = tuple(gate.gate_id for gate in self.gates)
        if len(ids) != len(set(ids)):
            raise ValueError("gate IDs must be unique")

    @property
    def fingerprint(self) -> str:
        return canonical_sha256({
            "run_id": self.run_id,
            "todo_id": self.todo_id,
            "lease_id": self.lease_id,
            "gates": [gate.to_dict() for gate in self.gates],
        })

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "run_id": self.run_id,
            "todo_id": self.todo_id,
            "lease_id": self.lease_id,
            "gates": [gate.to_dict() for gate in self.gates],
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True, slots=True)
class GateExecutionResult:
    gate_id: str
    gate_type: VerificationGateType
    required: bool
    outcome: GateOutcome
    duration_ms: int
    detail: str
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    output_truncated: bool = False
    provenance: GateProvenance | None = None

    def __post_init__(self) -> None:
        _text(self.gate_id, "gate_id")
        if not isinstance(self.gate_type, VerificationGateType):
            raise ValueError("gate_type must be VerificationGateType")
        if not isinstance(self.required, bool):
            raise ValueError("required must be bool")
        if not isinstance(self.outcome, GateOutcome):
            raise ValueError("outcome must be GateOutcome")
        _non_negative_int(self.duration_ms, "duration_ms")
        if not isinstance(self.detail, str):
            raise ValueError("detail must be a string")
        if self.exit_code is not None and (not isinstance(self.exit_code, int) or isinstance(self.exit_code, bool)):
            raise ValueError("exit_code must be integer or None")
        if not isinstance(self.stdout, str) or not isinstance(self.stderr, str):
            raise ValueError("stdout/stderr must be strings")
        if not isinstance(self.output_truncated, bool):
            raise ValueError("output_truncated must be bool")
        if self.provenance is not None:
            if not isinstance(self.provenance, GateProvenance):
                raise ValueError("provenance must be GateProvenance")
            if self.provenance.raw_exit_code != self.exit_code or self.provenance.gate_outcome is not self.outcome:
                raise ValueError("provenance must match raw exit code and gate outcome")

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate_id": self.gate_id,
            "gate_type": self.gate_type.value,
            "required": self.required,
            "outcome": self.outcome.value,
            "duration_ms": self.duration_ms,
            "detail": self.detail,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "output_truncated": self.output_truncated,
            **({"provenance": self.provenance.to_dict()} if self.provenance is not None else {}),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "GateExecutionResult":
        required = payload["required"]
        truncated = payload.get("output_truncated", False)
        if not isinstance(required, bool) or not isinstance(truncated, bool):
            raise ValueError("required/output_truncated must be bool")
        return cls(
            gate_id=str(payload["gate_id"]),
            gate_type=VerificationGateType(str(payload["gate_type"])),
            required=required,
            outcome=GateOutcome(str(payload["outcome"])),
            duration_ms=int(payload["duration_ms"]),
            detail=str(payload["detail"]),
            exit_code=None if payload.get("exit_code") is None else int(payload["exit_code"]),
            stdout=str(payload.get("stdout", "")),
            stderr=str(payload.get("stderr", "")),
            output_truncated=truncated,
            provenance=None if payload.get("provenance") is None else GateProvenance.from_dict(payload["provenance"]),
        )


@dataclass(frozen=True, slots=True)
class VerificationReport:
    report_id: str
    plan_id: str
    plan_fingerprint: str
    run_id: str
    todo_id: str
    lease_id: str
    candidate_head: str
    candidate_diff_sha256: str
    dirty_paths: tuple[str, ...]
    started_at: str
    completed_at: str
    results: tuple[GateExecutionResult, ...]
    outcome: VerificationOutcome
    candidate_content_sha256: str | None = None
    candidate_file_sha256: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "report_id", "plan_id", "plan_fingerprint", "run_id", "todo_id", "lease_id",
            "candidate_head", "candidate_diff_sha256", "started_at", "completed_at",
        ):
            _text(getattr(self, name), name)
        if len(self.plan_fingerprint) != 64 or len(self.candidate_diff_sha256) != 64:
            raise ValueError("fingerprints must be SHA-256 hex strings")
        if self.candidate_content_sha256 is not None:
            if (type(self.candidate_content_sha256) is not str
                    or len(self.candidate_content_sha256) != 64
                    or type(self.candidate_file_sha256) is not tuple
                    or any(type(row) is not tuple or len(row) != 2
                           or any(type(value) is not str or not value for value in row)
                           or len(row[1]) != 64 for row in self.candidate_file_sha256)
                    or len({row[0] for row in self.candidate_file_sha256}) != len(self.candidate_file_sha256)):
                raise ValueError('invalid raw candidate file identity')
            expected = hashlib.sha256(json.dumps(self.candidate_file_sha256,ensure_ascii=False,
                separators=(',', ':')).encode('utf8')).hexdigest()
            if expected != self.candidate_content_sha256:
                raise ValueError('raw candidate manifest fingerprint mismatch')
        if not isinstance(self.dirty_paths, tuple) or any(not isinstance(path, str) or not path for path in self.dirty_paths):
            raise ValueError("dirty_paths must be tuple[str, ...]")
        if len(set(self.dirty_paths)) != len(self.dirty_paths):
            raise ValueError("dirty_paths must not contain duplicates")
        if not isinstance(self.results, tuple) or not self.results:
            raise ValueError("results must be non-empty tuple")
        if any(not isinstance(item, GateExecutionResult) for item in self.results):
            raise ValueError("results must contain GateExecutionResult")
        if not isinstance(self.outcome, VerificationOutcome):
            raise ValueError("outcome must be VerificationOutcome")
        expected = VerificationOutcome.PASSED if all(
            (not result.required) or result.outcome is GateOutcome.PASSED for result in self.results
        ) else VerificationOutcome.FAILED
        if self.outcome is not expected:
            raise ValueError("outcome must match required gate results")
        for result in self.results:
            if result.provenance is not None and result.provenance.verification_report_id != self.report_id:
                raise ValueError("gate provenance report identity mismatch")

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "plan_id": self.plan_id,
            "plan_fingerprint": self.plan_fingerprint,
            "run_id": self.run_id,
            "todo_id": self.todo_id,
            "lease_id": self.lease_id,
            "candidate_head": self.candidate_head,
            "candidate_diff_sha256": self.candidate_diff_sha256,
            "dirty_paths": list(self.dirty_paths),
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "results": [item.to_dict() for item in self.results],
            "outcome": self.outcome.value,
            "candidate_content_sha256": self.candidate_content_sha256,
            "candidate_file_sha256": [list(row) for row in self.candidate_file_sha256],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "VerificationReport":
        return cls(
            report_id=str(payload["report_id"]),
            plan_id=str(payload["plan_id"]),
            plan_fingerprint=str(payload["plan_fingerprint"]),
            run_id=str(payload["run_id"]),
            todo_id=str(payload["todo_id"]),
            lease_id=str(payload["lease_id"]),
            candidate_head=str(payload["candidate_head"]),
            candidate_diff_sha256=str(payload["candidate_diff_sha256"]),
            dirty_paths=tuple(str(item) for item in payload.get("dirty_paths", [])),
            started_at=str(payload["started_at"]),
            completed_at=str(payload["completed_at"]),
            results=tuple(GateExecutionResult.from_dict(item) for item in payload["results"]),
            outcome=VerificationOutcome(str(payload["outcome"])),
            candidate_content_sha256=payload.get('candidate_content_sha256'),
            candidate_file_sha256=tuple(tuple(row) for row in payload.get('candidate_file_sha256', ())),
        )


@dataclass(frozen=True, slots=True)
class VerificationCoordinatorResult:
    report: VerificationReport
    reused_existing_report: bool
    run_status: str

    def __post_init__(self) -> None:
        if not isinstance(self.report, VerificationReport):
            raise ValueError("report must be VerificationReport")
        if not isinstance(self.reused_existing_report, bool):
            raise ValueError("reused_existing_report must be bool")
        _text(self.run_status, "run_status")

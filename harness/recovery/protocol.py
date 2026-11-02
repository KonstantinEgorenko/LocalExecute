'Strict local JSON contracts for recovery Ministral diagnosis and Ornith replan.'
from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any

from ..evaluation.models import BenchmarkDifficulty, BenchmarkTaskClass
from harness.router.policy_models import AmbiguityLevel, NavigationNeed
from .models import FailureDiagnosisV2, FailureEvidenceV2, FailureKind, ReplanContractV2, _safe_path


class RecoveryProtocolError(ValueError):
    def __init__(self, message, *, code="PROTOCOL_INVALID"):
        super().__init__(message)
        self.code = code


DIAGNOSIS_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema_version", "failure_kind", "confidence", "root_cause", "evidence",
        "implicated_files", "corrected_invariants", "replan_required",
        "context_expansion_required", "can_override_deterministic",
    ],
    "properties": {
        "schema_version": {"type": "integer", "const": 1},
        "failure_kind": {"type": "string", "enum": [item.value for item in FailureKind]},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "root_cause": {"type": "string", "minLength": 1, "maxLength": 5000},
        "evidence": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 8, "uniqueItems": True},
        "implicated_files": {"type": "array", "items": {"type": "string"}, "maxItems": 8, "uniqueItems": True},
        "corrected_invariants": {"type": "array", "items": {"type": "string"}, "maxItems": 8, "uniqueItems": True},
        "replan_required": {"type": "boolean"},
        "context_expansion_required": {"type": "boolean"},
        "can_override_deterministic": {"type": "boolean", "const": False},
    },
}


def diagnosis_failure_kinds(bundle: FailureEvidenceV2) -> tuple[FailureKind, ...]:
    """Execution non-completion alone cannot prove an API/test semantic failure."""
    values = tuple(FailureKind)
    if 'diagnosis_path_domain' not in bundle.source_metadata:
        return values
    reports = [item for item in bundle.evidence_items if item.evidence_type == 'DETERMINISTIC_VERIFICATION']
    has_semantic_source = any(item.evidence_type in ('SOURCE_FILE', 'TEST_SOURCE', 'FAILED_CANDIDATE_SNAPSHOT', 'JUDGE_REPLAN')
                              for item in bundle.evidence_items)
    has_failed_gate_feedback = any(item.payload.get('raw', {}).get('verification_feedback', {}).get('state') == 'CURRENT_FAILED_CANDIDATE'
                                   for item in bundle.evidence_items)
    if (reports and all(item.payload.get('raw', {}).get('kind') == 'EXECUTION_NOT_VERIFIABLE' for item in reports)
            and not has_semantic_source and not has_failed_gate_feedback):
        return tuple(kind for kind in values if kind not in (FailureKind.API_SEMANTICS, FailureKind.TEST_SEMANTICS,
                                                            FailureKind.WRONG_HYPOTHESIS, FailureKind.WRONG_FILE))
    return values


def diagnosis_response_schema(bundle: FailureEvidenceV2) -> dict[str, Any]:
    """Legacy shape unchanged; authorized production domain requires linked facts."""
    schema = deepcopy(DIAGNOSIS_RESPONSE_SCHEMA)
    schema['properties']['failure_kind']['enum'] = [kind.value for kind in diagnosis_failure_kinds(bundle)]
    if 'diagnosis_path_domain' in bundle.source_metadata:
        schema['properties']['corrected_invariants']['items'] = {
            'type': 'object', 'additionalProperties': False, 'required': ['statement', 'evidence_ids'],
            'properties': {
                'statement': {'type': 'string', 'minLength': 1, 'maxLength': 4000},
                'evidence_ids': {'type': 'array', 'minItems': 1, 'maxItems': 8, 'uniqueItems': True,
                                 'items': {'type': 'string', 'enum': sorted(bundle.allowed_evidence_ids)}},
            },
        }
    return schema


def _reject_opaque_identifier_claims(payload, bundle):
    """Audit identities cannot be quoted as semantic diagnosis evidence.

    This checks objectively identifiable metadata, not natural-language entailment.
    Evidence IDs remain valid citations; only semantic prose is inspected.
    """
    identifiers = set()
    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == 'fingerprint' and isinstance(item, str):
                    identifiers.update((item, item[:16]))
                if key in ('report_id', 'evidence_id') and isinstance(item, str):
                    suffix = item.rsplit(':', 1)[-1]
                    if re.fullmatch('[0-9a-f]{12,64}', suffix):
                        identifiers.add(suffix)
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
    visit(bundle.to_dict())
    prose = [payload['root_cause'], *(item['statement'] for item in payload['corrected_invariants'])]
    # A valid evidence citation can contain a report hash suffix. Remove the
    # complete authorized IDs before checking bare opaque metadata claims.
    for evidence_id in sorted(bundle.allowed_evidence_ids, key=len, reverse=True):
        prose = [text.replace(evidence_id, '<authorized evidence citation>') for text in prose]
    if any(identifier in text for text in prose for identifier in identifiers if len(identifier) >= 12):
        raise RecoveryProtocolError('opaque bundle/verification identities cannot support semantic claims; cite evidence IDs')

REPLAN_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema_version", "source_task_id", "revised_objective", "task_class", "difficulty",
        "target_files", "navigation_need", "context_tokens", "dependency_depth", "ambiguity", "risk",
        "required_evidence", "constraints", "fresh_workspace_required", "preserve_tests", "confidence",
    ],
    "properties": {
        "schema_version": {"type": "integer", "const": 1},
        "source_task_id": {"type": "string", "minLength": 1},
        "revised_objective": {"type": "string", "minLength": 1, "maxLength": 12000},
        "task_class": {"type": "string", "enum": [item.value for item in BenchmarkTaskClass]},
        "difficulty": {"type": "string", "enum": [item.value for item in BenchmarkDifficulty]},
        "target_files": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 8, "uniqueItems": True},
        "navigation_need": {"type": "string", "enum": [item.value for item in NavigationNeed]},
        "context_tokens": {"type": "integer", "minimum": 512, "maximum": 16384},
        "dependency_depth": {"type": "integer", "minimum": 0, "maximum": 32},
        "ambiguity": {"type": "string", "enum": [item.value for item in AmbiguityLevel]},
        "risk": {"type": "string", "enum": [item.value for item in AmbiguityLevel]},
        "required_evidence": {"type": "array", "items": {"type": "string"}, "maxItems": 8, "uniqueItems": True},
        "constraints": {"type": "array", "items": {"type": "string"}, "maxItems": 12, "uniqueItems": True},
        "fresh_workspace_required": {"type": "boolean", "const": True},
        "preserve_tests": {"type": "boolean", "const": True},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
    },
}


def _transport_json(content: str, name: str) -> str:
    """Normalize only a single outer JSON markdown fence.

    This is transport normalization, not semantic repair. Prose outside the
    fence, nested fences, non-JSON fence languages, or malformed fences remain
    protocol failures.
    """
    if not isinstance(content, str) or not content.strip():
        raise RecoveryProtocolError(f"{name} response must be non-empty JSON")

    stripped = content.strip()
    if not stripped.startswith("```"):
        return stripped

    lines = stripped.splitlines()
    if len(lines) < 3:
        raise RecoveryProtocolError(f"{name} response has malformed markdown fence")

    opener = lines[0].strip().casefold()
    if opener not in {"```", "```json"}:
        raise RecoveryProtocolError(
            f"{name} response markdown fence must be plain or json"
        )
    if lines[-1].strip() != "```":
        raise RecoveryProtocolError(
            f"{name} response markdown fence must close without trailing prose"
        )

    inner = "\n".join(lines[1:-1]).strip()
    if not inner or "```" in inner:
        raise RecoveryProtocolError(
            f"{name} response markdown fence contains invalid nested content"
        )
    return inner


def _object(content: str, expected: frozenset[str], name: str) -> dict[str, Any]:
    normalized = _transport_json(content, name)
    try:
        payload = json.loads(normalized)
    except json.JSONDecodeError as exc:
        raise RecoveryProtocolError(f"{name} response invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise RecoveryProtocolError(f"{name} response must be JSON object")
    if set(payload) != expected:
        raise RecoveryProtocolError(f"{name} response keys must match contract exactly")
    if payload.get("schema_version") != 1:
        raise RecoveryProtocolError(f"{name} schema_version must be 1")
    return payload


def parse_diagnosis_response(content: str, bundle: FailureEvidenceV2) -> FailureDiagnosisV2:
    expected = frozenset(DIAGNOSIS_RESPONSE_SCHEMA["required"])
    payload = _object(content, expected, "diagnosis")
    if payload['failure_kind'] not in {kind.value for kind in diagnosis_failure_kinds(bundle)}:
        raise RecoveryProtocolError('diagnosis semantic failure kind requires source, Judge or actual verification evidence; execution non-completion is insufficient')
    evidence = payload.get("evidence")
    if not isinstance(evidence, list) or not evidence or len(evidence) > 8 or len(evidence) != len(set(evidence)):
        raise RecoveryProtocolError("diagnosis evidence must contain 1..8 unique IDs")
    unknown = sorted(set(evidence) - set(bundle.allowed_evidence_ids))
    if unknown:
        raise RecoveryProtocolError(f"diagnosis cited unknown evidence IDs: {unknown}")
    files = payload.get("implicated_files")
    if not isinstance(files, list) or len(files) > 8:
        raise RecoveryProtocolError("diagnosis implicated_files malformed")
    if 'diagnosis_path_domain' in bundle.source_metadata and any(not isinstance(path, str) for path in files):
        raise RecoveryProtocolError('diagnosis implicated_files must contain strings')
    try:
        normalized_files = [_safe_path(path, "implicated_files") for path in files]
    except ValueError as exc:
        raise RecoveryProtocolError(str(exc), code="UNSAFE_PATH") from exc
    if len(set(normalized_files)) != len(normalized_files):
        raise RecoveryProtocolError("implicated_files must contain unique paths", code="DUPLICATE_PATHS")
    unknown_files = sorted(set(normalized_files) - set(bundle.workspace_paths))
    if unknown_files:
        raise RecoveryProtocolError(f"diagnosis implicated unknown workspace files: {unknown_files}", code="UNAUTHORIZED_PATH")
    invariants = payload.get("corrected_invariants")
    invariant_evidence = ()
    if 'diagnosis_path_domain' in bundle.source_metadata:
        if not isinstance(invariants, list) or len(invariants) > 8:
            raise RecoveryProtocolError('diagnosis corrected_invariants malformed')
        statements, links = [], []
        for item in invariants:
            if (not isinstance(item, dict) or set(item) != {'statement', 'evidence_ids'}
                    or not isinstance(item['statement'], str) or not item['statement'].strip()
                    or len(item['statement']) > 4000):
                raise RecoveryProtocolError('diagnosis invariant requires a bounded statement and evidence_ids')
            refs = item['evidence_ids']
            if (not isinstance(refs, list) or not 1 <= len(refs) <= 8
                    or any(not isinstance(ref, str) for ref in refs)
                    or len(set(refs)) != len(refs) or set(refs) - bundle.allowed_evidence_ids):
                raise RecoveryProtocolError('diagnosis invariant cites invalid evidence IDs')
            statements.append(item['statement'])
            links.append(tuple(refs))
        if not isinstance(payload['root_cause'], str):
            raise RecoveryProtocolError('diagnosis root_cause must be a string')
        _reject_opaque_identifier_claims(payload, bundle)
        invariants, invariant_evidence = statements, tuple(links)
    if not isinstance(invariants, list) or len(invariants) > 8 or any(not isinstance(x, str) or not x.strip() for x in invariants):
        raise RecoveryProtocolError("diagnosis corrected_invariants malformed")
    try:
        diagnosis = FailureDiagnosisV2(
            FailureKind(str(payload["failure_kind"])),
            float(payload["confidence"]),
            str(payload["root_cause"]),
            tuple(str(x) for x in evidence),
            tuple(normalized_files),
            tuple(str(x) for x in invariants),
            payload["replan_required"],
            payload["context_expansion_required"],
            payload["can_override_deterministic"],
            invariant_evidence,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RecoveryProtocolError(f"invalid diagnosis response: {exc}") from exc
    return diagnosis


def parse_replan_response(
    content: str,
    bundle: FailureEvidenceV2,
    diagnosis: FailureDiagnosisV2,
    *,
    max_context_tokens: int,
    max_target_files: int,
) -> ReplanContractV2:
    expected = frozenset(REPLAN_RESPONSE_SCHEMA["required"])
    payload = _object(content, expected, "replan")
    if str(payload.get("source_task_id")) != bundle.task_id:
        raise RecoveryProtocolError("replan source_task_id mismatch")
    targets = payload.get("target_files")
    if not isinstance(targets, list) or not (1 <= len(targets) <= max_target_files) or len(targets) != len(set(targets)):
        raise RecoveryProtocolError("replan target_files malformed")
    normalized_targets = [str(path).replace("\\", "/") for path in targets]
    unknown = sorted(set(normalized_targets) - set(bundle.workspace_paths))
    if unknown:
        raise RecoveryProtocolError(f"replan targets unknown workspace files: {unknown}")
    immutable = sorted(set(normalized_targets) & set(bundle.immutable_paths))
    if immutable:
        raise RecoveryProtocolError(f"replan cannot target immutable paths: {immutable}")
    refs = payload.get("required_evidence")
    if not isinstance(refs, list) or len(refs) > 8 or len(refs) != len(set(refs)):
        raise RecoveryProtocolError("replan required_evidence malformed")
    unknown_refs = sorted(set(refs) - set(bundle.allowed_evidence_ids))
    if unknown_refs:
        raise RecoveryProtocolError(f"replan cited unknown evidence IDs: {unknown_refs}")
    if not set(diagnosis.evidence).intersection(refs):
        raise RecoveryProtocolError("replan must carry forward at least one diagnosis evidence ID")
    context_tokens = payload.get("context_tokens")
    if not isinstance(context_tokens, int) or isinstance(context_tokens, bool) or not 512 <= context_tokens <= max_context_tokens:
        raise RecoveryProtocolError("replan context_tokens outside policy")
    constraints = payload.get("constraints")
    if not isinstance(constraints, list) or len(constraints) > 12 or any(not isinstance(x, str) or not x.strip() for x in constraints):
        raise RecoveryProtocolError("replan constraints malformed")
    try:
        plan = ReplanContractV2(
            str(payload["source_task_id"]),
            str(payload["revised_objective"]),
            BenchmarkTaskClass(str(payload["task_class"])),
            BenchmarkDifficulty(str(payload["difficulty"])),
            tuple(normalized_targets),
            NavigationNeed(str(payload["navigation_need"])),
            int(context_tokens),
            int(payload["dependency_depth"]),
            AmbiguityLevel(str(payload["ambiguity"])),
            AmbiguityLevel(str(payload["risk"])),
            tuple(str(x) for x in refs),
            tuple(str(x) for x in constraints),
            payload["fresh_workspace_required"],
            payload["preserve_tests"],
            float(payload["confidence"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RecoveryProtocolError(f"invalid replan response: {exc}") from exc
    return plan


def diagnosis_retry_feedback(content, bundle):
    """Read-only diagnostic projection; never repair or authorize a response."""
    feedback={"error_code":"PROTOCOL_INVALID","rejected_paths":[],"canonical_paths":[],"duplicate_indices":[]}
    try:
        parse_diagnosis_response(content,bundle)
        feedback["error_code"]="NO_PROTOCOL_ERROR"
    except RecoveryProtocolError as exc:
        feedback["error_code"]=exc.code
    try:
        raw=json.loads(_transport_json(content,"diagnosis"))
        files=raw.get("implicated_files")
        if type(files) is not list:return feedback
        seen={}
        for index,path in enumerate(files[:8]):
            feedback["rejected_paths"].append(path.encode("utf-8")[:256].decode("utf-8","ignore") if type(path) is str else "<non-string>")
            try:canonical=_safe_path(path,"implicated_files")
            except ValueError:canonical=None
            feedback["canonical_paths"].append(canonical[:256] if canonical else None)
            if canonical is not None:
                if canonical in seen:feedback["duplicate_indices"].append([seen[canonical],index])
                else:seen[canonical]=index
    except (ValueError,TypeError,AttributeError):pass
    return feedback

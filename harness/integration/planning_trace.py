"""Best-effort, mission-scoped forensic trace for transient Lead planning.

The trace is observability only: the planner never reads it to select work,
schedule a Todo, decide feasibility, or restore authoritative planner state.
Trace I/O failure is explicitly fail-open for planning and retained in
``last_error`` for the caller/operator.

Durable schema v2 is content-minimized: raw Lead requests, model responses,
compiled/normalized candidates, and feasibility text are not persisted.
Request identity is calculated from a secret-redacted projection. Model
response and candidate fingerprints describe the original transient values
without persisting those values.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Any
import uuid

from harness.lead.models import LeadRequest, LeadResult
from harness.mission.models import MissionSpecV1, stable_sha256

from .model_invocation_lineage import (
    ModelInvocationKindV1,
    ModelInvocationManifestV1,
    ModelInvocationStatusV1,
    runtime_fingerprint,
    stable_fingerprint,
)


_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _is_secret_key(key: Any) -> bool:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(key))
    normalized = re.sub(r"[^a-z0-9]+", "_", text.casefold()).strip("_")
    exact = {"token", "secret", "authorization", "password", "api_key", "apikey"}
    suffixes = ("_token", "_secret", "_authorization", "_password", "_api_key", "_apikey")
    return normalized in exact or normalized.endswith(suffixes)


def _plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if hasattr(value, "items"):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


def _serialized_bytes(value: Any) -> int:
    encoded = json.dumps(
        _plain(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return len(encoded)


def _diagnostic_summary(error: BaseException) -> str:
    message = str(error)
    fingerprint = stable_fingerprint({
        "exception_type": type(error).__name__,
        "message": message,
    })
    return (
        f"{type(error).__name__}: "
        f"fingerprint={fingerprint}; "
        f"bytes={len(message.encode('utf-8'))}"
    )


def _secret_values(value: Any) -> tuple[str, ...]:
    """Return request-secret values in stable replacement order."""
    values: set[str] = set()

    def collect(item: Any) -> None:
        if isinstance(item, dict) or hasattr(item, "items"):
            for key, child in item.items():
                if _is_secret_key(key) and isinstance(child, str) and child:
                    values.add(child)
                collect(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                collect(child)

    collect(value)
    return tuple(sorted(values, key=lambda item: (-len(item), item)))


def _sanitize_text(value: str, secret_values: tuple[str, ...]) -> str:
    sanitized = value
    for secret in secret_values:
        sanitized = sanitized.replace(secret, "[REDACTED]")
    return sanitized


def _redact(value: Any, secret_values: tuple[str, ...] = ()) -> Any:
    if isinstance(value, dict) or hasattr(value, "items"):
        return {
            str(key): (
                "[REDACTED]"
                if _is_secret_key(key)
                else _plain(item)
                if str(key) == "raw_response_fingerprint"
                else _redact(item, secret_values)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item, secret_values) for item in value]
    if isinstance(value, str):
        return _sanitize_text(value, secret_values)
    return value


def _safe_component(value: str) -> str:
    safe = _SAFE_NAME_RE.sub("-", value).strip(".-") or "mission"
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    return f"{safe[:64]}-{digest}"


def _parse_disposition(content: str) -> str:
    try:
        raw = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return "UNPARSEABLE"
    value = raw.get("disposition") if isinstance(raw, dict) else None
    return str(value) if value in {"PLAN", "NEEDS_ATTENTION"} else "INVALID_SCHEMA"


def _lead_runtime_metadata(runtime: Any, response: Any = None) -> tuple[str, str | None, str, str]:
    logical_model_id = (
        getattr(response, "model_id", None)
        or getattr(runtime, "model_id", None)
        or "ornith_lead"
    )
    model_tag = getattr(response, "model_tag", None)
    context_length = getattr(response, "context_length", None)
    values: dict[str, Any] = {
        "runtime_kind": type(runtime).__name__,
        "logical_model_id": logical_model_id,
        "model_tag": model_tag,
        "context_length": context_length,
        "output_tokens": getattr(runtime, "output_tokens", None),
        "timeout_seconds": getattr(runtime, "timeout_seconds", None),
    }
    registry = getattr(runtime, "registry", None)
    if registry is not None:
        try:
            definition = registry.get(logical_model_id)
            server = registry.get_server(definition.server)
            model_tag = model_tag or definition.ollama_model
            context_length = context_length or int(definition.context["default"])
            values.update({
                "model_tag": model_tag,
                "context_length": context_length,
                "server_id": server.server_id,
                "execution": definition.runtime.get("execution"),
                "configured_num_gpu": definition.runtime.get("num_gpu"),
            })
        except (AttributeError, KeyError, TypeError, ValueError):
            pass
    return logical_model_id, model_tag, type(runtime).__name__, runtime_fingerprint(values)


def _candidate_semantics(items: tuple[Any, ...], *, include_text: bool, secrets: tuple[str, ...]) -> dict[str, Any]:
    # These are the production predicates, including the normalizer's exact
    # CODE-only mutation consumer and reconnaissance eligibility definitions.
    from . import plan_feasibility as feasibility
    from . import plan_shape_normalization as shape

    rows = []
    mutation_ids, recon_ids, verification_ids, incompatible_ids = [], [], [], []
    edges = []
    for item in items:
        todo = item.todo
        objective = todo.objective
        encoded = objective.encode("utf-8")
        verification = feasibility.is_verification_only_objective(objective)
        incompatible = feasibility.detect_candidate_diff_incompatible_todo(item) is not None
        if shape._is_mutation_consumer(item):
            mutation_ids.append(todo.todo_id)
        if shape._is_reconnaissance_only(item):
            recon_ids.append(todo.todo_id)
        if verification:
            verification_ids.append(todo.todo_id)
        if incompatible:
            incompatible_ids.append(todo.todo_id)
        edges.extend([dependency, todo.todo_id] for dependency in todo.dependencies)
        row = {
            "todo_id": todo.todo_id,
            "dependencies": list(todo.dependencies),
            "required_gates": list(todo.required_gates),
            "task_type": item.routing_task_spec.task_type.value,
            "execution_responsibility": feasibility.classify_execution_responsibility(objective).value,
            "has_unconditional_mutation_deliverable": feasibility.has_unconditional_mutation_deliverable(objective),
            "is_verification_only_objective": verification,
            "non_deliverable_reconnaissance": feasibility.detect_non_deliverable_reconnaissance(item) is not None,
            "candidate_diff_incompatibility": incompatible,
            "objective_bytes": len(encoded),
            "objective_fingerprint": hashlib.sha256(encoded).hexdigest(),
        }
        if include_text:
            sanitized = _sanitize_text(objective, secrets).encode("utf-8")
            row["objective_text"] = sanitized[:1024].decode("utf-8", errors="ignore")
            row["truncated"] = len(sanitized) > 1024
        rows.append(row)
    return {
        "items": rows,
        "item_count": len(rows),
        "root_todo_ids": [item.todo.todo_id for item in items if not item.todo.dependencies],
        "dependency_edges": edges,  # [prerequisite, dependent]
        "dependency_edge_count": len(edges),
        "mutation_todo_ids": mutation_ids,
        "mutation_consumer_count": len(mutation_ids),
        "reconnaissance_todo_ids": recon_ids,
        "reconnaissance_only_count": len(recon_ids),
        "verification_todo_ids": verification_ids,
        "verification_only_count": len(verification_ids),
        "candidate_diff_incompatible_todo_ids": incompatible_ids,
        "candidate_diff_incompatible_count": len(incompatible_ids),
    }


class PlanningTraceAttempt:
    """Runtime-compatible wrapper that records every actual Lead model call."""

    def __init__(
        self,
        recorder: "PlanningTraceRecorder",
        inner: Any,
        spec: MissionSpecV1,
        request: LeadRequest,
        budget_before: int,
    ) -> None:
        self.recorder = recorder
        self.inner = inner
        self.spec = spec
        self.request = request
        self.budget_before = budget_before
        self.sequences: list[int] = []
        self._physical_call_count = 0

    def complete(self, system_prompt: str, user_prompt: str, response_schema: Any = None) -> Any:
        invocation_id = f"{self.spec.mission_id}:lead-plan:{uuid.uuid4().hex}"
        budget_before = max(0, self.budget_before - self._physical_call_count)
        self._physical_call_count += 1
        input_fingerprint = stable_fingerprint({
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "response_schema": response_schema,
        })
        started = time.monotonic()
        try:
            response = self.inner.complete(system_prompt, user_prompt, response_schema)
        except Exception as exc:
            logical_model_id, model_tag, runtime_kind, runtime_hash = _lead_runtime_metadata(self.inner)
            self.recorder.record_invocation_manifest(
                self.spec,
                ModelInvocationManifestV1(
                    schema_version=1,
                    invocation_id=invocation_id,
                    request_kind=ModelInvocationKindV1.LEAD_PLAN,
                    mission_id=self.spec.mission_id,
                    goal_id=self.spec.goal_id,
                    logical_model_id=logical_model_id,
                    model_tag=model_tag,
                    runtime_kind=runtime_kind,
                    runtime_fingerprint=runtime_hash,
                    input_fingerprint=input_fingerprint,
                    budget_before={"lead_calls_remaining": budget_before},
                    budget_after={"lead_calls_remaining": max(0, budget_before - 1)},
                    duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                    status=ModelInvocationStatusV1.FAILED,
                    diagnostic=_diagnostic_summary(exc),
                ),
            )
            raise

        content = str(getattr(response, "content", ""))
        sequence = self.recorder.record_model_call(
            self.spec,
            self.request,
            budget_before=budget_before,
            response_content=content,
        )
        if sequence is not None:
            self.sequences.append(sequence)

        logical_model_id, model_tag, runtime_kind, runtime_hash = _lead_runtime_metadata(
            self.inner,
            response,
        )
        self.recorder.record_invocation_manifest(
            self.spec,
            ModelInvocationManifestV1(
                schema_version=1,
                invocation_id=invocation_id,
                request_kind=ModelInvocationKindV1.LEAD_PLAN,
                mission_id=self.spec.mission_id,
                goal_id=self.spec.goal_id,
                logical_model_id=logical_model_id,
                model_tag=model_tag,
                runtime_kind=runtime_kind,
                runtime_fingerprint=runtime_hash,
                input_fingerprint=input_fingerprint,
                output_fingerprint=stable_fingerprint(content),
                budget_before={"lead_calls_remaining": budget_before},
                budget_after={"lead_calls_remaining": max(0, budget_before - 1)},
                duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                status=ModelInvocationStatusV1.SUCCESS,
            ),
        )
        return response

    def finalize(
        self,
        *,
        result: LeadResult,
        budget_after: int,
        compiled_items: tuple[Any, ...] = (),
        normalized_items: tuple[Any, ...] = (),
        decision: Any = None,
    ) -> None:
        if not self.sequences:
            return
        self.recorder.finalize_call(
            self.spec,
            self.sequences[-1],
            result=result,
            budget_after=budget_after,
            compiled_items=compiled_items,
            normalized_items=normalized_items,
            decision=decision,
        )


class PlanningTraceRecorder:
    """Atomic JSON trace writer; never an authority for planner state."""

    schema_version = 2

    def __init__(self, root: Path, mission_trace_registry: Any = None) -> None:
        self.root = Path(root)
        self.mission_trace_registry = mission_trace_registry
        self.last_error: str | None = None
        self._call_secrets: dict[Path, tuple[str, ...]] = {}

    def _mission_dir(self, spec: MissionSpecV1) -> Path:
        return self.root / _safe_component(spec.mission_id)

    def _path(self, spec: MissionSpecV1, sequence: int) -> Path:
        return self._mission_dir(spec) / f"call-{sequence:04d}.json"

    def _next_sequence(self, spec: MissionSpecV1) -> int:
        directory = self._mission_dir(spec)
        existing = [
            int(match.group(1))
            for path in directory.glob("call-*.json")
            if (match := re.fullmatch(r"call-(\d{4})\.json", path.name))
        ]
        return max(existing, default=0) + 1

    def _write(self, path: Path, payload: dict[str, Any], secret_values: tuple[str, ...] = ()) -> bool:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            encoded = json.dumps(_redact(_plain(payload), secret_values), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
                handle.write(encoded)
                handle.flush()
                temp = Path(handle.name)
            temp.replace(path)
            return True
        except (OSError, TypeError, ValueError) as exc:
            self.last_error = f"planning trace write failed: {exc}"
            try:
                temp.unlink(missing_ok=True)  # type: ignore[name-defined]
            except (OSError, UnboundLocalError):
                pass
            return False

    def _read(self, path: Path) -> dict[str, Any] | None:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self.last_error = f"planning trace read failed: {exc}"
            return None

    def record_invocation_manifest(
        self,
        spec: MissionSpecV1,
        manifest: ModelInvocationManifestV1,
    ) -> None:
        if self.mission_trace_registry is None:
            return
        try:
            recorder = self.mission_trace_registry.get(spec.mission_id, spec.goal_id)
            recorder.record_model_invocation(manifest)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            self.last_error = f"planning invocation trace write failed: {exc}"

    def begin_attempt(self, spec: MissionSpecV1, request: LeadRequest, budget_before: int, inner: Any) -> PlanningTraceAttempt:
        return PlanningTraceAttempt(self, inner, spec, request, budget_before)

    def record_model_call(self, spec: MissionSpecV1, request: LeadRequest, *, budget_before: int, response_content: str) -> int | None:
        sequence = self._next_sequence(spec)

        request_raw = _plain(request.to_dict())
        secret_values = _secret_values(request_raw)
        request_projection = _redact(request_raw, secret_values)
        constraints = request_projection.get("constraints", {})
        guidance = (
            constraints.get("plan_refinement_guidance")
            if isinstance(constraints, dict)
            else None
        )
        instructions = guidance.get("instructions") if isinstance(guidance, dict) else None

        payload = {
            "schema_version": self.schema_version,
            "mission_id": spec.mission_id,
            "goal_id": spec.goal_id,
            "planning_call_index": sequence,
            "planning_budget_before": budget_before,
            "planning_budget_after": max(0, budget_before - 1),

            "request_fingerprint": stable_sha256(request_projection),
            "request_bytes": _serialized_bytes(request_projection),
            "request_constraint_count": len(constraints) if isinstance(constraints, dict) else 0,

            "refinement_guidance_present": isinstance(guidance, dict),
            "refinement_instruction_fingerprint": (
                stable_sha256(instructions)
                if isinstance(instructions, str)
                else None
            ),
            "refinement_instruction_bytes": (
                len(instructions.encode("utf-8"))
                if isinstance(instructions, str)
                else 0
            ),

            "raw_response_fingerprint": stable_sha256(response_content),
            "raw_response_bytes": len(response_content.encode("utf-8")),
            "parse_disposition": _parse_disposition(response_content),

            "lead_outcome": None,
            "compiled_candidate_count": 0,
            "compiled_candidate_fingerprint": None,
            "normalized_candidate_count": 0,
            "normalized_candidate_fingerprint": None,
            "feasibility": None,
        }

        if not self._write(self._path(spec, sequence), payload):
            return None

        self._call_secrets[self._path(spec, sequence)] = secret_values
        return sequence

    def finalize_call(
        self,
        spec: MissionSpecV1,
        sequence: int,
        *,
        result: LeadResult,
        budget_after: int,
        compiled_items: tuple[Any, ...],
        normalized_items: tuple[Any, ...],
        decision: Any,
    ) -> None:
        path = self._path(spec, sequence)
        secrets = self._call_secrets.pop(path, ())
        payload = self._read(path)
        if payload is None:
            return

        compiled = [item.to_dict() for item in compiled_items]
        normalized = [item.to_dict() for item in normalized_items]

        reason = None if decision is None else decision.reason
        guidance = None if decision is None else decision.guidance

        payload.update({
            "planning_budget_after": budget_after,
            "lead_outcome": result.outcome.value,
            "lead_call_count_in_attempt": result.lead_call_count,

            "compiled_candidate_count": len(compiled),
            "compiled_candidate_fingerprint": (
                stable_sha256(compiled)
                if compiled
                else None
            ),

            "normalized_candidate_count": len(normalized),
            "normalized_candidate_fingerprint": (
                stable_sha256(normalized)
                if normalized
                else None
            ),

            "feasibility": None if decision is None else {
                "decision": decision.decision.value,
                "issue_kind": decision.evidence.get("issue_kind"),
                "offending_todo_ids": list(decision.offending_todo_keys),
                "reason_fingerprint": (
                    stable_sha256(reason)
                    if isinstance(reason, str) and reason
                    else None
                ),
                "reason_bytes": (
                    len(reason.encode("utf-8"))
                    if isinstance(reason, str)
                    else 0
                ),
                "refinement_instruction_fingerprint": (
                    stable_sha256(guidance)
                    if isinstance(guidance, str) and guidance
                    else None
                ),
            },
        })

        try:
            for name, items in (("compiled", compiled_items), ("normalized", normalized_items)):
                payload[f"{name}_candidate_semantics"] = _candidate_semantics(
                    items, include_text=os.environ.get("SWARM_PLANNING_TRACE_TODO_TEXT") == "1",
                    secrets=secrets,
                )
        except Exception as exc:
            # Diagnostics must never change planning or feasibility outcomes.
            self.last_error = _diagnostic_summary(exc)
        self._write(path, payload)


__all__ = ["PlanningTraceRecorder"]

'Real-model protocol conformance probe for Judge and recovery roles.\n\nThe probe is intentionally independent from target repositories.  It uses\nproduction model IDs, the process-local single-endpoint overlay, production\ntransports and strict parsers against deterministic in-memory evidence.\n'
from __future__ import annotations

import argparse
import hashlib
import json
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ..config import ConfigLoader
from ..critic.models import CriticAction, CriticAnalysis, CriticReport, CriticVerdict, FailureClass
from ..evaluation.models import BenchmarkDifficulty, BenchmarkTaskClass
from ..executor.client import OllamaNativeClient, OllamaTimeouts
from ..judge.errors import JudgeProtocolError
from ..judge.models import JudgeEvidenceBundle, JudgePolicy
from ..judge.prompt import JudgePromptBuilder
from ..judge.protocol import JUDGE_RESPONSE_SCHEMA, parse_judge_response
from ..judge.transport import judge_transport_schema
from ..models import ModelRegistry
from harness.router.policy_models import AmbiguityLevel, NavigationNeed
from ..recovery.controller import RecoveryControllerV2
from ..recovery.models import FailureDiagnosisV2, FailureEvidenceV2, FailureKind, RecoveryEvidenceItem
from ..recovery.prompt import diagnosis_messages, replan_prompts
from ..recovery.protocol import (
    DIAGNOSIS_RESPONSE_SCHEMA,
    REPLAN_RESPONSE_SCHEMA,
    RecoveryProtocolError,
    parse_diagnosis_response,
    parse_replan_response,
)
from ..recovery.transport import diagnosis_transport_schema, replan_transport_schema
from ..resource_manager.locks import ResourceLocks
from ..verification.models import (
    GateExecutionResult,
    GateOutcome,
    VerificationGateType,
    VerificationOutcome,
    VerificationReport,
)
from .single_endpoint import SingleEndpointOrnithRuntime, install_single_endpoint_overlay


TAXONOMY = (
    "MARKDOWN_FENCE",
    "INVALID_JSON",
    "EXTRA_KEYS",
    "MISSING_KEYS",
    "WRONG_ENUM",
    "WRONG_TYPE",
    "UNKNOWN_EVIDENCE_ID",
    "MISSING_REQUIRED_EVIDENCE",
    "DETERMINISTIC_OUTCOME_MUTATION",
    "INVALID_FILE_REFERENCE",
    "IMMUTABLE_FILE_TARGET",
    "SEMANTIC_CONTRACT_OTHER",
    "MODEL_EXECUTION_ERROR",
)


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def classify_failure(raw: str | None, error: str, expected_keys: set[str] | None = None) -> str:
    'Map a strict parser/model error to the stable production integration.2 taxonomy.'

    lowered = error.casefold()
    if "execution" in lowered or "runtime failure" in lowered or "ollama unavailable" in lowered:
        return "MODEL_EXECUTION_ERROR"
    stripped = "" if raw is None else raw.lstrip()
    if stripped.startswith("```"):
        return "MARKDOWN_FENCE"
    if "invalid json" in lowered or "not valid json" in lowered:
        return "INVALID_JSON"
    if "keys must match" in lowered and raw:
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return "INVALID_JSON"
        if isinstance(payload, dict) and expected_keys is not None:
            if set(payload) - expected_keys:
                return "EXTRA_KEYS"
            if expected_keys - set(payload):
                return "MISSING_KEYS"
    if "unknown evidence" in lowered or "invented evidence" in lowered:
        return "UNKNOWN_EVIDENCE_ID"
    if "must cite" in lowered or "carry forward" in lowered:
        return "MISSING_REQUIRED_EVIDENCE"
    if "changed deterministic" in lowered or "deterministic outcome mismatch" in lowered:
        return "DETERMINISTIC_OUTCOME_MUTATION"
    if "immutable" in lowered:
        return "IMMUTABLE_FILE_TARGET"
    if "unknown workspace" in lowered or "unknown file" in lowered or "source_task_id mismatch" in lowered:
        return "INVALID_FILE_REFERENCE"
    if "is not a valid" in lowered or "enum" in lowered:
        return "WRONG_ENUM"
    if "must be string" in lowered or "must be numeric" in lowered or "malformed" in lowered:
        return "WRONG_TYPE"
    return "SEMANTIC_CONTRACT_OTHER"


def aggregate_attempts(role: str, fixture: str, iterations: list[dict[str, Any]]) -> dict[str, Any]:
    valid = sum(1 for item in iterations if item["valid"])
    failures = Counter(
        attempt["failure_taxonomy"]
        for item in iterations
        for attempt in item["attempts"]
        if attempt["failure_taxonomy"] is not None
    )
    attempts = [attempt for item in iterations for attempt in item["attempts"]]
    return {
        "role": role,
        "fixture": fixture,
        "valid": valid,
        "total": len(iterations),
        "rate": 0.0 if not iterations else valid / len(iterations),
        "model_attempts": len(attempts),
        "markdown_fence_rate": 0.0 if not attempts else sum(bool(item["markdown_fence"]) for item in attempts) / len(attempts),
        "unknown_evidence_rate": 0.0 if not attempts else sum(item["failure_taxonomy"] == "UNKNOWN_EVIDENCE_ID" for item in attempts) / len(attempts),
        "schema_shape_failure_rate": 0.0 if not attempts else sum(item["failure_taxonomy"] in {"EXTRA_KEYS", "MISSING_KEYS", "WRONG_ENUM", "WRONG_TYPE"} for item in attempts) / len(attempts),
        "failure_taxonomy": {name: failures.get(name, 0) for name in TAXONOMY},
        "iterations": iterations,
    }


def _judge_bundle(outcome: VerificationOutcome) -> JudgeEvidenceBundle:
    passed = outcome is VerificationOutcome.PASSED
    gate = GateExecutionResult(
        "tests",
        VerificationGateType.TESTS,
        True,
        GateOutcome.PASSED if passed else GateOutcome.FAILED,
        1,
        "Deterministic fixture tests passed." if passed else "Expected 85.0, received -1400.0.",
        0 if passed else 1,
    )
    verification = VerificationReport(
        "verification_report",
        "plan-fixture",
        "1" * 64,
        "run-fixture",
        "todo-fixture",
        "lease-fixture",
        "a" * 40,
        "2" * 64,
        ("pricing/service.py",),
        "2026-08-21T00:00:00+00:00",
        "2026-08-21T00:00:01+00:00",
        (gate,),
        outcome,
    )
    critic_analysis = CriticAnalysis(
        outcome,
        CriticVerdict.ACCEPT if passed else CriticVerdict.RETRY,
        FailureClass.NONE if passed else FailureClass.IMPLEMENTATION_ERROR,
        0.99,
        "The deterministic result is authoritative and the candidate is consistent." if passed else "The implementation violates percentage conversion semantics.",
        ("candidate_diff",),
        CriticAction.NONE if passed else CriticAction.RETRY_WITH_CONTEXT,
        () if passed else ("Convert percentage points to a fraction without changing tests.",),
        False,
    )
    critic = CriticReport(
        "critic_report",
        "3" * 64,
        "run-fixture",
        "todo-fixture",
        verification.report_id,
        verification.candidate_diff_sha256,
        critic_analysis,
        "ministral_reasoner",
        "ministral-3:14b",
        1,
        "critic_raw_fixture",
        "2026-08-21T00:00:02+00:00",
    )
    request = {
        "outcome": outcome.value,
        "allowed_evidence_ids": ["verification_report", "critic_report", "candidate_diff"],
    }
    return JudgeEvidenceBundle(
        f"probe-judge-{outcome.value.casefold()}",
        fingerprint(request),
        "run-fixture",
        "goal-fixture",
        "todo-fixture",
        "Review a deterministic percentage-conversion candidate.",
        verification,
        critic,
        "a" * 40,
        "2" * 64,
        "diff --git a/pricing/service.py b/pricing/service.py\n",
        ("pricing/service.py",),
        {"objective": "Preserve APIs and immutable tests."},
        frozenset(("verification_report", "critic_report", "candidate_diff")),
    )


def _failure_bundle() -> FailureEvidenceV2:
    return FailureEvidenceV2(
        "phase21_2_percentage_bug",
        "Fix percentage conversion in pricing/service.py while preserving APIs and immutable tests.",
        BenchmarkTaskClass.SINGLE_FILE_BUG,
        BenchmarkDifficulty.NORMAL,
        {"fixture": "phase21_2"},
        ("pricing/service.py", "tests/test_pricing.py"),
        ("tests/test_pricing.py",),
        ("qwen35_heavy_fast",),
        (
            RecoveryEvidenceItem("objective", "OBJECTIVE", "Bounded objective", {"text": "Fix percentage conversion."}),
            RecoveryEvidenceItem("file:pricing/service.py", "SOURCE_FILE", "Current implementation", {"path": "pricing/service.py", "content": "return amount * percent"}),
            RecoveryEvidenceItem("file:tests/test_pricing.py", "TEST_SOURCE", "Immutable deterministic test", {"path": "tests/test_pricing.py", "content": "assert discounted(100, 15) == 85"}),
            RecoveryEvidenceItem("verification_failure", "DETERMINISTIC_VERIFICATION", "Required test failed", {"output": "assert -1400.0 == 85.0"}),
        ),
    )


def _diagnosis_fixture() -> FailureDiagnosisV2:
    return FailureDiagnosisV2(
        FailureKind.API_SEMANTICS,
        0.99,
        "Percentage points were applied as a direct multiplier instead of division by 100.",
        ("file:pricing/service.py", "verification_failure"),
        ("pricing/service.py",),
        ("Fifteen percent must be represented as 0.15 before multiplication.",),
        True,
        False,
        False,
    )


class ProtocolProbe:
    def __init__(self, *, root: Path, artifact_dir: Path) -> None:
        install_single_endpoint_overlay()
        self.root = root.resolve(strict=True)
        self.artifact_dir = artifact_dir.resolve(strict=False)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.loader = ConfigLoader(self.root)
        self.registry = ModelRegistry.from_loader(self.loader)
        urls = {
            self.registry.get_server("lead").ollama_base_url.rstrip("/"),
            self.registry.get_server("worker").ollama_base_url.rstrip("/"),
        }
        if urls != {"http://127.0.0.1:11435"}:
            raise RuntimeError(f"protocol probe requires one physical endpoint :11435, got {sorted(urls)}")
        self.native = OllamaNativeClient(OllamaTimeouts.from_policies(self.loader.load_policies()))
        self.ornith = SingleEndpointOrnithRuntime(
            self.registry,
            ResourceLocks(),
            timeout_seconds=180,
            output_tokens=4096,
            model_id="ornith_lead",
        )
        self.recovery_policy = RecoveryControllerV2.from_project_root(self.root).policy

    def _persist_attempt(
        self,
        *,
        role: str,
        fixture: str,
        iteration: int,
        attempt: int,
        request: dict[str, Any],
        raw: str | None,
        model_id: str,
        model_tag: str,
        latency_ms: int,
        error: str | None,
        expected_keys: set[str],
    ) -> dict[str, Any]:
        stem = f"{role}-{fixture}-i{iteration:03d}-a{attempt:02d}"
        request_path = self.artifact_dir / f"{stem}-request.json"
        response_path = self.artifact_dir / f"{stem}-response.txt"
        request_path.write_text(json.dumps(request, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        response_path.write_text("" if raw is None else raw, encoding="utf-8")
        taxonomy = None if error is None else classify_failure(raw, error, expected_keys)
        return {
            "attempt": attempt,
            "model_id": model_id,
            "model_tag": model_tag,
            "request_fingerprint": fingerprint(request),
            "request_path": str(request_path),
            "raw_response_path": str(response_path),
            "parse_pass": error is None,
            "protocol_error": error,
            "failure_taxonomy": taxonomy,
            "latency_ms": latency_ms,
            "response_bytes": 0 if raw is None else len(raw.encode("utf-8")),
            "markdown_fence": bool(raw and raw.lstrip().startswith("```")),
        }

    def judge(self, outcome: VerificationOutcome, count: int) -> dict[str, Any]:
        bundle = _judge_bundle(outcome)
        policy = JudgePolicy()
        prompts = JudgePromptBuilder(policy)
        schema = judge_transport_schema(bundle)
        iterations: list[dict[str, Any]] = []
        fixture = outcome.value.casefold()
        for iteration in range(count):
            attempts: list[dict[str, Any]] = []
            prior_error: str | None = None
            prior_response: str | None = None
            valid = False
            for attempt in range(policy.max_model_attempts):
                system, user = prompts.prompts(bundle, prior_error=prior_error, prior_response=prior_response)
                request = {"model_id": policy.model_id, "system": system, "user": user, "format": schema}
                raw: str | None = None
                started = time.monotonic()
                try:
                    response = self.ornith.complete(system, user, schema)
                    raw = response.content
                    parse_judge_response(raw, bundle)
                    error = None
                    model_tag = response.model_tag
                except Exception as exc:  # probe must retain model and parser failures
                    error = f"{type(exc).__name__}: {exc}"
                    model_tag = self.registry.get(policy.model_id).ollama_model
                attempts.append(self._persist_attempt(
                    role="judge", fixture=fixture, iteration=iteration, attempt=attempt,
                    request=request, raw=raw, model_id=policy.model_id, model_tag=model_tag,
                    latency_ms=int(round((time.monotonic() - started) * 1000)), error=error,
                    expected_keys=set(JUDGE_RESPONSE_SCHEMA["required"]),
                ))
                if error is None:
                    valid = True
                    break
                prior_error, prior_response = error, raw
            iterations.append({"iteration": iteration, "valid": valid, "attempts": attempts})
        return aggregate_attempts("judge", fixture, iterations)

    def diagnosis(self, count: int) -> tuple[dict[str, Any], FailureDiagnosisV2]:
        bundle = _failure_bundle()
        policy = self.recovery_policy
        definition = self.registry.get(policy.diagnosis_model_id)
        server = self.registry.get_server(definition.server)
        context = min(int(definition.context["default"]), policy.max_context_tokens)
        num_gpu = definition.runtime.get("num_gpu")
        schema = diagnosis_transport_schema(bundle)
        iterations: list[dict[str, Any]] = []
        last_valid = _diagnosis_fixture()
        self.native.preload_model(server.ollama_base_url, definition.ollama_model, context, num_gpu=num_gpu)
        try:
            for iteration in range(count):
                attempts: list[dict[str, Any]] = []
                prior_error: str | None = None
                prior_response: str | None = None
                valid = False
                for attempt in range(policy.max_diagnosis_model_attempts):
                    messages = diagnosis_messages(bundle, policy, prior_error=prior_error, prior_response=prior_response)
                    request = {"model_id": definition.logical_id, "model_tag": definition.ollama_model, "messages": [item.to_dict() for item in messages], "format": schema, "num_ctx": context, "num_gpu": num_gpu}
                    raw: str | None = None
                    started = time.monotonic()
                    try:
                        payload = self.native.chat(server.ollama_base_url, definition.ollama_model, messages, -1, context, num_gpu=num_gpu, format_schema=schema, temperature=0.0)
                        raw = payload.get("message", {}).get("content") if isinstance(payload.get("message"), dict) else None
                        if not isinstance(raw, str):
                            raise RecoveryProtocolError("diagnosis content missing")
                        last_valid = parse_diagnosis_response(raw, bundle)
                        error = None
                    except Exception as exc:
                        error = f"{type(exc).__name__}: {exc}"
                    attempts.append(self._persist_attempt(
                        role="diagnosis", fixture="failed_verification", iteration=iteration, attempt=attempt,
                        request=request, raw=raw, model_id=definition.logical_id, model_tag=definition.ollama_model,
                        latency_ms=int(round((time.monotonic() - started) * 1000)), error=error,
                        expected_keys=set(DIAGNOSIS_RESPONSE_SCHEMA["required"]),
                    ))
                    if error is None:
                        valid = True
                        break
                    prior_error, prior_response = error, raw
                iterations.append({"iteration": iteration, "valid": valid, "attempts": attempts})
        finally:
            self.native.unload_model(server.ollama_base_url, definition.ollama_model)
        return aggregate_attempts("diagnosis", "failed_verification", iterations), last_valid

    def replan(self, count: int, diagnosis: FailureDiagnosisV2 | None = None) -> dict[str, Any]:
        bundle = _failure_bundle()
        diagnosis = diagnosis or _diagnosis_fixture()
        policy = self.recovery_policy
        schema = replan_transport_schema(bundle, max_context_tokens=policy.max_context_tokens, max_target_files=policy.max_target_files)
        iterations: list[dict[str, Any]] = []
        definition = self.registry.get(policy.replan_model_id)
        for iteration in range(count):
            attempts: list[dict[str, Any]] = []
            prior_error: str | None = None
            prior_response: str | None = None
            valid = False
            for attempt in range(policy.max_replan_model_attempts):
                system, user = replan_prompts(bundle, diagnosis, policy, prior_error=prior_error, prior_response=prior_response)
                request = {"model_id": definition.logical_id, "model_tag": definition.ollama_model, "system": system, "user": user, "format": schema}
                raw: str | None = None
                started = time.monotonic()
                try:
                    response = self.ornith.complete(system, user, schema)
                    raw = response.content
                    parse_replan_response(raw, bundle, diagnosis, max_context_tokens=policy.max_context_tokens, max_target_files=policy.max_target_files)
                    error = None
                    model_tag = response.model_tag
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"
                    model_tag = definition.ollama_model
                attempts.append(self._persist_attempt(
                    role="replan", fixture="localized_repair", iteration=iteration, attempt=attempt,
                    request=request, raw=raw, model_id=definition.logical_id, model_tag=model_tag,
                    latency_ms=int(round((time.monotonic() - started) * 1000)), error=error,
                    expected_keys=set(REPLAN_RESPONSE_SCHEMA["required"]),
                ))
                if error is None:
                    valid = True
                    break
                prior_error, prior_response = error, raw
            iterations.append({"iteration": iteration, "valid": valid, "attempts": attempts})
        return aggregate_attempts("replan", "localized_repair", iterations)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Measure real Swarm model protocol conformance")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--role", choices=("judge", "diagnosis", "replan"))
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--iterations", type=int)
    parser.add_argument("--judge-outcome", choices=("passed", "failed", "both"), default="both")
    parser.add_argument("--judge-passed-iterations", type=int, default=20)
    parser.add_argument("--judge-failed-iterations", type=int, default=20)
    parser.add_argument("--diagnosis-iterations", type=int, default=20)
    parser.add_argument("--replan-iterations", type=int, default=10)
    parser.add_argument("--json-output", type=Path, required=True)
    parser.add_argument("--artifact-dir", type=Path)
    return parser


def run(args: argparse.Namespace) -> int:
    if args.all == bool(args.role):
        raise ValueError("select exactly one of --all or --role")
    counts = (args.judge_passed_iterations, args.judge_failed_iterations, args.diagnosis_iterations, args.replan_iterations)
    if any(value <= 0 for value in counts) or (args.iterations is not None and args.iterations <= 0):
        raise ValueError("iteration counts must be positive")
    output = args.json_output.resolve(strict=False)
    artifact_dir = (args.artifact_dir or output.with_suffix("").with_name(output.stem + "_artifacts")).resolve(strict=False)
    probe = ProtocolProbe(root=args.root, artifact_dir=artifact_dir)
    started = datetime.now(timezone.utc).isoformat()
    suites: dict[str, dict[str, Any]] = {}
    diagnosis: FailureDiagnosisV2 | None = None

    if args.all or args.role == "judge":
        passed_count = args.iterations or args.judge_passed_iterations
        failed_count = args.iterations or args.judge_failed_iterations
        if args.judge_outcome in {"passed", "both"}:
            suites["judge_passed_fixture"] = probe.judge(VerificationOutcome.PASSED, passed_count)
        if args.judge_outcome in {"failed", "both"}:
            suites["judge_failed_fixture"] = probe.judge(VerificationOutcome.FAILED, failed_count)
    if args.all or args.role == "diagnosis":
        suites["diagnosis"], diagnosis = probe.diagnosis(args.iterations or args.diagnosis_iterations)
    if args.all or args.role == "replan":
        suites["replan"] = probe.replan(args.iterations or args.replan_iterations, diagnosis)

    thresholds = {
        "judge_passed_fixture": 0.95,
        "judge_failed_fixture": 0.95,
        "diagnosis": 0.95,
        "replan": 0.90,
    }
    passed = all(summary["rate"] >= thresholds[name] for name, summary in suites.items())
    document = {
        "schema_version": 1,
        "started_at": started,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "root": str(probe.root),
        "physical_endpoint": "http://127.0.0.1:11435",
        "target_workspace_accessed": False,
        "artifact_dir": str(artifact_dir),
        "status": "PASS" if passed else "FAIL",
        "thresholds": {name: thresholds[name] for name in suites},
        "suites": suites,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({
        "status": document["status"],
        "json_output": str(output),
        "artifact_dir": str(artifact_dir),
        "suites": {name: {key: value for key, value in summary.items() if key in {"valid", "total", "rate", "model_attempts", "markdown_fence_rate", "unknown_evidence_rate", "schema_shape_failure_rate", "failure_taxonomy"}} for name, summary in suites.items()},
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if passed else 2


def main() -> int:
    try:
        return run(build_parser().parse_args())
    except Exception as exc:
        print(json.dumps({"status": "ERROR", "error_type": type(exc).__name__, "error": str(exc)}, ensure_ascii=False, indent=2, sort_keys=True))
        return 4


if __name__ == "__main__":
    raise SystemExit(main())

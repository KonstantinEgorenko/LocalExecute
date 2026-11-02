'recovery normalization of frozen failure records into evidence-bound recovery inputs.'
from __future__ import annotations
from harness.compatibility.configuration import legacy_keywords

from typing import Any, Mapping, Sequence

from ..evaluation.models import BenchmarkCase
from .models import FailureEvidenceV2, RecoveryEvidenceItem


def _attempt_items(prefix: str, attempts: Sequence[Mapping[str, Any]]) -> list[RecoveryEvidenceItem]:
    items: list[RecoveryEvidenceItem] = []
    for index, attempt in enumerate(attempts):
        profile_id = str(attempt.get("profile_id") or f"unknown-{index}")
        result = attempt.get("result") if isinstance(attempt.get("result"), Mapping) else {}
        verification = str(attempt.get("verification_output", ""))[-12000:]
        items.append(
            RecoveryEvidenceItem(
                f"{prefix}:attempt:{index}:result",
                "ATTEMPT_RESULT",
                f"Failed execution attempt {index} by {profile_id}",
                {
                    "profile_id": profile_id,
                    "verified": bool(attempt.get("verified", False)),
                    "latency_ms": int(attempt.get("latency_ms", 0) or 0),
                    "outcome": result.get("outcome"),
                    "reason": result.get("reason"),
                    "changed_paths": list(result.get("changed_paths", ())),
                    "model_calls": int(result.get("model_calls", 0) or 0),
                    "tool_calls": int(result.get("tool_calls", 0) or 0),
                    "protocol_failures": int(result.get("protocol_failures", 0) or 0),
                    "tool_failures": int(result.get("tool_failures", 0) or 0),
                },
            )
        )
        items.append(
            RecoveryEvidenceItem(
                f"{prefix}:attempt:{index}:verification",
                "DETERMINISTIC_VERIFICATION",
                f"Deterministic verification output for failed {profile_id} attempt",
                {"output": verification},
            )
        )
        final_files = attempt.get("final_files")
        if isinstance(final_files, Mapping):
            items.append(
                RecoveryEvidenceItem(
                    f"{prefix}:attempt:{index}:candidate",
                    "FAILED_CANDIDATE_SNAPSHOT",
                    f"Text snapshot after failed recovery attempt {index}",
                    {"files": dict(final_files)},
                )
            )
    return items


@legacy_keywords({'phase14_record': 'evaluation_record'})
def build_failure_evidence(
    *,
    case: BenchmarkCase,
    fixture: Any,
    evaluation_record: Mapping[str, Any],
    recovery_attempts: Sequence[Mapping[str, Any]] = (),
    max_evidence_items: int = 32,
) -> FailureEvidenceV2:
    if evaluation_record.get("verified") is not False:
        raise ValueError("Phase 15 recovery evidence requires a naturally failed Phase 14 record")
    workspace_paths = tuple(sorted(str(path).replace("\\", "/") for path in fixture.files))
    immutable_paths = tuple(str(path).replace("\\", "/") for path in fixture.immutable_paths)
    items: list[RecoveryEvidenceItem] = [
        RecoveryEvidenceItem(
            "objective",
            "OBJECTIVE",
            "Original objective that produced repeated deterministic failure",
            {"objective": fixture.objective},
        )
    ]
    for path in workspace_paths:
        evidence_type = "TEST_SOURCE" if path in immutable_paths else "SOURCE_FILE"
        items.append(
            RecoveryEvidenceItem(
                f"file:{path}",
                evidence_type,
                f"Frozen pre-attempt text for {path}",
                {"path": path, "content": str(fixture.files[path])},
            )
        )
    historical = evaluation_record.get("attempts")
    if not isinstance(historical, list) or not historical:
        raise ValueError("Phase 14 failed record must contain attempts")
    items.extend(_attempt_items("phase14", historical))
    items.extend(_attempt_items("recovery", recovery_attempts))
    if len(items) > max_evidence_items:
        raise ValueError(f"failure evidence exceeds max_evidence_items={max_evidence_items}")
    failed_profiles: list[str] = []
    for attempt in [*historical, *recovery_attempts]:
        profile = attempt.get("profile_id") if isinstance(attempt, Mapping) else None
        if isinstance(profile, str) and profile and profile not in failed_profiles:
            failed_profiles.append(profile)
    return FailureEvidenceV2(
        task_id=case.case_id,
        objective=fixture.objective,
        source_task_class=case.task_class,
        source_difficulty=case.difficulty,
        source_metadata=dict(case.metadata),
        workspace_paths=workspace_paths,
        immutable_paths=immutable_paths,
        failed_profile_ids=tuple(failed_profiles),
        evidence_items=tuple(items),
    )

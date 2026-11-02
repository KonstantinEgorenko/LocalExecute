'Deterministic recovery recovery control policy.'
from __future__ import annotations
from harness.compatibility.configuration import resolve_config_path, normalize_configuration_document

from pathlib import Path
from typing import Iterable
import yaml

from ..errors import ConfigurationError
from harness.observability.fault_attribution import FaultAttribution, FaultCode, FaultDomain
from harness.router.policy_models import FailureSignalV2, ResourceStateV2, TaskProfileV2
from .models import (
    ContextExpansionRequestV2,
    FailureDiagnosisV2,
    FailureKind,
    RecoveryAction,
    RecoveryPolicy,
    ReplanContractV2,
)


class RecoveryControllerV2:
    def __init__(self, policy: RecoveryPolicy) -> None:
        if not isinstance(policy, RecoveryPolicy):
            raise ValueError("policy must be RecoveryPolicy")
        self.policy = policy

    @classmethod
    def from_project_root(cls, root: Path) -> "RecoveryControllerV2":
        path = resolve_config_path(Path(root) / "config" / "phase15.yaml")
        try:
            document = normalize_configuration_document(yaml.safe_load(path.read_text(encoding="utf-8")))
        except OSError as exc:
            raise ConfigurationError(f"cannot read Phase 15 config: {exc}") from exc
        except yaml.YAMLError as exc:
            raise ConfigurationError(f"malformed Phase 15 YAML: {exc}") from exc
        if not isinstance(document, dict) or document.get("schema_version") != 1:
            raise ConfigurationError("phase15.yaml requires schema_version: 1")
        raw = document.get("failure_recovery_v2")
        if not isinstance(raw, dict):
            raise ConfigurationError("phase15.yaml.failure_recovery_v2 must be mapping")
        live = raw.get("live_acceptance", {})
        if not isinstance(live, dict):
            raise ConfigurationError("phase15 live_acceptance must be mapping")
        try:
            policy = RecoveryPolicy(
                str(raw["diagnosis_model_id"]),
                str(raw["replan_model_id"]),
                int(raw["max_diagnosis_model_attempts"]),
                int(raw["max_replan_model_attempts"]),
                int(raw["max_replans_per_task"]),
                float(raw["min_diagnosis_confidence"]),
                int(raw["max_context_tokens"]),
                int(raw["max_target_files"]),
                int(raw["max_evidence_items"]),
                int(raw["max_response_bytes"]),
                raw["require_fresh_workspace"],
                raw["require_deterministic_verification"],
                int(live["recovery_cases"]),
                float(live["min_recovered_rate"]),
                int(live["max_infrastructure_invalid_runs"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigurationError(f"invalid Phase 15 policy: {exc}") from exc
        return cls(policy)

    def next_action(self, diagnosis: FailureDiagnosisV2, *, replans_used: int) -> RecoveryAction:
        if not isinstance(diagnosis, FailureDiagnosisV2):
            raise ValueError("diagnosis must be FailureDiagnosisV2")
        if not isinstance(replans_used, int) or isinstance(replans_used, bool) or replans_used < 0:
            raise ValueError("replans_used must be integer >= 0")
        if diagnosis.failure_kind is FailureKind.INFRASTRUCTURE:
            return RecoveryAction.RETRY_INFRASTRUCTURE
        if diagnosis.confidence < self.policy.min_diagnosis_confidence:
            return RecoveryAction.HUMAN_REVIEW
        if replans_used >= self.policy.max_replans_per_task:
            return RecoveryAction.STOP
        if diagnosis.context_expansion_required or diagnosis.failure_kind is FailureKind.CONTEXT_INSUFFICIENT:
            return RecoveryAction.EXPAND_CONTEXT
        if diagnosis.replan_required:
            return RecoveryAction.ORNITH_REPLAN
        return RecoveryAction.HUMAN_REVIEW

    def context_request(self, task_id: str, diagnosis: FailureDiagnosisV2) -> ContextExpansionRequestV2:
        if self.next_action(diagnosis, replans_used=0) is not RecoveryAction.EXPAND_CONTEXT:
            raise ValueError("diagnosis does not require context expansion")
        return ContextExpansionRequestV2(
            task_id,
            diagnosis.root_cause,
            diagnosis.implicated_files,
            diagnosis.evidence,
            self.policy.max_context_tokens,
        )

    @staticmethod
    def failure_signals(profile_ids: Iterable[str]) -> tuple[FailureSignalV2, ...]:
        unique: list[str] = []
        for profile_id in profile_ids:
            if profile_id not in unique:
                unique.append(profile_id)
        return tuple(
            FailureSignalV2(
                FaultDomain.VERIFICATION,
                FaultCode.DETERMINISTIC_GATE_FAILED,
                FaultAttribution.CANDIDATE_IMPLEMENTATION,
                profile_id,
            )
            for profile_id in unique
        )

    def task_profile(
        self,
        plan: ReplanContractV2,
        *,
        failed_profile_ids: Iterable[str],
        worker_online: bool = True,
        free_vram_gb: float | None = 14.0,
        reclaimable_vram_gb: float = 0.0,
        loaded_profile_ids: tuple[str, ...] = (),
    ) -> TaskProfileV2:
        if plan.context_tokens > self.policy.max_context_tokens:
            raise ValueError("replan exceeds Phase 15 context policy")
        return TaskProfileV2(
            task_id=f"{plan.source_task_id}:replan",
            task_class=plan.task_class,
            difficulty=plan.difficulty,
            known_target_files=plan.target_files,
            estimated_file_count=len(plan.target_files),
            navigation_need=plan.navigation_need,
            context_tokens=plan.context_tokens,
            dependency_depth=plan.dependency_depth,
            ambiguity=plan.ambiguity,
            risk=plan.risk,
            testable=True,
            parallel_safe=False,
            requires_workspace_mutation=True,
            previous_failures=self.failure_signals(failed_profile_ids),
            resources=ResourceStateV2(
                worker_online,
                free_vram_gb,
                reclaimable_vram_gb,
                loaded_profile_ids,
            ),
        )

'research campaign.R6.6 failure-driven repair domain and policy.\n\nProtected GATE evaluation may reject an immutable candidate.  R6.6 preserves\nthat failed commit forever and, for explicitly repairable candidate-owned\nfailures, may create a *new* hypothesis and ExperimentSpec rooted at the exact\nfailed candidate commit.  TEST is deliberately outside this repair surface.\n'
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, TYPE_CHECKING

from .candidate_evaluation import (
    CandidateEvaluationStatusV1,
    CandidateEvaluationSurfaceV1,
    StoredCandidateEvaluationV1,
)
from .models import (
    ExperimentFailureKindV1,
    ExperimentSpecV1,
    HypothesisModeV1,
    HypothesisStatusV1,
    HypothesisV1,
)
from .serialization import (
    freeze_json,
    non_negative_int,
    plain,
    positive_int,
    safe_id,
    sha256_hex,
    stable_sha256,
    timestamp,
)

if TYPE_CHECKING:  # pragma: no cover
    from .ports import ResearchCampaignContextV1
    from .store import StoredExperimentV1


@dataclass(frozen=True, slots=True)
class GateFailureEvidenceV1:
    """Immutable evidence copied from one terminal protected GATE failure."""

    campaign_id: str
    gate_evaluation_id: str
    gate_evaluation_fingerprint: str
    source_experiment_id: str
    source_experiment_fingerprint: str
    source_candidate_commit: str
    evaluation_contract_hash: str
    status: CandidateEvaluationStatusV1
    failure_kind: ExperimentFailureKindV1
    reason: str | None
    ended_at: str
    artifact_refs: tuple[str, ...] = ()
    diagnostics: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("campaign_id", "gate_evaluation_id", "source_experiment_id"):
            safe_id(getattr(self, name), name)
        for name in (
            "gate_evaluation_fingerprint",
            "source_experiment_fingerprint",
            "evaluation_contract_hash",
        ):
            object.__setattr__(self, name, sha256_hex(getattr(self, name), name))
        commit = self.source_candidate_commit.lower()
        if len(commit) != 40 or any(ch not in "0123456789abcdef" for ch in commit):
            raise ValueError("source_candidate_commit must be full 40-character Git commit")
        object.__setattr__(self, "source_candidate_commit", commit)
        if self.status not in {CandidateEvaluationStatusV1.FAILED, CandidateEvaluationStatusV1.INVALID}:
            raise ValueError("repair evidence requires FAILED or INVALID GATE result")
        if not isinstance(self.failure_kind, ExperimentFailureKindV1):
            raise ValueError("failure_kind must be ExperimentFailureKindV1")
        object.__setattr__(self, "ended_at", timestamp(self.ended_at, "ended_at"))
        refs = tuple(str(item) for item in self.artifact_refs)
        if any(not item for item in refs) or len(set(refs)) != len(refs):
            raise ValueError("artifact_refs must contain unique non-empty strings")
        object.__setattr__(self, "artifact_refs", refs)
        diagnostics = freeze_json(self.diagnostics, "diagnostics")
        if not isinstance(diagnostics, Mapping):
            raise ValueError("diagnostics must be mapping")
        object.__setattr__(self, "diagnostics", diagnostics)
        if self.reason is not None and (not isinstance(self.reason, str) or not self.reason.strip()):
            raise ValueError("reason must be non-empty string when present")

    @property
    def fingerprint(self) -> str:
        return stable_sha256(self.to_dict(include_fingerprint=False))

    def to_dict(self, *, include_fingerprint: bool = True) -> dict[str, Any]:
        payload = {
            "schema_version": 1,
            "campaign_id": self.campaign_id,
            "gate_evaluation_id": self.gate_evaluation_id,
            "gate_evaluation_fingerprint": self.gate_evaluation_fingerprint,
            "source_experiment_id": self.source_experiment_id,
            "source_experiment_fingerprint": self.source_experiment_fingerprint,
            "source_candidate_commit": self.source_candidate_commit,
            "evaluation_contract_hash": self.evaluation_contract_hash,
            "status": self.status.value,
            "failure_kind": self.failure_kind.value,
            "reason": self.reason,
            "ended_at": self.ended_at,
            "artifact_refs": list(self.artifact_refs),
            "diagnostics": plain(self.diagnostics),
        }
        if include_fingerprint:
            payload["fingerprint"] = self.fingerprint
        return payload

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "GateFailureEvidenceV1":
        if raw.get("schema_version") != 1:
            raise ValueError("GateFailureEvidenceV1 schema_version must be 1")
        result = cls(
            campaign_id=str(raw["campaign_id"]),
            gate_evaluation_id=str(raw["gate_evaluation_id"]),
            gate_evaluation_fingerprint=str(raw["gate_evaluation_fingerprint"]),
            source_experiment_id=str(raw["source_experiment_id"]),
            source_experiment_fingerprint=str(raw["source_experiment_fingerprint"]),
            source_candidate_commit=str(raw["source_candidate_commit"]),
            evaluation_contract_hash=str(raw["evaluation_contract_hash"]),
            status=CandidateEvaluationStatusV1(str(raw["status"])),
            failure_kind=ExperimentFailureKindV1(str(raw["failure_kind"])),
            reason=None if raw.get("reason") is None else str(raw["reason"]),
            ended_at=str(raw["ended_at"]),
            artifact_refs=tuple(raw.get("artifact_refs", ())),
            diagnostics=raw.get("diagnostics", {}),
        )
        expected = raw.get("fingerprint")
        if expected is not None and expected != result.fingerprint:
            raise ValueError("GateFailureEvidenceV1 fingerprint mismatch")
        return result

    @classmethod
    def from_candidate_evaluation(cls, stored: StoredCandidateEvaluationV1) -> "GateFailureEvidenceV1":
        if stored.spec.surface is not CandidateEvaluationSurfaceV1.GATE:
            raise ValueError("repair evidence may be created only from GATE evaluation")
        result = stored.result
        if result is None or not result.is_terminal:
            raise ValueError("repair evidence requires terminal GATE evaluation")
        if result.status not in {CandidateEvaluationStatusV1.FAILED, CandidateEvaluationStatusV1.INVALID}:
            raise ValueError("repair evidence requires failed/invalid GATE evaluation")
        if result.failure_kind is None or result.ended_at is None:
            raise ValueError("repair evidence requires failure_kind and ended_at")
        return cls(
            campaign_id=stored.spec.campaign_id,
            gate_evaluation_id=stored.spec.evaluation_id,
            gate_evaluation_fingerprint=stored.spec.fingerprint,
            source_experiment_id=stored.spec.candidate.source_experiment_id,
            source_experiment_fingerprint=stored.spec.candidate.source_experiment_fingerprint,
            source_candidate_commit=stored.spec.candidate.candidate_commit,
            evaluation_contract_hash=stored.spec.evaluation_contract_hash,
            status=result.status,
            failure_kind=result.failure_kind,
            reason=result.reason,
            ended_at=result.ended_at,
            artifact_refs=result.artifact_refs,
            diagnostics=result.telemetry,
        )


@dataclass(frozen=True, slots=True)
class RepairProposalV1:
    """Durable immutable lineage from one GATE failure to one new experiment."""

    repair_id: str
    campaign_id: str
    evidence: GateFailureEvidenceV1
    hypothesis: HypothesisV1
    experiment: ExperimentSpecV1
    repair_depth: int
    created_at: str

    def __post_init__(self) -> None:
        safe_id(self.repair_id, "repair_id")
        safe_id(self.campaign_id, "campaign_id")
        if not isinstance(self.evidence, GateFailureEvidenceV1):
            raise ValueError("evidence must be GateFailureEvidenceV1")
        if not isinstance(self.hypothesis, HypothesisV1):
            raise ValueError("hypothesis must be HypothesisV1")
        if not isinstance(self.experiment, ExperimentSpecV1):
            raise ValueError("experiment must be ExperimentSpecV1")
        if self.evidence.campaign_id != self.campaign_id:
            raise ValueError("repair evidence campaign mismatch")
        if self.hypothesis.campaign_id != self.campaign_id or self.experiment.campaign_id != self.campaign_id:
            raise ValueError("repair proposal components must share campaign_id")
        if self.experiment.hypothesis_id != self.hypothesis.hypothesis_id:
            raise ValueError("repair experiment must reference repair hypothesis")
        if self.evidence.source_experiment_id not in self.experiment.parent_experiment_ids:
            raise ValueError("repair experiment must derive from failed source experiment")
        if self.experiment.code_base_commit != self.evidence.source_candidate_commit:
            raise ValueError("repair experiment code_base_commit must be exact failed candidate commit")
        if self.experiment.evaluation_contract_hash != self.evidence.evaluation_contract_hash:
            raise ValueError("repair experiment must preserve EvaluationContractHash")
        provenance = self.experiment.execution
        expected = {
            "repair_evidence_fingerprint": self.evidence.fingerprint,
            "repair_source_gate_evaluation_id": self.evidence.gate_evaluation_id,
            "repair_source_experiment_id": self.evidence.source_experiment_id,
        }
        for key, value in expected.items():
            if provenance.get(key) != value:
                raise ValueError(f"repair experiment execution.{key} must preserve controller lineage")
        object.__setattr__(self, "repair_depth", positive_int(self.repair_depth, "repair_depth"))
        object.__setattr__(self, "created_at", timestamp(self.created_at, "created_at"))

    @property
    def fingerprint(self) -> str:
        return stable_sha256(
            {
                "schema_version": 1,
                "campaign_id": self.campaign_id,
                "evidence_fingerprint": self.evidence.fingerprint,
                "hypothesis": self.hypothesis.to_dict(),
                "experiment_fingerprint": self.experiment.fingerprint,
                "repair_depth": self.repair_depth,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "repair_id": self.repair_id,
            "campaign_id": self.campaign_id,
            "evidence": self.evidence.to_dict(),
            "hypothesis": self.hypothesis.to_dict(),
            "experiment": self.experiment.to_dict(),
            "repair_depth": self.repair_depth,
            "created_at": self.created_at,
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RepairProposalV1":
        if raw.get("schema_version") != 1:
            raise ValueError("RepairProposalV1 schema_version must be 1")
        result = cls(
            repair_id=str(raw["repair_id"]),
            campaign_id=str(raw["campaign_id"]),
            evidence=GateFailureEvidenceV1.from_dict(raw["evidence"]),
            hypothesis=HypothesisV1.from_dict(raw["hypothesis"]),
            experiment=ExperimentSpecV1.from_dict(raw["experiment"]),
            repair_depth=int(raw["repair_depth"]),
            created_at=str(raw["created_at"]),
        )
        expected = raw.get("fingerprint")
        if expected is not None and expected != result.fingerprint:
            raise ValueError("RepairProposalV1 fingerprint mismatch")
        return result


@dataclass(frozen=True, slots=True)
class RepairRegistrationV1:
    requested_repair_id: str
    canonical_repair_id: str
    fingerprint: str
    repair_experiment_id: str
    created: bool
    reused_existing: bool


class RepairPlanningPort(Protocol):
    def propose(
        self,
        context: "ResearchCampaignContextV1",
        evidence: GateFailureEvidenceV1,
        source: "StoredExperimentV1",
        *,
        repair_depth: int,
        created_at: str,
    ) -> RepairProposalV1 | None: ...


@dataclass(frozen=True, slots=True)
class DeterministicRepairPlannerV1:
    """Safe default planner that turns typed GATE evidence into a new R6 experiment.

    It does not mutate the failed candidate or call a model.  It preserves the
    source model/tool/resource/scope configuration, starts from the exact failed
    commit, and changes only the repair objective plus controller-owned lineage.
    The existing Swarm Lead/worker remains the authority that diagnoses and
    implements the actual code repair.
    """

    created_by: str = "Phase22-R6.6-DeterministicRepairPlannerV1"

    def propose(
        self,
        context: "ResearchCampaignContextV1",
        evidence: GateFailureEvidenceV1,
        source: "StoredExperimentV1",
        *,
        repair_depth: int,
        created_at: str,
    ) -> RepairProposalV1 | None:
        if source.spec.campaign_id != context.state.campaign_id:
            raise ValueError("repair source campaign mismatch")
        raw_objective = source.spec.execution.get("objective")
        if not isinstance(raw_objective, str) or not raw_objective.strip():
            return None
        digest = evidence.fingerprint[:24]
        reason = evidence.reason or evidence.failure_kind.value
        reason = reason[:16_000]
        source_objective_fingerprint = stable_sha256({"objective": raw_objective.strip()})
        objective = (
            "Repair the immutable candidate-owned failure discovered by protected GATE evaluation. "
            "Do not modify the failed candidate commit in place; produce a new descendant candidate. "
            f"Failure kind: {evidence.failure_kind.value}. "
            f"Authoritative protected GATE repair requirement: {reason}. "
            "Treat the protected GATE failure evidence above as authoritative for the repair target. "
            "Preserve the existing candidate's unrelated behavior, experiment scope, evaluation contract, dataset, "
            "model/tool policy, and controller constraints unless the protected GATE requirement explicitly conflicts with them. "
            "Do not replay or follow any prior mutation target that conflicts with the protected GATE requirement."
        )
        hypothesis_id = f"r66h-{digest}"
        experiment_id = f"r66e-{digest}"
        repair_id = f"r66r-{digest}"
        hypothesis = HypothesisV1(
            hypothesis_id=hypothesis_id,
            campaign_id=evidence.campaign_id,
            statement=f"Repair {evidence.failure_kind.value} in candidate {evidence.source_experiment_id}",
            mechanism="Use protected GATE failure evidence to make a bounded descendant repair without changing candidate identity.",
            predicted_effect="The new descendant candidate preserves DEV behavior and no longer reproduces the protected GATE failure.",
            falsifier=f"The descendant still fails protected GATE with {evidence.failure_kind.value} or violates verification/scope constraints.",
            created_by=self.created_by,
            created_at=created_at,
            mode=HypothesisModeV1.RESEARCH_QUALITY,
            parent_hypothesis_ids=(source.spec.hypothesis_id,),
            evidence_refs=(f"gate:{evidence.gate_evaluation_id}",),
            status=HypothesisStatusV1.ACTIVE,
        )
        execution = dict(plain(source.spec.execution))
        execution.update(
            {
                "objective": objective,
                "goal_capsule": f"Repair protected GATE failure for {evidence.source_experiment_id}",
                "repair_evidence_fingerprint": evidence.fingerprint,
                "repair_source_gate_evaluation_id": evidence.gate_evaluation_id,
                "repair_source_experiment_id": evidence.source_experiment_id,
                "repair_source_objective_fingerprint": source_objective_fingerprint,
            }
        )
        experiment = ExperimentSpecV1(
            experiment_id=experiment_id,
            campaign_id=evidence.campaign_id,
            hypothesis_id=hypothesis_id,
            parent_experiment_ids=(evidence.source_experiment_id,),
            strategy_id="r66-repair",
            parameters=plain(source.spec.parameters),
            model_id=source.spec.model_id,
            provider=source.spec.provider,
            model_version=source.spec.model_version,
            prompt_hash=source.spec.prompt_hash,
            sampling_config=plain(source.spec.sampling_config),
            tool_policy_hash=source.spec.tool_policy_hash,
            code_base_commit=evidence.source_candidate_commit,
            candidate_patch_ref=None,
            dataset_fingerprint=source.spec.dataset_fingerprint,
            evaluation_contract_hash=source.spec.evaluation_contract_hash,
            seed=source.spec.seed,
            resource_budget=plain(source.spec.resource_budget),
            execution=execution,
            created_at=created_at,
        )
        return RepairProposalV1(
            repair_id=repair_id,
            campaign_id=evidence.campaign_id,
            evidence=evidence,
            hypothesis=hypothesis,
            experiment=experiment,
            repair_depth=repair_depth,
            created_at=created_at,
        )


@dataclass(frozen=True, slots=True)
class FailureDrivenRepairPolicyV1:
    max_total_repairs: int = 3
    max_depth: int = 2
    repairable_failure_kinds: tuple[ExperimentFailureKindV1, ...] = (
        ExperimentFailureKindV1.IMPLEMENTATION_BUG,
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "max_total_repairs", positive_int(self.max_total_repairs, "max_total_repairs"))
        object.__setattr__(self, "max_depth", positive_int(self.max_depth, "max_depth"))
        kinds = tuple(self.repairable_failure_kinds)
        if not kinds or any(not isinstance(item, ExperimentFailureKindV1) for item in kinds):
            raise ValueError("repairable_failure_kinds must contain ExperimentFailureKindV1")
        if len(set(kinds)) != len(kinds):
            raise ValueError("repairable_failure_kinds must be unique")
        object.__setattr__(self, "repairable_failure_kinds", kinds)

    def evidence_for(self, stored: StoredCandidateEvaluationV1) -> GateFailureEvidenceV1 | None:
        if stored.spec.surface is not CandidateEvaluationSurfaceV1.GATE or stored.result is None:
            return None
        if stored.result.status not in {CandidateEvaluationStatusV1.FAILED, CandidateEvaluationStatusV1.INVALID}:
            return None
        if stored.result.failure_kind not in self.repairable_failure_kinds:
            return None
        return GateFailureEvidenceV1.from_candidate_evaluation(stored)

    def select_failure(
        self,
        rows: tuple[StoredCandidateEvaluationV1, ...],
        *,
        allowed_source_experiment_ids: frozenset[str],
        already_repaired_evaluation_ids: frozenset[str],
    ) -> GateFailureEvidenceV1 | None:
        eligible: list[tuple[int, str, GateFailureEvidenceV1]] = []
        for row in rows:
            if row.spec.candidate.source_experiment_id not in allowed_source_experiment_ids:
                continue
            if row.spec.evaluation_id in already_repaired_evaluation_ids:
                continue
            evidence = self.evidence_for(row)
            if evidence is None:
                continue
            raw_rank = row.spec.metadata.get("top_k_rank", 1_000_000)
            rank = non_negative_int(raw_rank, "metadata.top_k_rank")
            eligible.append((rank, row.spec.evaluation_id, evidence))
        if not eligible:
            return None
        eligible.sort(key=lambda item: (item[0], item[1]))
        return eligible[0][2]

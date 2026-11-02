"""Phase 22.R6.5 immutable candidate evaluation domain.

R6.5 separates candidate creation from protected confirmation/final evaluation.
A candidate selected from DEV is identified by the exact source ExperimentSpec
fingerprint and exact candidate Git commit. GATE/TEST evaluation records may score
or reject that immutable candidate, but they carry no mutation objective and no
coding authority.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping

from .models import ExperimentFailureKindV1
from .serialization import (
    finite_number,
    freeze_json,
    git_commit,
    non_negative_int,
    non_negative_number,
    optional_text,
    optional_timestamp,
    plain,
    safe_id,
    sha256_hex,
    stable_sha256,
    timestamp,
    timestamp_value,
)


class EvaluationProposalKindV1(StrEnum):
    SEARCH = "SEARCH"
    CONFIRMATION = "CONFIRMATION"
    FINAL = "FINAL"


class CandidateEvaluationSurfaceV1(StrEnum):
    GATE = "GATE"
    TEST = "TEST"


class CandidateEvaluationStatusV1(StrEnum):
    PLANNED = "PLANNED"
    RUNNING = "RUNNING"
    SCORED = "SCORED"
    FAILED = "FAILED"
    INVALID = "INVALID"
    CANCELLED = "CANCELLED"


_TERMINAL_CANDIDATE_EVALUATION_STATUSES = {
    CandidateEvaluationStatusV1.SCORED,
    CandidateEvaluationStatusV1.FAILED,
    CandidateEvaluationStatusV1.INVALID,
    CandidateEvaluationStatusV1.CANCELLED,
}


@dataclass(frozen=True, slots=True)
class CandidateReferenceV1:
    """Immutable identity of one successfully produced research candidate."""

    campaign_id: str
    source_experiment_id: str
    source_experiment_fingerprint: str
    candidate_commit: str
    evaluation_contract_hash: str

    def __post_init__(self) -> None:
        safe_id(self.campaign_id, "campaign_id")
        safe_id(self.source_experiment_id, "source_experiment_id")
        object.__setattr__(
            self,
            "source_experiment_fingerprint",
            sha256_hex(self.source_experiment_fingerprint, "source_experiment_fingerprint"),
        )
        commit = git_commit(self.candidate_commit, "candidate_commit")
        if len(commit) != 40:
            raise ValueError("candidate_commit must be full 40-character Git commit")
        object.__setattr__(self, "candidate_commit", commit)
        object.__setattr__(
            self,
            "evaluation_contract_hash",
            sha256_hex(self.evaluation_contract_hash, "evaluation_contract_hash"),
        )

    @property
    def identity_hash(self) -> str:
        return stable_sha256(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "campaign_id": self.campaign_id,
            "source_experiment_id": self.source_experiment_id,
            "source_experiment_fingerprint": self.source_experiment_fingerprint,
            "candidate_commit": self.candidate_commit,
            "evaluation_contract_hash": self.evaluation_contract_hash,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CandidateReferenceV1":
        if raw.get("schema_version") != 1:
            raise ValueError("CandidateReferenceV1 schema_version must be 1")
        return cls(
            campaign_id=str(raw["campaign_id"]),
            source_experiment_id=str(raw["source_experiment_id"]),
            source_experiment_fingerprint=str(raw["source_experiment_fingerprint"]),
            candidate_commit=str(raw["candidate_commit"]),
            evaluation_contract_hash=str(raw["evaluation_contract_hash"]),
        )


@dataclass(frozen=True, slots=True)
class CandidateEvaluationSpecV1:
    """One immutable GATE or TEST evaluation request.

    DEV remains an ExperimentResult concern. R6.5 records only protected GATE and
    TEST evaluations here, so the spec intentionally has no coding objective,
    mutation scope, candidate patch, model selection or execution configuration.
    """

    evaluation_id: str
    campaign_id: str
    candidate: CandidateReferenceV1
    proposal_kind: EvaluationProposalKindV1
    surface: CandidateEvaluationSurfaceV1
    evaluation_contract_hash: str
    seed: int
    created_at: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        safe_id(self.evaluation_id, "evaluation_id")
        safe_id(self.campaign_id, "campaign_id")
        if not isinstance(self.candidate, CandidateReferenceV1):
            raise ValueError("candidate must be CandidateReferenceV1")
        if self.candidate.campaign_id != self.campaign_id:
            raise ValueError("candidate campaign_id must match evaluation campaign_id")
        if not isinstance(self.proposal_kind, EvaluationProposalKindV1):
            raise ValueError("proposal_kind must be EvaluationProposalKindV1")
        if not isinstance(self.surface, CandidateEvaluationSurfaceV1):
            raise ValueError("surface must be protected GATE or TEST CandidateEvaluationSurfaceV1")
        expected_kind = {
            CandidateEvaluationSurfaceV1.GATE: EvaluationProposalKindV1.CONFIRMATION,
            CandidateEvaluationSurfaceV1.TEST: EvaluationProposalKindV1.FINAL,
        }
        if self.surface not in expected_kind:
            raise ValueError("CandidateEvaluationSpecV1 surface must be GATE or TEST")
        if self.proposal_kind is not expected_kind[self.surface]:
            raise ValueError("proposal_kind must match protected evaluation surface")
        contract_hash = sha256_hex(self.evaluation_contract_hash, "evaluation_contract_hash")
        if contract_hash != self.candidate.evaluation_contract_hash:
            raise ValueError("candidate/evaluation EvaluationContractHash mismatch")
        object.__setattr__(self, "evaluation_contract_hash", contract_hash)
        object.__setattr__(self, "seed", non_negative_int(self.seed, "seed"))
        object.__setattr__(self, "created_at", timestamp(self.created_at, "created_at"))
        metadata = freeze_json(self.metadata, "metadata")
        if not isinstance(metadata, Mapping):
            raise ValueError("metadata must be mapping")
        forbidden = {
            "objective",
            "execution",
            "candidate_patch_ref",
            "mutation_scope",
            "allowed_mutation_scope",
            "model_id",
            "provider",
        }
        collision = forbidden.intersection(metadata)
        if collision:
            raise ValueError(
                "candidate evaluation metadata cannot carry coding authority: " + ", ".join(sorted(collision))
            )
        object.__setattr__(self, "metadata", metadata)

    @property
    def fingerprint(self) -> str:
        return stable_sha256(
            {
                "schema_version": 1,
                "candidate_identity_hash": self.candidate.identity_hash,
                "proposal_kind": self.proposal_kind.value,
                "surface": self.surface.value,
                "evaluation_contract_hash": self.evaluation_contract_hash,
                "seed": self.seed,
                "metadata": plain(self.metadata),
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "evaluation_id": self.evaluation_id,
            "campaign_id": self.campaign_id,
            "candidate": self.candidate.to_dict(),
            "proposal_kind": self.proposal_kind.value,
            "surface": self.surface.value,
            "evaluation_contract_hash": self.evaluation_contract_hash,
            "seed": self.seed,
            "created_at": self.created_at,
            "metadata": plain(self.metadata),
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CandidateEvaluationSpecV1":
        if raw.get("schema_version") != 1:
            raise ValueError("CandidateEvaluationSpecV1 schema_version must be 1")
        result = cls(
            evaluation_id=str(raw["evaluation_id"]),
            campaign_id=str(raw["campaign_id"]),
            candidate=CandidateReferenceV1.from_dict(raw["candidate"]),
            proposal_kind=EvaluationProposalKindV1(str(raw["proposal_kind"])),
            surface=CandidateEvaluationSurfaceV1(str(raw["surface"])),
            evaluation_contract_hash=str(raw["evaluation_contract_hash"]),
            seed=int(raw["seed"]),
            created_at=str(raw["created_at"]),
            metadata=raw.get("metadata", {}),
        )
        expected = raw.get("fingerprint")
        if expected is not None and expected != result.fingerprint:
            raise ValueError("CandidateEvaluationSpecV1 fingerprint mismatch")
        return result


@dataclass(frozen=True, slots=True)
class CandidateEvaluationResultV1:
    evaluation_id: str
    campaign_id: str
    status: CandidateEvaluationStatusV1
    started_at: str | None = None
    ended_at: str | None = None
    primary_metric: float | None = None
    secondary_metrics: Mapping[str, float] = field(default_factory=dict)
    metric_variance: float | None = None
    failure_kind: ExperimentFailureKindV1 | None = None
    telemetry: Mapping[str, Any] = field(default_factory=dict)
    artifact_refs: tuple[str, ...] = ()
    reason: str | None = None

    def __post_init__(self) -> None:
        safe_id(self.evaluation_id, "evaluation_id")
        safe_id(self.campaign_id, "campaign_id")
        if not isinstance(self.status, CandidateEvaluationStatusV1):
            raise ValueError("status must be CandidateEvaluationStatusV1")
        object.__setattr__(self, "started_at", optional_timestamp(self.started_at, "started_at"))
        object.__setattr__(self, "ended_at", optional_timestamp(self.ended_at, "ended_at"))
        if self.ended_at is not None and self.started_at is None:
            raise ValueError("ended_at requires started_at")
        if self.started_at is not None and self.ended_at is not None:
            if timestamp_value(self.ended_at) < timestamp_value(self.started_at):
                raise ValueError("ended_at must not precede started_at")
        if self.primary_metric is not None:
            object.__setattr__(self, "primary_metric", finite_number(self.primary_metric, "primary_metric"))
        if not isinstance(self.secondary_metrics, Mapping):
            raise ValueError("secondary_metrics must be mapping")
        secondary: dict[str, float] = {}
        for key, value in self.secondary_metrics.items():
            if not isinstance(key, str) or not key:
                raise ValueError("secondary metric names must be non-empty strings")
            secondary[key] = finite_number(value, f"secondary_metrics.{key}")
        object.__setattr__(self, "secondary_metrics", freeze_json(secondary, "secondary_metrics"))
        if self.metric_variance is not None:
            object.__setattr__(self, "metric_variance", non_negative_number(self.metric_variance, "metric_variance"))
        telemetry = freeze_json(self.telemetry, "telemetry")
        if not isinstance(telemetry, Mapping):
            raise ValueError("telemetry must be mapping")
        object.__setattr__(self, "telemetry", telemetry)
        refs = tuple(optional_text(item, "artifact_refs item", max_length=32_000) for item in self.artifact_refs)
        if any(item is None for item in refs) or len(set(refs)) != len(refs):
            raise ValueError("artifact_refs must contain unique non-empty strings")
        object.__setattr__(self, "artifact_refs", tuple(item for item in refs if item is not None))
        object.__setattr__(self, "reason", optional_text(self.reason, "reason", max_length=32_000))

        if self.status is CandidateEvaluationStatusV1.PLANNED:
            if any(
                value is not None
                for value in (self.started_at, self.ended_at, self.primary_metric, self.metric_variance, self.failure_kind)
            ):
                raise ValueError("PLANNED candidate evaluation cannot contain execution/result fields")
        elif self.status is CandidateEvaluationStatusV1.RUNNING:
            if self.started_at is None or self.ended_at is not None:
                raise ValueError("RUNNING candidate evaluation requires started_at and no ended_at")
            if self.primary_metric is not None or self.failure_kind is not None:
                raise ValueError("RUNNING candidate evaluation cannot contain terminal outcome")
        else:
            if self.started_at is None or self.ended_at is None:
                raise ValueError("terminal candidate evaluation requires started_at and ended_at")
            if self.status is CandidateEvaluationStatusV1.SCORED:
                if self.primary_metric is None:
                    raise ValueError("SCORED candidate evaluation requires primary_metric")
                if self.failure_kind is not None:
                    raise ValueError("SCORED candidate evaluation cannot contain failure_kind")
            else:
                if self.failure_kind is None and self.status is not CandidateEvaluationStatusV1.CANCELLED:
                    raise ValueError(f"{self.status.value} candidate evaluation requires failure_kind")
                if self.primary_metric is not None:
                    raise ValueError(f"{self.status.value} candidate evaluation cannot contain primary_metric")

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL_CANDIDATE_EVALUATION_STATUSES

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "evaluation_id": self.evaluation_id,
            "campaign_id": self.campaign_id,
            "status": self.status.value,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "primary_metric": self.primary_metric,
            "secondary_metrics": plain(self.secondary_metrics),
            "metric_variance": self.metric_variance,
            "failure_kind": None if self.failure_kind is None else self.failure_kind.value,
            "telemetry": plain(self.telemetry),
            "artifact_refs": list(self.artifact_refs),
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CandidateEvaluationResultV1":
        if raw.get("schema_version") != 1:
            raise ValueError("CandidateEvaluationResultV1 schema_version must be 1")
        return cls(
            evaluation_id=str(raw["evaluation_id"]),
            campaign_id=str(raw["campaign_id"]),
            status=CandidateEvaluationStatusV1(str(raw["status"])),
            started_at=None if raw.get("started_at") is None else str(raw["started_at"]),
            ended_at=None if raw.get("ended_at") is None else str(raw["ended_at"]),
            primary_metric=None if raw.get("primary_metric") is None else float(raw["primary_metric"]),
            secondary_metrics=raw.get("secondary_metrics", {}),
            metric_variance=None if raw.get("metric_variance") is None else float(raw["metric_variance"]),
            failure_kind=(
                None if raw.get("failure_kind") is None else ExperimentFailureKindV1(str(raw["failure_kind"]))
            ),
            telemetry=raw.get("telemetry", {}),
            artifact_refs=tuple(raw.get("artifact_refs", ())),
            reason=None if raw.get("reason") is None else str(raw["reason"]),
        )


@dataclass(frozen=True, slots=True)
class StoredCandidateEvaluationV1:
    spec: CandidateEvaluationSpecV1
    result: CandidateEvaluationResultV1 | None


@dataclass(frozen=True, slots=True)
class CandidateEvaluationRegistrationV1:
    requested_evaluation_id: str
    canonical_evaluation_id: str
    fingerprint: str
    created: bool
    reused_existing: bool

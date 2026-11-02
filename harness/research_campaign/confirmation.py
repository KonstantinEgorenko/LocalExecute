'Immutable confirmation/final-evaluation policy for research campaign.R6.5.\n\nThe protected-evaluation path deliberately has no mutation or coding authority.\nIt ranks already-terminal DEV candidates and asks a CandidateEvaluationPort to\nscore the exact immutable candidate commit on GATE or TEST.\n'
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence

from .candidate_evaluation import (
    CandidateEvaluationResultV1,
    CandidateEvaluationSpecV1,
    CandidateEvaluationSurfaceV1,
    CandidateReferenceV1,
    EvaluationProposalKindV1,
    StoredCandidateEvaluationV1,
)
from .contracts import MetricDirectionV1
from .models import ExperimentStatusV1
from .serialization import non_negative_int, positive_int, stable_sha256, timestamp
from .store import StoredExperimentV1


class CandidateEvaluationPort(Protocol):
    """Trusted scorer for one immutable candidate commit.

    Implementations may materialize/read the exact commit and run trusted
    evaluator infrastructure, but the request intentionally carries no coding
    objective, patch, mutation scope, model routing, or worker authority.
    """

    def evaluate(
        self,
        spec: CandidateEvaluationSpecV1,
        *,
        resume: bool,
    ) -> CandidateEvaluationResultV1: ...


@dataclass(frozen=True, slots=True)
class CandidateEvaluationPolicyV1:
    top_k: int = 3
    gate_seed: int = 0
    test_seed: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "top_k", positive_int(self.top_k, "top_k"))
        object.__setattr__(self, "gate_seed", non_negative_int(self.gate_seed, "gate_seed"))
        object.__setattr__(self, "test_seed", non_negative_int(self.test_seed, "test_seed"))

    @staticmethod
    def _metric_key(metric: float, direction: MetricDirectionV1) -> float:
        if direction is MetricDirectionV1.MAXIMIZE:
            return -metric
        if direction is MetricDirectionV1.MINIMIZE:
            return metric
        raise ValueError(f"unsupported metric direction: {direction!r}")

    def top_dev_candidates(
        self,
        experiments: Sequence[StoredExperimentV1],
        *,
        direction: MetricDirectionV1,
        evaluation_contract_hash: str,
    ) -> tuple[StoredExperimentV1, ...]:
        eligible: list[StoredExperimentV1] = []
        for item in experiments:
            result = item.result
            if item.spec.evaluation_contract_hash != evaluation_contract_hash:
                continue
            if (
                result is None
                or result.status is not ExperimentStatusV1.SUCCEEDED
                or result.primary_metric is None
                or result.candidate_commit is None
            ):
                continue
            eligible.append(item)
        eligible.sort(
            key=lambda item: (
                self._metric_key(float(item.result.primary_metric), direction),  # type: ignore[union-attr]
                item.spec.experiment_id,
            )
        )
        return tuple(eligible[: self.top_k])

    def best_gate_evaluation(
        self,
        evaluations: Sequence[StoredCandidateEvaluationV1],
        *,
        direction: MetricDirectionV1,
        allowed_source_experiment_ids: frozenset[str],
    ) -> StoredCandidateEvaluationV1 | None:
        scored = [
            item
            for item in evaluations
            if item.spec.surface is CandidateEvaluationSurfaceV1.GATE
            and item.spec.candidate.source_experiment_id in allowed_source_experiment_ids
            and item.result is not None
            and item.result.status.value == "SCORED"
            and item.result.primary_metric is not None
        ]
        if not scored:
            return None
        scored.sort(
            key=lambda item: (
                self._metric_key(float(item.result.primary_metric), direction),  # type: ignore[union-attr]
                item.spec.candidate.source_experiment_id,
                item.spec.evaluation_id,
            )
        )
        return scored[0]

    @staticmethod
    def candidate_reference(item: StoredExperimentV1) -> CandidateReferenceV1:
        result = item.result
        if (
            result is None
            or result.status is not ExperimentStatusV1.SUCCEEDED
            or result.candidate_commit is None
        ):
            raise ValueError("candidate reference requires successful source experiment commit")
        return CandidateReferenceV1(
            campaign_id=item.spec.campaign_id,
            source_experiment_id=item.spec.experiment_id,
            source_experiment_fingerprint=item.spec.fingerprint,
            candidate_commit=result.candidate_commit,
            evaluation_contract_hash=item.spec.evaluation_contract_hash,
        )

    def gate_spec(
        self,
        item: StoredExperimentV1,
        *,
        rank: int,
        created_at: str,
    ) -> CandidateEvaluationSpecV1:
        candidate = self.candidate_reference(item)
        digest = stable_sha256(
            {
                "candidate_identity_hash": candidate.identity_hash,
                "surface": CandidateEvaluationSurfaceV1.GATE.value,
                "seed": self.gate_seed,
            }
        )
        return CandidateEvaluationSpecV1(
            evaluation_id=f"r65e-gate-{digest[:24]}",
            campaign_id=item.spec.campaign_id,
            candidate=candidate,
            proposal_kind=EvaluationProposalKindV1.CONFIRMATION,
            surface=CandidateEvaluationSurfaceV1.GATE,
            evaluation_contract_hash=item.spec.evaluation_contract_hash,
            seed=self.gate_seed,
            created_at=timestamp(created_at, "created_at"),
            metadata={"top_k_rank": rank},
        )

    def test_spec(
        self,
        item: StoredExperimentV1,
        *,
        gate_evaluation_id: str,
        created_at: str,
    ) -> CandidateEvaluationSpecV1:
        candidate = self.candidate_reference(item)
        digest = stable_sha256(
            {
                "candidate_identity_hash": candidate.identity_hash,
                "surface": CandidateEvaluationSurfaceV1.TEST.value,
                "seed": self.test_seed,
            }
        )
        return CandidateEvaluationSpecV1(
            evaluation_id=f"r65e-test-{digest[:24]}",
            campaign_id=item.spec.campaign_id,
            candidate=candidate,
            proposal_kind=EvaluationProposalKindV1.FINAL,
            surface=CandidateEvaluationSurfaceV1.TEST,
            evaluation_contract_hash=item.spec.evaluation_contract_hash,
            seed=self.test_seed,
            created_at=timestamp(created_at, "created_at"),
            metadata={"confirmed_gate_evaluation_id": gate_evaluation_id},
        )

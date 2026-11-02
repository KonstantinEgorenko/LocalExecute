"""Public fingerprint helpers for Phase 22 research identity."""
from __future__ import annotations

from .contracts import EvaluationContractV1
from .models import ExperimentSpecV1
from .serialization import canonical_json, stable_sha256


def evaluation_contract_hash(contract: EvaluationContractV1) -> str:
    if not isinstance(contract, EvaluationContractV1):
        raise ValueError("contract must be EvaluationContractV1")
    return contract.contract_hash


def experiment_fingerprint(spec: ExperimentSpecV1) -> str:
    if not isinstance(spec, ExperimentSpecV1):
        raise ValueError("spec must be ExperimentSpecV1")
    return spec.fingerprint


__all__ = [
    "canonical_json",
    "evaluation_contract_hash",
    "experiment_fingerprint",
    "stable_sha256",
]

'Typed, revision-bound projection of verification evidence for Critic input.'
from __future__ import annotations

from ..verification.models import (
    VerificationReport, GateProvenance, VerificationSubject, ExecutionPurpose,
    GateOutcome, source_revision_identity,
)
from .errors import CriticIntegrityError


def verification_context(bundle):
    try:
        report = VerificationReport.from_dict(bundle.verification_report)
        if (report.report_id != bundle.verification_report_id or report.run_id != bundle.run_id
                or report.todo_id != bundle.todo_id or report.candidate_head != bundle.candidate_head
                or report.candidate_diff_sha256 != bundle.candidate_diff_sha256
                or report.outcome is not bundle.verification_outcome):
            raise ValueError("Critic/Phase7 report or revision identity mismatch")
        candidate = source_revision_identity(report.candidate_head, report.candidate_diff_sha256)
        groups = {purpose.value: [] for purpose in ExecutionPurpose}
        semantic_results = []
        for gate in report.results:
            expected_purpose = (ExecutionPurpose.REGRESSION_NEGATIVE_CONTROL if gate.gate_id == 'regression_oracle' else
                                ExecutionPurpose.IMMUTABLE_BASELINE if gate.gate_id == 'immutable_baseline_tests' else
                                ExecutionPurpose.CANDIDATE_VALIDATION)
            provenance = gate.provenance
            if provenance is None:
                # Legacy ordinary gates still describe the current candidate.
                # A shadow execution cannot acquire provenance by inference.
                if expected_purpose is not ExecutionPurpose.CANDIDATE_VALIDATION:
                    raise ValueError("shadow verification gate missing authoritative provenance")
                provenance = GateProvenance(VerificationSubject.CANDIDATE, expected_purpose,
                    gate.exit_code, gate.outcome, False, report.report_id, candidate)
            expected_identity = bundle.original_source_revision_identity if expected_purpose is ExecutionPurpose.REGRESSION_NEGATIVE_CONTROL else candidate
            if provenance.execution_purpose is not expected_purpose or provenance.source_revision_identity != expected_identity:
                raise ValueError("verification provenance purpose or source revision mismatch")
            observation = {"gate_id": gate.gate_id, "required": gate.required,
                           **provenance.to_dict(), "detail": gate.detail,
                           "stdout": gate.stdout, "stderr": gate.stderr,
                           "output_truncated": gate.output_truncated}
            if expected_purpose is ExecutionPurpose.REGRESSION_NEGATIVE_CONTROL:
                observation['expected_failure_observed'] = gate.outcome is GateOutcome.PASSED
                observation['meaning'] = 'Original SOURCE negative control; expected regression assertion failures prove the tests detect the original defect. Not candidate verification failure.'
            groups[expected_purpose.value].append(observation)
            semantic_results.append({"gate_id": gate.gate_id, "required": gate.required,
                                     "outcome": gate.outcome.value, "provenance": provenance.to_dict()})
        required = [g for g in report.results if g.required]
        return {
            "authoritative_phase7": {"subject_revision": "CANDIDATE", "deterministic_outcome": report.outcome.value,
                "required_gate_count": len(required), "required_gates_passed": sum(g.outcome is GateOutcome.PASSED for g in required),
                "verification_report_id": report.report_id, "source_revision_identity": candidate,
                "deterministic_outcome_is_immutable": True},
            "deterministic_verification": {**{k:v for k,v in report.to_dict().items() if k != 'results'}, "results": semantic_results},
            "candidate_validation": groups[ExecutionPurpose.CANDIDATE_VALIDATION.value],
            "immutable_baseline_verification": groups[ExecutionPurpose.IMMUTABLE_BASELINE.value],
            "regression_negative_controls": groups[ExecutionPurpose.REGRESSION_NEGATIVE_CONTROL.value],
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise CriticIntegrityError(f"invalid Critic verification provenance: {exc}") from exc

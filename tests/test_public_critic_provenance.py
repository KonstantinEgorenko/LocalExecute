import copy
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from harness.critic.models import CriticEvidenceBundle, CriticEvidenceItem, CriticPolicy
from harness.critic.errors import CriticIntegrityError, CriticProtocolError
from harness.critic.prompt import CriticPromptBuilder
from harness.critic.protocol import parse_critic_response
from harness.critic.verification_context import verification_context
from harness.verification.models import (
    VerificationReport, GateProvenance, VerificationSubject, ExecutionPurpose,
    GateOutcome, VerificationOutcome, source_revision_identity,
)

def synthetic_capture():
    # Deliberately synthetic first-party fixture, not a captured private request.
    from harness.verification.models import GateExecutionResult, VerificationGateType
    results=tuple(GateExecutionResult(name,VerificationGateType.TESTS,True,GateOutcome.PASSED,1,
        'Synthetic unit-test evidence',1 if name=='regression_oracle' else 0,
        'FAILED: 2 failed (synthetic negative control)' if name=='regression_oracle' else 'synthetic pass')
        for name in ('candidate_diff','scope_guard','python_syntax','immutable_baseline_tests',
                     'pytest','regression_change','regression_oracle','regression_preservation'))
    report=VerificationReport('report','plan','a'*64,'run','todo','lease','b'*40,'c'*64,
        ('example.py',),'2026-01-01T00:00:00Z','2026-01-01T00:00:01Z',results,VerificationOutcome.PASSED)
    return {'request':{'deterministic_verification':report.to_dict(),
        'candidate':{'diff':'synthetic public candidate diff'},'work_contract':{},'allowed_evidence_ids':['verified']},
        'response':{'message':{'content':json.dumps(dict(deterministic_outcome='FAILED',verdict='CONCERN',
            failure_class='IMPLEMENTATION_ERROR',confidence=.9,summary='Synthetic invalid override',
            evidence=['verified'],recommended_action='HUMAN_REVIEW',retry_guidance=[],can_override_deterministic=False))}}}



def synthetic_bundle():
    captured = synthetic_capture()
    request = captured['request']; report = copy.deepcopy(request['deterministic_verification'])
    candidate = source_revision_identity(report['candidate_head'], report['candidate_diff_sha256'])
    original = source_revision_identity(report['candidate_head'], hashlib.sha256(b'').hexdigest())
    for gate in report['results']:
        negative = gate['gate_id'] == 'regression_oracle'
        purpose = (ExecutionPurpose.REGRESSION_NEGATIVE_CONTROL if negative else
                   ExecutionPurpose.IMMUTABLE_BASELINE if gate['gate_id'] == 'immutable_baseline_tests' else
                   ExecutionPurpose.CANDIDATE_VALIDATION)
        gate['provenance'] = GateProvenance(
            VerificationSubject.ORIGINAL_SOURCE if negative else VerificationSubject.CANDIDATE,
            purpose, gate['exit_code'], GateOutcome(gate['outcome']), negative, report['report_id'],
            original if negative else candidate).to_dict()
    return CriticEvidenceBundle('request', 'f'*64, report['run_id'], 'goal', report['todo_id'], 'objective',
        report['report_id'], VerificationOutcome.PASSED, report['candidate_head'], report['candidate_diff_sha256'],
        request['candidate']['diff'], tuple(report['dirty_paths']), request['work_contract'], report,
        tuple(CriticEvidenceItem(i, 'VERIFICATION_REPORT', 'VerificationRunner', 'PASSED', 'evidence')
              for i in request['allowed_evidence_ids']), original)


def response(bundle, concern=False):
    return json.dumps(dict(deterministic_outcome=bundle.verification_outcome.value,
        verdict='CONCERN' if concern else 'ACCEPT', failure_class='IMPLEMENTATION_ERROR' if concern else 'NONE',
        confidence=.9, summary='Independent concern about candidate scope' if concern else 'Candidate and negative control independently validated',
        evidence=[next(iter(bundle.allowed_evidence_ids))], recommended_action='HUMAN_REVIEW' if concern else 'NONE',
        retry_guidance=[], can_override_deterministic=False))


def test_synthetic_input_has_separate_typed_subjects_and_immutable_authority():
    bundle = synthetic_bundle()
    request = json.loads(CriticPromptBuilder(CriticPolicy()).messages(bundle)[1].content.split('\n', 1)[1])
    authority = request['authoritative_phase7']
    assert authority['deterministic_outcome'] == 'PASSED'
    assert authority['required_gate_count'] == authority['required_gates_passed'] == 8
    oracle = request['regression_negative_controls'][0]
    assert oracle['subject_revision'] == 'ORIGINAL_SOURCE'
    assert oracle['execution_purpose'] == 'REGRESSION_NEGATIVE_CONTROL'
    assert oracle['raw_exit_code'] == 1 and oracle['gate_outcome'] == 'PASSED'
    assert oracle['expected_failure'] and oracle['expected_failure_observed']
    assert '2 failed' in oracle['stdout'] and 'FAILED' in oracle['stdout']
    assert oracle['verification_report_id'] == bundle.verification_report_id
    assert oracle['source_revision_identity'] != authority['source_revision_identity']
    assert all(x['subject_revision'] == 'CANDIDATE' for x in request['candidate_validation'])
    assert request['immutable_baseline_verification'][0]['execution_purpose'] == 'IMMUTABLE_BASELINE'
    assert all('stdout' not in x for x in request['deterministic_verification']['results'])
    assert VerificationReport.from_dict(bundle.verification_report).outcome is VerificationOutcome.PASSED


def test_synthetic_invalid_response_remains_rejected():
    captured = synthetic_capture()
    with pytest.raises(CriticProtocolError, match='changed deterministic verification outcome'):
        parse_critic_response(captured['response']['message']['content'], synthetic_bundle())


@pytest.mark.parametrize('concern', [False, True])
def test_correct_response_or_grounded_concern_preserves_outcome(concern):
    bundle = synthetic_bundle()
    result = parse_critic_response(response(bundle, concern), bundle)
    assert result.deterministic_outcome is VerificationOutcome.PASSED
    assert result.verdict.value == ('CONCERN' if concern else 'ACCEPT')


@pytest.mark.parametrize('gate_id,exit_code', [('pytest', 1), ('regression_oracle', 0)])
def test_candidate_failure_or_unexpected_negative_control_pass_stays_failed(gate_id, exit_code):
    bundle = synthetic_bundle(); report = copy.deepcopy(dict(bundle.verification_report))
    gate = next(g for g in report['results'] if g['gate_id'] == gate_id)
    gate['outcome'] = gate['provenance']['gate_outcome'] = 'FAILED'
    gate['exit_code'] = gate['provenance']['raw_exit_code'] = exit_code
    report['outcome'] = 'FAILED'
    bundle = replace(bundle, verification_report=report, verification_outcome=VerificationOutcome.FAILED)
    context = verification_context(bundle)
    assert context['authoritative_phase7']['deterministic_outcome'] == 'FAILED'
    if gate_id == 'regression_oracle':
        assert not context['regression_negative_controls'][0]['expected_failure_observed']


@pytest.mark.parametrize('field,value', [('verification_report_id', 'wrong'), ('source_revision_identity', 'a'*64),
    ('execution_purpose', 'CANDIDATE_VALIDATION'), ('raw_exit_code', 0), ('expected_failure', False), ('gate_outcome', 'FAILED')])
def test_corrupted_provenance_fails_closed(field, value):
    bundle = synthetic_bundle(); report = copy.deepcopy(dict(bundle.verification_report))
    next(g for g in report['results'] if g['gate_id'] == 'regression_oracle')['provenance'][field] = value
    with pytest.raises(CriticIntegrityError):
        CriticPromptBuilder(CriticPolicy()).messages(replace(bundle, verification_report=report))


def test_missing_negative_control_provenance_fails_closed():
    bundle = synthetic_bundle(); report = copy.deepcopy(dict(bundle.verification_report))
    next(g for g in report['results'] if g['gate_id'] == 'regression_oracle').pop('provenance')
    with pytest.raises(CriticIntegrityError, match='missing authoritative provenance'):
        verification_context(replace(bundle, verification_report=report))


@pytest.mark.parametrize('field,value', [('report_id', 'wrong'), ('candidate_head', 'wrong'), ('run_id', 'wrong')])
def test_report_identity_mismatch_fails_closed(field, value):
    bundle = synthetic_bundle(); report = copy.deepcopy(dict(bundle.verification_report));report[field] = value
    with pytest.raises(CriticIntegrityError):verification_context(replace(bundle, verification_report=report))


@pytest.mark.parametrize('outcome', ['ERROR', 'TIMEOUT'])
def test_negative_control_process_errors_cannot_become_success(outcome):
    bundle = synthetic_bundle(); report = copy.deepcopy(dict(bundle.verification_report))
    gate = next(g for g in report['results'] if g['gate_id'] == 'regression_oracle')
    gate['outcome'] = gate['provenance']['gate_outcome'] = outcome
    gate['exit_code'] = gate['provenance']['raw_exit_code'] = None
    report['outcome'] = 'FAILED'
    context = verification_context(replace(bundle, verification_report=report, verification_outcome=VerificationOutcome.FAILED))
    assert context['authoritative_phase7']['deterministic_outcome'] == 'FAILED'
    assert not context['regression_negative_controls'][0]['expected_failure_observed']

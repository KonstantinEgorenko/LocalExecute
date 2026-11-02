"""Frozen normative authority at both constrained-generation and raw boundaries."""
from dataclasses import replace
import json
from pathlib import Path
import pytest
from test_semantic_acceptance import bundle, response
from harness.judge import JudgeProtocolError, parse_judge_response
from harness.judge.protocol import judge_response_schema
from harness.verification.acceptance import independent_obligation_refs
from harness.protocol_transport import ollama_compatible_schema

def branches(b, oid, status='SATISFIED'):
    return [row for row in judge_response_schema(b)['properties']['acceptance_evidence']['items']['oneOf'] if oid in row['properties']['obligation_id']['enum'] and status in row['properties']['status']['enum']]

def test_every_obligation_has_exact_authoritative_domain(tmp_path):
    b = bundle(tmp_path)
    for obligation in b.acceptance_obligations.obligations:
        row, = branches(b, obligation.obligation_id)
        basis = row['properties']['expectation_basis']
        assert set(basis['items']['enum']) == independent_obligation_refs(b.acceptance_obligations, obligation)
        assert basis['minItems'] == 1 and basis['uniqueItems'] is True
        assert basis['maxItems'] <= 8
        projected = ollama_compatible_schema(row)
        assert projected['properties']['expectation_basis']['items'] == basis['items']
    assert parse_judge_response(json.dumps(response(b)), b).decision.value == 'ACCEPT'

def test_exact_e3_verification_row_rejected_in_schema_and_raw_parser(tmp_path):
    b = bundle(tmp_path)
    raw = response(b)
    row = next((r for r in raw['acceptance_evidence'] if r['obligation_id'] == 'verification'))
    row.update(evidence_refs=['original_objective', 'public_contract_0_3', 'verification_report'], expectation_basis=['original_objective', 'public_contract_0_3'])
    schema_row, = branches(b, 'verification')
    assert schema_row['properties']['expectation_basis']['items']['enum'] == ['original_constraints']
    assert not set(row['expectation_basis']).issubset(schema_row['properties']['expectation_basis']['items']['enum'])
    with pytest.raises(JudgeProtocolError, match='obligation-incompatible'):
        parse_judge_response(json.dumps(raw), b)

@pytest.mark.parametrize('ref', ['original_source_0', 'candidate_diff', 'generated_test_diff', 'verification_report', 'critic_report', 'explorer_hypotheses', 'original_objective'])
@pytest.mark.parametrize('mixed', [False, True])
def test_behavior_rejects_non_normative_basis_even_mixed_with_valid(tmp_path, ref, mixed):
    b = bundle(tmp_path)
    raw = response(b)
    row = next((r for r in raw['acceptance_evidence'] if r['obligation_id'].startswith('public_contract')))
    row['expectation_basis'] = ([row['obligation_id']] if mixed else []) + [ref]
    schema_row, = branches(b, row['obligation_id'])
    assert ref not in schema_row['properties']['expectation_basis']['items']['enum']
    with pytest.raises(JudgeProtocolError, match='obligation-incompatible'):
        parse_judge_response(json.dumps(raw), b)

def test_empty_normative_domain_allows_unresolved_never_satisfied(tmp_path):
    b = bundle(tmp_path)
    frozen = b.acceptance_obligations
    frozen = replace(frozen, obligations=tuple((replace(o, evidence_refs=('original_source_0',)) if o.obligation_id == 'verification' else o for o in frozen.obligations)))
    b = replace(b, acceptance_obligations=frozen)
    assert not branches(b, 'verification')
    row, = branches(b, 'verification', 'UNRESOLVED')
    assert row['properties']['expectation_basis']['maxItems'] == 0
    raw = response(b, status='UNRESOLVED', decision='REPLAN')
    assert parse_judge_response(json.dumps(raw), b).decision.value == 'REPLAN'
    next((r for r in raw['acceptance_evidence'] if r['obligation_id'] == 'verification'))['status'] = 'SATISFIED'
    with pytest.raises(JudgeProtocolError, match='nonempty'):
        parse_judge_response(json.dumps(raw), b)

def test_verification_support_and_deterministic_identity_bound_in_schema(tmp_path):
    b = bundle(tmp_path)
    schema = judge_response_schema(b)
    assert schema['properties']['deterministic_outcome']['enum'] == ['PASSED']
    row, = branches(b, 'verification')
    assert row['properties']['evidence_refs'] == {'type': 'array', 'minItems': 1, 'maxItems': 1, 'items': {'type': 'string', 'enum': ['verification_report']}}
    assert row['properties']['candidate_evidence']['enum'] == ['candidate_diff']
    raw = response(b)
    next((r for r in raw['acceptance_evidence'] if r['obligation_id'] == 'verification'))['evidence_refs'] = ['candidate_diff']
    with pytest.raises(JudgeProtocolError, match='requires verification_report'):
        parse_judge_response(json.dumps(raw), b)

def test_readme_contract_is_separate_normative_domain(tmp_path):
    from harness.verification.acceptance import build_acceptance_obligations
    from tests.judge_helpers import setup_failed_with_critic
    b = bundle(tmp_path / 'bundle')
    *_, contract, lease, verification, critic = setup_failed_with_critic(tmp_path / 'contract')
    frozen = build_acceptance_obligations(run_id='r', contract=contract, sources={}, tests={}, public_contracts={'README.md': 'Public rule: return input plus one.'})
    b = replace(b, acceptance_obligations=frozen)
    obligation = next((o for o in frozen.obligations if o.obligation_id.startswith('public_spec')))
    row, = branches(b, obligation.obligation_id)
    assert row['properties']['expectation_basis']['items']['enum'] == [obligation.obligation_id]

@pytest.mark.parametrize('refs', [[], ['original_constraints'], ['critic_report'], ['candidate_diff'], ['verification_report', 'original_constraints'], ['verification_report', 'critic_report'], ['verification_report', 'verification_report']])
def test_verification_support_requires_exact_singleton_even_with_report(tmp_path, refs):
    b = bundle(tmp_path)
    raw = response(b)
    row = next((r for r in raw['acceptance_evidence'] if r['obligation_id'] == 'verification'))
    row['evidence_refs'] = refs
    with pytest.raises(JudgeProtocolError, match='sole evidence ref'):
        parse_judge_response(json.dumps(raw), b)

def test_correct_verification_separates_normative_and_deterministic_support(tmp_path):
    b = bundle(tmp_path)
    raw = response(b)
    analysis = parse_judge_response(json.dumps(raw), b)
    row = next((r for r in analysis.acceptance_evidence if r.obligation_id == 'verification'))
    assert row.status == 'SATISFIED' and row.support == 'INDEPENDENT'
    assert row.evidence_refs == ('verification_report',) and row.expectation_basis == ('original_constraints',)
    for obligation in b.acceptance_obligations.obligations:
        if obligation.kind != 'VERIFICATION':
            branch, = branches(b, obligation.obligation_id)
            assert 'candidate_diff' in branch['properties']['evidence_refs']['items']['enum']

def test_support_binding_uses_obligation_kind_not_historical_id(tmp_path):
    b = bundle(tmp_path)
    b = replace(b, acceptance_obligations=replace(b.acceptance_obligations, obligations=tuple((replace(o, obligation_id='deterministic_checks') if o.kind == 'VERIFICATION' else o for o in b.acceptance_obligations.obligations))))
    branch, = branches(b, 'deterministic_checks')
    assert branch['properties']['evidence_refs']['items']['enum'] == ['verification_report']
    raw = response(b)
    assert parse_judge_response(json.dumps(raw), b).decision.value == 'ACCEPT'
    next((r for r in raw['acceptance_evidence'] if r['obligation_id'] == 'deterministic_checks'))['evidence_refs'] = ['original_constraints']
    with pytest.raises(JudgeProtocolError, match='sole evidence ref'):
        parse_judge_response(json.dumps(raw), b)

def test_failed_verification_cannot_accept_even_with_correct_verification_row(tmp_path):
    from harness.verification import VerificationOutcome, GateOutcome
    b = bundle(tmp_path)
    b = replace(b, verification_report=replace(b.verification_report, outcome=VerificationOutcome.FAILED, results=tuple((replace(g, outcome=GateOutcome.FAILED) for g in b.verification_report.results))))
    assert 'ACCEPT' not in judge_response_schema(b)['properties']['decision']['enum']
    raw = response(b)
    with pytest.raises(JudgeProtocolError):
        parse_judge_response(json.dumps(raw), b)
    raw['can_override_deterministic'] = True
    with pytest.raises(JudgeProtocolError, match='must be false'):
        parse_judge_response(json.dumps(raw), b)

def test_production_projector_transmits_singleton_support_without_output_repair(tmp_path):
    from harness.judge import JudgePolicy
    from harness.judge.prompt import JudgePromptBuilder
    from harness.integration.protocol_transport import SchemaConstrainedOrnithRuntime
    from harness.lead.runtime import LeadRuntimeResponse
    b = bundle(tmp_path)
    system, user = JudgePromptBuilder(JudgePolicy()).prompts(b)
    raw = response(b)
    next((r for r in raw['acceptance_evidence'] if r['obligation_id'] == 'verification'))['evidence_refs'] = ['original_constraints']
    content = json.dumps(raw)

    class Runtime:

        def complete(self, system, user, schema):
            rows = schema['properties']['acceptance_evidence']['items']['oneOf']
            branch = next((r for r in rows if r['properties']['obligation_id']['enum'] == ['verification'] and r['properties']['status']['enum'] == ['SATISFIED']))
            assert branch['properties']['evidence_refs']['items']['enum'] == ['verification_report']
            assert branch['properties']['evidence_refs']['minItems'] == branch['properties']['evidence_refs']['maxItems'] == 1
            return LeadRuntimeResponse(content, 'ornith_lead', 'stub', 1024)
    reply = SchemaConstrainedOrnithRuntime(Runtime()).complete(system, user, 'json')
    assert reply.content == content
    with pytest.raises(JudgeProtocolError, match='sole evidence ref'):
        parse_judge_response(reply.content, b)

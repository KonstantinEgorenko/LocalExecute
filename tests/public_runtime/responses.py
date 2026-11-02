"""Deterministic physical backend responses for the public invoice fixture.

Test answers are public and are never part of production packages or prompts.
"""
import json
from harness.verification.acceptance import AcceptanceObligationsV1, independent_obligation_refs
proof={'simulated_roles':[]}
discovery_index=0
class UnknownRole(Exception): pass

def action(name,args):
    from types import SimpleNamespace
    return SimpleNamespace(content=json.dumps(dict(schema_version=1,action=name,arguments=args,summary=None)))

def role_of(payload):
    users='\n'.join(m.get('content','') for m in payload['messages'] if m['role']=='user')
    if 'JUDGE_REQUEST\n' in users:return 'JUDGE'
    if 'CRITIC_REQUEST\n' in users:return 'CRITIC'
    properties=payload.get('format',{}).get('properties',{}) if isinstance(payload.get('format'),dict) else {}
    if set(properties)=={'groups'}:return 'PUBLIC_AUTHORITY'
    if set(properties)=={'group_id','witness'}:return 'PUBLIC_WITNESS'
    if 'Input=' in users:return 'PLAN'
    for stage in ('SOURCE','TEST'):
        if 'RESPONSIBILITY\n'+stage+'\n' in users:return stage
    raise UnknownRole()

def response(payload):
    global discovery_index
    try:
        role = role_of(payload)
    except UnknownRole:
        from tests.public_runtime.responses import action
        sequence = [('VIEW', {'path': ''}), ('VIEW', {'path': 'billing.py'}), ('VIEW', {'path': 'tests'}), ('VIEW', {'path': 'tests/test_billing.py'}), ('RESOLVE_EDIT_INTENT', {'source_targets': ['billing.py'], 'test_targets': ['tests/test_billing.py'], 'relevant_facts': ['Observed source and public tests.']})]
        name, args = sequence[discovery_index]
        discovery_index += 1
        proof['simulated_roles'].append('SIMPLE_DISCOVERY')
        return dict(model=payload['model'], done=True, done_reason='stop', message={'role': 'assistant', 'content': action(name, args).content})
    proof['simulated_roles'].append(role)
    user = next((x['content'] for x in payload['messages'] if x['role'] == 'user'))
    tool_calls = []
    if role == 'PLAN':
        objective = json.loads(user.split('Input=', 1)[1])['objective']
        content = dict(schema_version=1, scenario='BUGFIX_WITH_REGRESSION', objective=objective, scope='SINGLE_FILE', target_hints=[], requires_regression_change=True, requires_research=False, risk='LOW', ambiguity='MEDIUM')
    elif role == 'PUBLIC_AUTHORITY':
        data = json.loads(user)
        content = {'groups': {g['group_id']: {'group_id': g['group_id'], 'status': 'SUPPORTED', 'public_refs': g['public_refs']} for g in data['groups']}}
    elif role == 'PUBLIC_WITNESS':
        group = json.loads(user)['group']
        content = {'group_id': group['group_id'], 'witness': dict(module_path=group['binding']['module_path'], callable_name=group['binding']['callable_name'], arguments={'args': [], 'kwargs': {'items': [[100, 2], [50, 4]], 'discount': 0.1, 'tax': 0.08}}, expected_expression='round_half_up(sum_products(items)*(1-discount)*(1+tax),2)')}
    elif role in ('SOURCE', 'TEST'):
        if role == 'SOURCE':
            args = dict(file='billing.py', start_line=19, end_line=19, replacement_lines=['    return money(base * (1 - Decimal(str(discount))) * (1 + Decimal(str(tax))))'])
        else:
            args = dict(file='tests/test_billing.py', start_line=1, end_line=4, replacement_lines=['from billing import invoice_total, format_invoice', 'def test_existing():', '    assert str(invoice_total([(10,2)])) == "20.00"', '    assert format_invoice([(2.5,1)]) == "$2.50"', '', 'def test_discount_and_tax():', '    assert str(invoice_total([(100, 2), (50, 4)], discount=0.1, tax=0.08)) == "388.80"'])
        content = ''
        tool_calls = [{'function': {'name': 'apply_edit', 'arguments': args}}]
    elif role == 'CRITIC':
        data = json.loads(user.split('\n', 1)[1])
        content = dict(deterministic_outcome='PASSED', verdict='ACCEPT', failure_class='NONE', confidence=0.9, summary='Synthetic offline review of freshly verified candidate.', evidence=[entry['evidence_id'] for entry in data['evidence_catalog'][:2]], recommended_action='NONE', retry_guidance=[], can_override_deterministic=False)
    else:
        data = json.loads(user.split('\n', 1)[1])
        frozen = AcceptanceObligationsV1.from_dict(data['frozen_acceptance_obligations'])
        content = dict(deterministic_outcome='PASSED', decision='ACCEPT', confidence=0.9, summary='Synthetic offline verdict; fresh verification and public evidence.', evidence=['verification_report', 'critic_report', 'candidate_diff'], next_action='NONE', handoff_guidance=[], can_override_deterministic=False, acceptance_evidence=[dict(obligation_id=o.obligation_id, status='SATISFIED', candidate_evidence='candidate_diff', evidence_refs=['verification_report'] if o.kind == 'VERIFICATION' else ['candidate_diff'], expectation_basis=sorted(independent_obligation_refs(frozen, o)), summary='Synthetic offline fixture: public contract supported by fresh candidate.') for o in frozen.obligations])
    return dict(model=payload['model'], done=True, done_reason='stop', message={'role': 'assistant', 'content': content if isinstance(content, str) else json.dumps(content), 'tool_calls': tool_calls}, prompt_eval_count=50, eval_count=20, prompt_eval_duration=100000000, eval_duration=100000000)

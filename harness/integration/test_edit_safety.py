'Bounded TEST interaction contract; verification remains acceptance authority.'
import hashlib
import json
from harness.verification.acceptance import canonical, fingerprint
from harness.verification.test_contract import inspect_tests


class TestEditSafetyError(ValueError):
    __test__ = False
    def __init__(self, category, evidence):
        self.category, self.evidence = category, evidence
        super().__init__(category)


def protect_assertions(baseline, candidate, policy):
    'Reuse exactly the independent verification structural predicate, before CAS.'
    try:
        details = inspect_tests(baseline, candidate, policy)
    except (SyntaxError, TypeError) as exc:
        raise TestEditSafetyError('TEST_ASSERTION_PRESERVATION_REJECTED',
            {'malformed_test_candidate':type(exc).__name__}) from exc
    if any(details.values()):
        raise TestEditSafetyError('TEST_ASSERTION_PRESERVATION_REJECTED', details)


def compact_context(*, contract, frozen, baseline, current, report, rejection,
                    original_intent, source_receipt, binding, remaining):
    """No truncation: full inputs stay durable, only normative evidence is inlined."""
    if (not isinstance(binding,dict) or binding.get('run_id')!=frozen['run_id']
        or binding.get('todo_id')!=frozen['todo_id']
        or any(source_receipt.get(k)!=binding.get(k) for k in ('run_id','todo_id','lease_id'))
        or fingerprint(contract)!=frozen['contract_fingerprint']):
        raise TestEditSafetyError('TEST_REPAIR_CONTEXT_INVALID',{'reason':'IDENTITY_OR_CONTRACT_MISMATCH'})
    versions=binding.get('versions')
    if (not isinstance(versions,list) or any(not isinstance(v,(list,tuple)) or len(v)!=3
        or any(type(x) is not str for x in v) for v in versions)
        or {v[0] for v in versions}!=set(current)
        or any(type(v[2]) is not str or v[2]!=hashlib.sha256(current[v[0]].encode()).hexdigest() for v in versions)):
        raise TestEditSafetyError('TEST_REPAIR_CONTEXT_INVALID',{'reason':'CURRENT_SOURCE_CAS_BINDING_MISMATCH'})
    normative = [e for e in frozen['evidence'] if e['provenance'] in
                 ('AUTHORITATIVE_REQUIREMENT', 'PUBLIC_CONTRACT_EVIDENCE', 'PREEXISTING_TEST_EVIDENCE')]
    # Exact duplicate evidence content is represented once, retaining all IDs.
    texts = {}; aliases=[]
    for e in normative:
        if e['content']==canonical(contract) or e['content']==contract['objective']:
            aliases.append({'evidence_id':e['evidence_id'],'reference':
                'full_contract_sha256' if e['content']==canonical(contract) else 'authoritative_contract.objective',
                            'content_sha256':hashlib.sha256(e['content'].encode()).hexdigest()})
            continue
        texts.setdefault(e['content'], []).append(e['evidence_id'])
    failed = [{**{k:g[k] for k in ('gate_id','outcome','detail')},
               'causal_output_lines':[{'line':i+1,'text':line} for i,line in enumerate(g.get('stdout','').splitlines())
                   if line.startswith(('FAILED ', 'E ', '> '))],
               'full_stdout_sha256':hashlib.sha256(g.get('stdout','').encode()).hexdigest(),
               'full_stderr_sha256':hashlib.sha256(g.get('stderr','').encode()).hexdigest()}
              for g in (report or {}).get('results', ()) if g['outcome']!='PASSED']
    projected_contract={**contract,'constraints':dict(contract['constraints'])}
    receipts={}
    # These two values are derived qualification receipts, not requirements.
    # The full contract remains durable and bound by contract_fingerprint.
    for key in ('PLANNING_SCOPE_EVIDENCE_V1','QUALIFIED_EXECUTION_SHAPE_V1'):
        if key in projected_contract['constraints']:
            receipts[key]={'sha256':fingerprint(projected_contract['constraints'].pop(key))}
    payload = dict(schema_version=1, kind='BOUNDED_TEST_REPAIR_V1', binding=binding,
        authoritative_contract=projected_contract, qualification_receipt_refs=receipts,
        full_contract_sha256=fingerprint(contract), obligations=frozen['obligations'],
        normative_evidence=[{'evidence_ids':ids,'content':text} for text,ids in texts.items()],
        exact_content_aliases=aliases,
        unresolved_semantics=frozen['unresolved_semantics'], baseline_tests=baseline,
        current_files=current, failed_gates=failed, pre_cas_rejection=rejection,
        original_intent={k:v for k,v in (original_intent or {}).items() if k!='tool_calls_raw'},
        original_intent_sha256=fingerprint(original_intent), remaining_test_repair_generations=remaining,
        instruction='Add regression coverage. Preserve every existing baseline assertion and its function/control-flow binding. Never replace or disable it.',
        references={'acceptance_sha256':fingerprint(frozen),
                    'source_verification_report_id':source_receipt['report_id'],
                    'source_verification_sha256':fingerprint(source_receipt),
                    'verification_report_id':(report or {}).get('report_id'),
                    'verification_report_sha256':fingerprint(report) if report else None})
    return payload


def qualify_payload(payload, counter):
    """Counter must qualify the actual serialized chat/template/tools, not text alone.

    Unsupported tokenizers fail closed. No bytes/4 heuristic is accepted as proof.
    Physical dispatch callers retain the complete rejected request locally.
    """
    options=payload.get('options', {})
    ctx=options.get('num_ctx'); reserve=options.get('num_predict')
    raw=json.dumps(payload,allow_nan=False).encode('utf8')
    evidence={'request_sha256':hashlib.sha256(raw).hexdigest(), 'serialized_bytes':len(raw),
              'num_ctx':ctx, 'output_reservation':reserve, 'exact_token_count':None}
    if not callable(counter):
        evidence['reason']='TOKENIZER_QUALIFICATION_UNAVAILABLE'
        raise TestEditSafetyError('TEST_REPAIR_CONTEXT_BUDGET_EXCEEDED',evidence)
    try:proof=counter(payload)
    except Exception as exc:
        evidence.update(reason='TOKENIZER_QUALIFICATION_FAILED',qualification_error=type(exc).__name__)
        raise TestEditSafetyError('TEST_REPAIR_CONTEXT_BUDGET_EXCEEDED',evidence) from exc
    if (not isinstance(proof,dict) or proof.get('request_sha256')!=evidence['request_sha256']
        or proof.get('model')!=payload.get('model') or proof.get('scope')!='SERIALIZED_CHAT_WITH_TOOLS'
        or proof.get('method') not in ('EXACT_TOKENIZER','VERIFIED_CONSERVATIVE_BOUND')
        or type(proof.get('prompt_tokens')) is not int or proof['prompt_tokens']<0
        or type(ctx) is not int or type(reserve) is not int or min(ctx,reserve)<=0):
        evidence['reason']='INVALID_TOKENIZER_QUALIFICATION'
        raise TestEditSafetyError('TEST_REPAIR_CONTEXT_BUDGET_EXCEEDED',evidence)
    evidence.update(proof)
    if proof['method']=='EXACT_TOKENIZER':evidence['exact_token_count']=proof['prompt_tokens']
    if proof['prompt_tokens']+reserve>ctx:
        evidence['reason']='PROMPT_PLUS_OUTPUT_EXCEEDS_CONTEXT'
        raise TestEditSafetyError('TEST_REPAIR_CONTEXT_BUDGET_EXCEEDED',evidence)
    return evidence

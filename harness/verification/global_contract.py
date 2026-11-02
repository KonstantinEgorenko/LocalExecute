"""Caller-owned, bounded requirements for fresh integrated verification."""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from .acceptance import canonical, fingerprint
from .callable_authority import PublicCallableAuthorityV1, validate_arguments
from .public_reference import evaluate_public_reference_value, PublicReferenceExceptionV1

GLOBAL_VERIFICATION = 'GLOBAL_INTEGRATED_VERIFICATION_V1'
GLOBAL_GATES = ('global_structure', 'global_semantics', 'global_pytest', 'global_baseline')


class GlobalVerificationError(RuntimeError):
    def __init__(self, code, detail):
        self.code = code
        super().__init__(f'{code}: {detail}')


def require(value, code, detail):
    if not value:
        raise GlobalVerificationError(code, detail)


def sha(text):
    return hashlib.sha256(text.encode('utf8')).hexdigest()


def policy_document(policy):
    return {**{k:getattr(policy,k) for k in ('trusted_worktrees_only','require_candidate_dirty',
        'fail_fast','max_gates','max_gate_timeout_seconds','max_total_timeout_seconds',
        'max_output_bytes','max_source_bytes','trusted_python_executable')},
        'command_catalog':{k:v.to_dict() for k,v in policy.command_catalog.items()},
        'global_verifier_source':{name:hashlib.sha256((Path(__file__).resolve().parents[1]/name).read_bytes()).hexdigest()
            for name in ('verification/global_contract.py','verification/global_checks.py',
                'verification/runner.py','integration/global_verification.py')}}


@dataclass(frozen=True, slots=True)
class GlobalVerificationContractV1:
    document: str

    def to_dict(self):
        return json.loads(self.document)

    @classmethod
    def admit(cls, spec, policy, *, graph):
        if GLOBAL_VERIFICATION not in spec.global_constraints:
            return None
        try:
            raw=spec.to_dict()['global_constraints'][GLOBAL_VERIFICATION]
            keys={'schema_version','mission_id','goal_id','required_graph_fingerprint',
                'public_authority_fingerprint','acceptance_contract','required_todos',
                'required_source_paths','required_test_paths','baseline_tests','baseline_manifest_fingerprint',
                'semantic_cases','verification_policy_fingerprint','max_gate_seconds','max_total_seconds',
                'max_attempts','terminal_acceptance','fingerprint'}
            if type(raw) is not dict or set(raw)!=keys:raise ValueError('fields')
            if type(raw['schema_version']) is not int or raw['schema_version']!=1:raise ValueError('schema version')
            if (raw['mission_id'],raw['goal_id'])!=(spec.mission_id,spec.goal_id):raise ValueError('mission identity')
            if raw['fingerprint']!=fingerprint({k:v for k,v in raw.items() if k!='fingerprint'}):raise ValueError('fingerprint')
            if len(canonical(raw).encode('utf8'))>256*1024:raise ValueError('contract size')
            if graph is None:raise ValueError('admitted required graph missing')
            gd=graph.to_dict();authority=PublicCallableAuthorityV1.admit(spec,graph)
            if authority is None:raise ValueError('admitted public authority missing')
            ad=authority.to_dict()
            if raw['required_graph_fingerprint']!=gd['fingerprint'] or raw['public_authority_fingerprint']!=ad['fingerprint']:
                raise ValueError('graph/public authority identity')
            contract=raw['acceptance_contract']
            if set(contract)!={'source','text','sha256'} or contract['source']!=gd['source'] or sha(contract['text'])!=contract['sha256'] or contract['sha256']!=gd['source_fingerprint']:
                raise ValueError('original acceptance contract')
            expected=[n.logical_id for n in graph.todos]
            if raw['required_todos']!=expected or graph.exact_todo_count!=len(expected):raise ValueError('exact Todo set/order')
            for field,scopes in (('required_source_paths',[p for n in graph.todos for p in n.source_scope]),
                                 ('required_test_paths',[p for n in graph.todos for p in n.test_scope])):
                # The integration boundary supplies the authoritatively admitted
                # graph, whose scopes already passed canonical path validation.
                if tuple(raw[field])!=tuple(scopes) or len(set(scopes))!=len(scopes) or any(not p.endswith('.py') for p in scopes):raise ValueError('explicit Python file scopes required')
            tests=raw['baseline_tests']
            if set(tests)!=set(raw['required_test_paths']):raise ValueError('complete trusted baseline test manifest')
            for path,row in tests.items():
                if set(row)!={'text','sha256'} or sha(row['text'])!=row['sha256']:raise ValueError('baseline test hash')
            if fingerprint(tests)!=raw['baseline_manifest_fingerprint']:raise ValueError('baseline manifest fingerprint')
            if raw['verification_policy_fingerprint']!=fingerprint(policy_document(policy)):raise ValueError('trusted verification policy changed')
            for key,maximum in (('max_gate_seconds',policy.max_gate_timeout_seconds),('max_total_seconds',policy.max_total_timeout_seconds)):
                if type(raw[key]) is not int or not 1<=raw[key]<=maximum:raise ValueError('timeout bound')
            if raw['max_gate_seconds']>raw['max_total_seconds']:raise ValueError('timeout order')
            if type(raw['max_attempts']) is not int or raw['max_attempts']!=1:raise ValueError('only one explicitly budgeted attempt supported')
            if raw['terminal_acceptance']!='INDEPENDENT_ACCEPTANCE_REQUIRED':raise ValueError('independent terminal acceptance must remain mandatory')
            callables={(c['logical_todo_id'],c['qualified_name']):c for c in ad['callables']}
            cases=raw['semantic_cases']
            if type(cases) is not list or not 1<=len(cases)<=128:raise ValueError('bounded trusted semantic cases')
            seen=set();exceptions=set()
            for case in cases:
                if set(case)!={'logical_todo_id','qualified_name','arguments'}:raise ValueError('semantic case fields')
                key=(case['logical_todo_id'],case['qualified_name']);declaration=callables[key]
                if set(case['arguments'])!=set(declaration['arguments']):raise ValueError('semantic argument binding')
                value=evaluate_public_reference_value(declaration['reference_expression'],case['arguments'])
                validate_arguments(declaration,{'args':[],'kwargs':case['arguments']},declaration['reference_expression'],
                    value.exception_class if isinstance(value,PublicReferenceExceptionV1) else '')
                if isinstance(value,PublicReferenceExceptionV1):
                    if value.exception_class!=declaration['expected_exception']:raise ValueError('exception authority')
                    exceptions.add(key)
                seen.add(key)
            if seen!=set(callables):raise ValueError('every declared callable requires semantic cases')
            if any(c['expected_exception'] and k not in exceptions for k,c in callables.items()):raise ValueError('declared exception coverage missing')
            return cls(canonical(raw))
        except (KeyError,ValueError,TypeError) as exc:
            raise GlobalVerificationError('GLOBAL_CONTRACT_INVALID',str(exc)) from exc

"""Opt-in declarations admitted at trusted ingress, never inferred from candidates.

The declaration is public input, not a signature granting filesystem access.
Original interface observations and candidate observations remain separate.
"""
from __future__ import annotations

import ast
import hashlib
import json
from dataclasses import dataclass
from collections.abc import Mapping

AUTHORITY = 'PUBLIC_CALLABLE_AUTHORITY_V1'
INGRESS = 'PUBLIC_CALLABLE_AUTHORITY_INGRESS_V1'


class PublicCallableAuthorityError(ValueError):
    """Typed deterministic admission failure, with stable first-cause code."""
    def __init__(self, code, detail):
        self.code = code
        self.detail = detail
        super().__init__(code + ': ' + detail)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def failure(code, detail):
    raise PublicCallableAuthorityError(code, detail)


def canonical_path(path):
    if (type(path) is not str or not path or '\\' in path or ':' in path
            or any(p in ('', '.', '..', '.git') for p in path.split('/'))
            or any(c in path for c in '*?[]')):
        failure('PUBLIC_AUTHORITY_PATH_INVALID', str(path))
    return path


@dataclass(frozen=True, slots=True)
class CallableInterfaceObservationV1:
    module_path: str
    qualified_name: str
    observation_kind: str
    source_sha256: str
    present: bool
    signature_fingerprint: str
    constructor: str


@dataclass(frozen=True, slots=True)
class VerifiedAcceptedDependencyV1:
    """Integration-issued proof after authoritative stores and Git validation.

    This value is never parsed from model responses or WorkContract constraints.
    Its source hashes bind lineage, not expected public semantics.
    """
    document: str

    def __post_init__(self):
        value=json.loads(self.document)
        fields={'schema_version','consumer_run_id','consumer_todo_id','consumer_lease_id',
            'required_graph_fingerprint','producer_logical_todo_id','producer_physical_todo_id',
            'producer_run_id','producer_attempt_id','producer_lease_id','accepted_commit_sha',
            'accepted_tree_sha','dependency_version','receipt_artifact_id','receipt_sha256',
            'receipt_fingerprint','source_sha256'}
        if (type(value) is not dict or set(value)!=fields or type(value['schema_version']) is not int
                or value['schema_version']!=1 or type(value['source_sha256']) is not dict
                or any(type(value[k]) is not str or not value[k] for k in fields-{'schema_version','source_sha256'})
                or canonical(value)!=self.document):
            failure('PUBLIC_AUTHORITY_ACCEPTED_DEPENDENCY_INVALID','typed binding')

    def to_dict(self):
        return json.loads(self.document)


def public_read_projection(session, paths):
    """Harness-only bounded read view; worker mutation scope is never widened."""
    from dataclasses import replace
    from harness.workspace import Workspace, WorkspaceAccess
    allowed=tuple(dict.fromkeys((*session.policy.allowed_paths,*(canonical_path(p) for p in paths))))
    return Workspace(replace(session,policy=replace(session.policy,
        access=WorkspaceAccess.READ_ONLY,allowed_paths=allowed,command_prefixes=())))


def observe_interface(source, module_path, qualified_name, kind, *, candidate=False):
    """Static bounded call shape; never executes a class or its constructor."""
    from .acceptance import public_signature
    canonical_path(module_path)
    if type(qualified_name) is not str:
        failure('PUBLIC_AUTHORITY_SYMBOL_INVALID', 'qualified name must be string')
    parts = qualified_name.split('.')
    if (kind not in ('FUNCTION', 'INSTANCE_METHOD') or len(parts) != (1 if kind == 'FUNCTION' else 2)
            or any(not n.isidentifier() or n.startswith('_') for n in parts)):
        failure('PUBLIC_AUTHORITY_SYMBOL_INVALID', qualified_name)
    if type(source) is not str or len(source.encode()) > 32768:
        failure('PUBLIC_AUTHORITY_SOURCE_INVALID', module_path)
    tree = ast.parse(source)
    if kind=='FUNCTION' and any(isinstance(n,ast.ClassDef) and n.name==parts[0] for n in tree.body):
        failure('PUBLIC_AUTHORITY_BINDING_UNSUPPORTED', 'declared function collides with original class')
    for node in tree.body:
        targets = node.targets if isinstance(node,ast.Assign) else [node.target] if isinstance(node,ast.AnnAssign) else []
        if any(isinstance(target,ast.Name) and target.id==parts[0] for target in targets):
            failure('PUBLIC_AUTHORITY_DYNAMIC_BINDING_UNSUPPORTED', qualified_name)
    constructor = 'NONE'
    candidates = tree.body
    if kind == 'INSTANCE_METHOD':
        classes = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == parts[0]]
        if len(classes) != 1:
            failure('PUBLIC_AUTHORITY_CLASS_UNRESOLVED', qualified_name)
        cls = classes[0]
        if any(not isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))
                and not (isinstance(n,ast.Expr) and isinstance(n.value,ast.Constant) and type(n.value.value) is str)
                for n in cls.body):
            failure('PUBLIC_AUTHORITY_DYNAMIC_CLASS_BINDING_UNSUPPORTED', qualified_name)
        # Only a statically proven object() constructor is supported initially.
        # No inherited, metaclass, decorated or user-defined construction.
        if (cls.bases or cls.keywords or cls.decorator_list
                or any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                       and n.name in ('__init__', '__new__') for n in cls.body)):
            failure('PUBLIC_AUTHORITY_CONSTRUCTOR_UNSUPPORTED', qualified_name)
        constructor = 'IMPLICIT_OBJECT_ZERO_ARGUMENTS'
        candidates = cls.body
    matches = [n for n in candidates if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == parts[-1]]
    signature = ''
    if matches:
        if len(matches) != 1 or not isinstance(matches[0], ast.FunctionDef) or matches[0].decorator_list:
            failure('PUBLIC_AUTHORITY_BINDING_UNSUPPORTED', qualified_name)
        node = matches[0]
        shape = public_signature(node)
        if kind == 'INSTANCE_METHOD':
            if shape['posonly'] or not shape['positional'] or shape['positional'][0] != 'self':
                failure('PUBLIC_AUTHORITY_RECEIVER_INVALID', qualified_name)
            shape = {**shape, 'positional': shape['positional'][1:]}
        if (shape['posonly'] or shape['vararg'] or shape['kwarg'] or shape['defaults']
                or shape['kw_defaults']):
            failure('PUBLIC_AUTHORITY_SIGNATURE_UNSUPPORTED', qualified_name)
        signature = digest(shape)
    return CallableInterfaceObservationV1(module_path, qualified_name,
        'CANDIDATE_INTERFACE_OBSERVATION' if candidate else 'ORIGINAL_INTERFACE_OBSERVATION',
        hashlib.sha256(source.encode()).hexdigest(), bool(matches), signature, constructor)


@dataclass(frozen=True, slots=True)
class PublicCallableAuthorityV1:
    """Immutable canonical authority packet, with typed lifecycle identity."""
    document: str

    def __post_init__(self):
        raw = json.loads(self.document)
        if canonical(raw) != self.document or len(self.document.encode()) > 65536:
            failure('PUBLIC_AUTHORITY_INVALID', 'noncanonical or oversized document')
        keys = {'schema_version', 'mission_id', 'goal_id', 'graph_fingerprint', 'original_sources',
                'public_requirements', 'callables', 'fingerprint'}
        if set(raw) != keys or type(raw['schema_version']) is not int or raw['schema_version'] != 1:
            failure('PUBLIC_AUTHORITY_INVALID', 'document fields')
        if any(type(raw[k]) is not str or not raw[k] for k in ('mission_id', 'goal_id', 'graph_fingerprint')):
            failure('PUBLIC_AUTHORITY_IDENTITY_INVALID', 'mission/goal/graph')
        if digest({k: v for k, v in raw.items() if k != 'fingerprint'}) != raw['fingerprint']:
            failure('PUBLIC_AUTHORITY_FINGERPRINT_MISMATCH', 'declaration')
        if type(raw['original_sources']) is not dict or not 1 <= len(raw['original_sources']) <= 8:
            failure('PUBLIC_AUTHORITY_SOURCE_INVALID', 'source count')
        for path, row in raw['original_sources'].items():
            canonical_path(path)
            if (type(row) is not dict or set(row) != {'text', 'sha256'}
                    or type(row['text']) is not str or len(row['text'].encode()) > 32768
                    or hashlib.sha256(row['text'].encode()).hexdigest() != row['sha256']):
                failure('PUBLIC_AUTHORITY_SOURCE_FINGERPRINT_MISMATCH', path)
        reqs = raw['public_requirements']
        if type(reqs) is not dict or not 1 <= len(reqs) <= 8:
            failure('PUBLIC_AUTHORITY_PROVENANCE_INVALID', 'requirements')
        for key, row in reqs.items():
            if (type(row) is not dict or set(row) != {'provenance', 'path', 'text', 'sha256'}
                    or row['provenance'] not in ('PUBLIC_CONTRACT_EVIDENCE', 'AUTHORITATIVE_REQUIREMENT')
                    or type(row['text']) is not str or not row['text']
                    or hashlib.sha256(row['text'].encode()).hexdigest() != row['sha256']):
                failure('PUBLIC_AUTHORITY_PROVENANCE_INVALID', key)
            canonical_path(row['path'])
        rows = raw['callables']
        if type(rows) is not list or not 1 <= len(rows) <= 3:
            failure('PUBLIC_AUTHORITY_INVALID', 'callable count')
        identities, obligations = set(), set()
        for row in rows:
            required = {'logical_todo_id', 'module_path', 'qualified_name', 'kind', 'signature',
                        'constructor', 'arguments', 'result', 'reference_expression', 'expected_exception',
                        'public_refs', 'obligation_id', 'mandatory_stage', 'original_presence', 'delegation'}
            if type(row) is not dict or set(row) != required:
                failure('PUBLIC_AUTHORITY_INVALID', 'callable fields')
            identity = (row['module_path'], row['qualified_name'])
            if identity in identities or row['obligation_id'] in obligations:
                failure('PUBLIC_AUTHORITY_AMBIGUOUS', 'duplicate callable/obligation')
            identities.add(identity); obligations.add(row['obligation_id'])
            if (row['original_presence'] not in ('DECLARED_ABSENT', 'OBSERVED_ORIGINAL')
                    or row['mandatory_stage'] != 'AFTER_OWN_TODO_ACCEPTANCE'
                    or not row['logical_todo_id'] or not row['obligation_id']):
                failure('PUBLIC_AUTHORITY_STAGE_INVALID', row['qualified_name'])
            source = raw['original_sources'].get(row['module_path'])
            if source is None:
                failure('PUBLIC_AUTHORITY_SOURCE_MISSING', row['module_path'])
            observed = observe_interface(source['text'], *identity, row['kind'])
            if observed.present != (row['original_presence'] == 'OBSERVED_ORIGINAL'):
                failure('PUBLIC_AUTHORITY_ORIGINAL_PRESENCE_MISMATCH', row['qualified_name'])
            if observed.present and observed.signature_fingerprint != digest(row['signature']):
                failure('PUBLIC_AUTHORITY_SIGNATURE_MISMATCH', row['qualified_name'])
            if row['constructor'] != observed.constructor:
                failure('PUBLIC_AUTHORITY_CONSTRUCTOR_MISMATCH', row['qualified_name'])
            shape = row['signature']
            if (type(shape) is not dict or set(shape) != {'posonly', 'positional', 'kwonly', 'vararg', 'kwarg', 'defaults', 'kw_defaults'}
                    or type(shape['positional']) is not list or type(shape['kwonly']) is not list
                    or any(shape[k] for k in ('posonly', 'vararg', 'kwarg', 'defaults', 'kw_defaults'))
                    or not 1 <= len(shape['positional'] + shape['kwonly']) <= 8):
                failure('PUBLIC_AUTHORITY_SIGNATURE_UNSUPPORTED', row['qualified_name'])
            parameters = shape['positional'] + shape['kwonly']
            if (any(type(n) is not str or not n.isidentifier() or n in ('self','cls') for n in parameters)
                    or len(set(parameters)) != len(parameters)
                    or type(row['arguments']) is not dict or set(row['arguments']) != set(parameters)):
                failure('PUBLIC_AUTHORITY_ARGUMENT_SCHEMA_INVALID', row['qualified_name'])
            for name, domain in row['arguments'].items():
                if (type(domain) is not dict or set(domain) != {'kind', 'minimum', 'maximum'}
                        or domain['kind'] not in ('INTEGER', 'ORDERED_INTEGER_PAIR')
                        or type(domain['minimum']) is not int or type(domain['maximum']) is not int
                        or not -1000000 <= domain['minimum'] <= domain['maximum'] <= 1000000):
                    failure('PUBLIC_AUTHORITY_ARGUMENT_SCHEMA_INVALID', name)
            if (row['result'] != {'type': 'integer'} or row['expected_exception'] not in ('', 'ValueError')
                    or type(row['public_refs']) is not list or not row['public_refs']
                    or any(type(ref) is not str for ref in row['public_refs'])
                    or not set(row['public_refs']) <= set(reqs)):
                failure('PUBLIC_AUTHORITY_SEMANTICS_UNSUPPORTED', row['qualified_name'])
            # Expressibility and parameter binding are checked without candidate execution.
            from .public_reference import evaluate_public_reference_value
            examples = {p: (domain['minimum'] if domain['kind'] == 'INTEGER' else [domain['minimum'], domain['maximum']])
                        for p, domain in row['arguments'].items()}
            result = evaluate_public_reference_value(row['reference_expression'], examples, required_names=parameters)
            if type(result) is not int:
                failure('PUBLIC_AUTHORITY_RESULT_TYPE_INVALID', row['qualified_name'])
            delegation = row['delegation']
            if delegation is not None and (type(delegation) is not dict or set(delegation) != {'logical_todo_id', 'module_path', 'qualified_name'}):
                failure('PUBLIC_AUTHORITY_DELEGATION_INVALID', row['qualified_name'])
        for row in rows:
            dep = row['delegation']
            if dep is not None and not any((other['module_path'], other['qualified_name'], other['logical_todo_id']) ==
                (dep['module_path'], dep['qualified_name'], dep['logical_todo_id']) for other in rows):
                failure('PUBLIC_AUTHORITY_DELEGATION_UNBOUND', row['qualified_name'])

    def to_dict(self):
        return json.loads(self.document)

    @classmethod
    def admit(cls, spec, graph):
        """Bind declarations to a graph already admitted by the integration layer."""
        raw = spec.global_constraints.get(AUTHORITY)
        if raw is None:
            return None
        if graph is None:
            failure('PUBLIC_AUTHORITY_GRAPH_REQUIRED', 'trusted lifecycle graph')
        # WorkContract constraints freeze dictionaries recursively; normalize at ingress.
        def thaw(value):
            if isinstance(value, Mapping):
                return {k: thaw(v) for k, v in value.items()}
            if isinstance(value, (tuple, list)):
                return [thaw(v) for v in value]
            return value
        authority = cls(canonical(thaw(raw)))
        doc = authority.to_dict()
        if (doc['mission_id'], doc['goal_id'], doc['graph_fingerprint']) != (
                spec.mission_id, spec.goal_id, json.loads(graph.document)['fingerprint']):
            failure('PUBLIC_AUTHORITY_IDENTITY_MISMATCH', 'trusted mission/goal/graph')
        nodes = {n.logical_id: n for n in graph.todos}
        for row in doc['callables']:
            node = nodes.get(row['logical_todo_id'])
            if node is None or row['module_path'] not in node.source_scope:
                failure('PUBLIC_AUTHORITY_TODO_MISMATCH', row['qualified_name'])
            if row['delegation'] is not None and row['delegation']['logical_todo_id'] not in node.dependencies:
                failure('PUBLIC_AUTHORITY_DEPENDENCY_MISMATCH', row['qualified_name'])
        return authority

    def observe_candidate(self, sources, logical_todo_id):
        """Interface evidence only; not accepted revision, normative authority or PASS."""
        result = []
        for row in self.to_dict()['callables']:
            if row['logical_todo_id'] != logical_todo_id:
                continue
            if row['module_path'] not in sources:
                failure('PUBLIC_AUTHORITY_CANDIDATE_MISSING', row['qualified_name'])
            observed = observe_interface(sources[row['module_path']], row['module_path'], row['qualified_name'], row['kind'], candidate=True)
            if not observed.present or observed.signature_fingerprint != digest(row['signature']):
                failure('PUBLIC_AUTHORITY_CANDIDATE_SIGNATURE_MISMATCH', row['qualified_name'])
            if observed.constructor != row['constructor']:
                failure('PUBLIC_AUTHORITY_CONSTRUCTOR_MISMATCH', row['qualified_name'])
            result.append(observed)
        if not result:
            failure('PUBLIC_AUTHORITY_TODO_MISMATCH', logical_todo_id)
        return tuple(result)


def from_frozen(frozen):
    value = next((e.content for e in frozen.evidence if e.evidence_id == 'trusted_callable_authority'), None)
    return PublicCallableAuthorityV1(value) if value is not None else None


def declaration_for(frozen, module_path, symbol):
    authority = from_frozen(frozen)
    if authority is None:
        return None
    return next((row for row in authority.to_dict()['callables']
                 if (row['module_path'], row['qualified_name']) == (module_path, symbol)), None)


def validate_arguments(row, arguments, expression, expected_exception):
    if (type(arguments) is not dict or set(arguments) != {'args', 'kwargs'}
            or arguments['args'] != [] or type(arguments['args']) is not list
            or type(arguments['kwargs']) is not dict or set(arguments['kwargs']) != set(row['arguments'])):
        failure('PUBLIC_AUTHORITY_ARGUMENTS_INVALID', 'exact named arguments; no receiver')
    for name, domain in row['arguments'].items():
        value = arguments['kwargs'][name]
        items = [value] if domain['kind'] == 'INTEGER' else value
        if (type(items) is not list or len(items) != (1 if domain['kind'] == 'INTEGER' else 2)
                or any(type(x) is not int or not domain['minimum'] <= x <= domain['maximum'] for x in items)
                or domain['kind'] == 'ORDERED_INTEGER_PAIR' and items[0] > items[1]):
            failure('PUBLIC_AUTHORITY_ARGUMENT_DOMAIN_INVALID', name)
    if (type(expression) is not str or ast.dump(ast.parse(expression, mode='eval'), include_attributes=False)
            != ast.dump(ast.parse(row['reference_expression'], mode='eval'), include_attributes=False)):
        failure('PUBLIC_AUTHORITY_REFERENCE_INVALID', 'exact parameter-bound declared relationship')
    from .public_reference import evaluate_public_reference_value, PublicReferenceExceptionV1
    value = evaluate_public_reference_value(expression, arguments['kwargs'], required_names=row['arguments'])
    exception = value.exception_class if isinstance(value, PublicReferenceExceptionV1) else ''
    if expected_exception != exception or exception and exception != row['expected_exception']:
        failure('PUBLIC_AUTHORITY_EXCEPTION_MISMATCH', 'frozen public exception contract')
    if not exception and type(value) is not int:
        failure('PUBLIC_AUTHORITY_RESULT_TYPE_INVALID', 'integer result required')
    return value


def active_declarations(frozen):
    from harness.control_plane.todo_identity import make_todo_id
    authority = from_frozen(frozen)
    if authority is None:
        return ()
    doc = authority.to_dict()
    constraints = json.loads(next(e.content for e in frozen.evidence if e.evidence_id == 'original_constraints'))['constraints']
    graph = constraints['REQUIRED_DEPENDENCY_GRAPH_V1']
    if (constraints.get(INGRESS) != doc['fingerprint'] or graph['fingerprint'] != doc['graph_fingerprint']
            or graph['goal_id'] != doc['goal_id'] or graph['mission_id'] != doc['mission_id']):
        failure('PUBLIC_AUTHORITY_INGRESS_MISMATCH', 'trusted declaration binding')
    pending = {n['logical_id']:set(n['dependencies']) for n in graph['todos']}
    order = []
    while pending:
        ready = sorted(k for k,v in pending.items() if not v)
        if not ready:
            failure('PUBLIC_AUTHORITY_GRAPH_INVALID', 'dependency cycle')
        order.extend(ready)
        for key in ready:
            del pending[key]
        for deps in pending.values():
            deps.difference_update(ready)
    logical = next((key for i,key in enumerate(order,1) if make_todo_id(doc['goal_id'],i) == frozen.todo_id), None)
    if logical is None:
        failure('PUBLIC_AUTHORITY_TODO_MISMATCH', frozen.todo_id)
    rows = tuple(row for row in doc['callables'] if row['logical_todo_id'] == logical)
    if not rows:
        failure('PUBLIC_AUTHORITY_TODO_MISMATCH', logical)
    return rows


def build_declared_acceptance(*, run_id, contract, sources, unresolved, public_contracts=None, accepted_dependency=None):
    from .acceptance import (AcceptanceObligationsV1, AcceptanceObligationV1, SemanticEvidenceV1,
                             SemanticProvenanceV1 as P, fingerprint)
    raw = contract.to_dict()['constraints'][AUTHORITY]
    authority = PublicCallableAuthorityV1(canonical(raw))
    if contract.constraints.get(INGRESS) != raw['fingerprint'] or contract.goal_id != raw['goal_id']:
        failure('PUBLIC_AUTHORITY_INGRESS_MISMATCH', 'no caller-admitted declaration')
    evidence = [SemanticEvidenceV1('original_constraints', P.AUTHORITATIVE_REQUIREMENT, canonical(contract.to_dict())),
                SemanticEvidenceV1('trusted_callable_authority', P.AUTHORITATIVE_REQUIREMENT, authority.document)]
    for key, row in raw['public_requirements'].items():
        if public_contracts is not None and row['provenance']=='PUBLIC_CONTRACT_EVIDENCE' and public_contracts.get(row['path']) != row['text']:
            failure('PUBLIC_AUTHORITY_PUBLIC_SOURCE_CHANGED', row['path'])
        evidence.append(SemanticEvidenceV1(key, P(row['provenance']), row['text'], row['path'], row['sha256']))
    for i, (path, row) in enumerate(sorted(raw['original_sources'].items())):
        evidence.append(SemanticEvidenceV1('original_source_'+str(i), P.OBSERVED_IMPLEMENTATION, row['text'], path, row['sha256']))
    provisional = AcceptanceObligationsV1(1,run_id,contract.todo_id,fingerprint(contract.to_dict()),
        (AcceptanceObligationV1('verification','VERIFICATION','All required deterministic gates must pass.',('original_constraints',)),),
        tuple(evidence),tuple(unresolved))
    rows = active_declarations(provisional)
    dependency_observations = {}
    for row in rows:
        if sources.get(row['module_path']) != raw['original_sources'][row['module_path']]['text']:
            failure('PUBLIC_AUTHORITY_ORIGINAL_SOURCE_CHANGED', row['module_path'])
        # Dependency visibility is interface observation, never a substitute for
        # the exact accepted revision/lease handoff (C1.3).
        dep = row['delegation']
        if dep is not None:
            from dataclasses import asdict
            observations = authority.observe_candidate(sources,dep['logical_todo_id'])
            dependency_observations[dep['logical_todo_id']] = dict(logical_todo_id=dep['logical_todo_id'],
                stage='CURRENT_DEPENDENCY_INTERFACE_VISIBLE',accepted_revision='NOT_QUALIFIED_C1_3_BOUNDARY',
                observations=[asdict(o) for o in observations])
            if accepted_dependency is not None:
                if not isinstance(accepted_dependency,VerifiedAcceptedDependencyV1):
                    failure('PUBLIC_AUTHORITY_ACCEPTED_DEPENDENCY_INVALID','integration-issued typed proof required')
                binding=accepted_dependency.to_dict()
                if ((binding['consumer_run_id'],binding['consumer_todo_id'],binding['required_graph_fingerprint'],
                        binding['producer_logical_todo_id']) != (run_id,contract.todo_id,raw['graph_fingerprint'],dep['logical_todo_id'])
                        or binding['source_sha256'].get(dep['module_path']) != hashlib.sha256(sources[dep['module_path']].encode('utf8')).hexdigest()):
                    failure('PUBLIC_AUTHORITY_ACCEPTED_DEPENDENCY_MISMATCH','exact accepted source/lineage')
                dependency_observations[dep['logical_todo_id']]['stage']='VERIFIED_ACCEPTED_DEPENDENCY'
                dependency_observations[dep['logical_todo_id']]['accepted_revision']=binding
    obligations = tuple(AcceptanceObligationV1(row['obligation_id'],'BEHAVIOR',
        'Satisfy the trusted declared public relationship and integration requirement.',
        tuple(row['public_refs'])+('trusted_callable_authority',)) for row in rows)
    evidence = provisional.evidence + tuple(SemanticEvidenceV1('dependency_interface:'+key,P.CANDIDATE_DERIVED,
        canonical(value)) for key,value in sorted(dependency_observations.items()))
    return __import__('dataclasses').replace(provisional,obligations=obligations+provisional.obligations,evidence=evidence)


def declared_surface(frozen):
    return [dict(module_path=r['module_path'],callable_name=r['qualified_name'],
        parameters=r['signature']['positional']+r['signature']['kwonly'],
        positional_parameters=r['signature']['positional'],literal_defaults={},keyword_witness_supported=True,
        docstring=None,callable_kind=r['kind'],constructor=r['constructor'],declaration=r)
        for r in active_declarations(frozen)]


def declared_binding(frozen, obligation):
    row = next((r for r in active_declarations(frozen) if r['obligation_id'] == obligation.obligation_id),None)
    if row is None:
        return None
    return dict(evidence_id='trusted_callable_authority',module_path=row['module_path'],
        callable_name=row['qualified_name'],signature_fingerprint=digest(row['signature']),
        binding_source='TRUSTED_CALLABLE_DECLARATION')

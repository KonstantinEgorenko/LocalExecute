"""Bounded public acceptance provenance. No execution oracle or confidence score."""
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
import ast
import hashlib
import json
from .witness_domain import PublicWitnessInputValidationV1
from .public_reference import public_reference_language_contract


class SemanticProvenanceV1(StrEnum):
    AUTHORITATIVE_REQUIREMENT = 'AUTHORITATIVE_REQUIREMENT'
    PUBLIC_CONTRACT_EVIDENCE = 'PUBLIC_CONTRACT_EVIDENCE'
    PREEXISTING_TEST_EVIDENCE = 'PREEXISTING_TEST_EVIDENCE'
    OBSERVED_IMPLEMENTATION = 'OBSERVED_IMPLEMENTATION'
    MODEL_HYPOTHESIS = 'MODEL_HYPOTHESIS'
    CANDIDATE_DERIVED = 'CANDIDATE_DERIVED'
    GENERATED_TEST_DERIVED = 'GENERATED_TEST_DERIVED'


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


@dataclass(frozen=True, slots=True)
class UnsupportedRequiredObligationV1:
    missing_capabilities: tuple[str, ...]
    root_acceptance_fingerprint: str
    source_evidence: tuple[str, ...]
    code: str = 'UNSUPPORTED_REQUIRED_OBLIGATION'

    def to_dict(self):
        return asdict(self)


def unsupported_required_obligation(frozen):
    """Known verifier capability failures only; implementation faults stay repairable."""
    if frozen is None:
        return None
    missing = tuple(code for code in ('PUBLIC_INTERPRETER_TOPOLOGY_UNSUPPORTED',
        'PUBLIC_WITNESS_BACKEND_GRAMMAR_INCOMPATIBLE', 'PUBLIC_INTERPRETER_CONTEXT_UNSUPPORTED')
        if any(code in item for item in frozen.unresolved_semantics))
    if not missing:
        return None
    return UnsupportedRequiredObligationV1(missing, fingerprint(frozen.to_dict()),
        tuple(e.evidence_id for e in frozen.evidence))


@dataclass(frozen=True, slots=True)
class PublicSymbolOriginV1:
    symbol_kind: str
    qualified_name: str
    signature_json: str
    signature_fingerprint: str
    source_start_line: int
    source_end_line: int

    def __post_init__(self):
        if self.symbol_kind != 'FUNCTION' or not self.qualified_name.isidentifier():
            raise ValueError('unsupported public symbol origin')
        if fingerprint(json.loads(self.signature_json)) != self.signature_fingerprint:
            raise ValueError('public signature fingerprint mismatch')
        if (type(self.source_start_line) is not int or type(self.source_end_line) is not int
                or not 1 <= self.source_start_line <= self.source_end_line):
            raise ValueError('invalid public symbol span')


def public_signature(node):
    """Public call shape, independent of body, docstring, annotations and location."""
    return {'posonly': [a.arg for a in node.args.posonlyargs],
            'positional': [a.arg for a in node.args.args],
            'kwonly': [a.arg for a in node.args.kwonlyargs],
            'vararg': node.args.vararg.arg if node.args.vararg else None,
            'kwarg': node.args.kwarg.arg if node.args.kwarg else None,
            'defaults': [ast.dump(v, include_attributes=False) for v in node.args.defaults],
            'kw_defaults': [ast.dump(v, include_attributes=False) if v else None for v in node.args.kw_defaults]}


@dataclass(frozen=True, slots=True)
class SemanticEvidenceV1:
    evidence_id: str
    provenance: SemanticProvenanceV1
    content: str
    path: str = ''
    content_sha256: str = ''
    origin: PublicSymbolOriginV1 | None = None

    def __post_init__(self):
        if not isinstance(self.provenance, SemanticProvenanceV1):
            raise ValueError('invalid semantic provenance')
        if not self.evidence_id or type(self.content) is not str or len(self.content.encode('utf-8')) > 32768:
            raise ValueError('invalid or oversized public evidence')
        if self.origin is not None and (not isinstance(self.origin, PublicSymbolOriginV1)
                or self.provenance is not SemanticProvenanceV1.PUBLIC_CONTRACT_EVIDENCE):
            raise ValueError('invalid public origin provenance')


@dataclass(frozen=True, slots=True)
class AcceptanceObligationV1:
    obligation_id: str
    kind: str
    requirement: str
    evidence_refs: tuple[str, ...]
    mandatory: bool = True

    def __post_init__(self):
        if self.kind not in ('OBJECTIVE', 'BEHAVIOR', 'COMPATIBILITY', 'VERIFICATION'):
            raise ValueError('invalid obligation kind')
        if type(self.mandatory) is not bool or not self.obligation_id or not self.requirement:
            raise ValueError('invalid obligation')
        if type(self.evidence_refs) is not tuple or not self.evidence_refs:
            raise ValueError('obligation needs immutable evidence references')


@dataclass(frozen=True, slots=True)
class AcceptanceObligationsV1:
    schema_version: int
    run_id: str
    todo_id: str
    contract_fingerprint: str
    obligations: tuple[AcceptanceObligationV1, ...]
    evidence: tuple[SemanticEvidenceV1, ...]
    unresolved_semantics: tuple[str, ...] = ()
    public_interpretations: tuple['PublicInterpretationV1', ...] = ()

    def __post_init__(self):
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError('acceptance schema_version must be integer 1')
        if not self.run_id or not self.todo_id or len(self.contract_fingerprint) != 64:
            raise ValueError('invalid acceptance identity')
        if any(type(getattr(self, k)) is not tuple for k in ('obligations','evidence','unresolved_semantics','public_interpretations')):
            raise ValueError('acceptance state must be immutable')
        if not self.obligations or len(self.obligations) > 32 or len(self.evidence) > 32:
            raise ValueError('acceptance cardinality limit')
        if len(self.public_interpretations) > 32 or any(not isinstance(i,PublicInterpretationV1) for i in self.public_interpretations):
            raise ValueError('interpretations must be bounded immutable typed objects')
        ids = [e.evidence_id for e in self.evidence]
        if len(ids) != len(set(ids)) or len({o.obligation_id for o in self.obligations}) != len(self.obligations):
            raise ValueError('duplicate acceptance identity')
        if any(not set(o.evidence_refs) <= set(ids) for o in self.obligations):
            raise ValueError('unknown obligation evidence')
        if len(canonical(self.to_dict()).encode('utf-8')) > 65536:
            raise ValueError('acceptance exceeds 64 KiB; cannot silently truncate requirements')

    def to_dict(self):
        value = asdict(self)
        # Historical snapshots without origin retain their exact canonical bytes.
        for evidence in value['evidence']:
            if evidence['origin'] is None:
                del evidence['origin']
        for interpretation in value['public_interpretations']:
            if interpretation['input_validation'] is None:
                del interpretation['input_validation']
            witness = interpretation.get('witness')
            if witness is not None and witness['expected_exception'] is None:
                del witness['expected_exception']
        return value

    @classmethod
    def from_dict(cls, raw):
        return cls(raw['schema_version'], raw['run_id'], raw['todo_id'], raw['contract_fingerprint'],
            tuple(AcceptanceObligationV1(**{**o,'evidence_refs':tuple(o['evidence_refs'])}) for o in raw['obligations']),
            tuple(SemanticEvidenceV1(**{**e,'provenance':SemanticProvenanceV1(e['provenance']),
                'origin':PublicSymbolOriginV1(**e['origin']) if e.get('origin') is not None else None}) for e in raw['evidence']),
            tuple(raw['unresolved_semantics']),
            tuple(PublicInterpretationV1(**{**p,'public_refs':tuple(p['public_refs']),
                'input_validation':PublicWitnessInputValidationV1(**{**p['input_validation'],
                    'public_refs':tuple(p['input_validation']['public_refs'])}) if p.get('input_validation') is not None else None,
                'witness':PublicWitnessV1(**p['witness']) if p.get('witness') is not None else None})
                for p in raw.get('public_interpretations', ())))


@dataclass(frozen=True, slots=True)
class PublicInterpretationV1:
    obligation_id: str
    expected_behavior: str
    public_refs: tuple[str, ...]
    unresolved: bool
    provenance: str = 'MODEL_HYPOTHESIS'
    candidate_independent: bool = True
    witness: 'PublicWitnessV1 | None' = None
    input_validation: PublicWitnessInputValidationV1 | None = None

    def __post_init__(self):
        if type(self.unresolved) is not bool or self.provenance != 'MODEL_HYPOTHESIS' or self.candidate_independent is not True:
            raise ValueError('public interpretation is a candidate-independent hypothesis, never authority')
        if not self.obligation_id or type(self.expected_behavior) is not str or not self.expected_behavior or len(self.expected_behavior.encode('utf-8')) > 2048:
            raise ValueError('invalid bounded public interpretation')
        if type(self.public_refs) is not tuple or len(self.public_refs) > 8 or any(type(r) is not str or not r for r in self.public_refs) or len(self.public_refs) != len(set(self.public_refs)):
            raise ValueError('invalid immutable public interpretation references')
        if self.witness is not None and not isinstance(self.witness,PublicWitnessV1):
            raise ValueError('witness must be immutable typed public data')
        if self.input_validation is not None and not isinstance(self.input_validation,PublicWitnessInputValidationV1):
            raise ValueError('invalid typed witness input validation')


@dataclass(frozen=True, slots=True)
class PublicWitnessV1:
    module_path: str
    callable_name: str
    arguments_json: str
    expected_text: str
    expected_expression: str | None = None
    expected_exception: str | None = None

    def __post_init__(self):
        if any(type(v) is not str for v in (self.module_path,self.callable_name,self.arguments_json,self.expected_text)):
            raise ValueError('public witness fields must be exact strings')
        if len(self.arguments_json.encode('utf-8')) > 2048 or len(self.expected_text.encode('utf-8')) > 256:
            raise ValueError('public witness exceeds bounds')
        if not self.module_path or not 1 <= len(self.callable_name.split('.')) <= 2 or any(not p.isidentifier() or p.startswith('_') for p in self.callable_name.split('.')):
            raise ValueError('invalid public callable')
        if self.expected_exception not in (None, '', 'ValueError'):
            raise ValueError('unsupported expected public exception')
        if self.expected_expression is not None and (type(self.expected_expression) is not str or not self.expected_expression or len(self.expected_expression.encode()) > 1024):
            raise ValueError('invalid bounded public reference expression')


def public_callable_surface(frozen):
    from .callable_authority import from_frozen, declared_surface
    if from_frozen(frozen) is not None:
        return declared_surface(frozen)
    result=[]
    for evidence in frozen.evidence:
        if evidence.provenance is not SemanticProvenanceV1.OBSERVED_IMPLEMENTATION or not evidence.path.endswith('.py'):
            continue
        try:
            tree=ast.parse(evidence.content)
        except SyntaxError:
            continue
        for node in tree.body:
            if isinstance(node,ast.FunctionDef) and not node.name.startswith('_'):
                defaults = {}
                for parameter, value in zip((node.args.posonlyargs+node.args.args)[-len(node.args.defaults):], node.args.defaults):
                    if isinstance(value, ast.Constant):
                        defaults[parameter.arg] = value.value
                for parameter, value in zip(node.args.kwonlyargs, node.args.kw_defaults):
                    if isinstance(value, ast.Constant):
                        defaults[parameter.arg] = value.value
                result.append({'module_path':evidence.path,'callable_name':node.name,
                    'parameters':[a.arg for a in node.args.posonlyargs+node.args.args+node.args.kwonlyargs],
                    'positional_parameters':[a.arg for a in node.args.posonlyargs+node.args.args],
                    'literal_defaults':defaults,
                    'keyword_witness_supported':not (node.args.posonlyargs or node.args.vararg or node.args.kwarg),
                    'docstring':ast.get_docstring(node,clean=False)})
    return result


def independent_obligation_refs(frozen, obligation):
    kinds = {SemanticProvenanceV1.AUTHORITATIVE_REQUIREMENT, SemanticProvenanceV1.PUBLIC_CONTRACT_EVIDENCE,
             SemanticProvenanceV1.PREEXISTING_TEST_EVIDENCE}
    from .callable_authority import from_frozen
    if obligation.kind == 'BEHAVIOR' and from_frozen(frozen) is None:
        kinds.discard(SemanticProvenanceV1.AUTHORITATIVE_REQUIREMENT)
    return {e.evidence_id for e in frozen.evidence if e.provenance in kinds} & set(obligation.evidence_refs)


def public_witness_requires_boolean(frozen, obligation):
    """Only explicit public return-type requirements; never infer from candidate."""
    import re
    refs = independent_obligation_refs(frozen, obligation)
    return any(re.search(r'\b(?:boolean return type|return (?:a )?boolean(?: authorization decision)?)\b',
                        e.content, re.IGNORECASE)
               for e in frozen.evidence if e.evidence_id in refs and e.provenance in
               (SemanticProvenanceV1.AUTHORITATIVE_REQUIREMENT, SemanticProvenanceV1.PUBLIC_CONTRACT_EVIDENCE))


def public_witness_binding(frozen, obligation):
    """Unique frozen origin through existing obligation/evidence edges only.

    No text matching, candidate inspection, or first-function selection. Legacy
    snapshots without origin remain unresolved rather than rewriting history.
    """
    from .callable_authority import from_frozen, declared_binding
    if from_frozen(frozen) is not None:
        return declared_binding(frozen,obligation)
    origins = {e.evidence_id:e for e in frozen.evidence if e.origin is not None}
    refs = set(obligation.evidence_refs)
    if any(e.evidence_id in refs and e.provenance is SemanticProvenanceV1.PUBLIC_CONTRACT_EVIDENCE
           and e.path.endswith('.py') and e.origin is None for e in frozen.evidence):
        return None
    direct = [origins[r] for r in refs if r in origins]
    source = 'FROZEN_EVIDENCE_ORIGIN'
    if not direct:
        # Only an explicitly co-referenced public evidence node can connect a
        # README/spec obligation to a source-owned contract.
        public_refs = refs & {e.evidence_id for e in frozen.evidence
            if e.provenance is SemanticProvenanceV1.PUBLIC_CONTRACT_EVIDENCE}
        connected = set().union(*(set(o.evidence_refs) for o in frozen.obligations
            if public_refs & set(o.evidence_refs))) if public_refs else set()
        direct = [origins[r] for r in connected if r in origins]
        source = 'FROZEN_OBLIGATION_GRAPH'
    identities = {(e.path, e.origin.qualified_name, e.origin.signature_fingerprint) for e in direct}
    if len(identities) != 1:
        return None
    evidence = sorted(direct, key=lambda e:e.evidence_id)[0]
    return {'evidence_id':evidence.evidence_id,'module_path':evidence.path,
            'callable_name':evidence.origin.qualified_name,
            'signature_fingerprint':evidence.origin.signature_fingerprint,'binding_source':source}


def validate_witness_binding(frozen, obligation, module_path, callable_name):
    binding = public_witness_binding(frozen, obligation)
    if binding is None:
        raise ValueError('PUBLIC_WITNESS_BINDING_UNRESOLVED')
    if (module_path, callable_name) != (binding['module_path'], binding['callable_name']):
        raise ValueError('PUBLIC_WITNESS_BINDING_MISMATCH: public docstring witness must target its original callable')
    return binding


def public_interpretation_request(frozen):
    """No implementation expressions, candidate diff or generated test data."""
    required = [o for o in frozen.obligations if o.kind != 'VERIFICATION']
    public = [e for e in frozen.evidence if e.provenance in (
        SemanticProvenanceV1.AUTHORITATIVE_REQUIREMENT, SemanticProvenanceV1.PUBLIC_CONTRACT_EVIDENCE,
        SemanticProvenanceV1.PREEXISTING_TEST_EVIDENCE)]
    refs = [e.evidence_id for e in public]
    surface=public_callable_surface(frozen)
    def json_value(depth):
        choices=[{'type':t} for t in ('null','boolean','number','string')]
        if depth:
            child=json_value(depth-1)
            choices += [{'type':'array','maxItems':16,'items':child}]
        return {'anyOf':choices}
    witness_choices=[{'type':'null'}]
    for s in surface:
        if not s['keyword_witness_supported']:
            continue
        from .public_witness_contract import callable_witness_contract
        try: capability=callable_witness_contract(frozen,s)
        except ValueError: continue
        argument_properties=capability.argument_schema(lambda name:({'type':'number'} if type(s['literal_defaults'].get(name)) in (int,float) else json_value(2)))
        arguments_schema={'type':'object','additionalProperties':False,'required':['args','kwargs'],
            'properties':{'args':{'type':'array','maxItems':0,'items':{'type':'null'}},
                'kwargs':{'type':'object','additionalProperties':False,'required':s['parameters'],
                    'properties':argument_properties}}}
        witness_choices.append({'type':'object','additionalProperties':False,
            'required':['module_path','callable_name','arguments','expected_expression'],
            'properties':{'module_path':{'type':'string','enum':[s['module_path']]},
                'callable_name':{'type':'string','enum':[s['callable_name']]},
                'arguments':arguments_schema,'expected_expression':{'type':'string','minLength':1,'maxLength':1024}}})
        if 'declaration' in s:
            choice = witness_choices[-1]
            choice['required'].append('expected_exception')
            choice['properties']['expected_exception'] = {'type':'string','enum':['',s['declaration']['expected_exception']] if s['declaration']['expected_exception'] else ['']}
            choice['properties']['expected_expression']['enum'] = [s['declaration']['reference_expression']]
    def bound_schema(obligation):
        binding = public_witness_binding(frozen, obligation)
        choices = [witness_choices[0]]
        if binding is not None:
            choices += [choice for choice in witness_choices[1:]
                if choice['properties']['module_path']['enum'] == [binding['module_path']]
                and choice['properties']['callable_name']['enum'] == [binding['callable_name']]]
        return {'anyOf':choices}
    properties={o.obligation_id:{'type':'object','additionalProperties':False,
        'required':['obligation_id','expected_behavior','public_refs','unresolved','witness'],
        'properties':{'obligation_id':{'type':'string','enum':[o.obligation_id]},
            'expected_behavior':{'type':'string','minLength':1,'maxLength':2048},
            'public_refs':{'type':'array','minItems':1,'maxItems':8,
                'items':{'type':'string','enum':sorted(independent_obligation_refs(frozen,o))}},
            'unresolved':{'type':'boolean'},'witness':bound_schema(o)}} for o in required}
    schema={'type':'object','additionalProperties':False,'required':['interpretations'],
        'properties':{'interpretations':{'type':'object','additionalProperties':False,
            'required':list(properties),'properties':properties}}}
    system=('Interpret public acceptance requirements BEFORE any candidate mutation. '
        'You have no candidate, implementation expressions, generated tests, or explorer hypotheses. '
        'State a concise expected relationship/behavior for each obligation, grounded only in provided public evidence. '
        'Resolve ordering and numeric relationships explicitly when stated in a public contract. '
        'Do not infer missing semantics. If ambiguous or conflicting set unresolved=true. '
        'Your output is MODEL_HYPOTHESIS requiring final Judge entailment validation, never authoritative new requirements. '
        'Compile one small candidate-independent public witness for each obligation where supported: '
        'a call to its documented existing public Python function, arguments containing exactly args (array) and kwargs (object), '
        'Use args=[] and supply ALL named parameters, including optional parameters, in kwargs as required by the selected function schema. '
        'Every numeric optional argument MUST differ from its declared default; defaults in API metadata are not witness values. '
        'Provide expected_expression in the bounded public-reference arithmetic language, not a guessed numeric answer. '
        + public_reference_language_contract() +
        'The harness evaluates this expression retaining its type and renders text only for diagnostics. '
        'An explicit Boolean return contract requires exactly two Boolean named parameters and a Boolean '
        'reference using both, for example bool_or(active,override); text(...) is not a Boolean result. '
        'The Boolean hypothesis is checked against all four Boolean input combinations. '
        'Use the actual parameter names; explicitly include all numeric option parameters in the relationship. '
        'Arguments must be actual JSON values, never JSON encoded inside a string or Python tuple notation. '
        'Use argument units/scales exactly as described by cited frozen public evidence. '
        'Do not convert units/scales unless that evidence explicitly specifies conversion. '
        'Use simple round-number inputs so the expected public result can be established without uncertain arithmetic. '
        'Exercise the stated interaction using non-default non-zero argument values, not only trivial/default cases already tested. '
        'Ground the expected result only in public requirements; implementation bodies are unavailable. '
        'Unsupported or ambiguous semantics require witness=null and unresolved=true. '
        'No chain-of-thought, code, tools or execution; JSON only matching response_contract.')
    projected = [asdict(e) for e in public]
    from .callable_authority import from_frozen, AUTHORITY
    if from_frozen(frozen) is not None:
        for item in projected:
            if item['evidence_id'] in ('original_constraints','trusted_callable_authority'):
                content = json.loads(item['content'])
                declaration = content['constraints'][AUTHORITY] if item['evidence_id']=='original_constraints' else content
                declaration['original_sources'] = {p:{'sha256':v['sha256']} for p,v in declaration['original_sources'].items()}
                item['content'] = canonical(content)
        system += (' The callable may be a declared new API absent from the original source or an instance method. '
            'Follow the explicit trusted constructor contract; never supply self/cls or a fabricated receiver. '
            'Use expected_exception="ValueError" only for the declared reversed primitive outcome; otherwise use "". '
            'Reference data domains are finite qualification bounds, not a restriction of the public integer API. '
            'Dependency interface visibility is not proof of accepted commit/lease handoff.')
    payload={'obligations':[asdict(o) for o in required], 'public_evidence':projected,
        'public_callable_surface':surface,
        'authoritative_witness_bindings':{o.obligation_id:public_witness_binding(frozen,o) for o in required},
        'response_contract':schema,'candidate_present':False}
    if from_frozen(frozen) is not None:
        payload['dependency_interface_observations'] = [json.loads(e.content) for e in frozen.evidence
            if e.evidence_id.startswith('dependency_interface:') and e.provenance is SemanticProvenanceV1.CANDIDATE_DERIVED]
    return system, canonical(payload), schema


def public_witness_repair_request(frozen, checked, *, proposal_fingerprint, attempt_index):
    """Localize existing public authority and rejected model output; no answers."""
    if (type(attempt_index) is not int or attempt_index != 1
            or type(proposal_fingerprint) is not str or len(proposal_fingerprint) != 64
            or replace(checked, public_interpretations=()) != replace(frozen, public_interpretations=())):
        raise ValueError('invalid public witness repair identity')
    system, _, schema = public_interpretation_request(frozen)
    public = {e.evidence_id:e for e in frozen.evidence if e.provenance in (
        SemanticProvenanceV1.AUTHORITATIVE_REQUIREMENT, SemanticProvenanceV1.PUBLIC_CONTRACT_EVIDENCE,
        SemanticProvenanceV1.PREEXISTING_TEST_EVIDENCE)}
    obligations = {o.obligation_id:o for o in frozen.obligations}
    surface = public_callable_surface(frozen)
    failed = []
    unchanged = []
    for interpretation in checked.public_interpretations:
        validation = interpretation.input_validation
        if validation is None or validation.status == 'VALID':
            unchanged.append(asdict(interpretation))
            continue
        obligation = obligations[interpretation.obligation_id]
        binding = public_witness_binding(frozen, obligation)
        failed.append({'obligation':asdict(obligation),
            'validation':validation.to_dict(),
            'previous_proposal':asdict(interpretation),
            'cited_public_evidence':[asdict(public[r]) for r in interpretation.public_refs],
            'authoritative_witness_binding':binding,
            'public_callable_surface':[s for s in surface if binding is not None and
                (s['module_path'],s['callable_name']) == (binding['module_path'],binding['callable_name'])]})
    packet = {'schema_version':1,'public_snapshot_fingerprint':fingerprint(frozen.to_dict()),
        'proposal_fingerprint':proposal_fingerprint,'attempt_index':attempt_index,
        'failed_obligations':failed,'unchanged_model_proposals':unchanged,
        'candidate_present':False,'response_contract':schema,
        'instruction':'Author a fresh complete interpretation. Argument units, scale, representation and '
            'transformations must be supported by the cited public evidence. Do not invent unit conversions '
            'or merely change arbitrary example numbers. Original implementation output is not authority. '
            'If public evidence cannot support an admissible witness, set unresolved=true and witness=null.'}
    return system, 'WITNESS_INPUT_DOMAIN_REPAIR\n'+canonical(packet), schema


def validate_public_interpretations(payload, frozen):
    if type(payload) is not dict or set(payload) != {'interpretations'} or type(payload['interpretations']) is not dict:
        raise ValueError('invalid public interpretation envelope')
    expected = {o.obligation_id:o for o in frozen.obligations if o.kind != 'VERIFICATION'}
    rows = []
    public = {e.evidence_id for e in frozen.evidence if e.provenance in (
        SemanticProvenanceV1.AUTHORITATIVE_REQUIREMENT, SemanticProvenanceV1.PUBLIC_CONTRACT_EVIDENCE,
        SemanticProvenanceV1.PREEXISTING_TEST_EVIDENCE)}
    for oid,p in payload['interpretations'].items():
        if type(p) is not dict or set(p) != {'obligation_id','expected_behavior','public_refs','unresolved','witness'}:
            raise ValueError('invalid interpretation keys')
        if p['obligation_id'] != oid:
            raise ValueError('public interpretation key/identity mismatch')
        if oid not in expected or type(p['public_refs']) is not list or not set(p['public_refs']) <= public:
            raise ValueError('unknown/duplicate interpretation or non-public evidence')
        obligation = expected.pop(oid)
        if not p['unresolved'] and not set(p['public_refs']) & independent_obligation_refs(frozen,obligation):
            raise ValueError('interpretation is not grounded in its public obligation')
        witness=None if p['witness'] is None else PublicWitnessV1(**p['witness'])
        if witness is not None:
            validate_witness_binding(frozen, obligation, witness.module_path, witness.callable_name)
        if witness is not None and (witness.module_path,witness.callable_name) not in {
                (s['module_path'],s['callable_name']) for s in public_callable_surface(frozen)}:
            raise ValueError('witness targets an unknown original public callable')
        rows.append(PublicInterpretationV1(**{**p,'witness':witness,'public_refs':tuple(p['public_refs'])}))
    if expected:
        raise ValueError('missing public interpretation')
    return tuple(rows)


def build_acceptance_obligations(*, run_id, contract, sources, tests, public_contracts=None, unresolved=(), accepted_dependency=None):
    """Snapshot public data before mutation. Docstrings are claims for Judge to
    assess, never automatically true; implementation expressions are not specs.
    """
    from .callable_authority import AUTHORITY, build_declared_acceptance
    if AUTHORITY in contract.constraints:
        return build_declared_acceptance(run_id=run_id,contract=contract,sources=sources,unresolved=unresolved,
            public_contracts=public_contracts,accepted_dependency=accepted_dependency)
    P = SemanticProvenanceV1
    evidence = [SemanticEvidenceV1('original_objective', P.AUTHORITATIVE_REQUIREMENT, contract.objective),
        SemanticEvidenceV1('original_constraints', P.AUTHORITATIVE_REQUIREMENT, canonical(contract.to_dict()))]
    obligations = [AcceptanceObligationV1('objective', 'OBJECTIVE', contract.objective, ('original_objective',)),
        AcceptanceObligationV1('verification', 'VERIFICATION',
            'All required deterministic gates must pass; passing tests alone does not prove semantic correctness.',
            ('original_constraints',))]
    for i, (path, text) in enumerate(sorted(sources.items())):
        sha = hashlib.sha256(text.encode('utf-8')).hexdigest()
        evidence.append(SemanticEvidenceV1(f'original_source_{i}', P.OBSERVED_IMPLEMENTATION, text, path, sha))
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for j, node in enumerate(n for n in ast.walk(tree) if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))):
            doc = ast.get_docstring(node, clean=False)
            if not doc:
                continue
            ref = f'public_contract_{i}_{j}'
            origin = None
            if isinstance(node, ast.FunctionDef) and node in tree.body:
                signature = public_signature(node)
                origin = PublicSymbolOriginV1('FUNCTION', node.name, canonical(signature), fingerprint(signature),
                    node.lineno, node.end_lineno)
            evidence.append(SemanticEvidenceV1(ref, P.PUBLIC_CONTRACT_EVIDENCE, doc, path, sha, origin))
            obligations.append(AcceptanceObligationV1(ref, 'BEHAVIOR',
                'Preserve applicable explicit public contract (not implementation-derived): ' + doc, (ref, 'original_objective')))
    for i, (path, text) in enumerate(sorted(tests.items())):
        evidence.append(SemanticEvidenceV1(f'preexisting_test_{i}', P.PREEXISTING_TEST_EVIDENCE,
            text, path, hashlib.sha256(text.encode('utf-8')).hexdigest()))
    for i, (path, text) in enumerate(sorted((public_contracts or {}).items())):
        ref = f'public_spec_{i}'
        evidence.append(SemanticEvidenceV1(ref, P.PUBLIC_CONTRACT_EVIDENCE, text, path,
            hashlib.sha256(text.encode('utf-8')).hexdigest()))
        obligations.append(AcceptanceObligationV1(ref, 'BEHAVIOR',
            'Satisfy applicable pre-existing public specification; judge applicability/conflicts without inventing semantics.',
            (ref, 'original_objective')))
    # The root objective can be grounded in its explicit pre-existing public
    # contracts, not just its often terse request text. Freeze those references
    # now; candidate/test-derived material can never expand them afterwards.
    root = obligations[0]
    obligations[0] = replace(root, evidence_refs=(*root.evidence_refs,
        *(e.evidence_id for e in evidence if e.provenance is SemanticProvenanceV1.PUBLIC_CONTRACT_EVIDENCE)))
    if 'PUBLIC_SEMANTIC_GROUP_LIMIT' in contract.constraints:
        limit=contract.constraints['PUBLIC_SEMANTIC_GROUP_LIMIT']
        if type(limit) is not int or limit!=3:
            raise ValueError('unsupported bounded public group contract')
        origins=[e for e in evidence if e.origin is not None]
        identities={(e.path,e.origin.qualified_name,e.origin.signature_fingerprint) for e in origins}
        if not 1<=len(identities)<=limit:
            raise ValueError('public callable topology exceeds explicit group contract')
        # Preserve the complete original objective/specification in each API
        # row. All rows remain mandatory: no arbitrary first-function binding,
        # omitted public requirement, or candidate-derived normative content.
        expanded=[]
        origin_refs={e.evidence_id for e in origins}
        for obligation in obligations:
            direct=set(obligation.evidence_refs)&origin_refs
            if obligation.kind!='VERIFICATION' and len(direct)!=1:
                for origin in origins:
                    expanded.append(replace(obligation,
                        obligation_id=obligation.obligation_id+':'+origin.evidence_id,
                        evidence_refs=tuple(r for r in obligation.evidence_refs if r not in origin_refs)
                            +(origin.evidence_id,)))
            else:expanded.append(obligation)
        obligations=expanded
    return AcceptanceObligationsV1(1, run_id, contract.todo_id, fingerprint(contract.to_dict()),
        tuple(obligations), tuple(evidence), tuple(unresolved))


def public_group_limit(frozen):
    constraints=next((e.content for e in frozen.evidence if e.evidence_id=='original_constraints'),None)
    if constraints is None or 'PUBLIC_SEMANTIC_GROUP_LIMIT' not in constraints:
        return 1
    value=__import__('json').loads(constraints)['constraints'].get('PUBLIC_SEMANTIC_GROUP_LIMIT',1)
    if type(value) is not int or value not in (1,3):
        raise ValueError('invalid bounded public group contract')
    return value


@dataclass(frozen=True, slots=True)
class AcceptanceEvidenceV1:
    obligation_id: str
    status: str
    evidence_refs: tuple[str, ...]
    expectation_basis: tuple[str, ...]
    summary: str
    support: str
    candidate_evidence: str

    def __post_init__(self):
        if self.candidate_evidence != 'candidate_diff' or type(self.candidate_evidence) is not str:
            raise ValueError('acceptance must bind the current candidate_diff')
        if self.status not in ('SATISFIED','VIOLATED','UNRESOLVED') or self.support not in ('INDEPENDENT','NON_INDEPENDENT'):
            raise ValueError('invalid semantic support/status')
        if self.status == 'SATISFIED' and self.support != 'INDEPENDENT':
            raise ValueError('non-independent evidence cannot satisfy acceptance')
        if not self.obligation_id or type(self.summary) is not str or not self.summary or len(self.summary.encode('utf-8')) > 2048:
            raise ValueError('invalid acceptance evidence identity/summary')
        for refs in (self.evidence_refs,self.expectation_basis):
            if type(refs) is not tuple or len(refs) > 8 or any(type(r) is not str or not r for r in refs) or len(refs) != len(set(refs)):
                raise ValueError('acceptance references must be bounded immutable unique strings')

    def to_dict(self):
        return asdict(self)


def assess_acceptance_rows(obligations, rows):
    """Validate known provenance only. A citation is not semantic entailment.
    Judge must assess entailment/contradictions; uncertainty stays UNRESOLVED.
    """
    if type(rows) is not list or len(rows) != len(obligations.obligations):
        raise ValueError('acceptance evidence must cover every frozen obligation exactly once')
    expected = {o.obligation_id:o for o in obligations.obligations}
    known = {e.evidence_id for e in obligations.evidence} | {'candidate_diff','generated_test_diff','verification_report','critic_report','explorer_hypotheses'}
    results = []
    for row in rows:
        if type(row) is not dict or set(row) != {'obligation_id','status','candidate_evidence','evidence_refs','expectation_basis','summary'}:
            raise ValueError('invalid acceptance evidence keys')
        if type(row['candidate_evidence']) is not str or row['candidate_evidence'] != 'candidate_diff':
            raise ValueError('acceptance must bind the current candidate_diff')
        oid = row['obligation_id']
        if oid not in expected:
            raise ValueError('unknown or duplicate obligation')
        obligation = expected.pop(oid)
        if row['status'] not in ('SATISFIED','VIOLATED','UNRESOLVED'):
            raise ValueError('invalid semantic status')
        for key in ('evidence_refs','expectation_basis'):
            refs = row[key]
            if type(refs) is not list or len(refs) > 8 or any(type(x) is not str or x not in known for x in refs) or len(refs) != len(set(refs)):
                raise ValueError('invalid acceptance evidence references')
        if type(row['summary']) is not str or not row['summary'] or len(row['summary'].encode('utf-8')) > 2048:
            raise ValueError('bounded acceptance explanation required, no reasoning transcript')
        normative = set(row['expectation_basis']) & independent_obligation_refs(obligations,obligation)
        support = 'INDEPENDENT' if normative else 'NON_INDEPENDENT'
        status = row['status']
        if not normative and status == 'SATISFIED':
            status = 'UNRESOLVED'
        if status == 'SATISFIED' and obligation.kind == 'VERIFICATION' and 'verification_report' not in row['evidence_refs']:
            status = 'UNRESOLVED'
        interpretation = next((i for i in obligations.public_interpretations if i.obligation_id == oid),None)
        if interpretation is not None and interpretation.unresolved:
            status = 'UNRESOLVED'
        results.append(AcceptanceEvidenceV1(oid, status, tuple(row['evidence_refs']), tuple(row['expectation_basis']), row['summary'], support, row['candidate_evidence']))
    return tuple(results)

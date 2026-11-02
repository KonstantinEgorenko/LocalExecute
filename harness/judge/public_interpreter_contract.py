"""Candidate-independent callable grouping and compact public semantic protocol.

Shared by production and opt-in diagnostics; validators retain acceptance authority.
"""
from dataclasses import dataclass
from ..agent_loop.protocol import decode_strict_json
from ..verification.acceptance import (canonical, fingerprint, independent_obligation_refs,
    public_witness_binding, public_interpretation_request)
from ..verification.witness_domain import PublicWitnessInputValidationV1

RECOVERY_KEYS = ('attempt_index', 'previous_attempt_status', 'validation_reason', 'exception_class')
FRESH_INSTRUCTION = (' The prior model hypothesis was rejected by original-call admissibility. '
    'Author a fresh hypothesis from the frozen public evidence. '
    'Do not reconstruct or normalize an earlier proposal.')


def recovery_state(validation):
    if not isinstance(validation, PublicWitnessInputValidationV1) or (
            validation.status != 'INVALID' or validation.reason != 'ORIGINAL_CALL_NOT_ADMISSIBLE'):
        raise ValueError('resynthesis requires verified original-call INVALID evidence')
    return dict(attempt_index=1, previous_attempt_status=validation.status,
        validation_reason=validation.reason, exception_class=validation.exception_class)


def fresh_witness_request(frozen, group, validation):
    system, user, schema = witness_request(frozen, group)
    packet = decode_strict_json(user)
    packet['recovery_state'] = recovery_state(validation)
    return system + FRESH_INSTRUCTION, canonical(packet), schema

@dataclass(frozen=True, slots=True)
class PublicSemanticGroupV1:
    group_id: str
    module_path: str
    callable_name: str
    signature_fingerprint: str
    obligation_ids: tuple[str, ...]
    public_refs: tuple[str, ...]

    def project(self, frozen):
        # Existing production projection is the public-content boundary.
        _, user, _ = public_interpretation_request(frozen)
        public = decode_strict_json(user)
        return {'group_id': self.group_id, 'binding': {
            'module_path': self.module_path, 'callable_name': self.callable_name,
            'signature_fingerprint': self.signature_fingerprint},
            'obligation_ids': list(self.obligation_ids), 'public_refs': list(self.public_refs),
            'public_evidence': [e for e in public['public_evidence'] if e['evidence_id'] in self.public_refs],
            'public_callable_surface': [{k: v for k, v in s.items() if k != 'docstring'}
                for s in public['public_callable_surface']
                if (s['module_path'], s['callable_name']) == (self.module_path, self.callable_name)]}


def semantic_groups(frozen):
    """Binding/evidence topology only. Never inspect oracle or evidence text."""
    buckets = {}
    for obligation in frozen.obligations:
        if obligation.kind == 'VERIFICATION':
            continue
        binding = public_witness_binding(frozen, obligation)
        if binding is None:
            raise ValueError('diagnostic structural binding unresolved')
        key = tuple(binding[k] for k in ('module_path', 'callable_name', 'signature_fingerprint'))
        ids, refs = buckets.setdefault(key, (set(), set()))
        ids.add(obligation.obligation_id)
        refs.update(independent_obligation_refs(frozen, obligation))
    return tuple(PublicSemanticGroupV1('group:' + fingerprint(key), *key,
        tuple(sorted(ids)), tuple(sorted(refs))) for key, (ids, refs) in sorted(buckets.items()))


def authority_request(frozen, groups):
    properties = {g.group_id: {'type': 'object', 'additionalProperties': False,
        'required': ['group_id', 'status', 'public_refs'], 'properties': {
            'group_id': {'type': 'string', 'enum': [g.group_id]},
            'status': {'type': 'string', 'enum': ['SUPPORTED', 'CONFLICTING', 'AMBIGUOUS']},
            'public_refs': {'type': 'array', 'minItems': 1, 'maxItems': len(g.public_refs),
                'uniqueItems': True, 'items': {'type': 'string', 'enum': list(g.public_refs)}}}}
        for g in groups}
    schema = {'type': 'object', 'additionalProperties': False, 'required': ['groups'],
        'properties': {'groups': {'type': 'object', 'additionalProperties': False,
            'required': list(properties), 'properties': properties}}}
    system = ('Classify the public authority for each callable group using only provided frozen public evidence. '
        'SUPPORTED: evidence supplies one coherent public semantic contract. '
        'CONFLICTING: currently authoritative evidence gives mutually incompatible requirements without an '
        'authoritative precedence rule resolving them. AMBIGUOUS: evidence does not specify enough semantics '
        'to construct the required relationship safely. Do not invent precedence or unit conversions. '
        'Return only disposition and cited evidence IDs. No rationale, arithmetic, numeric examples, '
        'tools or execution. JSON only matching response_contract.')
    return system, canonical({'groups': [{k: v for k, v in g.project(frozen).items()
        if k != 'public_callable_surface'} for g in groups],
        'response_contract': schema, 'candidate_present': False}), schema


def parse_authority(raw, groups):
    value = decode_strict_json(raw)
    if type(value) is not dict or set(value) != {'groups'} or type(value['groups']) is not dict:
        raise ValueError('invalid authority envelope')
    if set(value['groups']) != {g.group_id for g in groups}:
        raise ValueError('authority group coverage mismatch')
    for g in groups:
        row = value['groups'][g.group_id]
        if (type(row) is not dict or set(row) != {'group_id', 'status', 'public_refs'}
            or row['group_id'] != g.group_id or row['status'] not in ('SUPPORTED', 'CONFLICTING', 'AMBIGUOUS')
            or type(row['public_refs']) is not list or not row['public_refs']
            or any(type(r) is not str for r in row['public_refs'])
            or len(set(row['public_refs'])) != len(row['public_refs'])
            or not set(row['public_refs']) <= set(g.public_refs)):
            raise ValueError('invalid authority disposition/references')
    return value


def witness_request(frozen, group, rejected=None):
    from ..verification.acceptance import public_callable_surface
    from ..verification.public_witness_contract import callable_witness_contract
    surface=next(s for s in public_callable_surface(frozen) if (s['module_path'],s['callable_name'])==(group.module_path,group.callable_name))
    capability=callable_witness_contract(frozen,surface)
    production_system, _, production_schema = public_interpretation_request(frozen)
    row_schema = production_schema['properties']['interpretations']['properties'][group.obligation_ids[0]]
    choices = [c for c in row_schema['properties']['witness']['anyOf'] if c['type'] == 'object']
    if len(choices) != 1:
        raise ValueError('single authoritative diagnostic witness schema required')
    schema = {'type': 'object', 'additionalProperties': False, 'required': ['group_id', 'witness'],
        'properties': {'group_id': {'type': 'string', 'enum': [group.group_id]}, 'witness': choices[0]}}
    # Reuse the authoritative argument/reference-language instructions verbatim.
    rules = production_system[production_system.index('a call to its documented'):
        production_system.index('Unsupported or ambiguous semantics require')]
    system = ('Synthesize ONE candidate-independent witness for this supported public callable group, '
        'grounded only in its provided public evidence. You have no implementation body, candidate or tests. '
        'Do not create separate obligation rows. The witness is a model hypothesis, not new authority. '
        'The witness is ' + rules + 'No rationale, tools or execution. JSON only matching response_contract.')
    payload = {'group': group.project(frozen), 'response_contract': schema, 'candidate_present': False}
    if capability.kind!='LEGACY':
        payload['reference_contract']=capability.readiness()
        system+=(' This callable has a typed nonnumeric reference contract. Use the exact parameter-bound relationship '+capability.expression+'. Numeric option requirements apply only to numeric optional parameters. '
            'Generic examples: trim_casefold(label); map_get(values,trim_casefold(label),fallback) returns the matching scalar or the supplied fallback; '
            'pairs_map_normalized(entries) trims/casefolds ASCII keys in input order, last duplicate wins. Mapping arguments must be JSON objects, never pair arrays. No implementation expression or candidate result is supplied.')
        if capability.kind=='DECLARED_PUBLIC_CALLABLE':
            system += (' This declared callable uses typed integer or ordered integer-pair inputs, not ASCII/mapping examples. '
                'The receiver is supplied by the fixed harness constructor contract, never by witness arguments. '
                'Include expected_exception: "ValueError" only for a declared reversed-interval outcome; otherwise "". '
                'Use the exact public reference relationship and argument domains in reference_contract.')
    if rejected is not None:
        payload['rejected_witness'] = rejected['witness']
        payload['input_validation'] = rejected['validation']
        system += (' The previous witness was rejected by original-input admissibility. Author one fresh '
            'witness supported by the same public evidence. Do not invent conversions or normalize '
            'previous values automatically. Original returned values are not authority.')
    return system, canonical(payload), schema


def compact_envelope(raw, group, frozen):
    """Evaluation-only projection; copy model values exactly into production rows."""
    value = decode_strict_json(raw)
    if (type(value) is not dict or set(value) != {'group_id', 'witness'}
        or value['group_id'] != group.group_id or type(value['witness']) is not dict):
        raise ValueError('invalid compact witness envelope')
    obligations = {o.obligation_id: o for o in frozen.obligations if o.kind != 'VERIFICATION'}
    if set(obligations) != set(group.obligation_ids):
        raise ValueError('compact fixture must have exactly one complete callable group')
    return canonical({'interpretations': {oid: {'obligation_id': oid,
        'expected_behavior': 'Model-authored diagnostic callable witness.',
        'public_refs': sorted(independent_obligation_refs(frozen, obligations[oid])),
        'unresolved': False, 'witness': value['witness']} for oid in group.obligation_ids}})


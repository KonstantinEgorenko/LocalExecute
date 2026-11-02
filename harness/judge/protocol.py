"""Strict local protocol for Ornith Judge output."""
from __future__ import annotations

import json
import copy
import hashlib
from dataclasses import replace
from typing import Any

from .errors import JudgeProtocolError
from .models import JudgeAnalysis, JudgeEvidenceBundle, JudgeDecision, JudgeAction, canonical_json
from ..agent_loop.protocol import decode_strict_json
from ..verification.acceptance import assess_acceptance_rows, validate_public_interpretations, public_callable_surface, canonical, independent_obligation_refs

JUDGE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "deterministic_outcome", "decision", "confidence", "summary", "evidence",
        "next_action", "handoff_guidance", "can_override_deterministic",
    ],
    "properties": {
        "deterministic_outcome": {"type": "string", "enum": ["PASSED", "FAILED"]},
        "decision": {"type": "string", "enum": [d.value for d in JudgeDecision]},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "summary": {"type": "string", "minLength": 1, "maxLength": 5000},
        "evidence": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 3, "uniqueItems": True},
        "next_action": {"type": "string", "enum": [a.value for a in JudgeAction]},
        "handoff_guidance": {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 1200}, "maxItems": 8, "uniqueItems": True},
        "can_override_deterministic": {"type": "boolean", "const": False},
    },
}


def parse_public_interpretations(content, frozen):
    payload = decode_strict_json(content)
    if type(payload) is not dict or type(payload.get('interpretations')) is not dict:
        raise ValueError('invalid public interpretation envelope')
    for row in payload['interpretations'].values():
        if type(row) is not dict:
            raise ValueError('invalid public interpretation row')
        witness = row.get('witness')
        if witness is not None:
            from ..verification.callable_authority import from_frozen, declaration_for, validate_arguments
            declared = from_frozen(frozen) is not None
            keys = {'module_path','callable_name','arguments','expected_expression'} | ({'expected_exception'} if declared else set())
            if type(witness) is not dict or set(witness) != keys:
                raise ValueError('invalid public witness wire fields')
            from ..verification.acceptance import validate_witness_binding
            obligation = next((o for o in frozen.obligations if o.obligation_id == row.get('obligation_id')), None)
            if obligation is None:
                raise ValueError('unknown public witness obligation')
            validate_witness_binding(frozen, obligation, witness['module_path'], witness['callable_name'])
            surface = next((s for s in public_callable_surface(frozen)
                if (s['module_path'],s['callable_name']) == (witness['module_path'],witness['callable_name'])), None)
            if surface is None or not surface['keyword_witness_supported']:
                raise ValueError('unsupported original public callable')
            arguments = witness['arguments']
            if (type(arguments) is not dict or set(arguments) != {'args','kwargs'}
                    or type(arguments['args']) is not list or arguments['args']
                    or type(arguments['kwargs']) is not dict
                    or set(arguments['kwargs']) != set(surface['parameters'])):
                raise ValueError('public witness requires every named parameter and empty args')
            numeric_options = [name for name, default in surface['literal_defaults'].items()
                if type(default) in (int,float)]
            for name in numeric_options:
                value = arguments['kwargs'][name]
                if type(value) not in (int,float) or value == surface['literal_defaults'][name]:
                    raise ValueError('public witness must exercise non-default numeric options')
            from ..verification.public_reference import evaluate_public_reference_value
            from ..verification.acceptance import public_witness_requires_boolean
            boolean_contract = public_witness_requires_boolean(frozen, obligation)
            required = surface['parameters'] if boolean_contract else numeric_options
            if boolean_contract and (len(required) != 2 or any(type(v) is not bool for v in arguments['kwargs'].values())):
                raise ValueError('Boolean witness supports exactly two Boolean parameters')
            if declared:
                expected_value = validate_arguments(declaration_for(frozen,witness['module_path'],witness['callable_name']),
                    arguments,witness['expected_expression'],witness['expected_exception'])
            else:
                expected_value = evaluate_public_reference_value(witness['expected_expression'],arguments['kwargs'],required_names=required)
            if boolean_contract and type(expected_value) is not bool:
                raise ValueError('public Boolean contract requires a Boolean reference result')
            from ..verification.public_witness_contract import callable_witness_contract
            callable_witness_contract(frozen,surface).validate(arguments['kwargs'],witness['expected_expression'])
            expected = str(expected_value)
            row['witness'] = {**{k:v for k,v in witness.items() if k != 'arguments'},
                'arguments_json':canonical(arguments),'expected_text':expected}
    rows = validate_public_interpretations(payload, frozen)
    for row in rows:
        if row.witness is None:
            continue
        arguments = decode_strict_json(row.witness.arguments_json)
        if (type(arguments) is not dict or set(arguments) != {'args', 'kwargs'}
                or type(arguments['args']) is not list or type(arguments['kwargs']) is not dict):
            raise ValueError('public witness arguments require exact args array and kwargs object')
        surface = next(s for s in public_callable_surface(frozen)
            if (s['module_path'],s['callable_name']) == (row.witness.module_path,row.witness.callable_name))
        positional = dict(zip(surface['positional_parameters'], arguments['args']))
        if set(positional) & set(arguments['kwargs']):
            raise ValueError('duplicate positional/keyword witness argument')
        supplied = {**positional, **arguments['kwargs']}
        for name, default in surface['literal_defaults'].items():
            if type(default) in (int,float) and (name not in supplied or supplied[name] == default):
                raise ValueError('public witness must exercise non-default numeric options')
        # Bound nesting independently of byte length, before executing any call.
        pending = [(arguments, 0)]
        count = 0
        while pending:
            value, depth = pending.pop()
            count += 1
            if depth > 8 or count > 128:
                raise ValueError('public witness argument structure exceeds bounds')
            if isinstance(value, dict):
                pending.extend((v, depth + 1) for v in value.values())
            elif isinstance(value, list):
                pending.extend((v, depth + 1) for v in value)
    return rows


def judge_required_keys(*, with_acceptance: bool) -> tuple[str, ...]:
    """Single field inventory, independent of evidence classification."""
    return tuple(JUDGE_RESPONSE_SCHEMA['required']) + (('acceptance_evidence',) if with_acceptance else ())


def judge_response_schema(bundle):
    schema = copy.deepcopy(JUDGE_RESPONSE_SCHEMA)
    schema['required'] = list(judge_required_keys(with_acceptance=bundle.acceptance_obligations is not None))
    schema['properties']['deterministic_outcome']['enum'] = [bundle.deterministic_outcome.value]
    schema['properties']['evidence']['items']['enum'] = sorted(bundle.allowed_evidence_ids)
    # llama.cpp cannot combine object properties with sibling oneOf: the
    # partial decision/action branches can replace the full object grammar.
    # JudgeAnalysis enforces the authoritative decision/action relation.
    schema['properties']['evidence']['allOf'] = [
        {'contains': {'enum': [ref]}} for ref in ('verification_report', 'critic_report')]
    if bundle.deterministic_outcome.value == 'FAILED':
        schema['properties']['decision']['enum'].remove('ACCEPT')
    if bundle.acceptance_obligations is None:
        return schema
    obligations = bundle.acceptance_obligations
    if obligations.unresolved_semantics or any(r.status != 'SATISFIED' for r in bundle.semantic_assessment):
        schema['properties']['decision']['enum'] = [d for d in schema['properties']['decision']['enum'] if d != 'ACCEPT']
    refs = [e.evidence_id for e in obligations.evidence] + ['candidate_diff','generated_test_diff',
        'verification_report','critic_report','explorer_hypotheses']
    branches = []
    for obligation in obligations.obligations:
        normative = sorted(independent_obligation_refs(obligations, obligation))
        # Status branches make nonempty SATISFIED grounding explicit without
        # allowing the model to select another obligation's normative domain.
        for satisfied in (False, True):
            if satisfied and not normative:
                continue
            row = {'type':'object','additionalProperties':False,
            'required':['obligation_id','status','candidate_evidence','evidence_refs','expectation_basis','summary'],
            'properties':{'obligation_id':{'type':'string','enum':[obligation.obligation_id]},
                  'status':{'type':'string','enum':['SATISFIED'] if satisfied else ['VIOLATED','UNRESOLVED']},
                  'candidate_evidence':{'type':'string','enum':['candidate_diff']},
                'evidence_refs':{'type':'array','maxItems':8,'uniqueItems':True,'items':{'type':'string','enum':refs}},
                'expectation_basis':{'type':'array','minItems':1 if satisfied else 0,
                    'maxItems':min(8,len(normative)), 'uniqueItems':True,
                    'items':{'type':'string','enum':normative} if normative else {'type':'string'}},
                'summary':{'type':'string','minLength':1,'maxLength':2048}}}
            if satisfied and obligation.kind == 'VERIFICATION':
                # Encode support identity in items, not backend-optional contains.
                # Normative authority remains in the separate frozen basis domain.
                row['properties']['evidence_refs'] = {'type':'array','minItems':1,'maxItems':1,
                    'items':{'type':'string','enum':['verification_report']}}
            branches.append(row)
    schema['properties']['acceptance_evidence'] = {'type':'array',
        'minItems':len(obligations.obligations),'maxItems':len(obligations.obligations),
        'uniqueItems':True, 'items':{'oneOf':branches},
        'allOf':[{'contains':{'type':'object','required':['obligation_id'],
            'properties':{'obligation_id':{'enum':[o.obligation_id]}}},
            'minContains':1,'maxContains':1} for o in obligations.obligations]}
    return schema


def judge_schema_fingerprint(schema: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(schema).encode('utf-8')).hexdigest()


def judge_validation_feedback(content: str | None, bundle: JudgeEvidenceBundle, error: str) -> dict[str, Any]:
    """Bounded diagnostics derived from the contract; never repairs a verdict."""
    schema = judge_response_schema(bundle)
    result = {'required_schema_fingerprint': judge_schema_fingerprint(schema),
              'response_fingerprint': None if content is None else hashlib.sha256(content.encode('utf-8')).hexdigest(),
              'missing_fields': [], 'unexpected_fields': [], 'invalid_field_types': [],
              'violated_protocol_constraints': [error.encode('utf-8')[:2048].decode('utf-8', 'ignore')]}
    try:
        payload = decode_strict_json(content or '')
    except ValueError:
        return result
    if type(payload) is not dict:
        result['invalid_field_types'] = [{'field': '$', 'expected': 'object', 'observed': type(payload).__name__}]
        return result
    result['missing_fields'] = sorted(set(schema['required']) - set(payload))
    result['unexpected_fields'] = [k.encode('utf-8')[:128].decode('utf-8', 'ignore')
                                   for k in sorted(set(payload) - set(schema['properties']))[:32]]
    types = {'string': lambda v: type(v) is str, 'number': lambda v: type(v) in (int, float),
             'boolean': lambda v: type(v) is bool, 'array': lambda v: type(v) is list,
             'object': lambda v: type(v) is dict}
    def inspect(value, contract, field):
        if len(result['invalid_field_types']) >= 32:
            return
        expected = contract.get('type')
        if expected in types and not types[expected](value):
            result['invalid_field_types'].append({'field': field, 'expected': expected, 'observed': type(value).__name__})
            return
        if type(value) is dict:
            for key, child in contract.get('properties', {}).items():
                if key in value:
                    inspect(value[key], child, field + '.' + key)
        elif type(value) is list:
            child = contract.get('items', {})
            for i, item in enumerate(value[:64]):
                branches = child.get('oneOf', ())
                row = next((r for r in branches if type(item) is dict
                            and item.get('obligation_id') in r['properties']['obligation_id']['enum']), child)
                inspect(item, row, field + '[' + str(i) + ']')
    inspect(payload, schema, '$')
    return result


def _validate_normative_basis(frozen, rows):
    """Strict wire boundary; the assessor still derives support defensively."""
    if type(rows) is not list:
        raise ValueError('acceptance evidence must be an array')
    by_id = {o.obligation_id:o for o in frozen.obligations}
    for row in rows:
        if type(row) is not dict or type(row.get('obligation_id')) is not str:
            raise ValueError('invalid acceptance obligation identity')
        obligation = by_id.get(row['obligation_id'])
        if obligation is None:
            raise ValueError('unknown acceptance obligation identity')
        basis = row.get('expectation_basis')
        if type(basis) is not list or any(type(ref) is not str for ref in basis):
            raise ValueError('invalid normative expectation_basis')
        if not set(basis).issubset(independent_obligation_refs(frozen, obligation)):
            raise ValueError('cannot ACCEPT obligation-incompatible normative expectation_basis: '+obligation.obligation_id)
        if row.get('status') == 'SATISFIED':
            if not basis:
                raise ValueError('SATISFIED requires nonempty independent expectation_basis')
            if obligation.kind == 'VERIFICATION' and row.get('evidence_refs') != ['verification_report']:
                raise ValueError('SATISFIED verification requires verification_report as sole evidence ref')


def parse_judge_response(content: str, bundle: JudgeEvidenceBundle) -> JudgeAnalysis:
    if not isinstance(content, str) or not content.strip():
        raise JudgeProtocolError("judge response must be non-empty JSON text")
    if content.lstrip().startswith("```"):
        raise JudgeProtocolError("judge response must not use markdown fences")
    try:
        payload = decode_strict_json(content)
    except ValueError as exc:
        raise JudgeProtocolError(f"judge response is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise JudgeProtocolError("judge response must be a JSON object")
    expected = set(judge_required_keys(with_acceptance=bundle.acceptance_obligations is not None))
    if set(payload) != expected:
        raise JudgeProtocolError("judge response keys must match schema exactly")
    for key in ("deterministic_outcome", "decision", "summary", "next_action"):
        if not isinstance(payload.get(key), str):
            raise JudgeProtocolError(f"judge response {key} must be string")
    if not payload["summary"] or len(payload["summary"]) > 5000:
        raise JudgeProtocolError("judge summary length must be in [1, 5000]")
    confidence = payload.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise JudgeProtocolError("judge confidence must be numeric")
    evidence = payload.get("evidence")
    if not isinstance(evidence, list) or not (2 <= len(evidence) <= 3):
        raise JudgeProtocolError("judge evidence must contain 2..3 items")
    if any(not isinstance(item, str) or not item for item in evidence) or len(evidence) != len(set(evidence)):
        raise JudgeProtocolError("judge evidence IDs must be unique non-empty strings")
    allowed = bundle.allowed_evidence_ids
    unknown = sorted(set(evidence) - set(allowed))
    if unknown:
        raise JudgeProtocolError(f"judge cited unknown evidence IDs: {unknown}; allowed evidence IDs: {sorted(allowed)}")
    required_grounding = {"verification_report", "critic_report"}
    if not required_grounding.issubset(evidence):
        raise JudgeProtocolError("judge must cite verification_report and critic_report")
    guidance = payload.get("handoff_guidance")
    if not isinstance(guidance, list) or len(guidance) > 8:
        raise JudgeProtocolError("judge handoff_guidance must contain at most 8 items")
    if any(not isinstance(item, str) or not item or len(item) > 1200 for item in guidance) or len(guidance) != len(set(guidance)):
        raise JudgeProtocolError("judge handoff_guidance items must be unique non-empty strings up to 1200 chars")
    if payload.get("can_override_deterministic") is not False:
        raise JudgeProtocolError("judge can_override_deterministic must be false")
    try:
        # Support classification is harness-derived, never model supplied.
        base = {k:v for k,v in payload.items() if k != 'acceptance_evidence'}
        analysis = JudgeAnalysis.from_dict(base)
        if bundle.acceptance_obligations is not None:
            _validate_normative_basis(bundle.acceptance_obligations, payload['acceptance_evidence'])
            rows = assess_acceptance_rows(bundle.acceptance_obligations, payload['acceptance_evidence'])
            # A final disposition cannot erase disagreement from the isolated
            # public-contract comparison by citing passing downstream tests.
            isolated = {r.obligation_id:r for r in bundle.semantic_assessment}
            rows = tuple(replace(r,status=isolated[r.obligation_id].status)
                if r.obligation_id in isolated and isolated[r.obligation_id].status != 'SATISFIED'
                    and r.status == 'SATISFIED' else r for r in rows)
            if bundle.acceptance_obligations.unresolved_semantics:
                rows = tuple(replace(r, status='UNRESOLVED') for r in rows)
            analysis = replace(analysis, acceptance_evidence=rows)
    except (KeyError, TypeError, ValueError) as exc:
        raise JudgeProtocolError(f"invalid judge response: {exc}") from exc
    if analysis.deterministic_outcome is not bundle.deterministic_outcome:
        raise JudgeProtocolError("judge changed deterministic verification outcome")
    return analysis

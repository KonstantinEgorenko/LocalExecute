"""Request-derived structured-output schema for the Judge transport.

The strict parser remains the final semantic authority. This module only
narrows the already frozen schema to values present in the current request.
"""
from __future__ import annotations

from typing import Any

from ..protocol_transport import ollama_compatible_schema
from .models import JudgeEvidenceBundle
from .protocol import judge_required_keys, judge_response_schema, judge_schema_fingerprint


def judge_transport_schema(bundle: JudgeEvidenceBundle) -> dict[str, Any]:
    """Return an isolated schema constrained to the current evidence bundle."""

    return project_judge_contract(judge_response_schema(bundle))


def project_judge_contract(schema: dict[str, Any], fingerprint: str | None = None) -> dict[str, Any]:
    """Project the canonical contract without losing mandatory object fields."""
    if type(schema) is not dict or schema.get('type') != 'object' or schema.get('additionalProperties') is not False:
        raise ValueError('invalid Judge structured-output object contract')
    if fingerprint is not None and fingerprint != judge_schema_fingerprint(schema):
        raise ValueError('Judge schema fingerprint mismatch')
    required, properties = schema.get('required'), schema.get('properties')
    if type(required) is not list or type(properties) is not dict or set(required) != set(properties):
        raise ValueError('Judge contract requires every declared field')
    expected = set(judge_required_keys(with_acceptance='acceptance_evidence' in properties))
    if set(required) != expected:
        raise ValueError('Judge contract lost mandatory verdict fields')
    if 'oneOf' in schema or 'anyOf' in schema:
        raise ValueError('Judge contract cannot mix object properties with union branches')
    projected = ollama_compatible_schema(schema)
    def supported_arrays(value):
        if isinstance(value, dict):
            if value.get('type') == 'array' and 'allOf' in value:
                # contains/uniqueness require parser state and are not supported
                # by the Ollama/llama.cpp grammar subset. Do not send an array
                # intersection that can obscure its mandatory items grammar.
                clauses = value['allOf']
                if type(clauses) is not list or any(type(c) is not dict or 'contains' not in c
                        or set(c) - {'contains', 'minContains', 'maxContains'} for c in clauses):
                    raise ValueError('unsupported Judge array intersection')
                del value['allOf']
            for child in value.values():
                supported_arrays(child)
        elif isinstance(value, list):
            for child in value:
                supported_arrays(child)
    supported_arrays(projected)
    return projected

"""Deterministic prompt and wire schema for the Ornith Lead."""
from __future__ import annotations

import json

from ..router.profile import Complexity, ContextBucket, NeedLevel, Scope, TaskType, ValidationLevel


def _values(kind):
    return [item.value for item in kind]


def build_response_schema(config):
    """Return the exact JSON shape constrained at the Ollama generation boundary."""
    lead = config["lead"]
    string_list = {
        "type": "array",
        "items": {"type": "string", "minLength": 1},
        "uniqueItems": True,
    }
    context_estimate = {
        "type": "object",
        "additionalProperties": False,
        "required": ["estimated_tokens", "bucket"],
        "properties": {
            "estimated_tokens": {
                "anyOf": [
                    {"type": "integer", "minimum": 0},
                    {"type": "null"},
                ]
            },
            "bucket": {"type": "string", "enum": _values(ContextBucket)},
        },
    }
    routing = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "task_type",
            "complexity",
            "scope",
            "reasoning_need",
            "repo_exploration_need",
            "tool_depth",
            "context_estimate",
            "testability",
            "risk",
            "router_confidence",
        ],
        "properties": {
            "task_type": {"type": "string", "enum": _values(TaskType)},
            "complexity": {"type": "string", "enum": _values(Complexity)},
            "scope": {"type": "string", "enum": _values(Scope)},
            "reasoning_need": {"type": "string", "enum": _values(NeedLevel)},
            "repo_exploration_need": {"type": "string", "enum": _values(NeedLevel)},
            "tool_depth": {"type": "string", "enum": _values(NeedLevel)},
            "context_estimate": context_estimate,
            "testability": {"type": "string", "enum": _values(ValidationLevel)},
            "risk": {"type": "string", "enum": _values(NeedLevel)},
            "router_confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
    }
    todo = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "key",
            "objective",
            "dependencies",
            "priority",
            "constraints",
            "allowed_scope",
            "forbidden_scope",
            "required_evidence",
            "routing",
        ],
        "properties": {
            "key": {"type": "string", "minLength": 1},
            "objective": {"type": "string", "minLength": 1},
            "dependencies": {
                **string_list,
                "maxItems": lead["max_dependencies_per_todo"],
            },
            "priority": {"type": "integer", "minimum": 0},
            "constraints": {"type": "object"},
            "allowed_scope": string_list,
            "forbidden_scope": string_list,
            "required_evidence": string_list,
            "routing": routing,
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "disposition",
            "goal_summary",
            "assumptions",
            "attention_reason",
            "todos",
        ],
        "properties": {
            "schema_version": {"type": "integer", "const": lead["schema_version"]},
            "disposition": {
                "type": "string",
                "enum": ["PLAN", "NEEDS_ATTENTION"],
            },
            "goal_summary": {"type": "string", "minLength": 1},
            "assumptions": string_list,
            "attention_reason": {
                "anyOf": [
                    {"type": "string", "minLength": 1},
                    {"type": "null"},
                ]
            },
            "todos": {
                "type": "array",
                "items": todo,
                "maxItems": lead["max_todos"],
            },
        },
    }


def build_prompt(request, config):
    lead = config["lead"]
    context = json.dumps(
        request.to_dict(),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    enums = json.dumps(
        {
            "task_type": _values(TaskType),
            "complexity": _values(Complexity),
            "scope": _values(Scope),
            "need_level": _values(NeedLevel),
            "testability": _values(ValidationLevel),
            "context_bucket": _values(ContextBucket),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    plan_example = json.dumps(
        {
            "schema_version": lead["schema_version"],
            "disposition": "PLAN",
            "goal_summary": "Concise goal summary",
            "assumptions": [],
            "attention_reason": None,
            "todos": [
                {
                    "key": "T1",
                    "objective": "One bounded work item",
                    "dependencies": [],
                    "priority": 0,
                    "constraints": {},
                    "allowed_scope": [],
                    "forbidden_scope": [],
                    "required_evidence": [],
                    "routing": {
                        "task_type": "code",
                        "complexity": "trivial",
                        "scope": "local",
                        "reasoning_need": "low",
                        "repo_exploration_need": "low",
                        "tool_depth": "low",
                        "context_estimate": {
                            "estimated_tokens": 1000,
                            "bucket": "small",
                        },
                        "testability": "high",
                        "risk": "low",
                        "router_confidence": 0.9,
                    },
                }
            ],
        },
        separators=(",", ":"),
    )
    attention_example = json.dumps(
        {
            "schema_version": lead["schema_version"],
            "disposition": "NEEDS_ATTENTION",
            "goal_summary": "Concise goal summary",
            "assumptions": [],
            "attention_reason": "Critical missing information prevents a safe plan",
            "todos": [],
        },
        separators=(",", ":"),
    )
    return (
        "You are Ornith Lead. Perform decomposition only. "
        "Return exactly one JSON object matching the runtime-supplied JSON Schema; "
        "no Markdown, prose, chain-of-thought, or extra keys. "
        "PLAN and NEEDS_ATTENTION are values of the string field `disposition`; "
        "they are never JSON object keys. "
        "Use PLAN whenever the objective can be safely decomposed. "
        "Use NEEDS_ATTENTION only when critical missing or ambiguous information "
        "prevents a safe decomposition. An instruction such as `do not execute` "
        "does NOT require attention: Lead never executes work. "
        f"PLAN must contain 1..{lead['max_todos']} DAG todos; NEEDS_ATTENTION must "
        "contain zero todos and a non-empty attention_reason. "
        "For PLAN set attention_reason to null. "
        "Todo `priority` MUST be a non-negative JSON integer; use 0 when no "
        "explicit relative priority is required. "
        "`reasoning_need`, `repo_exploration_need`, `tool_depth`, and `risk` "
        "MUST use only need_level enum values; words such as `minimal` are invalid. "
        "`allowed_scope` and `forbidden_scope` are WorkContract scope/path "
        "restrictions, not routing scope labels; use [] when no such restriction "
        "is needed. Parent request bounds are authoritative and must not be "
        "contradicted. "
        "Never select a model, fabricate RoutingDecision, route or execute workers, "
        "call tools, edit files, run tests, or claim execution occurred. "
        "Do not include selected_model, scores, resource state, failures, "
        "escalation, or execution decisions. "
        f"Maximum dependencies per Todo={lead['max_dependencies_per_todo']}. "
        f"Exact enum values={enums}. "
        f"Canonical PLAN example={plan_example}. "
        f"Canonical NEEDS_ATTENTION example={attention_example}. "
        f"Request={context}"
    )


def build_repair_prompt(request, invalid, errors, config):
    return (
        build_prompt(request, config)
        + " The previous response was rejected. Repair it without changing the "
        "requested objective. Return corrected canonical JSON only. Errors="
        + json.dumps(errors, ensure_ascii=False)
        + " Invalid="
        + invalid
    )

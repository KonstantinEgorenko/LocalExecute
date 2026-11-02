import json

import pytest

from harness.agent_loop import AgentActionKind, AgentProtocolError, parse_agent_action
from harness.workspace import ToolName


def test_tool_action_parses_strict_canonical_json():
    value = parse_agent_action(
        json.dumps(
            {
                "schema_version": 1,
                "action": "TOOL",
                "tool_call": {"tool": "READ_TEXT", "arguments": {"path": "a.py"}},
                "final": None,
            }
        )
    )
    assert value.action is AgentActionKind.TOOL
    assert value.tool_call.tool is ToolName.READ_TEXT
    assert dict(value.tool_call.arguments) == {"path": "a.py"}


def test_final_action_parses_strict_canonical_json():
    value = parse_agent_action(
        '{"schema_version":1,"action":"FINAL","tool_call":null,"final":{"summary":"done"}}'
    )
    assert value.action is AgentActionKind.FINAL
    assert value.final.summary == "done"


@pytest.mark.parametrize(
    "raw",
    [
        "```json\n{}\n```",
        '{"schema_version":1,"FINAL":{"summary":"done"}}',
        '{"schema_version":1,"action":"FINAL","tool_call":null,"final":{"summary":"done"},"extra":1}',
        '{"schema_version":2,"action":"FINAL","tool_call":null,"final":{"summary":"done"}}',
        '{"schema_version":1,"action":"TOOL","tool_call":{"tool":"NOPE","arguments":{}},"final":null}',
        '{"schema_version":1,"action":"TOOL","tool_call":{"tool":"READ_TEXT","arguments":{}},"final":{"summary":"bad"}}',
        '{"schema_version":1,"action":"FINAL","tool_call":{"tool":"READ_TEXT","arguments":{}},"final":{"summary":"bad"}}',
    ],
)
def test_protocol_rejects_noncanonical_responses(raw):
    with pytest.raises(AgentProtocolError):
        parse_agent_action(raw)


def test_tool_arguments_reject_nonfinite_json_values():
    raw = '{"schema_version":1,"action":"TOOL","tool_call":{"tool":"RUN_COMMAND","arguments":{"timeout_seconds":NaN}},"final":null}'
    with pytest.raises(AgentProtocolError):
        parse_agent_action(raw)


@pytest.mark.parametrize("version", ["true", "false", '"1"', "1.0", "null"])
def test_schema_version_requires_integer_one(version):
    raw = '{"schema_version":' + version + ',"action":"FINAL","tool_call":null,"final":{"summary":"done"}}'
    with pytest.raises(AgentProtocolError, match="schema_version"):
        parse_agent_action(raw)


@pytest.mark.parametrize("raw", [
    '{"schema_version":1,"schema_version":1,"action":"FINAL","tool_call":null,"final":{"summary":"done"}}',
    '{"schema_version":1,"action":"TOOL","tool_call":{"tool":"READ_TEXT","arguments":{"path":"a.py","path":"b.py"}},"final":null}',
])
def test_duplicate_keys_are_rejected_at_any_depth(raw):
    with pytest.raises(AgentProtocolError, match="duplicate JSON key"):
        parse_agent_action(raw)


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_nonstandard_json_constants_are_rejected_during_decoding(literal):
    raw = '{"schema_version":1,"action":"TOOL","tool_call":{"tool":"READ_TEXT","arguments":{"nested":{"value":' + literal + '}}},"final":null}'
    with pytest.raises(AgentProtocolError, match="non-finite JSON literal"):
        parse_agent_action(raw)

"""Backend-compatible JSON Schema projection for Ollama structured output.

The local parsers retain the full semantic contract.  The transport projection
removes keywords rejected by the deployed grammar backend while preserving
object shape, required keys, primitive types, enums and array cardinality.
"""
from __future__ import annotations

from typing import Any


_PARSER_ONLY_KEYWORDS = frozenset({
    "uniqueItems",
    "minLength",
    "maxLength",
    "minimum",
    "maximum",
})


def ollama_compatible_schema(value: Any) -> Any:
    if isinstance(value, list):
        return [ollama_compatible_schema(item) for item in value]
    if not isinstance(value, dict):
        return value

    projected = {
        key: ollama_compatible_schema(item)
        for key, item in value.items()
        if key not in _PARSER_ONLY_KEYWORDS and key != "const"
    }
    if "const" in value:
        projected["enum"] = [ollama_compatible_schema(value["const"])]
    return projected

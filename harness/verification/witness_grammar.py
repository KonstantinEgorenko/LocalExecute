"""Witness-only grammar projection and conversion-only local qualification.

The authoritative schema/parser are unchanged. Installed llama.cpp parses the
schema option before --help exits: neither a model path nor generation is used.
This proves conversion, not model quality or a hard GPU allocation cap.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess

from ..protocol_transport import ollama_compatible_schema

PROJECTION_REVISION = 'PUBLIC_WITNESS_PREFIX_TUPLE_V1'
MAX_SCHEMA_BYTES = 65536


def schema_bytes(schema):
    value = json.dumps(schema, ensure_ascii=False, allow_nan=False,
                       separators=(',', ':')).encode('utf-8')
    if len(value) > MAX_SCHEMA_BYTES:
        raise ValueError('PUBLIC_WITNESS_SCHEMA_TOO_LARGE')
    return value


def project_witness_schema(schema):
    """Remove only redundant items:false on an exactly bounded typed tuple.

    The installed converter gives items precedence over prefixItems. With
    items absent, its prefixItems branch emits exactly those positional rules.
    min/maxItems and all positional types remain; host validation retains ASCII
    and scalar bounds already enforced by the production semantic parser.
    Unsupported tuple shapes fail rather than silently widening the grammar.
    """
    value = deepcopy(ollama_compatible_schema(schema))

    def visit(node):
        if isinstance(node, list):
            for child in node:
                visit(child)
        elif isinstance(node, dict):
            if 'prefixItems' in node and node.get('items') is False:
                prefix = node['prefixItems']
                if (node.get('type') != 'array' or type(prefix) is not list
                        or not 1 <= len(prefix) <= 16
                        or type(node.get('minItems')) is not int
                        or type(node.get('maxItems')) is not int
                        or node['minItems'] != len(prefix)
                        or node['maxItems'] != len(prefix)
                        or any(type(child) is not dict for child in prefix)):
                    raise ValueError('PUBLIC_WITNESS_TUPLE_PROJECTION_UNSUPPORTED')
                del node['items']
            for child in node.values():
                visit(child)
    visit(value)
    schema_bytes(value)
    return value


class LocalWitnessGrammarCompiler:
    """A bounded installed-binary compiler, never a model transport.

    No success is inferred from a mock model response. Callers freeze binary
    fingerprints separately from source; drift during qualification fails.
    The CLI does not expose generated grammar bytes, so no grammar hash is
    invented from its help text.
    """
    def __init__(self, runner=None):
        self.runner = Path(runner) if runner else (
            Path.home() / 'AppData/Local/Programs/Ollama/lib/ollama/llama-server.exe')
        self.library = self.runner.parent / 'libllama-common.dll'

    def _identity(self):
        try:
            return {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in (self.runner, self.library)}
        except OSError as exc:
            raise ValueError('PUBLIC_WITNESS_GRAMMAR_COMPILER_UNAVAILABLE') from exc

    def qualify(self, authoritative_schema):
        projected = project_witness_schema(authoritative_schema)
        encoded = schema_bytes(projected)
        # Windows argv is bounded more tightly than the protocol schema budget.
        if len(encoded) > 12000:
            raise ValueError('PUBLIC_WITNESS_GRAMMAR_COMPILER_ARG_LIMIT')
        binaries = self._identity()
        try:
            version = subprocess.run([str(self.runner), '--version'],
                capture_output=True, timeout=10, check=False)
            result = subprocess.run([str(self.runner), '--json-schema', encoded.decode('utf-8'), '--help'],
                capture_output=True, timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError('PUBLIC_WITNESS_GRAMMAR_COMPILER_UNAVAILABLE') from exc
        if self._identity() != binaries:
            raise ValueError('PUBLIC_WITNESS_GRAMMAR_COMPILER_DRIFT')
        if (version.returncode != 0 or result.returncode != 0
                or b'common params' not in result.stdout):
            from ..transport_diagnostics import safe_text
            raise ValueError('PUBLIC_WITNESS_BACKEND_GRAMMAR_INCOMPATIBLE: ' +
                             safe_text(result.stderr.decode('utf-8', errors='replace')))
        return {'status': 'NATIVE_COMPILER_PROVEN', 'projection_revision': PROJECTION_REVISION,
            'authoritative_schema_sha256': hashlib.sha256(schema_bytes(authoritative_schema)).hexdigest(),
            'backend_schema_sha256': hashlib.sha256(encoded).hexdigest(),
            'compiler_binaries': binaries,
            'compiler_build': (version.stdout + version.stderr).decode('utf-8', errors='replace').strip(),
            'generated_grammar_sha256': None, 'grammar_export': 'NOT_EXPOSED_BY_INSTALLED_CLI',
            'real_preloads': 0, 'real_generations': 0}

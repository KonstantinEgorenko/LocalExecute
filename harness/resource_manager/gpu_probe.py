"""Bounded GPU subprocess evidence and the Windows NVML environment prerequisite."""
from __future__ import annotations

import ctypes
from dataclasses import dataclass
from enum import Enum
import os
from pathlib import Path

from ..errors import ResourceDiscoveryError

OUTPUT_LIMIT = 8192

class GpuProbeFailure(str, Enum):
    EXECUTABLE_NOT_FOUND = 'EXECUTABLE_NOT_FOUND'
    EXECUTION_PERMISSION_DENIED = 'EXECUTION_PERMISSION_DENIED'
    PROCESS_CREATION_FAILED = 'PROCESS_CREATION_FAILED'
    PROCESS_EXIT_NONZERO = 'PROCESS_EXIT_NONZERO'
    PROBE_TIMEOUT = 'PROBE_TIMEOUT'
    OUTPUT_DECODE_FAILED = 'OUTPUT_DECODE_FAILED'
    OUTPUT_PARSE_FAILED = 'OUTPUT_PARSE_FAILED'
    GPU_NOT_DETECTED = 'GPU_NOT_DETECTED'
    ENVIRONMENT_UNAVAILABLE = 'ENVIRONMENT_UNAVAILABLE'

@dataclass(frozen=True, slots=True)
class GpuProbeEvidence:
    probe_id: str
    timestamp: str
    invocation_context: str
    executable: str | None
    executable_sha256: str | None
    environment_fingerprint: str
    environment_added_keys: tuple[str, ...]
    returncode: int | None = None
    timeout: bool = False
    exception_category: str | None = None
    stdout: str = ''
    stderr: str = ''
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    parse_status: str = 'NOT_RUN'
    gpu_inventory_available: bool = False
    failure_classification: GpuProbeFailure | None = None

class GpuProbeError(ResourceDiscoveryError):
    def __init__(self, message: str, evidence: GpuProbeEvidence):
        super().__init__(message)
        self.evidence = evidence

def bounded_output(value: str | bytes | None) -> tuple[str, bool]:
    raw = value if isinstance(value, bytes) else (value or '').encode('utf-8', 'replace')
    text = raw[:OUTPUT_LIMIT].decode('utf-8', 'replace')
    return text.encode('utf-8')[:OUTPUT_LIMIT].decode('utf-8', 'ignore'), len(raw) > OUTPUT_LIMIT

def windows_program_files() -> str:
    """OS-owned known folder, not a hardcoded path or an inherited PATH entry."""
    buffer = ctypes.create_unicode_buffer(32768)
    api = ctypes.WinDLL('shell32', use_last_error=True).SHGetFolderPathW
    api.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint, ctypes.c_wchar_p]
    api.restype = ctypes.c_long
    if api(None, 0x26, None, 0, buffer) != 0:  # CSIDL_PROGRAM_FILES
        raise OSError('Windows Program Files known folder unavailable')
    path = Path(buffer.value)
    if not path.is_absolute() or not path.is_dir():
        raise OSError('Windows Program Files known folder is invalid')
    return str(path)

def gpu_subprocess_environment(environment: dict[str, str]) -> tuple[dict[str, str], tuple[str, ...]]:
    """Keep replacement semantics; supply only the proven missing NVML prerequisite.

    The parent environment is never modified. Existing explicit values are not
    overridden. No PATH expansion, model configuration or resource policy change.
    """
    result = dict(environment)
    keys = {key.upper() for key in result}
    if os.name == 'nt' and not keys.intersection({'PROGRAMFILES', 'PROGRAMW6432'}):
        result['ProgramFiles'] = windows_program_files()
        return result, ('ProgramFiles',)
    return result, ()

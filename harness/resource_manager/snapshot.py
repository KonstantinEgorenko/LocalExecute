"""Read-only system and GPU discovery plus serializable resource snapshots."""
from __future__ import annotations

import ctypes
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from typing import Callable, Sequence

from ..errors import ResourceDiscoveryError
from .gpu_probe import (GpuProbeEvidence, GpuProbeError, GpuProbeFailure,
                        bounded_output, gpu_subprocess_environment)


@dataclass(frozen=True, slots=True)
class RamSnapshot:
    total_gb: float
    used_gb: float
    available_gb: float


@dataclass(frozen=True, slots=True)
class GpuSnapshot:
    name: str
    vram_total_gb: float
    vram_used_gb: float
    vram_free_gb: float
    utilization_percent: float | None
    probe: GpuProbeEvidence | None = None


@dataclass(frozen=True, slots=True)
class ResourceSnapshot:
    timestamp: str
    ram: RamSnapshot | None
    gpu: GpuSnapshot | None
    lead_online: bool
    worker_online: bool
    model_statuses: tuple[object, ...]
    gpu_worker_lock: object
    cpu_bandwidth_lock: object
    discovery_errors: tuple[str, ...] = ()
    gpu_probe: GpuProbeEvidence | None = None

    def to_dict(self) -> dict[str, object]:
        def encode(value: object) -> object:
            if hasattr(value, "value"):
                return getattr(value, "value")
            if hasattr(value, "__dataclass_fields__"):
                return {key: encode(item) for key, item in asdict(value).items()}
            if isinstance(value, tuple):
                return [encode(item) for item in value]
            if isinstance(value, list):
                return [encode(item) for item in value]
            if isinstance(value, dict):
                return {str(key): encode(item) for key, item in value.items()}
            return value
        return encode(self)  # type: ignore[return-value]


def _gb(value: int | float) -> float:
    return round(float(value) / 1024**3, 3)


def discover_ram() -> RamSnapshot:
    if not hasattr(ctypes, "windll"):
        raise ResourceDiscoveryError("Windows RAM discovery requires Windows")

    class MemoryStatus(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                   ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                   ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                   ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                   ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

    status = MemoryStatus()
    status.dwLength = ctypes.sizeof(MemoryStatus)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise ResourceDiscoveryError("GlobalMemoryStatusEx failed")
    return RamSnapshot(_gb(status.ullTotalPhys), _gb(status.ullTotalPhys - status.ullAvailPhys), _gb(status.ullAvailPhys))


def parse_nvidia_smi_csv(output: str) -> GpuSnapshot:
    rows = [line.strip() for line in output.splitlines() if line.strip()]
    if not rows:
        raise ResourceDiscoveryError("No GPU devices in nvidia-smi output")
    snapshots = []
    for row in rows:
        parts = [part.strip() for part in row.split(",")]
        if len(parts) != 5 or not parts[0]:
            raise ResourceDiscoveryError("Unexpected nvidia-smi CSV output")
        try:
            total, used, free = (float(value) for value in parts[1:4])
            utilization = None if parts[4].upper() in {"N/A", ""} else float(parts[4])
        except ValueError as exc:
            raise ResourceDiscoveryError("Invalid nvidia-smi numeric value") from exc
        if (not all(math.isfinite(v) for v in (total, used, free)) or total <= 0
                or used < 0 or free < 0 or used + free > total + 1
                or (utilization is not None and (not math.isfinite(utilization) or not 0 <= utilization <= 100))):
            raise ResourceDiscoveryError("Invalid nvidia-smi memory/utilization bounds")
        snapshots.append(GpuSnapshot(parts[0], round(total / 1024, 3), round(used / 1024, 3), round(free / 1024, 3), utilization))
    # Preserve the existing first-device policy, never sum unrelated GPUs.
    return snapshots[0]


def discover_gpu(command_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> GpuSnapshot:
    command: Sequence[str] = (
        "nvidia-smi", "--query-gpu=name,memory.total,memory.used,memory.free,utilization.gpu",
        "--format=csv,noheader,nounits",
    )
    resolved = shutil.which(command[0])
    identity = None
    if resolved:
        try:
            identity = hashlib.sha256(Path(resolved).read_bytes()).hexdigest()
        except OSError:
            pass  # Identity observation failure cannot create an admitted GPU.
    evidence = GpuProbeEvidence(uuid.uuid4().hex, now_utc(), f'pid:{os.getpid()}',
        resolved, identity, '', ())

    def fail(category, message, **fields):
        receipt = replace(evidence, failure_classification=category, **fields)
        raise GpuProbeError(message, receipt)

    try:
        environment, added = gpu_subprocess_environment(dict(os.environ))
    except OSError as exc:
        fail(GpuProbeFailure.ENVIRONMENT_UNAVAILABLE, 'GPU probe environment unavailable', exception_category=type(exc).__name__)
    evidence = replace(evidence, environment_added_keys=added,
        environment_fingerprint=hashlib.sha256(json.dumps(environment, sort_keys=True).encode('utf-8')).hexdigest())
    try:
        result = command_runner(command, capture_output=True, text=True, timeout=5, check=False, env=environment)
    except subprocess.TimeoutExpired as exc:
        stdout, stdout_truncated = bounded_output(exc.stdout)
        stderr, stderr_truncated = bounded_output(exc.stderr)
        fail(GpuProbeFailure.PROBE_TIMEOUT, 'nvidia-smi timed out', timeout=True,
             exception_category=type(exc).__name__, stdout=stdout, stderr=stderr,
             stdout_truncated=stdout_truncated, stderr_truncated=stderr_truncated)
    except UnicodeError as exc:
        fail(GpuProbeFailure.OUTPUT_DECODE_FAILED, 'nvidia-smi output decode failed', exception_category=type(exc).__name__)
    except FileNotFoundError as exc:
        fail(GpuProbeFailure.EXECUTABLE_NOT_FOUND, 'nvidia-smi was not found', exception_category=type(exc).__name__)
    except PermissionError as exc:
        fail(GpuProbeFailure.EXECUTION_PERMISSION_DENIED, 'nvidia-smi permission denied', exception_category=type(exc).__name__)
    except OSError as exc:
        fail(GpuProbeFailure.PROCESS_CREATION_FAILED, 'nvidia-smi process creation failed', exception_category=type(exc).__name__)
    stdout, stdout_truncated = bounded_output(result.stdout)
    stderr, stderr_truncated = bounded_output(result.stderr)
    evidence = replace(evidence, returncode=result.returncode, stdout=stdout, stderr=stderr,
        stdout_truncated=stdout_truncated, stderr_truncated=stderr_truncated)
    if result.returncode != 0:
        fail(GpuProbeFailure.PROCESS_EXIT_NONZERO,
             f'nvidia-smi failed: exit={result.returncode}; stdout={stdout!r}; stderr={stderr!r}')
    try:
        gpu = parse_nvidia_smi_csv(result.stdout)
    except ResourceDiscoveryError as exc:
        fail(GpuProbeFailure.GPU_NOT_DETECTED if not result.stdout.strip() else GpuProbeFailure.OUTPUT_PARSE_FAILED,
             str(exc), parse_status='FAIL', exception_category=type(exc).__name__)
    return replace(gpu, probe=replace(evidence, parse_status='PASS', gpu_inventory_available=True))


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()

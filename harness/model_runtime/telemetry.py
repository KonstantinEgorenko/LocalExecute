'Read-only RAM/VRAM/Ollama telemetry and durable model runtime event reporting.\n\nmodel runtime intentionally has no third-party telemetry dependency. Host RAM is\nsampled with Python stdlib APIs so a clean Swarm venv does not need psutil.\n'
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class MemoryTelemetry:
    ram_available_mib: float
    ram_used_mib: float
    ram_total_mib: float
    vram_used_mib: float | None
    vram_free_mib: float | None
    vram_total_mib: float | None


def _mib(value: int | float) -> float:
    return float(value) / (1024.0 * 1024.0)


def _windows_memory_bytes() -> tuple[int, int]:
    """Return (total_phys_bytes, available_phys_bytes) via GlobalMemoryStatusEx."""

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ok = kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
    if not ok:
        raise OSError(ctypes.get_last_error(), "GlobalMemoryStatusEx failed")
    return int(status.ullTotalPhys), int(status.ullAvailPhys)


def _proc_meminfo_bytes(path: str | Path = "/proc/meminfo") -> tuple[int, int]:
    values: dict[str, int] = {}
    with Path(path).open("r", encoding="utf-8") as f:
        for line in f:
            if ":" not in line:
                continue
            key, rest = line.split(":", 1)
            parts = rest.strip().split()
            if not parts:
                continue
            try:
                amount = int(parts[0])
            except ValueError:
                continue
            multiplier = 1024 if len(parts) > 1 and parts[1].lower() == "kb" else 1
            values[key] = amount * multiplier

    total = values.get("MemTotal")
    available = values.get("MemAvailable")
    if available is None and total is not None:
        # Conservative fallback for older kernels without MemAvailable.
        available = max(
            0,
            values.get("MemFree", 0)
            + values.get("Buffers", 0)
            + values.get("Cached", 0)
            + values.get("SReclaimable", 0)
            - values.get("Shmem", 0),
        )
    if total is None or available is None:
        raise RuntimeError("/proc/meminfo is missing MemTotal/MemAvailable")
    return int(total), int(available)


def _sysconf_memory_bytes() -> tuple[int, int]:
    page_size = int(os.sysconf("SC_PAGE_SIZE"))
    total_pages = int(os.sysconf("SC_PHYS_PAGES"))
    available_pages = int(os.sysconf("SC_AVPHYS_PAGES"))
    if page_size <= 0 or total_pages <= 0 or available_pages < 0:
        raise RuntimeError("sysconf returned invalid physical-memory counters")
    return total_pages * page_size, available_pages * page_size


def _ram_memory_bytes() -> tuple[int, int]:
    if os.name == "nt":
        return _windows_memory_bytes()
    proc = Path("/proc/meminfo")
    if proc.is_file():
        try:
            return _proc_meminfo_bytes(proc)
        except (OSError, RuntimeError):
            pass
    try:
        return _sysconf_memory_bytes()
    except (AttributeError, OSError, ValueError, RuntimeError) as exc:
        raise RuntimeError("Unable to sample host RAM with stdlib telemetry") from exc


def _parse_optional_number(raw: str) -> float | None:
    text = raw.strip()
    normalized = text.upper().replace("[", "").replace("]", "").strip()
    if not normalized or normalized in {"N/A", "NA", "-", "NOT SUPPORTED", "NOT_SUPPORTED"}:
        return None
    try:
        import math
        value = float(text)
        return value if math.isfinite(value) and value >= 0 else None
    except ValueError:
        return None


class MemoryProbe:
    def sample(self) -> MemoryTelemetry:
        ram_total, ram_available = _ram_memory_bytes()
        ram_used = max(0, ram_total - ram_available)

        vram_used = vram_total = vram_free = None
        try:
            cp = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=memory.used,memory.total,memory.free",
                    "--format=csv,noheader,nounits",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if cp.returncode == 0:
                row = next((x.strip() for x in cp.stdout.splitlines() if x.strip()), "")
                vals = [x.strip() for x in row.split(",")]
                if len(vals) >= 2:
                    vram_used = _parse_optional_number(vals[0])
                    vram_total = _parse_optional_number(vals[1])
                    if len(vals) >= 3:
                        vram_free = _parse_optional_number(vals[2])
        except OSError:
            pass

        return MemoryTelemetry(
            ram_available_mib=_mib(ram_available),
            ram_used_mib=_mib(ram_used),
            ram_total_mib=_mib(ram_total),
            vram_used_mib=vram_used,
            vram_free_mib=vram_free,
            vram_total_mib=vram_total,
        )


class RuntimeJournal:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.dir = self.root / "state" / "evaluation" / "phase20v"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.dir / "runtime_events.jsonl"
        self.snapshot_path = self.dir / "runtime_snapshot.json"
        self._guard = threading.Lock()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def event(self, kind: str, **payload: Any) -> None:
        row = {"schema_version": 1, "timestamp": self._now(), "kind": kind, **payload}
        with self._guard:
            with self.events_path.open("a", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    def snapshot(self, payload: dict[str, Any]) -> None:
        body = {"schema_version": 1, "timestamp": self._now(), **payload}
        with self._guard:
            fd, tmp = tempfile.mkstemp(
                prefix=self.snapshot_path.name + ".", suffix=".tmp", dir=self.dir
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                    json.dump(body, f, ensure_ascii=False, sort_keys=True, indent=2)
                    f.write("\n")
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.snapshot_path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)


def process_gpu_telemetry() -> dict[str, Any]:
    """Read-only support probe; N/A is normal on Windows WDDM.

    A single process sample cannot establish peak or map every allocation to
    model residency. Consumers must not attribute unrelated processes to Swarm.
    """
    argv = ["nvidia-smi", "--query-compute-apps=pid,process_name,used_gpu_memory", "--format=csv,noheader,nounits"]
    try:
        cp = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10, check=False)
        rows = []
        for line in cp.stdout.splitlines():
            fields = [x.strip() for x in line.split(",")]
            if len(fields) >= 3:
                rows.append(dict(pid=fields[0], process=fields[1], allocation_mib=_parse_optional_number(fields[2])))
        return dict(argv=argv, returncode=cp.returncode, rows=rows, raw_stdout=cp.stdout[-32768:], raw_stderr=cp.stderr[-32768:],
                    process_allocation_supported=bool(rows) and all(r["allocation_mib"] is not None for r in rows),
                    model_peak_allocation="NOT_PROVEN", hard_peak_cap="NOT_PROVEN")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return dict(argv=argv, error=str(exc), process_allocation_supported=False, model_peak_allocation="NOT_PROVEN", hard_peak_cap="NOT_PROVEN")

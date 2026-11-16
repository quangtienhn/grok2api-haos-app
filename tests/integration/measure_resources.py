#!/usr/bin/env python3
"""Measure startup time, memory and CPU of the real Grok2API binary.

This is a developer-machine measurement (Windows amd64). It is NOT a Home
Assistant OS / Intel N150 measurement and must be reported as such.

    python tests/integration/measure_resources.py
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import statistics
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from run_integration import Instance, _b64_32b  # noqa: E402

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_VM_READ = 0x0010


class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", wt.DWORD),
        ("PageFaultCount", wt.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wt.DWORD), ("dwHighDateTime", wt.DWORD)]


def _to_int(low: int, high: int) -> int:
    return (high << 32) | low


def sample(pid: int) -> dict[str, float]:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ, False, pid)
    if not handle:
        raise RuntimeError(f"OpenProcess failed: {ctypes.get_last_error()}")
    try:
        counters = PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            raise RuntimeError(f"GetProcessMemoryInfo failed: {ctypes.get_last_error()}")
        created, exited, kernel, user = FILETIME(), FILETIME(), FILETIME(), FILETIME()
        if not kernel32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                        ctypes.byref(kernel), ctypes.byref(user)):
            raise RuntimeError(f"GetProcessTimes failed: {ctypes.get_last_error()}")
        cpu = (_to_int(kernel.dwLowDateTime, kernel.dwHighDateTime)
               + _to_int(user.dwLowDateTime, user.dwHighDateTime)) / 1e7
        return {
            "working_set_mb": round(counters.WorkingSetSize / (1024 * 1024), 1),
            "peak_working_set_mb": round(counters.PeakWorkingSetSize / (1024 * 1024), 1),
            "private_mb": round(counters.PagefileUsage / (1024 * 1024), 1),
            "cpu_seconds": round(cpu, 2),
        }
    finally:
        kernel32.CloseHandle(handle)


def main() -> int:
    import tempfile
    import shutil

    binary = Path(__file__).resolve().parents[2] / ".build/grok2api-windows-amd64.exe"
    if not binary.exists():
        print(f"binary not found: {binary}", file=sys.stderr)
        return 2

    results: dict[str, object] = {"binary": str(binary), "binary_bytes": binary.stat().st_size,
                                 "runs": []}
    startup_times = []
    for run in range(3):
        workdir = Path(tempfile.mkdtemp(prefix="grok2api-bench-",
                                       dir=Path(tempfile.gettempdir()) / "opencode"))
        inst = Instance(
            workdir=workdir,
            binary=binary,
            admin_user="admin",
            admin_password="Bench-" + uuid.uuid4().hex[:12],
            extra_secrets={"jwt": uuid.uuid4().hex * 2, "enc": _b64_32b()},
        )
        try:
            started = time.perf_counter()
            inst.start(timeout=180)
            startup = time.perf_counter() - started
            startup_times.append(round(startup, 2))
            samples = []
            for delay in (5, 30, 60):
                time.sleep(delay if not samples else delay - samples[-1]["at_seconds"])
                point = sample(inst.process.pid)
                point["at_seconds"] = delay
                samples.append(point)
            results["runs"].append({"startup_seconds": round(startup, 2), "samples": samples})
        finally:
            inst.stop_graceful()
            shutil.rmtree(workdir, ignore_errors=True)

    results["startup_seconds"] = {
        "runs": startup_times,
        "median": round(statistics.median(startup_times), 2),
    }
    idle = [run["samples"][-1] for run in results["runs"]]
    results["idle_60s"] = {
        "working_set_mb": round(statistics.mean(s["working_set_mb"] for s in idle), 1),
        "private_mb": round(statistics.mean(s["private_mb"] for s in idle), 1),
        "cpu_seconds": round(statistics.mean(s["cpu_seconds"] for s in idle), 2),
    }
    print(json.dumps(results, indent=2))
    out = Path(__file__).resolve().parent / "resources.json"
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nwritten: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

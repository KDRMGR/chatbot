"""CPU, RAM, GPU and VRAM readings for the dashboard overview.

Windows APIs through ctypes (psutil is a native extension that Smart App Control may block on this PC); the GPU comes from
``nvidia-smi``, which ships with the graphics driver. Every reader is injectable so the logic is testable without hardware, and a
missing reader (no NVIDIA GPU, no nvidia-smi, not Windows) simply yields ``None`` instead of an error.
"""
from __future__ import annotations

import asyncio
import ctypes
import logging
import os
import shutil
import subprocess
import sys
import time
from ctypes import wintypes as wt
from typing import Any, Callable

logger = logging.getLogger(__name__)

_WINDOWS = sys.platform == "win32"
_NVIDIA_QUERY = "name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw"


class _MemoryStatus(ctypes.Structure):
    _fields_ = [("dwLength", wt.DWORD), ("dwMemoryLoad", wt.DWORD), ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong), ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong), ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong), ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


class _ProcessMemory(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD), ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t), ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t), ("PrivateUsage", ctypes.c_size_t)]


def read_memory() -> dict[str, float] | None:
    """Physical memory of the whole PC, in MB."""
    if not _WINDOWS:
        return None
    status = _MemoryStatus()
    status.dwLength = ctypes.sizeof(status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    total, free = status.ullTotalPhys / 1048576, status.ullAvailPhys / 1048576
    return {"total_mb": round(total), "used_mb": round(total - free), "percent": float(status.dwMemoryLoad)}


def read_cpu_times() -> tuple[int, int, int] | None:
    """(idle, kernel, user) in 100 ns ticks since boot; kernel time includes idle time."""
    if not _WINDOWS:
        return None
    idle, kernel, user = (wt.FILETIME() for _ in range(3))
    if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
        return None
    as_int = lambda ft: (ft.dwHighDateTime << 32) | ft.dwLowDateTime  # noqa: E731
    return as_int(idle), as_int(kernel), as_int(user)


def cpu_percent(previous: tuple[int, int, int], current: tuple[int, int, int]) -> float | None:
    idle = current[0] - previous[0]
    total = (current[1] - previous[1]) + (current[2] - previous[2])
    if total <= 0:
        return None
    return round(max(0.0, min(100.0, 100.0 * (total - idle) / total)), 1)


def read_process() -> dict[str, float] | None:
    """Memory of THIS process (the bot), in MB."""
    if not _WINDOWS:
        return None
    kernel32 = ctypes.windll.kernel32
    kernel32.GetCurrentProcess.restype = wt.HANDLE
    info = _ProcessMemory()
    info.cb = ctypes.sizeof(info)
    kernel32.K32GetProcessMemoryInfo.argtypes = [wt.HANDLE, ctypes.POINTER(_ProcessMemory), wt.DWORD]
    if not kernel32.K32GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(info), info.cb):
        return None
    return {"private_mb": round(info.PrivateUsage / 1048576, 1), "working_set_mb": round(info.WorkingSetSize / 1048576, 1)}


def parse_nvidia_smi(text: str) -> dict[str, Any] | None:
    """Parse one line of ``nvidia-smi --query-gpu=... --format=csv,noheader,nounits`` (the first GPU). "[N/A]" becomes None."""
    for line in text.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 6:
            continue

        def number(value: str) -> float | None:
            try:
                return float(value)
            except ValueError:
                return None

        return {"available": True, "name": parts[0], "utilization_percent": number(parts[1]), "vram_used_mb": number(parts[2]),
                "vram_total_mb": number(parts[3]), "temperature_c": number(parts[4]), "power_w": number(parts[5])}
    return None


def run_nvidia_smi(timeout: float = 4.0) -> str | None:
    exe = shutil.which("nvidia-smi")
    if exe is None:
        return None
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        done = subprocess.run([exe, f"--query-gpu={_NVIDIA_QUERY}", "--format=csv,noheader,nounits"], capture_output=True,
                              text=True, timeout=timeout, creationflags=flags, env={**os.environ})
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


class SystemStats:
    """Cached readings: asking the dashboard's 2-second refresh for a fresh GPU query each time would be wasteful."""

    def __init__(
        self, *, clock: Callable[[], float] = time.monotonic, read_cpu: Callable = read_cpu_times, read_mem: Callable = read_memory,
        read_proc: Callable = read_process, run_gpu: Callable = run_nvidia_smi, ttl_s: float = 2.0, gpu_ttl_s: float = 5.0,
    ) -> None:
        self._clock, self._read_cpu, self._read_mem, self._read_proc, self._run_gpu = clock, read_cpu, read_mem, read_proc, run_gpu
        self._ttl, self._gpu_ttl = ttl_s, gpu_ttl_s
        self._lock = asyncio.Lock()
        self._cache: tuple[float, dict[str, Any]] | None = None
        self._gpu_cache: tuple[float, dict[str, Any]] | None = None
        self._cpu_prev = read_cpu()

    def _gpu(self, now: float) -> dict[str, Any]:
        if self._gpu_cache and now - self._gpu_cache[0] < self._gpu_ttl:
            return self._gpu_cache[1]
        text = self._run_gpu()
        gpu = (parse_nvidia_smi(text) if text else None) or {"available": False}
        self._gpu_cache = (now, gpu)
        return gpu

    def _sample(self) -> dict[str, Any]:
        now = self._clock()
        cpu = None
        current = self._read_cpu()
        if current is not None and self._cpu_prev is not None:
            cpu = cpu_percent(self._cpu_prev, current)
        if current is not None:
            self._cpu_prev = current
        return {"cpu_percent": cpu, "ram": self._read_mem(), "process": self._read_proc(), "gpu": self._gpu(now)}

    async def snapshot(self) -> dict[str, Any]:
        async with self._lock:
            now = self._clock()
            if self._cache and now - self._cache[0] < self._ttl:
                return self._cache[1]
            try:
                data = await asyncio.to_thread(self._sample)
            except Exception:  # noqa: BLE001 - a broken reading must never break the dashboard
                logger.exception("could not read system statistics")
                data = {"cpu_percent": None, "ram": None, "process": None, "gpu": {"available": False}}
            self._cache = (now, data)
            return data

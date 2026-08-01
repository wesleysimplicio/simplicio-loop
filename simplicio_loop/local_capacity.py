"""Measured local capacity and conservative adaptive worker admission."""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = "simplicio.local-capacity/v1"
DEFAULT_DISK_FLOOR_BYTES = 1 << 30
DEFAULT_MEMORY_FLOOR_BYTES = 512 << 20


@dataclass(frozen=True)
class CapacitySample:
    """One measured probe; unavailable signals are represented explicitly."""

    requested_workers: int
    safe_workers: int
    cpu_count: int | None
    memory_available_bytes: int | None
    disk_free_bytes: int | None
    measured: tuple[str, ...]
    unavailable: tuple[str, ...]
    null_reasons: dict[str, str]
    observed_at_ns: int
    schema: str = SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "requested_workers": self.requested_workers,
            "safe_workers": self.safe_workers,
            "cpu_count": self.cpu_count,
            "memory_available_bytes": self.memory_available_bytes,
            "disk_free_bytes": self.disk_free_bytes,
            "measured": list(self.measured),
            "unavailable": list(self.unavailable),
            "null_reasons": dict(self.null_reasons),
            "observed_at_ns": self.observed_at_ns,
        }


def _memory_available() -> int | None:
    try:
        import psutil

        return int(psutil.virtual_memory().available)
    except (ImportError, OSError, AttributeError, ValueError):
        return None


def probe_local_capacity(
    root: str | os.PathLike[str] = ".",
    *,
    requested_workers: int,
    reserve_workers: int = 1,
    disk_floor_bytes: int = DEFAULT_DISK_FLOOR_BYTES,
    memory_floor_bytes: int = DEFAULT_MEMORY_FLOOR_BYTES,
    now_ns: int | None = None,
) -> CapacitySample:
    """Measure safe physical worker capacity without estimating missing signals."""

    requested = max(1, int(requested_workers))
    unavailable: list[str] = []
    null_reasons: dict[str, str] = {}
    measured: list[str] = []
    try:
        cpu_count = int(os.cpu_count() or 0) or None
    except (OSError, TypeError, ValueError):
        cpu_count = None
    if cpu_count is None:
        unavailable.append("cpu_count")
        null_reasons["cpu_count"] = "os_cpu_count_unavailable"
    else:
        measured.append("cpu_count")

    memory = _memory_available()
    if memory is None:
        unavailable.append("memory_available_bytes")
        null_reasons["memory_available_bytes"] = "psutil_unavailable_or_probe_failed"
    else:
        measured.append("memory_available_bytes")

    try:
        disk = int(shutil.disk_usage(Path(root).resolve()).free)
    except (OSError, ValueError):
        disk = None
    if disk is None:
        unavailable.append("disk_free_bytes")
        null_reasons["disk_free_bytes"] = "disk_probe_failed"
    else:
        measured.append("disk_free_bytes")

    safe = 1
    if not unavailable and disk >= max(0, int(disk_floor_bytes)) and memory >= max(0, int(memory_floor_bytes)):
        safe = max(1, min(requested, cpu_count - max(0, int(reserve_workers))))
    elif disk is not None and disk < max(0, int(disk_floor_bytes)):
        null_reasons["workers"] = "disk_pressure"
    elif memory is not None and memory < max(0, int(memory_floor_bytes)):
        null_reasons["workers"] = "memory_pressure"
    else:
        null_reasons["workers"] = "required_capacity_signal_unavailable"
    return CapacitySample(
        requested_workers=requested,
        safe_workers=safe,
        cpu_count=cpu_count,
        memory_available_bytes=memory,
        disk_free_bytes=disk,
        measured=tuple(sorted(measured)),
        unavailable=tuple(sorted(unavailable)),
        null_reasons=null_reasons,
        observed_at_ns=time.time_ns() if now_ns is None else int(now_ns),
    )


__all__ = ["CapacitySample", "probe_local_capacity"]

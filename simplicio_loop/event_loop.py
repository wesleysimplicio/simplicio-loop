"""Optional uvloop selection with a safe default and feature flag."""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
from dataclasses import dataclass
from typing import Any, Awaitable, Dict, Optional


EVENT_LOOP_SCHEMA = "simplicio.event-loop-selection/v1"

_UNSET = object()


@dataclass(frozen=True)
class LoopSelection:
    name: str
    enabled: bool
    reason: str
    platform: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "schema": EVENT_LOOP_SCHEMA,
            "name": self.name,
            "enabled": self.enabled,
            "reason": self.reason,
            "platform": self.platform,
        }


def select_event_loop(
    *,
    platform: Optional[str] = None,
    uvloop_module: Any = _UNSET,
    enabled: Optional[bool] = None,
) -> LoopSelection:
    """Pick the event-loop policy.

    ``uvloop_module`` defaults to the sentinel ``_UNSET`` so a real
    ``import uvloop`` is attempted. Callers (tests) that want to force the
    "uvloop not installed" path deterministically, independent of whether
    uvloop actually happens to be installed on the machine running the
    test, pass ``uvloop_module=None`` explicitly.
    """
    platform_name = platform or sys.platform
    if platform_name.startswith("win"):
        return LoopSelection("asyncio", False, "windows_default", platform_name)
    if enabled is False:
        return LoopSelection("asyncio", False, "feature_disabled", platform_name)
    if uvloop_module is _UNSET:
        try:
            uvloop_module = importlib.import_module("uvloop")
        except ImportError:
            uvloop_module = None
    if uvloop_module is None:
        return LoopSelection("asyncio", False, "uvloop_unavailable", platform_name)
    return LoopSelection("uvloop", True, "optional_extra_available", platform_name)


def configure_event_loop(*, enabled: Optional[bool] = None) -> LoopSelection:
    if enabled is None:
        raw = os.environ.get("SIMPLICIO_LOOP_UVLOOP", "auto").strip().lower()
        enabled = raw not in {"0", "false", "off", "disabled"}
    selection = select_event_loop(enabled=enabled)
    if selection.enabled:
        module = importlib.import_module("uvloop")
        asyncio.set_event_loop_policy(module.EventLoopPolicy())
    return selection


def run(awaitable: Awaitable[Any], *, uvloop: Optional[bool] = None) -> Any:
    configure_event_loop(enabled=uvloop)
    return asyncio.run(awaitable)


__all__ = ["EVENT_LOOP_SCHEMA", "LoopSelection", "configure_event_loop", "run", "select_event_loop"]

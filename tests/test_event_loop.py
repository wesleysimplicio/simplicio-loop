from __future__ import annotations

import asyncio
import importlib
import sys
from types import SimpleNamespace

from simplicio_loop.event_loop import configure_event_loop, run, select_event_loop


def test_windows_always_uses_default_even_when_uvloop_is_present() -> None:
    fake = SimpleNamespace(EventLoopPolicy=asyncio.DefaultEventLoopPolicy)
    selected = select_event_loop(platform="win32", uvloop_module=fake)
    assert selected.name == "asyncio"
    assert selected.reason == "windows_default"


def test_unix_falls_back_when_optional_extra_is_absent(monkeypatch) -> None:
    # Hermetic: force the internal importlib lookup to fail regardless of whether
    # uvloop happens to be installed in the ambient test environment (it is, on
    # this real Unix canary host — see docs/uvloop-rollout.md). Without this
    # patch the assertion below silently depends on the environment NOT having
    # the optional extra installed, which is exactly backwards for a test whose
    # job is to prove the fallback path.
    real_import_module = importlib.import_module

    def _raise_for_uvloop(name: str, *args: object, **kwargs: object) -> object:
        if name == "uvloop":
            raise ImportError("simulated: uvloop optional extra not installed")
        return real_import_module(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", _raise_for_uvloop)
    selected = select_event_loop(platform="linux", uvloop_module=None, enabled=True)
    assert selected.name == "asyncio"
    assert selected.enabled is False
    assert selected.reason == "uvloop_unavailable"


def test_feature_flag_can_disable_available_uvloop() -> None:
    fake = SimpleNamespace(EventLoopPolicy=asyncio.DefaultEventLoopPolicy)
    selected = select_event_loop(platform="linux", uvloop_module=fake, enabled=False)
    assert selected.reason == "feature_disabled"


def test_available_uvloop_is_selected_on_unix() -> None:
    fake = SimpleNamespace(EventLoopPolicy=asyncio.DefaultEventLoopPolicy)
    selected = select_event_loop(platform="linux", uvloop_module=fake, enabled=True)
    assert selected.name == "uvloop"
    assert selected.enabled is True


def test_configure_default_is_safe_on_current_platform(monkeypatch) -> None:
    monkeypatch.setenv("SIMPLICIO_LOOP_UVLOOP", "0")
    selected = configure_event_loop()
    assert selected.enabled is False
    assert selected.name == "asyncio"


def test_run_uses_selection_and_executes_coroutine(monkeypatch) -> None:
    monkeypatch.setenv("SIMPLICIO_LOOP_UVLOOP", "0")
    assert run(asyncio.sleep(0, result="ok")) == "ok"


def test_selection_receipt_is_structured() -> None:
    selected = select_event_loop(platform=sys.platform, enabled=False)
    receipt = selected.as_dict()
    assert receipt["schema"] == "simplicio.event-loop-selection/v1"
    assert receipt["platform"] == sys.platform

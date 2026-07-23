from __future__ import annotations

import os

import pytest

from simplicio_loop.hub_daemon import default_transport
from simplicio_loop.platform_capabilities import probe_af_unix, probe_hub_transport
from simplicio_loop.core_network_guard import install as install_core_network_guard


@pytest.fixture(autouse=True)
def disable_operator_bootstrap_network_by_default(monkeypatch) -> None:
    """Hermetic tests opt into the real bootstrap explicitly at their test seam."""
    monkeypatch.setenv("SIMPLICIO_LOOP_AUTO_BOOTSTRAP_OPERATORS", "0")


def pytest_configure(config) -> None:
    """Make `check.py --core-gate` unable to reach the real network.

    This is deliberately opt-in so normal pytest runs and tests which exercise
    local AF_UNIX IPC retain their native socket behaviour.
    """
    if os.environ.get("SIMPLICIO_CORE_NO_NETWORK") != "1":
        return
    install_core_network_guard()


def _skip_when_unavailable(capability) -> None:
    if not capability.available:
        pytest.skip(
            "CAPABILITY_UNAVAILABLE[%s]: %s"
            % (capability.reason_code, capability.detail)
        )


@pytest.fixture
def require_af_unix() -> None:
    _skip_when_unavailable(probe_af_unix())


@pytest.fixture
def require_default_hub_transport() -> None:
    _skip_when_unavailable(probe_hub_transport(default_transport()))

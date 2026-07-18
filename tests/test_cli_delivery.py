"""Tests for --delivery CLI enumeration and friendly validation (issue #559)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from simplicio_loop import delivery


def test_normalize_valid_target():
    """AC2: a valid target normalizes without error."""
    assert delivery.normalize_delivery_target("verified") == "verified"
    assert delivery.normalize_delivery_target("IMPLEMENTED") == "implemented"
    assert delivery.normalize_delivery_target("  pr-open  ") == "pr-open"


def test_normalize_invalid_target_raises_friendly_error():
    """AC2/AC3: an invalid target raises DeliveryTargetError listing accepted values."""
    with pytest.raises(delivery.DeliveryTargetError) as exc:
        delivery.normalize_delivery_target("local")
    msg = str(exc.value)
    assert "local" in msg
    assert "accepted values" in msg
    for v in delivery.DELIVERY_ORDER[1:]:
        assert v in msg


def test_cli_help_lists_delivery_values():
    """AC1/AC4: `simplicio-loop run --help` enumerates accepted --delivery values."""
    proc = subprocess.run(
        [sys.executable, "-m", "simplicio_loop.cli", "run", "--help"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    for v in delivery.DELIVERY_ORDER[1:]:
        assert v in proc.stdout, f"help missing delivery value {v!r}"


def test_cli_invalid_delivery_exits_nonzero_without_traceback():
    """AC3/AC5: invalid --delivery exits != 0 and does NOT leak a Python traceback."""
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "simplicio_loop.cli",
            "run",
            "--task",
            "/tmp/does-not-exist-559.md",
            "--repo",
            ".",
            "--delivery",
            "local",
            "--max-iterations",
            "1",
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert "Traceback" not in proc.stderr
    assert "unsupported delivery target" in proc.stderr


def test_cli_valid_delivery_does_not_early_exit_on_normalization():
    """AC5 regression: a valid --delivery passes normalization (reaches conduct_run path)."""
    # Use a nonexistent task so conduct_run fails later, proving normalization passed.
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "simplicio_loop.cli",
            "run",
            "--task",
            "/tmp/does-not-exist-559.md",
            "--repo",
            ".",
            "--delivery",
            "verified",
            "--max-iterations",
            "1",
        ],
        capture_output=True,
        text=True,
    )
    # Must NOT be the delivery-validation error (exit 2).
    assert "unsupported delivery target" not in proc.stderr

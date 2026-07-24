from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


def test_authority_e2e_harness_selftest_is_real_and_offline():
    script = Path(__file__).parents[1] / "scripts" / "authority_e2e.py"
    try:
        result = subprocess.run([sys.executable, str(script), "selftest"], capture_output=True, text=True, close_fds=False)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 6:
            pytest.skip("Python Windows subprocess handle unavailable")
        raise
    assert result.returncode == 0, result.stderr
    assert "PASS" in result.stdout

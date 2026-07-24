from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_authority_e2e_harness_selftest_is_real_and_offline():
    script = Path(__file__).parents[1] / "scripts" / "authority_e2e.py"
    result = subprocess.run([sys.executable, str(script), "selftest"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "PASS" in result.stdout

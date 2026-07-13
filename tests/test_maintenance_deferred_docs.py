import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def test_maintenance_deferred_example_runs():
    example = REPO / "examples" / "maintenance-deferred" / "run_example.py"
    completed = subprocess.run(
        [sys.executable, str(example)],
        cwd=str(REPO),
        text=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=180,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "maintenance-receipt.json" in completed.stdout
    assert "mapper_scan_required" in completed.stdout

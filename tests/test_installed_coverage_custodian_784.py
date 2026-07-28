import json
import os
from pathlib import Path
import subprocess
import sys


def test_installed_mapper_fast_loop_e2e(tmp_path):
    installed = os.environ.get("SIMPLICIO_784_INSTALLED_TARGET")
    if not installed:
        return
    output = tmp_path / "receipt.json"
    script = Path(__file__).parents[1] / "scripts" / "installed_coverage_custodian_e2e_784.py"
    env = dict(os.environ, PYTHONPATH=installed)
    subprocess.run(
        [sys.executable, str(script), "--root", str(tmp_path / "run"),
         "--output", str(output), "--repetitions", "3"],
        cwd=tmp_path, env=env, check=True,
    )
    receipt = json.loads(output.read_text())
    assert receipt["classification"] == "MEASURED_INSTALLED_ARTIFACTS"
    assert all(path.startswith(installed) for path in receipt["module_roots"].values())
    assert receipt["e2e"]["terminal"] is True
    assert receipt["e2e"]["workers_materialized"] == 1
    assert receipt["e2e"]["workers_avoided"] == 1
    assert receipt["benchmark"]["address_case"]["addresses"] == 10_000
    assert receipt["benchmark"]["address_case"]["workers_materialized"] == 0
    assert receipt["local_llm"] is False

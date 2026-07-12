import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "planes_installed_e2e.py"
TASK = ROOT / "contracts/task-to-delivery/fixtures/planes/task.md"


def test_installed_planes_gate_uses_real_path_and_raw_markdown():
    result = subprocess.run([sys.executable, str(SCRIPT), "--task", str(TASK), "--json"],
                            cwd=ROOT, capture_output=True, text=True,
                            stdin=subprocess.DEVNULL, timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["schema"] == "simplicio.planes-installed-e2e/v1"
    assert receipt["status"] == "PLANNED"
    assert receipt["proof_kind"] == "measured"
    assert receipt["task_source"].endswith("contracts/task-to-delivery/fixtures/planes/task.md")
    assert len(receipt["hops"]) == 4
    assert all(h["returncode"] == 0 for h in receipt["hops"])
    assert all(row["ok"] and os.path.isabs(row["path"]) for row in receipt["installed"])


def test_installed_gate_has_no_fake_or_in_process_operator_path():
    source = SCRIPT.read_text(encoding="utf-8")
    assert "stand-in" not in source.lower()
    assert "import simplicio" not in source
    assert "subprocess.run" in source
    assert "--task-file" in source
    assert "--execute" in source


def test_missing_installed_binary_fails_closed(tmp_path):
    env = dict(os.environ)
    env["PATH"] = str(tmp_path)
    result = subprocess.run([sys.executable, str(SCRIPT), "--json"], cwd=ROOT,
                            env=env, capture_output=True, text=True,
                            stdin=subprocess.DEVNULL, timeout=30)
    assert result.returncode == 2
    receipt = json.loads(result.stdout)
    assert receipt["status"] == "BLOCKED"
    assert receipt["reason"]

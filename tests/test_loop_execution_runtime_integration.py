from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from simplicio_loop import loop_execution_receipt as receipt_mod

sys.path.insert(0, str(Path(__file__).parent))
from test_loop_execution_receipt import _fixture  # noqa: E402


@pytest.mark.skipif(shutil.which("simplicio") is None, reason="simplicio-runtime is not installed")
def test_runtime_verifies_the_loop_published_receipt(tmp_path, monkeypatch):
    repo, run = _fixture(tmp_path)
    monkeypatch.setattr(receipt_mod, "_git_commit", lambda _repo: "a" * 40)
    receipt_mod.publish_loop_execution_receipt(
        repo=repo, run_dir=run, manifest={"run_id": "run-1"}
    )

    result = subprocess.run(
        ["simplicio", "loop-execution", "--json", "--repo", str(repo)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["status"] == "VERIFIED", report
    assert report["verified"] is True
    assert report["run_id"] == "run-1"

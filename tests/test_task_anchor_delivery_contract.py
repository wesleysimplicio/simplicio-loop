from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ANCHOR = ROOT / "scripts" / "task_anchor.py"


def _run(anchor_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, SIMPLICIO_ANCHOR_FILE=str(anchor_path))
    return subprocess.run([sys.executable, str(ANCHOR), *args], cwd=ROOT, env=env,
                          capture_output=True, text=True, check=False)


def test_set_freezes_strict_delivery_contract(tmp_path: Path):
    anchor = tmp_path / "anchor.json"
    contract = tmp_path / "delivery.json"
    contract.write_text(json.dumps({
        "schema": "simplicio.delivery-contract/v1",
        "open_pr": False,
        "push_branch": True,
        "allow_new_files_in_repo": False,
        "allow_comments_in_code": False,
        "commit_message_convention": "#<id> - fix: <desc>",
    }), encoding="utf-8")
    result = _run(anchor, "set", "--item", "526", "--goal", "delivery", "--delivery", str(contract),
                  "--ac", "contract is frozen")
    assert result.returncode == 0, result.stderr + result.stdout
    saved = json.loads(anchor.read_text(encoding="utf-8"))
    assert saved["delivery"]["allow_new_files_in_repo"] is False


def test_set_rejects_unknown_delivery_field(tmp_path: Path):
    anchor = tmp_path / "anchor.json"
    contract = tmp_path / "delivery.json"
    contract.write_text(json.dumps({"schema": "simplicio.delivery-contract/v1", "unexpected": True}), encoding="utf-8")
    result = _run(anchor, "set", "--goal", "delivery", "--delivery", str(contract), "--ac", "contract is frozen")
    assert result.returncode == 2
    assert "invalid delivery contract" in result.stdout

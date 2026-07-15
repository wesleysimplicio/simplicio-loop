"""Focused evidence for the multi-device identity and fencing contract (#183-#186)."""
import json
import os
import subprocess
import sys

from scripts.agent_identity import ensure_identity, identity_matches, lease_identity


def test_identity_is_stable_but_session_can_be_overridden(tmp_path):
    path = tmp_path / "identity.json"
    first = ensure_identity(path=str(path), runtime="codex", session_id="s-a",
                            agent_id="agent-a", device_id="device-a")
    second = ensure_identity(path=str(path), runtime="claude", session_id="s-b",
                             agent_id="agent-a", device_id="device-a")
    assert first["agent_id"] == second["agent_id"] == "agent-a"
    assert first["device_id"] == second["device_id"] == "device-a"
    assert second["runtime"] == "claude" and second["session_id"] == "s-b"
    assert identity_matches(lease_identity(second), second)


def test_backlog_lease_records_identity_and_rejects_other_device(tmp_path):
    root = os.path.dirname(os.path.dirname(__file__))
    backlog = tmp_path / "backlog.jsonl"
    items = tmp_path / "items.json"
    items.write_text(json.dumps([{"id": "T1", "goal": "Ship distributed claim",
                                  "acs": ["Lease is fenced by agent identity"]}]), encoding="utf-8")
    env = os.environ.copy()
    env["SIMPLICIO_BACKLOG_FILE"] = str(backlog)
    env["SIMPLICIO_IDENTITY_FILE"] = str(tmp_path / "identity.json")
    def run(*args):
        return subprocess.run([sys.executable, os.path.join(root, "scripts", "task_backlog.py"), *args],
                              env=env, text=True, capture_output=True, stdin=subprocess.DEVNULL)
    assert run("init", "--goal", "distributed", "--item-file", str(items)).returncode == 0
    claim = run("next", "--agent-id", "agent-a", "--runtime", "codex",
                "--session-id", "session-a", "--device-id", "device-a")
    assert claim.returncode == 0
    item_id, _goal, fence = claim.stdout.strip().split("\t")
    raw = backlog.read_text(encoding="utf-8")
    assert '"agent_id": "agent-a"' in raw and '"device_id": "device-a"' in raw
    wrong = run("heartbeat", "--item", item_id, "--agent-id", "agent-a", "--runtime", "claude",
                "--session-id", "session-b", "--device-id", "device-b", "--fence", fence)
    assert wrong.returncode == 12
    right = run("heartbeat", "--item", item_id, "--agent-id", "agent-a", "--runtime", "codex",
                "--session-id", "session-a", "--device-id", "device-a", "--fence", fence)
    assert right.returncode == 0, right.stdout + right.stderr

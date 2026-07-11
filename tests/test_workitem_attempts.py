"""Evidence for bounded WorkItem attempts and fail-closed lease mutation (#171)."""

import json
import os
import subprocess
import sys


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKLOG = os.path.join(REPO, "scripts", "task_backlog.py")


def run(args, cwd, env):
    return subprocess.run(
        [sys.executable, BACKLOG, *args],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
    )


def test_retry_creates_new_attempt_and_anonymous_mutation_is_rejected(tmp_path):
    backlog = tmp_path / "backlog.jsonl"
    items = tmp_path / "items.json"
    items.write_text(
        json.dumps(
            [
                {
                    "id": "T1",
                    "goal": "Ship bounded work",
                    "acs": ["A real acceptance criterion"],
                }
            ]
        ),
        encoding="utf-8",
    )
    env = {**os.environ, "SIMPLICIO_BACKLOG_FILE": str(backlog)}
    assert (
        run(
            ["init", "--goal", "Ship", "--item-file", str(items)], str(tmp_path), env
        ).returncode
        == 0
    )

    first = run(["next", "--worker", "worker-a"], str(tmp_path), env)
    assert first.returncode == 0
    item_id, _goal, fence = first.stdout.strip().split("\t")

    second = run(["next", "--worker", "worker-b"], str(tmp_path), env)
    assert second.returncode == 0
    assert "no ready items" in second.stdout

    blocked = run(
        ["block", "--item", item_id, "--reason", "no", "--code", "cancelled"],
        str(tmp_path),
        env,
    )
    assert blocked.returncode == 12

    failed = run(
        [
            "fail",
            "--item",
            item_id,
            "--worker",
            "worker-a",
            "--fence",
            fence,
            "--reason",
            "retry",
            "--code",
            "verify",
            "--fingerprint",
            "fp-1",
        ],
        str(tmp_path),
        env,
    )
    assert failed.returncode == 0

    retry = run(["next", "--worker", "worker-a"], str(tmp_path), env)
    assert retry.returncode == 0
    _item_id, _goal, retry_fence = retry.stdout.strip().split("\t")
    assert retry_fence != fence

    records = [
        json.loads(line) for line in backlog.read_text(encoding="utf-8").splitlines()
    ]
    item = next(record for record in records if record.get("id") == item_id)
    attempts = item["attempts"]
    assert len(attempts) == 2
    assert attempts[0]["status"] == "failed"
    assert attempts[1]["status"] == "active"
    assert attempts[0]["attempt_id"] != attempts[1]["attempt_id"]

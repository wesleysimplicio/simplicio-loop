import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ORACLE = os.path.join(REPO, "scripts", "completion_oracle.py")


def _run(args, cwd, env=None):
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    return subprocess.run([sys.executable, ORACLE] + args, capture_output=True, text=True,
                          cwd=cwd, env=full_env, timeout=30, stdin=subprocess.DEVNULL)


def _seed_run(run_dir):
    (run_dir / "manifest.json").write_text(json.dumps({
        "schema": "simplicio.run-manifest/v1",
        "delivery_target": "verified"
    }), encoding="utf-8")
    (run_dir / "task-contract.json").write_text(json.dumps({"schema": "simplicio.task-contract-collection/v1"}), encoding="utf-8")
    (run_dir / "mapper-context.json").write_text(json.dumps({"handoff": {}}), encoding="utf-8")
    (run_dir / "operator-receipt.json").write_text(json.dumps({"schema": "simplicio.operator-receipt/v0"}), encoding="utf-8")
    (run_dir / "delivery-receipt.json").write_text(json.dumps({
        "schema": "simplicio.delivery-receipt/v1",
        "target": "verified",
        "current_state": "implemented",
        "ready": False,
        "source_kind": "local",
        "source_payload": {}
    }), encoding="utf-8")


def _seed_loop(loop):
    (loop / "scratchpad.md").write_text("---\ncompletion_promise: \"SIMPLICIO_DONE\"\n---\ngoal\n", encoding="utf-8")
    (loop / "anchor.json").write_text(json.dumps({"criteria": [{"id": "AC1", "status": "done"}]}), encoding="utf-8")
    (loop / "watcher_challenge.json").write_text(json.dumps({
        "challenge": "abc", "goal_fp": "", "written_at": "2026-07-10T00:00:00Z"
    }), encoding="utf-8")
    (loop / "watcher_state.json").write_text(json.dumps({
        "match": True, "status": "MEASURED", "checked_at": "2026-07-10T00:00:01Z",
        "challenge": "abc", "goal_fp": ""
    }), encoding="utf-8")


def test_oracle_requires_matching_watcher_and_verified_evidence(tmp_path):
    loop = tmp_path / ".orchestrator" / "loop"
    loop.mkdir(parents=True)
    run_dir = tmp_path / ".orchestrator" / "runs" / "r1"
    run_dir.mkdir(parents=True)
    _seed_loop(loop)
    _seed_run(run_dir)
    (run_dir / "evidence-receipt.json").write_text(json.dumps({
        "schema": "simplicio.evidence-receipt/v1",
        "status": "UNVERIFIED",
        "criteria": [{"id": "AC1", "verification_state": "unverified"}],
        "summary": {"criteria_total": 1, "criteria_verified": 0,
                    "scenario_total": 1, "scenario_verified": 0, "rule_total": 1, "rule_verified": 0}
    }), encoding="utf-8")
    blocked = _run(["--loop-dir", str(loop), "--run-dir", str(run_dir),
                    "--response-text", "<promise>SIMPLICIO_DONE</promise>"], str(tmp_path))
    assert blocked.returncode == 1, blocked.stdout + blocked.stderr
    (run_dir / "evidence-receipt.json").write_text(json.dumps({
        "schema": "simplicio.evidence-receipt/v1",
        "status": "VERIFIED",
        "criteria": [{"id": "AC1", "verification_state": "verified"}],
        "summary": {"criteria_total": 1, "criteria_verified": 1,
                    "scenario_total": 1, "scenario_verified": 1, "rule_total": 1, "rule_verified": 1}
    }), encoding="utf-8")
    (run_dir / "delivery-receipt.json").write_text(json.dumps({
        "schema": "simplicio.delivery-receipt/v1",
        "target": "verified",
        "current_state": "verified",
        "ready": True,
        "source_kind": "local",
        "source_payload": {
            "evidence_receipt": "evidence-receipt.json",
            "criteria_verified": 1
        }
    }), encoding="utf-8")
    ok = _run(["--loop-dir", str(loop), "--run-dir", str(run_dir),
               "--response-text", "<promise>SIMPLICIO_DONE</promise>"], str(tmp_path))
    assert ok.returncode == 0, ok.stdout + ok.stderr


def test_oracle_blocks_when_anchor_is_missing_even_with_verified_receipts(tmp_path):
    loop = tmp_path / ".orchestrator" / "loop"
    loop.mkdir(parents=True)
    run_dir = tmp_path / ".orchestrator" / "runs" / "r1"
    run_dir.mkdir(parents=True)
    (loop / "scratchpad.md").write_text("---\ncompletion_promise: \"SIMPLICIO_DONE\"\n---\ngoal\n", encoding="utf-8")
    (loop / "watcher_challenge.json").write_text(json.dumps({
        "challenge": "abc", "goal_fp": "", "written_at": "2026-07-10T00:00:00Z"
    }), encoding="utf-8")
    (loop / "watcher_state.json").write_text(json.dumps({
        "match": True, "status": "MEASURED", "checked_at": "2026-07-10T00:00:01Z",
        "challenge": "abc", "goal_fp": ""
    }), encoding="utf-8")
    _seed_run(run_dir)
    (run_dir / "evidence-receipt.json").write_text(json.dumps({
        "schema": "simplicio.evidence-receipt/v1",
        "status": "VERIFIED",
        "criteria": [{"id": "AC1", "verification_state": "verified"}],
        "summary": {"criteria_total": 1, "criteria_verified": 1,
                    "scenario_total": 1, "scenario_verified": 1, "rule_total": 1, "rule_verified": 1}
    }), encoding="utf-8")
    blocked = _run(["--loop-dir", str(loop), "--run-dir", str(run_dir),
                    "--response-text", "<promise>SIMPLICIO_DONE</promise>"], str(tmp_path))
    assert blocked.returncode == 1, blocked.stdout + blocked.stderr
    payload = json.loads(blocked.stdout)
    assert payload["reason_code"] == "anchor_missing"


def test_oracle_distinguishes_verified_from_merge_ready(tmp_path):
    loop = tmp_path / ".orchestrator" / "loop"
    loop.mkdir(parents=True)
    run_dir = tmp_path / ".orchestrator" / "runs" / "r1"
    run_dir.mkdir(parents=True)
    _seed_loop(loop)
    _seed_run(run_dir)
    (run_dir / "manifest.json").write_text(json.dumps({
        "schema": "simplicio.run-manifest/v1",
        "delivery_target": "merge-ready"
    }), encoding="utf-8")
    (run_dir / "evidence-receipt.json").write_text(json.dumps({
        "schema": "simplicio.evidence-receipt/v1",
        "status": "VERIFIED",
        "criteria": [{"id": "AC1", "verification_state": "verified"}],
        "summary": {"criteria_total": 1, "criteria_verified": 1,
                    "scenario_total": 1, "scenario_verified": 1, "rule_total": 1, "rule_verified": 1}
    }), encoding="utf-8")
    (run_dir / "delivery-receipt.json").write_text(json.dumps({
        "schema": "simplicio.delivery-receipt/v1",
        "target": "merge-ready",
        "current_state": "verified",
        "ready": False
    }), encoding="utf-8")
    blocked = _run(["--loop-dir", str(loop), "--run-dir", str(run_dir),
                    "--response-text", "<promise>SIMPLICIO_DONE</promise>"], str(tmp_path))
    assert blocked.returncode == 1, blocked.stdout + blocked.stderr
    payload = json.loads(blocked.stdout)
    assert payload["reason_code"] == "delivery_target_not_met"
    (run_dir / "delivery-receipt.json").write_text(json.dumps({
        "schema": "simplicio.delivery-receipt/v1",
        "target": "merge-ready",
        "current_state": "merge-ready",
        "ready": True,
        "source_kind": "github",
        "source_payload": {
            "pr": {"url": "https://example/pr/1", "head_sha": "abc", "base_sha": "def"},
            "checks": {"green": True},
            "reviews": {"approvals": 1, "open_threads": 0},
            "branch": {"up_to_date": True}
        }
    }), encoding="utf-8")
    ok = _run(["--loop-dir", str(loop), "--run-dir", str(run_dir),
               "--response-text", "<promise>SIMPLICIO_DONE</promise>"], str(tmp_path))
    assert ok.returncode == 0, ok.stdout + ok.stderr


def test_oracle_requires_pr_receipt_fields_for_pr_open(tmp_path):
    loop = tmp_path / ".orchestrator" / "loop"
    loop.mkdir(parents=True)
    run_dir = tmp_path / ".orchestrator" / "runs" / "r1"
    run_dir.mkdir(parents=True)
    _seed_loop(loop)
    _seed_run(run_dir)
    (run_dir / "manifest.json").write_text(json.dumps({
        "schema": "simplicio.run-manifest/v1",
        "delivery_target": "pr-open"
    }), encoding="utf-8")
    (run_dir / "evidence-receipt.json").write_text(json.dumps({
        "schema": "simplicio.evidence-receipt/v1",
        "status": "VERIFIED",
        "criteria": [{"id": "AC1", "verification_state": "verified"}],
        "summary": {"criteria_total": 1, "criteria_verified": 1,
                    "scenario_total": 1, "scenario_verified": 1, "rule_total": 1, "rule_verified": 1}
    }), encoding="utf-8")
    (run_dir / "delivery-receipt.json").write_text(json.dumps({
        "schema": "simplicio.delivery-receipt/v1",
        "target": "pr-open",
        "current_state": "pr-open",
        "source_kind": "github",
        "source_payload": {"pr": {"url": "https://example/pr/1"}},
        "ready": False
    }), encoding="utf-8")
    blocked = _run(["--loop-dir", str(loop), "--run-dir", str(run_dir),
                    "--response-text", "<promise>SIMPLICIO_DONE</promise>"], str(tmp_path))
    assert blocked.returncode == 1, blocked.stdout + blocked.stderr
    payload = json.loads(blocked.stdout)
    assert payload["reason_code"] == "delivery_source_incomplete"


def test_oracle_requires_merge_ready_source_requery_fields(tmp_path):
    loop = tmp_path / ".orchestrator" / "loop"
    loop.mkdir(parents=True)
    run_dir = tmp_path / ".orchestrator" / "runs" / "r1"
    run_dir.mkdir(parents=True)
    _seed_loop(loop)
    _seed_run(run_dir)
    (run_dir / "manifest.json").write_text(json.dumps({
        "schema": "simplicio.run-manifest/v1",
        "delivery_target": "merge-ready"
    }), encoding="utf-8")
    (run_dir / "evidence-receipt.json").write_text(json.dumps({
        "schema": "simplicio.evidence-receipt/v1",
        "status": "VERIFIED",
        "criteria": [{"id": "AC1", "verification_state": "verified"}],
        "summary": {"criteria_total": 1, "criteria_verified": 1,
                    "scenario_total": 1, "scenario_verified": 1, "rule_total": 1, "rule_verified": 1}
    }), encoding="utf-8")
    (run_dir / "delivery-receipt.json").write_text(json.dumps({
        "schema": "simplicio.delivery-receipt/v1",
        "target": "merge-ready",
        "current_state": "merge-ready",
        "source_kind": "github",
        "source_payload": {
            "pr": {"url": "https://example/pr/1", "head_sha": "abc", "base_sha": "def"},
            "checks": {"green": True},
            "reviews": {"approvals": 1, "open_threads": 2},
            "branch": {"up_to_date": True}
        },
        "ready": False
    }), encoding="utf-8")
    blocked = _run(["--loop-dir", str(loop), "--run-dir", str(run_dir),
                    "--response-text", "<promise>SIMPLICIO_DONE</promise>"], str(tmp_path))
    assert blocked.returncode == 1, blocked.stdout + blocked.stderr
    payload = json.loads(blocked.stdout)
    assert payload["reason_code"] == "review_threads_open"


def test_oracle_requires_release_proofs_for_released_target(tmp_path):
    loop = tmp_path / ".orchestrator" / "loop"
    loop.mkdir(parents=True)
    run_dir = tmp_path / ".orchestrator" / "runs" / "r1"
    run_dir.mkdir(parents=True)
    _seed_loop(loop)
    _seed_run(run_dir)
    (run_dir / "manifest.json").write_text(json.dumps({
        "schema": "simplicio.run-manifest/v1",
        "delivery_target": "released"
    }), encoding="utf-8")
    (run_dir / "evidence-receipt.json").write_text(json.dumps({
        "schema": "simplicio.evidence-receipt/v1",
        "status": "VERIFIED",
        "criteria": [{"id": "AC1", "verification_state": "verified"}],
        "summary": {"criteria_total": 1, "criteria_verified": 1,
                    "scenario_total": 1, "scenario_verified": 1, "rule_total": 1, "rule_verified": 1}
    }), encoding="utf-8")
    (run_dir / "delivery-receipt.json").write_text(json.dumps({
        "schema": "simplicio.delivery-receipt/v1",
        "target": "released",
        "current_state": "released",
        "source_kind": "github",
        "source_payload": {
            "release": {
                "tag": "v1.2.3",
                "assets": ["simplicio-loop.whl"],
                "checksums_verified": True,
                "signatures_verified": True,
                "sbom_present": True
            },
            "install_smoke": {"passed": False}
        },
        "ready": False
    }), encoding="utf-8")
    blocked = _run(["--loop-dir", str(loop), "--run-dir", str(run_dir),
                    "--response-text", "<promise>SIMPLICIO_DONE</promise>"], str(tmp_path))
    assert blocked.returncode == 1, blocked.stdout + blocked.stderr
    payload = json.loads(blocked.stdout)
    assert payload["reason_code"] == "install_smoke_failed"


def test_oracle_writes_completion_receipt_bound_to_run_and_challenge(tmp_path):
    loop = tmp_path / ".orchestrator" / "loop"
    loop.mkdir(parents=True)
    run_dir = tmp_path / ".orchestrator" / "runs" / "r1"
    run_dir.mkdir(parents=True)
    _seed_loop(loop)
    _seed_run(run_dir)
    (run_dir / "evidence-receipt.json").write_text(json.dumps({
        "schema": "simplicio.evidence-receipt/v1",
        "status": "VERIFIED",
        "criteria": [{"id": "AC1", "verification_state": "verified"}],
        "summary": {"criteria_total": 1, "criteria_verified": 1,
                    "scenario_total": 1, "scenario_verified": 1, "rule_total": 1, "rule_verified": 1}
    }), encoding="utf-8")
    (run_dir / "delivery-receipt.json").write_text(json.dumps({
        "schema": "simplicio.delivery-receipt/v1",
        "target": "verified",
        "current_state": "verified",
        "ready": True,
        "source_kind": "local",
        "source_payload": {
            "evidence_receipt": "evidence-receipt.json",
            "criteria_verified": 1
        }
    }), encoding="utf-8")
    ok = _run(["--loop-dir", str(loop), "--run-dir", str(run_dir),
               "--response-text", "<promise>SIMPLICIO_DONE</promise>", "--write-receipt"], str(tmp_path))
    assert ok.returncode == 0, ok.stdout + ok.stderr
    payload = json.loads(ok.stdout)
    receipt_path = run_dir / "completion-receipt.json"
    assert payload["receipt_path"] == str(receipt_path)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["schema"] == "simplicio.completion-receipt/v1"
    assert receipt["ready"] is True
    assert receipt["verdict"] == "COMPLETE"
    assert receipt["run_id"] == "r1"
    assert receipt["challenge"] == "abc"
    assert receipt["artifacts"]["delivery_receipt"].endswith("delivery-receipt.json")
    assert receipt["delivery_target"] == "verified"
    assert receipt["delivery_state"] == "verified"


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _selfrun import run_module
    run_module(globals(), "test_completion_oracle")

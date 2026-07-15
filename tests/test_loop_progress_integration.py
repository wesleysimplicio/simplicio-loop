"""Tests for the progress-feedback nucleus worker (issue #298, EPIC #296).

`scripts/loop_progress.py` composes % of completion from task_backlog (drain-level items) +
task_anchor (per-item ACs) + its own event trail (turn position). These tests exercise the CLI
end-to-end (subprocess, isolated env vars) plus the pure formula directly.
"""
import importlib.util
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKER = os.path.join(REPO, "scripts", "loop_progress.py")

_spec = importlib.util.spec_from_file_location("loop_progress_test", WORKER)
loop_progress = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(loop_progress)


def _run(args, cwd, env):
    full_env = dict(os.environ)
    full_env.update(env)
    return subprocess.run([sys.executable, WORKER] + args, capture_output=True, text=True,
                          cwd=cwd, env=full_env, stdin=subprocess.DEVNULL)


def _env(tmp_path):
    return {
        "SIMPLICIO_PROGRESS_DIR": str(tmp_path),
        "SIMPLICIO_ANCHOR_FILE": str(tmp_path / "anchor.json"),
        "SIMPLICIO_BACKLOG_FILE": str(tmp_path / "backlog.jsonl"),
    }


def test_selftest_passes():
    r = subprocess.run([sys.executable, WORKER, "selftest"], capture_output=True, text=True,
                       cwd=REPO, stdin=subprocess.DEVNULL)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "MEASURED|loop_progress selftest:" in r.stdout, r.stdout
    assert "FAIL" not in r.stdout, r.stdout


def test_pct_formula_drain_matches_ac2_example():
    # Backlog: 5 items, 2 done. Anchor: 1/3 ACs. pct_overall = (2 + 1/3)/5 = 0.4667.
    pct_item, pct_overall, mode = loop_progress.compute_pct(
        {"done": 1, "total": 3}, {"items_total": 5, "items_done": 2}, 5, 9)
    assert mode == "drain"
    assert abs(pct_item - (1 / 3.0)) < 1e-6
    assert abs(pct_overall - (2 + 1 / 3.0) / 5) < 1e-6


def test_pct_formula_converge_when_no_backlog():
    pct_item, pct_overall, mode = loop_progress.compute_pct({"done": 2, "total": 4}, None, 3, 9)
    assert mode == "converge"
    assert abs(pct_overall - (0.5 * 0.9 + (3 / 9.0) * 0.1)) < 1e-6


def test_pct_formula_none_when_no_sources():
    pct_item, pct_overall, mode = loop_progress.compute_pct(None, None, 0, 9)
    assert mode == "none"
    assert pct_item is None
    assert pct_overall is None


def test_status_reports_unverified_pct_without_any_source(tmp_path):
    env = _env(tmp_path)
    r = _run(["status"], str(tmp_path), env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.startswith("UNVERIFIED|pct=?"), r.stdout


def test_emit_and_status_json_report_measured_pct_with_sources(tmp_path):
    env = _env(tmp_path)
    with open(env["SIMPLICIO_ANCHOR_FILE"], "w", encoding="utf-8") as f:
        json.dump({"item": "T3", "criteria": [
            {"id": "AC1", "status": "done"},
            {"id": "AC2", "status": "pending"},
            {"id": "AC3", "status": "pending"},
        ]}, f)
    with open(env["SIMPLICIO_BACKLOG_FILE"], "w", encoding="utf-8") as f:
        f.write(json.dumps({"kind": "master", "goal": "test"}) + "\n")
        for i, st in enumerate(["done", "done", "running", "ready", "ready"], start=1):
            f.write(json.dumps({"kind": "item", "id": "T%d" % i, "status": st}) + "\n")

    emitted = _run(["emit", "--step", "operate", "--status", "begin", "--item", "T3"],
                   str(tmp_path), env)
    assert emitted.returncode == 0, emitted.stdout + emitted.stderr
    assert emitted.stdout.startswith("MEASURED|"), emitted.stdout

    st = _run(["status", "--json"], str(tmp_path), env)
    assert st.returncode == 0, st.stdout + st.stderr
    snap = json.loads(st.stdout)
    assert abs(snap["pct_overall"] - (2 + 1 / 3.0) / 5) < 1e-3
    assert snap["mode"] == "drain"

    header = _run(["render", "--turn-header"], str(tmp_path), env)
    assert header.returncode == 0, header.stdout + header.stderr
    assert header.stdout.startswith("MEASURED|"), header.stdout
    assert "fase F1" in header.stdout, header.stdout

    full = _run(["render", "--full"], str(tmp_path), env)
    assert full.returncode == 0, full.stdout + full.stderr
    md_path = os.path.join(str(tmp_path), "PROGRESS.md")
    assert os.path.exists(md_path)
    with open(md_path, encoding="utf-8") as f:
        body = f.read()
    assert "simplicio-loop" in body


def test_snapshot_deletion_does_not_change_recomputed_pct(tmp_path):
    """AC7 — this worker is a projection, never an authority."""
    env = _env(tmp_path)
    with open(env["SIMPLICIO_ANCHOR_FILE"], "w", encoding="utf-8") as f:
        json.dump({"item": "T1", "criteria": [
            {"id": "AC1", "status": "done"}, {"id": "AC2", "status": "pending"}]}, f)
    with open(env["SIMPLICIO_BACKLOG_FILE"], "w", encoding="utf-8") as f:
        f.write(json.dumps({"kind": "master", "goal": "t"}) + "\n")
        f.write(json.dumps({"kind": "item", "id": "T1", "status": "running"}) + "\n")

    _run(["emit", "--step", "decide", "--status", "begin", "--item", "T1"], str(tmp_path), env)
    before = json.loads(_run(["status", "--json"], str(tmp_path), env).stdout)
    os.remove(os.path.join(str(tmp_path), "progress.json"))
    after = json.loads(_run(["status", "--json"], str(tmp_path), env).stdout)
    assert abs(before["pct_overall"] - after["pct_overall"]) < 1e-9


def test_corrupted_jsonl_degrades_gracefully(tmp_path):
    """AC4 — a truncated/corrupt progress.jsonl never crashes emit/status/render (exit 0)."""
    env = _env(tmp_path)
    events_path = os.path.join(str(tmp_path), "progress.jsonl")
    os.makedirs(str(tmp_path), exist_ok=True)
    with open(events_path, "w", encoding="utf-8") as f:
        f.write("{this is not json\n")
        f.write('{"step": "operate", "status": "begin"}\n')
    r = _run(["status"], str(tmp_path), env)
    assert r.returncode == 0, r.stdout + r.stderr
    r2 = _run(["emit", "--step", "watcher", "--status", "end", "--outcome", "pass"],
              str(tmp_path), env)
    assert r2.returncode == 0, r2.stdout + r2.stderr
    r3 = _run(["render", "--full"], str(tmp_path), env)
    assert r3.returncode == 0, r3.stdout + r3.stderr


def test_emit_rejects_unknown_step():
    r = subprocess.run([sys.executable, WORKER, "emit", "--step", "not-a-step"],
                       capture_output=True, text=True, cwd=REPO, stdin=subprocess.DEVNULL)
    assert r.returncode == 2, r.stdout + r.stderr


def test_describe_cli_lists_verbs_and_steps():
    r = subprocess.run([sys.executable, WORKER, "--describe-cli"], capture_output=True, text=True,
                       cwd=REPO, stdin=subprocess.DEVNULL)
    assert r.returncode == 0, r.stdout + r.stderr
    spec = json.loads(r.stdout)
    assert set(spec["verbs"]) == {"emit", "status", "render", "selftest"}
    assert spec["steps"] == loop_progress.STEPS


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _selfrun import run_module
    run_module(globals(), "test_loop_progress")

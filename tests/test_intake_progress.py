"""Tests for intake-stage progress instrumentation (issue #299, EPIC #296).

Covers the fail-open `emit` hooks wired into `scripts/task_backlog.py` (init/next/done/skip/
block/fail), `scripts/task_anchor.py` (set/mark) and `scripts/preflight.py` (build_report).
"""
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKLOG = os.path.join(REPO, "scripts", "task_backlog.py")
ANCHOR = os.path.join(REPO, "scripts", "task_anchor.py")
PROGRESS = os.path.join(REPO, "scripts", "loop_progress.py")


def _env(tmp_path):
    return {
        "SIMPLICIO_PROGRESS_DIR": str(tmp_path),
        "SIMPLICIO_ANCHOR_FILE": str(tmp_path / "anchor.json"),
        "SIMPLICIO_BACKLOG_FILE": str(tmp_path / "backlog.jsonl"),
    }


def _run(script, args, cwd, env):
    full_env = dict(os.environ)
    full_env.update(env)
    return subprocess.run([sys.executable, script] + args, capture_output=True, text=True,
                          cwd=cwd, env=full_env, stdin=subprocess.DEVNULL)


def _events(tmp_path):
    path = tmp_path / "progress.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def test_backlog_init_emits_triage_end_with_item_and_ac_counts(tmp_path):
    env = _env(tmp_path)
    item_file = tmp_path / "items.json"
    item_file.write_text(json.dumps([
        {"id": "T1", "goal": "First", "acs": ["A real criterion one", "A real criterion two"]},
        {"id": "T2", "goal": "Second", "acs": ["A real criterion three"]},
    ]), encoding="utf-8")
    r = _run(BACKLOG, ["init", "--goal", "Drain", "--item-file", str(item_file)],
             str(tmp_path), env)
    assert r.returncode == 0, r.stdout + r.stderr
    events = _events(tmp_path)
    assert events, "expected at least one progress event after init"
    ev = events[-1]
    assert ev["step"] == "triage" and ev["status"] == "end"
    assert "2 itens" in ev["detail"] and "3 ACs" in ev["detail"]
    assert ev["rebaseline"] is False
    assert os.path.exists(str(tmp_path / "PROGRESS.md"))


def test_backlog_reinit_marks_rebaseline_true(tmp_path):
    env = _env(tmp_path)
    item_file = tmp_path / "items.json"
    item_file.write_text(json.dumps([
        {"id": "T1", "goal": "First", "acs": ["A real criterion one"]},
    ]), encoding="utf-8")
    _run(BACKLOG, ["init", "--goal", "Drain", "--item-file", str(item_file)], str(tmp_path), env)
    r2 = _run(BACKLOG, ["init", "--goal", "Drain again", "--item-file", str(item_file)],
              str(tmp_path), env)
    assert r2.returncode == 0, r2.stdout + r2.stderr
    events = _events(tmp_path)
    assert events[-1]["rebaseline"] is True


def test_backlog_next_emits_triage_begin_with_item(tmp_path):
    env = _env(tmp_path)
    item_file = tmp_path / "items.json"
    item_file.write_text(json.dumps([
        {"id": "T1", "goal": "First", "acs": ["A real criterion one"]},
    ]), encoding="utf-8")
    _run(BACKLOG, ["init", "--goal", "Drain", "--item-file", str(item_file)], str(tmp_path), env)
    r = _run(BACKLOG, ["next", "--worker", "w1"], str(tmp_path), env)
    assert r.returncode == 0, r.stdout + r.stderr
    events = _events(tmp_path)
    claim_events = [e for e in events if e["status"] == "begin" and e["item_id"] == "T1"]
    assert claim_events, events


def test_backlog_done_moves_pct_and_emits_pass_outcome(tmp_path):
    env = _env(tmp_path)
    item_file = tmp_path / "items.json"
    item_file.write_text(json.dumps([
        {"id": "T1", "goal": "First", "acs": ["A real criterion one"]},
        {"id": "T2", "goal": "Second", "acs": ["A real criterion two"]},
    ]), encoding="utf-8")
    _run(BACKLOG, ["init", "--goal", "Drain", "--item-file", str(item_file)], str(tmp_path), env)
    claim = _run(BACKLOG, ["next", "--worker", "w1"], str(tmp_path), env)
    fence = claim.stdout.strip().split("\t")[2]
    records = [json.loads(line) for line in
               (tmp_path / "backlog.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    goal_fp = next(r for r in records if r.get("id") == "T1")["goal_fp"]
    anchor_path = tmp_path / "own_anchor.json"
    anchor_path.write_text(json.dumps({
        "goal_fp": goal_fp,
        "criteria": [{"id": "AC1", "status": "done", "evidence": "e.log"}],
    }), encoding="utf-8")
    done = _run(BACKLOG, ["done", "--item", "T1", "--anchor", str(anchor_path),
                          "--worker", "w1", "--fence", fence], str(tmp_path), env)
    assert done.returncode == 0, done.stdout + done.stderr
    st = _run(PROGRESS, ["status", "--json"], str(tmp_path), env)
    snap = json.loads(st.stdout)
    assert abs(snap["pct_overall"] - 0.5) < 1e-6, snap
    events = _events(tmp_path)
    assert any(e["outcome"] == "pass" and e["item_id"] == "T1" for e in events)


def test_anchor_set_and_mark_emit_progress_events(tmp_path):
    env = _env(tmp_path)
    r1 = _run(ANCHOR, ["set", "--item", "T1", "--goal", "g",
                       "--ac", "First real criterion", "--ac", "Second real criterion"],
              str(tmp_path), env)
    assert r1.returncode == 0, r1.stdout + r1.stderr
    r2 = _run(ANCHOR, ["mark", "--id", "AC1", "--status", "done", "--evidence", "e.log"],
              str(tmp_path), env)
    assert r2.returncode == 0, r2.stdout + r2.stderr
    events = _events(tmp_path)
    assert any(e["step"] == "triage" and "2 ACs" in (e.get("detail") or "") for e in events)
    assert any(e["step"] == "journal" and e["outcome"] == "pass" for e in events)
    st = _run(PROGRESS, ["status", "--json"], str(tmp_path), env)
    snap = json.loads(st.stdout)
    # converge mode (no backlog): pct_item=0.5 (1/2 ACs); last event step=journal (7/9) ->
    # 0.5*0.9 + (7/9)*0.1 = 0.5278
    assert abs(snap["pct_overall"] - (0.5 * 0.9 + (7 / 9.0) * 0.1)) < 1e-6, snap


def test_progress_instrumentation_is_fail_open_when_progress_dir_unwritable(tmp_path):
    """AC7 — a broken progress sink must never change the backlog worker's own exit code."""
    env = _env(tmp_path)
    item_file = tmp_path / "items.json"
    item_file.write_text(json.dumps([
        {"id": "T1", "goal": "First", "acs": ["A real criterion one"]},
    ]), encoding="utf-8")
    # Point the progress dir at a path that cannot be created (a file, not a directory).
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x", encoding="utf-8")
    env["SIMPLICIO_PROGRESS_DIR"] = str(blocker / "nested")
    r = _run(BACKLOG, ["init", "--goal", "Drain", "--item-file", str(item_file)],
             str(tmp_path), env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "frozen 1 item(s)" in r.stdout, r.stdout


def test_preflight_selftest_and_existing_worker_selftests_stay_green(tmp_path):
    """AC8 — task_backlog/task_anchor selftests remain green after instrumentation."""
    r1 = subprocess.run([sys.executable, ANCHOR, "selftest"], capture_output=True, text=True,
                       cwd=REPO, stdin=subprocess.DEVNULL)
    assert r1.returncode == 0, r1.stdout + r1.stderr
    assert "FAIL" not in r1.stdout
    r2 = subprocess.run([sys.executable, BACKLOG, "selftest"], capture_output=True, text=True,
                       cwd=REPO, stdin=subprocess.DEVNULL)
    assert r2.returncode == 0, r2.stdout + r2.stderr


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _selfrun import run_module
    run_module(globals(), "test_intake_progress")

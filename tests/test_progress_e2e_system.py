"""E2E test for the progress-feedback subsystem (issue #304, EPIC #296).

Drives a synthetic 3-item/2-AC-per-item drain run through the real CLIs (task_backlog.py,
task_anchor.py, loop_progress.py) and asserts:
  (a) the full per-turn event sequence lands in progress.jsonl;
  (b) pct_overall is monotonically non-decreasing across the run (no `rebaseline` fires here);
  (c) the run ends at 100% with `run_state: done`.
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
    r = subprocess.run([sys.executable, script] + args, capture_output=True, text=True,
                       cwd=cwd, env=full_env, stdin=subprocess.DEVNULL)
    assert r.returncode in (0, 12), "%s %s -> rc=%d\n%s%s" % (
        script, args, r.returncode, r.stdout, r.stderr)
    return r


def _status_json(tmp_path, env):
    r = _run(PROGRESS, ["status", "--json"], str(tmp_path), env)
    return json.loads(r.stdout)


def _events(tmp_path):
    path = tmp_path / "progress.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def test_three_item_two_ac_run_is_monotonic_and_ends_at_100_done(tmp_path):
    env = _env(tmp_path)
    item_file = tmp_path / "items.json"
    item_file.write_text(json.dumps([
        {"id": "T1", "goal": "First item", "acs": ["A real criterion one", "A real criterion two"]},
        {"id": "T2", "goal": "Second item", "acs": ["Another real criterion one", "Another real criterion two"]},
        {"id": "T3", "goal": "Third item", "acs": ["Third real criterion one", "Third real criterion two"]},
    ]), encoding="utf-8")

    # Fase 0: freeze the backlog
    _run(BACKLOG, ["init", "--goal", "Drain e2e", "--item-file", str(item_file)], str(tmp_path), env)
    pct_trace = [_status_json(tmp_path, env)["pct_overall"]]

    for item_id, item_goal, ac_texts in (
        ("T1", "First item", ["A real criterion one", "A real criterion two"]),
        ("T2", "Second item", ["Another real criterion one", "Another real criterion two"]),
        ("T3", "Third item", ["Third real criterion one", "Third real criterion two"]),
    ):
        claim = _run(BACKLOG, ["next", "--worker", "w1"], str(tmp_path), env)
        assert claim.stdout.startswith(item_id + "\t"), claim.stdout
        fence = claim.stdout.strip().split("\t")[2]

        # --force: each item is a genuinely different goal, so re-anchoring must not be blocked
        # by the DRIFT guard that protects against an accidental goal swap mid-item.
        _run(ANCHOR, ["set", "--item", item_id, "--goal", item_goal, "--force",
                     "--ac", ac_texts[0], "--ac", ac_texts[1]], str(tmp_path), env)
        pct_trace.append(_status_json(tmp_path, env)["pct_overall"])

        # turn 1: verify AC1
        _run(ANCHOR, ["mark", "--id", "AC1", "--status", "done", "--evidence", "e1.log"],
             str(tmp_path), env)
        pct_trace.append(_status_json(tmp_path, env)["pct_overall"])

        # turn 2: verify AC2
        _run(ANCHOR, ["mark", "--id", "AC2", "--status", "done", "--evidence", "e2.log"],
             str(tmp_path), env)
        pct_trace.append(_status_json(tmp_path, env)["pct_overall"])

        # item done -> backlog closes it
        records = [json.loads(line) for line in
                   (tmp_path / "backlog.jsonl").read_text(encoding="utf-8").splitlines()
                   if line.strip()]
        goal_fp = next(r for r in records if r.get("id") == item_id)["goal_fp"]
        anchor_snapshot = tmp_path / "anchor.json"
        with open(anchor_snapshot, encoding="utf-8") as f:
            anchor_data = json.load(f)
        assert anchor_data["goal_fp"] == goal_fp
        done_result = _run(BACKLOG, ["done", "--item", item_id, "--anchor", str(anchor_snapshot),
                                    "--worker", "w1", "--fence", fence], str(tmp_path), env)
        assert done_result.returncode == 0, done_result.stdout + done_result.stderr
        pct_trace.append(_status_json(tmp_path, env)["pct_overall"])

    # (b) monotonicity: pct_overall never decreases across the whole run
    for i in range(1, len(pct_trace)):
        prev, cur = pct_trace[i - 1], pct_trace[i]
        assert prev is None or cur is None or cur >= prev - 1e-9, (
            "pct_overall regressed at step %d: %r -> %r (trace=%r)" % (i, prev, cur, pct_trace))

    # (c) 100% at the end
    final = _status_json(tmp_path, env)
    assert abs(final["pct_overall"] - 1.0) < 1e-6, final

    # run_state stays "running" until an explicit refeed_exit event closes it (that's #302's
    # hooks/loop_stop.py's job, exercised separately in test_transcript_progress.py) — assert
    # the mechanism produces "done" when that final event IS emitted, closing the loop here.
    _run(PROGRESS, ["emit", "--step", "refeed_exit", "--status", "end", "--outcome", "pass",
                   "--detail", "promise verificada"], str(tmp_path), env)
    closed = _status_json(tmp_path, env)
    assert closed["run_state"] == "done", closed
    assert abs(closed["pct_overall"] - 1.0) < 1e-6, closed

    # (a) the full per-turn event sequence landed
    events = _events(tmp_path)
    steps_seen = [e["step"] for e in events]
    assert steps_seen.count("triage") >= 3 + 3  # backlog init/claim + anchor set, x3 items
    assert steps_seen.count("journal") >= 6  # 2 AC marks x 3 items
    assert steps_seen[-1] == "refeed_exit"


def test_full_run_event_dump_is_available_for_pr_evidence(tmp_path):
    """A real turn dump (AC10 of #300, referenced again here) — smoke-check the events are valid
    JSON, ordered by append, and iteration is non-decreasing (append-only order invariant shared
    with loop_journal.py)."""
    env = _env(tmp_path)
    item_file = tmp_path / "items.json"
    item_file.write_text(json.dumps([{"id": "T1", "goal": "Only item", "acs": ["A real criterion"]}]),
                        encoding="utf-8")
    _run(BACKLOG, ["init", "--goal", "g", "--item-file", str(item_file)], str(tmp_path), env)
    _run(PROGRESS, ["emit", "--step", "preflight", "--status", "begin", "--iteration", "1"],
         str(tmp_path), env)
    _run(PROGRESS, ["emit", "--step", "survey", "--status", "end", "--outcome", "pass",
                   "--iteration", "1"], str(tmp_path), env)
    events = _events(tmp_path)
    assert events, "expected at least one event"
    iterations = [e["iteration"] for e in events]
    assert iterations == sorted(iterations)


def test_monotonicity_check_is_red_on_mutated_formula():
    """#304 AC3 — prove the monotonicity assertion is a REAL gate, not a tautology: mutate
    compute_pct so it double-counts a stale anchor (the exact bug this suite's main test caught
    during development, fixed by the `anchor_matches_active` guard in loop_progress.py) and
    confirm the SAME assertion pattern used above would then fail (red on the broken formula,
    green on the fixed one)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("loop_progress_mutation_test",
                                                   os.path.join(REPO, "scripts", "loop_progress.py"))
    lp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lp)

    # the FIXED formula: no regression when the anchor no longer matches the active item
    _, fixed_before, _ = lp.compute_pct({"item": "T1", "done": 2, "total": 2},
                                        {"items_total": 3, "items_done": 0, "active_item": "T1"}, 7, 9)
    _, fixed_after, _ = lp.compute_pct({"item": "T1", "done": 2, "total": 2},
                                       {"items_total": 3, "items_done": 1, "active_item": None}, 7, 9)
    assert fixed_after >= fixed_before - 1e-9, (fixed_before, fixed_after)

    # the MUTATED (pre-fix) formula: blindly folds in a stale anchor regardless of active_item
    def mutated_compute_pct(anchor_state, backlog_state, step_index, steps_total):
        pct_item = None
        if anchor_state and anchor_state.get("total"):
            pct_item = anchor_state["done"] / float(anchor_state["total"])
        if backlog_state and backlog_state.get("items_total"):
            active_pct = pct_item if pct_item is not None else 0.0
            pct_overall = (backlog_state["items_done"] + active_pct) / float(backlog_state["items_total"])
            return pct_item, pct_overall, "drain"
        return None, None, "none"

    # Step 1: T1 just finished (items_done bumps to 1) but the anchor is still T1's — fully
    # verified (2/2) — the pre-fix formula double-counts it on top of items_done, inflating the %.
    _, mutated_inflated, _ = mutated_compute_pct(
        {"item": "T1", "done": 2, "total": 2},
        {"items_total": 3, "items_done": 1, "active_item": None}, 7, 9)
    # Step 2: T2 is claimed and its anchor is frozen fresh (0/2 verified) — the SAME items_done=1,
    # but now the (correctly reset) anchor contributes 0 instead of the stale 1.0.
    _, mutated_after_reset, _ = mutated_compute_pct(
        {"item": "T2", "done": 0, "total": 2},
        {"items_total": 3, "items_done": 1, "active_item": "T2"}, 3, 9)
    assert mutated_after_reset < mutated_inflated - 1e-9, (
        "expected the pre-fix formula to regress here (that's the bug this test proves is now "
        "caught); got %r -> %r" % (mutated_inflated, mutated_after_reset))

    # The FIXED formula does not exhibit this regression for the identical transition.
    _, fixed_inflated, _ = lp.compute_pct(
        {"item": "T1", "done": 2, "total": 2},
        {"items_total": 3, "items_done": 1, "active_item": None}, 7, 9)
    _, fixed_after_reset, _ = lp.compute_pct(
        {"item": "T2", "done": 0, "total": 2},
        {"items_total": 3, "items_done": 1, "active_item": "T2"}, 3, 9)
    assert fixed_after_reset >= fixed_inflated - 1e-9, (fixed_inflated, fixed_after_reset)


def test_overhead_receipt_emit_call_is_under_200ms(tmp_path):
    """#304 AC6 — overhead receipt, not a promise: measure a single in-process
    `loop_progress.emit_event()` call (the per-step hot path every instrumented worker takes) and
    print the receipt. "Numbers only with a receipt" — this IS the receipt."""
    import importlib.util
    import time as _time
    spec = importlib.util.spec_from_file_location(
        "loop_progress_overhead_test", os.path.join(REPO, "scripts", "loop_progress.py"))
    lp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lp)

    orig = {k: os.environ.get(k) for k in
           ("SIMPLICIO_PROGRESS_DIR", "SIMPLICIO_ANCHOR_FILE", "SIMPLICIO_BACKLOG_FILE")}
    os.environ["SIMPLICIO_PROGRESS_DIR"] = str(tmp_path)
    os.environ["SIMPLICIO_ANCHOR_FILE"] = str(tmp_path / "anchor.json")
    os.environ["SIMPLICIO_BACKLOG_FILE"] = str(tmp_path / "backlog.jsonl")
    try:
        samples = []
        for i in range(20):
            t0 = _time.perf_counter()
            lp.emit_event("operate", status="begin", item="T1", detail="overhead sample %d" % i)
            samples.append((_time.perf_counter() - t0) * 1000.0)
    finally:
        for k, v in orig.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    samples.sort()
    p50 = samples[len(samples) // 2]
    p95 = samples[int(len(samples) * 0.95)]
    print("MEASURED|loop_progress.emit_event() overhead receipt: n=%d p50=%.2fms p95=%.2fms "
         "max=%.2fms (budget: <200ms/turn)" % (len(samples), p50, p95, max(samples)))
    assert p95 < 200.0, "emit_event() p95 overhead %.2fms exceeds the 200ms/turn budget" % p95


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _selfrun import run_module
    run_module(globals(), "test_progress_e2e")

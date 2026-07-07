"""End-to-end tests for `scripts/task_backlog.py` — the frozen multi-item decomposition ABOVE the
per-item task anchor (Phase 0):

  * `init` is fail-closed: no items / zero-AC item refused; a GENESIS (no-code) repo refuses a
    plan without `--genesis`, and with it forces the scaffold item to lead.
  * `genesis --exit-code` is the deterministic greenfield detector (exit 10).
  * `next` honors `depends_on`, re-prints the claimed item (one in flight), and prints exactly
    `empty` when everything drained (the drain-mode dry signal).
  * `done` is GATED on the real anchor: it refuses (exit 12) with no anchor, with another item's
    anchor, or with pending ACs — and passes only after a real `task_anchor.py set` + `mark`.

Every test isolates state via SIMPLICIO_BACKLOG_FILE / SIMPLICIO_ANCHOR_FILE into a tmp dir and
pins the genesis detector with --root.
"""
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _env(tmp_path):
    return dict(os.environ,
                SIMPLICIO_BACKLOG_FILE=str(tmp_path / "backlog.jsonl"),
                SIMPLICIO_ANCHOR_FILE=str(tmp_path / "anchor.json"))


def _run(script, args, env):
    return subprocess.run([sys.executable, os.path.join(REPO, "scripts", script)] + args,
                          capture_output=True, text=True, cwd=REPO, env=env)


def _backlog(args, env):
    return _run("task_backlog.py", args, env)


def _anchor(args, env):
    return _run("task_anchor.py", args, env)


def _plan(tmp_path, items, name="plan.json"):
    p = tmp_path / name
    p.write_text(json.dumps(items), encoding="utf-8")
    return str(p)


def _code_root(tmp_path):
    """A --root that is NOT genesis (has one code file)."""
    root = tmp_path / "coderoot"
    root.mkdir(exist_ok=True)
    (root / "app.py").write_text("print('x')\n", encoding="utf-8")
    return str(root)


def _drive_anchor_done(goal, acs, env):
    """Arm the real anchor for one item and verify every AC — the flow `next` prescribes."""
    s = _anchor(["set", "--goal", goal, "--force"] + sum([["--ac", a] for a in acs], []), env)
    assert s.returncode == 0, s.stdout + s.stderr
    for i in range(len(acs)):
        m = _anchor(["mark", "--id", "AC%d" % (i + 1), "--status", "done",
                     "--evidence", "receipt-%d" % (i + 1)], env)
        assert m.returncode == 0, m.stdout + m.stderr


def test_init_refuses_zero_ac_and_empty(tmp_path):
    env = _env(tmp_path)
    root = _code_root(tmp_path)
    r = _backlog(["init", "--goal", "g", "--root", root,
                  "--items-file", _plan(tmp_path, [])], env)
    assert r.returncode == 2, "empty plan must refuse (2), got %d:\n%s" % (r.returncode, r.stdout)
    r = _backlog(["init", "--goal", "g", "--root", root,
                  "--items-file", _plan(tmp_path, [{"goal": "x", "acs": []}])], env)
    assert r.returncode == 2, "zero-AC item must refuse (2), got %d:\n%s" % (r.returncode, r.stdout)
    assert "acceptance criteria" in r.stdout, r.stdout


def test_genesis_enforcement_and_scaffold_reorder(tmp_path):
    env = _env(tmp_path)
    empty = tmp_path / "empty"
    empty.mkdir()
    plan = [{"goal": "add the endpoint", "acs": ["endpoint responds"]},
            {"goal": "bootstrap the project", "acs": ["structure exists", "one test green"],
             "tags": ["scaffold"]},
            {"goal": "add the docs page", "acs": ["page renders"]}]
    # a genesis repo without --genesis is BLOCKED (12)
    r = _backlog(["init", "--goal", "greenfield app", "--root", str(empty),
                  "--items-file", _plan(tmp_path, plan)], env)
    assert r.returncode == 12, "expected BLOCKED 12, got %d:\n%s" % (r.returncode, r.stdout)
    assert "genesis" in r.stdout.lower(), r.stdout
    # --genesis without a scaffold-tagged item refuses (2)
    r = _backlog(["init", "--goal", "greenfield app", "--genesis", "--root", str(empty),
                  "--items-file", _plan(tmp_path, [{"goal": "x", "acs": ["a"]}])], env)
    assert r.returncode == 2, r.stdout
    assert "scaffold" in r.stdout, r.stdout
    # --genesis with the scaffold given NOT first: freezes AND auto-orders it to T1
    r = _backlog(["init", "--goal", "greenfield app", "--genesis", "--root", str(empty),
                  "--items-file", _plan(tmp_path, plan)], env)
    assert r.returncode == 0, r.stdout + r.stderr
    n = _backlog(["next", "--json"], env)
    assert n.returncode == 0, n.stdout + n.stderr
    got = json.loads(n.stdout)
    assert got["id"] == "T1" and got["goal"] == "bootstrap the project", n.stdout
    assert "task_anchor.py set" in got["arm"], n.stdout


def test_genesis_exit_code(tmp_path):
    env = _env(tmp_path)
    empty = tmp_path / "empty"
    empty.mkdir()
    r = _backlog(["genesis", "--root", str(empty), "--exit-code"], env)
    assert r.returncode == 10, "expected genesis exit 10, got %d:\n%s" % (r.returncode, r.stdout)
    assert "genesis" in r.stdout, r.stdout
    (empty / "main.py").write_text("pass\n", encoding="utf-8")
    r = _backlog(["genesis", "--root", str(empty), "--exit-code"], env)
    assert r.returncode == 0, "one .py must clear genesis, got %d:\n%s" % (r.returncode, r.stdout)
    assert "code" in r.stdout, r.stdout


def test_next_honors_depends_on_and_reprints_claimed(tmp_path):
    env = _env(tmp_path)
    plan = [{"id": "T1", "goal": "first thing", "acs": ["a1"]},
            {"id": "T2", "goal": "second thing", "acs": ["a2"], "depends_on": ["T1"]}]
    r = _backlog(["init", "--goal", "two steps", "--root", _code_root(tmp_path),
                  "--items-file", _plan(tmp_path, plan)], env)
    assert r.returncode == 0, r.stdout + r.stderr
    first = json.loads(_backlog(["next", "--json"], env).stdout)
    assert first["id"] == "T1", first
    again = json.loads(_backlog(["next", "--json"], env).stdout)
    assert again["id"] == "T1" and again["verdict"] == "claimed", again  # one in flight
    _drive_anchor_done("first thing", ["a1"], env)
    d = _backlog(["done", "--id", "T1"], env)
    assert d.returncode == 0, d.stdout + d.stderr
    nxt = json.loads(_backlog(["next", "--json"], env).stdout)
    assert nxt["id"] == "T2", "depends_on not honored:\n%s" % nxt


def test_done_is_gated_on_the_real_anchor(tmp_path):
    env = _env(tmp_path)
    plan = [{"goal": "item one", "acs": ["ac one", "ac two"]},
            {"goal": "item two", "acs": ["other ac"]}]
    r = _backlog(["init", "--goal", "gated", "--root", _code_root(tmp_path),
                  "--items-file", _plan(tmp_path, plan)], env)
    assert r.returncode == 0, r.stdout + r.stderr
    # no anchor armed at all -> BLOCKED 12
    d = _backlog(["done", "--id", "T1"], env)
    assert d.returncode == 12, "no-anchor done must block (12), got %d:\n%s" % (d.returncode, d.stdout)
    assert "blocked" in d.stdout.lower(), d.stdout
    # anchor armed for ANOTHER item's goal -> BLOCKED 12 (wrong fingerprint)
    _drive_anchor_done("item two", ["other ac"], env)
    d = _backlog(["done", "--id", "T1"], env)
    assert d.returncode == 12, "wrong-fp done must block (12), got %d:\n%s" % (d.returncode, d.stdout)
    # anchor armed for THIS item but ACs still pending -> BLOCKED 12
    s = _anchor(["set", "--goal", "item one", "--force",
                 "--ac", "ac one", "--ac", "ac two"], env)
    assert s.returncode == 0, s.stdout + s.stderr
    d = _backlog(["done", "--id", "T1"], env)
    assert d.returncode == 12, "pending-AC done must block (12), got %d:\n%s" % (d.returncode, d.stdout)
    # every AC verified on the real anchor -> done, evidence copied
    _drive_anchor_done("item one", ["ac one", "ac two"], env)
    d = _backlog(["done", "--id", "T1"], env)
    assert d.returncode == 0, d.stdout + d.stderr
    st = _backlog(["status"], env)
    assert "[done   ] T1" in st.stdout, st.stdout


def test_drain_dry_prints_exactly_empty(tmp_path):
    env = _env(tmp_path)
    plan = [{"goal": "only item", "acs": ["one ac"]},
            {"goal": "doomed item", "acs": ["never"]}]
    r = _backlog(["init", "--goal", "drain me", "--root", _code_root(tmp_path),
                  "--items-file", _plan(tmp_path, plan)], env)
    assert r.returncode == 0, r.stdout + r.stderr
    _backlog(["next"], env)
    _drive_anchor_done("only item", ["one ac"], env)
    assert _backlog(["done", "--id", "T1"], env).returncode == 0
    sk = _backlog(["skip", "--id", "T2"], env)
    assert sk.returncode == 2, "skip without --reason must refuse (2):\n%s" % sk.stdout
    assert _backlog(["skip", "--id", "T2", "--reason", "out of scope"], env).returncode == 0
    n = _backlog(["next"], env)
    assert n.returncode == 0, n.stdout + n.stderr
    assert n.stdout.strip() == "empty", "dry signal must be exactly 'empty':\n%r" % n.stdout


def test_check_drift_and_idempotent_reinit(tmp_path):
    env = _env(tmp_path)
    plan = [{"goal": "stable item", "acs": ["an ac"]}]
    r = _backlog(["init", "--goal", "the frozen goal", "--root", _code_root(tmp_path),
                  "--items-file", _plan(tmp_path, plan)], env)
    assert r.returncode == 0, r.stdout + r.stderr
    d = _backlog(["check", "--goal", "a totally different goal", "--exit-code"], env)
    assert d.returncode == 11, "expected DRIFT 11, got %d:\n%s" % (d.returncode, d.stdout)
    assert "DRIFT" in d.stdout, d.stdout
    ok = _backlog(["check", "--goal", "the frozen goal", "--exit-code"], env)
    assert ok.returncode == 0, ok.stdout
    assert "BACKLOG_OK" in ok.stdout, ok.stdout
    # progress, then re-init the SAME goal: idempotent, done state preserved
    _backlog(["next"], env)
    _drive_anchor_done("stable item", ["an ac"], env)
    assert _backlog(["done", "--id", "T1"], env).returncode == 0
    r = _backlog(["init", "--goal", "the frozen goal", "--root", _code_root(tmp_path),
                  "--items-file", _plan(tmp_path, plan)], env)
    assert r.returncode == 0, r.stdout + r.stderr
    st = _backlog(["status"], env)
    assert "[done   ] T1" in st.stdout, "re-init lost progress:\n%s" % st.stdout
    # a CHANGED goal without --force is BLOCKED 12
    r = _backlog(["init", "--goal", "a swapped goal", "--root", _code_root(tmp_path),
                  "--items-file", _plan(tmp_path, plan)], env)
    assert r.returncode == 12, "goal swap without --force must block (12):\n%s" % r.stdout


def test_selftest_and_unknown_verb(tmp_path):
    env = _env(tmp_path)
    r = _backlog(["selftest"], env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PASS" in r.stdout, r.stdout
    r = _backlog(["definitely-not-a-real-verb-xyz"], env)
    assert r.returncode == 2, r.stdout
    assert "Traceback" not in r.stderr, "unknown verb crashed:\n%s" % r.stderr


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _selfrun import run_module
    run_module(globals(), "test_task_backlog")

"""scripts/_locked_append.py — locked JSONL append + tolerant-count helpers (issue #127).

Two layers of proof:
  1. Unit-level: the helper appends valid JSON, creates parent dirs, and — critically — FAILS
     OPEN (skips the write, never writes unlocked/partial) when the lock can't be acquired within
     its timeout.
  2. Concurrency: K REAL OS processes (`multiprocessing.Process`, not threads — threads never
     exercise actual cross-process file locking) each append M lines to the SAME journal-shaped
     file at once. Post-condition: exactly K*M lines, every single one valid JSON. This is the
     regression guard for the exact bug class #127 fixes: two writers racing an unlocked
     `open(path, "a").write()` interleaving into a torn/unparseable line.
"""
import json
import multiprocessing
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))
import _locked_append as la  # noqa: E402

K_WORKERS = 8
M_LINES = 40


def test_appends_valid_json_line(tmp_path):
    path = str(tmp_path / "journal.jsonl")
    assert la.locked_append_line(path, json.dumps({"a": 1})) is True
    assert la.locked_append_line(path, json.dumps({"a": 2})) is True
    with open(path, encoding="utf-8") as f:
        lines = [ln for ln in f if ln.strip()]
    assert len(lines) == 2
    assert json.loads(lines[0]) == {"a": 1}
    assert json.loads(lines[1]) == {"a": 2}


def test_adds_trailing_newline_when_missing(tmp_path):
    path = str(tmp_path / "j.jsonl")
    la.locked_append_line(path, '{"x":1}')  # no trailing \n supplied
    la.locked_append_line(path, '{"x":2}')
    with open(path, encoding="utf-8") as f:
        text = f.read()
    assert text == '{"x":1}\n{"x":2}\n'


def test_creates_parent_dirs(tmp_path):
    path = str(tmp_path / "nested" / "deep" / "journal.jsonl")
    assert la.locked_append_line(path, json.dumps({"ok": True})) is True
    assert os.path.exists(path)


def test_flush_and_fsync_survive_before_lock_release(tmp_path):
    # Not directly observable from the outside, but a write that returns True must be durably on
    # disk (readable) immediately after the call returns — this is the contract fsync buys us.
    path = str(tmp_path / "durable.jsonl")
    la.locked_append_line(path, json.dumps({"durable": True}))
    with open(path, encoding="utf-8") as f:
        assert json.loads(f.read().strip()) == {"durable": True}


def test_timeout_skips_write_never_writes_unlocked(tmp_path):
    """Fail-open contract: if the lock is already held (by THIS process, a different fd — POSIX
    flock is per-open-file-description, so a second fd contends genuinely), a short-timeout call
    must return False and must NOT append anything — never a partial/unlocked write."""
    if la.fcntl is None:
        return  # POSIX-only path exercised here; Windows CI (if any) skips this specific check
    path = str(tmp_path / "contended.jsonl")
    lock_path = la._lock_path(path)
    la._ensure_lock_file(lock_path)
    held_fh = open(lock_path, "a+b")
    la.fcntl.flock(held_fh.fileno(), la.fcntl.LOCK_EX)
    try:
        ok = la.locked_append_line(path, json.dumps({"should": "not-write"}), timeout_ms=80)
        assert ok is False
        assert not os.path.exists(path), "a timed-out lock must never write, not even partially"
    finally:
        la.fcntl.flock(held_fh.fileno(), la.fcntl.LOCK_UN)
        held_fh.close()
    # once the lock is free again, a normal call succeeds
    assert la.locked_append_line(path, json.dumps({"now": "ok"})) is True


def test_count_jsonl_lines_valid_only(tmp_path):
    path = tmp_path / "clean.jsonl"
    path.write_text('{"a":1}\n{"a":2}\n{"a":3}\n', encoding="utf-8")
    valid, corrupt = la.count_jsonl_lines(str(path))
    assert (valid, corrupt) == (3, 0)


def test_count_jsonl_lines_counts_corrupt_without_raising(tmp_path):
    path = tmp_path / "dirty.jsonl"
    path.write_text('{"a":1}\n{"a":2\nnot json at all\n{"a":3}\n', encoding="utf-8")
    valid, corrupt = la.count_jsonl_lines(str(path))
    assert valid == 2
    assert corrupt == 2


def test_count_jsonl_lines_missing_file_is_zero_zero(tmp_path):
    assert la.count_jsonl_lines(str(tmp_path / "nope.jsonl")) == (0, 0)


def test_count_jsonl_lines_ignores_blank_lines(tmp_path):
    path = tmp_path / "blanks.jsonl"
    path.write_text('{"a":1}\n\n\n{"a":2}\n', encoding="utf-8")
    assert la.count_jsonl_lines(str(path)) == (2, 0)


# ── concurrency (K real processes) ──────────────────────────────────────────────────────────


def _worker(path, worker_id, m):
    """Top-level, picklable worker body — runs in its OWN OS process."""
    scripts_dir = os.path.join(REPO, "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    from _locked_append import locked_append_line

    for i in range(m):
        rec = {
            "worker": worker_id,
            "seq": i,
            "iteration": worker_id * 1000 + i,
            "action": "concurrent-write",
            "gate": "pass",
            "ts": time.time(),
        }
        ok = locked_append_line(path, json.dumps(rec))
        if not ok:
            raise RuntimeError("worker %d: lock acquisition failed on write %d" % (worker_id, i))


def test_k_processes_m_lines_all_valid_json_no_loss(tmp_path):
    journal = str(tmp_path / "journal.jsonl")
    procs = []
    for w in range(K_WORKERS):
        p = multiprocessing.Process(target=_worker, args=(journal, w, M_LINES))
        p.start()
        procs.append(p)
    for p in procs:
        p.join(timeout=90)
        assert p.exitcode == 0, (
            "worker process %s failed or timed out (exitcode=%s)" % (p.pid, p.exitcode)
        )

    with open(journal, encoding="utf-8") as f:
        lines = [ln for ln in f if ln.strip()]

    expected = K_WORKERS * M_LINES
    assert len(lines) == expected, (
        "expected %d lines, got %d — a write was lost under concurrency" % (expected, len(lines))
    )

    seen = set()
    for ln in lines:
        rec = json.loads(ln)  # raises ValueError if this line is torn/interleaved — that IS the
        # regression this test guards against (#127): an unlocked writer can corrupt a line.
        seen.add((rec["worker"], rec["seq"]))
    assert len(seen) == expected, "duplicate or garbled records survived under concurrency"

    valid, corrupt = la.count_jsonl_lines(journal)
    assert (valid, corrupt) == (expected, 0)


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _selfrun import run_module
    run_module(globals(), "test_locked_append")

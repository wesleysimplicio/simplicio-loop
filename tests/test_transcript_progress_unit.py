"""Tests for transcript-surfaced progress (issue #302, EPIC #296).

Covers hooks/loop_stop.py's `_progress_header_prefix()` (re-feed header enrichment,
fail-open) and the "promise REJEITADA" emit on an unfulfilled promise, using the same
env-var-based isolation as the other progress test modules (no monkeypatch fixture, so this
runs under the bare-python3 fallback too).
"""
import importlib.util
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOOP_STOP = os.path.join(REPO, "hooks", "loop_stop.py")
PROGRESS = os.path.join(REPO, "scripts", "loop_progress.py")

_hspec = importlib.util.spec_from_file_location("loop_stop_transcript_test", LOOP_STOP)
loop_stop = importlib.util.module_from_spec(_hspec)
_hspec.loader.exec_module(loop_stop)

_pspec = importlib.util.spec_from_file_location("loop_progress_transcript_test", PROGRESS)
loop_progress = importlib.util.module_from_spec(_pspec)
_pspec.loader.exec_module(loop_progress)


class _env_ctx:
    def __init__(self, mapping):
        self.mapping = mapping
        self._orig = {}

    def __enter__(self):
        for k, v in self.mapping.items():
            self._orig[k] = os.environ.get(k)
            os.environ[k] = v
        return self

    def __exit__(self, *exc):
        for k, v in self._orig.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return False


def _env(tmp_path):
    return {
        "SIMPLICIO_PROGRESS_DIR": str(tmp_path),
        "SIMPLICIO_ANCHOR_FILE": str(tmp_path / "anchor.json"),
        "SIMPLICIO_BACKLOG_FILE": str(tmp_path / "backlog.jsonl"),
    }


def _events(tmp_path):
    path = tmp_path / "progress.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def test_progress_header_prefix_empty_without_any_source(tmp_path):
    with _env_ctx(_env(tmp_path)):
        prefix = loop_stop._progress_header_prefix(3, 24)
    assert prefix == " · pct=?"


def test_progress_header_prefix_shows_fase_etapa_item_acs_pct(tmp_path):
    env = _env(tmp_path)
    with _env_ctx(env):
        (tmp_path / "anchor.json").write_text(json.dumps({
            "item": "T1", "criteria": [{"id": "AC1", "status": "done"},
                                       {"id": "AC2", "status": "pending"}]}), encoding="utf-8")
        (tmp_path / "backlog.jsonl").write_text(
            json.dumps({"kind": "master", "goal": "g"}) + "\n" +
            json.dumps({"kind": "item", "id": "T1", "status": "running"}) + "\n",
            encoding="utf-8")
        prefix = loop_stop._progress_header_prefix(3, 24)
    assert "fase F" in prefix
    assert "item T1" in prefix
    assert "ACs 1/2" in prefix
    assert "50%" in prefix
    # the outer bracket already carries the iteration -> no redundant "· iter N" in the prefix
    assert "iter" not in prefix


def test_progress_header_prefix_regenerates_progress_md(tmp_path):
    with _env_ctx(_env(tmp_path)):
        loop_stop._progress_header_prefix(1, 0)
    assert (tmp_path / "PROGRESS.md").exists()
    assert (tmp_path / "progress.json").exists()


def test_progress_header_prefix_is_fail_open_without_loop_progress_module(tmp_path, monkeypatch=None):
    """AC2 — simulate loop_progress.py being unavailable; the prefix degrades to ''."""
    with _env_ctx(_env(tmp_path)):
        orig = loop_stop._loop_progress_module
        loop_stop._loop_progress_module = lambda: None
        try:
            prefix = loop_stop._progress_header_prefix(1, 0)
        finally:
            loop_stop._loop_progress_module = orig
    assert prefix == ""


def test_emit_final_progress_never_raises_when_module_missing(tmp_path):
    with _env_ctx(_env(tmp_path)):
        orig = loop_stop._loop_progress_module
        loop_stop._loop_progress_module = lambda: None
        try:
            loop_stop._emit_final_progress("test reason", "blocked")  # must not raise
        finally:
            loop_stop._loop_progress_module = orig


def test_emit_final_progress_writes_refeed_exit_event(tmp_path):
    with _env_ctx(_env(tmp_path)):
        loop_stop._emit_final_progress("promise verificada", "pass")
        events = _events(tmp_path)
    exits = [e for e in events if e["step"] == "refeed_exit"]
    assert exits and exits[-1]["outcome"] == "pass"
    assert exits[-1]["detail"] == "promise verificada"


def test_run_state_done_after_promise_verified_event(tmp_path):
    with _env_ctx(_env(tmp_path)):
        loop_stop._emit_final_progress("promise verificada", "pass")
        snap = loop_progress.build_snapshot()
    assert snap["run_state"] == "done"


def test_promise_rejected_event_gives_capped_neutral_run_state(tmp_path):
    """A rejected-but-continuing promise emits blocked/refeed_exit; run_state falls to the
    generic 'blocked' bucket (transient — the next real turn event resets it to 'running')."""
    with _env_ctx(_env(tmp_path)):
        loop_stop._emit_final_progress("promise REJEITADA: sem evidência no turno", "blocked")
        snap = loop_progress.build_snapshot()
    assert snap["run_state"] == "blocked"
    header = loop_progress.render_turn_header(snap)
    # no DRIFT/STALLED keyword in this detail -> no special banner, but the event is on record
    assert "⚠" not in header


def test_loop_stop_module_imports_cleanly_and_has_new_helpers():
    assert hasattr(loop_stop, "_progress_header_prefix")
    assert hasattr(loop_stop, "_emit_final_progress")
    assert hasattr(loop_stop, "cleanup_and_stop")


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _selfrun import run_module
    run_module(globals(), "test_transcript_progress")

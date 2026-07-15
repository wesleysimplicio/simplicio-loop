"""#78: coverage for scripts/agentsview_adapter.py — the agentsview source_adapter binding.

Every subcommand talks to either a local SQLite DB (AGENTSVIEW_DB) or an HTTP API
(AGENTSVIEW_API), gated by AGENTSVIEW_MODE. Per the "never fake-pass" discipline this suite
exercises ONLY local/offline paths:
  - `--dry-run` on every verb (prints the SQL/route it WOULD run, no I/O)
  - sqlite mode against a DB path that does not exist (sqlite_query's own not-found guard)
  - http mode against a loopback port nothing listens on (127.0.0.1 only — never a real
    external network call) to prove the urllib error path degrades to a clean {"error": ...}
    instead of a stack trace
  - pure arg-parsing / CLI-contract behavior (missing required flags, unknown subcommand)
"""
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADAPTER = os.path.join(REPO, "scripts", "agentsview_adapter.py")


def _run(args, env=None, cwd=None):
    full_env = dict(os.environ)
    # isolate from any real agentsview install that might exist on the host
    full_env.pop("AGENTSVIEW_DB", None)
    full_env.pop("AGENTSVIEW_API", None)
    full_env.pop("AGENTSVIEW_MODE", None)
    if env:
        full_env.update(env)
    return subprocess.run([sys.executable, ADAPTER] + args, capture_output=True, text=True,
                          cwd=cwd or REPO, env=full_env, timeout=30)


# ── --dry-run: every verb must print its intended action and do zero I/O ──────


def test_list_ready_dry_run_prints_sql_and_api_without_touching_anything(tmp_path):
    r = _run(["list_ready", "--dry-run"], env={"AGENTSVIEW_DB": str(tmp_path / "nope.db")})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "[DRY-RUN]" in r.stdout
    assert "SQL" in r.stdout and "API" in r.stdout
    assert not (tmp_path / "nope.db").exists(), "dry-run must not create the db file"


def test_get_details_dry_run_sqlite_mode(tmp_path):
    r = _run(["get_details", "--id", "abc123", "--dry-run"],
             env={"AGENTSVIEW_DB": str(tmp_path / "nope.db")})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "abc123" in r.stdout
    assert "[DRY-RUN]" in r.stdout


def test_get_details_dry_run_http_mode():
    r = _run(["get_details", "--id", "s1", "--dry-run"],
             env={"AGENTSVIEW_MODE": "http", "AGENTSVIEW_API": "http://127.0.0.1:1"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "/api/v1/sessions/s1" in r.stdout


def test_claim_and_close_and_update_status_and_attach_evidence_dry_run(tmp_path):
    db = str(tmp_path / "nope.db")
    for args in (["claim", "--id", "1", "--dry-run"],
                 ["close", "--id", "1", "--dry-run"],
                 ["update_status", "--id", "1", "--state", "resumed", "--dry-run"],
                 ["attach_evidence", "--id", "1", "--note", "n", "--dry-run"]):
        r = _run(args, env={"AGENTSVIEW_DB": db})
        assert r.returncode == 0, "%r -> %s" % (args, r.stdout + r.stderr)
        assert "[DRY-RUN]" in r.stdout, "%r -> %s" % (args, r.stdout)
    assert not os.path.exists(db)


def test_cost_summary_and_agent_breakdown_dry_run():
    for args in (["cost_summary", "--days", "3", "--dry-run"],
                 ["agent_breakdown", "--days", "3", "--dry-run"]):
        r = _run(args, env={"AGENTSVIEW_DB": "/nonexistent/nope.db"})
        assert r.returncode == 0, "%r -> %s" % (args, r.stdout + r.stderr)
        assert "[DRY-RUN]" in r.stdout


# ── sqlite mode, real call, but against a DB path that does not exist ─────────


def test_list_ready_sqlite_missing_db_reports_error_not_crash(tmp_path):
    r = _run(["list_ready"], env={"AGENTSVIEW_DB": str(tmp_path / "missing.db")})
    assert r.returncode == 0, r.stdout + r.stderr
    payload = json.loads(r.stdout)
    assert "error" in payload, "a missing DB must report a clean error, not silently succeed"
    assert "not found" in payload["error"].lower()


def test_get_details_sqlite_missing_db_reports_error_not_crash(tmp_path):
    r = _run(["get_details", "--id", "x"], env={"AGENTSVIEW_DB": str(tmp_path / "missing.db")})
    assert r.returncode == 0, r.stdout + r.stderr
    payload = json.loads(r.stdout)
    assert "error" in payload


# ── http mode against an unreachable loopback port — never a real network call ─


def test_list_ready_http_unreachable_endpoint_reports_error_not_crash():
    r = _run(["list_ready"],
             env={"AGENTSVIEW_MODE": "http", "AGENTSVIEW_API": "http://127.0.0.1:1"})
    assert r.returncode == 0, r.stdout + r.stderr
    payload = json.loads(r.stdout)
    assert "error" in payload, "an unreachable API must report a clean error, not crash"


def test_cost_summary_http_unreachable_endpoint_reports_error_not_crash():
    r = _run(["cost_summary"],
             env={"AGENTSVIEW_MODE": "http", "AGENTSVIEW_API": "http://127.0.0.1:1"})
    assert r.returncode == 0, r.stdout + r.stderr
    payload = json.loads(r.stdout)
    assert "error" in payload


# ── CLI contract: required flags, unknown subcommand ───────────────────────────


def test_get_details_missing_required_id_is_a_clean_argparse_error():
    r = _run(["get_details"])
    assert r.returncode == 2, r.stdout + r.stderr
    assert "Traceback" not in r.stderr
    assert "--id" in r.stderr


def test_update_status_rejects_unknown_state_choice():
    r = _run(["update_status", "--id", "1", "--state", "bogus-state"])
    assert r.returncode == 2, r.stdout + r.stderr
    assert "Traceback" not in r.stderr


def test_unknown_subcommand_is_a_clean_argparse_error():
    r = _run(["not-a-real-command"])
    assert r.returncode == 2, r.stdout + r.stderr
    assert "Traceback" not in r.stderr


def test_no_subcommand_is_a_clean_argparse_error():
    r = _run([])
    assert r.returncode == 2, r.stdout + r.stderr
    assert "Traceback" not in r.stderr


# ── pure/local helper functions, imported directly (no subprocess needed) ─────


def test_detect_db_path_prefers_env_override(tmp_path):
    sys.path.insert(0, os.path.join(REPO, "scripts"))
    try:
        import importlib
        if "agentsview_adapter" in sys.modules:
            del sys.modules["agentsview_adapter"]
        mod = importlib.import_module("agentsview_adapter")
        db_file = tmp_path / "custom.db"
        db_file.write_text("")
        old = os.environ.get("AGENTSVIEW_DB")
        os.environ["AGENTSVIEW_DB"] = str(db_file)
        try:
            assert mod.detect_db_path() == str(db_file)
        finally:
            if old is None:
                os.environ.pop("AGENTSVIEW_DB", None)
            else:
                os.environ["AGENTSVIEW_DB"] = old
    finally:
        sys.path.remove(os.path.join(REPO, "scripts"))


def test_mode_and_api_base_defaults():
    sys.path.insert(0, os.path.join(REPO, "scripts"))
    try:
        import importlib
        if "agentsview_adapter" in sys.modules:
            del sys.modules["agentsview_adapter"]
        mod = importlib.import_module("agentsview_adapter")
        for k in ("AGENTSVIEW_MODE", "AGENTSVIEW_API"):
            os.environ.pop(k, None)
        assert mod.mode() == "sqlite"
        assert mod.api_base() == "http://127.0.0.1:8080"
    finally:
        sys.path.remove(os.path.join(REPO, "scripts"))


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _selfrun import run_module
    run_module(globals(), "test_agentsview_adapter")

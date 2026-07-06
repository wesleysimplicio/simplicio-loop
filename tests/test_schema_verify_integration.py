"""Integration test for `scripts/schema_verify.py` (#110) — CLI + real sqlite database together.

Drives the full pipeline via subprocess: a diff file on disk, a real sqlite file created and
migrated out-of-band, and `--db-url sqlite:///...` wiring the static parser to the live-verify
step. Distinct from `test_schema_verify_unit.py` (pure `parse_diff`, no I/O, no CLI).
"""
import json
import os
import sqlite3
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEMA_VERIFY = os.path.join(REPO, "scripts", "schema_verify.py")


def _run(args, cwd):
    return subprocess.run([sys.executable, SCHEMA_VERIFY] + args, capture_output=True, text=True,
                          cwd=cwd, timeout=30)


def test_clean_diff_passes_without_db(tmp_path):
    diff = tmp_path / "clean.diff"
    diff.write_text(
        "+    INSERT INTO users (email) VALUES ('a@b.com');\n"
        "+    ALTER TABLE users ADD COLUMN email varchar(255);\n",
        encoding="utf-8",
    )
    r = _run(["--diff", str(diff)], cwd=str(tmp_path))
    assert r.returncode == 0, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert out["verdict"] == "PASS"
    assert out["findings"] == []


def test_drifted_diff_blocks_without_db(tmp_path):
    diff = tmp_path / "drift.diff"
    diff.write_text("+    INSERT INTO users (phone) VALUES ('555');\n", encoding="utf-8")
    r = _run(["--diff", str(diff)], cwd=str(tmp_path))
    assert r.returncode == 1, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert out["verdict"] == "BLOCKED"
    assert out["findings"][0]["column"] == "phone"
    assert out["note"].startswith("UNVERIFIED(live)")


def test_missing_diff_file_errors_cleanly(tmp_path):
    r = _run(["--diff", str(tmp_path / "nope.diff")], cwd=str(tmp_path))
    assert r.returncode == 2, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert out["verdict"] == "ERROR"


def test_drifted_diff_with_live_db_column_present_still_blocked_but_verified(tmp_path):
    # The column exists in the live DB but was never migrated IN THIS DIFF — still schema drift
    # for review purposes, but the finding must carry the live_verified=True annotation.
    db_path = tmp_path / "app.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE users (id INTEGER, phone TEXT)")
    conn.commit()
    conn.close()

    diff = tmp_path / "drift.diff"
    diff.write_text("+    INSERT INTO users (phone) VALUES ('555');\n", encoding="utf-8")

    r = _run(["--diff", str(diff), "--db-url", "sqlite:///%s" % db_path], cwd=str(tmp_path))
    out = json.loads(r.stdout)
    assert out["live"] is True
    finding = out["findings"][0]
    assert finding["live_verified"] is True
    assert "LIVE VERIFIED" in finding["message"]
    assert out["verdict"] == "PASS"


def test_drifted_diff_with_live_db_column_absent_blocks(tmp_path):
    db_path = tmp_path / "app.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE users (id INTEGER)")
    conn.commit()
    conn.close()

    diff = tmp_path / "drift.diff"
    diff.write_text("+    INSERT INTO users (phone) VALUES ('555');\n", encoding="utf-8")

    r = _run(["--diff", str(diff), "--db-url", "sqlite:///%s" % db_path], cwd=str(tmp_path))
    assert r.returncode == 1, r.stdout + r.stderr
    out = json.loads(r.stdout)
    finding = out["findings"][0]
    assert finding["live_verified"] is False
    assert out["verdict"] == "BLOCKED"


def test_selftest_verb():
    r = subprocess.run([sys.executable, SCHEMA_VERIFY, "selftest"], capture_output=True,
                       text=True, cwd=REPO, timeout=30)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "ALL PASS" in r.stdout


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _selfrun import run_module
    run_module(globals(), "test_schema_verify_integration")

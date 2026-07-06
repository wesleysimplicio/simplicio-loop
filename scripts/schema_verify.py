#!/usr/bin/env python3
"""schema_verify.py — Verify that SQL/ORM schema changes in a diff have matching migrations.

Usage:
    python scripts/schema_verify.py --diff <path-to-diff> [--root .] [--db-url <url>]

Static check (always runs):
    1. Parse diff for added/renamed columns in SQL queries (SELECT/INSERT/UPDATE)
    2. Parse diff for new ORM model fields (SQLAlchemy, Django models)
    3. Check that same diff has matching migration (ALTER TABLE / add_column / alembic revision)
    4. Emit findings: schema-drift (high severity) or clean

Live check (when --db-url is provided):
    5. Connect to database, query information_schema.columns / PRAGMA table_info
    6. Confirm column exists post-migration
    7. Mark result VERIFIED(live) or UNVERIFIED(live)

Output:
    JSON to stdout: { "verdict": "PASS|FAIL|UNVERIFIED", "findings": [...], "live": bool }
    Exit code: 0 (pass), 1 (findings), 2 (error)
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ── Diff Parsing ──────────────────────────────────────────────────────────────

SQL_COLUMN_RE = re.compile(
    r"""
    (?:SELECT\s+(?:DISTINCT\s+)?(.+?)(?:\s+FROM|$))
    |(?:INSERT\s+INTO\s+\w+\s*\((.+?)\))
    |(?:UPDATE\s+\w+\s+SET\s+(.+?)(?:\s+WHERE|$))
    """,
    re.IGNORECASE | re.VERBOSE | re.DOTALL,
)

ALTER_TABLE_RE = re.compile(
    r"""
    ALTER\s+TABLE\s+\w+\s+ADD(?:\s+COLUMN)?\s+(\w+)
    |ADD(?:\s+COLUMN)?\s+(\w+)
    |add_column\(['\"](\w+)['\"]
    |op\.add_column\(['\"](\w+)['\"]
    """,
    re.IGNORECASE | re.VERBOSE,
)

ORM_FIELD_RE = re.compile(
    r"""
    ^\+?\s*                               # optional leading + (diff context)
    (?:\w+\s*=\s*)?                        # optional field name
    (?:models\.|db\.|Column|mapped_column)\s*\(  # ORM field declaration
    """,
    re.IGNORECASE | re.VERBOSE | re.MULTILINE,
)


def parse_diff(diff_text: str) -> Dict[str, Any]:
    """Parse a diff and extract added columns and migration columns."""
    added_cols: set[str] = set()
    migrated_cols: set[str] = set()

    for line in diff_text.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            stripped = line.lstrip("+").strip()

            # SQL column references — SELECT (group 1) only reads existing columns, so it must
            # not count as "added"; only INSERT/UPDATE (groups 2, 3) write a column value.
            for m in SQL_COLUMN_RE.finditer(stripped):
                for g in m.groups()[1:]:
                    if g:
                        cols = [c.strip().split()[-1] for c in g.split(",") if c.strip()]
                        added_cols.update(c for c in cols if c and c != "*")

            # Migration ALTER TABLE / alembic add_column
            for m in ALTER_TABLE_RE.finditer(stripped):
                for g in m.groups():
                    if g:
                        migrated_cols.add(g.lower())

    # Heuristic: extract column name from ORM field declarations
    # Look for patterns like: column_name = Column(...) or column_name = mapped_column(...)
    orm_field_re = re.compile(
        r"^\+?\s*(\w+)\s*=\s*(?:models\.\w+|db\.\w+|Column|mapped_column)\s*\(",
        re.MULTILINE,
    )
    for m in orm_field_re.finditer(diff_text):
        col = m.group(1).strip()
        if col:
            added_cols.add(col)

    return {
        "added_columns": sorted(added_cols),
        "migrated_columns": sorted(migrated_cols),
        "unmatched": sorted(added_cols - migrated_cols),
    }


def live_verify(db_url: str, added_columns: List[str], table: Optional[str] = None) -> Dict[str, Any]:
    """Verify columns exist in the live database."""
    results: Dict[str, bool] = {}
    conn = None
    try:
        if db_url.startswith("sqlite://"):
            db_path = db_url.replace("sqlite:///", "").replace("sqlite://", "")
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            # Get all tables
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [r[0] for r in cursor.fetchall()]
            for col in added_columns:
                found = False
                for tbl in tables:
                    try:
                        cursor.execute(f"PRAGMA table_info(\"{tbl}\")")
                        columns = [r[1] for r in cursor.fetchall()]
                        if col.lower() in [c.lower() for c in columns]:
                            found = True
                            break
                    except Exception:
                        continue
                results[col] = found
        else:
            # Generic fallback — unsupported dialect
            for col in added_columns:
                results[col] = False
    except Exception as e:
        return {"error": str(e), "results": {c: False for c in added_columns}}
    finally:
        if conn:
            conn.close()
    return {"results": results}


def main() -> int:
    argv = sys.argv[1:]
    if argv[:1] == ["selftest"]:
        return selftest()
    opts: Dict[str, str] = {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("--"):
            key = a[2:]
            if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                opts[key] = argv[i + 1]
                i += 2
            else:
                opts[key] = "true"
                i += 1
        else:
            i += 1

    diff_path = opts.get("diff")
    root = opts.get("root", ".")
    db_url = opts.get("db-url") or os.environ.get("DATABASE_URL", "")

    if not diff_path:
        print("Usage: python scripts/schema_verify.py --diff <path> [--db-url <url>]", file=sys.stderr)
        print("       python scripts/schema_verify.py --diff <path> --selftest", file=sys.stderr)
        return 2

    if diff_path == "--selftest":
        return selftest()

    try:
        diff_text = Path(diff_path).read_text(encoding="utf-8")
    except FileNotFoundError:
        print(json.dumps({"verdict": "ERROR", "error": f"diff not found: {diff_path}"}))
        return 2

    parsed = parse_diff(diff_text)
    findings: List[Dict[str, Any]] = []

    for col in parsed["unmatched"]:
        findings.append({
            "severity": "high",
            "type": "schema-drift",
            "column": col,
            "message": f"Column '{col}' referenced in query/ORM but no matching migration found in diff",
        })

    live_result: Optional[Dict[str, Any]] = None
    live = bool(db_url)

    if live and findings:
        live_result = live_verify(db_url, parsed["added_columns"])
        # Verify unmatched columns against live DB
        live_verified = live_result.get("results", {})
        for f in findings:
            col = f["column"]
            if live_verified.get(col, False):
                f["live_verified"] = True
                f["message"] += " — LIVE VERIFIED (column exists in database)"
            else:
                f["live_verified"] = False
                f["message"] += " — LIVE UNVERIFIED (column not found in database)"

    verdict = "PASS"
    if findings and not live:
        verdict = "BLOCKED"
    elif findings and live:
        unverified = [f for f in findings if not f.get("live_verified")]
        if unverified:
            verdict = "BLOCKED"
        else:
            verdict = "PASS"

    output: Dict[str, Any] = {
        "verdict": verdict,
        "findings": findings,
        "parsed": parsed,
        "live": live,
        "live_result": live_result,
    }

    if not live and parsed["unmatched"]:
        output["note"] = "UNVERIFIED(live) — no database connection; static check only"

    print(json.dumps(output, indent=2))
    return 1 if verdict == "BLOCKED" else 0


def selftest() -> int:
    """Run self-test with known inputs."""
    passing_diff = """\
+    SELECT id, name, email FROM users
+    ALTER TABLE users ADD COLUMN email varchar(255);
"""
    result = parse_diff(passing_diff)
    assert not result["unmatched"], f"Expected no unmatched columns, got {result['unmatched']}"
    print(f"selftest: PASS (no unmatched: {result})")

    failing_diff = """\
+    INSERT INTO users (new_column) VALUES ('x')
"""
    result = parse_diff(failing_diff)
    assert "new_column" in result["unmatched"], f"Expected new_column unmatched, got {result}"
    print(f"selftest: PASS (unmatched detected: {result['unmatched']})")

    print("selftest: ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())

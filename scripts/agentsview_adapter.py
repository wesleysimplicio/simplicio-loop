#!/usr/bin/env python3
"""agentsview source_adapter — session analytics & cost observability for simplicio-loop.

A concrete binding of the `source_adapter` extension point for agentsview (kenn-io).
Connects to agentsview's local SQLite database or HTTP API to discover stalled agent sessions
and report cost data for billing/observability.

Usage:
    python3 scripts/agentsview_adapter.py list_ready [--dry-run]
    python3 scripts/agentsview_adapter.py get_details --id <session-id> [--dry-run]
    python3 scripts/agentsview_adapter.py claim --id <session-id> [--dry-run]
    python3 scripts/agentsview_adapter.py update_status --id <session-id> --state <state> [--dry-run]
    python3 scripts/agentsview_adapter.py attach_evidence --id <session-id> --note <note> [--dry-run]
    python3 scripts/agentsview_adapter.py close --id <session-id> [--dry-run]
    python3 scripts/agentsview_adapter.py cost_summary [--days 7] [--dry-run]
    python3 scripts/agentsview_adapter.py agent_breakdown [--days 7] [--dry-run]

Environment:
    AGENTSVIEW_DB     Path to agentsview SQLite DB (default: auto-detect)
    AGENTSVIEW_API    agentsview HTTP API base URL (default: http://127.0.0.1:8080)
    AGENTSVIEW_MODE   Connection mode: 'sqlite' (default) or 'http'
"""
import argparse
import json
import os
import sqlite3
import subprocess
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))


def detect_db_path():
    candidates = [
        os.environ.get("AGENTSVIEW_DB", ""),
        os.path.expanduser("~/.agentsview/data.db"),
        os.path.expanduser("~/Library/Application Support/agentsview/data.db"),
    ]
    for p in candidates:
        if p and os.path.isfile(p):
            return p
    # fallback
    return candidates[1] if candidates[1] else candidates[0]


def mode():
    return os.environ.get("AGENTSVIEW_MODE", "sqlite")


def api_base():
    return os.environ.get("AGENTSVIEW_API", "http://127.0.0.1:8080")


def sqlite_query(db, query, params=()):
    if not os.path.isfile(db):
        return {"error": f"agentsview DB not found at {db}"}
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    cur = conn.execute(query, params)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def http_get(path, params=None):
    url = f"{api_base()}{path}"
    if params:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        url += f"?{qs}"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {"error": str(e)}


def http_patch(path, data):
    url = f"{api_base()}{path}"
    body = json.dumps(data).encode()
    req = urllib.request.Request(url, data=body, method="PATCH")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {"error": str(e)}


def list_ready(mode, db, dry_run=False):
    """List stalled/incomplete sessions (metadata only)."""
    if dry_run:
        q = ("SELECT s.id, s.agent, s.project, s.started_at, s.updated_at, "
             "s.status FROM sessions s WHERE s.status != 'completed' "
             "ORDER BY s.updated_at DESC LIMIT 50")
        print(f"[DRY-RUN] SQL: {q}")
        print("[DRY-RUN] API: GET /api/v1/sessions?filter=status!=completed")
        return

    if mode == "sqlite":
        rows = sqlite_query(db, """
            SELECT s.id, s.agent, s.project, s.started_at, s.updated_at, s.status
            FROM sessions s
            WHERE s.status IS NULL OR s.status != 'completed'
            ORDER BY s.updated_at DESC
            LIMIT 50
        """)
        print(json.dumps(rows, indent=2, default=str))
    else:
        data = http_get("/api/v1/sessions", {"filter": "status!=completed"})
        print(json.dumps(data, indent=2, default=str))


def get_details(mode, db, session_id, dry_run=False):
    """Full session detail with messages."""
    if dry_run:
        if mode == "sqlite":
            print(f"[DRY-RUN] SQL: SELECT * FROM sessions s LEFT JOIN messages m "
                  f"ON m.session_id = s.id WHERE s.id = '{session_id}'")
        else:
            print(f"[DRY-RUN] API: GET /api/v1/sessions/{session_id}")
        return

    if mode == "sqlite":
        rows = sqlite_query(db, """
            SELECT s.*, m.role, m.content_preview, m.cost_estimate, m.duration_ms
            FROM sessions s
            LEFT JOIN messages m ON m.session_id = s.id
            WHERE s.id = ?
            ORDER BY m.created_at ASC
        """, (session_id,))
        print(json.dumps(rows, indent=2, default=str))
    else:
        data = http_get(f"/api/v1/sessions/{session_id}")
        print(json.dumps(data, indent=2, default=str))


def claim(mode, db, session_id, dry_run=False):
    """Atomic claim on a session (mark as in-progress)."""
    if dry_run:
        print(f"[DRY-RUN] Mark session {session_id} as in-progress")
        return

    if mode == "sqlite":
        sqlite_query(db, "UPDATE sessions SET status = 'in-progress' WHERE id = ?",
                     (session_id,))
        print(json.dumps({"status": "claimed", "id": session_id}))
    else:
        data = http_patch(f"/api/v1/sessions/{session_id}", {"status": "in-progress"})
        print(json.dumps(data, indent=2, default=str))


def update_status(mode, db, session_id, state, dry_run=False):
    """Update session status."""
    if dry_run:
        print(f"[DRY-RUN] Update session {session_id} status -> {state}")
        return

    if mode == "sqlite":
        sqlite_query(db, "UPDATE sessions SET status = ? WHERE id = ?",
                     (state, session_id))
        print(json.dumps({"status": "updated", "id": session_id, "new_state": state}))
    else:
        data = http_patch(f"/api/v1/sessions/{session_id}", {"status": state})
        print(json.dumps(data, indent=2, default=str))


def attach_evidence(mode, db, session_id, note, dry_run=False):
    """Attach evidence note to a session."""
    if dry_run:
        print(f"[DRY-RUN] Attach evidence to {session_id}: {note}")
        return

    if mode == "sqlite":
        sqlite_query(db, "UPDATE sessions SET evidence_note = ? WHERE id = ?",
                     (note, session_id))
        print(json.dumps({"status": "evidence_attached", "id": session_id}))
    else:
        data = http_patch(f"/api/v1/sessions/{session_id}", {"evidence": note})
        print(json.dumps(data, indent=2, default=str))


def close(mode, db, session_id, dry_run=False):
    """Close a session as completed."""
    if dry_run:
        print(f"[DRY-RUN] Close session {session_id} as completed")
        return

    if mode == "sqlite":
        sqlite_query(db, "UPDATE sessions SET status = 'completed' WHERE id = ?",
                     (session_id,))
        print(json.dumps({"status": "closed", "id": session_id}))
    else:
        data = http_patch(f"/api/v1/sessions/{session_id}",
                          {"status": "completed", "outcome": "merged"})
        print(json.dumps(data, indent=2, default=str))


def cost_summary(mode, db, days=7, dry_run=False):
    """Daily cost summary for billing/observability."""
    if dry_run:
        if mode == "sqlite":
            print(f"[DRY-RUN] SQL: daily cost summary for last {days} days")
        else:
            print(f"[DRY-RUN] API: GET /api/v1/usage/daily?days={days}")
        return

    if mode == "sqlite":
        rows = sqlite_query(db, """
            SELECT DATE(m.created_at) AS day,
                   COUNT(DISTINCT m.session_id) AS sessions,
                   SUM(m.cost_estimate) AS total_cost,
                   SUM(m.duration_ms) / 60000.0 AS total_minutes
            FROM messages m
            WHERE m.created_at > datetime('now', ? || ' days')
            GROUP BY DATE(m.created_at)
            ORDER BY day DESC
        """, (f"-{days}",))
        print(json.dumps(rows, indent=2, default=str))
    else:
        data = http_get("/api/v1/usage/daily", {"days": str(days)})
        print(json.dumps(data, indent=2, default=str))


def agent_breakdown(mode, db, days=7, dry_run=False):
    """Agent usage breakdown."""
    if dry_run:
        print(f"[DRY-RUN] Agent usage breakdown for last {days} days")
        return

    if mode == "sqlite":
        rows = sqlite_query(db, """
            SELECT s.agent, COUNT(*) AS session_count,
                   AVG(CAST((julianday('now') - julianday(s.started_at)) * 24 AS REAL)) AS avg_hours,
                   SUM(m.cost_estimate) AS total_cost
            FROM sessions s
            LEFT JOIN messages m ON m.session_id = s.id
            WHERE s.started_at > datetime('now', ? || ' days')
            GROUP BY s.agent
            ORDER BY total_cost DESC
        """, (f"-{days}",))
        print(json.dumps(rows, indent=2, default=str))
    else:
        data = http_get("/api/v1/agents")
        print(json.dumps(data, indent=2, default=str))


def main():
    parser = argparse.ArgumentParser(
        description="agentsview source_adapter for simplicio-loop")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("list_ready", help="List stalled/incomplete sessions")
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("get_details", help="Full session detail")
    p.add_argument("--id", required=True)
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("claim", help="Claim a session")
    p.add_argument("--id", required=True)
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("update_status", help="Update session status")
    p.add_argument("--id", required=True)
    p.add_argument("--state", required=True, choices=["stalled", "resumed", "completed"])
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("attach_evidence", help="Attach evidence to session")
    p.add_argument("--id", required=True)
    p.add_argument("--note", required=True)
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("close", help="Close session as completed")
    p.add_argument("--id", required=True)
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("cost_summary", help="Daily cost summary")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("agent_breakdown", help="Agent usage breakdown")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--dry-run", action="store_true")

    args = parser.parse_args()
    db = detect_db_path()
    m = mode()

    dispatch = {
        "list_ready": lambda: list_ready(m, db, args.dry_run),
        "get_details": lambda: get_details(m, db, args.id, args.dry_run),
        "claim": lambda: claim(m, db, args.id, args.dry_run),
        "update_status": lambda: update_status(m, db, args.id, args.state, args.dry_run),
        "attach_evidence": lambda: attach_evidence(m, db, args.id, args.note, args.dry_run),
        "close": lambda: close(m, db, args.id, args.dry_run),
        "cost_summary": lambda: cost_summary(m, db, args.days, args.dry_run),
        "agent_breakdown": lambda: agent_breakdown(m, db, args.days, args.dry_run),
    }

    dispatch[args.command]()  # coverage: no branch


if __name__ == "__main__":
    main()

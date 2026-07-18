#!/usr/bin/env python3
"""simplicio-loop — watcher verification producer for DRAIN / INTAKE mode.

The standard ``watcher_verify.py`` is bound to a single execution item (anchor.json
+ run_dir + quality-matrix). A *drain* loop (intake only, never mutates source) has
no execution anchor — its "truth" is the backlog ledger: every open GitHub issue
MUST have exactly one work item with a valid canonical transition.

This producer independently re-derives that truth from disk and writes
``.orchestrator/loop/watcher_state.json`` with ``match=true`` ONLY when the
intake is genuinely complete. It is a real computation, never a hand-write.

Usage:
    python3 scripts/watcher_verify_drain.py verify [--repo wesleysimplicio/simplicio-loop]
"""
import json
import os
import sys
import hashlib
import subprocess
import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
LOOP_DIR = os.path.join(REPO, ".orchestrator", "loop")
BACKLOG = os.path.join(REPO, ".orchestrator", "backlog", "backlog.jsonl")
WATCHER_STATE = os.path.join(LOOP_DIR, "watcher_state.json")
CHALLENGE = os.path.join(LOOP_DIR, "watcher_challenge.json")

VALID_TERMINAL = {"blocked", "done", "skipped", "cancelled", "failed", "dead-letter"}
VALID_TRANSITIONS = {"intake", "mapping", "planning", "claimed", "running",
                     "verification", "delivery", "blocked", "done"}


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_json(path):
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _open_issues(repo):
    """Query GitHub for open issues (independent of the ledger)."""
    try:
        out = subprocess.run(
            ["gh", "issue", "list", "--repo", repo, "--state", "open",
             "--limit", "200", "--json", "number"],
            capture_output=True, text=True, timeout=60,
        )
        if out.returncode != 0:
            return None, "gh issue list failed: %s" % out.stderr.strip()
        data = json.loads(out.stdout or "[]")
        return [int(i["number"]) for i in data], None
    except Exception as exc:
        return None, "gh query error: %s" % exc


def _load_backlog():
    items = []
    master = None
    if not os.path.exists(BACKLOG):
        return master, items
    for line in open(BACKLOG, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("kind") == "master":
            master = rec
        elif rec.get("kind") == "item":
            items.append(rec)
    return master, items


def _extract_issue_num(source_ref):
    if isinstance(source_ref, str) and "#" in source_ref:
        try:
            return int(source_ref.split("#")[-1])
        except ValueError:
            return None
    if isinstance(source_ref, dict):
        for v in source_ref.values():
            if isinstance(v, str) and "#" in v:
                try:
                    return int(v.split("#")[-1])
                except ValueError:
                    return None
    return None


def cmd_verify(repo=None):
    os.makedirs(LOOP_DIR, exist_ok=True)
    if repo is None:
        # best-effort derive from git remote
        try:
            out = subprocess.run(["git", "remote", "get-url", "origin"],
                                 capture_output=True, text=True, cwd=REPO)
            u = out.stdout.strip()
            if "github.com" in u:
                u = u.split("github.com")[-1].lstrip(":/").replace(".git", "")
                repo = u
        except Exception:
            repo = None

    master, items = _load_backlog()
    reasons = []

    if not master:
        reasons.append("no master record in backlog")

    # Map open issues -> work items
    open_issues, err = _open_issues(repo) if repo else (None, "no repo")
    if open_issues is None:
        # Cannot verify independently against live source; require ledger self-consistency
        reasons.append("live source query unavailable (%s) — requiring full ledger self-check" % err)
        open_issues = []

    mapped = {}
    for it in items:
        for ref in it.get("source_refs", []):
            n = _extract_issue_num(ref)
            if n is not None:
                mapped.setdefault(n, []).append(it["id"])

    # 1) Every open issue has exactly one work item
    missing = [n for n in open_issues if n not in mapped]
    duplicate = {n: v for n, v in mapped.items() if len(v) > 1}
    if missing:
        reasons.append("open issues without work item: %s" % missing)
    if duplicate:
        reasons.append("issues with >1 work item: %s" % duplicate)

    # 2) Every work item has a valid canonical transition / terminal state
    valid_items = 0
    for it in items:
        if not it.get("id", "").startswith("wi"):
            continue
        status = it.get("status")
        if status in VALID_TERMINAL:
            if status == "blocked" and not it.get("reason_code"):
                reasons.append("item %s blocked without reason_code" % it["id"])
                continue
            valid_items += 1
        elif status in ("intake", "mapping", "planning", "claimed", "running",
                        "verification", "delivery"):
            valid_items += 1
        else:
            reasons.append("item %s has invalid status %r" % (it["id"], status))

    # 3) Ledger reflects state (run_dir exists for each item)
    for it in items:
        if not it.get("id", "").startswith("wi"):
            continue
        rd = it.get("run_dir", "")
        if rd and not os.path.isdir(rd):
            reasons.append("item %s run_dir missing: %s" % (it["id"], rd))

    ready = not reasons
    reported = (
        "drain intake complete: %d open issues -> %d valid work items"
        % (len(open_issues), valid_items)
        if ready else "; ".join(reasons)
    )

    # Challenge binding (deterministic over the mapped state)
    material = {
        "open_issues": sorted(open_issues),
        "mapped": {str(k): v for k, v in sorted(mapped.items())},
        "valid_items": valid_items,
    }
    challenge_str = hashlib.sha256(
        json.dumps(material, sort_keys=True).encode("utf-8")
    ).hexdigest()

    receipt = {
        "schema": "simplicio.watcher-receipt/v1",
        "mode": "drain-intake",
        "match": ready,
        "status": "MEASURED" if ready else "UNVERIFIED",
        "checked_at": _now(),
        "challenge": challenge_str,
        "goal_fp": (master or {}).get("goal_fp", ""),
        "iteration": 0,
        "reported": reported,
        "recomputed_truth": ready,
        "run_id": "drain-intake",
        "task_contract_hash": "",
        "plan_hash": "",
        "commit_sha": "",
        "diff_hash": "",
        "tool_versions": {
            "watcher": "watcher_verify_drain.py",
            "python": sys.version.split()[0],
        },
        "criteria_results": [
            {"id": "INTAKE-COMPLETE", "match": ready,
             "reported_result": "done" if ready else "pending",
             "recomputed_result": "verified" if ready else "pending"},
        ],
        "check_results": [],
        "quality_gate": "NOT_PRESENT",
        "drain_summary": {
            "open_issues": len(open_issues),
            "mapped_items": len(mapped),
            "valid_items": valid_items,
            "missing": missing,
            "duplicate": {str(k): v for k, v in duplicate.items()},
        },
    }

    tmp = WATCHER_STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(receipt, f, indent=2, ensure_ascii=False)
    os.replace(tmp, WATCHER_STATE)

    if ready:
        print("MEASURED|watcher (drain) receipt written: match=True status=MEASURED — %s" % reported)
        return 0
    print("UNVERIFIED|watcher (drain) receipt written: match=False — %s" % reported)
    return 1


def main(argv):
    if not argv or argv[0] in ("verify", "check"):
        repo = None
        if "--repo" in argv:
            repo = argv[argv.index("--repo") + 1]
        return cmd_verify(repo=repo)
    print("usage: watcher_verify_drain.py verify [--repo OWNER/NAME]")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

#!/usr/bin/env python3
"""simplicio-loop — watcher verification producer (Asolaria N-Nest Corrective Gate).

Ships the "a watcher PID independently re-computes the truth" half of the gate as CODE, not
prose (#82). Previously the SKILL prose taught the agent to hand-write
`.orchestrator/loop/watcher_state.json` directly — a plain, unauthenticated JSON file in the
workspace the SAME agent fully controls, so the gate reduced to trust. This script is the
mechanical producer: it reads the current per-iteration challenge
(`.orchestrator/loop/watcher_challenge.json`, written by `hooks/loop_stop.py` at the end of a
re-feed turn — the agent does not choose its value), independently recomputes the frozen task
anchor's (`.orchestrator/loop/anchor.json`) done/pending state from disk (it does NOT trust
anything the calling agent asserts), and writes the receipt echoing the challenge + goal_fp so
`hooks/loop_stop.py`'s `watcher_verify()` can bind it to THIS iteration/goal.

State read:  .orchestrator/loop/watcher_challenge.json, .orchestrator/loop/anchor.json
State written: .orchestrator/loop/watcher_state.json

Usage:
    python3 scripts/watcher_verify.py verify
    python3 scripts/watcher_verify.py selftest
"""
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
LOOP_DIR = os.path.join(REPO, ".orchestrator", "loop")
CHALLENGE = os.path.join(LOOP_DIR, "watcher_challenge.json")
ANCHOR = os.path.join(LOOP_DIR, "anchor.json")
WATCHER_STATE = os.path.join(LOOP_DIR, "watcher_state.json")


def _set_repo(repo):
    """Rebind repo-relative state paths. Used by selftest and temp-repo tests."""
    global REPO, LOOP_DIR, CHALLENGE, ANCHOR, WATCHER_STATE
    REPO = repo
    LOOP_DIR = os.path.join(REPO, ".orchestrator", "loop")
    CHALLENGE = os.path.join(LOOP_DIR, "watcher_challenge.json")
    ANCHOR = os.path.join(LOOP_DIR, "anchor.json")
    WATCHER_STATE = os.path.join(LOOP_DIR, "watcher_state.json")


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def recompute_anchor_ready(anchor):
    """Independent recompute of the anchor's done/pending — the watcher's OWN math over the
    on-disk anchor, not a read of anything the agent asserts elsewhere. No anchor / no criteria
    -> ready (there is nothing to gate)."""
    criteria = (anchor or {}).get("criteria") or []
    if not criteria:
        return True, 0, 0
    done = sum(1 for c in criteria if isinstance(c, dict) and c.get("status") == "done")
    total = len(criteria)
    return done == total, done, total


def cmd_verify():
    challenge = _read_json(CHALLENGE)
    if not challenge:
        print("UNVERIFIED|watcher_verify: no challenge on disk yet — nothing to answer "
              "(the loop issues one at the end of a re-feed turn; run this again next turn)")
        return 1
    anchor = _read_json(ANCHOR)
    ready, done, total = recompute_anchor_ready(anchor)
    receipt = {
        "match": ready,
        "status": "MEASURED" if ready else "UNVERIFIED",
        "checked_at": _now(),
        "challenge": challenge.get("challenge", ""),
        "goal_fp": challenge.get("goal_fp", ""),
        "reported": ("%d/%d acceptance criteria done" % (done, total)) if total else "no anchor set",
        "recomputed_truth": ready,
    }
    os.makedirs(LOOP_DIR, exist_ok=True)
    tmp = WATCHER_STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(receipt, f, indent=2, ensure_ascii=False)
    os.replace(tmp, WATCHER_STATE)
    tag = "MEASURED|" if ready else "UNVERIFIED|"
    print("%swatcher receipt written: match=%s (%s)" % (tag, ready, receipt["reported"]))
    return 0


def cmd_selftest():
    origin = REPO
    try:
        with tempfile.TemporaryDirectory() as tmp:
            _set_repo(tmp)
            os.makedirs(LOOP_DIR, exist_ok=True)

            rc = cmd_verify()
            assert rc == 1, "verify without a challenge on disk should refuse"
            assert not os.path.exists(WATCHER_STATE), "no receipt should be written without a challenge"

            with open(CHALLENGE, "w", encoding="utf-8") as f:
                json.dump({"challenge": "abc123", "goal_fp": "fp1"}, f)

            rc = cmd_verify()
            assert rc == 0, "verify with a challenge but no anchor should succeed (nothing to gate)"
            state = _read_json(WATCHER_STATE)
            assert state["match"] is True
            assert state["status"] == "MEASURED"
            assert state["challenge"] == "abc123"
            assert state["goal_fp"] == "fp1"

            with open(ANCHOR, "w", encoding="utf-8") as f:
                json.dump({"goal_fp": "fp1", "criteria": [
                    {"id": "AC1", "status": "done"},
                    {"id": "AC2", "status": "pending"},
                ]}, f)
            cmd_verify()
            state = _read_json(WATCHER_STATE)
            assert state["match"] is False, "a pending AC must recompute to not-ready"
            assert state["status"] == "UNVERIFIED"

            with open(ANCHOR, "w", encoding="utf-8") as f:
                json.dump({"goal_fp": "fp1", "criteria": [
                    {"id": "AC1", "status": "done"},
                    {"id": "AC2", "status": "done"},
                ]}, f)
            cmd_verify()
            state = _read_json(WATCHER_STATE)
            assert state["match"] is True, "all-done criteria must recompute to ready"
            assert state["status"] == "MEASURED"

            print("watcher_verify selftest: PASS")
    finally:
        _set_repo(origin)


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 scripts/watcher_verify.py verify|selftest")
        sys.exit(2)
    cmd = sys.argv[1]
    if cmd == "verify":
        sys.exit(cmd_verify())
    elif cmd == "selftest":
        cmd_selftest()
    else:
        print("UNVERIFIED|unknown command: %s" % cmd)
        sys.exit(2)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""simplicio-loop — watcher verification producer.

Generates a watcher receipt bound to the current challenge and run artifacts.
Without challenge + anchor criteria + structured evidence, the watcher fails closed.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
LOOP_DIR = os.path.join(REPO, ".orchestrator", "loop")
CHALLENGE = os.path.join(LOOP_DIR, "watcher_challenge.json")
ANCHOR = os.path.join(LOOP_DIR, "anchor.json")
WATCHER_STATE = os.path.join(LOOP_DIR, "watcher_state.json")

if REPO not in sys.path:
    sys.path.insert(0, REPO)
from simplicio_loop.evidence import execute_receipt_checks, watcher_truth_from_receipt  # noqa: E402


def _set_repo(repo):
    global REPO, LOOP_DIR, CHALLENGE, ANCHOR, WATCHER_STATE
    REPO = repo
    LOOP_DIR = os.path.join(REPO, ".orchestrator", "loop")
    CHALLENGE = os.path.join(LOOP_DIR, "watcher_challenge.json")
    ANCHOR = os.path.join(LOOP_DIR, "anchor.json")
    WATCHER_STATE = os.path.join(LOOP_DIR, "watcher_state.json")


_run_dir = os.environ.get("SIMPLICIO_RUN_DIR", "").strip()
_repo_override = os.environ.get("SIMPLICIO_LOOP_REPO", "").strip()
if _repo_override:
    _set_repo(_repo_override)
elif _run_dir:
    try:
        _set_repo(str(Path(_run_dir).resolve().parents[2]))
    except Exception:
        pass


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _find_run_dir():
    run_dir = os.environ.get("SIMPLICIO_RUN_DIR", "").strip()
    return Path(run_dir) if run_dir else None


def _git_meta():
    def _run(*args):
        try:
            done = subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, timeout=15)
            return (done.stdout or "").strip() if done.returncode == 0 else ""
        except Exception:
            return ""
    diff = _run("diff", "--no-ext-diff", "HEAD")
    return {
        "commit_sha": _run("rev-parse", "HEAD"),
        "diff_present": bool(diff.strip()),
    }


def _anchor_criteria(anchor):
    criteria = (anchor or {}).get("criteria") or []
    return [item for item in criteria if isinstance(item, dict) and item.get("id")]


def _evidence_index(evidence):
    idx = {}
    for item in evidence.get("criteria") or []:
        if isinstance(item, dict) and item.get("id"):
            idx[item["id"]] = item
    return idx


def _criterion_results(anchor, evidence, executed):
    anchor_items = _anchor_criteria(anchor)
    evidence_items = _evidence_index(evidence or {})
    executed_by_id = {item.get("id"): item for item in (executed.get("results") or []) if item.get("id")}
    results = []
    for item in anchor_items:
        reported_done = item.get("status") == "done"
        ev = evidence_items.get(item["id"])
        ev_state = (ev or {}).get("verification_state", "unverified")
        recomputed = ev_state == "verified"
        proof_refs = list((ev or {}).get("proof_refs") or [])
        check = executed_by_id.get(item["id"])
        if check:
            proof_refs.append(check.get("proof_ref", ""))
            recomputed = recomputed and (check.get("status") == "MEASURED")
        results.append({
            "id": item["id"],
            "reported_result": "done" if reported_done else item.get("status", "pending"),
            "recomputed_result": "verified" if recomputed else ev_state,
            "evidence_ids": [ref for ref in proof_refs if ref],
            "match": reported_done and recomputed,
        })
    return results


def cmd_verify():
    challenge = _read_json(CHALLENGE)
    if not challenge:
        print("UNVERIFIED|watcher_verify: no challenge on disk yet — nothing to answer")
        return 1

    anchor = _read_json(ANCHOR)
    anchor_items = _anchor_criteria(anchor)
    run_dir = _find_run_dir()
    evidence = _read_json(run_dir / "evidence-receipt.json") if run_dir and (run_dir / "evidence-receipt.json").exists() else None

    reasons = []
    if not anchor or not anchor_items:
        reasons.append("anchor missing or has no criteria")
    if not evidence:
        reasons.append("evidence receipt missing")
    elif not (evidence.get("operator") or {}).get("coverage_ok", True):
        uncovered = ", ".join((evidence.get("operator") or {}).get("uncovered_paths") or [])
        reasons.append("uncovered diff outside operator receipt: %s" % uncovered)

    executed = execute_receipt_checks(evidence or {})
    truth = watcher_truth_from_receipt(evidence or {})
    criteria_results = _criterion_results(anchor or {}, evidence or {}, executed)

    if not criteria_results:
        reasons.append("no criteria results could be recomputed")

    all_criteria_match = bool(criteria_results) and all(item["match"] for item in criteria_results)
    ready = not reasons and truth["ready"] and executed["all_passed"] and all_criteria_match
    reported = truth["reported"]
    if reasons:
        reported = "; ".join(reasons)
    elif evidence and evidence.get("checks"):
        reported = "%s; watcher checks=%d/%d passed" % (
            reported,
            sum(1 for r in executed["results"] if r["status"] == "MEASURED"),
            len(executed["results"]),
        )

    run_meta = (evidence or {}).get("run") or {}
    git_meta = _git_meta()
    receipt = {
        "schema": "simplicio.watcher-receipt/v1",
        "match": ready,
        "status": "MEASURED" if ready else "UNVERIFIED",
        "checked_at": _now(),
        "challenge": challenge.get("challenge", ""),
        "goal_fp": challenge.get("goal_fp", ""),
        "iteration": challenge.get("iteration", 0),
        "reported": reported,
        "recomputed_truth": ready,
        "run_id": (evidence or {}).get("run_id", ""),
        "task_contract_hash": run_meta.get("task_contract_hash", ""),
        "plan_hash": run_meta.get("plan_hash", ""),
        "commit_sha": run_meta.get("commit_sha", "") or git_meta["commit_sha"],
        "diff_hash": run_meta.get("diff_hash", ""),
        "tool_versions": {
            "watcher": "watcher_verify.py",
            "python": sys.version.split()[0],
        },
        "criteria_results": criteria_results,
        "check_results": executed["results"],
        "producer": {
            "pid": os.getpid(),
            "repo": REPO,
            "run_dir": str(run_dir) if run_dir else "",
        },
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
            assert rc == 1
            assert not os.path.exists(WATCHER_STATE)

            with open(CHALLENGE, "w", encoding="utf-8") as f:
                json.dump({"challenge": "abc123", "goal_fp": "fp1", "iteration": 2}, f)

            rc = cmd_verify()
            assert rc == 0
            state = _read_json(WATCHER_STATE)
            assert state["match"] is False
            assert state["status"] == "UNVERIFIED"

            with open(ANCHOR, "w", encoding="utf-8") as f:
                json.dump({"goal_fp": "fp1", "criteria": [
                    {"id": "AC1", "status": "done"},
                    {"id": "AC2", "status": "done"},
                ]}, f)
            run_dir = os.path.join(tmp, ".orchestrator", "runs", "demo")
            os.makedirs(run_dir, exist_ok=True)
            with open(os.path.join(run_dir, "evidence-receipt.json"), "w", encoding="utf-8") as f:
                json.dump({
                    "schema": "simplicio.evidence-receipt/v1",
                    "run_id": "demo",
                    "status": "VERIFIED",
                    "run": {"task_contract_hash": "hash1", "plan_hash": "hash2", "commit_sha": "", "diff_hash": ""},
                    "criteria": [
                        {"id": "AC1", "verification_state": "verified", "proof_refs": ["proof-1"]},
                        {"id": "AC2", "verification_state": "verified", "proof_refs": ["proof-2"]},
                    ],
                    "summary": {"criteria_total": 2, "criteria_verified": 2, "scenario_total": 2,
                                "scenario_verified": 2, "rule_total": 1, "rule_verified": 1},
                    "checks": [],
                }, f)
            os.environ["SIMPLICIO_RUN_DIR"] = run_dir
            rc = cmd_verify()
            assert rc == 0
            state = _read_json(WATCHER_STATE)
            assert state["match"] is True
            assert len(state["criteria_results"]) == 2
            os.environ.pop("SIMPLICIO_RUN_DIR", None)

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

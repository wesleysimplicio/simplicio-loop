#!/usr/bin/env python3
"""Run a watcher verification plan in a clean, detached worktree.

The implementation worker only produces the plan.  This process owns the
recomputed result and never trusts ``verification_state`` from its input
receipt.  A plan is intentionally small and declarative so it can be stored
in a run receipt and replayed by another runtime.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import tarfile
import platform
from pathlib import Path
from typing import Any, Dict, List

if str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from simplicio_loop.evidence import _command_policy, redact_sensitive_text  # noqa: E402

PLAN_SCHEMA = "simplicio.watcher-plan/v1"
RECEIPT_SCHEMA = "simplicio.independent-watcher-receipt/v1"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _stable_hash(value: Any) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=str(root), capture_output=True,
                            text=True, stdin=subprocess.DEVNULL, timeout=30)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or "git command failed").strip())
    return (result.stdout or "").strip()


def git_observation(root: Path) -> Dict[str, Any]:
    diff = _git(root, "diff", "--no-ext-diff", "HEAD")
    return {
        "commit_sha": _git(root, "rev-parse", "HEAD"),
        "diff_hash": hashlib.sha256(diff.encode("utf-8")).hexdigest(),
        "diff_present": bool(diff.strip()),
    }


def _validate_plan(plan: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    if plan.get("schema") != PLAN_SCHEMA:
        errors.append("plan_schema_invalid")
    for field in ("challenge", "run_id", "commit_sha", "diff_hash"):
        if not str(plan.get(field) or "").strip():
            errors.append(f"plan_{field}_missing")
    if not str(plan.get("task_contract_hash") or "").strip():
        errors.append("plan_task_contract_hash_missing")
    criteria = plan.get("criteria")
    if not isinstance(criteria, list) or not criteria:
        errors.append("plan_criteria_missing")
        return errors
    ids = [str(item.get("id") or "") for item in criteria if isinstance(item, dict)]
    if any(not item for item in ids):
        errors.append("criterion_id_missing")
    if len(ids) != len(set(ids)):
        errors.append("criterion_id_duplicate")
    for item in criteria:
        if not isinstance(item, dict):
            errors.append("criterion_not_object")
            continue
        argv, reason = _command_policy(item)
        if argv is None:
            errors.append(f"criterion_{item.get('id', '?')}_command_{reason.replace(' ', '_')}")
        if "reported_result" not in item:
            errors.append(f"criterion_{item.get('id', '?')}_reported_result_missing")
        if not isinstance(item.get("evidence_ids", []), list):
            errors.append(f"criterion_{item.get('id', '?')}_evidence_ids_invalid")
    return errors


def _artifact_results(root: Path, criterion: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Recompute declared evidence artifacts inside the clean snapshot."""
    results = []
    for raw in criterion.get("artifacts", []) or []:
        path = str(raw.get("path") if isinstance(raw, dict) else raw)
        expected_sha = str(raw.get("sha256", "") if isinstance(raw, dict) else "")
        candidate = (root / path).resolve()
        safe = candidate == root.resolve() or root.resolve() in candidate.parents
        exists = safe and candidate.is_file()
        digest = hashlib.sha256(candidate.read_bytes()).hexdigest() if exists else ""
        results.append({"path": path, "exists": exists, "sha256": digest,
                        "match": exists and (not expected_sha or expected_sha == digest)})
    return results


def _run_check(root: Path, criterion: Dict[str, Any], timeout: int) -> Dict[str, Any]:
    argv, reason = _command_policy(criterion)
    cid = str(criterion.get("id") or "")
    base = {"id": cid, "reported_result": criterion.get("reported_result", "pending"),
            "recomputed_result": "pending", "evidence_ids": criterion.get("evidence_ids", [])}
    if argv is None:
        return {**base, "match": False,
                "status": "UNVERIFIED", "reason": reason,
                "returncode": None, "runner_pid": None, "watcher_pid": os.getpid(),
                "process_isolated": False}
    cwd = root / str(criterion.get("cwd") or ".")
    if not cwd.is_dir() or root not in cwd.resolve().parents and cwd.resolve() != root.resolve():
        return {**base, "match": False, "status": "UNVERIFIED", "reason": "cwd_outside_snapshot",
                "returncode": None, "runner_pid": None, "watcher_pid": os.getpid(),
                "process_isolated": False}
    expected = int(criterion.get("expected_exit_code", 0))
    runner_pid = None
    try:
        isolated_env = {key: value for key, value in os.environ.items()
                        if key not in {"SIMPLICIO_RUN_DIR", "SIMPLICIO_LOOP_REPO", "PYTHONPATH"}}
        isolated_env["PYTHONNOUSERSITE"] = "1"
        process = subprocess.Popen(argv, cwd=str(cwd), shell=False, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   env=isolated_env)
        runner_pid = process.pid
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
            return {**base, "match": False,
                    "status": "UNVERIFIED", "reason": "criterion_timeout",
                    "returncode": None, "expected_exit_code": expected,
                    "runner_pid": runner_pid, "watcher_pid": os.getpid(),
                    "process_isolated": runner_pid != os.getpid()}
        ok = process.returncode == expected
        artifacts = _artifact_results(root, criterion)
        contains = [str(value) for value in criterion.get("stdout_contains", []) or []]
        stdout = redact_sensitive_text((stdout or "").strip())[-4000:]
        stderr = redact_sensitive_text((stderr or "").strip())[-4000:]
        ok = ok and all(value in stdout for value in contains) and all(item["match"] for item in artifacts)
        return {
            **base,
            "recomputed_result": "verified" if ok else "pending",
            "match": ok,
            "status": "MEASURED" if ok else "UNVERIFIED",
            "reason": "behavior and evidence matched" if ok else "behavior or evidence failed",
            "returncode": process.returncode,
            "expected_exit_code": expected,
            "stdout": stdout,
            "stderr": stderr,
            "stdout_contains": contains,
            "artifacts": artifacts,
            "argv": argv,
            "runner_pid": runner_pid,
            "watcher_pid": os.getpid(),
            "process_isolated": runner_pid != os.getpid(),
        }
    except Exception as exc:
        return {**base, "match": False, "status": "UNVERIFIED", "reason": redact_sensitive_text(str(exc)),
                "returncode": None, "runner_pid": runner_pid,
                "watcher_pid": os.getpid(),
                "process_isolated": bool(runner_pid and runner_pid != os.getpid())}


def verify(repo: str, plan: Dict[str, Any], timeout: int = 60) -> Dict[str, Any]:
    root = Path(repo).resolve()
    errors = _validate_plan(plan)
    observation: Dict[str, Any] = {}
    try:
        observation = git_observation(root)
    except Exception as exc:
        errors.append("git_observation_failed:" + redact_sensitive_text(str(exc)))
    if observation:
        if plan.get("commit_sha") != observation["commit_sha"]:
            errors.append("commit_mismatch")
        if plan.get("diff_hash") != observation["diff_hash"]:
            errors.append("diff_mismatch")
        # A dirty implementation tree cannot be silently replaced by HEAD in
        # the watcher snapshot. The caller must commit or provide a new plan.
        if observation["diff_present"]:
            errors.append("dirty_tree_requires_committed_snapshot")

    results: List[Dict[str, Any]] = []
    snapshot = ""
    if not errors:
        with tempfile.TemporaryDirectory(prefix="simplicio-watcher-") as tmp:
            snapshot_path = Path(tmp) / "repo"
            _git(root, "archive", "--format=tar", plan["commit_sha"], "-o", str(Path(tmp) / "repo.tar"))
            snapshot_path.mkdir()
            with tarfile.open(Path(tmp) / "repo.tar") as archive:
                archive.extractall(snapshot_path)
            snapshot = str(snapshot_path)
            results = [_run_check(snapshot_path, item, timeout) for item in plan["criteria"]]
    if errors and not results:
        results = [{"id": str(item.get("id", "")),
                    "reported_result": item.get("reported_result", "pending"),
                    "recomputed_result": "pending", "evidence_ids": item.get("evidence_ids", []),
                    "match": False, "status": "UNVERIFIED", "reason": "watcher_preflight_failed"}
                   for item in plan.get("criteria", []) if isinstance(item, dict)]

    all_passed = bool(results) and not errors and all(item["status"] == "MEASURED" for item in results)
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "status": "MEASURED" if all_passed else "UNVERIFIED",
        "match": all_passed,
        "checked_at": _now(),
        "challenge": plan.get("challenge", ""),
        "run_id": plan.get("run_id", ""),
        "commit_sha": observation.get("commit_sha", plan.get("commit_sha", "")),
        "diff_hash": observation.get("diff_hash", plan.get("diff_hash", "")),
        "plan_hash": _stable_hash(plan),
        "task_contract_hash": str(plan.get("task_contract_hash") or ""),
        "verify_plan_hash": _stable_hash(plan),
        "tool_versions": {"python": platform.python_version(),
                           "platform": platform.platform(), "watcher": RECEIPT_SCHEMA},
        "criteria_results": results,
        "errors": errors,
        "producer": {"pid": os.getpid(), "snapshot": bool(snapshot), "worker": "independent_watcher.py"},
    }
    receipt["receipt_hash"] = _stable_hash(receipt)
    return receipt


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="independent_watcher")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args(argv)
    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    receipt = verify(args.repo, plan, timeout=max(1, args.timeout))
    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(("MEASURED|" if receipt["match"] else "UNVERIFIED|") +
          f"independent watcher: {receipt['status']} criteria={len(receipt['criteria_results'])}")
    return 0 if receipt["match"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

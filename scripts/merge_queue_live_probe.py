#!/usr/bin/env python3
"""Fail-closed live probe for merge-queue convergence receipts.

This probe compares a configured live endpoint against a configured receipt and
only reports ``MEASURED`` when all required merge-queue, evidence-gate and
board-convergence fields agree. Missing config or partial data stays
``UNVERIFIED`` on purpose.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

SCHEMA = "simplicio.merge-queue-live-probe/v1"
DEFAULT_TIMEOUT = 5.0
ENV_ENDPOINT = "SIMPLICIO_MERGE_QUEUE_PROBE_ENDPOINT"
ENV_RECEIPT = "SIMPLICIO_MERGE_QUEUE_PROBE_RECEIPT"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load_json_document(location: str, *, timeout: float) -> tuple[dict[str, Any], str]:
    parsed = urllib.parse.urlparse(location)
    if parsed.scheme in {"http", "https"}:
        with urllib.request.urlopen(location, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return payload, location
    path = Path(location)
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload, str(path.resolve())


def _pick(mapping: dict[str, Any], *paths: tuple[str, ...]) -> Any:
    for path in paths:
        current: Any = mapping
        found = True
        for key in path:
            if not isinstance(current, dict) or key not in current:
                found = False
                break
            current = current[key]
        if found:
            return current
    return None


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "pass", "passed", "complete", "completed", "measured", "verified", "accepted"}
    return False


def _normalized_status(value: Any) -> str:
    return str(value or "").strip().lower()


def _extract_state(payload: dict[str, Any]) -> dict[str, Any]:
    merge_queue = dict(_pick(payload, ("merge_queue",)) or {})
    board = dict(_pick(payload, ("board",), ("projection",), ("execution_board",)) or {})
    evidence = dict(_pick(payload, ("evidence_gate",), ("evidence",)) or {})
    branch = str(merge_queue.get("branch") or _pick(payload, ("merge_queue_branch",)) or "").strip()
    worktree_path = str(merge_queue.get("worktree_path") or merge_queue.get("path") or _pick(payload, ("merge_queue_worktree_path",)) or "").strip()
    tree_sha = str(merge_queue.get("tree_sha") or _pick(payload, ("merge_queue_tree_sha",)) or "").strip()
    receipt_sha = str(merge_queue.get("receipt_sha") or _pick(payload, ("merge_queue_receipt_sha",)) or "").strip()
    merge_status = _normalized_status(merge_queue.get("status") or _pick(payload, ("merge_queue_status",)))
    evidence_ready = _as_bool(
        evidence.get("ready")
        or evidence.get("verified")
        or evidence.get("passed")
        or payload.get("evidence_gate")
    )
    evidence_status = _normalized_status(evidence.get("status") or evidence.get("verdict"))
    board_status = _normalized_status(board.get("status") or payload.get("board_status"))
    board_completion = board.get("completion_percent")
    if board_completion is None:
        board_completion = _pick(board, ("summary", "completion_percent"))
    board_fronts = _as_bool(board.get("fronts_converged"))
    if not board_fronts:
        board_fronts = _as_bool(_pick(board, ("summary", "fronts_converged")))
    board_converged = _as_bool(board.get("converged")) or (board_status == "complete" and (board_fronts or board_completion == 100))
    return {
        "branch": branch,
        "worktree_path": worktree_path,
        "tree_sha": tree_sha,
        "receipt_sha": receipt_sha,
        "merge_queue_status": merge_status,
        "evidence_gate_ready": evidence_ready and evidence_status not in {"", "fail", "failed", "unverified", "blocked"},
        "evidence_gate_status": evidence_status or ("pass" if evidence_ready else ""),
        "board_status": board_status,
        "board_completion_percent": board_completion,
        "board_fronts_converged": board_fronts,
        "board_converged": board_converged,
    }


def probe(*, endpoint: str | None = None, receipt: str | None = None,
          timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    endpoint = str(endpoint or os.environ.get(ENV_ENDPOINT) or "").strip()
    receipt = str(receipt or os.environ.get(ENV_RECEIPT) or "").strip()
    acceptance = {
        "endpoint_configured": bool(endpoint),
        "receipt_configured": bool(receipt),
        "endpoint_reachable": False,
        "receipt_loaded": False,
        "receipt_sha_matches": False,
        "branch_matches": False,
        "worktree_matches": False,
        "tree_sha_matches": False,
        "merge_queue_status_ok": False,
        "evidence_gate_ok": False,
        "board_converged": False,
    }
    reasons: list[str] = []
    live_payload: dict[str, Any] | None = None
    receipt_payload: dict[str, Any] | None = None
    live_source = endpoint
    receipt_source = receipt

    if not endpoint:
        reasons.append("endpoint_missing")
    if not receipt:
        reasons.append("receipt_missing")
    if reasons:
        return {
            "schema": SCHEMA,
            "tag": "UNVERIFIED",
            "verdict": "FAIL",
            "fail_closed": True,
            "reason_codes": reasons,
            "acceptance": acceptance,
            "sources": {"endpoint": live_source, "receipt": receipt_source},
        }

    try:
        receipt_payload, receipt_source = _load_json_document(receipt, timeout=timeout)
        acceptance["receipt_loaded"] = True
    except (OSError, ValueError, TypeError, urllib.error.URLError) as exc:
        reasons.append("receipt_load_failed:%s" % type(exc).__name__)

    try:
        live_payload, live_source = _load_json_document(endpoint, timeout=timeout)
        acceptance["endpoint_reachable"] = True
    except (OSError, ValueError, TypeError, urllib.error.URLError) as exc:
        reasons.append("endpoint_fetch_failed:%s" % type(exc).__name__)

    if not (live_payload and receipt_payload):
        return {
            "schema": SCHEMA,
            "tag": "UNVERIFIED",
            "verdict": "FAIL",
            "fail_closed": True,
            "reason_codes": reasons or ["payload_missing"],
            "acceptance": acceptance,
            "sources": {"endpoint": live_source, "receipt": receipt_source},
        }

    live = _extract_state(live_payload)
    recorded = _extract_state(receipt_payload)
    acceptance["receipt_sha_matches"] = bool(live["receipt_sha"] and live["receipt_sha"] == recorded["receipt_sha"])
    acceptance["branch_matches"] = bool(live["branch"] and live["branch"] == recorded["branch"])
    acceptance["worktree_matches"] = bool(live["worktree_path"] and live["worktree_path"] == recorded["worktree_path"])
    acceptance["tree_sha_matches"] = bool(live["tree_sha"] and live["tree_sha"] == recorded["tree_sha"])
    acceptance["merge_queue_status_ok"] = (
        live["merge_queue_status"] in {"accepted", "merged", "pass", "passed"}
        and live["merge_queue_status"] == recorded["merge_queue_status"]
    )
    acceptance["evidence_gate_ok"] = bool(live["evidence_gate_ready"] and recorded["evidence_gate_ready"])
    acceptance["board_converged"] = bool(live["board_converged"] and recorded["board_converged"])

    if not acceptance["receipt_sha_matches"]:
        reasons.append("receipt_sha_mismatch")
    if not acceptance["branch_matches"]:
        reasons.append("branch_mismatch")
    if not acceptance["worktree_matches"]:
        reasons.append("worktree_mismatch")
    if not acceptance["tree_sha_matches"]:
        reasons.append("tree_sha_mismatch")
    if not acceptance["merge_queue_status_ok"]:
        reasons.append("merge_queue_status_unverified")
    if not acceptance["evidence_gate_ok"]:
        reasons.append("evidence_gate_unverified")
    if not acceptance["board_converged"]:
        reasons.append("board_not_converged")

    ok = all(acceptance.values())
    return {
        "schema": SCHEMA,
        "tag": "MEASURED" if ok else "UNVERIFIED",
        "verdict": "PASS" if ok else "FAIL",
        "fail_closed": True,
        "reason_codes": reasons,
        "acceptance": acceptance,
        "sources": {"endpoint": live_source, "receipt": receipt_source},
        "live": live,
        "recorded": recorded,
        "comparison_hash": _canonical({"live": live, "recorded": recorded, "acceptance": acceptance}),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail-closed live probe for merge-queue receipts")
    parser.add_argument("--endpoint", default=os.environ.get(ENV_ENDPOINT, ""))
    parser.add_argument("--receipt", default=os.environ.get(ENV_RECEIPT, ""))
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)
    result = probe(endpoint=args.endpoint, receipt=args.receipt, timeout=args.timeout)
    wire = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.out:
        Path(args.out).write_text(wire, encoding="utf-8")
    sys.stdout.write(wire)
    return 0 if result["tag"] == "MEASURED" and result["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

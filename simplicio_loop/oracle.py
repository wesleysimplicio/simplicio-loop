from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .delivery import validate_delivery_receipt
from .evidence import watcher_truth_from_receipt

PROMISE_RE = re.compile(r"<promise>\s*(.*?)\s*</promise>", re.IGNORECASE | re.DOTALL)
COMPLETION_SCHEMA = "simplicio.completion-receipt/v1"


def _gate(name: str, ok: bool, reason_code: str, detail: str) -> Dict[str, Any]:
    return {
        "name": name,
        "status": "pass" if ok else "fail",
        "reason_code": reason_code,
        "detail": detail,
    }


def _load_json(path: Path) -> Dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _parse_frontmatter(text: str) -> Tuple[Dict[str, str] | None, str]:
    if not text.startswith("---"):
        return None, ""
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None, ""
    meta: Dict[str, str] = {}
    for line in parts[1].splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip().strip('"')
    return meta, parts[2].strip()


def _read_response(loop_dir: Path, response_text: str) -> str:
    if response_text.strip():
        return response_text
    last_response = loop_dir / "last_response.txt"
    if last_response.exists():
        try:
            return last_response.read_text(encoding="utf-8")
        except Exception:
            return ""
    return ""


def _anchor_gate(loop_dir: Path) -> Tuple[bool, Dict[str, Any], Dict[str, Any] | None]:
    anchor_path = loop_dir / "anchor.json"
    anchor = _load_json(anchor_path)
    if not anchor:
        return False, _gate("anchor", False, "anchor_missing", "anchor.json is missing or unreadable"), None
    criteria = anchor.get("criteria")
    if not isinstance(criteria, list) or not criteria:
        return False, _gate("anchor", False, "anchor_empty", "anchor criteria are missing or empty"), anchor
    pending = [c.get("id") for c in criteria if isinstance(c, dict) and c.get("status") != "done"]
    if pending:
        return False, _gate("anchor", False, "anchor_pending", f"open acceptance criteria: {', '.join(pending)}"), anchor
    return True, _gate("anchor", True, "anchor_complete", "all anchored acceptance criteria are done"), anchor


def _watcher_gate(loop_dir: Path) -> Tuple[bool, Dict[str, Any]]:
    watcher_state = _load_json(loop_dir / "watcher_state.json")
    watcher_challenge = _load_json(loop_dir / "watcher_challenge.json")
    if not watcher_state or not watcher_challenge:
        return False, _gate("watcher", False, "watcher_missing", "watcher receipt or challenge is missing")
    if not (watcher_state.get("match") and watcher_state.get("status") == "MEASURED"):
        return False, _gate("watcher", False, "watcher_mismatch", "watcher receipt is not MEASURED/matching")
    if watcher_state.get("challenge") != watcher_challenge.get("challenge"):
        return False, _gate("watcher", False, "watcher_challenge_mismatch", "watcher receipt does not match the current challenge")
    expected_goal_fp = watcher_challenge.get("goal_fp") or ""
    if expected_goal_fp and watcher_state.get("goal_fp") != expected_goal_fp:
        return False, _gate("watcher", False, "watcher_goal_mismatch", "watcher goal fingerprint does not match the current challenge")
    checked_at = watcher_state.get("checked_at") or ""
    written_at = watcher_challenge.get("written_at") or ""
    if checked_at and written_at and checked_at < written_at:
        return False, _gate("watcher", False, "watcher_stale", "watcher receipt predates the active challenge")
    return True, _gate("watcher", True, "watcher_verified", "watcher receipt matches the current challenge")


def _run_artifacts_gate(run_dir: Path) -> Tuple[bool, List[Dict[str, Any]], Dict[str, Any] | None]:
    required = {
        "manifest": "manifest.json",
        "task_contract": "task-contract.json",
        "mapper_receipt": "mapper-context.json",
        "operator_receipt": "operator-receipt.json",
        "evidence_receipt": "evidence-receipt.json",
        "delivery_receipt": "delivery-receipt.json",
    }
    gates: List[Dict[str, Any]] = []
    loaded: Dict[str, Any] = {}
    ok = True
    for name, filename in required.items():
        payload = _load_json(run_dir / filename)
        if not payload:
            ok = False
            gates.append(_gate(name, False, f"{name}_missing", f"{filename} is missing or unreadable"))
            continue
        loaded[name] = payload
        gates.append(_gate(name, True, f"{name}_present", f"{filename} loaded"))
    if not ok:
        return False, gates, None
    evidence = loaded["evidence_receipt"]
    verdict = watcher_truth_from_receipt(evidence)
    if not verdict["ready"]:
        gates.append(_gate("evidence_verdict", False, "evidence_not_verified", verdict["reported"]))
        return False, gates, loaded
    gates.append(_gate("evidence_verdict", True, "evidence_verified", verdict["reported"]))
    delivery = loaded["delivery_receipt"]
    manifest = loaded["manifest"]
    target = (manifest.get("delivery_target") or "").strip().lower()
    validation = validate_delivery_receipt(delivery, target=target)
    gates.extend(validation["gates"])
    if not validation["ok"]:
        return False, gates, loaded
    return True, gates, loaded


def completion_receipt_path(run_dir: str) -> Path:
    return Path(run_dir) / "completion-receipt.json"


def persist_completion_receipt(payload: Dict[str, Any], loop_dir: str, run_dir: str = "") -> str:
    loop = Path(loop_dir)
    run = Path(run_dir) if run_dir else None
    challenge = _load_json(loop / "watcher_challenge.json") or {}
    watcher_state = _load_json(loop / "watcher_state.json") or {}
    anchor = _load_json(loop / "anchor.json") or {}
    manifest = _load_json(run / "manifest.json") if run else {}
    delivery = _load_json(run / "delivery-receipt.json") if run else {}
    out = {
        "schema": COMPLETION_SCHEMA,
        "ready": bool(payload.get("ready")),
        "verdict": payload.get("verdict", "DELIVERY_PENDING"),
        "reason_code": payload.get("reason_code", "oracle_incomplete"),
        "reason": payload.get("reason", "completion gates not satisfied"),
        "tag": payload.get("tag", "UNVERIFIED"),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "loop_dir": str(loop),
        "run_dir": str(run) if run else "",
        "run_id": run.name if run else "",
        "delivery_target": manifest.get("delivery_target", ""),
        "delivery_state": delivery.get("current_state", ""),
        "challenge": challenge.get("challenge", ""),
        "goal_fp": challenge.get("goal_fp") or anchor.get("goal_fp") or "",
        "watcher_status": watcher_state.get("status", "UNVERIFIED"),
        "watcher_match": bool(watcher_state.get("match", False)),
        "gates": payload.get("gates", []),
    }
    if run:
        out["artifacts"] = {
            "manifest": str(run / "manifest.json"),
            "evidence_receipt": str(run / "evidence-receipt.json"),
            "delivery_receipt": str(run / "delivery-receipt.json"),
        }
        path = completion_receipt_path(str(run))
    else:
        path = loop / "completion-receipt.json"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return str(path)


def evaluate_completion(loop_dir: str, run_dir: str = "", response_text: str = "",
                        flow_gap: str = "") -> Dict[str, Any]:
    loop = Path(loop_dir)
    gates: List[Dict[str, Any]] = []
    result: Dict[str, Any] = {
        "ready": False,
        "verdict": "DELIVERY_PENDING",
        "reason_code": "oracle_incomplete",
        "reason": "completion gates not satisfied",
        "tag": "UNVERIFIED",
        "gates": gates,
    }

    scratchpad_path = loop / "scratchpad.md"
    if not scratchpad_path.exists():
        result["reason_code"] = "scratchpad_missing"
        result["reason"] = "scratchpad.md is missing"
        gates.append(_gate("scratchpad", False, "scratchpad_missing", "scratchpad.md is missing"))
        return result
    meta, _body = _parse_frontmatter(scratchpad_path.read_text(encoding="utf-8"))
    if meta is None:
        result["reason_code"] = "scratchpad_corrupt"
        result["reason"] = "scratchpad frontmatter is unreadable"
        gates.append(_gate("scratchpad", False, "scratchpad_corrupt", "scratchpad frontmatter is unreadable"))
        return result
    gates.append(_gate("scratchpad", True, "scratchpad_loaded", "scratchpad frontmatter loaded"))

    promise = (meta.get("completion_promise") or "").strip()
    if not promise or promise == "null":
        result["reason_code"] = "promise_missing"
        result["reason"] = "completion_promise is not configured"
        gates.append(_gate("promise", False, "promise_missing", "completion_promise is not configured"))
        return result

    resp = _read_response(loop, response_text)
    match = PROMISE_RE.search(resp or "")
    if not match or match.group(1).strip() != promise:
        result["reason_code"] = "promise_not_exact"
        result["reason"] = "exact completion promise not present in the active response"
        gates.append(_gate("promise", False, "promise_not_exact", "active response does not contain the exact completion promise"))
        return result
    gates.append(_gate("promise", True, "promise_exact", "active response contains the exact completion promise"))

    anchor_ok, anchor_gate, _anchor = _anchor_gate(loop)
    gates.append(anchor_gate)
    if not anchor_ok:
        result["reason_code"] = anchor_gate["reason_code"]
        result["reason"] = anchor_gate["detail"]
        return result

    watcher_ok, watcher_gate = _watcher_gate(loop)
    gates.append(watcher_gate)
    if not watcher_ok:
        result["reason_code"] = watcher_gate["reason_code"]
        result["reason"] = watcher_gate["detail"]
        return result

    if flow_gap:
        gates.append(_gate("flow_audit", False, "flow_audit_required", flow_gap))
        result["reason_code"] = "flow_audit_required"
        result["reason"] = flow_gap
        return result
    gates.append(_gate("flow_audit", True, "flow_audit_clear", "no flow-audit gap is open"))

    if not run_dir:
        gates.append(_gate("run_artifacts", False, "run_dir_missing", "run directory was not provided"))
        result["reason_code"] = "run_dir_missing"
        result["reason"] = "run directory was not provided"
        return result

    run_ok, artifact_gates, _loaded = _run_artifacts_gate(Path(run_dir))
    gates.extend(artifact_gates)
    if not run_ok:
        last_fail = next((gate for gate in reversed(artifact_gates) if gate["status"] == "fail"), artifact_gates[-1])
        result["reason_code"] = last_fail["reason_code"]
        result["reason"] = last_fail["detail"]
        return result

    result.update({
        "ready": True,
        "verdict": "COMPLETE",
        "reason_code": "completion_verified",
        "reason": "oracle verified promise, anchor, watcher, flow gate and run artifacts",
        "tag": "MEASURED",
    })
    return result

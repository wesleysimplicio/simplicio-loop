"""Support command surfaces extracted from :mod:`simplicio_loop.cli`."""
from __future__ import annotations

import json
from pathlib import Path

from .ops_ledger import (
    CONTEXT_SCHEMA,
    HANDSHAKE_SCHEMA,
    REQUIRED_CONTEXT_FIELDS,
    EventLedger,
    LedgerError,
    validate_handshake,
)


def drain_cli_failure(schema: str, reason_code: str, reason: str, **extra) -> dict:
    """Return an explicitly unverified drain result for invalid CLI input."""
    payload = {
        "schema": schema,
        "verdict": "CONTINUE",
        "ready": False,
        "reason_code": reason_code,
        "reason": reason,
        "tag": "UNVERIFIED",
    }
    payload.update(extra)
    return payload


def read_drain_snapshot(path: str, failure):
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError) as exc:
        return None, failure("snapshot_invalid", "could not read drain snapshot", error=str(exc))
    if not isinstance(payload, dict):
        return None, failure("snapshot_invalid", "drain snapshot must be a JSON object")
    return payload, None


def valid_drain_result(schema: str, payload) -> bool:
    """Check the minimum result envelope before exposing a loaded receipt."""
    if not isinstance(payload, dict) or payload.get("schema") != schema:
        return False
    if payload.get("verdict") not in {"DRAINED", "CONTINUE", "BLOCKED"}:
        return False
    if not isinstance(payload.get("ready"), bool):
        return False
    if payload.get("tag") not in {"MEASURED", "UNVERIFIED"}:
        return False
    return not (payload["ready"] and payload["verdict"] != "DRAINED")


def drain(action: str, snapshot_path: str, receipt_path: str, polls_required: int, *,
          evaluator, persist, load, receipt_error, failure, snapshot_reader, result_validator) -> int:
    """Evaluate, persist, or load a drain receipt and emit exactly one JSON value."""
    if action in {"evaluate", "persist"}:
        if not snapshot_path:
            print(json.dumps(failure("snapshot_required", "--snapshot is required"),
                             ensure_ascii=False, sort_keys=True))
            return 2
        snapshot, error = snapshot_reader(snapshot_path)
        if error is not None:
            print(json.dumps(error, ensure_ascii=False, sort_keys=True))
            return 2
        try:
            result = evaluator(snapshot, polls_required=polls_required)
        except (TypeError, ValueError, KeyError) as exc:
            result = failure("snapshot_invalid", "drain snapshot could not be evaluated", error=str(exc))
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 2
        if action == "evaluate":
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0
        if not receipt_path:
            print(json.dumps(failure("receipt_required", "--receipt is required"),
                             ensure_ascii=False, sort_keys=True))
            return 2
        try:
            result = persist(receipt_path, result=result)
        except (receipt_error, OSError, TypeError, ValueError) as exc:
            result = failure("receipt_persist_failed", "could not persist drain receipt", error=str(exc))
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 2
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0

    if action == "load":
        if not receipt_path:
            print(json.dumps(failure("receipt_required", "--receipt is required"),
                             ensure_ascii=False, sort_keys=True))
            return 2
        try:
            result = load(receipt_path)
        except (receipt_error, OSError, TypeError, ValueError) as exc:
            result = failure("receipt_invalid", "could not load drain receipt", error=str(exc))
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 2
        if result is None:
            print(json.dumps(failure("receipt_missing", "drain receipt does not exist"),
                             ensure_ascii=False, sort_keys=True))
            return 2
        if not result_validator(result):
            print(json.dumps(failure("receipt_invalid", "drain receipt has an invalid result envelope"),
                             ensure_ascii=False, sort_keys=True))
            return 2
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0

    print(json.dumps(failure("action_invalid", "unknown drain action"),
                     ensure_ascii=False, sort_keys=True))
    return 2


def _load_handshake(handshake_json: str, handshake_file: str, validator=None, ledger_error=None):
    """Load and validate one optional executor handshake."""
    if validator is None:
        validator = validate_handshake
    if ledger_error is None:
        ledger_error = LedgerError
    if handshake_json and handshake_file:
        raise ValueError("--handshake-json and --handshake-file are mutually exclusive")
    if not handshake_json and not handshake_file:
        return None
    raw = (Path(handshake_file).read_text(encoding="utf-8")
           if handshake_file else handshake_json)
    try:
        return validator(json.loads(raw))
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        if isinstance(exc, ledger_error):
            raise
        raise ledger_error("executor handshake JSON must be an object") from exc


def ledger_replay(path: str, compatibility: bool, recover_trailing: bool,
                  handshake_json: str, handshake_file: str, command: str = "replay",
                  handshake_loader=None, event_ledger=None, ledger_error=None,
                  context_schema=None, handshake_schema=None, required_context_fields=None) -> int:
    """Replay and validate a ledger through a deterministic, read-only JSON surface."""
    requested_path = str(path)
    try:
        if handshake_loader is None:
            handshake_loader = _load_handshake
        if event_ledger is None:
            event_ledger = EventLedger
        if ledger_error is None:
            ledger_error = LedgerError
        if context_schema is None:
            context_schema = CONTEXT_SCHEMA
        if handshake_schema is None:
            handshake_schema = HANDSHAKE_SCHEMA
        if required_context_fields is None:
            required_context_fields = REQUIRED_CONTEXT_FIELDS
        handshake = handshake_loader(handshake_json, handshake_file)
        if not compatibility and handshake is None:
            raise ledger_error(
                "strict ledger replay requires --handshake-json or --handshake-file"
            )
        events = event_ledger(path, compatibility=compatibility).replay(
            recover_trailing=recover_trailing
        )
        result = {
            "command": "ledger.%s" % command,
            "compatibility": bool(compatibility),
            "context_schema": context_schema,
            "event_count": len(events),
            "events": events,
            "handshake": handshake,
            "handshake_schema": handshake_schema if handshake is not None else None,
            "ok": True,
            "path": requested_path,
            "required_context": list(required_context_fields),
            "schema": "simplicio.ledger-replay/v1",
        }
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 0
    except (ledger_error, OSError, ValueError, json.JSONDecodeError) as exc:
        result = {
            "command": "ledger.%s" % command,
            "compatibility": bool(compatibility),
            "error": {"kind": exc.__class__.__name__, "message": str(exc)},
            "handshake": None,
            "ok": False,
            "path": requested_path,
            "schema": "simplicio.ledger-replay/v1",
        }
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 2


def findings_command(args) -> int:
    """List, report, reconcile, or diagnose continuous findings."""
    from . import finding_report as fr
    from . import finding_router as rt

    cmd = getattr(args, "findings_command", None)
    json_output = bool(getattr(args, "json", False))
    if cmd == "list":
        records = fr.read_findings()
        if json_output:
            print(json.dumps(records, ensure_ascii=False, indent=2))
        else:
            for record in records:
                print("%s [%s] %s:%s confirmed=%s" % (
                    record["ts"], record["severity"], record["stage"],
                    record["finding_id"], record["confirmed"],
                ))
        return 0
    if cmd == "report":
        records = fr.read_findings()
        by_stage = {}
        by_severity = {}
        for record in records:
            by_stage[record["stage"]] = by_stage.get(record["stage"], 0) + 1
            severity = record["severity"]
            by_severity[severity] = by_severity.get(severity, 0) + 1
        payload = {"schema": "simplicio.finding-report-aggregate/v1", "total": len(records),
                   "by_stage": by_stage, "by_severity": by_severity}
        print(json.dumps(payload, ensure_ascii=False, indent=2) if json_output else
              "total=%s by_stage=%s by_severity=%s" % (
                  payload["total"], by_stage, by_severity))
        return 0
    if cmd == "reconcile":
        untracked = rt.untracked_problems()
        blocked = rt.completion_blocked()
        payload = {"schema": "simplicio.finding-reconcile/v1", "untracked_count": len(untracked),
                   "untracked": untracked, "completion_blocked": blocked}
        print(json.dumps(payload, ensure_ascii=False, indent=2) if json_output else
              "untracked_confirmed_findings=%s (completion gate will block if >0)" %
              len(untracked))
        return 1 if blocked else 0
    if cmd == "doctor":
        findings_store = fr._FINDINGS_DIR / "findings.jsonl"
        routes_store = rt.LOCAL_STORE
        findings_present = findings_store.exists()
        routes_present = routes_store.exists()
        payload = {
            "schema": "simplicio.finding-doctor/v1",
            "findings_store_path": str(findings_store),
            "findings_store_present": findings_present,
            "routes_store_path": str(routes_store),
            "routes_store_present": routes_present,
            "store_present": findings_present and routes_present,
            "router_importable": True,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2) if json_output else
              "findings_store_present=%s routes_store_present=%s router_ok=%s" % (
                  findings_present, routes_present, payload["router_importable"]))
        return 0

    payload = {
        "schema": "simplicio.finding-command-error/v1",
        "ok": False,
        "error": {
            "code": "unknown_findings_command",
            "message": "unknown findings subcommand",
            "value": cmd,
        },
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 2

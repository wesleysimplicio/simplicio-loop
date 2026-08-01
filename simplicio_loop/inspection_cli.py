"""Support command surfaces extracted from :mod:`simplicio_loop.cli`."""
from __future__ import annotations

import subprocess
import hashlib
import re
from urllib.parse import urlparse
import json
from pathlib import Path
from . import drain as _drain

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


_REPO_COMPONENT = r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,98}[A-Za-z0-9])?"
_REPO_RE = re.compile(rf"^{_REPO_COMPONENT}/{_REPO_COMPONENT}$")
_IMPORT_RECEIPT_SCHEMA = "simplicio.findings-import-receipt/v2"
_MARKER_PREFIX = "simplicio-findings-import:"


def _valid_repo(repo: str):
    candidate = repo.strip().removesuffix(".git")
    return candidate if _REPO_RE.fullmatch(candidate) else None


def _github_repo_from_remote(remote: str):
    remote = remote.strip()
    scp = re.fullmatch(r"git@github\.com:(.+)", remote)
    if scp:
        return _valid_repo(scp.group(1))
    parsed = urlparse(remote)
    if parsed.scheme not in {"git", "https", "ssh"} or parsed.hostname != "github.com":
        return None
    return _valid_repo(parsed.path.strip("/"))


def _source_parent(source: str) -> Path:
    path = Path(source).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path if path.is_dir() else path.parent


def _repo_for_source(source: str, repo_map):
    parent = _source_parent(source).resolve()
    mapped = (repo_map or {}).get("repos", repo_map or {})
    for directory in (parent, *parent.parents):
        value = mapped.get(str(directory), mapped.get(directory.as_posix()))
        if value is not None:
            if not isinstance(value, str) or not _valid_repo(value):
                raise ValueError(f"invalid GitHub repository mapping for {directory}")
            return _valid_repo(value)
        try:
            result = subprocess.run(["git", "-C", str(directory), "config", "--get", "remote.origin.url"], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0:
            repo = _github_repo_from_remote(result.stdout)
            if repo:
                return repo
    return None


def _load_findings_import(path: str):
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid findings file: {exc}") from exc
    findings = payload.get("findings") if isinstance(payload, dict) else payload
    if not isinstance(findings, list):
        raise ValueError("findings input must be a JSON array or an object with a findings array")
    for index, finding in enumerate(findings):
        if not isinstance(finding, dict):
            raise ValueError(f"findings[{index}] must be an object")
        required = {"file": str, "line": int, "summary": str, "failure_scenario": str}
        if any(not isinstance(finding.get(key), kind) for key, kind in required.items()):
            raise ValueError(f"findings[{index}] requires file, line, summary, and failure_scenario")
        if finding["line"] < 1 or any(not finding[key].strip() for key in ("file", "summary", "failure_scenario")):
            raise ValueError(f"findings[{index}] canonical fields must be non-empty and line positive")
        if "verdict" in finding and (not isinstance(finding["verdict"], str) or not finding["verdict"].strip()):
            raise ValueError(f"findings[{index}].verdict must be a non-empty string")
    return findings


def _finding_hash(finding, repo: str) -> str:
    canonical = {key: finding[key] for key in ("file", "line", "summary", "failure_scenario")}
    if "verdict" in finding:
        canonical["verdict"] = finding["verdict"]
    text = json.dumps({"repo": repo, "finding": canonical}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _marker(finding_hash: str) -> str:
    return _MARKER_PREFIX + finding_hash


def _issue_url(repo: str, url) -> bool:
    return isinstance(url, str) and bool(re.fullmatch(rf"https://github\.com/{re.escape(repo)}/issues/[0-9]+", url))


def _receipt_path(args) -> Path:
    explicit = str(getattr(args, "receipt", "") or "")
    return Path(explicit) if explicit else Path(".simplicio/orchestrator/findings/import-receipt.json")


def _load_import_receipt(path: Path):
    if not path.exists():
        return {"schema": _IMPORT_RECEIPT_SCHEMA, "entries": {}}
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid import receipt JSON: {exc}") from exc
    if not isinstance(receipt, dict) or receipt.get("schema") != _IMPORT_RECEIPT_SCHEMA or not isinstance(receipt.get("entries"), dict):
        raise ValueError("import receipt must be a v2 object with entries")
    for marker, entry in receipt["entries"].items():
        if not isinstance(marker, str) or not marker.startswith(_MARKER_PREFIX) or not isinstance(entry, dict):
            raise ValueError("import receipt entry is invalid")
        repo, finding_hash, url = entry.get("repo"), entry.get("finding_hash"), entry.get("url")
        if marker != _marker(str(finding_hash)) or not isinstance(repo, str) or not _valid_repo(repo) or not _issue_url(repo, url):
            raise ValueError("import receipt entry provenance is invalid")
    return receipt


def _save_import_receipt(path: Path, receipt) -> None:
    _drain._atomic_write_receipt(path, receipt)


def _import_error(code: str, message: str, urls=None):
    return {"error": {"code": code, "message": message}, "urls": dict(urls or {})}


def _marker_state_path(marker: str) -> Path:
    return Path(".simplicio/orchestrator/findings/import-markers") / (marker.removeprefix(_MARKER_PREFIX) + ".json")


def _read_marker_state(path: Path):
    if not path.exists():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else None


def _claim_marker(marker: str, repo: str, finding_hash: str):
    path = _marker_state_path(marker)
    with _drain._receipt_lock(path):
        state = _read_marker_state(path)
        if state and state.get("status") == "resolved" and state.get("repo") == repo and state.get("finding_hash") == finding_hash and _issue_url(repo, state.get("url")):
            return "resolved", state["url"]
        if state and state.get("status") == "in_progress" and state.get("repo") == repo and state.get("finding_hash") == finding_hash:
            return "wait", None
        _drain._atomic_write_receipt(path, {"status": "in_progress", "repo": repo, "finding_hash": finding_hash})
        return "owner", None


def _resolve_marker(marker: str, repo: str, finding_hash: str, url: str):
    path = _marker_state_path(marker)
    with _drain._receipt_lock(path):
        _drain._atomic_write_receipt(path, {"status": "resolved", "repo": repo, "finding_hash": finding_hash, "url": url})


def _find_remote_issue(repo: str, marker: str):
    command = ["gh", "issue", "list", "--repo", repo, "--state", "all", "--search", marker, "--json", "url,title,body", "--limit", "1000000"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=60)
        rows = json.loads(result.stdout) if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        rows = None
    if not isinstance(rows, list):
        return False, None
    title_marker, body_marker = f"[{marker}]", f"<!-- {marker} -->"
    for row in rows:
        if isinstance(row, dict) and (str(row.get("title", "")).endswith(title_marker) or body_marker in str(row.get("body", ""))) and _issue_url(repo, row.get("url")):
            return True, row["url"]
    return True, None


def _create_remote_issue(repo: str, finding, labels, marker: str):
    title = f"[finding] {finding['summary']} [{marker}]"
    body = f"File: {finding['file']}:{finding['line']}\n\nFailure scenario: {finding['failure_scenario']}\n\n<!-- {marker} -->"
    command = ["gh", "issue", "create", "--repo", repo, "--title", title, "--body", body]
    for label in labels:
        command.extend(("--label", label))
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    urls = [line.strip() for line in result.stdout.splitlines() if _issue_url(repo, line.strip())]
    return urls[-1] if urls else None


def _coordinate_finding(finding, repo: str, labels):
    finding_hash = _finding_hash(finding, repo)
    marker = _marker(finding_hash)
    reconciled, remote_url = _find_remote_issue(repo, marker)
    if not reconciled:
        return marker, finding_hash, None, "remote_reconciliation_failed"
    if remote_url:
        _resolve_marker(marker, repo, finding_hash, remote_url)
        return marker, finding_hash, remote_url, None
    claim, state_url = _claim_marker(marker, repo, finding_hash)
    if claim == "resolved":
        reconciled, verified = _find_remote_issue(repo, marker)
        return marker, finding_hash, verified if reconciled else None, None if verified else "remote_reconciliation_failed"
    if claim == "owner":
        url = _create_remote_issue(repo, finding, labels, marker)
        if url:
            _resolve_marker(marker, repo, finding_hash, url)
        return marker, finding_hash, url, None if url else "issue_create_failed"
    for _ in range(20):
        state = _read_marker_state(_marker_state_path(marker))
        if state and state.get("status") == "resolved" and _issue_url(repo, state.get("url")):
            reconciled, verified = _find_remote_issue(repo, marker)
            if reconciled and verified:
                return marker, finding_hash, verified, None
        __import__("time").sleep(0.02)
    return marker, finding_hash, None, "finding_in_progress"


def _import_findings(args):
    try:
        findings = _load_findings_import(args.path)
        repo_map = json.loads(Path(args.repo_map).read_text(encoding="utf-8")) if args.repo_map else {}
        if not isinstance(repo_map, dict):
            raise ValueError("repo map must be a JSON object")
        labels = list(getattr(args, "label", []) or [])
        if any(not isinstance(label, str) or not label.strip() for label in labels):
            raise ValueError("labels must be non-empty strings")
        repositories = []
        for index, finding in enumerate(findings):
            repo = _repo_for_source(finding["file"], repo_map)
            if not repo:
                raise ValueError(f"could not resolve repository for findings[{index}]")
            repositories.append(repo)
        receipt_path = _receipt_path(args)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps(_import_error("invalid_import_input", str(exc)), sort_keys=True))
        return 2
    if args.dry_run:
        print(json.dumps({str(i): f"https://github.com/{repo}/issues/dry-run-{i}" for i, repo in enumerate(repositories)}, sort_keys=True))
        return 0
    try:
        with _drain._receipt_lock(receipt_path):
            receipt = _load_import_receipt(receipt_path)
    except (ValueError, _drain.DrainReceiptError) as exc:
        print(json.dumps(_import_error("corrupt_import_receipt", str(exc)), sort_keys=True))
        return 2
    urls = {}
    for index, (finding, repo) in enumerate(zip(findings, repositories)):
        marker, finding_hash, url, error = _coordinate_finding(finding, repo, labels)
        if error:
            print(json.dumps(_import_error(error, f"could not import findings[{index}]", urls), sort_keys=True))
            return 1
        receipt["entries"][marker] = {"repo": repo, "finding_hash": finding_hash, "url": url}
        try:
            with _drain._receipt_lock(receipt_path):
                latest = _load_import_receipt(receipt_path)
                latest["entries"].update(receipt["entries"])
                receipt = latest
                _save_import_receipt(receipt_path, receipt)
        except (OSError, ValueError, _drain.DrainReceiptError) as exc:
            urls[str(index)] = url
            print(json.dumps(_import_error("receipt_write_failed", str(exc), urls), sort_keys=True))
            return 1
        urls[str(index)] = url
    print(json.dumps(urls, ensure_ascii=False, sort_keys=True))
    return 0

def findings_command(args) -> int:
    """List, report, import, reconcile, or diagnose continuous findings."""
    from . import finding_report as fr
    from . import finding_router as rt

    cmd = getattr(args, "findings_command", None)
    json_output = bool(getattr(args, "json", False))
    if cmd == "import":
        return _import_findings(args)
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
        "error": {"code": "unknown_findings_command", "message": "unknown findings subcommand", "value": cmd},
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 2

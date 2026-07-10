"""CLI for simplicio-loop: install skills/hooks and expose task-contract utilities."""
from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import webbrowser
import json
from pathlib import Path

from . import __version__
from .drain import (
    SCHEMA as DRAIN_SCHEMA,
    DrainReceiptError,
    evaluate_drain,
    load_drain_receipt,
    persist_drain_receipt,
)
from .runner import (
    arm_run,
    apply_human_decision,
    change_phase,
    execute_operator,
    execute_operator_batch,
    read_status,
    reconcile_delivery,
    sync_source_state,
)
from .task_contract import compile_many, main as task_contract_main, preview_contract
from .ops_ledger import (
    CONTEXT_SCHEMA,
    HANDSHAKE_SCHEMA,
    LEGACY_COMPATIBILITY,
    REQUIRED_CONTEXT_FIELDS,
    EventLedger,
    LedgerError,
    validate_handshake,
)

BUNDLE = Path(__file__).resolve().parent / "_bundle"
DASHBOARD = BUNDLE / "hooks" / "simplicio_dashboard.py"
# Cross-platform temp dir (Windows has no /tmp) — must match hooks/simplicio_dashboard.py.
PID_FILE = Path(tempfile.gettempdir()) / "simplicio-token-monitor.pid"
DEFAULT_DASH_PORT = int(os.environ.get("SIMPLICIO_MONITOR_PORT", "9090"))


def _gui_available() -> bool:
    """True only when opening a browser is safe + non-blocking. On headless Linux (no DISPLAY),
    webbrowser.open() may launch a text browser that inherits stdin and blocks forever — and a
    blocking wait() is NOT caught by try/except — so we must skip it there."""
    if sys.platform == "darwin" or os.name == "nt":
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _copy_tree(src: Path, dst: Path) -> int:
    """Copy every file under src into dst, preserving structure. Returns file count."""
    count = 0
    for item in src.rglob("*"):
        if item.is_file():
            out = dst / item.relative_to(src)
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, out)
            count += 1
    return count


def install(target: Path, globally: bool) -> int:
    base = (Path.home() / ".claude") if globally else (target / ".claude")
    skills_dst = base / "skills"
    hooks_dst = (base / "hooks") if globally else (target / "hooks")

    if not (BUNDLE / "skills").is_dir():
        print("error: bundled skills not found in the installed package.", flush=True)
        return 1

    n_skills = _copy_tree(BUNDLE / "skills", skills_dst)
    n_hooks = _copy_tree(BUNDLE / "hooks", hooks_dst)

    print(f"simplicio-loop {__version__} installed:")
    print(f"  skills -> {skills_dst}  ({n_skills} files)")
    print(f"  hooks  -> {hooks_dst}  ({n_hooks} files)")
    print("")
    print("Use it in your agent runtime (Claude Code, Cursor, ...):")
    print("  /simplicio-loop finish all the open issues")
    return 0


def _port_up(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), 0.5):
            return True
    except OSError:
        return False


def _stop_dashboard() -> int:
    """Best-effort stop: kill the PID the dashboard recorded, then any stray server."""
    killed = False
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 15)
        killed = True
    except (OSError, ValueError):
        pass
    if os.name != "nt":  # pkill catches a server started outside this CLI
        try:
            subprocess.run(["pkill", "-f", "simplicio_dashboard.py"], check=False)
            killed = True
        except OSError:
            pass
    try:
        PID_FILE.unlink()
    except OSError:
        pass
    print("⬡ Token Monitor stopped." if killed else "⬡ dashboard was not running.")
    return 0


def dashboard(port: int, open_browser: bool, stop: bool) -> int:
    """Open (or stop) the Simplicio Token Monitor dashboard — works from anywhere after a pip
    install, no repo checkout needed. Starts the bundled server detached if it's not already up,
    then opens the browser. The dashboard is on-demand: nothing keeps it open, close it freely."""
    if stop:
        return _stop_dashboard()
    url = f"http://127.0.0.1:{port}"
    if not DASHBOARD.is_file():
        print("error: bundled dashboard not found in the installed package.", flush=True)
        return 1
    if not _port_up(port):
        logdir = Path.home() / ".simplicio" / "logs"
        logdir.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "PORT": str(port)}
        # Detach so the server outlives this CLI process (own session / no console window).
        kw = {"start_new_session": True} if os.name != "nt" else {
            "creationflags": 0x00000008 | 0x00000200}  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        with open(logdir / "token-monitor.log", "ab") as logf:
            try:
                subprocess.Popen([sys.executable or "python3", str(DASHBOARD)],
                                 env=env, stdout=logf, stderr=logf, stdin=subprocess.DEVNULL, **kw)
            except OSError as e:
                print(f"error: could not start the dashboard ({e}).", flush=True)
                return 1
        for _ in range(25):  # wait up to ~5s for the port to come up
            if _port_up(port):
                break
            time.sleep(0.2)
    if not _port_up(port):
        print(f"⬡ failed to start the dashboard — see ~/.simplicio/logs/token-monitor.log", flush=True)
        return 1
    print(f"⬡ Simplicio Token Monitor → {url}")
    if open_browser and _gui_available():
        try:
            webbrowser.open(url)
        except Exception:
            pass
    print("   stop it any time:  simplicio-loop dashboard --stop")
    return 0


def _write_task_contract(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(__import__("json").dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


def plan(task_path: str, out_path: str) -> int:
    raw = Path(task_path).read_text(encoding="utf-8")
    payload = compile_many(raw, source_path=task_path)
    out = Path(out_path)
    _write_task_contract(out, payload)
    print(f"simplicio-loop plan wrote {out}")
    for idx, task in enumerate(payload.get("tasks") or [], start=1):
        if idx > 1:
            print("")
        print(f"[task {idx}]")
        print(preview_contract(task))
    return 0


def run(repo: str, task_path: str, delivery: str, max_iterations: int) -> int:
    payload = arm_run(repo, task_path, delivery, max_iterations)
    print(__import__("json").dumps(payload, ensure_ascii=False, indent=2))
    return 0


def status(repo: str, run_id: str) -> int:
    payload = read_status(repo, run_id)
    print(__import__("json").dumps(payload, ensure_ascii=False, indent=2))
    return 0


def resume(repo: str, run_id: str) -> int:
    payload = change_phase(repo, run_id, "awaiting_decision", "resume requested from CLI")
    print(__import__("json").dumps(payload, ensure_ascii=False, indent=2))
    return 0


def tick(repo: str, run_id: str, task_index: int) -> int:
    payload = execute_operator(repo, run_id, task_index=task_index)
    print(__import__("json").dumps(payload, ensure_ascii=False, indent=2))
    return 0


def batch(repo: str, run_id: str, task_indices: str, max_workers: int, retry_budget: int) -> int:
    """Run selected ready tasks through the durable real-operator pool."""
    indices = None
    if task_indices.strip():
        try:
            indices = [int(value.strip()) for value in task_indices.split(",") if value.strip()]
        except ValueError as exc:
            raise ValueError("--task-indices must be a comma-separated list of integers") from exc
    payload = execute_operator_batch(
        repo,
        run_id,
        indices,
        max_workers=max_workers or None,
        retry_budget=retry_budget,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cancel(repo: str, run_id: str) -> int:
    payload = change_phase(repo, run_id, "cancelled", "cancel requested from CLI")
    print(__import__("json").dumps(payload, ensure_ascii=False, indent=2))
    return 0


def deliver(repo: str, run_id: str, state: str, source_kind: str, payload_file: str) -> int:
    source_payload = {}
    if payload_file:
        source_payload = json.loads(Path(payload_file).read_text(encoding="utf-8"))
    payload = reconcile_delivery(repo, run_id, state, source_kind=source_kind, source_payload=source_payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def decide(repo: str, run_id: str, decision_id: str, answer: str, impact: str) -> int:
    payload = apply_human_decision(repo, run_id, decision_id, answer, impact=impact)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def sync_source(repo: str, run_id: str, source: str, external_repo: str, pr: int, tag: str) -> int:
    payload = sync_source_state(repo, run_id, source, external_repo=external_repo, pr=pr or None, tag=tag)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _drain_cli_failure(reason_code: str, reason: str, **extra) -> dict:
    """Return a safe, JSON-serializable drain result for CLI input failures.

    The drain CLI is an evidence boundary: malformed or missing input must never
    look like a successful drain.  Keep the shape compatible with
    ``evaluate_drain`` while marking the result explicitly unverified.
    """
    payload = {
        "schema": DRAIN_SCHEMA,
        "verdict": "CONTINUE",
        "ready": False,
        "reason_code": reason_code,
        "reason": reason,
        "tag": "UNVERIFIED",
    }
    payload.update(extra)
    return payload


def _read_drain_snapshot(path: str):
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError) as exc:
        return None, _drain_cli_failure("snapshot_invalid", "could not read drain snapshot", error=str(exc))
    if not isinstance(payload, dict):
        return None, _drain_cli_failure("snapshot_invalid", "drain snapshot must be a JSON object")
    return payload, None


def _valid_drain_result(payload) -> bool:
    """Check the minimum result envelope before exposing a loaded receipt."""
    if not isinstance(payload, dict) or payload.get("schema") != DRAIN_SCHEMA:
        return False
    if payload.get("verdict") not in {"DRAINED", "CONTINUE", "BLOCKED"}:
        return False
    if not isinstance(payload.get("ready"), bool):
        return False
    if payload.get("tag") not in {"MEASURED", "UNVERIFIED"}:
        return False
    return not (payload["ready"] and payload["verdict"] != "DRAINED")


def drain(action: str, snapshot_path: str, receipt_path: str, polls_required: int) -> int:
    """Evaluate, persist, or load a drain receipt and emit exactly one JSON value.

    ``CONTINUE`` is a valid fail-closed verdict for a well-formed snapshot; a
    non-zero exit is reserved for unusable CLI input or a corrupt/missing receipt.
    """
    if action in {"evaluate", "persist"}:
        if not snapshot_path:
            print(json.dumps(_drain_cli_failure("snapshot_required", "--snapshot is required"),
                             ensure_ascii=False, sort_keys=True))
            return 2
        snapshot, failure = _read_drain_snapshot(snapshot_path)
        if failure is not None:
            print(json.dumps(failure, ensure_ascii=False, sort_keys=True))
            return 2
        try:
            result = evaluate_drain(snapshot, polls_required=polls_required)
        except (TypeError, ValueError, KeyError) as exc:
            result = _drain_cli_failure("snapshot_invalid", "drain snapshot could not be evaluated",
                                        error=str(exc))
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 2
        if action == "evaluate":
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0
        if not receipt_path:
            print(json.dumps(_drain_cli_failure("receipt_required", "--receipt is required"),
                             ensure_ascii=False, sort_keys=True))
            return 2
        try:
            result = persist_drain_receipt(receipt_path, result=result)
        except (DrainReceiptError, OSError, TypeError, ValueError) as exc:
            result = _drain_cli_failure("receipt_persist_failed", "could not persist drain receipt",
                                        error=str(exc))
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 2
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0

    if action == "load":
        if not receipt_path:
            print(json.dumps(_drain_cli_failure("receipt_required", "--receipt is required"),
                             ensure_ascii=False, sort_keys=True))
            return 2
        try:
            result = load_drain_receipt(receipt_path)
        except (DrainReceiptError, OSError, TypeError, ValueError) as exc:
            result = _drain_cli_failure("receipt_invalid", "could not load drain receipt", error=str(exc))
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 2
        if result is None:
            print(json.dumps(_drain_cli_failure("receipt_missing", "drain receipt does not exist"),
                             ensure_ascii=False, sort_keys=True))
            return 2
        if not _valid_drain_result(result):
            print(json.dumps(_drain_cli_failure("receipt_invalid", "drain receipt has an invalid result envelope"),
                             ensure_ascii=False, sort_keys=True))
            return 2
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0

    # ``argparse`` constrains this in normal use, but keeping the fallback
    # fail-closed makes direct calls to ``main`` safe as well.
    print(json.dumps(_drain_cli_failure("action_invalid", "unknown drain action"),
                     ensure_ascii=False, sort_keys=True))
    return 2
def _load_handshake(handshake_json: str, handshake_file: str):
    if handshake_json and handshake_file:
        raise ValueError("--handshake-json and --handshake-file are mutually exclusive")
    if not handshake_json and not handshake_file:
        return None
    raw = (Path(handshake_file).read_text(encoding="utf-8")
           if handshake_file else handshake_json)
    try:
        return validate_handshake(json.loads(raw))
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        if isinstance(exc, LedgerError):
            raise
        raise LedgerError("executor handshake JSON must be an object") from exc


def ledger_replay(path: str, compatibility: bool, recover_trailing: bool,
                  handshake_json: str, handshake_file: str, command: str = "replay") -> int:
    """Replay and validate a ledger through a deterministic, read-only JSON surface."""
    requested_path = str(path)
    try:
        handshake = _load_handshake(handshake_json, handshake_file)
        # Strict mode requires both hash-bound event context and the executor
        # handshake.  Legacy mode is an explicit escape hatch for pre-context
        # ledgers and is surfaced in the result so callers cannot confuse it
        # with a strict replay.
        if not compatibility and handshake is None:
            raise LedgerError(
                "strict ledger replay requires --handshake-json or --handshake-file"
            )
        events = EventLedger(path, compatibility=compatibility).replay(
            recover_trailing=recover_trailing
        )
        result = {
            "command": "ledger.%s" % command,
            "compatibility": bool(compatibility),
            "context_schema": CONTEXT_SCHEMA,
            "event_count": len(events),
            "events": events,
            "handshake": handshake,
            "handshake_schema": HANDSHAKE_SCHEMA if handshake is not None else None,
            "ok": True,
            "path": requested_path,
            "required_context": list(REQUIRED_CONTEXT_FIELDS),
            "schema": "simplicio.ledger-replay/v1",
        }
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 0
    except (LedgerError, OSError, ValueError, json.JSONDecodeError) as exc:
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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="simplicio-loop",
        description=(
            "Install the simplicio-loop super-plugin, open the Token Monitor dashboard, "
            "or compile task markdown into a canonical task contract."
        ),
    )
    parser.add_argument("-V", "--version", action="version", version=f"simplicio-loop {__version__}")
    sub = parser.add_subparsers(dest="command")

    p_install = sub.add_parser("install", help="install bundled skills + hooks into a runtime")
    p_install.add_argument("--target", default=".", help="project directory to install into")
    p_install.add_argument("--global", dest="globally", action="store_true",
                           help="install into ~/.claude instead of the project")

    p_dashboard = sub.add_parser("dashboard", help="open or stop the Token Monitor dashboard")
    p_dashboard.add_argument("--port", type=int, default=DEFAULT_DASH_PORT,
                             help="dashboard port (default: %(default)s)")
    p_dashboard.add_argument("--no-browser", dest="no_browser", action="store_true",
                             help="start the server but do not open a browser")
    p_dashboard.add_argument("--stop", action="store_true", help="stop a running Token Monitor")

    p_task = sub.add_parser("task", help="compile, validate, or preview markdown task contracts")
    p_task.add_argument("task_args", nargs=argparse.REMAINDER,
                        help="pass-through args for `simplicio-loop task`")

    p_plan = sub.add_parser("plan", help="compile a raw task into a contract and preview it")
    p_plan.add_argument("--task", required=True, help="markdown task file")
    p_plan.add_argument("--out", default=os.path.join(".orchestrator", "task-contract.json"),
                        help="where to write the compiled contract")

    p_run = sub.add_parser("run", help="arm a persisted run from a raw markdown task")
    p_run.add_argument("--task", required=True, help="markdown task file")
    p_run.add_argument("--repo", default=".", help="repository root")
    p_run.add_argument("--delivery", default="verified", help="requested delivery target")
    p_run.add_argument("--max-iterations", type=int, default=12, help="safety cap")

    p_status = sub.add_parser("status", help="show the latest run state or a specific run")
    p_status.add_argument("--repo", default=".", help="repository root")
    p_status.add_argument("--run-id", default="", help="run id to inspect")

    p_resume = sub.add_parser("resume", help="resume a non-terminal run")
    p_resume.add_argument("--repo", default=".", help="repository root")
    p_resume.add_argument("run_id", help="run id to resume")

    p_tick = sub.add_parser("tick", help="execute one planned task through simplicio-dev-cli")
    p_tick.add_argument("--repo", default=".", help="repository root")
    p_tick.add_argument("run_id", help="run id to tick")
    p_tick.add_argument("--task-index", type=int, default=1, help="1-based task index")

    p_batch = sub.add_parser("batch", help="continuously dispatch ready tasks through simplicio-dev-cli")
    p_batch.add_argument("--repo", default=".", help="repository root")
    p_batch.add_argument("run_id", help="run id to dispatch")
    p_batch.add_argument(
        "--task-indices",
        default="",
        help="comma-separated 1-based task indices (default: every task in the contract)",
    )
    p_batch.add_argument(
        "--max-workers",
        type=int,
        default=0,
        help="maximum live operator workers (default: SIMPLICIO_LOOP_OPERATOR_WORKERS/6)",
    )
    p_batch.add_argument("--retry-budget", type=int, default=3, help="retries after the first attempt")

    p_cancel = sub.add_parser("cancel", help="cancel a non-terminal run")
    p_cancel.add_argument("--repo", default=".", help="repository root")
    p_cancel.add_argument("run_id", help="run id to cancel")

    p_deliver = sub.add_parser("deliver", help="reconcile delivery state against local/external source evidence")
    p_deliver.add_argument("--repo", default=".", help="repository root")
    p_deliver.add_argument("run_id", help="run id to reconcile")
    p_deliver.add_argument("--state", required=True, help="delivery state reached")
    p_deliver.add_argument("--source-kind", default="local", help="source kind for this reconciliation")
    p_deliver.add_argument("--payload-file", default="", help="JSON file with source evidence payload")

    p_decide = sub.add_parser("decide", help="apply a human decision and invalidate dependent artifacts")
    p_decide.add_argument("--repo", default=".", help="repository root")
    p_decide.add_argument("run_id", help="run id to update")
    p_decide.add_argument("--decision-id", required=True, help="decision/question identifier")
    p_decide.add_argument("--answer", required=True, help="human answer")
    p_decide.add_argument("--impact", default="behavior-change", help="impact classification")

    p_sync = sub.add_parser("sync-source", help="requery external source state and reconcile delivery")
    p_sync.add_argument("--repo", default=".", help="repository root")
    p_sync.add_argument("run_id", help="run id to update")
    p_sync.add_argument("--source", required=True, help="external source kind")
    p_sync.add_argument("--external-repo", default="", help="external repo identifier, ex owner/name")
    p_sync.add_argument("--pr", type=int, default=0, help="pull request number")
    p_sync.add_argument("--tag", default="", help="release tag")

    p_drain = sub.add_parser("drain", help="evaluate, persist, or load a queue-drain receipt")
    p_drain.add_argument("action", nargs="?", default="",
                          help="operation to perform: evaluate, persist, or load")
    p_drain.add_argument("--snapshot", "--snapshot-file", dest="snapshot_path", default="",
                         help="JSON scheduler/source snapshot (evaluate and persist)")
    p_drain.add_argument("--receipt", "--receipt-file", dest="receipt_path", default="",
                         help="receipt JSON path (persist and load)")
    p_drain.add_argument("--polls-required", type=int, default=2,
                         help="identical empty polls required (default: %(default)s)")
    p_ledger = sub.add_parser("ledger", help="validate/replay the operational event ledger")
    ledger_sub = p_ledger.add_subparsers(dest="ledger_command", required=True)
    for ledger_command in ("replay", "validate"):
        p_ledger_action = ledger_sub.add_parser(
            ledger_command,
            help="%s events with hash-chain and context validation" % ledger_command,
        )
        p_ledger_action.add_argument("--path", required=True, help="JSONL EventLedger path")
        p_ledger_action.add_argument(
            "--compatibility",
            action="store_true",
            help="explicitly allow legacy v1 rows without context/handshake",
        )
        p_ledger_action.add_argument(
            "--recover-trailing",
            action="store_true",
            help="drop one corrupt trailing JSONL row under the ledger lock",
        )
        handshake = p_ledger_action.add_mutually_exclusive_group()
        handshake.add_argument(
            "--handshake-json",
            default="",
            help="inline executor handshake JSON (strict mode requires it)",
        )
        handshake.add_argument(
            "--handshake-file",
            default="",
            help="file containing executor handshake JSON (strict mode requires it)",
        )

    args = parser.parse_args(argv)
    command = args.command or "install"
    if command == "dashboard":
        return dashboard(args.port, not args.no_browser, args.stop)
    if command == "task":
        forwarded = list(args.task_args or [])
        if forwarded and forwarded[0] == "--":
            forwarded = forwarded[1:]
        return task_contract_main(forwarded)
    if command == "plan":
        return plan(args.task, args.out)
    if command == "run":
        return run(args.repo, args.task, args.delivery, args.max_iterations)
    if command == "status":
        return status(args.repo, args.run_id)
    if command == "resume":
        return resume(args.repo, args.run_id)
    if command == "tick":
        return tick(args.repo, args.run_id, args.task_index)
    if command == "batch":
        return batch(args.repo, args.run_id, args.task_indices, args.max_workers, args.retry_budget)
    if command == "cancel":
        return cancel(args.repo, args.run_id)
    if command == "deliver":
        return deliver(args.repo, args.run_id, args.state, args.source_kind, args.payload_file)
    if command == "decide":
        return decide(args.repo, args.run_id, args.decision_id, args.answer, args.impact)
    if command == "sync-source":
        return sync_source(args.repo, args.run_id, args.source, args.external_repo, args.pr, args.tag)
    if command == "drain":
        return drain(args.action, args.snapshot_path, args.receipt_path, args.polls_required)
    if command == "ledger":
        return ledger_replay(
            args.path,
            args.compatibility,
            args.recover_trailing,
            args.handshake_json,
            args.handshake_file,
            args.ledger_command,
        )
    return install(Path(args.target).resolve(), args.globally)


if __name__ == "__main__":
    raise SystemExit(main())

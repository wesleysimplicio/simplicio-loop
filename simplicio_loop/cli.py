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
try:
    from scripts import release_manifest as _release_manifest
except Exception:  # pragma: no cover - import shim for bundled scripts
    _release_manifest = None
try:
    from . import prototype_cli as _prototype_cli
except Exception:  # pragma: no cover - keeps `simplicio-loop` importable if this is missing
    _prototype_cli = None

from . import __version__
from . import delivery
from .drain import (
    SCHEMA as DRAIN_SCHEMA,
    DrainReceiptError,
    evaluate_drain,
    load_drain_receipt,
    persist_drain_receipt,
)
from .runner import (
    conduct_run,
    apply_human_decision,
    change_phase,
    defer_maintenance_backlog_only,
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
    REQUIRED_CONTEXT_FIELDS,
    EventLedger,
    LedgerError,
    validate_handshake,
)
from . import inspection_cli as _inspection_cli
from .progress import stream as stream_progress
from .oracle import evaluate_matrix, persist_completion_receipt
from .delivery import DELIVERY_ORDER
from .map_service_cli import configure_commands as configure_map_commands, dispatch as dispatch_map

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
        print("⬡ failed to start the dashboard — see ~/.simplicio/logs/token-monitor.log", flush=True)
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


def run(repo: str, task_path: str, delivery_arg: str, max_iterations: int) -> int:
    try:
        delivery_target = delivery.normalize_delivery_target(delivery_arg)
    except delivery.DeliveryTargetError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    payload = conduct_run(repo, task_path, delivery_target, max_iterations)
    print(__import__("json").dumps(payload, ensure_ascii=False, indent=2))
    return 0


def verify(repo: str, run_id: str) -> int:
    from .runner import verify_run
    payload = verify_run(repo, run_id)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["state"].get("phase") == "done" else 1


def _render_status_text(payload: dict) -> str:
    """Human-readable one-screen summary of a run status payload."""
    state = payload.get("state") or {}
    lines = ["simplicio-loop run status", ""]
    run_dir = payload.get("run_dir", "")
    if run_dir:
        lines.append(f"run_dir: {run_dir}")
    completion = state.get("completion") or {}
    lines.append(f"phase: {state.get('phase', 'UNKNOWN')}")
    lines.append(f"completion_tag: {completion.get('tag', 'UNVERIFIED')}")
    coverage = completion.get("coverage")
    if coverage:
        lines.append(f"coverage: {coverage}")
    delivery = state.get("delivery") or {}
    lines.append(f"delivery_ready: {delivery.get('ready', False)}")
    return "\n".join(lines) + "\n"


def status(repo: str, run_id: str, as_json: bool = False, as_text: bool = False) -> int:
    try:
        payload = read_status(repo, run_id)
    except (FileNotFoundError, OSError, ValueError, KeyError) as exc:
        payload = {"schema": "simplicio.status/v1", "status": "UNVERIFIED",
                   "reason_code": "run_missing", "error": str(exc)}
    # Default remains JSON (backwards-compatible). --text opts into the human summary.
    if as_text and not as_json:
        print(_render_status_text(payload))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def preflight(repo: str, as_json: bool = False) -> int:
    """Verify the core operators required by the loop and report runtime availability.

    Returns exit 0 when all required operators are present, 1 otherwise. Always emits a
    machine-readable JSON document (also when --json is omitted, for script consumption).
    """
    from .finding_router import route_finding as _route_finding

    def _probe(name: str, cmd) -> dict:
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
            ok = out.returncode == 0
            version = (out.stdout or out.stderr).strip().splitlines()[0] if ok else ""
            return {"name": name, "present": ok, "version": version,
                    "error": "" if ok else (out.stderr or out.stdout).strip()[:200]}
        except (OSError, subprocess.SubprocessError) as exc:
            return {"name": name, "present": False, "version": "", "error": str(exc)[:200]}

    repo_path = Path(repo).resolve()
    operators = [
        _probe("simplicio-mapper", ["simplicio-mapper", "--version"]),
        _probe("simplicio-dev-cli", ["simplicio-dev-cli", "--help"]),
        _probe("simplicio-py", ["simplicio-py", "--version"]),
        _probe("simplicio-runtime", ["simplicio", "--version"]),
    ]
    # dev-cli and simplicio-py are alternative names for the same action operator.
    action_present = any(o["present"] for o in operators
                         if o["name"] in ("simplicio-dev-cli", "simplicio-py"))
    all_present = operators[0]["present"] and action_present
    runtime_available = operators[3]["present"]
    payload = {
        "schema": "simplicio.preflight/v1",
        "repo": str(repo_path),
        "all_present": all_present,
        "operators": operators,
        "runtime_available": runtime_available,
        "degraded_features": [] if runtime_available else ["runtime-integration"],
    }
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print("simplicio-loop preflight")
        for op in operators:
            mark = "OK " if op["present"] else ("OPTIONAL" if op["name"] == "simplicio-runtime" else "MISSING")
            ver = f" ({op['version']})" if op["version"] else ""
            print(f"  [{mark}] {op['name']}{ver}")
        print("  all_present:" if all_present else "  NOT ALL REQUIRED OPERATORS PRESENT")
        if not runtime_available:
            print("  runtime integration: unavailable (core loop continues)")
    if not all_present:
        for op in operators:
            if not op["present"] and op["name"] != "simplicio-runtime":
                _route_finding(
                    stage="preflight",
                    finding_id=f"missing-operator-{op['name']}",
                    severity="high",
                    source=f"preflight:{op['name']}",
                    confirmed=True,
                    item_id=None,
                    repo=str(repo_path),
                    detail=f"Bound operator {op['name']} not present: {op.get('error','')}",
                )
    return 0 if all_present else 1


def resume(repo: str, run_id: str) -> int:
    payload = change_phase(repo, run_id, "awaiting_decision", "resume requested from CLI")
    print(__import__("json").dumps(payload, ensure_ascii=False, indent=2))
    return 0


def tick(repo: str, run_id: str, task_index: int) -> int:
    payload = execute_operator(repo, run_id, task_index=task_index)
    print(__import__("json").dumps(payload, ensure_ascii=False, indent=2))
    return 0


def batch(repo: str, run_id: str, task_indices: str, max_workers: int, retry_budget: int,
          serial: bool = False) -> int:
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
        auto_fan_out=not serial,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cancel(repo: str, run_id: str) -> int:
    payload = change_phase(repo, run_id, "cancelled", "cancel requested from CLI")
    print(__import__("json").dumps(payload, ensure_ascii=False, indent=2))
    return 0


def oracle(loop_dir: str, run_dir: str, response_text: str, flow_gap: str,
           write_receipt: bool) -> int:
    """Evaluate the shared completion oracle and its cross-runtime parity."""
    payload = evaluate_matrix(loop_dir, run_dir, response_text=response_text, flow_gap=flow_gap)
    if write_receipt:
        # Persist the canonical verdict once; the matrix is only the parity proof.
        from .oracle import evaluate_completion
        verdict = evaluate_completion(loop_dir, run_dir, response_text=response_text,
                                      flow_gap=flow_gap)
        payload["receipt_path"] = persist_completion_receipt(verdict, loop_dir, run_dir)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("parity") and payload.get("signature", [False])[0] else 1


def progress(repo: str, run_id: str, fmt: str, once: bool, interval: float,
             no_animation: bool = False, ascii_only: bool = False) -> int:
    """Render a run's portable progress event for an LLM, terminal, or dashboard."""
    try:
        status = read_status(repo, run_id)
    except (FileNotFoundError, OSError, ValueError, KeyError) as exc:
        print(json.dumps({"schema": "simplicio.progress/v1", "status": "UNVERIFIED",
                          "reason_code": "run_missing", "error": str(exc)}, ensure_ascii=False))
        return 2
    if not status or not status.get("run_dir"):
        print(json.dumps({"schema": "simplicio.progress/v1", "status": "UNVERIFIED",
                          "reason_code": "run_missing"}, ensure_ascii=False))
        return 2
    stream_progress(status["run_dir"], fmt=fmt, once=once, interval=interval,
                    no_animation=no_animation, ascii_only=ascii_only)
    return 0


def maintenance_deferred(repo: str, run_id: str, mode: str, disposition: str,
                         correction_summary: str, deferral_reason: str,
                         resume_instructions: list[str], evidence_status: str) -> int:
    if mode != "maintenance_deferred" or disposition != "backlog_only":
        print(json.dumps({
            "ready": False,
            "reason_code": "maintenance_mode_invalid",
            "tag": "UNVERIFIED",
        }, ensure_ascii=False, sort_keys=True))
        return 2
    payload = defer_maintenance_backlog_only(
        repo,
        run_id,
        correction_summary=correction_summary,
        deferral_reason=deferral_reason,
        resume_instructions=resume_instructions,
        evidence_status=evidence_status,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
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
    """Compatibility wrapper for the focused command helpers."""
    return _inspection_cli.drain_cli_failure(DRAIN_SCHEMA, reason_code, reason, **extra)


def _read_drain_snapshot(path: str):
    return _inspection_cli.read_drain_snapshot(path, _drain_cli_failure)


def _valid_drain_result(payload) -> bool:
    return _inspection_cli.valid_drain_result(DRAIN_SCHEMA, payload)


def drain(action: str, snapshot_path: str, receipt_path: str, polls_required: int) -> int:
    """Compatibility wrapper that preserves CLI-level dependency monkeypatches."""
    return _inspection_cli.drain(
        action, snapshot_path, receipt_path, polls_required,
        evaluator=evaluate_drain, persist=persist_drain_receipt, load=load_drain_receipt,
        receipt_error=DrainReceiptError, failure=_drain_cli_failure,
        snapshot_reader=_read_drain_snapshot, result_validator=_valid_drain_result,
    )
def _load_handshake(handshake_json: str, handshake_file: str):
    """Compatibility wrapper that preserves CLI-level validator monkeypatches."""
    return _inspection_cli._load_handshake(
        handshake_json, handshake_file, validator=validate_handshake,
        ledger_error=LedgerError,
    )


def ledger_replay(path: str, compatibility: bool, recover_trailing: bool,
                  handshake_json: str, handshake_file: str, command: str = "replay") -> int:
    """Compatibility wrapper for the focused read-only ledger surface."""
    return _inspection_cli.ledger_replay(
        path, compatibility, recover_trailing, handshake_json, handshake_file,
        command, handshake_loader=_load_handshake, event_ledger=EventLedger,
        ledger_error=LedgerError, context_schema=CONTEXT_SCHEMA,
        handshake_schema=HANDSHAKE_SCHEMA, required_context_fields=REQUIRED_CONTEXT_FIELDS,
    )


def findings_command(args) -> int:
    """Compatibility wrapper for the focused findings command surface."""
    return _inspection_cli.findings_command(args)


def main(argv=None) -> int:
    argv_list = list(argv) if argv is not None else list(sys.argv[1:])
    if argv_list[:1] == ["hub-drain-admit"]:
        from .hub_drain_admission_cli import main as drain_admission_main
        return drain_admission_main(argv_list[1:])
    if argv_list[:1] == ["hub-drain-plan"]:
        from .github_drain_intake_cli import main as drain_intake_main
        forwarded = argv_list[1:]
        return drain_intake_main(forwarded[1:] if forwarded[:1] == ["--"] else forwarded)
    parser = argparse.ArgumentParser(
        prog="simplicio-loop",
        description=(
            "Install the simplicio-loop super-plugin, open the Token Monitor dashboard, "
            "or compile task markdown into a canonical task contract."
        ),
    )
    parser.add_argument("-V", "--version", action="version", version=f"simplicio-loop {__version__}")
    # Bare `simplicio-loop` (no subcommand at all) falls through to `install` below with these
    # defaults — mirror p_install's own defaults here so that fallback doesn't crash with
    # AttributeError when no subparser ever ran to populate args.target/args.globally.
    parser.set_defaults(target=".", globally=False)
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

    p_prototype = sub.add_parser(
        "prototype", help="Prototype-First gate: plan/classify/validate-schema/doctor (#568 P0)"
    )
    p_prototype.add_argument("prototype_args", nargs=argparse.REMAINDER,
                             help="pass-through args for `simplicio-loop prototype`")

    p_plan = sub.add_parser("plan", help="compile a raw task into a contract and preview it")
    p_plan.add_argument("--task", required=True, help="markdown task file")
    p_plan.add_argument("--out", default=os.path.join(".orchestrator", "task-contract.json"),
                        help="where to write the compiled contract")

    p_run = sub.add_parser("run", help="arm, execute, and independently verify a raw markdown task")
    p_run.add_argument("--task", required=True, help="markdown task file")
    p_run.add_argument("--repo", default=".", help="repository root")
    p_run.add_argument(
        "--delivery", default="verified",
        metavar="{" + ",".join(DELIVERY_ORDER[1:]) + "}",
        help=(
            "requested delivery target, one of: " + ", ".join(DELIVERY_ORDER[1:])
            + " (use 'implemented' for a local-only run)"
        ),
    )
    p_run.add_argument("--max-iterations", type=int, default=12, help="safety cap")

    p_oracle = sub.add_parser("oracle", help="evaluate completion and cross-runtime parity")
    p_oracle.add_argument("--loop-dir", default=os.path.join(".orchestrator", "loop"))
    p_oracle.add_argument("--run-dir", default=os.environ.get("SIMPLICIO_RUN_DIR", ""))
    p_oracle.add_argument("--response-text", default="")
    p_oracle.add_argument("--flow-gap", default="")
    p_oracle.add_argument("--write-receipt", action="store_true")

    p_status = sub.add_parser("status", help="show the latest run state or a specific run")
    p_status.add_argument("--repo", default=".", help="repository root")
    p_status.add_argument("--run-id", default="", help="run id to inspect")
    p_status.add_argument("--json", action="store_true",
                          help="emit machine-readable JSON (this is also the default)")
    p_status.add_argument("--text", dest="as_text", action="store_true",
                          help="emit human-readable text instead of JSON")

    p_map = sub.add_parser("map", help="map-service cross-module status")
    map_sub = p_map.add_subparsers(dest="map_command", required=True)
    configure_map_commands(map_sub)

    p_preflight = sub.add_parser(
        "preflight", help="verify bound operators (mapper/dev-cli/runtime) are installed")
    p_preflight.add_argument("--repo", default=".", help="repository root")
    p_preflight.add_argument("--json", action="store_true",
                             help="emit machine-readable JSON (default: human-readable text)")

    p_verify = sub.add_parser("verify", help="run the independent watcher and delivery gates")
    p_verify.add_argument("--repo", default=".", help="repository root")
    p_verify.add_argument("run_id", help="run id to verify")

    p_progress = sub.add_parser("progress", help="render visual progress for a run")
    p_progress.add_argument("--repo", default=".", help="repository root")
    p_progress.add_argument("run_id", nargs="?", help="run id to render (legacy positional form)")
    p_progress.add_argument("--run", dest="run_flag", default="",
                            help="run id to render (explicit form)")
    p_progress.add_argument("--format", choices=("text", "json", "markdown", "ansi"),
                            default="text", dest="fmt", help="output format")
    p_progress.add_argument("--once", action="store_true", help="render one snapshot")
    p_progress.add_argument("--no-animation", action="store_true",
                            help="emit one static snapshot without spinner/ANSI control codes")
    p_progress.add_argument("--ascii", action="store_true", dest="ascii_only",
                            help="use ASCII icons/bar for terminals without Unicode support")
    p_progress.add_argument("--interval", type=float, default=0.25,
                            help="animation polling interval in seconds")

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
    p_batch.add_argument(
        "--serial", action="store_true",
        help="disable the default isolated fan-out and force the shared-run serial lane",
    )

    p_cancel = sub.add_parser("cancel", help="cancel a non-terminal run")
    p_cancel.add_argument("--repo", default=".", help="repository root")
    p_cancel.add_argument("run_id", help="run id to cancel")

    p_maintenance = sub.add_parser(
        "maintenance-deferred",
        aliases=["defer-maintenance"],
        help="record a maintenance-deferred backlog-only transition",
    )
    p_maintenance.add_argument("--repo", default=".", help="repository root")
    p_maintenance.add_argument("run_id", help="run id to update")
    p_maintenance.add_argument(
        "--mode", choices=("active", "maintenance_deferred"), required=True,
        help="explicit runner mode",
    )
    p_maintenance.add_argument(
        "--disposition", choices=("operator", "backlog_only"), required=True,
        help="explicit runner disposition",
    )
    p_maintenance.add_argument("--correction-summary", required=True)
    p_maintenance.add_argument("--deferral-reason", required=True)
    p_maintenance.add_argument("--resume-instruction", action="append", default=[])
    p_maintenance.add_argument("--evidence-status", default="UNVERIFIED")

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
    sub.add_parser(
        "hub-drain-plan",
        help="read-only PT-BR/EN GitHub drain intake; never executes the plan",
    )
    sub.add_parser(
        "hub-drain-admit",
        help="admit a final #627 checkpoint as held; never dispatches or executes it",
    )
    p_ledger = sub.add_parser("ledger", help="validate/replay the operational event ledger")
    p_findings = sub.add_parser("findings", help="WI-466: inspect and reconcile continuous findings")
    findings_sub = p_findings.add_subparsers(dest="findings_command", required=True)
    p_f_list = findings_sub.add_parser("list", help="list all routed findings (JSONL)")
    p_f_list.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    p_f_report = findings_sub.add_parser("report", help="show aggregated finding counts by stage/severity")
    p_f_report.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    p_f_reconcile = findings_sub.add_parser("reconcile", help="show dedup state and untracked findings")
    p_f_reconcile.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    p_f_doctor = findings_sub.add_parser("doctor", help="health-check the findings store and router state")
    p_f_doctor.add_argument("--json", action="store_true", help="emit machine-readable JSON")
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

    p_release_train = sub.add_parser(
        "release-train", help="release train continuous verification (#558)"
    )
    rt_sub = p_release_train.add_subparsers(
        dest="release_train_command", required=True
    )
    p_rt_check = rt_sub.add_parser(
        "check", help="validate component/ecosystem release schemas + local drift"
    )
    p_rt_check.add_argument("--repo", default=".", help="repository root")

    if argv_list:
        from .github_drain_intake_cli import looks_like_natural_request, main as drain_intake_main
        if looks_like_natural_request(argv_list) and (
            argv_list[0] not in sub.choices or argv_list[0].lower() == "drain"
        ):
            return drain_intake_main(argv_list)
    args = parser.parse_args(argv_list)
    command = args.command or "install"
    if command == "dashboard":
        return dashboard(args.port, not args.no_browser, args.stop)
    if command == "task":
        forwarded = list(args.task_args or [])
        if forwarded and forwarded[0] == "--":
            forwarded = forwarded[1:]
        return task_contract_main(forwarded)
    if command == "prototype":
        if _prototype_cli is None:
            parser.error("prototype_cli module not importable")
        forwarded = list(args.prototype_args or [])
        if forwarded and forwarded[0] == "--":
            forwarded = forwarded[1:]
        return _prototype_cli.main(forwarded)
    if command == "plan":
        return plan(args.task, args.out)
    if command == "run":
        return run(args.repo, args.task, args.delivery, args.max_iterations)
    if command == "oracle":
        return oracle(args.loop_dir, args.run_dir, args.response_text, args.flow_gap,
                      args.write_receipt)
    if command == "status":
        return status(args.repo, args.run_id, args.json, args.as_text)
    if command == "map":
        return dispatch_map(args)
    if command == "preflight":
        return preflight(args.repo, args.json)
    if command == "findings":
        return findings_command(args)
    if command == "verify":
        return verify(args.repo, args.run_id)
    if command == "progress":
        run_id = args.run_id or args.run_flag
        if not run_id:
            parser.error("progress requires a run id (positional or --run)")
        return progress(args.repo, run_id, args.fmt, args.once, args.interval,
                        args.no_animation, args.ascii_only)
    if command == "resume":
        return resume(args.repo, args.run_id)
    if command == "tick":
        return tick(args.repo, args.run_id, args.task_index)
    if command == "batch":
        return batch(args.repo, args.run_id, args.task_indices, args.max_workers, args.retry_budget, args.serial)
    if command == "cancel":
        return cancel(args.repo, args.run_id)
    if command in {"maintenance-deferred", "defer-maintenance"}:
        return maintenance_deferred(
            args.repo, args.run_id, args.mode, args.disposition,
            args.correction_summary, args.deferral_reason,
            args.resume_instruction, args.evidence_status,
        )
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
    if command == "release-train":
        if _release_manifest is None:
            parser.error("release_manifest script not importable")
        return _release_manifest.release_train_check(args.repo)
    return install(Path(args.target).resolve(), args.globally)


if __name__ == "__main__":
    raise SystemExit(main())

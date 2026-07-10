from __future__ import annotations

import json
import hashlib
import os
import random
import re
import subprocess
import string
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

from .delivery import build_delivery_receipt, normalize_delivery_target, write_delivery_receipt
from .evidence import build_evidence_receipt, redact_sensitive_text
from .source_state import github_delivery_payload, infer_github_delivery_state
from .task_contract import compile_many, validate_contract

RUNNER_SCHEMA = "simplicio.run-manifest/v1"
STATE_SCHEMA = "simplicio.run-state/v1"
OPERATOR_RECEIPT_SCHEMA = "simplicio.operator-receipt/v0"
PHASES = [
    "intake",
    "awaiting_decision",
    "mapping",
    "planning",
    "executing",
    "validating",
    "watching",
    "delivering",
    "done",
    "partial",
    "blocked",
    "cancelled",
]
MAPPER_MIN_VERSION = (0, 14, 0)
MAPPER_REQUIRED_VERBS = ("inspect", "handoff", "ask", "sync", "drift")
DEVCLI_REQUIRED_TOKENS = (" task", "--dry-run-task", "--json")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _rand_token(n: int = 10) -> str:
    chars = string.ascii_lowercase + string.digits
    return "".join(random.choice(chars) for _ in range(n))


def _run_id() -> str:
    return time.strftime("run-%Y%m%d-%H%M%S-", time.gmtime()) + _rand_token(8)


def _load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _default_completion_state() -> Dict[str, Any]:
    return {
        "ready": False,
        "receipt": "",
        "verdict": "DELIVERY_PENDING",
        "reason_code": "oracle_incomplete",
        "tag": "UNVERIFIED",
    }


def _completion_state(run_dir: Path, current: Dict[str, Any] | None = None) -> Dict[str, Any]:
    state = dict(current or _default_completion_state())
    receipt_path = run_dir / "completion-receipt.json"
    if not receipt_path.exists():
        return state
    payload = _load_json(receipt_path)
    state.update({
        "ready": bool(payload.get("ready", False)),
        "receipt": str(receipt_path),
        "verdict": payload.get("verdict", state.get("verdict", "DELIVERY_PENDING")),
        "reason_code": payload.get("reason_code", state.get("reason_code", "oracle_incomplete")),
        "tag": payload.get("tag", state.get("tag", "UNVERIFIED")),
    })
    return state


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _contract_path(run_dir: Path) -> Path:
    return run_dir / "task-contract.json"


def _append_jsonl(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _run_cmd(argv: List[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, timeout=180)


def _operator_env() -> Dict[str, str]:
    env = dict(os.environ)
    env.setdefault(
        "SIMPLICIO_MODEL",
        os.environ.get("SIMPLICIO_LOOP_OPERATOR_MODEL", "codex-cli/gpt-5.4"),
    )
    env.setdefault(
        "SIMPLICIO_CODEX_EFFORT",
        os.environ.get("SIMPLICIO_LOOP_OPERATOR_EFFORT", "medium"),
    )
    loop_test_cmd = os.environ.get("SIMPLICIO_LOOP_TEST_CMD", "").strip()
    if loop_test_cmd and not env.get("SIMPLICIO_TEST_CMD", "").strip():
        env["SIMPLICIO_TEST_CMD"] = loop_test_cmd
    return env


def _operator_timeout(kind: str) -> int:
    default = 60 if kind == "dry_run" else 600
    raw = os.environ.get("SIMPLICIO_LOOP_OPERATOR_TIMEOUT_SEC", "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(30, value)


def _devcli_env(repo_path: Path, base_env: Dict[str, str] | None = None) -> Dict[str, str]:
    env = dict(base_env or os.environ)
    repo_str = str(repo_path)
    current = env.get("PYTHONPATH", "").strip()
    env["PYTHONPATH"] = repo_str if not current else f"{repo_str}{os.pathsep}{current}"
    return env


def _devcli_cmd(repo_path: Path, *args: str) -> List[str]:
    if (repo_path / "simplicio" / "cli.py").exists():
        return [sys.executable, "-m", "simplicio.cli", *args]
    return ["simplicio-dev-cli", *args]


def _repo_fingerprint(repo_path: Path) -> Dict[str, str]:
    """Return a deterministic content fingerprint for mapper freshness gates.

    Git status alone cannot detect two edits to the same path, so the fingerprint includes
    file bytes for the relevant working tree while excluding generated mapper/run artifacts.
    This is intentionally local and model-free; a later mutation can therefore invalidate the
    plan without trusting an LLM's freshness claim.
    """
    digest = hashlib.sha256()
    files = []
    for root, dirs, names in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in {".git", ".orchestrator", ".simplicio", "__pycache__"}]
        for name in names:
            path = Path(root) / name
            try:
                rel = path.relative_to(repo_path).as_posix()
                data = path.read_bytes()
            except (OSError, ValueError):
                continue
            files.append((rel, data))
    for rel, data in sorted(files, key=lambda item: item[0]):
        digest.update(rel.encode("utf-8", "surrogateescape"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(data).digest())
    head = ""
    status = ""
    try:
        head_result = _run_cmd(["git", "rev-parse", "HEAD"], repo_path)
        head = (head_result.stdout or "").strip() if head_result.returncode == 0 else ""
        status_result = _run_cmd(["git", "status", "--porcelain=v1", "--untracked-files=all"], repo_path)
        if status_result.returncode == 0:
            filtered = []
            for raw_line in (status_result.stdout or "").splitlines():
                line = raw_line.rstrip()
                if len(line) <= 3:
                    continue
                path_text = line[3:].strip()
                parts = [part.strip() for part in path_text.split("->")] if "->" in path_text else [path_text]
                normalized = [part.replace("\\", "/").lstrip("./").lower() for part in parts if part.strip()]
                if normalized and all(
                    item.startswith(".orchestrator/")
                    or item.startswith(".simplicio/")
                    or item.startswith(".claude/")
                    for item in normalized
                ):
                    continue
                filtered.append(line)
            status = "\n".join(filtered).strip()
    except Exception:
        pass
    return {
        "head": head,
        "dirty_status_hash": hashlib.sha256(status.encode("utf-8")).hexdigest(),
        "tree_hash": digest.hexdigest(),
    }


def _repo_state_equivalent(left: Dict[str, str], right: Dict[str, str]) -> bool:
    """Return True when repo content and base commit are unchanged.

    `dirty_status_hash` is useful telemetry, but it can drift because helper-generated
    `.orchestrator`/`.simplicio` state or other non-material status noise changes while the
    tracked working tree bytes remain identical. Freshness gates should therefore key on the
    semantic repository state: commit + tree content hash.
    """
    return (
        (left.get("head") or "") == (right.get("head") or "")
        and (left.get("tree_hash") or "") == (right.get("tree_hash") or "")
    )


def _parse_version_tuple(text: str) -> tuple[int, int, int]:
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", text or "")
    if not m:
        return (0, 0, 0)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def _preflight_override(name: str) -> Dict[str, Any] | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    return json.loads(raw)


def _coverage(tasks: List[Dict[str, Any]]) -> Dict[str, Any]:
    total_scenarios = 0
    total_rules = 0
    for task in tasks:
        total_scenarios += len(task.get("scenarios") or [])
        total_rules += len(task.get("rules") or [])
    return {
        "scenarios": {"verified": 0, "total": total_scenarios},
        "rules": {"verified": 0, "total": total_rules},
    }


def _criteria_text(task: Dict[str, Any]) -> str:
    lines = []
    for scenario in task.get("scenarios") or []:
        parts = []
        if scenario.get("then"):
            parts.extend(scenario["then"])
        else:
            parts.append(scenario.get("title") or scenario.get("id") or "scenario")
        lines.append("- " + " ".join(parts))
    return "\n".join(lines)


def _constraints_text(task: Dict[str, Any]) -> str:
    lines = []
    for rule in task.get("rules") or []:
        lines.append(f"- {rule.get('id')}: {rule.get('text')}")
    deps = (task.get("dependencies") or {}).get("items") or []
    for dep in deps:
        lines.append(f"- dependency: {dep}")
    return "\n".join(lines)


def _task_goal(task: Dict[str, Any]) -> str:
    identity = task.get("identity") or {}
    story = task.get("story") or {}
    parts = [
        p
        for p in [
            identity.get("system"),
            identity.get("feature"),
            identity.get("type"),
            story.get("persona"),
            story.get("desire"),
            story.get("value"),
        ]
        if p
    ]
    return " | ".join(parts)


def _write_scratchpad(loop_dir: Path, goal: str, max_iterations: int, promise: str) -> None:
    body = "\n".join(
        [
            "---",
            "iteration: 1",
            f"max_iterations: {max_iterations}",
            f'completion_promise: "{promise}"',
            "evidence_required: true",
            "mode: converge",
            f'started_at: "{_now()}"',
            "---",
            "",
            goal,
            "",
        ]
    )
    (loop_dir / "scratchpad.md").write_text(body, encoding="utf-8")


def _write_watcher_challenge(loop_dir: Path, goal_fp: str) -> None:
    payload = {
        "challenge": f"wch-{_rand_token(12)}",
        "iteration": 1,
        "goal_fp": goal_fp,
        "written_at": _now(),
    }
    _write_json(loop_dir / "watcher_challenge.json", payload)


def _transition(run_dir: Path, state: Dict[str, Any], to_phase: str, reason: str,
                receipt: str = "", extra: Dict[str, Any] | None = None) -> Dict[str, Any]:
    if to_phase not in PHASES:
        raise ValueError(f"invalid phase {to_phase!r}")
    entry = {
        "ts": _now(),
        "from": state.get("phase"),
        "to": to_phase,
        "reason": reason,
        "receipt": receipt,
    }
    if extra:
        entry["extra"] = extra
    history = state.setdefault("history", [])
    history.append(entry)
    state["phase"] = to_phase
    state["updated_at"] = entry["ts"]
    _write_json(run_dir / "state.json", state)
    _append_jsonl(run_dir / "transitions.jsonl", entry)
    return state


def _preflight_mapper(repo_path: Path, run_root: Path) -> Dict[str, Any]:
    override = _preflight_override("SIMPLICIO_LOOP_FAKE_MAPPER_PREFLIGHT_JSON")
    if override is not None:
        version_stdout = str(override.get("version_stdout", ""))
        help_stdout = str(override.get("help_stdout", ""))
        version_rc = int(override.get("version_returncode", 0))
        help_rc = int(override.get("help_returncode", 0))
    else:
        version = _run_cmd(["simplicio-mapper", "--version"], repo_path)
        help_result = _run_cmd(["simplicio-mapper", "--help"], repo_path)
        version_stdout = (version.stdout or "").strip()
        help_stdout = (help_result.stdout or "").strip()
        version_rc = version.returncode
        help_rc = help_result.returncode
    parsed_version = _parse_version_tuple(version_stdout)
    missing_verbs = [verb for verb in MAPPER_REQUIRED_VERBS if verb not in help_stdout]
    task_aware_flags = ("--goal", "--task-file", "--task-fingerprint")
    supported_task_aware_flags = [flag for flag in task_aware_flags if flag in help_stdout]
    receipt = {
        "tool": "simplicio-mapper",
        "returncode": version_rc,
        "stdout": version_stdout,
        "help_returncode": help_rc,
        "help_stdout": help_stdout,
        "version": ".".join(str(part) for part in parsed_version),
        "min_version": ".".join(str(part) for part in MAPPER_MIN_VERSION),
        "version_ok": parsed_version >= MAPPER_MIN_VERSION,
        "required_verbs": list(MAPPER_REQUIRED_VERBS),
        "missing_verbs": missing_verbs,
        "task_aware_flags": list(task_aware_flags),
        "supported_task_aware_flags": supported_task_aware_flags,
        "task_aware_supported": len(supported_task_aware_flags) == len(task_aware_flags),
        "checked_at": _now(),
    }
    _write_json(run_root / "mapper-preflight.json", receipt)
    if version_rc != 0 or help_rc != 0:
        raise RuntimeError("simplicio-mapper unavailable")
    if parsed_version < MAPPER_MIN_VERSION:
        raise RuntimeError("simplicio-mapper below minimum version")
    if missing_verbs:
        raise RuntimeError("simplicio-mapper missing required capabilities")
    return receipt


def _preflight_operator(repo_path: Path, run_root: Path) -> Dict[str, Any]:
    override = _preflight_override("SIMPLICIO_LOOP_FAKE_DEVCLI_PREFLIGHT_JSON")
    if override is not None:
        help_stdout = str(override.get("help_stdout", ""))
        help_rc = int(override.get("help_returncode", 0))
        task_help_stdout = str(override.get("task_help_stdout", help_stdout))
        task_help_rc = int(override.get("task_help_returncode", help_rc))
    else:
        env = _devcli_env(repo_path)
        help_result = subprocess.run(
            _devcli_cmd(repo_path, "--help"),
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=180,
            env=env,
        )
        task_help_result = subprocess.run(
            _devcli_cmd(repo_path, "task", "--help"),
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=180,
            env=env,
        )
        help_stdout = (help_result.stdout or "").strip()
        help_rc = help_result.returncode
        task_help_stdout = (task_help_result.stdout or "").strip()
        task_help_rc = task_help_result.returncode
    capability_surface = " ".join(part for part in (help_stdout, task_help_stdout) if part)
    missing_tokens = [token for token in DEVCLI_REQUIRED_TOKENS if token not in (" " + capability_surface)]
    receipt = {
        "tool": "simplicio-dev-cli",
        "returncode": help_rc,
        "help_stdout": help_stdout,
        "task_help_returncode": task_help_rc,
        "task_help_stdout": task_help_stdout,
        "required_tokens": list(DEVCLI_REQUIRED_TOKENS),
        "missing_tokens": missing_tokens,
        "checked_at": _now(),
    }
    _write_json(run_root / "operator-preflight.json", receipt)
    if help_rc != 0 or task_help_rc != 0:
        raise RuntimeError("simplicio-dev-cli unavailable")
    if missing_tokens:
        raise RuntimeError("simplicio-dev-cli missing required capabilities")
    return receipt


def _run_mapper(repo_path: Path, run_root: Path, task_path: str = "", goal: str = "",
                task_fingerprint: str = "", target_hint: str = "") -> Dict[str, Any]:
    before = _repo_fingerprint(repo_path)
    mapper_preflight = _preflight_mapper(repo_path, run_root)
    scan = _run_cmd(["simplicio-mapper", "scan", ".", "--json", "--sync"], repo_path)
    inspect = _run_cmd(["simplicio-mapper", "inspect", ".", "--json", "--await"], repo_path)
    handoff_argv = ["simplicio-mapper", "handoff", ".", "--json", "--await"]
    task_aware_supported = bool(mapper_preflight.get("task_aware_supported"))
    if task_aware_supported and goal.strip():
        handoff_argv.extend(["--goal", goal.strip()])
    if task_aware_supported and task_path.strip():
        handoff_argv.extend(["--task-file", task_path.strip()])
    if task_aware_supported and task_fingerprint.strip():
        handoff_argv.extend(["--task-fingerprint", task_fingerprint.strip()])
    if task_aware_supported and target_hint.strip():
        handoff_argv.extend(["--target", target_hint.strip()])
    handoff = _run_cmd(handoff_argv, repo_path)
    payload = {
        "scan": {
            "returncode": scan.returncode,
            "stdout": json.loads(scan.stdout) if scan.stdout.strip() else {},
            "stderr": (scan.stderr or "").strip(),
        },
        "inspect": {
            "returncode": inspect.returncode,
            "stdout": json.loads(inspect.stdout) if inspect.stdout.strip() else {},
            "stderr": (inspect.stderr or "").strip(),
        },
        "handoff": {
            "returncode": handoff.returncode,
            "stdout": json.loads(handoff.stdout) if handoff.stdout.strip() else {},
            "stderr": (handoff.stderr or "").strip(),
        },
        "generated_at": _now(),
        "repo_state_before": before,
        "repo_state_after": _repo_fingerprint(repo_path),
    }
    _write_json(run_root / "mapper-context.json", payload)
    if scan.returncode != 0 or inspect.returncode != 0 or handoff.returncode != 0:
        raise RuntimeError("mapper scan/inspect/handoff failed")
    if not _repo_state_equivalent(payload["repo_state_before"], payload["repo_state_after"]):
        raise RuntimeError("repository changed during mapper survey; freshness cannot be proven")
    return payload


def _build_plan(tasks: List[Dict[str, Any]], mapper_payload: Dict[str, Any], repo_path: Path) -> Dict[str, Any]:
    return _build_plan_with_hints(tasks, mapper_payload, repo_path, "")


def _extract_repo_file_hints(task_text: str, repo_path: Path) -> List[str]:
    hints: List[str] = []
    for match in re.finditer(r"(?P<path>[A-Za-z0-9_./\\-]+\.(?:py|ts|tsx|js))", task_text or ""):
        raw = match.group("path").strip().replace("\\", "/")
        candidate = Path(raw)
        try:
            resolved = (repo_path / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
            rel = resolved.relative_to(repo_path.resolve()).as_posix()
        except (OSError, ValueError):
            continue
        low = rel.lower()
        if low.startswith(".orchestrator/") or low.startswith(".claude/") or low.startswith(".github/"):
            continue
        if low.startswith(".venv/") or low.startswith("venv/") or "/site-packages/" in low:
            continue
        if "/_bundle/" in low:
            continue
        if rel not in hints:
            hints.append(rel)
    return hints


def _build_plan_with_hints(tasks: List[Dict[str, Any]], mapper_payload: Dict[str, Any], repo_path: Path,
                           task_text: str) -> Dict[str, Any]:
    handoff = ((mapper_payload.get("handoff") or {}).get("stdout") or {}).get("context_pack") or {}
    explicit_hints = _extract_repo_file_hints(task_text, repo_path)
    filtered_targets = _candidate_targets(mapper_payload, repo_path)
    candidate_targets: List[str] = []
    for path in explicit_hints + filtered_targets:
        if path not in candidate_targets:
            candidate_targets.append(path)
    candidate_targets = candidate_targets[:8]
    steps = []
    for index, task in enumerate(tasks, start=1):
        task_steps = []
        for scenario in task.get("scenarios") or []:
            task_steps.append(
                {
                    "kind": "scenario",
                    "id": scenario.get("id"),
                    "title": scenario.get("title"),
                    "rule_refs": scenario.get("rule_refs") or [],
                    "verification_intent": scenario.get("verification_intent"),
                    "status": "pending",
                }
            )
        steps.append(
            {
                "task_index": index,
                "title": (task.get("identity") or {}).get("title") or _task_goal(task),
                "candidate_targets": list(candidate_targets),
                "steps": task_steps,
            }
        )
    return {
        "schema": "simplicio.plan/v0",
        "generated_at": _now(),
        "task_count": len(tasks),
        "mapper_targets": list(candidate_targets),
        "mapper_pack_hash": handoff.get("pack_hash", ""),
        "repo_state": mapper_payload.get("repo_state_after") or {},
        "freshness": {
            "verified": _repo_state_equivalent(
                mapper_payload.get("repo_state_before") or {},
                mapper_payload.get("repo_state_after") or {},
            ),
            "checked_at": mapper_payload.get("generated_at", ""),
            "current_state": _repo_fingerprint(repo_path),
        },
        "steps": steps,
    }


def _fallback_targets(repo_path: Path) -> List[str]:
    out: List[str] = []
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [
            d for d in dirs
            if d not in {".git", ".orchestrator", ".claude", ".simplicio", "__pycache__", ".venv", "venv", "site-packages"}
        ]
        for name in files:
            if not name.endswith((".py", ".ts", ".tsx", ".js")):
                continue
            full = Path(root) / name
            try:
                rel = full.relative_to(repo_path).as_posix()
            except ValueError:
                continue
            low = rel.lower()
            if "/_bundle/" in low or low.startswith(".github/"):
                continue
            out.append(rel)
    out.sort()
    return out[:8]


def _candidate_targets(mapper_payload: Dict[str, Any], repo_path: Path) -> List[str]:
    handoff = ((mapper_payload.get("handoff") or {}).get("stdout") or {}).get("context_pack") or {}
    files = handoff.get("files") or []
    ranked = []
    for item in files:
        path = item.get("path") if isinstance(item, dict) else None
        if not path:
            continue
        try:
            resolved = (repo_path / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
            resolved.relative_to(repo_path.resolve())
        except (OSError, ValueError):
            continue
        low = path.lower()
        if low.startswith(".orchestrator/") or low.startswith(".claude/"):
            continue
        if low.startswith(".venv/") or low.startswith("venv/") or "/site-packages/" in low:
            continue
        if low.startswith(".github/"):
            continue
        if "/_bundle/" in low.replace("\\", "/"):
            continue
        if low.endswith(".py") or low.endswith(".ts") or low.endswith(".tsx") or low.endswith(".js"):
            ranked.append(path)
    ranked = ranked[:8]
    return ranked or _fallback_targets(repo_path)


def _build_anchor(tasks: List[Dict[str, Any]], contract_hash: str) -> Dict[str, Any]:
    criteria = []
    index = 1
    for task_index, task in enumerate(tasks, start=1):
        for scenario in task.get("scenarios") or []:
            criteria.append({
                "id": f"AC{index}",
                "task_index": task_index,
                "scenario_id": scenario.get("id"),
                "title": scenario.get("title"),
                "rule_refs": scenario.get("rule_refs") or [],
                "status": "pending",
            })
            index += 1
    return {
        "schema": "simplicio.anchor/v1",
        "contract_hash": contract_hash,
        "criteria": criteria,
        "created_at": _now(),
    }


def _prepare_operator_receipt(repo_path: Path, run_root: Path, task: Dict[str, Any],
                              target: str) -> Dict[str, Any]:
    try:
        target_path = (repo_path / target).resolve() if not Path(target).is_absolute() else Path(target).resolve()
        target_path.relative_to(repo_path.resolve())
    except (OSError, ValueError) as exc:
        raise ValueError(f"operator target outside authorized repo: {target!r}") from exc
    _preflight_operator(repo_path, run_root)
    fake = os.environ.get("SIMPLICIO_LOOP_FAKE_OPERATOR_JSON", "").strip()
    if fake:
        payload = json.loads(fake)
        receipt = {
            "schema": OPERATOR_RECEIPT_SCHEMA,
            "mode": "dry_run",
            "tool": "simplicio-dev-cli",
            "execution_state": payload.get("execution_state", "dry_run"),
            "target": target,
            "goal": _task_goal(task),
            "argv": payload.get("argv", []),
            "returncode": payload.get("returncode", 0),
            "stdout": payload.get("stdout", {}),
            "stderr": payload.get("stderr", ""),
            "timed_out": False,
            "measured_at": _now(),
            "source": "env_override",
        }
        _write_json(run_root / "operator-receipt.json", receipt)
        return receipt

    argv = _devcli_cmd(
        repo_path,
        "task",
        _task_goal(task),
        "--root",
        str(repo_path),
        "--target",
        target,
        "--criteria",
        _criteria_text(task),
        "--constraints",
        _constraints_text(task),
        "--dry-run-task",
        "--json",
    )
    for bound in [target]:
        argv.extend(["--bound-paths", bound])
    try:
        op_env = _devcli_env(repo_path, _operator_env())
        result = subprocess.run(
            argv,
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=_operator_timeout("dry_run"),
            env=op_env,
        )
        stdout = (result.stdout or "").strip()
        parsed = {}
        if stdout:
            try:
                parsed = json.loads(stdout)
            except ValueError:
                parsed = {"raw": stdout}
        receipt = {
            "schema": OPERATOR_RECEIPT_SCHEMA,
            "mode": "dry_run",
            "tool": "simplicio-dev-cli",
            "execution_state": "dry_run" if result.returncode == 0 else "blocked",
            "target": target,
            "goal": _task_goal(task),
            "argv": argv,
            "returncode": result.returncode,
            "stdout": parsed,
            "stderr": (result.stderr or "").strip(),
            "timed_out": False,
            "measured_at": _now(),
            "source": "live_cli",
            "provider_config": {
                "model": op_env.get("SIMPLICIO_MODEL", ""),
                "effort": op_env.get("SIMPLICIO_CODEX_EFFORT", ""),
            },
        }
    except subprocess.TimeoutExpired as exc:
        op_env = _operator_env()
        receipt = {
            "schema": OPERATOR_RECEIPT_SCHEMA,
            "mode": "dry_run",
            "tool": "simplicio-dev-cli",
            "execution_state": "blocked",
            "target": target,
            "goal": _task_goal(task),
            "argv": argv,
            "returncode": None,
            "stdout": {},
            "stderr": f"timed out after {exc.timeout}s",
            "timed_out": True,
            "measured_at": _now(),
            "source": "live_cli",
            "provider_config": {
                "model": op_env.get("SIMPLICIO_MODEL", ""),
                "effort": op_env.get("SIMPLICIO_CODEX_EFFORT", ""),
            },
        }
    _write_json(run_root / "operator-receipt.json", receipt)
    return receipt


def arm_run(repo: str, task_path: str, delivery: str, max_iterations: int) -> Dict[str, Any]:
    repo_path = Path(repo).resolve()
    delivery = normalize_delivery_target(delivery)
    raw = Path(task_path).read_text(encoding="utf-8")
    compiled = compile_many(raw, source_path=str(Path(task_path).resolve()))
    tasks = compiled.get("tasks") or []
    validation_errors: List[str] = []
    validation_warnings: List[str] = []
    for idx, task in enumerate(tasks, start=1):
        verdict = validate_contract(task)
        validation_errors.extend([f"task[{idx}] {e}" for e in verdict["errors"]])
        validation_warnings.extend([f"task[{idx}] {w}" for w in verdict["warnings"]])
    if validation_errors:
        raise ValueError("invalid task contract: " + "; ".join(validation_errors))

    run_id = _run_id()
    run_root = repo_path / ".orchestrator" / "runs" / run_id
    loop_dir = run_root / "loop"
    loop_dir.mkdir(parents=True, exist_ok=True)

    promise = f"run-{run_id}-verified"
    manifest = {
        "schema": RUNNER_SCHEMA,
        "run_id": run_id,
        "repo": str(repo_path),
        "task_path": str(Path(task_path).resolve()),
        "delivery_target": delivery,
        "max_iterations": max_iterations,
        "completion_promise": promise,
        "created_at": _now(),
        "task_count": compiled["task_count"],
        "collection_hash": compiled["collection_hash"],
    }
    _write_json(run_root / "manifest.json", manifest)
    _write_json(run_root / "task-contract.json", compiled)
    _write_json(loop_dir / "anchor.json", _build_anchor(tasks, compiled["collection_hash"]))
    goal = "\n\n".join([_task_goal(task) for task in tasks if _task_goal(task)]).strip() or raw.strip()
    _write_scratchpad(loop_dir, goal, max_iterations, promise)
    first_goal_fp = (tasks[0].get("source") or {}).get("hash", "") if tasks else ""
    _write_watcher_challenge(loop_dir, first_goal_fp)
    state = {
        "schema": STATE_SCHEMA,
        "run_id": run_id,
        "phase": "intake",
        "delivery_target": delivery,
        "created_at": _now(),
        "updated_at": _now(),
        "task_count": compiled["task_count"],
        "coverage": _coverage(tasks),
        "validation": {"errors": validation_errors, "warnings": validation_warnings},
        "current_action": "task_contract_compiled",
        "next_action": "mapper_scan_required",
        "delivery": {"target": delivery, "current_state": "planned", "ready": False, "receipt": ""},
        "completion": _default_completion_state(),
        "mapper": {"ready": False, "receipt": "", "targets": []},
        "operator": {"ready": False, "receipt": "", "target": "", "execution_state": "proposed"},
        "evidence": {"ready": False, "receipt": "", "status": "UNVERIFIED"},
        "blockers": [],
        "attempts": 0,
        "history": [],
    }
    _write_json(run_root / "state.json", state)
    _append_jsonl(
        run_root / "transitions.jsonl",
        {
            "ts": _now(),
            "from": None,
            "to": "intake",
            "reason": "run armed from raw task",
            "receipt": str(run_root / "task-contract.json"),
        },
    )
    _transition(run_root, state, "mapping", "task contract compiled and persisted; mapper required",
                receipt=str(run_root / "task-contract.json"))
    try:
        primary_goal = _task_goal(tasks[0]) if tasks else raw.strip()
        mapper_payload = _run_mapper(
            repo_path,
            run_root,
            task_path=str(Path(task_path).resolve()),
            goal=primary_goal,
            task_fingerprint=compiled["collection_hash"],
        )
        state = _load_json(run_root / "state.json")
        handoff = ((mapper_payload.get("handoff") or {}).get("stdout") or {}).get("context_pack") or {}
        state["mapper"] = {
            "ready": True,
            "receipt": str(run_root / "mapper-context.json"),
            "targets": _candidate_targets(mapper_payload, repo_path),
        }
        state["current_action"] = "mapper_context_persisted"
        state["next_action"] = "plan_ready_for_decision"
        _write_json(run_root / "state.json", state)
        _transition(run_root, state, "planning", "mapper scan/inspect/handoff persisted",
                    receipt=str(run_root / "mapper-context.json"))
        plan = _build_plan_with_hints(tasks, mapper_payload, repo_path, raw)
        _write_json(run_root / "plan.json", plan)
        state = _load_json(run_root / "state.json")
        state["current_action"] = "plan_materialized"
        candidates = ((plan.get("steps") or [{}])[0].get("candidate_targets") or [])
        if candidates:
            receipt = _prepare_operator_receipt(repo_path, run_root, tasks[0], candidates[0])
            plan_hash = hashlib.sha256((run_root / "plan.json").read_bytes()).hexdigest()
            receipt["task_contract_hash"] = compiled["collection_hash"]
            receipt["plan_hash"] = plan_hash
            receipt["mapper_pack_hash"] = plan.get("mapper_pack_hash", "")
            receipt["authorized_targets"] = [candidates[0]]
            receipt["target_within_repo"] = True
            _write_json(run_root / "operator-receipt.json", receipt)
            state["operator"] = {
                "ready": receipt.get("execution_state") == "dry_run",
                "receipt": str(run_root / "operator-receipt.json"),
                "target": candidates[0],
                "execution_state": receipt.get("execution_state", "proposed"),
            }
            evidence = build_evidence_receipt(str(run_root))
            _write_json(run_root / "evidence-receipt.json", evidence)
            state["evidence"] = {
                "ready": False,
                "receipt": str(run_root / "evidence-receipt.json"),
                "status": evidence.get("status", "UNVERIFIED"),
            }
            delivery_receipt = build_delivery_receipt(str(run_root), delivery, current_state="implemented")
            write_delivery_receipt(str(run_root), delivery_receipt)
            state["delivery"] = {
                "target": delivery,
                "current_state": delivery_receipt["current_state"],
                "ready": delivery_receipt["ready"],
                "receipt": str(run_root / "delivery-receipt.json"),
                "source_checked_at": delivery_receipt["source_checked_at"],
            }
            state["current_action"] = "operator_dry_run_recorded"
            state["next_action"] = "await_operator_decision"
        else:
            state["current_action"] = "plan_materialized"
            state["next_action"] = "select_target_for_operator"
        _write_json(run_root / "state.json", state)
        _transition(run_root, state, "awaiting_decision", "plan derived from task contract + mapper",
                    receipt=str(run_root / "plan.json"))
    except Exception as exc:
        state = _load_json(run_root / "state.json")
        state["blockers"] = [str(exc)]
        state["current_action"] = "mapping_failed"
        state["next_action"] = "repair_mapper_or_repo"
        _write_json(run_root / "state.json", state)
        _transition(run_root, state, "blocked", "mapper integration failed",
                    receipt=str(run_root / "mapper-context.json"), extra={"error": str(exc)})
    return {"manifest": manifest, "state": _load_json(run_root / "state.json"), "run_dir": str(run_root)}


def _changed_paths(repo_path: Path) -> List[str]:
    try:
        result = _run_cmd(["git", "diff", "--name-only", "HEAD"], repo_path)
        paths = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
        status = _run_cmd(["git", "status", "--porcelain=v1", "--untracked-files=all"], repo_path)
        for line in (status.stdout or "").splitlines():
            if len(line) > 3 and line[3:].strip() not in paths:
                paths.append(line[3:].strip())
        return sorted(set(paths))
    except Exception:
        return []


def _capture_operator_checkpoint(run_dir: Path, repo_path: Path, targets: List[str]) -> Dict[str, Any]:
    checkpoint_dir = run_dir / "checkpoint"
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    files = []
    for target in sorted(set(t for t in targets if t)):
        path = repo_path / target
        exists = path.exists()
        content = path.read_text(encoding="utf-8") if exists else None
        files.append({
            "path": target,
            "exists": exists,
            "content": content,
        })
    return {
        "kind": "file-snapshot/v1",
        "created_at": _now(),
        "safe_targets": sorted(set(t for t in targets if t)),
        "files": files,
    }


def _restore_operator_checkpoint(checkpoint: Dict[str, Any], repo_path: Path, changed_paths: List[str]) -> Dict[str, Any]:
    targets = sorted(set(str(path) for path in (checkpoint.get("safe_targets") or []) if str(path)))
    changed = sorted(set(str(path) for path in (changed_paths or []) if str(path)))
    snapshots = {item["path"]: item for item in (checkpoint.get("files") or []) if isinstance(item, dict) and item.get("path")}
    if not changed:
        for rel in targets:
            snap = snapshots.get(rel)
            if not snap:
                continue
            path = repo_path / rel
            exists_now = path.exists()
            content_now = path.read_text(encoding="utf-8") if exists_now else None
            if bool(snap.get("exists")) != exists_now or (snap.get("exists") and snap.get("content") != content_now):
                changed.append(rel)
    if not changed:
        return {"attempted": False, "restored": False, "reason": "no_changed_paths"}
    if not targets:
        return {"attempted": False, "restored": False, "reason": "checkpoint_targets_missing"}
    if any(path not in targets for path in changed):
        return {"attempted": False, "restored": False, "reason": "changed_paths_outside_checkpoint_scope"}
    for rel in changed:
        snap = snapshots.get(rel)
        if not snap:
            return {"attempted": False, "restored": False, "reason": f"missing_snapshot:{rel}"}
        path = repo_path / rel
        if snap.get("exists"):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(snap.get("content") or "", encoding="utf-8")
        elif path.exists():
            path.unlink()
    return {"attempted": True, "restored": True, "reason": "restored_checkpoint"}


def _operator_failure_fingerprint(returncode: int | None, stderr: str, stdout: Any) -> str:
    parts = [f"returncode={returncode}"]
    if stderr:
        parts.append(f"stderr={stderr}")
    if stdout:
        if isinstance(stdout, dict):
            parts.append("stdout=" + json.dumps(stdout, ensure_ascii=False, sort_keys=True))
        else:
            parts.append(f"stdout={stdout}")
    blob = " | ".join(parts)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def execute_operator(repo: str, run_id: str, task_index: int = 1) -> Dict[str, Any]:
    """Execute one planned task through the real dev-cli and persist an immutable receipt.

    `run` intentionally arms and dry-runs only.  This explicit tick is the mutation boundary;
    it cannot run without the mapper/plan/operator preflight artifacts created by `arm_run`.
    """
    status = read_status(repo, run_id)
    run_dir = Path(status["run_dir"])
    repo_path = Path(status["manifest"]["repo"]).resolve()
    contract = _load_json(run_dir / "task-contract.json")
    tasks = contract.get("tasks") or []
    if task_index < 1 or task_index > len(tasks):
        raise ValueError(f"task index out of range: {task_index}")
    plan_path = run_dir / "plan.json"
    mapper_path = run_dir / "mapper-context.json"
    operator_path = run_dir / "operator-receipt.json"
    if not plan_path.exists() or not mapper_path.exists() or not operator_path.exists():
        raise RuntimeError("execution requires fresh mapper, plan, and operator preflight receipts")
    plan = _load_json(plan_path)
    before = _repo_fingerprint(repo_path)
    current = _repo_fingerprint(repo_path)
    planned_state = plan.get("repo_state") or {}
    if planned_state and not _repo_state_equivalent(planned_state, current):
        raise RuntimeError("repository changed after planning; re-run mapper before execution")
    task = tasks[task_index - 1]
    attempt = int((status["state"] or {}).get("attempts", 0)) + 1
    targets = (plan.get("steps") or [])[task_index - 1].get("candidate_targets") or []
    target = targets[0] if targets else status["state"].get("operator", {}).get("target", "")
    if not target:
        raise RuntimeError("plan has no authorized operator target")
    _preflight_operator(repo_path, run_dir)
    argv = _devcli_cmd(
        repo_path,
        "task",
        _task_goal(task),
        "--root",
        str(repo_path),
        "--target",
        target,
        "--criteria",
        _criteria_text(task),
        "--constraints",
        _constraints_text(task),
        "--json",
        "--bound-paths",
        target,
    )
    checkpoint = _capture_operator_checkpoint(run_dir, repo_path, targets or [target])
    op_env = _devcli_env(repo_path, _operator_env())
    provider_config = {
        "model": op_env.get("SIMPLICIO_MODEL", ""),
        "effort": op_env.get("SIMPLICIO_CODEX_EFFORT", ""),
    }
    fake = os.environ.get("SIMPLICIO_LOOP_FAKE_OPERATOR_EXEC_JSON", "").strip()
    if fake:
        payload = json.loads(fake)
        for rel, content in (payload.get("write_files") or {}).items():
            path = repo_path / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(content), encoding="utf-8")
        returncode = int(payload.get("returncode", 0))
        stdout = payload.get("stdout", {})
        stderr = redact_sensitive_text(str(payload.get("stderr", "")))
        source = "env_override"
    else:
        try:
            result = subprocess.run(
                argv,
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                timeout=_operator_timeout("execute"),
                env=op_env,
            )
            returncode = result.returncode
            raw_stdout = (result.stdout or "").strip()
            try:
                stdout = json.loads(raw_stdout) if raw_stdout else {}
            except ValueError:
                stdout = {"raw": redact_sensitive_text(raw_stdout)}
            stderr = redact_sensitive_text((result.stderr or "").strip())
            source = "live_cli"
        except subprocess.TimeoutExpired as exc:
            returncode = None
            stdout = {}
            stderr = f"timed out after {exc.timeout}s"
            source = "live_cli"
    after = _repo_fingerprint(repo_path)
    changed = _changed_paths(repo_path)
    rollback = {"attempted": False, "restored": False, "reason": "not_needed"}
    if returncode != 0:
        rollback = _restore_operator_checkpoint(checkpoint, repo_path, changed)
        if rollback.get("restored"):
            changed = _changed_paths(repo_path)
            after = _repo_fingerprint(repo_path)
    receipt = {
        "schema": OPERATOR_RECEIPT_SCHEMA,
        "mode": "execute",
        "tool": "simplicio-dev-cli",
        "execution_state": "applied" if returncode == 0 else "blocked",
        "attempt": attempt,
        "retry_budget": 3,
        "target": target,
        "authorized_targets": targets,
        "target_within_repo": True,
        "goal": _task_goal(task),
        "argv": argv,
        "returncode": returncode,
        "stdout": stdout,
        "stderr": stderr,
        "timed_out": returncode is None,
        "started_at": _now(),
        "finished_at": _now(),
        "source": source,
        "provider_config": provider_config,
        "checkpoint": checkpoint,
        "rollback": rollback,
        "failure_fingerprint": "" if returncode == 0 else _operator_failure_fingerprint(returncode, stderr, stdout),
        "task_contract_hash": contract.get("collection_hash", ""),
        "plan_hash": hashlib.sha256(plan_path.read_bytes()).hexdigest(),
        "mapper_pack_hash": plan.get("mapper_pack_hash", ""),
        "repo_state_before": before,
        "repo_state_after": after,
        "changed_paths": changed,
        "diff_hash": after.get("tree_hash", ""),
    }
    _write_json(operator_path, receipt)
    state = status["state"]
    state["operator"] = {
        "ready": returncode == 0,
        "receipt": str(operator_path),
        "target": target,
        "execution_state": receipt["execution_state"],
    }
    state["current_action"] = "operator_executed" if returncode == 0 else "operator_failed"
    state["next_action"] = "watcher_behavioral_verification" if returncode == 0 else "repair_operator_or_plan"
    state["attempts"] = int(state.get("attempts", 0)) + 1
    _write_json(run_dir / "state.json", state)
    _transition(run_dir, state, "validating" if returncode == 0 else "blocked",
                "dev-cli execution receipt persisted", receipt=str(operator_path),
                extra={"changed_paths": changed})
    if returncode == 0:
        evidence = build_evidence_receipt(str(run_dir))
        _write_json(run_dir / "evidence-receipt.json", evidence)
        state = _load_json(run_dir / "state.json")
        state["evidence"] = {"ready": False, "receipt": str(run_dir / "evidence-receipt.json"), "status": evidence.get("status", "UNVERIFIED")}
        _write_json(run_dir / "state.json", state)
    return read_status(repo, run_id)


def read_status(repo: str, run_id: str = "") -> Dict[str, Any]:
    repo_path = Path(repo).resolve()
    runs_root = repo_path / ".orchestrator" / "runs"
    if not runs_root.exists():
        raise FileNotFoundError("no runs directory found")
    chosen = None
    if run_id:
        chosen = runs_root / run_id
    else:
        candidates = sorted([p for p in runs_root.iterdir() if p.is_dir()], key=lambda p: p.name)
        if not candidates:
            raise FileNotFoundError("no runs found")
        chosen = candidates[-1]
    manifest = _load_json(chosen / "manifest.json")
    state = _load_json(chosen / "state.json")
    state["completion"] = _completion_state(chosen, state.get("completion"))
    return {
        "run_dir": str(chosen),
        "manifest": manifest,
        "state": state,
    }


def change_phase(repo: str, run_id: str, to_phase: str, reason: str) -> Dict[str, Any]:
    status = read_status(repo, run_id)
    run_dir = Path(status["run_dir"])
    state = status["state"]
    if state.get("phase") in {"done", "cancelled"}:
        raise ValueError(f"run already terminal: {state.get('phase')}")
    if to_phase == "awaiting_decision":
        state["next_action"] = "mapper_scan_required"
    elif to_phase == "cancelled":
        state["next_action"] = "none"
    _transition(run_dir, state, to_phase, reason, receipt=str(run_dir / "state.json"))
    return read_status(repo, run_id)


def reconcile_delivery(repo: str, run_id: str, current_state: str, source_kind: str = "local",
                       source_payload: Dict[str, Any] | None = None) -> Dict[str, Any]:
    status = read_status(repo, run_id)
    run_dir = Path(status["run_dir"])
    manifest = status["manifest"]
    state = status["state"]
    receipt = build_delivery_receipt(str(run_dir), manifest.get("delivery_target") or "verified",
                                     current_state=current_state, source_kind=source_kind,
                                     source_payload=source_payload or {})
    write_delivery_receipt(str(run_dir), receipt)
    state["delivery"] = {
        "target": receipt["target"],
        "current_state": receipt["current_state"],
        "ready": receipt["ready"],
        "receipt": str(run_dir / "delivery-receipt.json"),
        "source_checked_at": receipt["source_checked_at"],
        "source_kind": source_kind,
    }
    if receipt["ready"]:
        state["current_action"] = "delivery_reconciled"
        state["next_action"] = "completion_oracle"
        next_phase = "delivering" if current_state not in {"verified", "done"} else "validating"
    else:
        state["current_action"] = "delivery_reconciliation_failed"
        state["next_action"] = "collect_missing_delivery_evidence"
        next_phase = "partial"
        state.setdefault("blockers", [])
        fail_gate = next((gate for gate in receipt.get("gates", []) if gate.get("status") == "fail"), None)
        if fail_gate:
            state["blockers"] = [fail_gate.get("detail", "delivery reconciliation failed")]
    _write_json(run_dir / "state.json", state)
    _transition(run_dir, state, next_phase, "delivery state reconciled", receipt=str(run_dir / "delivery-receipt.json"))
    return read_status(repo, run_id)


def apply_human_decision(repo: str, run_id: str, decision_id: str, answer: str,
                         impact: str = "behavior-change") -> Dict[str, Any]:
    status = read_status(repo, run_id)
    run_dir = Path(status["run_dir"])
    state = status["state"]
    contract_payload = _load_json(_contract_path(run_dir))
    tasks = contract_payload.get("tasks") or []
    if not tasks:
        raise ValueError("task contract collection is empty")
    changed = False
    for task in tasks:
        ledger = task.setdefault("decision_ledger", [])
        for item in ledger:
            if item.get("id") == decision_id:
                item["resolved"] = True
                item["answer"] = answer
                item["resolved_at"] = _now()
                item["resolution_impact"] = impact
                changed = True
        for bucket_name in ("questions", "assumptions", "blockers"):
            for item in task.get(bucket_name) or []:
                if item.get("id") == decision_id:
                    item["resolved"] = True
                    item["answer"] = answer
                    item["resolved_at"] = _now()
                    item["resolution_impact"] = impact
                    changed = True
    if not changed:
        raise ValueError(f"decision id not found: {decision_id}")
    contract_payload["revision"] = int(contract_payload.get("revision", 1)) + 1
    contract_payload["updated_at"] = _now()
    _write_json(_contract_path(run_dir), contract_payload)
    invalidated = []
    for name in ("plan.json", "operator-receipt.json", "evidence-receipt.json", "delivery-receipt.json"):
        path = run_dir / name
        if path.exists():
            path.unlink()
            invalidated.append(name)
    state["phase"] = "awaiting_decision"
    state["updated_at"] = _now()
    state["current_action"] = "human_decision_applied"
    state["next_action"] = "rebuild_plan_from_updated_contract"
    state["operator"] = {"ready": False, "receipt": "", "target": "", "execution_state": "invalidated"}
    state["evidence"] = {"ready": False, "receipt": "", "status": "INVALIDATED"}
    state["delivery"] = {"target": state.get("delivery_target"), "current_state": "planned", "ready": False, "receipt": ""}
    state["completion"] = _default_completion_state()
    state["blockers"] = []
    _write_json(run_dir / "state.json", state)
    _transition(run_dir, state, "awaiting_decision", "human decision applied; dependent artifacts invalidated",
                receipt=str(_contract_path(run_dir)), extra={"decision_id": decision_id, "invalidated": invalidated})
    return read_status(repo, run_id)


def sync_source_state(repo: str, run_id: str, source: str, external_repo: str = "",
                      pr: int | None = None, tag: str = "") -> Dict[str, Any]:
    status = read_status(repo, run_id)
    manifest = status["manifest"]
    target = manifest.get("delivery_target") or "verified"
    if source != "github":
        raise ValueError(f"unsupported source: {source!r}")
    payload = github_delivery_payload(external_repo, pr=pr, tag=tag, target_state=target)
    current_state = infer_github_delivery_state(payload)
    return reconcile_delivery(repo, run_id, current_state, source_kind="github", source_payload=payload)

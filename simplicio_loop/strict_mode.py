"""Strict operator-only loop mode with adaptive Runtime bind.

When ``SIMPLICIO_LOOP_STRICT`` is on, the loop refuses silent degradation to
LLM hand-survey / hand-edit: bound operators are mandatory, evidence is
mandatory, and mutation authority stays fail-closed.

Runtime (``simplicio`` binary from ``simplicio-runtime``) is **adaptive**:
if it is available and operational, the loop **uses and requires** it for the
run (``runtime-backed`` effects). If it is absent, the core mapper→dev-cli
loop continues unless the operator forced ``SIMPLICIO_LOOP_REQUIRE_RUNTIME=1``.

Fast follows the same adaptive pattern under strict mode: when ``simplicio-fast``
is on PATH, strict treats it as required so the session cannot silently drop it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Any, Mapping, Optional, Sequence

TRUE_VALUES = frozenset({"1", "true", "yes", "on", "strict", "full-stack", "required"})
FALSE_VALUES = frozenset({"0", "false", "no", "off", "disabled", "standalone", "legacy"})

CORE_OPERATORS: tuple[str, ...] = ("simplicio-mapper", "simplicio-dev-cli")
RUNTIME_BINARY = "simplicio"
FAST_BINARY = "simplicio-fast"
# Accept either action binary name for the operate role.
ACTION_ALIASES: tuple[str, ...] = ("simplicio-dev-cli", "simplicio-py")


def _env(env: Optional[Mapping[str, str]] = None) -> Mapping[str, str]:
    return os.environ if env is None else env


def env_flag(name: str, *, env: Optional[Mapping[str, str]] = None, default: str = "") -> str:
    return str(_env(env).get(name, default) or "").strip().lower()


def is_truthy(value: str) -> bool:
    return value in TRUE_VALUES


def is_falsy(value: str) -> bool:
    return value in FALSE_VALUES


def strict_enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    """Return True when strict operator-only mode is armed."""
    source = _env(env)
    if is_truthy(env_flag("SIMPLICIO_LOOP_STRICT", env=source)):
        return True
    mode = env_flag("SIMPLICIO_LOOP_MODE", env=source)
    if mode in {"strict", "full-stack"}:
        return True
    # Active orchestrator state + explicit SIMPLICIO_LOOP=1 also arms strict for hosts.
    if is_truthy(env_flag("SIMPLICIO_LOOP", env=source)) and is_truthy(
        env_flag("SIMPLICIO_LOOP_STRICT_DEFAULT", env=source, default="0")
    ):
        return True
    return False


def _probe_version(binary: str, args: Sequence[str] = ("--version",), timeout: float = 8.0) -> dict[str, Any]:
    path = shutil.which(binary)
    if not path:
        return {"binary": binary, "present": False, "operational": False, "version": "", "error": "not on PATH"}
    try:
        completed = subprocess.run(
            [path, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        ok = completed.returncode == 0
        version = ""
        if ok:
            text = (completed.stdout or completed.stderr or "").strip()
            version = text.splitlines()[0] if text else "ok"
        return {
            "binary": binary,
            "present": True,
            "operational": ok,
            "version": version,
            "error": "" if ok else (completed.stderr or completed.stdout or "probe failed")[:200],
            "path": path,
        }
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "binary": binary,
            "present": True,
            "operational": False,
            "version": "",
            "error": str(exc)[:200],
            "path": path,
        }


def runtime_status(env: Optional[Mapping[str, str]] = None) -> dict[str, Any]:
    """Probe the native Runtime CLI (``simplicio``)."""
    del env  # probe is process-global PATH
    return _probe_version(RUNTIME_BINARY, ("--version",))


def fast_status(env: Optional[Mapping[str, str]] = None) -> dict[str, Any]:
    del env
    return _probe_version(FAST_BINARY, ("--version",))


def action_operator_status(env: Optional[Mapping[str, str]] = None) -> dict[str, Any]:
    del env
    for name in ACTION_ALIASES:
        status = _probe_version(name, ("--help",) if name == "simplicio-dev-cli" else ("--version",))
        if status["operational"]:
            status["role"] = "operate"
            status["resolved_as"] = name
            return status
    # Prefer reporting the primary name.
    status = _probe_version("simplicio-dev-cli", ("--help",))
    status["role"] = "operate"
    status["resolved_as"] = "simplicio-dev-cli"
    return status


def require_runtime_mode(env: Optional[Mapping[str, str]] = None) -> str:
    """Return auto|required|off for Runtime binding policy."""
    raw = env_flag("SIMPLICIO_LOOP_REQUIRE_RUNTIME", env=env, default="auto")
    if not raw:
        return "auto"
    if is_falsy(raw):
        return "off"
    if raw in {"1", "true", "yes", "on", "required", "strict"}:
        return "required"
    if raw == "auto":
        return "auto"
    # Unknown → fail-closed to auto (never silently off).
    return "auto"


def required_bound_operators(env: Optional[Mapping[str, str]] = None) -> list[str]:
    """Binaries the running loop must keep available.

    Always: mapper + operate (dev-cli or py alias checked separately by callers).
    Runtime: when mode=required, always; when mode=auto, only if operational now
    (then it stays required for the rest of the run so it cannot silently drop).
    Fast: under strict mode, if operational now it is required.
    """
    source = _env(env)
    required: list[str] = list(CORE_OPERATORS)
    rt_mode = require_runtime_mode(source)
    rt = runtime_status(source)
    if rt_mode == "required":
        required.append(RUNTIME_BINARY)
    elif rt_mode == "auto" and rt["operational"]:
        required.append(RUNTIME_BINARY)

    if strict_enabled(source):
        fast = fast_status(source)
        if fast["operational"]:
            required.append(FAST_BINARY)
        # Under strict, prefer Fast required only when present; never invent Fast if missing.

    # De-dupe preserving order
    seen: set[str] = set()
    ordered: list[str] = []
    for name in required:
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def missing_required_operators(env: Optional[Mapping[str, str]] = None) -> list[str]:
    """Return required binaries that are missing or non-operational."""
    missing: list[str] = []
    required = required_bound_operators(env)
    # Operate role: either alias satisfies simplicio-dev-cli requirement.
    action_ok = action_operator_status(env)["operational"]
    for name in required:
        if name == "simplicio-dev-cli":
            if not action_ok:
                missing.append("simplicio-dev-cli")
            continue
        if name == RUNTIME_BINARY:
            if not runtime_status(env)["operational"]:
                missing.append(RUNTIME_BINARY)
            continue
        if name == FAST_BINARY:
            if not fast_status(env)["operational"]:
                missing.append(FAST_BINARY)
            continue
        # mapper and others
        status = _probe_version(name, ("--version",))
        if not status["operational"]:
            missing.append(name)
    return missing


def resolve_execution_profile(env: Optional[Mapping[str, str]] = None) -> str:
    """Pick standalone vs runtime-backed.

    Explicit ``SIMPLICIO_EXECUTION_PROFILE`` wins when set to a valid value.
    Otherwise: runtime-backed when Runtime is operational (auto/required), else standalone.
    """
    source = _env(env)
    explicit = env_flag("SIMPLICIO_EXECUTION_PROFILE", env=source)
    if explicit in {"standalone", "runtime-backed"}:
        return explicit
    if explicit and explicit not in {"auto", ""}:
        # Invalid explicit value — raise at call site via runner; here fall through to auto.
        pass
    rt_mode = require_runtime_mode(source)
    if rt_mode == "off":
        return "standalone"
    if runtime_status(source)["operational"]:
        return "runtime-backed"
    if rt_mode == "required":
        # Caller must treat missing runtime as BLOCKED; profile still names the intent.
        return "runtime-backed"
    return "standalone"


def hand_edit_forbidden(env: Optional[Mapping[str, str]] = None) -> bool:
    """Under strict mode, host hand-edit tools must not mutate the repo."""
    if strict_enabled(env):
        return True
    return is_truthy(env_flag("SIMPLICIO_LOOP_FORBID_HAND_EDIT", env=env))


def evidence_required_locked(env: Optional[Mapping[str, str]] = None) -> bool:
    """Strict mode refuses evidence_required=false on the scratchpad."""
    return strict_enabled(env)


def recommended_env(env: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    """Env vars to export for a strict, runtime-aware armada."""
    profile = resolve_execution_profile(env)
    out = {
        "SIMPLICIO_LOOP": "1",
        "SIMPLICIO_LOOP_STRICT": "1",
        "SIMPLICIO_REQUIRE_MUTATION_AUTHORITY": "1",
        "SIMPLICIO_LOOP_AUTO_PLANNING_RECEIPT": "1",
        "SIMPLICIO_LOOP_REQUIRE_RUNTIME": "auto",
        "SIMPLICIO_EXECUTION_PROFILE": profile,
        "SIMPLICIO_LOOP_FORBID_HAND_EDIT": "1",
    }
    if fast_status(env)["operational"]:
        out["SIMPLICIO_FAST_MODE"] = "required"
    return out


def preflight_payload(repo: str, *, strict: bool = False, env: Optional[Mapping[str, str]] = None) -> dict[str, Any]:
    """Build the machine-readable preflight document (strict-aware)."""
    source = dict(_env(env))
    if strict:
        source["SIMPLICIO_LOOP_STRICT"] = "1"
    mapper = _probe_version("simplicio-mapper", ("--version",))
    action = action_operator_status(source)
    runtime = runtime_status(source)
    fast = fast_status(source)
    operators = [
        {
            "name": "simplicio-mapper",
            "present": mapper["operational"],
            "version": mapper.get("version", ""),
            "error": mapper.get("error", ""),
        },
        {
            "name": "simplicio-dev-cli",
            "present": action["operational"],
            "version": action.get("version", ""),
            "error": action.get("error", ""),
            "resolved_as": action.get("resolved_as", "simplicio-dev-cli"),
        },
        {
            "name": "simplicio-py",
            "present": action.get("resolved_as") == "simplicio-py" and action["operational"],
            "version": action.get("version", "") if action.get("resolved_as") == "simplicio-py" else "",
            "error": "",
        },
        {
            "name": "simplicio-runtime",
            "present": runtime["operational"],
            "version": runtime.get("version", ""),
            "error": runtime.get("error", ""),
        },
        {
            "name": "simplicio-fast",
            "present": fast["operational"],
            "version": fast.get("version", ""),
            "error": fast.get("error", ""),
        },
    ]
    required = required_bound_operators(source)
    missing = missing_required_operators(source)
    profile = resolve_execution_profile(source)
    runtime_available = runtime["operational"]
    strict_on = strict_enabled(source)
    all_present = not missing
    degraded: list[str] = []
    if not runtime_available:
        degraded.append("runtime-integration")
    if not fast["operational"]:
        degraded.append("fast-integration")
    return {
        "schema": "simplicio.preflight/v1",
        "repo": repo,
        "strict": strict_on,
        "all_present": all_present,
        "operators": operators,
        "required_operators": required,
        "missing_operators": missing,
        "runtime_available": runtime_available,
        "runtime_operational": runtime_available,
        "fast_available": fast["operational"],
        "execution_profile": profile,
        "hand_edit_forbidden": hand_edit_forbidden(source),
        "recommended_env": recommended_env(source) if strict_on else {},
        "degraded_features": degraded,
    }


__all__ = [
    "CORE_OPERATORS",
    "action_operator_status",
    "evidence_required_locked",
    "fast_status",
    "hand_edit_forbidden",
    "missing_required_operators",
    "preflight_payload",
    "recommended_env",
    "require_runtime_mode",
    "required_bound_operators",
    "resolve_execution_profile",
    "runtime_status",
    "strict_enabled",
]

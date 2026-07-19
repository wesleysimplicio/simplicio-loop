"""Real ``CodexRuntimeDriver`` / ``ClaudeRuntimeDriver`` execution (issue #287, final
slice: "execução Codex + Claude e receipts auditáveis").

Earlier slices of #287 (``model_registry.py``, ``model_router.py``,
``runtime_execution_receipt.py``) deliberately stopped short of invoking a real
runtime -- they only decide/shape. This module is the driver layer they call out
to: a genuine, non-interactive ``subprocess`` invocation of the installed
``codex``/``claude`` CLIs, never a simulated response.

Design rules (mirrors ``model_registry.py``'s ``probe()`` discipline):

- A driver only ever reports what it actually measured. A field it cannot
  observe (e.g. the CLI never echoes back which model served the request) is
  recorded as the literal string ``"UNAVAILABLE"`` -- never guessed to equal the
  requested model.
- A missing binary, an auth/policy error, a timeout, and a genuine success are
  all *real* outcomes and are all reported honestly via :class:`RuntimeDriverResult`
  -- this module never turns a real failure into a fabricated "success" receipt.
- ``execute()`` always performs the real subprocess call when the binary is on
  PATH; it does not require network access to *attempt* the call (whether the
  call itself succeeds is a separate, honestly-reported question).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .runtime_execution_receipt import STOP_REASONS, UNAVAILABLE, build_runtime_execution_receipt

DEFAULT_TIMEOUT_SECONDS = 180
_SHELL_SHIM_SUFFIXES = (".cmd", ".bat")
_NPM_SHIM_JS_RE = re.compile(r'"%dp0%\\(?P<rel>[^"]+?\.js)"')


def _resolve_npm_shim(shim_path: str) -> Optional[List[str]]:
    """Parse a Windows npm-generated ``.CMD`` launcher to find the real ``node
    <script.js>`` invocation it wraps, so the real CLI can be exec'd directly
    (``argv``, ``shell=False``) instead of routing arbitrary task text through
    ``cmd.exe`` (where ``&``/``|``/``%``/``^`` are shell metacharacters -- a
    genuine injection risk this repo's own ``action_gate.py`` guards against
    elsewhere). Returns ``None`` when the shim doesn't match the standard
    template, so the caller can fall back rather than silently mis-invoke.
    """
    try:
        text = Path(shim_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = _NPM_SHIM_JS_RE.search(text)
    if not match:
        return None
    js_abs = Path(shim_path).parent / match.group("rel")
    if not js_abs.exists():
        return None
    node = shutil.which("node")
    if not node:
        return None
    return [node, str(js_abs)]


def _run_cli(argv: Sequence[str], *, cwd: Optional[str], timeout: int) -> "subprocess.CompletedProcess[str]":
    """Run a real CLI invocation, resolving npm shell-shim wrappers on Windows.

    ``shutil.which`` may resolve a binary like ``codex`` to a Windows ``.CMD``
    shim (npm's launcher) rather than a native executable; ``CreateProcess``
    cannot exec those directly with ``shell=False`` (raises
    ``FileNotFoundError``/WinError 2) even though the binary genuinely exists
    and genuinely runs from an interactive shell. This resolves the shim to
    its real ``node <script.js>`` invocation and execs that directly
    (argv-list, no shell) -- never routes task/prompt text through
    ``cmd.exe``, which would let shell metacharacters in the prompt escape
    into command interpretation. A shim that doesn't match the standard
    template falls back to ``shell=True`` as a last resort (still the real
    binary, just less safely invoked) rather than skipping the attempt.
    """
    argv = list(argv)
    resolved = shutil.which(argv[0]) or argv[0]
    common_kwargs = dict(cwd=cwd, capture_output=True, text=True, timeout=timeout,
                          encoding="utf-8", errors="replace")
    if os.name == "nt" and resolved.lower().endswith(_SHELL_SHIM_SUFFIXES):
        prefix = _resolve_npm_shim(resolved)
        if prefix is not None:
            return subprocess.run(prefix + argv[1:], **common_kwargs)
        command = subprocess.list2cmdline([resolved] + argv[1:])
        return subprocess.run(command, shell=True, **common_kwargs)
    argv[0] = resolved
    return subprocess.run(argv, **common_kwargs)


class RuntimeDriverError(ValueError):
    """Raised only for programmer error (bad arguments) -- never for a runtime's own
    failure, which is reported through :class:`RuntimeDriverResult` instead so it can
    become an honest audit receipt rather than an exception that swallows evidence."""


@dataclass
class RuntimeDriverResult:
    """What a driver actually observed from one real invocation."""

    ok: bool
    exit_status: Optional[int]
    stdout: str
    stderr: str
    duration_seconds: float
    stop_reason: str
    resolved_model: Optional[Dict[str, Any]]
    usage: Dict[str, Any]
    argv: List[str]
    error: str = ""

    def __post_init__(self) -> None:
        if self.stop_reason not in STOP_REASONS:
            raise RuntimeDriverError(
                f"stop_reason must be one of {sorted(STOP_REASONS)}, got {self.stop_reason!r}"
            )


def _unavailable_usage(latency_seconds: Optional[float] = None) -> Dict[str, Any]:
    return {
        "tokens": UNAVAILABLE,
        "cost_usd": UNAVAILABLE,
        "latency_seconds": round(latency_seconds, 3) if latency_seconds is not None else UNAVAILABLE,
    }


class _BaseCliRuntimeDriver:
    """Shared subprocess plumbing for a headless/non-interactive CLI driver."""

    name = "base"
    binary = ""
    provider = ""

    def is_installed(self) -> bool:
        return bool(self.binary) and shutil.which(self.binary) is not None

    def version(self) -> str:
        """A real, cheap, non-mutating ``--version`` probe -- never fabricated."""
        if not self.is_installed():
            return UNAVAILABLE
        try:
            proc = _run_cli([self.binary, "--version"], cwd=None, timeout=20)
            text = (proc.stdout or proc.stderr or "").strip()
            return text or UNAVAILABLE
        except (OSError, subprocess.SubprocessError):
            return UNAVAILABLE

    def build_argv(self, prompt: str, *, extra_args: Sequence[str] = ()) -> List[str]:
        raise NotImplementedError

    def parse_result(self, argv: List[str], proc: "subprocess.CompletedProcess[str]",
                      duration: float) -> RuntimeDriverResult:
        raise NotImplementedError

    def execute(self, prompt: str, *, cwd: Optional[Path] = None,
                timeout: int = DEFAULT_TIMEOUT_SECONDS,
                extra_args: Sequence[str] = ()) -> RuntimeDriverResult:
        """Perform one real, non-interactive invocation of this runtime's CLI.

        Never simulated: when the binary is missing this returns a genuine
        ``UNAVAILABLE``-flavoured failure rather than skipping the attempt;
        when the binary is present the actual subprocess is launched and its
        real stdout/stderr/exit status/duration are what gets reported.
        """
        if not prompt or not prompt.strip():
            raise RuntimeDriverError("prompt is required")
        if not self.is_installed():
            return RuntimeDriverResult(
                ok=False, exit_status=None, stdout="", stderr="",
                duration_seconds=0.0, stop_reason="error", resolved_model=None,
                usage=_unavailable_usage(0.0), argv=[self.binary or "<unset>"],
                error=f"{self.binary!r} binary not found on PATH",
            )
        argv = self.build_argv(prompt, extra_args=extra_args)
        started = time.monotonic()
        try:
            proc = _run_cli(argv, cwd=str(cwd) if cwd else None, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            duration = time.monotonic() - started
            return RuntimeDriverResult(
                ok=False, exit_status=None,
                stdout=str(exc.stdout or ""), stderr=str(exc.stderr or ""),
                duration_seconds=duration, stop_reason="timeout", resolved_model=None,
                usage=_unavailable_usage(duration), argv=argv,
                error=f"timed out after {timeout}s",
            )
        except (OSError, subprocess.SubprocessError) as exc:
            duration = time.monotonic() - started
            return RuntimeDriverResult(
                ok=False, exit_status=None, stdout="", stderr="",
                duration_seconds=duration, stop_reason="error", resolved_model=None,
                usage=_unavailable_usage(duration), argv=argv, error=str(exc),
            )
        duration = time.monotonic() - started
        return self.parse_result(argv, proc, duration)

    def build_receipt(self, *, route_id: str, requested: Dict[str, Any],
                       session: Dict[str, Any], result: RuntimeDriverResult,
                       tree: Dict[str, Any], previous_route_id: str = "",
                       fallback_reason_code: str = "",
                       evidence_refs: Optional[Sequence[str]] = None) -> Dict[str, Any]:
        """Wrap one :class:`RuntimeDriverResult` in a genuine ``runtime-execution-receipt``.

        This never invents a value ``result`` did not measure; ``build_runtime_execution_receipt``
        itself enforces the "UNAVAILABLE, never fabricated" discipline for any field
        this driver could not observe.
        """
        argv_redacted = [str(a) for a in result.argv[:-1]] + (
            ["<prompt redacted>"] if result.argv else []
        )
        return build_runtime_execution_receipt(
            route_id=route_id,
            requested=requested,
            resolved=result.resolved_model,
            driver={
                "name": self.name,
                "binary": self.binary,
                "version": self.version(),
                "identity_verified": self.is_installed(),
            },
            session=session,
            argv_redacted=argv_redacted,
            env_allowlist=[],
            tree=tree,
            exit_status=result.exit_status,
            duration_seconds=result.duration_seconds,
            stop_reason=result.stop_reason,
            usage=result.usage,
            evidence_refs=evidence_refs,
            previous_route_id=previous_route_id,
            fallback_reason_code=fallback_reason_code,
        )


class CodexRuntimeDriver(_BaseCliRuntimeDriver):
    """Real driver for the ``codex`` CLI's non-interactive ``exec`` subcommand.

    Invokes ``codex exec --json <prompt>`` and parses the genuine JSONL event
    stream Codex prints (``thread.started`` / ``item.completed`` /
    ``turn.completed``) -- token usage comes from the real ``turn.completed``
    event; the final agent message comes from the real ``agent_message`` item.
    Codex's JSON stream does not currently echo back which model served the
    turn, so ``resolved.model_id`` is honestly ``UNAVAILABLE`` unless a future
    Codex version adds that field (checked defensively below, never assumed).
    """

    name = "codex"
    binary = "codex"
    provider = "openai"

    def build_argv(self, prompt: str, *, extra_args: Sequence[str] = ()) -> List[str]:
        return [self.binary, "exec", "--json", *extra_args, prompt]

    def parse_result(self, argv: List[str], proc: "subprocess.CompletedProcess[str]",
                      duration: float) -> RuntimeDriverResult:
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        model_id = UNAVAILABLE
        tokens: Any = UNAVAILABLE
        final_message = ""
        for line in stdout.splitlines():
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            etype = event.get("type")
            if etype == "item.completed":
                item = event.get("item") or {}
                if isinstance(item, dict) and item.get("type") == "agent_message":
                    final_message = str(item.get("text") or final_message)
            elif etype == "turn.completed":
                usage = event.get("usage") or {}
                if isinstance(usage, dict) and usage:
                    numeric = [v for v in usage.values() if isinstance(v, (int, float))]
                    if numeric:
                        tokens = sum(numeric)
            # Defensive, never assumed: some Codex versions may add a model field.
            candidate_model = event.get("model") or (event.get("item") or {}).get("model") \
                if isinstance(event.get("item"), dict) else event.get("model")
            if candidate_model:
                model_id = str(candidate_model)
        ok = proc.returncode == 0
        resolved_model = {
            "runtime": "codex",
            "provider": self.provider,
            "model_id": model_id,
            "verified": model_id != UNAVAILABLE,
        }
        error = "" if ok else (stderr.strip() or f"codex exec exited with status {proc.returncode}")
        return RuntimeDriverResult(
            ok=ok,
            exit_status=proc.returncode,
            stdout=final_message or stdout,
            stderr=stderr,
            duration_seconds=round(duration, 3),
            stop_reason="completed" if ok else "error",
            resolved_model=resolved_model,
            usage={"tokens": tokens, "cost_usd": UNAVAILABLE, "latency_seconds": round(duration, 3)},
            argv=argv,
            error=error,
        )


class ClaudeRuntimeDriver(_BaseCliRuntimeDriver):
    """Real driver for the ``claude`` CLI's non-interactive print mode (``-p``).

    Invokes ``claude -p <prompt> --output-format json``. Claude Code's JSON
    result envelope reports ``is_error``/``api_error_status`` on failure (e.g.
    an organization policy blocking Claude subscription access to Claude Code --
    a real, observed failure mode in some sandboxes, distinct from "binary
    missing") and ``usage``/``total_cost_usd`` on success. It does not echo
    back a resolved model id, so ``resolved.model_id`` stays honestly
    ``UNAVAILABLE`` rather than assumed equal to the requested model.
    """

    name = "claude"
    binary = "claude"
    provider = "anthropic"

    def build_argv(self, prompt: str, *, extra_args: Sequence[str] = ()) -> List[str]:
        return [self.binary, "-p", prompt, "--output-format", "json", *extra_args]

    def parse_result(self, argv: List[str], proc: "subprocess.CompletedProcess[str]",
                      duration: float) -> RuntimeDriverResult:
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        try:
            payload: Dict[str, Any] = json.loads(stdout) if stdout.strip() else {}
        except ValueError:
            payload = {}
        is_error = bool(payload.get("is_error", proc.returncode != 0))
        ok = proc.returncode == 0 and not is_error
        usage = payload.get("usage") or {}
        tokens: Any = UNAVAILABLE
        if isinstance(usage, dict) and usage:
            numeric_fields = [
                usage.get("input_tokens"), usage.get("output_tokens"),
                usage.get("cache_creation_input_tokens"), usage.get("cache_read_input_tokens"),
            ]
            numeric = [v for v in numeric_fields if isinstance(v, (int, float))]
            if numeric:
                tokens = sum(numeric)
        resolved_model = {
            "runtime": "claude",
            "provider": self.provider,
            "model_id": UNAVAILABLE,
            "verified": False,
        }
        stop_reason = "completed" if ok else "error"
        error = "" if ok else str(
            payload.get("result") or stderr.strip() or f"claude -p exited with status {proc.returncode}"
        )
        return RuntimeDriverResult(
            ok=ok,
            exit_status=proc.returncode if proc.returncode is not None else payload.get("api_error_status"),
            stdout=str(payload.get("result") or stdout),
            stderr=stderr,
            duration_seconds=round(duration, 3),
            stop_reason=stop_reason,
            resolved_model=resolved_model,
            usage={
                "tokens": tokens,
                "cost_usd": payload.get("total_cost_usd", UNAVAILABLE),
                "latency_seconds": round(duration, 3),
            },
            argv=argv,
            error=error,
        )


_DRIVERS_BY_RUNTIME = {
    "codex": CodexRuntimeDriver,
    "claude": ClaudeRuntimeDriver,
}


def driver_for_runtime(runtime: str) -> Optional[_BaseCliRuntimeDriver]:
    """Return the real driver instance for a ``model_router``-selected ``runtime``
    string, or ``None`` when no real driver is wired for it yet (e.g. any of the
    other 10 adapters this repo supports) -- callers must treat ``None`` as
    "no live execution possible", never silently skip to a fabricated success."""
    cls = _DRIVERS_BY_RUNTIME.get(str(runtime or "").strip())
    return cls() if cls else None


def probe_cli_hook(entry: Dict[str, Any]) -> Dict[str, Any]:
    """``ModelCapabilityRegistry`` probe hook: a real, non-mutating ``--version``
    check for the ``codex``/``claude`` runtimes, wired via ``probe_hooks={...}``
    at registry construction time (see ``model_registry.py`` docstring)."""
    runtime = str(entry.get("runtime") or "")
    driver = driver_for_runtime(runtime)
    if driver is None:
        return {"status": "UNVERIFIED", "available": False, "detail": "no real driver wired for this runtime"}
    installed = driver.is_installed()
    if not installed:
        return {"status": "MEASURED", "available": False, "detail": f"{driver.binary} not found on PATH"}
    version = driver.version()
    return {
        "status": "MEASURED",
        "available": version != UNAVAILABLE,
        "detail": f"{driver.binary} --version -> {version}" if version != UNAVAILABLE else "version probe failed",
    }


CLI_PROBE_HOOKS = {"codex": probe_cli_hook, "claude": probe_cli_hook}


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "CLI_PROBE_HOOKS",
    "ClaudeRuntimeDriver",
    "CodexRuntimeDriver",
    "RuntimeDriverError",
    "RuntimeDriverResult",
    "driver_for_runtime",
    "probe_cli_hook",
]

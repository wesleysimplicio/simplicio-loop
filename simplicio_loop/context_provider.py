"""Public Loop-to-Fast context provider.

The Loop owns the public boundary; Fast owns snapshot reads.  This module never
reads source files or starts a local model.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

FAST_CONTEXT_SCHEMA = "simplicio.fast.context/v1"
PACKET_SCHEMA = "simplicio.context-packet/v1"
ERROR_SCHEMA = "simplicio.loop-context-error/v1"
DEFAULT_SNAPSHOT = Path(".simplicio") / "fast" / "project.sfast"
DEFAULT_MAX_BYTES = 131072
DEFAULT_TIMEOUT_SECONDS = 60.0


class ContextProviderError(ValueError):
    """Raised when the public Fast context boundary cannot be trusted."""


def _canonical(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _resolved_repo(repo: str | Path) -> Path:
    root = Path(repo).expanduser().resolve()
    if not root.is_dir():
        raise ContextProviderError(f"repository root is not a directory: {root}")
    return root


def _resolved_snapshot(root: Path, snapshot: Optional[str | Path]) -> Path:
    candidate = Path(snapshot) if snapshot is not None else DEFAULT_SNAPSHOT
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.expanduser().resolve()
    simplicio_root = (root / ".simplicio").resolve()
    try:
        resolved.relative_to(simplicio_root)
    except ValueError as exc:
        raise ContextProviderError("Fast snapshots must stay under <repo>/.simplicio") from exc
    if resolved.suffix != ".sfast":
        raise ContextProviderError("Fast snapshot must use the .sfast extension")
    if not resolved.is_file():
        raise ContextProviderError(f"Fast snapshot does not exist: {resolved}")
    return resolved


def _executable(fast_bin: Optional[str | Path]) -> str:
    if fast_bin is not None:
        return str(fast_bin)
    command = shutil.which("simplicio-fast")
    if not command:
        raise ContextProviderError("simplicio-fast is not installed or not on PATH")
    return command


def _packet_from_fast(raw: Mapping[str, Any], *, term: str) -> Dict[str, Any]:
    if raw.get("schema") != FAST_CONTEXT_SCHEMA:
        raise ContextProviderError("Fast returned an unexpected context schema")
    provenance = raw.get("provenance")
    if not isinstance(provenance, Mapping):
        raise ContextProviderError("Fast context is missing provenance")
    generation = str(provenance.get("snapshot_generation") or "").strip()
    snapshot_sha256 = str(provenance.get("snapshot_sha256") or "").strip()
    if not generation or not snapshot_sha256:
        raise ContextProviderError("Fast provenance must include snapshot generation and sha256")
    spans = raw.get("spans")
    if not isinstance(spans, list) or any(not isinstance(span, Mapping) for span in spans):
        raise ContextProviderError("Fast context spans must be a list of mappings")
    normalized_provenance = dict(provenance)
    normalized_provenance.update({
        "provider": "simplicio-fast",
        "local_llm_started": False,
        "request_term": term,
    })
    payload: Dict[str, Any] = {
        "generation": generation,
        "spans": [dict(span) for span in spans],
        "provenance": normalized_provenance,
        "fidelity": "targeted",
        "complete": bool(raw.get("complete", True)),
        "source": "loop-fast",
    }
    packet = {
        "schema": PACKET_SCHEMA,
        **payload,
        "content_sha256": hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest(),
    }
    return packet


def request_context(
    repo: str | Path,
    term: str,
    *,
    snapshot: Optional[str | Path] = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    fast_bin: Optional[str | Path] = None,
) -> Dict[str, Any]:
    """Request a validated Agent-compatible context packet from Fast."""
    normalized_term = str(term).strip()
    if not normalized_term:
        raise ContextProviderError("context term must not be empty")
    if max_bytes < 1024:
        raise ContextProviderError("max_bytes must be at least 1024")
    if timeout <= 0:
        raise ContextProviderError("timeout must be positive")
    root = _resolved_repo(repo)
    snapshot_path = _resolved_snapshot(root, snapshot)
    command: Sequence[str] = (
        _executable(fast_bin),
        "context",
        normalized_term,
        "--root",
        str(root),
        "-s",
        str(snapshot_path),
        "--max-bytes",
        str(max_bytes),
        "--json",
    )
    try:
        completed = subprocess.run(
            list(command),
            cwd=str(root),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ContextProviderError(f"simplicio-fast context request failed: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "no diagnostic").strip()
        raise ContextProviderError(f"simplicio-fast context exited {completed.returncode}: {detail[-1000:]}")
    try:
        raw = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ContextProviderError("simplicio-fast returned invalid JSON") from exc
    if not isinstance(raw, Mapping):
        raise ContextProviderError("simplicio-fast context response must be an object")
    return _packet_from_fast(raw, term=normalized_term)


__all__ = [
    "ContextProviderError",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_SNAPSHOT",
    "DEFAULT_TIMEOUT_SECONDS",
    "ERROR_SCHEMA",
    "FAST_CONTEXT_SCHEMA",
    "PACKET_SCHEMA",
    "request_context",
]
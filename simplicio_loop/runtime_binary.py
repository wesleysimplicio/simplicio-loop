"""Canonical Simplicio Runtime binary and MCP capability preflight (#692)."""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

MCP_PROTOCOL = "2024-11-05"
SCHEMA = "simplicio.runtime-preflight/v1"


class RuntimeBinaryError(RuntimeError):
    """No safe canonical Runtime executable or capability set was found."""


@dataclass(frozen=True)
class RuntimeBinary:
    path: str
    source: str
    version: Optional[str] = None


def _usable(path: str) -> str:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        found = shutil.which(str(candidate))
        if found:
            candidate = Path(found)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise RuntimeBinaryError(f"Runtime binary does not exist: {path}") from exc
    if not resolved.is_file():
        raise RuntimeBinaryError(f"Runtime binary is not a file: {resolved}")
    return str(resolved)


def resolve_simplicio_binary(*, explicit: Optional[str] = None, environ: Optional[Mapping[str, str]] = None,
                             candidates: Sequence[str] = ("simplicio", "simplicio-runtime")) -> RuntimeBinary:
    """Resolve one executable deterministically, with explicit provenance."""
    env = os.environ if environ is None else environ
    configured = explicit or env.get("SIMPLICIO_RUNTIME_BIN")
    if configured:
        return RuntimeBinary(_usable(configured), "explicit" if explicit else "environment")
    for name in candidates:
        found = shutil.which(name)
        if found:
            return RuntimeBinary(_usable(found), "path")
    raise RuntimeBinaryError("canonical Simplicio Runtime binary was not found")


def probe_version(binary: RuntimeBinary, *, runner: Callable[..., Any] = subprocess.run) -> RuntimeBinary:
    """Attach an observed version without treating a probe failure as success."""
    try:
        result = runner([binary.path, "--version"], capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeBinaryError("Runtime binary version probe failed") from exc
    if getattr(result, "returncode", 1) != 0 or not str(getattr(result, "stdout", "")).strip():
        raise RuntimeBinaryError("Runtime binary did not report a version")
    return RuntimeBinary(binary.path, binary.source, str(result.stdout).strip().splitlines()[0])


def verify_mcp_capabilities(initialize_result: Mapping[str, Any], *, required_tools: Sequence[str] = ()) -> dict[str, Any]:
    """Validate MCP protocol/capabilities before an effect can be dispatched."""
    if not isinstance(initialize_result, Mapping):
        raise RuntimeBinaryError("MCP initialize result must be an object")
    if initialize_result.get("protocolVersion") != MCP_PROTOCOL:
        raise RuntimeBinaryError("Runtime MCP protocol version mismatch")
    capabilities = initialize_result.get("capabilities")
    if not isinstance(capabilities, Mapping):
        raise RuntimeBinaryError("Runtime MCP initialize omitted capabilities")
    tools = capabilities.get("tools", ())
    if isinstance(tools, Mapping):
        available = set(str(key) for key in tools)
    elif isinstance(tools, (list, tuple, set)):
        available = {str(item.get("name") if isinstance(item, Mapping) else item) for item in tools}
    else:
        available = set()
    missing = sorted(set(required_tools) - available)
    if missing:
        raise RuntimeBinaryError("Runtime MCP missing required tools: " + ", ".join(missing))
    return {"schema": SCHEMA, "status": "READY", "protocol": MCP_PROTOCOL,
            "capabilities": sorted(str(key) for key in capabilities),
            "tools": sorted(available), "effects_authorized": False}


def runtime_preflight(*, binary: RuntimeBinary, initialize_result: Mapping[str, Any], required_tools: Sequence[str] = ()) -> dict[str, Any]:
    receipt = verify_mcp_capabilities(initialize_result, required_tools=required_tools)
    receipt.update({"binary": binary.path, "binary_source": binary.source, "version": binary.version})
    return receipt


__all__ = ["MCP_PROTOCOL", "SCHEMA", "RuntimeBinary", "RuntimeBinaryError", "probe_version", "resolve_simplicio_binary", "runtime_preflight", "verify_mcp_capabilities"]

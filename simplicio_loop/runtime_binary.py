"""Canonical Simplicio Runtime binary and MCP capability preflight (#692)."""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

MCP_PROTOCOL = "2024-11-05"
SCHEMA = "simplicio.runtime-preflight/v1"
EFFECT_TRANSACTION_SCHEMA = "simplicio.effect-transaction/v1"
HBI_COMPATIBILITY = "v1"
HBP_COMPATIBILITY = "v1"
RUNTIME_VERSION_MIN = (1, 0, 0)
RUNTIME_VERSION_MAX_EXCLUSIVE = (5, 0, 0)
REPAIR_COMMAND = "python -m pip install -U simplicio-runtime"
RUNTIME_PRODUCTS = frozenset(("simplicio", "simplicio-runtime"))
_VERSION_RE = re.compile(r"(?<!\d)(\d+)\.(\d+)\.(\d+)(?:[-+.]([0-9A-Za-z.-]+))?")
_IDENTITY_CACHE: dict[tuple[str, str], "RuntimeBinary"] = {}


class RuntimeBinaryError(RuntimeError):
    """No safe canonical Runtime executable or capability set was found."""


@dataclass(frozen=True)
class RuntimeBinary:
    path: str
    source: str
    version: Optional[str] = None
    sha256: Optional[str] = None
    compiled_identity: Optional[str] = None


def repair_instruction() -> str:
    """Return the one repair instruction shared by all preflight failures."""
    return f"Repair: run `{REPAIR_COMMAND}` or set SIMPLICIO_RUNTIME_BIN to an absolute path."


def _error(message: str) -> RuntimeBinaryError:
    return RuntimeBinaryError(f"{message}. {repair_instruction()}")


def _usable(path: str) -> str:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        found = shutil.which(str(candidate))
        if found:
            candidate = Path(found)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise _error(f"Runtime binary does not exist: {path}") from exc
    if not resolved.is_file():
        raise _error(f"Runtime binary is not a file: {resolved}")
    return str(resolved)


def resolve_simplicio_binary(*, explicit: Optional[str] = None, environ: Optional[Mapping[str, str]] = None,
                             candidates: Sequence[str] = ("simplicio", "simplicio-runtime")) -> RuntimeBinary:
    """Resolve one executable deterministically, retaining explicit provenance.

    Version and product identity are deliberately probed separately so callers can
    resolve a path without starting a process and then gate process creation on
    ``probe_version``.
    """
    env = os.environ if environ is None else environ
    configured = explicit or env.get("SIMPLICIO_RUNTIME_BIN")
    if configured:
        return RuntimeBinary(_usable(configured), "explicit" if explicit else "environment")
    for index, name in enumerate(candidates):
        found = shutil.which(name)
        if found:
            source = "path" if index == 0 else "legacy-path"
            return RuntimeBinary(_usable(found), source)
    raise _error("canonical Simplicio Runtime binary was not found")


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise _error(f"Runtime binary could not be hashed: {path}") from exc
    return digest.hexdigest()


def _product_identity(output: str) -> str:
    first_line = output.strip().splitlines()[0] if output.strip() else ""
    match = re.match(r"^([A-Za-z][A-Za-z0-9-]*)\b", first_line)
    product = match.group(1).lower() if match else ""
    if product not in RUNTIME_PRODUCTS:
        raise _error(f"unexpected Runtime product identity: {first_line or '(empty)'}")
    return product


def _version_tuple(value: str) -> Optional[tuple[int, int, int]]:
    match = _VERSION_RE.search(value)
    return tuple(int(part) for part in match.groups()[:3]) if match else None


def _versions_match(left: Optional[str], right: Optional[str]) -> bool:
    left_version = _version_tuple(left or "")
    right_version = _version_tuple(right or "")
    return not left_version or not right_version or left_version == right_version


def probe_version(binary: RuntimeBinary, *, runner: Optional[Callable[..., Any]] = None) -> RuntimeBinary:
    """Attach an observed version and compiled identity without accepting collisions."""
    runner = subprocess.run if runner is None else runner
    try:
        digest = _sha256(binary.path)
    except RuntimeBinaryError:
        if runner is subprocess.run:
            raise
        # Unit-test/in-process runners may model a binary without creating an
        # executable on disk; production probes remain hash-gated.
        digest = None
    cache_key = (str(Path(binary.path).resolve()), digest or "")
    if runner is subprocess.run and digest is not None and cache_key in _IDENTITY_CACHE:
        cached = _IDENTITY_CACHE[cache_key]
        return RuntimeBinary(cached.path, binary.source, cached.version, cached.sha256, cached.compiled_identity)
    try:
        result = runner([binary.path, "--version"], capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise _error("Runtime binary version probe failed") from exc
    stdout = str(getattr(result, "stdout", ""))
    if getattr(result, "returncode", 1) != 0 or not stdout.strip():
        raise _error("Runtime binary did not report a version")
    identity = _product_identity(stdout)
    observed = RuntimeBinary(binary.path, binary.source, stdout.strip().splitlines()[0], digest, identity)
    if runner is subprocess.run and digest is not None:
        _IDENTITY_CACHE[cache_key] = observed
    return observed


def resolve_and_probe_simplicio_binary(*, explicit: Optional[str] = None,
                                       environ: Optional[Mapping[str, str]] = None,
                                       candidates: Sequence[str] = ("simplicio", "simplicio-runtime"),
                                       runner: Optional[Callable[..., Any]] = None) -> RuntimeBinary:
    """Resolve and probe the canonical binary, falling back only for PATH aliases.

    An explicit constructor value or environment override is authoritative and
    therefore never silently replaced by a different executable.
    """
    env = os.environ if environ is None else environ
    runner = subprocess.run if runner is None else runner
    configured = explicit or env.get("SIMPLICIO_RUNTIME_BIN")
    if configured:
        return probe_version(resolve_simplicio_binary(explicit=explicit, environ=env), runner=runner)
    failures: list[str] = []
    for name in candidates:
        found = shutil.which(name)
        if not found:
            continue
        try:
            return probe_version(
                RuntimeBinary(_usable(found), "path" if name == candidates[0] else "legacy-path"),
                runner=runner,
            )
        except RuntimeBinaryError as exc:
            failures.append(str(exc).split(". Repair:", 1)[0])
    if failures:
        raise _error("Runtime candidates were rejected: " + "; ".join(failures))
    raise _error("canonical Simplicio Runtime binary was not found")


def verify_mcp_capabilities(initialize_result: Mapping[str, Any], *, required_tools: Sequence[str] = (),
                            tools_result: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    """Validate MCP protocol/capabilities before an effect can be dispatched."""
    if not isinstance(initialize_result, Mapping):
        raise _error("MCP initialize result must be an object")
    if initialize_result.get("protocolVersion") != MCP_PROTOCOL:
        raise _error("Runtime MCP protocol version mismatch")
    capabilities = initialize_result.get("capabilities")
    if not isinstance(capabilities, Mapping):
        raise _error("Runtime MCP initialize omitted capabilities")
    advertised = (tools_result or {}).get("tools", ()) if isinstance(tools_result, Mapping) else capabilities.get("tools", ())
    if isinstance(advertised, Mapping):
        available = {str(key) for key in advertised}
    elif isinstance(advertised, (list, tuple, set)):
        available = {str(item.get("name") if isinstance(item, Mapping) else item) for item in advertised}
    else:
        available = set()
    missing = sorted(set(required_tools) - available)
    if missing:
        raise _error("Runtime MCP missing required tools: " + ", ".join(missing))
    return {"schema": SCHEMA, "status": "READY", "protocol": MCP_PROTOCOL,
            "capabilities": sorted(str(key) for key in capabilities),
            "tools": sorted(available), "effects_authorized": False}


def _compatibility_manifest(initialize_result: Mapping[str, Any]) -> Mapping[str, Any]:
    """Read the versioned Runtime contract from supported MCP manifest locations."""
    server_info = initialize_result.get("serverInfo")
    experimental = initialize_result.get("capabilities", {}).get("experimental", {})
    candidates = (
        server_info.get("compatibility") if isinstance(server_info, Mapping) else None,
        experimental.get("simplicio") if isinstance(experimental, Mapping) else None,
        initialize_result.get("simplicioCompatibility"),
        server_info,
    )
    for candidate in candidates:
        if isinstance(candidate, Mapping):
            return candidate
    return {}


def _manifest_value(manifest: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in manifest:
            return manifest[name]
    return None


def _verify_server_identity(initialize_result: Mapping[str, Any], binary: RuntimeBinary,
                            *, require_contract: bool = False) -> dict[str, str]:
    server_info = initialize_result.get("serverInfo")
    if not isinstance(server_info, Mapping):
        raise _error("Runtime MCP initialize omitted server identity")
    name = str(server_info.get("name", "")).lower()
    if name not in RUNTIME_PRODUCTS:
        raise _error("Runtime MCP server identity is not Simplicio Runtime")
    server_version = str(server_info.get("version", ""))
    if not _versions_match(binary.version, server_version):
        raise _error("Runtime MCP server version does not match the probed binary")
    parsed_version = _version_tuple(server_version)
    if parsed_version is None:
        raise _error("Runtime MCP server version is malformed")
    if not (RUNTIME_VERSION_MIN <= parsed_version < RUNTIME_VERSION_MAX_EXCLUSIVE):
        raise _error("Runtime MCP server version is outside the supported range")

    manifest = _compatibility_manifest(initialize_result)
    compatibility: dict[str, str] = {
        "runtime_version": "PASS", "hbi": "UNVERIFIED", "hbp": "UNVERIFIED",
    }
    contracts = (
        ("hbi", HBI_COMPATIBILITY, ("hbi", "hbiVersion")),
        ("hbp", HBP_COMPATIBILITY, ("hbp", "hbpVersion")),
        ("effect_transaction_schema", EFFECT_TRANSACTION_SCHEMA,
         ("effectTransactionSchema", "effect_transaction_schema")),
    )
    for label, expected, names in contracts:
        value = _manifest_value(manifest, *names)
        if value is None:
            if require_contract:
                raise _error(f"Runtime MCP compatibility manifest omitted {label}")
            continue
        if str(value) != expected:
            raise _error(f"Runtime MCP {label} compatibility mismatch")
        compatibility[label] = "PASS"
    return compatibility


def runtime_preflight(*, binary: RuntimeBinary, initialize_result: Mapping[str, Any], required_tools: Sequence[str] = (),
                      tools_result: Optional[Mapping[str, Any]] = None,
                      require_server_identity: bool = False,
                      require_compatibility_contract: bool = False) -> dict[str, Any]:
    receipt = verify_mcp_capabilities(initialize_result, required_tools=required_tools, tools_result=tools_result)
    receipt.update({"binary": binary.path, "binary_source": binary.source, "version": binary.version,
                    "compiled_identity": binary.compiled_identity, "sha256": binary.sha256,
                    "effect_transaction_schema": EFFECT_TRANSACTION_SCHEMA})
    if require_server_identity:
        receipt["compatibility"] = _verify_server_identity(
            initialize_result, binary, require_contract=require_compatibility_contract,
        )
    return receipt



def runtime_doctor_report(*, receipt: Optional[Mapping[str, Any]] = None,
                          error: Optional[BaseException] = None) -> dict[str, Any]:
    """Return stable machine-readable Runtime diagnostics and one exact repair command."""
    if error is not None:
        return {
            "schema": "simplicio.runtime-doctor/v1",
            "status": "INCOMPATIBLE",
            "error": str(error),
            "repair_command": REPAIR_COMMAND,
        }
    data = dict(receipt or {})
    compatibility = data.get("compatibility")
    return {
        "schema": "simplicio.runtime-doctor/v1",
        "status": data.get("status", "UNAVAILABLE"),
        "resolved_path": data.get("binary"),
        "version": data.get("version"),
        "sha256": data.get("sha256"),
        "compiled_identity": data.get("compiled_identity"),
        "protocol": data.get("protocol"),
        "tools": list(data.get("tools", ())),
        "compatibility": dict(compatibility) if isinstance(compatibility, Mapping) else {},
        "repair_command": REPAIR_COMMAND,
    }


__all__ = ["EFFECT_TRANSACTION_SCHEMA", "HBI_COMPATIBILITY", "HBP_COMPATIBILITY", "MCP_PROTOCOL",
           "REPAIR_COMMAND", "SCHEMA", "RuntimeBinary", "RuntimeBinaryError",
           "probe_version", "repair_instruction", "resolve_and_probe_simplicio_binary",
           "runtime_doctor_report",
           "resolve_simplicio_binary", "runtime_preflight", "verify_mcp_capabilities"]

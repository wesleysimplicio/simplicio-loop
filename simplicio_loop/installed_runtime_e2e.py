"""Installed-path Loop to Runtime smoke contract (#693).

The probe is dependency-injected so local validation can exercise the full
component chain without paid CI or a network service.  It never calls an
effect when a prerequisite probe is unavailable.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Callable, Mapping

from .installed_process_e2e import InstalledProcessError, run_installed_process_smoke

SCHEMA = "simplicio.installed-runtime-e2e/v1"
COMPONENTS = ("mapper", "dev_cli", "watcher", "hbp", "runtime")


class InstalledE2EError(RuntimeError):
    """The installed component chain is malformed or cannot be verified."""


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def run_installed_smoke(
    repo: str,
    probes: Mapping[str, Callable[[Mapping[str, Any]], Mapping[str, Any]]],
    *,
    mapper_envelope_hash: str = "",
    plan_hash: str = "",
) -> dict[str, Any]:
    """Probe all installed boundaries with one correlation and explicit failure."""
    if not repo or not mapper_envelope_hash or not plan_hash:
        raise InstalledE2EError("repo, mapper envelope hash and plan hash are required")
    correlation_id = uuid.uuid4().hex
    context = {
        "schema": SCHEMA,
        "repo": repo,
        "correlation_id": correlation_id,
        "mapper_envelope_hash": mapper_envelope_hash,
        "plan_hash": plan_hash,
    }
    components: dict[str, Any] = {}
    for name in COMPONENTS:
        probe = probes.get(name)
        if probe is None:
            components[name] = {"status": "UNAVAILABLE", "reason": "probe_missing"}
            break
        try:
            result = dict(probe(context))
        except Exception as exc:
            components[name] = {"status": "UNAVAILABLE", "reason": type(exc).__name__}
            break
        result.setdefault("status", "READY")
        result["correlation_id"] = correlation_id
        components[name] = result
        if result["status"] != "READY":
            break
    ready = all(
        components.get(name, {}).get("status") == "READY" for name in COMPONENTS
    )
    return {
        "schema": SCHEMA,
        "status": "READY" if ready else "BLOCKED",
        "installed": True,
        "effects_attempted": False,
        "correlation_id": correlation_id,
        "mapper_envelope_hash": mapper_envelope_hash,
        "plan_hash": plan_hash,
        "components": components,
        "report_hash": _digest(components),
    }


__all__ = [
    "COMPONENTS",
    "InstalledE2EError",
    "InstalledProcessError",
    "SCHEMA",
    "run_installed_smoke",
    "run_installed_process_smoke",
]

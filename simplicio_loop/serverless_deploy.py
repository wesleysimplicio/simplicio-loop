"""Fail-closed serverless deployment planning (issue #903)."""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

SCHEMA = "simplicio.loop-serverless-deploy/v1"
BACKENDS = ("modal", "daytona")


class DeploymentGateError(RuntimeError):
    """A deployment cannot cross the explicit action gate safely."""


def _hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_plan(repo: str | Path = ".", *, backend: str = "modal",
               environment: str = "", image: str = "") -> dict[str, Any]:
    """Build a read-only provider plan and record adapter availability."""
    backend = str(backend or "").strip().lower()
    if backend not in BACKENDS:
        raise ValueError(f"backend must be one of {BACKENDS}")
    root = Path(repo).resolve()
    adapter = shutil.which(backend)
    payload: dict[str, Any] = {
        "schema": SCHEMA, "mode": "dry_run", "repo": str(root),
        "backend": backend, "environment": str(environment or ""), "image": str(image or ""),
        "adapter": adapter, "adapter_available": bool(adapter),
        "action_gate": "required_for_apply", "effects_attempted": False,
        "status": "PLAN_READY" if adapter else "BLOCKED",
        "reason_code": None if adapter else "serverless_adapter_missing",
        "steps": ["validate_action_gate", f"resolve_{backend}_workspace",
                  "deploy_or_wake_sandbox", "persist_hbp_receipt", "smoke_verify_endpoint"],
    }
    payload["receipt_hash"] = _hash(payload)
    return payload


def execute_plan(plan: dict[str, Any], *, apply: bool = False,
                 action_gate: bool = False) -> dict[str, Any]:
    """Refuse unsafe execution and classify missing provider capability."""
    result = dict(plan)
    if not apply:
        result.update({"mode": "dry_run", "effects_attempted": False})
    elif not action_gate:
        result.update({"status": "BLOCKED", "reason_code": "action_gate_required",
                       "effects_attempted": False})
    elif not plan.get("adapter_available"):
        result.update({"status": "BLOCKED", "reason_code": "serverless_adapter_missing",
                       "effects_attempted": False})
    else:
        raise DeploymentGateError("provider execution adapter is not enabled in this package")
    result["receipt_hash"] = _hash({key: value for key, value in result.items()
                                    if key != "receipt_hash"})
    return result


__all__ = ["BACKENDS", "DeploymentGateError", "SCHEMA", "build_plan", "execute_plan"]

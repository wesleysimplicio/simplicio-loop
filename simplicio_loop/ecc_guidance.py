"""Loop-side admission and provenance for the optional ECC advisory lane.

The Loop does not load ECC bodies or execute ECC hooks. It records the
Mapper-owned provenance and lets the already-integrated Dev CLI consume one
bounded advisory pack during its normal prompt build. This keeps ECC opt-in,
read-only, and outside Simplicio's mutation, receipt, and convergence
authority.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

ECC_SOURCE_REPOSITORY = "https://github.com/affaan-m/ECC"
ECC_DEFAULT_REF = "0c1d7be9a750627fb2a6534c78a998cc46d03f9c"
ECC_MANIFEST_SCHEMA = "simplicio.ecc-manifest/v1"
ECC_DEFAULT_MANIFEST_HASH = "c5a9a1624f07d822f566c7bac07acb47544359ae6e2a2fb69f504f1201813384"
ECC_DOCTOR_SCHEMA = "simplicio.ecc-doctor/v1"
LOOP_ECC_DOCTOR_SCHEMA = "simplicio.loop-ecc-doctor/v1"
ECC_GUIDANCE_REF_SCHEMA = "simplicio.ecc-guidance-ref/v1"
_MAX_COMPONENTS = 8


class EccGuidanceError(ValueError):
    """Raised when required ECC admission cannot be verified."""


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "enabled"}


def ecc_required(env: Mapping[str, str] | None = None) -> bool:
    values = os.environ if env is None else env
    return _truthy(values.get("SIMPLICIO_ECC_REQUIRED"))


def ecc_enabled(env: Mapping[str, str] | None = None) -> bool:
    values = os.environ if env is None else env
    configured = values.get("SIMPLICIO_ECC_ENABLED")
    if configured is not None:
        return _truthy(configured)
    return bool(str(values.get("SIMPLICIO_ECC_ROOT") or "").strip())


def _empty_provenance() -> dict[str, str]:
    return {
        "status": "UNAVAILABLE",
        "expected_ref": ECC_DEFAULT_REF,
        "observed_ref": "",
    }


def _base_receipt(*, enabled: bool, required: bool, status: str) -> dict[str, Any]:
    return {
        "schema": LOOP_ECC_DOCTOR_SCHEMA,
        "status": status,
        "enabled": enabled,
        "required": required,
        "source": {"repository": ECC_SOURCE_REPOSITORY, "ref": ECC_DEFAULT_REF},
        "provenance": _empty_provenance(),
        "manifest_hash": ECC_DEFAULT_MANIFEST_HASH,
        "authority": "simplicio-mapper",
        "execution_policy": "advisory-only",
        "hooks": "disabled",
        "orchestration": "disabled",
        "consumer": "simplicio-dev-cli",
        "body_policy": "loop stores provenance only; Dev CLI consumes the bounded pack",
        "errors": [],
    }


def _persist(run_root: str | Path | None, payload: Mapping[str, Any]) -> None:
    if run_root is None:
        return
    path = Path(run_root) / "ecc-doctor.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(dict(payload), ensure_ascii=False, indent=2) + "\n"
    path.write_text(serialized, encoding="utf-8")


def _provider_errors(payload: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    if payload.get("schema") != ECC_DOCTOR_SCHEMA:
        errors.append("provider_schema_invalid")
    source = payload.get("source")
    if (
        not isinstance(source, Mapping)
        or source.get("repository") != ECC_SOURCE_REPOSITORY
        or source.get("ref") != ECC_DEFAULT_REF
    ):
        errors.append("source_or_ref_mismatch")
    provenance = payload.get("provenance")
    if (
        not isinstance(provenance, Mapping)
        or provenance.get("status") != "VERIFIED"
        or provenance.get("expected_ref") != ECC_DEFAULT_REF
        or provenance.get("observed_ref") != ECC_DEFAULT_REF
    ):
        errors.append("provenance_unverified")
    if payload.get("manifest_hash") != ECC_DEFAULT_MANIFEST_HASH:
        errors.append("manifest_hash_mismatch")
    if payload.get("allow_hooks") is not False or payload.get("hooks_effective") is not False:
        errors.append("hooks_not_disabled")
    return errors


def _mapper_command(env: Mapping[str, str]) -> str:
    requested = str(env.get("SIMPLICIO_MAPPER_BIN") or "simplicio-mapper").strip()
    if Path(requested).is_file():
        return requested
    return shutil.which(requested) or requested


def inspect_ecc(
    repo: str | Path,
    run_root: str | Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """Record ECC admission without loading advisory component bodies.

    Optional failures remain observable but fail open; SIMPLICIO_ECC_REQUIRED=1
    turns them into a durable blocked admission for the Loop.
    """
    values = dict(os.environ if env is None else env)
    required = ecc_required(values)
    enabled = ecc_enabled(values) or required
    if not enabled:
        receipt = _base_receipt(enabled=False, required=False, status="DISABLED")
        _persist(run_root, receipt)
        return receipt

    command = [_mapper_command(values), "ecc", "doctor", "--json"]
    receipt = _base_receipt(enabled=True, required=required, status="UNAVAILABLE")
    receipt["command"] = command
    try:
        process = (runner or subprocess.run)(
            command,
            cwd=str(Path(repo).resolve()),
            env=values,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=max(1, int(timeout)),
            check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        receipt["errors"] = [f"mapper_ecc_doctor_failed: {exc}"]
        receipt["status"] = "BLOCKED" if required else "UNAVAILABLE"
        _persist(run_root, receipt)
        return receipt

    receipt["returncode"] = process.returncode
    raw: dict[str, Any] = {}
    output = str(process.stdout or "").strip()
    if output:
        try:
            parsed = json.loads(output)
            if isinstance(parsed, dict):
                raw = parsed
            else:
                receipt["errors"] = ["provider_payload_not_object"]
        except (TypeError, ValueError):
            receipt["errors"] = ["provider_payload_invalid_json"]
    elif process.returncode != 0:
        receipt["errors"] = ["provider_payload_missing"]

    provider_status = str(raw.get("status") or "")
    provider_errors = [
        str(item) for item in raw.get("errors", [])
        if isinstance(item, str) and item.strip()
    ]
    if provider_status == "READY":
        errors = _provider_errors(raw)
        if not errors:
            receipt["status"] = "READY"
            receipt["provenance"] = dict(raw.get("provenance") or {})
            receipt["manifest_hash"] = str(raw.get("manifest_hash") or "")
        else:
            receipt["status"] = "BLOCKED" if required else "UNAVAILABLE"
            receipt["errors"].extend(errors)
    elif provider_status in {"DISABLED", "UNAVAILABLE", "BLOCKED"}:
        receipt["status"] = (
            "BLOCKED" if required and provider_status != "READY" else provider_status
        )
    else:
        receipt["status"] = "BLOCKED" if required else "UNAVAILABLE"
        receipt["errors"].append("provider_status_invalid")

    receipt["errors"].extend(provider_errors)
    if required and receipt["status"] != "READY":
        receipt["status"] = "BLOCKED"
        if not receipt["errors"]:
            receipt["errors"] = ["required_ecc_guidance_not_ready"]
    _persist(run_root, receipt)
    return receipt


def ensure_ecc_ready(receipt: Mapping[str, Any]) -> None:
    if bool(receipt.get("required")) and receipt.get("status") != "READY":
        detail = "; ".join(str(item) for item in receipt.get("errors") or [])
        raise EccGuidanceError(
            "SIMPLICIO_ECC_REQUIRED is enabled, but ECC admission is not verified"
            + (f": {detail}" if detail else "")
        )


def _validate_reference(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    required = {
        "schema",
        "status",
        "stage",
        "role_id",
        "source",
        "provenance",
        "manifest_hash",
        "pack_hash",
        "authority",
        "execution_policy",
        "components",
    }
    if set(value) != required or value.get("schema") != ECC_GUIDANCE_REF_SCHEMA:
        return None
    if value.get("status") != "READY" or value.get("manifest_hash") != ECC_DEFAULT_MANIFEST_HASH:
        return None
    source = value.get("source")
    if (
        not isinstance(source, Mapping)
        or set(source) != {"repository", "ref"}
        or source.get("repository") != ECC_SOURCE_REPOSITORY
        or source.get("ref") != ECC_DEFAULT_REF
    ):
        return None
    provenance = value.get("provenance")
    if (
        not isinstance(provenance, Mapping)
        or set(provenance) != {"status", "expected_ref", "observed_ref"}
        or provenance.get("status") != "VERIFIED"
        or provenance.get("expected_ref") != ECC_DEFAULT_REF
        or provenance.get("observed_ref") != ECC_DEFAULT_REF
    ):
        return None
    if (
        value.get("authority") != "simplicio-mapper"
        or value.get("execution_policy") != "advisory-only"
    ):
        return None
    if (
        not isinstance(value.get("stage"), str)
        or not value["stage"]
        or not isinstance(value.get("role_id"), str)
        or not value["role_id"]
    ):
        return None
    if not isinstance(value.get("pack_hash"), str) or len(value["pack_hash"]) != 64:
        return None
    components = value.get("components")
    if not isinstance(components, list) or len(components) > _MAX_COMPONENTS:
        return None
    for component in components:
        if not isinstance(component, Mapping) or set(component) != {
            "kind",
            "name",
            "path",
            "sha256",
            "content_sha256",
            "truncated",
        }:
            return None
        if not all(isinstance(component.get(key), str) for key in (
            "kind", "name", "path", "sha256", "content_sha256"
        )):
            return None
        if not isinstance(component.get("truncated"), bool):
            return None
        if ".." in Path(str(component["path"])).parts:
            return None
    return dict(value)


def extract_guidance_reference(value: Any, *, _depth: int = 0) -> dict[str, Any] | None:
    """Find only the Dev CLI hash-only ECC reference; never accept prompt bodies."""
    if _depth > 5 or not isinstance(value, Mapping):
        return None
    if "ecc_guidance_ref" in value:
        return _validate_reference(value.get("ecc_guidance_ref"))
    for key in ("result", "stdout", "payload", "data", "task_result", "output"):
        child = value.get(key)
        found = extract_guidance_reference(child, _depth=_depth + 1)
        if found is not None:
            return found
    return None


def doctor_command(repo: str = ".", *, as_json: bool = False) -> int:
    receipt = inspect_ecc(repo)
    if as_json:
        print(json.dumps(receipt, ensure_ascii=False, indent=2))
    else:
        print("simplicio-loop ECC doctor")
        print(f"  status: {receipt['status']}")
        print(f"  enabled: {receipt['enabled']}")
        print(f"  required: {receipt['required']}")
        print(f"  source: {ECC_SOURCE_REPOSITORY}@{ECC_DEFAULT_REF}")
        if receipt.get("errors"):
            print("  errors: " + "; ".join(str(item) for item in receipt["errors"]))
    return 0 if receipt["status"] != "BLOCKED" else 2


__all__ = [
    "ECC_DEFAULT_MANIFEST_HASH",
    "ECC_DEFAULT_REF",
    "ECC_GUIDANCE_REF_SCHEMA",
    "ECC_SOURCE_REPOSITORY",
    "EccGuidanceError",
    "ecc_enabled",
    "ecc_required",
    "doctor_command",
    "ensure_ecc_ready",
    "extract_guidance_reference",
    "inspect_ecc",
]

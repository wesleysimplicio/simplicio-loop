"""Publish the run-bound ``simplicio.loop-execution/v1`` receipt.

The Loop owns the execution state and publishes one immutable, runtime-readable
projection only after the watcher, delivery, quality-matrix, and oracle gates
have passed.  The projection is a separate bundle so the Runtime never has to
guess which nested run files belong together or follow ``..`` paths.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Mapping

SCHEMA = "simplicio.loop-execution/v1"
CONTRACT_VERSION = "v1"
CHAIN = [
    "simplicio-loop",
    "simplicio-mapper",
    "simplicio-dev-cli",
    "simplicio-runtime",
]

_STATE_FILES = {
    "scratchpad_frontmatter": "scratchpad.md",
    "journal_record": "journal.jsonl",
    "anchor": "anchor.json",
    "watcher_challenge": "watcher_challenge.json",
    "watcher_state": "watcher_state.json",
}


class LoopExecutionReceiptError(RuntimeError):
    """Raised when a verified run cannot produce a complete receipt."""


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as exc:
        raise LoopExecutionReceiptError(f"{label} is unreadable: {exc}") from exc
    if not isinstance(payload, dict):
        raise LoopExecutionReceiptError(f"{label} must contain a JSON object")
    return payload


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_commit(repo: Path) -> str:
    process = subprocess.Popen(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        stdout, _stderr = process.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise LoopExecutionReceiptError("repository commit probe timed out")
    commit = (stdout or "").strip()
    if process.returncode != 0 or not commit:
        raise LoopExecutionReceiptError("repository commit could not be measured")
    return commit


def _component(
    *,
    version: str,
    origin: str,
    receipt: str | None = None,
    fallback: bool = False,
    **extra: Any,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "version": str(version).strip(),
        "origin": str(origin).strip() or "installed",
        "fallback": bool(fallback),
    }
    if not value["version"]:
        raise LoopExecutionReceiptError("component version is missing")
    if receipt:
        value["receipt"] = receipt
    value.update(extra)
    return value


def _contained_path(root: Path, candidate: Path, label: str) -> Path:
    """Resolve *candidate* and reject escapes or symlink indirection."""
    root_resolved = root.resolve()
    candidate_resolved = candidate.resolve(strict=False)
    try:
        candidate_resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise LoopExecutionReceiptError(
            f"{label} escapes its allowed root: {candidate}"
        ) from exc
    if candidate.is_symlink():
        raise LoopExecutionReceiptError(f"{label} must not be a symlink: {candidate}")
    return candidate_resolved


def _validated_run_id(manifest: Mapping[str, Any], run_dir: Path) -> str:
    value = str(manifest.get("run_id") or "").strip()
    if not value or value in {".", ".."} or "/" in value or "\\" in value:
        raise LoopExecutionReceiptError("manifest run_id is missing or unsafe")
    if value != run_dir.name:
        raise LoopExecutionReceiptError(
            f"manifest run_id {value!r} does not match run directory {run_dir.name!r}"
        )
    return value


def _stack_component(stack_lock: Mapping[str, Any], name: str) -> dict[str, Any]:
    for item in stack_lock.get("components", []) or []:
        if isinstance(item, Mapping) and item.get("name") == name:
            return dict(item)
    raise LoopExecutionReceiptError(f"stack lock is missing {name}")


def _copy_entry(source: Path, bundle: Path, name: str, run_dir: Path) -> dict[str, Any]:
    _contained_path(run_dir, source, f"source artifact {name}")
    if not source.is_file():
        raise LoopExecutionReceiptError(f"required receipt is missing: {source}")
    destination = bundle / name
    _contained_path(run_dir, destination, f"destination artifact {name}")
    if destination.is_symlink():
        raise LoopExecutionReceiptError(f"destination artifact must not be a symlink: {destination}")
    shutil.copyfile(source, destination)
    return {
        "path": name,
        "present": True,
        "valid": True,
        "sha256": _sha256(destination),
    }


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def build_receipt(
    *,
    repo: Path,
    run_dir: Path,
    manifest: Mapping[str, Any],
    stack_lock: Mapping[str, Any],
    mapper_preflight: Mapping[str, Any],
    operator_preflight: Mapping[str, Any],
    commit: str,
    artifacts: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Build the canonical envelope after all source files are copied."""
    from . import __version__

    mapper = _stack_component(stack_lock, "simplicio-mapper")
    dev_cli = _stack_component(stack_lock, "simplicio-cli")
    fast = _stack_component(stack_lock, "simplicio-fast")
    runtime = _stack_component(stack_lock, "simplicio-runtime")

    mapper_version = str(mapper_preflight.get("version") or mapper.get("version") or "")
    dev_version = str(operator_preflight.get("version") or dev_cli.get("version") or "")
    fast_version = str(fast.get("version") or "")
    if not fast_version:
        raise LoopExecutionReceiptError("Fast version is missing from the stack lock")
    if fast.get("available") is False:
        raise LoopExecutionReceiptError("Fast is unavailable in the frozen stack lock")
    runtime_version = str(runtime.get("version") or "")
    if not runtime_version or runtime_version.lower() == "installed":
        raise LoopExecutionReceiptError("Runtime version is missing from the stack lock")

    relative_run_dir = run_dir.relative_to(repo).as_posix()
    return {
        "schema": SCHEMA,
        "contract_version": CONTRACT_VERSION,
        "origin": {
            "component": "simplicio-loop",
            "version": str(__version__),
            "commit": commit,
        },
        "run_id": str(manifest.get("run_id") or run_dir.name),
        "workspace": str(repo.resolve()),
        "run_dir": relative_run_dir,
        "chain": list(CHAIN),
        "fallback_used": False,
        "fallback_declared": False,
        "artifacts": dict(artifacts),
        "mapper": _component(
            version=mapper_version,
            origin=str(mapper.get("executable") or "installed"),
            receipt="mapper.json",
            source_receipt="mapper-context.json",
        ),
        "dev_cli": _component(
            version=dev_version,
            origin=str(dev_cli.get("executable") or "installed"),
            receipt="dev-cli.json",
            source_receipt="operator-receipt.json",
        ),
        "fast": _component(
            version=fast_version,
            origin=str(fast.get("executable") or "installed"),
            verified=bool(fast.get("available", True)),
        ),
        "runtime": _component(
            version=runtime_version,
            origin=str(runtime.get("executable") or "installed"),
            build_sha=str(runtime.get("build_sha") or ""),
        ),
        "result": {
            "run_id": str(manifest.get("run_id") or run_dir.name),
            "status": "VERIFIED",
            "verified": True,
        },
        "bundle": {
            "path": relative_run_dir,
            "sha256": hashlib.sha256(
                json.dumps(dict(artifacts), sort_keys=True).encode("utf-8")
            ).hexdigest(),
        },
    }


def publish_loop_execution_receipt(
    *, repo: Path, run_dir: Path, manifest: Mapping[str, Any]
) -> dict[str, Any]:
    """Publish the receipt and return its measured publication metadata.

    Non-git temporary repositories used by legacy unit tests keep their former
    state-machine behavior. Real Loop runs are always git worktrees; there a
    missing source artifact raises and prevents a false ``done`` transition.
    """
    repo = repo.resolve()
    run_dir = _contained_path(repo, run_dir, "run directory")
    _contained_path(run_dir, run_dir / "loop", "loop state directory")
    bundle = _contained_path(run_dir, run_dir / "runtime-loop-execution", "runtime bundle")
    supplied_run_id = _validated_run_id(manifest, run_dir)

    try:
        commit = _git_commit(repo)
    except LoopExecutionReceiptError as exc:
        if not (repo / ".git").exists():
            return {"status": "SKIPPED", "reason": "repository_not_git", "detail": str(exc)}
        raise

    loop_dir = run_dir / "loop"
    bundle.mkdir(parents=True, exist_ok=True)
    source_paths = {
        **{name: loop_dir / source for name, source in _STATE_FILES.items()},
        "mapper": run_dir / "mapper-context.json",
        "dev_cli": run_dir / "operator-receipt.json",
    }
    artifacts: dict[str, dict[str, Any]] = {}
    for name, filename in _STATE_FILES.items():
        artifacts[name] = _copy_entry(source_paths[name], bundle, filename, run_dir)
    _copy_entry(source_paths["mapper"], bundle, "mapper.json", run_dir)
    _copy_entry(source_paths["dev_cli"], bundle, "dev-cli.json", run_dir)

    manifest_payload = _read_json(run_dir / "manifest.json", "manifest")
    file_run_id = _validated_run_id(manifest_payload, run_dir)
    if file_run_id != supplied_run_id:
        raise LoopExecutionReceiptError("provided and persisted manifest run_id values differ")
    stack_lock = _read_json(run_dir / "stack-lock.json", "stack lock")
    mapper_preflight = _read_json(run_dir / "mapper-preflight.json", "Mapper preflight")
    operator_preflight = _read_json(run_dir / "operator-preflight.json", "Dev CLI preflight")
    receipt = build_receipt(
        repo=repo,
        run_dir=bundle,
        manifest=manifest_payload,
        stack_lock=stack_lock,
        mapper_preflight=mapper_preflight,
        operator_preflight=operator_preflight,
        commit=commit,
        artifacts={
            name: {**entry, "path": entry["path"]}
            for name, entry in artifacts.items()
        },
    )
    receipt_path = repo / ".simplicio" / "loop-execution.json"
    _atomic_json(receipt_path, receipt)
    return {
        "status": "VERIFIED",
        "receipt": str(receipt_path),
        "bundle": str(bundle),
        "run_id": receipt["run_id"],
        "commit": commit,
    }


__all__ = [
    "CONTRACT_VERSION",
    "CHAIN",
    "LoopExecutionReceiptError",
    "SCHEMA",
    "build_receipt",
    "publish_loop_execution_receipt",
]

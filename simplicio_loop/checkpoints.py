from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

CHECKPOINT_SCHEMA = "simplicio.loop.checkpoint/v1"
FANIN_SCHEMA = "simplicio.loop.fan-in/v1"
PROMOTION_FENCE_SCHEMA = "simplicio.loop.promotion-fence/v1"
SAFE_STATES = frozenset({"ORIENTED", "PLANNED", "DRY_RUN", "APPLIED", "VERIFY_FOCUSED", "VERIFY_FULL", "READY_TO_PROMOTE", "PROMOTED", "HELD", "CANCELLED", "SEALED"})
TERMINAL_STATES = frozenset({"READY_TO_PROMOTE", "PROMOTED", "HELD", "CANCELLED", "SEALED"})


class CheckpointError(ValueError):
    """Raised when a checkpoint or fan-in receipt cannot be trusted."""


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CheckpointError(f"{name} must be a non-empty string")
    return value.strip()


def _digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _strings(values: Sequence[str] | None, name: str) -> list[str]:
    return sorted({_text(item, name) for item in (values or [])})


def _atomic_write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent), text=True)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            except OSError:
                pass
            finally:
                os.close(directory_fd)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def build_checkpoint(
    *,
    task_id: str,
    attempt_id: str,
    candidate_id: str,
    shard_id: str,
    state: str,
    repo: str,
    source_commit: str,
    fast_generation: str,
    snapshot_sha256: str,
    capabilities: Mapping[str, Any] | None = None,
    handles: Sequence[str] | None = None,
    receipts: Sequence[str] | None = None,
    effect_receipt_digest: str | None = None,
    previous_digest: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    state = _text(state, "state").upper()
    if state not in SAFE_STATES:
        raise CheckpointError(f"checkpoint state is not resumable: {state}")
    if state == "APPLIED" and not effect_receipt_digest:
        raise CheckpointError("APPLIED checkpoint requires effect_receipt_digest")
    result: dict[str, Any] = {
        "schema": CHECKPOINT_SCHEMA,
        "task_id": _text(task_id, "task_id"),
        "attempt_id": _text(attempt_id, "attempt_id"),
        "candidate_id": _text(candidate_id, "candidate_id"),
        "shard_id": _text(shard_id, "shard_id"),
        "state": state,
        "repo": _text(repo, "repo"),
        "source_commit": _text(source_commit, "source_commit"),
        "fast_generation": _text(fast_generation, "fast_generation"),
        "snapshot_sha256": _text(snapshot_sha256, "snapshot_sha256"),
        "capabilities": dict(capabilities or {}),
        "handles": _strings(handles, "handle"),
        "receipts": _strings(receipts, "receipt"),
        "effect_receipt_digest": _text(effect_receipt_digest, "effect_receipt_digest") if effect_receipt_digest else None,
        "previous_digest": _text(previous_digest, "previous_digest") if previous_digest else None,
        "created_at": created_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }
    result["checkpoint_id"] = _digest({key: result[key] for key in ("task_id", "attempt_id", "candidate_id", "shard_id", "state", "previous_digest")})
    result["checkpoint_digest"] = _digest({key: value for key, value in result.items() if key != "checkpoint_digest"})
    return result


def verify_checkpoint(checkpoint: Mapping[str, Any], *, expected_identity: Mapping[str, str] | None = None) -> dict[str, Any]:
    if not isinstance(checkpoint, Mapping) or checkpoint.get("schema") != CHECKPOINT_SCHEMA:
        raise CheckpointError("unsupported checkpoint schema")
    value = dict(checkpoint)
    for key in ("task_id", "attempt_id", "candidate_id", "shard_id", "repo", "source_commit", "fast_generation", "snapshot_sha256", "checkpoint_id", "checkpoint_digest"):
        _text(value.get(key), key)
    state = _text(value.get("state"), "state").upper()
    if state not in SAFE_STATES:
        raise CheckpointError(f"checkpoint state is not resumable: {state}")
    for key in ("handles", "receipts"):
        if not isinstance(value.get(key), list) or value[key] != _strings(value[key], key[:-1]):
            raise CheckpointError(f"{key} must be unique and sorted lists")
    if value.get("effect_receipt_digest") is not None:
        _text(value["effect_receipt_digest"], "effect_receipt_digest")
    if state == "APPLIED" and not value.get("effect_receipt_digest"):
        raise CheckpointError("APPLIED checkpoint requires effect_receipt_digest")
    expected_id = _digest({key: value[key] for key in ("task_id", "attempt_id", "candidate_id", "shard_id", "state") } | {"previous_digest": value.get("previous_digest")})
    if value["checkpoint_id"] != expected_id:
        raise CheckpointError("checkpoint_id mismatch")
    if value["checkpoint_digest"] != _digest({key: item for key, item in value.items() if key != "checkpoint_digest"}):
        raise CheckpointError("checkpoint_digest mismatch")
    for key, expected in (expected_identity or {}).items():
        if value.get(key) != expected:
            raise CheckpointError(f"stale checkpoint identity: {key}")
    return value


def write_checkpoint(path: str | Path, checkpoint: Mapping[str, Any]) -> dict[str, Any]:
    value = verify_checkpoint(checkpoint)
    _atomic_write(Path(path), value)
    return value


def read_checkpoint(path: str | Path, *, expected_identity: Mapping[str, str] | None = None) -> dict[str, Any]:
    target = Path(path)
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CheckpointError("checkpoint missing") from exc
    except json.JSONDecodeError as exc:
        raise CheckpointError("checkpoint JSON is corrupt") from exc
    return verify_checkpoint(value, expected_identity=expected_identity)


def fanin_checkpoints(checkpoints: Sequence[Mapping[str, Any]], *, expected_shard_ids: Sequence[str], expected_identity: Mapping[str, str] | None = None) -> dict[str, Any]:
    expected = _strings(expected_shard_ids, "shard_id")
    if not expected:
        raise CheckpointError("expected_shard_ids must not be empty")
    by_shard: dict[str, dict[str, Any]] = {}
    for raw in checkpoints:
        item = verify_checkpoint(raw, expected_identity=expected_identity)
        shard_id = item["shard_id"]
        if shard_id in by_shard:
            raise CheckpointError(f"duplicate shard: {shard_id}")
        if item["state"] not in TERMINAL_STATES:
            raise CheckpointError(f"shard is not terminal: {shard_id}")
        by_shard[shard_id] = item
    if sorted(by_shard) != expected:
        raise CheckpointError(f"fan-in shard mismatch: missing={sorted(set(expected) - set(by_shard))}, extra={sorted(set(by_shard) - set(expected))}")
    identity_keys = ("task_id", "attempt_id", "candidate_id", "fast_generation", "snapshot_sha256")
    identity = {key: by_shard[expected[0]][key] for key in identity_keys}
    if any(any(item[key] != identity[key] for key in identity_keys) for item in by_shard.values()):
        raise CheckpointError("fan-in identity mismatch")
    result = {"schema": FANIN_SCHEMA, **identity, "shard_ids": expected, "checkpoint_digests": [by_shard[key]["checkpoint_digest"] for key in expected], "status": "READY"}
    result["fan_in_digest"] = _digest(result)
    return result


def promote_winner(candidates: Sequence[Mapping[str, Any]], *, winner_id: str, fence_path: str | Path) -> dict[str, Any]:
    winner = _text(winner_id, "winner_id")
    matches = [dict(item) for item in candidates if _text(item.get("candidate_id"), "candidate_id") == winner]
    if len(matches) != 1:
        raise CheckpointError("winner must be unique and present")
    if matches[0].get("status") not in {"verified", "READY_TO_PROMOTE"}:
        raise CheckpointError("winner is not verified")
    value = {"schema": PROMOTION_FENCE_SCHEMA, "winner_id": winner, "candidate_digest": _text(matches[0].get("candidate_digest"), "candidate_digest"), "status": "SEALED"}
    value["fence_digest"] = _digest(value)
    target = Path(fence_path)
    if target.exists():
        existing = json.loads(target.read_text(encoding="utf-8"))
        if existing == value:
            return existing
        raise CheckpointError("promotion fence already belongs to another winner")
    _atomic_write(target, value)
    return value

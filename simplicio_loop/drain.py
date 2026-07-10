"""Composed queue-drain verification."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Union

try:
    # Reuse the repository's cross-process lock implementation.  Importing the
    # helper by path keeps this module usable from a source checkout as well as
    # from an installed package.
    _SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
    if str(_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS))
    import _locked_append as _locks
except ImportError:  # pragma: no cover - a lock is required for persistence
    _locks = None

try:  # Keep the installed package self-contained when ``scripts/`` is absent.
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - Windows
    _fcntl = None
try:
    import msvcrt as _msvcrt
except ImportError:  # pragma: no cover - POSIX
    _msvcrt = None

SCHEMA = "simplicio.drain-receipt/v1"
ACTIVE_STATES = {"claimed", "running", "verification", "delivery"}


class DrainReceiptError(ValueError):
    """Raised when a persisted drain receipt is invalid or cannot be locked."""


def _canonical(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _ensure_lock_file(lock_path: Path) -> None:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("ab") as handle:
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()


def _acquire_local_posix(handle, timeout_ms: int) -> bool:
    deadline = time.monotonic() + timeout_ms / 1000.0
    while True:
        try:
            _fcntl.flock(handle.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)
            return True
        except OSError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.02)


def _acquire_local_windows(handle, timeout_ms: int) -> bool:  # pragma: no cover - Windows
    deadline = time.monotonic() + timeout_ms / 1000.0
    while True:
        try:
            handle.seek(0)
            _msvcrt.locking(handle.fileno(), _msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.02)


def _release_local_posix(handle) -> None:
    try:
        _fcntl.flock(handle.fileno(), _fcntl.LOCK_UN)
    except Exception:
        pass


def _release_local_windows(handle) -> None:  # pragma: no cover - Windows
    try:
        handle.seek(0)
        _msvcrt.locking(handle.fileno(), _msvcrt.LK_UNLCK, 1)
    except Exception:
        pass


@contextmanager
def _receipt_lock(path: Path):
    """Hold the sidecar lock while checking and replacing a receipt.

    The data file itself is replaced atomically, so readers never observe a
    partial JSON document.  The sidecar lock serializes the check/replace pair
    across processes, which makes repeated verifier calls idempotent.
    """
    lock_path = Path(str(path) + ".lock")
    if _locks is not None:
        _locks._ensure_lock_file(str(lock_path))
    else:
        _ensure_lock_file(lock_path)
    if (_locks is not None and _locks.fcntl is not None) or _fcntl is not None:
        acquire = _locks._acquire_posix if _locks is not None else _acquire_local_posix
        release = _locks._release_posix if _locks is not None else _release_local_posix
        try:
            with lock_path.open("a+b") as handle:
                if not acquire(handle, _locks.DEFAULT_TIMEOUT_MS if _locks is not None else 2000):
                    raise DrainReceiptError("drain receipt lock acquisition timed out")
                try:
                    yield
                finally:
                    release(handle)
        except OSError as exc:
            raise DrainReceiptError("drain receipt lock error: %s" % exc) from exc
    elif (_locks is not None and _locks.msvcrt is not None) or _msvcrt is not None:  # pragma: no cover
        acquire = _locks._acquire_windows if _locks is not None else _acquire_local_windows
        release = _locks._release_windows if _locks is not None else _release_local_windows
        try:
            with lock_path.open("r+b") as handle:
                if not acquire(handle, _locks.DEFAULT_TIMEOUT_MS if _locks is not None else 2000):
                    raise DrainReceiptError("drain receipt lock acquisition timed out")
                try:
                    yield
                finally:
                    release(handle)
        except OSError as exc:
            raise DrainReceiptError("drain receipt lock error: %s" % exc) from exc
    else:  # pragma: no cover
        raise DrainReceiptError("no cross-process locking primitive is available")


def _read_receipt(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DrainReceiptError("persisted drain receipt is invalid JSON") from exc
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise DrainReceiptError("persisted drain receipt has an invalid schema")
    return value


def _atomic_write_receipt(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name = ".%s.%s.tmp" % (path.name, uuid.uuid4().hex)
    temp_path = path.with_name(temp_name)
    try:
        with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(_canonical(payload) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temp_path), str(path))
        # A directory fsync is supported on POSIX and is harmlessly skipped on
        # platforms that do not allow opening directories this way.
        if os.name != "nt":
            try:
                directory_fd = os.open(str(path.parent), os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def _fail(code: str, detail: str, **extra: Any) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "schema": SCHEMA,
        "verdict": "CONTINUE",
        "ready": False,
        "reason_code": code,
        "reason": detail,
        "tag": "UNVERIFIED",
    }
    result.update(extra)
    return result


def _poll_is_empty(poll: Any) -> bool:
    if isinstance(poll, Mapping):
        for key in ("ready", "runnable", "active", "claimed", "running", "blocked", "dead_letter", "delivery"):
            value = poll.get(key, 0)
            if isinstance(value, (list, tuple, set, dict)) and value:
                return False
            if isinstance(value, (int, float)) and value:
                return False
        return True
    return str(poll or "").strip().lower().startswith("empty")


def _same_polls(polls: Sequence[Any], required: int) -> bool:
    if required < 1 or len(polls) < required:
        return False
    tail = list(polls[-required:])
    return all(_poll_is_empty(item) for item in tail) and all(item == tail[0] for item in tail[1:])


def _task_id(task: Mapping[str, Any], index: int) -> str:
    return str(task.get("id") or "T%d" % (index + 1))


def _evidence_ok(evidence: Mapping[str, Any], challenge: str) -> bool:
    if evidence.get("watcher_status") != "MEASURED" or not evidence.get("watcher_match"):
        return False
    if evidence.get("oracle_verdict") not in {"COMPLETE", "DRAINED"}:
        return False
    if evidence.get("fresh") is not True or not evidence.get("checked_at"):
        return False
    if not evidence.get("contract_hash") or not evidence.get("receipt_id"):
        return False
    return not challenge or evidence.get("challenge") == challenge


def evaluate_drain(snapshot: Mapping[str, Any], polls_required: int = 2) -> Dict[str, Any]:
    """Recompute a queue verdict from an immutable scheduler/source snapshot.

    ``snapshot`` contains ``tasks``, ``active_leases`` and chronological source ``polls``.
    A task is complete only when its watcher and oracle evidence is fresh and measured and
    its delivery target is satisfied.
    """
    if not isinstance(snapshot, Mapping):
        return _fail("snapshot_invalid", "drain snapshot is not an object")
    tasks = snapshot.get("tasks")
    if not isinstance(tasks, list):
        return _fail("tasks_missing", "drain snapshot has no task list")
    if not _same_polls(snapshot.get("polls") or [], polls_required):
        return _fail(
            "source_not_quiet",
            "source has not returned the same empty snapshot for the required polls",
            polls_required=polls_required,
        )

    active_leases = snapshot.get("active_leases", 0)
    if not isinstance(active_leases, int) or active_leases < 0:
        return _fail("leases_invalid", "active_leases must be a non-negative integer")
    if active_leases:
        return _fail("leases_active", "active leases remain", active_leases=active_leases)

    pending: List[str] = []
    evidence_pending: List[str] = []
    challenge = str(snapshot.get("challenge") or "")
    for index, raw_task in enumerate(tasks):
        if not isinstance(raw_task, Mapping):
            return _fail("task_invalid", "task record is not an object", task_index=index)
        task_id = _task_id(raw_task, index)
        state = str(raw_task.get("state") or "").strip().lower()
        if state in ACTIVE_STATES or state in {"ready", "blocked", "dead-letter"}:
            pending.append(task_id)
            continue
        if state != "done":
            return _fail("task_state_unknown", "task has no terminal verified state", task_id=task_id, state=state)
        evidence = raw_task.get("evidence")
        if not isinstance(evidence, Mapping):
            evidence_pending.append(task_id)
            continue
        if not _evidence_ok(evidence, challenge):
            evidence_pending.append(task_id)
            continue
        if not bool(raw_task.get("delivery_satisfied", False)):
            evidence_pending.append(task_id)

    if pending:
        return _fail("tasks_pending", "queue still has unresolved tasks", pending_tasks=pending)
    if evidence_pending:
        return _fail("evidence_pending", "done tasks lack fresh measured evidence", evidence_pending=evidence_pending)

    receipt_seed = {"tasks": tasks, "polls": list(snapshot.get("polls") or []), "challenge": challenge}
    # Canonical JSON makes the key independent of mapping insertion order and
    # therefore stable across verifier processes and runtimes.
    receipt_key = hashlib.sha256(_canonical(receipt_seed).encode("utf-8")).hexdigest()
    return {
        "schema": SCHEMA,
        "verdict": "DRAINED",
        "ready": True,
        "reason_code": "drain_verified",
        "reason": "source quiet, no active leases, all tasks verified and delivered",
        "tag": "MEASURED",
        "polls_required": polls_required,
        "polls_observed": len(snapshot.get("polls") or []),
        "task_count": len(tasks),
        "active_leases": 0,
        "receipt_key": receipt_key,
    }


def load_drain_receipt(path: Union[str, os.PathLike]) -> Optional[Dict[str, Any]]:
    """Load a persisted receipt, rejecting torn or foreign-schema files."""
    return _read_receipt(Path(path))


def persist_drain_receipt(
    path: Union[str, os.PathLike],
    snapshot: Optional[Mapping[str, Any]] = None,
    result: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Atomically persist and return one drain verdict.

    ``result`` may be supplied when the caller already evaluated the snapshot;
    otherwise ``evaluate_drain(snapshot)`` is called.  For convenience, a
    previously evaluated result can also be passed as the second argument.
    Equal receipts are returned without rewriting the file.  A changed verdict
    (for example, a late arrival after a prior DRAINED result) replaces the old
    receipt atomically while holding the same sidecar lock.
    """
    if result is None and isinstance(snapshot, Mapping) and snapshot.get("schema") == SCHEMA and "verdict" in snapshot:
        result = snapshot
    elif result is None:
        if not isinstance(snapshot, Mapping):
            raise DrainReceiptError("snapshot is required when result is not supplied")
        result = evaluate_drain(snapshot)
    if not isinstance(result, Mapping) or result.get("schema") != SCHEMA:
        raise DrainReceiptError("drain result has an invalid schema")
    payload = dict(result)
    if payload.get("verdict") not in {"DRAINED", "CONTINUE", "BLOCKED"}:
        raise DrainReceiptError("drain result has an invalid verdict")

    receipt_path = Path(path)
    with _receipt_lock(receipt_path):
        existing = _read_receipt(receipt_path)
        if existing is not None and _canonical(existing) == _canonical(payload):
            return existing
        _atomic_write_receipt(receipt_path, payload)
        return payload


# More explicit spelling for callers that prefer a write-oriented API.  Keep
# both names public so existing integrations can adopt persistence gradually.
write_drain_receipt = persist_drain_receipt


__all__ = [
    "SCHEMA",
    "DrainReceiptError",
    "evaluate_drain",
    "load_drain_receipt",
    "persist_drain_receipt",
    "write_drain_receipt",
]

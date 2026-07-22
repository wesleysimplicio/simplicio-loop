"""Content bindings and invalidation ledger for derived evidence.

Receipts are capabilities: a PASS is useful only for the exact repository and run
identity it measured.  This module is the single implementation of that rule.  It
is deliberately stdlib-only so producers, hooks, and the completion oracle can all
use it without introducing an import cycle.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

SCHEMA = "simplicio.evidence-binding/v1"
INVALIDATION_SCHEMA = "simplicio.evidence-invalidation/v1"
DERIVED_RECEIPTS = ("quality-matrix.json", "watcher_state.json", "delivery-receipt.json", "completion-receipt.json")


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def content_hash(value: Any) -> str:
    """Hash JSON-like policy/config data or exact bytes without ambiguous coercion."""
    payload = value if isinstance(value, bytes) else _canonical(value)
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(("git", "-C", str(repo), *args), text=True,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return completed.stdout.strip() if completed.returncode == 0 else ""


def capture_evidence_binding(repo: str | os.PathLike[str], *, run_id: str, task_id: str,
                             attempt_id: str, policy: Any = None, config: Any = None,
                             toolchain: Any = None, task_contract: Any = None) -> dict[str, Any]:
    """Measure the complete evidence identity, including uncommitted and untracked bytes."""
    root = Path(repo).resolve()
    head = _git(root, "rev-parse", "HEAD")
    tree = _git(root, "rev-parse", "HEAD^{tree}")
    diff = _git(root, "diff", "--binary", "HEAD", "--")
    untracked: list[dict[str, str]] = []
    for rel in sorted(filter(None, _git(root, "ls-files", "--others", "--exclude-standard").splitlines())):
        path = root / rel
        try:
            untracked.append({"path": rel, "hash": content_hash(path.read_bytes())})
        except OSError:
            untracked.append({"path": rel, "hash": "unreadable"})
    body: dict[str, Any] = {
        "schema": SCHEMA, "run_id": str(run_id), "task_id": str(task_id),
        "attempt_id": str(attempt_id), "head_hash": head, "tree_hash": tree,
        "diff_hash": content_hash({"diff": diff, "untracked": untracked}),
        "policy_hash": content_hash(policy), "config_hash": content_hash(config),
        "toolchain_hash": content_hash(toolchain),
        "task_contract_hash": content_hash(task_contract),
    }
    body["binding_hash"] = content_hash(body)
    return body


def bind_receipt(receipt: Mapping[str, Any], binding: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(receipt)
    result["evidence_binding"] = dict(binding)
    result["evidence_status"] = "fresh"
    return result


def validate_receipt_binding(receipt: Mapping[str, Any] | None,
                             current: Mapping[str, Any]) -> dict[str, Any]:
    """Fail closed for legacy, malformed, other-attempt, or content-stale receipts."""
    bound = (receipt or {}).get("evidence_binding")
    if not isinstance(bound, Mapping):
        return {"ok": False, "stale": True, "reason_code": "evidence_binding_missing",
                "changed_fields": ["evidence_binding"], "migration": "reexecute_receipt"}
    if bound.get("schema") != SCHEMA or bound.get("binding_hash") != content_hash(
            {k: v for k, v in bound.items() if k != "binding_hash"}):
        return {"ok": False, "stale": True, "reason_code": "evidence_binding_invalid",
                "changed_fields": ["binding_hash"]}
    fields = tuple(k for k in current if k not in {"schema", "binding_hash"})
    changed = [key for key in fields if bound.get(key) != current.get(key)]
    if changed:
        return {"ok": False, "stale": True,
                "reason_code": "evidence_attempt_mismatch" if "attempt_id" in changed else "evidence_binding_stale",
                "changed_fields": changed}
    return {"ok": True, "stale": False, "reason_code": "evidence_binding_fresh", "changed_fields": []}


def invalidate_derived_evidence(run_dir: str | os.PathLike[str], reason_code: str,
                                changed_fields: Sequence[str], *, binding_before: str = "",
                                binding_after: str = "", dependency_proof: Mapping[str, Any] | None = None,
                                affected_receipts: Sequence[str] | None = None) -> dict[str, Any]:
    """Atomically append an idempotent tombstone; history is never deleted.

    Selective invalidation is accepted only with a persisted dependency proof.
    Otherwise all derived receipt classes are invalidated.
    """
    root = Path(run_dir)
    root.mkdir(parents=True, exist_ok=True)
    receipts = tuple(affected_receipts or DERIVED_RECEIPTS) if dependency_proof else DERIVED_RECEIPTS
    core = {"schema": INVALIDATION_SCHEMA, "reason_code": str(reason_code),
            "changed_fields": sorted(set(map(str, changed_fields))), "receipts": sorted(set(receipts)),
            "binding_before": binding_before, "binding_after": binding_after,
            "dependency_proof": dict(dependency_proof) if dependency_proof else None}
    event_id = content_hash(core)
    ledger = root / "evidence-invalidation.jsonl"
    existing = ledger.read_text(encoding="utf-8").splitlines() if ledger.exists() else []
    for line in existing:
        try:
            event = json.loads(line)
            if event.get("event_id") == event_id:
                return event
        except (ValueError, TypeError):
            continue
    event = dict(core, event_id=event_id, invalidated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    payload = "\n".join(existing + [json.dumps(event, sort_keys=True, separators=(",", ":"))]) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=ledger.name + ".", dir=str(root))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(payload); stream.flush(); os.fsync(stream.fileno())
        os.replace(tmp, ledger)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)
    return event

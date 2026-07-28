"""Offline-verifiable HBP evidence ledger and independent completion oracle."""
from __future__ import annotations

import hashlib
import json
import os
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

SCHEMA = "simplicio.hbp-ledger-receipt/v1"
VERIFY_SCHEMA = "simplicio.hbp-ledger-verification/v1"
ORACLE_SCHEMA = "simplicio.hbp-completion-oracle/v1"
GENESIS_HASH = "0" * 64


class HbpError(RuntimeError):
    reason_code = "HBP_ERROR"


class HbpAppendError(HbpError):
    reason_code = "HBP_APPEND_REJECTED"


def _normalize(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        raise TypeError("floats are not allowed in canonical HBP receipts")
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, Mapping):
        return {
            unicodedata.normalize("NFC", str(key)): _normalize(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    raise TypeError(f"unsupported canonical HBP type: {type(value).__name__}")


def canonical_bytes(value: Any) -> bytes:
    """Canonical UTF-8 JSON: NFC strings, sorted keys, compact separators."""
    return json.dumps(
        _normalize(value), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


@dataclass(frozen=True)
class HbpBinding:
    run_id: str
    plan_hash: str
    generation: str
    attempt: int
    fence: int
    stage: str

    def __post_init__(self) -> None:
        if not all((self.run_id, self.plan_hash, self.generation, self.stage)):
            raise ValueError("run_id, plan_hash, generation and stage are required")
        if self.attempt < 1 or self.fence < 0:
            raise ValueError("attempt must be positive and fence non-negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id, "plan_hash": self.plan_hash,
            "generation": self.generation, "attempt": self.attempt,
            "fence": self.fence, "stage": self.stage,
        }


@dataclass(frozen=True)
class AcceptanceEvidence:
    ac_id: str
    evidence_uri: str
    evidence_hash: str
    verdict: str = "PASS"

    def __post_init__(self) -> None:
        if not self.ac_id or not self.evidence_uri:
            raise ValueError("acceptance evidence requires ac_id and evidence_uri")
        if len(self.evidence_hash) != 64 or any(
            char not in "0123456789abcdef" for char in self.evidence_hash
        ):
            raise ValueError("evidence_hash must be lowercase SHA-256")
        if self.verdict not in {"PASS", "FAIL", "UNVERIFIED"}:
            raise ValueError("unsupported evidence verdict")

    def to_dict(self) -> dict[str, str]:
        return {
            "ac_id": self.ac_id, "evidence_uri": self.evidence_uri,
            "evidence_hash": self.evidence_hash, "verdict": self.verdict,
        }


def build_receipt(*, sequence: int, binding: HbpBinding,
                  previous_receipt_hash: str,
                  evidence: Sequence[AcceptanceEvidence],
                  payload: Mapping[str, Any],
                  observed_at_ns: int | None = None) -> dict[str, Any]:
    if sequence < 1:
        raise ValueError("sequence must be positive")
    if len(previous_receipt_hash) != 64:
        raise ValueError("previous_receipt_hash must be SHA-256")
    payload_hash = canonical_sha256(payload)
    receipt = {
        "schema": SCHEMA, "sequence": sequence,
        "binding": binding.to_dict(),
        "previous_receipt_hash": previous_receipt_hash,
        "acceptance_evidence": [item.to_dict() for item in evidence],
        "payload_hash": payload_hash,
        "payload": dict(payload),
        "observed_at_ns": observed_at_ns if observed_at_ns is not None else time.time_ns(),
        "local_llm": False,
    }
    receipt["receipt_hash"] = canonical_sha256(receipt)
    return receipt


def _without_hash(receipt: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in receipt.items() if key != "receipt_hash"}


def _read_jsonl(path: Path) -> tuple[list[dict[str, Any]], str | None]:
    if not path.exists():
        return [], "LEDGER_MISSING"
    receipts: list[dict[str, Any]] = []
    try:
        raw = path.read_bytes()
    except OSError:
        return [], "LEDGER_UNREADABLE"
    for line_number, line in enumerate(raw.splitlines(), start=1):
        try:
            value = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return receipts, f"INVALID_JSON_LINE_{line_number}"
        if not isinstance(value, dict):
            return receipts, f"INVALID_RECEIPT_LINE_{line_number}"
        receipts.append(value)
    return receipts, None


class HbpLedger:
    """Append-only JSONL ledger; each append re-verifies existing history."""

    def __init__(self, path: str | Path, binding: HbpBinding) -> None:
        self.path = Path(path)
        self.binding = binding

    def append(self, *, evidence: Sequence[AcceptanceEvidence],
               payload: Mapping[str, Any]) -> dict[str, Any]:
        verification = verify_file(self.path, expected=self.binding, allow_missing=True)
        if verification["status"] not in {"VERIFIED", "EMPTY"}:
            raise HbpAppendError(str(verification["reason_code"]))
        receipts, _ = _read_jsonl(self.path)
        previous = receipts[-1]["receipt_hash"] if receipts else GENESIS_HASH
        receipt = build_receipt(
            sequence=len(receipts) + 1, binding=self.binding,
            previous_receipt_hash=previous, evidence=evidence, payload=payload,
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
        descriptor = os.open(str(self.path), flags, 0o600)
        try:
            os.write(descriptor, canonical_bytes(receipt) + b"\n")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return receipt

    def verify(self) -> dict[str, Any]:
        return verify_file(self.path, expected=self.binding)


def _binding_reason(actual: Mapping[str, Any], expected: HbpBinding) -> str | None:
    wanted = expected.to_dict()
    for field in ("run_id", "stage", "fence"):
        if actual.get(field) != wanted[field]:
            return "CROSS_" + field.upper()
    for field in ("plan_hash", "generation", "attempt"):
        if actual.get(field) != wanted[field]:
            return "STALE_" + field.upper()
    return None


def verify_receipts(receipts: Sequence[Mapping[str, Any]], *,
                    expected: HbpBinding) -> dict[str, Any]:
    started = time.perf_counter_ns()
    previous = GENESIS_HASH
    evidence_by_ac: dict[str, list[dict[str, Any]]] = {}
    if not receipts:
        return _verification("EMPTY", "NO_RECEIPTS", 0, evidence_by_ac, started)
    for index, receipt in enumerate(receipts, start=1):
        if receipt.get("schema") != SCHEMA:
            return _verification("LEGACY", "LEGACY_SCHEMA", index - 1,
                                 evidence_by_ac, started)
        required = {
            "sequence", "binding", "previous_receipt_hash", "acceptance_evidence",
            "payload_hash", "payload", "observed_at_ns", "receipt_hash",
        }
        if not required.issubset(receipt):
            return _verification("INVALID", "MISSING_FIELDS", index - 1,
                                 evidence_by_ac, started)
        if receipt.get("sequence") != index:
            return _verification("INVALID", "SEQUENCE_MISMATCH", index - 1,
                                 evidence_by_ac, started)
        if receipt.get("previous_receipt_hash") != previous:
            return _verification("INVALID", "PREVIOUS_HASH_MISMATCH", index - 1,
                                 evidence_by_ac, started)
        binding = receipt.get("binding")
        if not isinstance(binding, Mapping):
            return _verification("INVALID", "BINDING_INVALID", index - 1,
                                 evidence_by_ac, started)
        binding_reason = _binding_reason(binding, expected)
        if binding_reason:
            return _verification("INVALID", binding_reason, index - 1,
                                 evidence_by_ac, started)
        try:
            if canonical_sha256(receipt.get("payload")) != receipt.get("payload_hash"):
                return _verification("INVALID", "PAYLOAD_TAMPERED", index - 1,
                                     evidence_by_ac, started)
            recomputed = canonical_sha256(_without_hash(receipt))
        except (TypeError, ValueError):
            return _verification("INVALID", "CANONICALIZATION_FAILED", index - 1,
                                 evidence_by_ac, started)
        if recomputed != receipt.get("receipt_hash"):
            return _verification("INVALID", "RECEIPT_TAMPERED", index - 1,
                                 evidence_by_ac, started)
        rows = receipt.get("acceptance_evidence")
        if not isinstance(rows, list):
            return _verification("INVALID", "EVIDENCE_INVALID", index - 1,
                                 evidence_by_ac, started)
        for row in rows:
            if not isinstance(row, Mapping) or not row.get("ac_id"):
                return _verification("INVALID", "EVIDENCE_INVALID", index - 1,
                                     evidence_by_ac, started)
            evidence_by_ac.setdefault(str(row["ac_id"]), []).append(dict(row))
        previous = str(receipt["receipt_hash"])
    return _verification("VERIFIED", "OK", len(receipts), evidence_by_ac, started,
                         head_hash=previous)


def _verification(status: str, reason: str, verified: int,
                  evidence: Mapping[str, Any], started_ns: int,
                  *, head_hash: str | None = None) -> dict[str, Any]:
    payload = {
        "schema": VERIFY_SCHEMA, "status": status, "reason_code": reason,
        "verified_receipts": verified, "head_hash": head_hash,
        "acceptance_evidence": dict(evidence),
        "verification_duration_ns": time.perf_counter_ns() - started_ns,
        "offline": True, "local_llm": False,
    }
    payload["verification_hash"] = canonical_sha256(payload)
    return payload


def verify_file(path: str | Path, *, expected: HbpBinding,
                allow_missing: bool = False) -> dict[str, Any]:
    receipts, error = _read_jsonl(Path(path))
    if error:
        if allow_missing and error == "LEDGER_MISSING":
            return _verification("EMPTY", "NO_RECEIPTS", 0, {}, time.perf_counter_ns())
        return _verification("INVALID", error, len(receipts), {},
                             time.perf_counter_ns())
    return verify_receipts(receipts, expected=expected)


def completion_oracle(receipts: Sequence[Mapping[str, Any]], *,
                      expected: HbpBinding,
                      acceptance_criteria: Iterable[str]) -> dict[str, Any]:
    verification = verify_receipts(receipts, expected=expected)
    expected_acs = sorted({str(item) for item in acceptance_criteria if str(item)})
    matrix: list[dict[str, Any]] = []
    evidence_index = verification.get("acceptance_evidence", {})
    for ac_id in expected_acs:
        rows = evidence_index.get(ac_id, []) if isinstance(evidence_index, Mapping) else []
        passing = [
            row for row in rows
            if row.get("verdict") == "PASS"
            and isinstance(row.get("evidence_hash"), str)
            and len(row["evidence_hash"]) == 64
            and row.get("evidence_uri")
        ]
        matrix.append({
            "ac_id": ac_id, "status": "VERIFIED" if passing else "MISSING",
            "evidence_hashes": sorted({row["evidence_hash"] for row in passing}),
            "evidence_uris": sorted({str(row["evidence_uri"]) for row in passing}),
        })
    if verification["status"] == "LEGACY":
        verdict, reason = "PARTIAL", "LEGACY_CHAIN"
    elif verification["status"] != "VERIFIED":
        verdict, reason = "BLOCKED", str(verification["reason_code"])
    elif not expected_acs or any(row["status"] != "VERIFIED" for row in matrix):
        verdict, reason = "PARTIAL", "AC_EVIDENCE_MISSING"
    else:
        verdict, reason = "COMPLETE", "OK"
    payload = {
        "schema": ORACLE_SCHEMA, "verdict": verdict, "reason_code": reason,
        "binding": expected.to_dict(), "acceptance_matrix": matrix,
        "chain": verification, "offline": True, "local_llm": False,
    }
    payload["oracle_hash"] = canonical_sha256(payload)
    return payload


__all__ = [
    "AcceptanceEvidence", "GENESIS_HASH", "HbpAppendError", "HbpBinding",
    "HbpLedger", "SCHEMA", "build_receipt", "canonical_bytes",
    "canonical_sha256", "completion_oracle", "verify_file", "verify_receipts",
]

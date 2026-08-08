#!/usr/bin/env python3
"""Replay Loop journal/recovery fixtures and emit deterministic receipts.

The harness is offline and composes the production journal and recovery modules.  It
never calls a provider, Runtime transport, or network service.  A suite can therefore
reproduce failure handling from committed journal, anchor, receipt, cursor, and lease
fixtures with byte-stable output.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import loop_journal
from simplicio_loop.attempt_journal import AttemptJournal, AttemptJournalError
from simplicio_loop.recovery import (
    RecoveryError,
    build_ac_evidence_receipt,
    build_cursor,
    recover_after_crash,
    validate_ac_evidence_receipt,
)

SUITE_SCHEMA = "simplicio.journal-replay-suite/v1"
RECEIPT_SCHEMA = "simplicio.journal-replay-receipt/v1"
SCENARIO_RECEIPT_SCHEMA = "simplicio.journal-replay-scenario-receipt/v1"
PROMISE_RE = re.compile(r"<promise>\s*(.*?)\s*</promise>", re.IGNORECASE | re.DOTALL)


class ReplayError(ValueError):
    """A fixture is malformed or does not reproduce its declared outcome."""


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def encode_receipt(receipt: Mapping[str, Any]) -> str:
    """Return the canonical byte representation written by the CLI."""
    return _canonical(dict(receipt)) + "\n"


def _require_mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ReplayError(f"{field} must be an object")
    return dict(value)


def _identity(suite: Mapping[str, Any]) -> dict[str, str]:
    raw = _require_mapping(suite.get("identity"), "identity")
    required = ("run_id", "work_item_id", "attempt_id", "actor", "environment_id")
    identity = {field: str(raw.get(field) or "").strip() for field in required}
    missing = [field for field, value in identity.items() if not value]
    if missing:
        raise ReplayError("identity missing: %s" % ", ".join(missing))
    return identity


def _replay_attempt_journal(rows: list[dict[str, Any]], identity: Mapping[str, str]) -> tuple[str, bool]:
    """Import the human journal twice through the real typed AttemptJournal."""
    with tempfile.TemporaryDirectory(prefix="journal-replay-1139-") as raw_tmp:
        root = Path(raw_tmp)
        legacy = root / "legacy.jsonl"
        legacy.write_text("".join(_canonical(row) + "\n" for row in rows), encoding="utf-8")
        journal = AttemptJournal(root / "attempt.jsonl")
        first = journal.import_legacy(
            legacy,
            run_id=identity["run_id"],
            work_item_id=identity["work_item_id"],
            attempt_id=identity["attempt_id"],
            actor=identity["actor"],
        )
        second = journal.import_legacy(
            legacy,
            run_id=identity["run_id"],
            work_item_id=identity["work_item_id"],
            attempt_id=identity["attempt_id"],
            actor=identity["actor"],
        )
        replayed = journal.replay()
    return _sha256(replayed), first == second == replayed


def _recover(scenario: Mapping[str, Any], identity: Mapping[str, str]) -> dict[str, Any]:
    recovery = _require_mapping(scenario.get("recovery"), "recovery")
    cursor_input = _require_mapping(recovery.get("cursor", {}), "recovery.cursor")
    cursor = build_cursor(
        **identity,
        last_sequence=int(cursor_input.get("last_sequence", 0)),
        applied_event_ids=cursor_input.get("applied_event_ids") or [],
        projection_hash=str(cursor_input.get("projection_hash") or ""),
    )
    runtime = _require_mapping(
        recovery.get("runtime", {"status": "MEASURED", "pending": 0}),
        "recovery.runtime",
    )
    return recover_after_crash(
        recovery.get("events") or [],
        cursor,
        source_state=recovery.get("source_state"),
        runtime_reconcile=lambda: dict(runtime),
        lease=recovery.get("lease"),
        provider_identity={
            "actor": identity["actor"],
            "environment_id": identity["environment_id"],
        },
    )


def _anchor_ready(anchor: Mapping[str, Any]) -> tuple[bool, list[str]]:
    criteria = anchor.get("criteria")
    if not isinstance(criteria, list) or not criteria:
        return False, []
    ids: list[str] = []
    for item in criteria:
        if not isinstance(item, Mapping) or not str(item.get("id") or "").strip():
            return False, []
        ids.append(str(item["id"]))
        if str(item.get("status") or "").lower() not in {"verified", "done", "waived:no-infra"}:
            return False, ids
    return True, ids


def _evidence_receipt(
    scenario: Mapping[str, Any], identity: Mapping[str, str], required_criteria: list[str]
) -> tuple[bool, str]:
    source = scenario.get("evidence_receipt")
    if source is None:
        return False, ""
    raw = _require_mapping(source, "evidence_receipt")
    try:
        receipt = build_ac_evidence_receipt(
            **identity,
            observed_at=str(raw.get("observed_at") or ""),
            challenge_id=str(raw.get("challenge_id") or ""),
            criteria=raw.get("criteria") or [],
        )
        validate_ac_evidence_receipt(
            receipt,
            required_criteria=required_criteria,
            expected_identity=identity,
        )
    except RecoveryError:
        return False, ""
    return True, receipt["receipt_hash"]


def _classify(
    *, loop: Mapping[str, Any], stall: Mapping[str, Any], recovery: Mapping[str, Any],
    anchor_ready: bool, evidence_ready: bool
) -> tuple[str, bool, bool]:
    response = str(loop.get("assistant_response") or "")
    match = PROMISE_RE.search(response)
    promise_present = match is not None
    expected_promise = str(loop.get("completion_promise") or "")
    promise_exact = bool(match and expected_promise and match.group(1) == expected_promise)

    if bool(loop.get("stop")):
        return "STOP", promise_present, promise_exact
    if recovery.get("status") == "BLOCKED":
        return "BLOCKED", promise_present, promise_exact
    if recovery.get("status") == "RECLAIM_REQUIRED":
        return "LEASE_RECOVERY_REQUIRED", promise_present, promise_exact
    if promise_exact and anchor_ready and evidence_ready and recovery.get("status") == "COMPLETE":
        return "CONVERGED", promise_present, promise_exact
    iteration = int(loop.get("iteration", 1))
    max_iterations = int(loop.get("max_iterations", 0))
    if max_iterations > 0 and iteration >= max_iterations:
        return "MAX_ITERATIONS", promise_present, promise_exact
    if promise_present:
        return "INVALID_PROMISE", promise_present, promise_exact
    if stall.get("verdict") == "STALLED":
        return "STALLED", promise_present, promise_exact
    if recovery.get("status") == "RESUMED":
        return "CRASH_RECOVERED", promise_present, promise_exact
    return "ACTIVE", promise_present, promise_exact


def replay_scenario(scenario: Mapping[str, Any], identity: Mapping[str, str]) -> dict[str, Any]:
    scenario_id = str(scenario.get("id") or "").strip()
    if not scenario_id:
        raise ReplayError("scenario.id is required")
    journal = scenario.get("journal")
    if not isinstance(journal, list) or not journal or any(not isinstance(row, Mapping) for row in journal):
        raise ReplayError(f"{scenario_id}: journal must be a non-empty list of objects")
    rows = [dict(row) for row in journal]
    try:
        typed_journal_hash, replay_stable = _replay_attempt_journal(rows, identity)
        recovery = _recover(scenario, identity)
    except (AttemptJournalError, RecoveryError, ValueError) as exc:
        raise ReplayError(f"{scenario_id}: {exc}") from exc

    anchor = _require_mapping(scenario.get("anchor"), f"{scenario_id}.anchor")
    anchor_ready, criterion_ids = _anchor_ready(anchor)
    evidence_ready, evidence_receipt_hash = _evidence_receipt(scenario, identity, criterion_ids)
    stall = loop_journal.analyze(rows, int(scenario.get("stall_threshold", 3)))
    loop = _require_mapping(scenario.get("loop"), f"{scenario_id}.loop")
    outcome, promise_present, promise_exact = _classify(
        loop=loop,
        stall=stall,
        recovery=recovery,
        anchor_ready=anchor_ready,
        evidence_ready=evidence_ready,
    )
    receipt = {
        "schema": SCENARIO_RECEIPT_SCHEMA,
        "id": scenario_id,
        "fixture_hash": _sha256(scenario),
        "outcome": outcome,
        "journal_hash": _sha256(rows),
        "typed_journal_hash": typed_journal_hash,
        "replay_stable": replay_stable,
        "stall": {
            "verdict": stall["verdict"],
            "stall_count": stall["stall_count"],
            "fingerprint": stall["fingerprint"],
        },
        "recovery": {
            "status": recovery.get("status"),
            "reason_code": recovery.get("reason_code"),
            "execution_allowed": recovery.get("execution_allowed"),
        },
        "anchor_ready": anchor_ready,
        "evidence_ready": evidence_ready,
        "evidence_receipt_hash": evidence_receipt_hash,
        "promise_present": promise_present,
        "promise_exact": promise_exact,
    }
    receipt["receipt_hash"] = _sha256(receipt)
    return receipt


def replay_suite(suite: Mapping[str, Any], *, check_expected: bool = False) -> dict[str, Any]:
    if not isinstance(suite, Mapping) or suite.get("schema") != SUITE_SCHEMA:
        raise ReplayError("unsupported journal replay suite schema")
    identity = _identity(suite)
    scenarios = suite.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        raise ReplayError("scenarios must be a non-empty list")
    receipts = []
    for raw in scenarios:
        scenario = _require_mapping(raw, "scenario")
        receipt = replay_scenario(scenario, identity)
        expected = str(scenario.get("expected_outcome") or "")
        if check_expected and receipt["outcome"] != expected:
            raise ReplayError(
                "%s: outcome=%s, expected=%s" % (receipt["id"], receipt["outcome"], expected)
            )
        receipts.append(receipt)
    result = {
        "schema": RECEIPT_SCHEMA,
        "suite_hash": _sha256(suite),
        "scenario_count": len(receipts),
        "outcomes": sorted(row["outcome"] for row in receipts),
        "scenarios": receipts,
    }
    result["receipt_hash"] = _sha256(result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Replay deterministic Loop journal/recovery fixtures without network access."
    )
    parser.add_argument("fixture", type=Path, help="simplicio.journal-replay-suite/v1 JSON file")
    parser.add_argument(
        "--check", action="store_true", help="fail when a scenario differs from expected_outcome"
    )
    args = parser.parse_args(argv)
    try:
        suite = json.loads(args.fixture.read_text(encoding="utf-8"))
        receipt = replay_suite(suite, check_expected=args.check)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"journal_replay: {exc}", file=sys.stderr)
        return 1
    sys.stdout.write(encode_receipt(receipt))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

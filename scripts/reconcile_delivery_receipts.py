#!/usr/bin/env python3
"""Reconcile delivery receipts for simplicio-loop intake WIs.

Closes the known gap: cron ticks reported canonical_state=done via GitHub truth
(issues CLOSED + PRs MERGED) but the physical delivery-receipt.json was never
written and the ledger canonical_state was not updated for most WIs.

This script:
  1. Iterates .orchestrator/intake/issue-*/ directories.
  2. Reads intake-contract.json to recover the issue number.
  3. Verifies GitHub truth independently via `gh issue view <n> --json state`.
  4. If MERGED/CLOSED, writes delivery-receipt.json (ready:true) with evidence.
  5. Appends a ledger row marking canonical_state=done with the delivery receipt path.

Idempotent: skips WIs that already have a ready delivery-receipt.json.
No source mutation. Honest: only writes receipts for issues verified closed/merged.
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
LEDGER_DIR = HERE / ".orchestrator" / "intake"
LEDGER_PATH = LEDGER_DIR / "ledger.jsonl"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _gh_issue_state(number: int, timeout: int = 30) -> str | None:
    cmd = ["gh", "issue", "view", str(number), "--json", "state", "--jq", ".state"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        if proc.returncode != 0:
            return None
        return proc.stdout.strip() or None
    except Exception:
        return None


def _recover_issue_number(contract: dict) -> int | None:
    # Canonical placement: source.item_id (string or int).
    src = contract.get("source") or {}
    if isinstance(src, dict):
        item = src.get("item_id")
        if item is not None:
            try:
                return int(item)
            except Exception:
                pass
        url = src.get("url") or ""
        if "/issues/" in url:
            try:
                return int(url.split("/issues/")[1].split("/")[0])
            except Exception:
                return None
    # Fallbacks.
    for key in ("issue", "issue_number", "number", "github_issue"):
        if key in contract and isinstance(contract[key], int):
            return contract[key]
    url = contract.get("url") or contract.get("github_url") or ""
    if "/issues/" in url:
        try:
            return int(url.split("/issues/")[1].split("/")[0])
        except Exception:
            return None
    return None


def main() -> int:
    if not LEDGER_DIR.exists():
        print(f"MEASURED| no intake dir at {LEDGER_DIR}", flush=True)
        return 0

    issue_dirs = sorted([d for d in LEDGER_DIR.glob("issue-*") if d.is_dir()])
    print(f"MEASURED| found {len(issue_dirs)} WI dirs to reconcile", flush=True)

    written = 0
    skipped = 0
    failed = 0
    ledger_rows = []

    for d in issue_dirs:
        contract_path = d / "intake-contract.json"
        receipt_path = d / "delivery-receipt.json"
        if not contract_path.exists():
            skipped += 1
            continue
        try:
            contract = json.loads(contract_path.read_text())
        except Exception:
            failed += 1
            continue

        # Idempotency: skip if receipt already ready.
        if receipt_path.exists():
            try:
                existing = json.loads(receipt_path.read_text())
                if existing.get("ready") is True:
                    skipped += 1
                    continue
            except Exception:
                pass

        number = _recover_issue_number(contract)
        if number is None:
            failed += 1
            print(f"UNVERIFIED| could not recover issue number for {d.name}", flush=True)
            continue

        state = _gh_issue_state(number)
        if state not in ("MERGED", "CLOSED"):
            # Not yet delivered on GitHub — do NOT write a false receipt.
            failed += 1
            print(f"UNVERIFIED| issue {number} GitHub state={state} (not MERGED/CLOSED) -> skip", flush=True)
            continue

        receipt = {
            "schema": "simplicio.delivery-receipt/v1",
            "issue": number,
            "ready": True,
            "delivered_at": _now(),
            "github_state": state,
            "intake_contract_path": str(contract_path),
            "evidence": {
                "github_truth": f"gh issue view {number} -> state={state}",
                "source": "reconcile_delivery_receipts.py",
            },
        }
        receipt_path.write_text(json.dumps(receipt, indent=2, ensure_ascii=False))
        written += 1
        ledger_rows.append({
            "ts": _now(),
            "tick": f"reconcile-delivery-{_now()}",
            "issue": number,
            "canonical_state": "done",
            "delivery_receipt_path": str(receipt_path),
            "github_state": state,
            "note": "delivery receipt written post-hoc from verified GitHub truth (MERGED/CLOSED)",
        })
        print(f"MEASURED| wrote delivery-receipt.json for issue {number} (state={state})", flush=True)

    if ledger_rows:
        with LEDGER_PATH.open("a", encoding="utf-8") as f:
            for row in ledger_rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(f"MEASURED| reconcile complete: written={written} skipped={skipped} failed={failed}", flush=True)
    print(f"MEASURED| ledger appended {len(ledger_rows)} done rows", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

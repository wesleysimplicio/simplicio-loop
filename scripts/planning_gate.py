#!/usr/bin/env python3
"""CLI shell for the #284 planning-receipt / mutation-authority gate.

    python3 scripts/planning_gate.py capture-source --repo owner/name --issue 284 \
        --out .simplicio/loop-runs/<run>/source-snapshot.json
    python3 scripts/planning_gate.py build --run-dir <dir> --task-contract <path> \
        --plan <path> --run-id <id> --attempt <n> [--lease-id L] [--fencing-token F] \
        [--source-snapshot <path from capture-source>]
    python3 scripts/planning_gate.py check --run-dir <dir> --run-id <id> --attempt <n> \
        --task-contract <path> --plan <path> [--lease-id L] [--fencing-token F] \
        [--source-snapshot <path to a FRESH capture-source re-query>]
    python3 scripts/planning_gate.py selftest

`capture-source` at build time freezes the GitHub issue's content hash into the
planning receipt/mutation authority; re-running `capture-source` and passing
its output to `check --source-snapshot` immediately before mutation detects
source drift (the issue/comments changed since planning) and blocks fail-closed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from simplicio_loop.plan_contract import validate_plan
from simplicio_loop.planning_gate import (
    build_planning_receipt,
    content_hash,
    evaluate_mutation_authority,
    receipt_path,
)
from simplicio_loop.source_snapshot import capture_github_issue_snapshot


def _load(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _load_optional(path: str | None) -> dict | None:
    if not path:
        return None
    return _load(path)


def _source_snapshot_hash(snapshot: dict | None) -> str:
    if not snapshot:
        return ""
    return str((snapshot.get("source") or {}).get("snapshot_hash") or "")


def cmd_capture_source(args: argparse.Namespace) -> int:
    """#284 item 1: capture a GitHub issue source snapshot (title/body/labels/
    milestone/assignees/comments -> content hash + revision), fail-closed on any
    `gh` failure. Write it to disk so `build`/`check` can fold its hash into the
    mutation-authority identity tuple (source drift then invalidates authority)."""
    snapshot = capture_github_issue_snapshot(args.repo, args.issue)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(snapshot, ensure_ascii=False, indent=2))
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    contract = _load(args.task_contract)
    plan = _load(args.plan)
    tasks = contract.get("tasks") or []
    plan_validation = validate_plan(
        plan, tasks, args.repo or ".",
        contract_hash=contract.get("collection_hash", ""),
    )
    source_snapshot = _load_optional(args.source_snapshot)
    receipt = build_planning_receipt(
        run_id=args.run_id, attempt=args.attempt, contract=contract, plan=plan,
        plan_validation=plan_validation, lease_id=args.lease_id, fencing_token=args.fencing_token,
        source_snapshot=source_snapshot,
    )
    out = receipt_path(args.run_dir)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0 if receipt["ready_for_mutation"] else 1


def cmd_check(args: argparse.Namespace) -> int:
    contract = _load(args.task_contract)
    plan = _load(args.plan)
    task_contract_hash = str(contract.get("collection_hash") or content_hash(contract))
    plan_hash = content_hash(plan)
    current_source_snapshot = _load_optional(args.source_snapshot)
    verdict = evaluate_mutation_authority(
        args.run_dir, run_id=args.run_id, attempt=args.attempt,
        task_contract_hash=task_contract_hash, plan_hash=plan_hash,
        lease_id=args.lease_id, fencing_token=args.fencing_token,
        source_snapshot_hash=_source_snapshot_hash(current_source_snapshot),
    )
    print(json.dumps(verdict, ensure_ascii=False, indent=2))
    return 0 if verdict["ok"] else 1


def cmd_selftest(_args: argparse.Namespace) -> int:
    import tempfile

    from simplicio_loop.planning_gate import (
        mutation_authority_token,
        verify_mutation_authority,
    )

    with tempfile.TemporaryDirectory() as tmp:
        run_dir = Path(tmp)

        contract = {"schema": "simplicio.task-contract-collection/v1", "collection_hash": "abc123",
                    "tasks": [{"id": "T1", "scenarios": [], "rules": []}]}
        plan = {
            "schema": "simplicio.plan/v1",
            "task_contract_hash": "abc123",
            "mapper_pack_hash": "mp1",
            "context_pack_hash": "mp1",
            "repo_state": {"head": "h1", "tree_hash": "t1"},
            "freshness": {"verified": True, "current_state": {"head": "h1", "tree_hash": "t1"}},
            "steps": [{"candidate_targets": ["a.py"], "to_create": ["a.py"], "steps": []}],
        }
        plan_validation = validate_plan(plan, contract["tasks"], str(run_dir),
                                        contract_hash=contract["collection_hash"],
                                        current_state={"head": "h1", "tree_hash": "t1"})
        assert plan_validation["valid"], plan_validation["errors"]

        receipt = build_planning_receipt(run_id="run-1", attempt=1, contract=contract, plan=plan,
                                         plan_validation=plan_validation, lease_id="lease-1",
                                         fencing_token="7")
        assert receipt["ready_for_mutation"] is True
        assert receipt["mutation_authority"]

        (run_dir / "planning-receipt.json").write_text(json.dumps(receipt), encoding="utf-8")

        task_contract_hash = receipt["task_contract_hash"]
        plan_hash = receipt["plan_hash"]

        ok = evaluate_mutation_authority(run_dir, run_id="run-1", attempt=1,
                                         task_contract_hash=task_contract_hash, plan_hash=plan_hash,
                                         lease_id="lease-1", fencing_token="7")
        assert ok["ok"] is True, ok

        # a stale plan hash (repo/plan changed after planning) invalidates the authority
        stale = evaluate_mutation_authority(run_dir, run_id="run-1", attempt=1,
                                            task_contract_hash=task_contract_hash, plan_hash="different",
                                            lease_id="lease-1", fencing_token="7")
        assert stale["ok"] is False and stale["reason_code"] == "mutation_authority_invalid", stale

        # a rotated lease/fence (lost lease, new attempt owner) invalidates the authority
        rotated = evaluate_mutation_authority(run_dir, run_id="run-1", attempt=1,
                                              task_contract_hash=task_contract_hash, plan_hash=plan_hash,
                                              lease_id="lease-2", fencing_token="8")
        assert rotated["ok"] is False and rotated["reason_code"] == "mutation_authority_invalid", rotated

        # missing receipt fails closed
        missing = evaluate_mutation_authority(Path(tmp) / "nope", run_id="run-1", attempt=1,
                                              task_contract_hash=task_contract_hash, plan_hash=plan_hash)
        assert missing["ok"] is False and missing["reason_code"] == "planning_receipt_missing", missing

        # #284 item 1: a GitHub source snapshot folded into the receipt at build time,
        # then a DIFFERENT snapshot captured just before mutation, must block on drift.
        source_snapshot_a = {"schema": "simplicio.source-snapshot/v1",
                              "source": {"provider": "github", "repo": "acme/repo", "item_id": "284",
                                         "revision": "2026-01-01T00:00:00Z#comments=0",
                                         "snapshot_hash": "hash-a", "observed_at": "2026-01-01T00:00:00Z"}}
        source_snapshot_b = {"schema": "simplicio.source-snapshot/v1",
                              "source": {"provider": "github", "repo": "acme/repo", "item_id": "284",
                                         "revision": "2026-01-02T00:00:00Z#comments=1",
                                         "snapshot_hash": "hash-b", "observed_at": "2026-01-02T00:00:00Z"}}
        receipt_with_source = build_planning_receipt(
            run_id="run-src", attempt=1, contract=contract, plan=plan,
            plan_validation=plan_validation, source_snapshot=source_snapshot_a,
        )
        assert receipt_with_source["ready_for_mutation"] is True
        assert receipt_with_source["source"]["snapshot_hash"] == "hash-a"
        (run_dir / "planning-receipt.json").write_text(json.dumps(receipt_with_source), encoding="utf-8")

        unchanged = evaluate_mutation_authority(
            run_dir, run_id="run-src", attempt=1,
            task_contract_hash=receipt_with_source["task_contract_hash"],
            plan_hash=receipt_with_source["plan_hash"], source_snapshot_hash="hash-a",
        )
        assert unchanged["ok"] is True, unchanged

        drifted = evaluate_mutation_authority(
            run_dir, run_id="run-src", attempt=1,
            task_contract_hash=receipt_with_source["task_contract_hash"],
            plan_hash=receipt_with_source["plan_hash"], source_snapshot_hash="hash-b",
        )
        assert drifted["ok"] is False and drifted["reason_code"] == "source_drift", drifted
        assert source_snapshot_b["source"]["snapshot_hash"] != source_snapshot_a["source"]["snapshot_hash"]

        # restore the no-source receipt for the remaining assertions below
        (run_dir / "planning-receipt.json").write_text(json.dumps(receipt), encoding="utf-8")

        # an unready plan (validation failed) never mints an authority
        bad_plan_validation = dict(plan_validation, valid=False, errors=["task_step_count_mismatch"])
        bad_receipt = build_planning_receipt(run_id="run-1", attempt=1, contract=contract, plan=plan,
                                             plan_validation=bad_plan_validation)
        assert bad_receipt["ready_for_mutation"] is False
        assert bad_receipt["mutation_authority"] == ""

        # token determinism sanity
        t1 = mutation_authority_token(run_id="r", attempt=1, task_contract_hash="c", plan_hash="p")
        t2 = mutation_authority_token(run_id="r", attempt=1, task_contract_hash="c", plan_hash="p")
        assert t1 == t2
        assert verify_mutation_authority(t1, run_id="r", attempt=1, task_contract_hash="c", plan_hash="p")
        assert not verify_mutation_authority(t1, run_id="r", attempt=2, task_contract_hash="c", plan_hash="p")

    print("selftest: PASS planning-gate")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="planning_gate")
    sub = parser.add_subparsers(dest="verb", required=True)

    p_capture = sub.add_parser("capture-source")
    p_capture.add_argument("--repo", required=True, help="owner/name")
    p_capture.add_argument("--issue", required=True)
    p_capture.add_argument("--out", required=True, help="path to write the source-snapshot JSON")
    p_capture.set_defaults(func=cmd_capture_source)

    p_build = sub.add_parser("build")
    p_build.add_argument("--run-dir", required=True)
    p_build.add_argument("--task-contract", required=True)
    p_build.add_argument("--plan", required=True)
    p_build.add_argument("--repo", default=".")
    p_build.add_argument("--run-id", required=True)
    p_build.add_argument("--attempt", type=int, required=True)
    p_build.add_argument("--lease-id", default="")
    p_build.add_argument("--fencing-token", default="")
    p_build.add_argument("--source-snapshot", default="", help="path to a source-snapshot JSON from capture-source")
    p_build.set_defaults(func=cmd_build)

    p_check = sub.add_parser("check")
    p_check.add_argument("--run-dir", required=True)
    p_check.add_argument("--task-contract", required=True)
    p_check.add_argument("--plan", required=True)
    p_check.add_argument("--run-id", required=True)
    p_check.add_argument("--attempt", type=int, required=True)
    p_check.add_argument("--lease-id", default="")
    p_check.add_argument("--fencing-token", default="")
    p_check.add_argument("--source-snapshot", default="", help="path to a FRESH source-snapshot JSON to detect drift")
    p_check.set_defaults(func=cmd_check)

    p_self = sub.add_parser("selftest")
    p_self.set_defaults(func=cmd_selftest)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

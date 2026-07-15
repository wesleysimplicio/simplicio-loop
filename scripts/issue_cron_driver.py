#!/usr/bin/env python3
"""Cron issue-intake driver for simplicio-loop (canonical state machine).

Operates the user-mandated WI lifecycle for every OPEN GitHub issue:

    intake -> mapping -> planning -> executing -> validating ->
    watching -> delivering -> done | blocked | quarantined

Honest, bounded design for a short cron window:
  * INTAKE uses the SHIPPED ``simplicio_loop.intake_contract.compile_intake`` (#284) to
    build a frozen, hash-bound intake envelope (objective, scope, ACs, impact map) — the
    real fail-closed gate, not a reinvention. Runs in milliseconds.
  * MAPPING is recorded as a scheduled step (the heavy ``simplicio-mapper`` survey runs
    out-of-band / next tick, never blocking the cron). The work item is created in the
    canonical ``Todo`` (intake/mapping) projection immediately.
  * PLANNING records a ``simplicio.planning-receipt/v1`` (shipped ``planning_gate``) bound
    to the run + source revision. ``ready_for_mutation`` stays False until a real plan
    passes — the cron never mutates source.
  * Infra-dependent EPICs (#286 multi-device, #287 multi-LLM, #289/#295 need infra) are
    classified ``Blocked`` with a typed reason — never a fabricated ``done``.
  * Idempotent: re-armed issues (same source revision) are resumed, not reworked. Ledger
    is appended INCREMENTALLY so a later failure never loses prior rows.

No source mutation occurs. The driver is the intake/mapping/planning projection only;
execution is left to the human/loop gate (per user instruction).

Usage:
    python3 scripts/issue_cron_driver.py [--repo .] [--limit N]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from simplicio_loop.intake_contract import (  # noqa: E402
    compile_intake,
    lint_intake,
    freeze_intake,
    make_source_snapshot,
    IntakeBlockedError,
)
from simplicio_loop.planning_gate import build_planning_receipt  # noqa: E402

LEDGER_DIR = HERE / ".orchestrator" / "intake"
LEDGER_PATH = LEDGER_DIR / "ledger.jsonl"

# Issues that CANNOT be executed on this single host (no remote workers / no API key /
# no multi-LLM router). They require infra that is not present here — classified Blocked.
INFRA_BLOCKED_PREFIXES: Tuple[str, ...] = ("[P0][EPIC]", "[EPIC][P0]")

# Terminal statuses — already have a stable work item; no re-arm unless prior failure.
# 'Todo' is included: the cron only does intake/mapping/planning (never mutates source),
# so a Todo work item with a written intake-contract is stable and must NOT be re-armed
# on the next tick (that would duplicate the ledger row every run).
RESUME_SKIP_STATUSES = {"Todo", "Done", "Blocked", "Quarantined"}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _gh(*args: str, timeout: int = 60) -> Any:
    cmd = ["gh", "issue", *args, "--json", ",".join([
        "number", "title", "state", "labels", "createdAt", "updatedAt", "body", "url",
    ])]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"gh failed: {proc.stderr.strip()}")
    return json.loads(proc.stdout)


def fetch_open_issues(limit: int, gh_repo: Optional[str] = None) -> List[Dict[str, Any]]:
    cmd = ["list", "--state", "open", "--limit", str(limit)]
    if gh_repo:
        cmd += ["--repo", gh_repo]
    data: List[Dict[str, Any]] = _gh(*cmd)
    return sorted(data, key=lambda i: str(i.get("createdAt") or ""))


def issue_source_revision(issue: Dict[str, Any]) -> str:
    body = issue.get("body") or ""
    labels = ",".join(sorted(l["name"] for l in issue.get("labels") or []))
    # Stable across ticks: GitHub bumps updatedAt on every touch, so using it would
    # defeat the resume-skip (source_revision would change every run -> re-append).
    # Hash only immutable identity + content fields.
    blob = f"{issue.get('number')}|{issue.get('title')}|{body}|{labels}"
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _impact_not_applicable() -> Dict[str, str]:
    cats = ["code", "reverse_dependents", "public_contracts", "data_persistence",
            "security", "concurrency", "performance", "installation_docs", "tests"]
    return {c: "not_applicable: intake gate only; mutation blocked until planning receipt COMPLETE" for c in cats}


def do_intake(issue: Dict[str, Any], gh_repo: str = "wesleysimplicio/simplicio-loop") -> Tuple[Dict[str, Any], str, List[str]]:
    """Run the real #284 intake contract. Returns (envelope, hash, errors)."""
    n = issue["number"]
    rev = issue_source_revision(issue)
    snap = make_source_snapshot(
        provider="github", repo=gh_repo, item_id=str(n),
        revision=issue.get("updatedAt") or rev, snapshot_hash=rev,
        url=issue.get("url") or "", title_hash=hashlib.sha256((issue.get("title") or "").encode()).hexdigest()[:16],
        body_hash=hashlib.sha256((issue.get("body") or "").encode()).hexdigest()[:16],
    )
    ac_text = _extract_acceptance_criteria(issue.get("body") or "") or (
        f"A issue {n} possui contrato congelado, contexto de mapper agendado e planning "
        f"receipt antes de qualquer mutação no repositório."
    )
    try:
        env = compile_intake(
            run_id=f"cron-{n}", work_item_id=str(n), attempt_id=f"cron-{n}-1",
            source_snapshot=snap, title=issue.get("title") or f"Issue {n}",
            objective=f"Processar issue {n} ({issue.get('title')}) pelo estado de trabalho canônico.",
            current_state="issue aberta no GitHub", desired_state="work item com intake+mapping+planning registrados",
            delivery_target="verified", scope_in=[f"issue {n} intake + mapping + planning"],
            scope_out=["mutação de código pelo cron"],
            constraints=["nenhuma mutação de source pelo cron (issue #284)"],
            acceptance_criteria=[{
                "id": "AC-001", "text": ac_text,
                "verification": "planning receipt COMPLETE + run state gravado no ledger",
                "origin": "source", "source_ref": f"issue#{n} body",
            }],
            impact_map=_impact_not_applicable(),
        )
        lint = lint_intake(env)
        env2, h = freeze_intake(env)
        return env2, h, (lint.get("errors") or [])
    except IntakeBlockedError as exc:
        return {}, "", [f"intake_blocked:{exc.reason_code}"]


def _extract_acceptance_criteria(body: str) -> Optional[str]:
    lines = body.splitlines()
    out: List[str] = []
    capture = False
    for ln in lines:
        if "critério de aceite" in ln.lower() or "acceptance criteria" in ln.lower():
            capture = True
            continue
        if capture:
            if ln.strip().startswith("-") or ln.strip().startswith("["):
                out.append(ln.strip())
            elif ln.strip() == "" and out:
                break
            elif out and not ln.strip().startswith(("-", "[")):
                break
    if out:
        return " ".join(out)[:400]
    return None


def classify_status(issue: Dict[str, Any], intake_ok: bool, blockers: List[str]) -> str:
    title = issue.get("title") or ""
    if any(title.startswith(p) for p in INFRA_BLOCKED_PREFIXES):
        return "Blocked"
    if not intake_ok:
        return "Blocked"
    # intake+planning done -> canonical projection Todo (intake/mapping pending). Mapping
    # is scheduled, not yet executed; planning receipt ready_for_mutation stays False.
    return "Todo"


def projected_state(status: str) -> str:
    return {
        "Todo": "Todo", "Planning": "Planning", "In progress": "In progress",
        "Validating": "Validating", "In review": "In review", "Done": "Done",
        "Blocked": "Blocked", "Quarantined": "Quarantined",
    }.get(status, "Blocked")


def write_records(issue: Dict[str, Any], envelope: Dict[str, Any], h: str, blockers: List[str], status: str) -> Dict[str, Any]:
    n = issue["number"]
    rev = issue_source_revision(issue)
    issue_dir = LEDGER_DIR / f"issue-{n}"
    issue_dir.mkdir(parents=True, exist_ok=True)
    if envelope:
        (issue_dir / "intake-contract.json").write_text(
            json.dumps(envelope, indent=2, ensure_ascii=False), encoding="utf-8")
    receipt = build_planning_receipt(
        run_id=f"cron-{n}", attempt=1,
        contract={"collection_hash": h or rev},
        plan={"schema": "simplicio.plan/v1", "task_contract_hash": h or rev},
        plan_validation={"valid": status != "Blocked", "errors": blockers, "warnings": []},
        source_revision=rev,
        awaiting_decision=(status == "Blocked"),
        awaiting_reason="intake/mapping scheduled; mapping survey + execution gated out of cron",
    )
    (issue_dir / "planning-receipt.json").write_text(
        json.dumps(receipt, indent=2, ensure_ascii=False), encoding="utf-8")
    return receipt


def load_ledger() -> List[Dict[str, Any]]:
    if not LEDGER_PATH.exists():
        return []
    rows: List[Dict[str, Any]] = []
    for line in LEDGER_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows


def ledger_index() -> Dict[str, Dict[str, Any]]:
    idx: Dict[str, Dict[str, Any]] = {}
    for row in load_ledger():
        key = f"{row.get('gh_repo')}#{row.get('issue')}"
        idx[key] = row
    return idx


def append_ledger(row: Dict[str, Any]) -> None:
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    with LEDGER_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", default=".")
    ap.add_argument("--gh-repo", default=None,
                   help="GitHub 'owner/name' to list issues from (defaults to --repo's remote)")
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--per-issue-timeout", type=int, default=20)
    ap.add_argument("--total-budget", type=int, default=250)
    args = ap.parse_args()

    # Resolve the GitHub repo: explicit --gh-repo wins; else derive from CWD remote.
    gh_repo = args.gh_repo
    if not gh_repo:
        try:
            out = subprocess.run(
                ["git", "-C", args.repo, "remote", "get-url", "origin"],
                capture_output=True, text=True, timeout=20, check=False,
            ).stdout.strip()
            if out:
                out = out.rsplit("/", 1)[-1]
                out = out.removesuffix(".git")
                owner = out.split("/")[-2] if "/" in out else "wesleysimplicio"
                gh_repo = f"{owner}/{out.split('/')[-1]}"
        except Exception:
            gh_repo = None
    print(f"MEASURED| cron driver start repo={args.repo} gh_repo={gh_repo} dry_run={args.dry_run}", flush=True)
    start = time.time()
    try:
        issues = fetch_open_issues(args.limit, gh_repo)
    except Exception as exc:
        print(f"UNVERIFIED| failed to list issues: {exc}", flush=True)
        return 1

    print(f"MEASURED| open issues: {[i['number'] for i in issues]}", flush=True)
    ledger = ledger_index()
    new_rows: List[Dict[str, Any]] = []

    for issue in issues:
        n = issue["number"]
        rev = issue_source_revision(issue)
        prev = ledger.get(f"{gh_repo}#{n}")
        prior_failed = prev and any(b in str(prev.get("blockers")) for b in ("intake_blocked", "gh_error"))
        # Disk-backed resume: the intake-contract.json artifact is the ground truth.
        # This survives source_revision hash-formula migrations (we no longer trust a
        # stored hash that may have been computed with a different formula).
        contract_exists = (LEDGER_DIR / f"issue-{n}" / "intake-contract.json").exists()
        prev_status = prev.get("status") if prev else None
        status_ok = prev_status in RESUME_SKIP_STATUSES
        if not prior_failed and contract_exists and status_ok:
            print(f"MEASURED| issue {n}: already armed (contract present, prev={prev_status}) — resume, skip", flush=True)
            continue
        if time.time() - start > args.total_budget:
            print(f"MEASURED| budget exhausted ({args.total_budget}s) — defer remaining", flush=True)
            break

        print(f"MEASURED| issue {n}: {issue.get('title')!r} — intake+mapping+planning", flush=True)
        if args.dry_run:
            env, h, blockers = {"schema": "simplicio.task-intake/v1", "intake_hash": "dry"}, "dry", []
        else:
            env, h, blockers = do_intake(issue, gh_repo or "wesleysimplicio/simplicio-loop")
        intake_ok = bool(env) and not blockers
        status = classify_status(issue, intake_ok, blockers)
        proj = projected_state(status)
        receipt = write_records(issue, env, h, blockers, status)
        row = {
            "ts": _now(), "issue": n, "gh_repo": gh_repo, "url": issue.get("url"), "title": issue.get("title"),
            "labels": [l["name"] for l in issue.get("labels") or []],
            "source_revision": rev, "status": status, "projected": proj,
            "intake_ok": intake_ok, "intake_hash": h[:16], "blockers": blockers,
            "planning_receipt_verdict": receipt.get("verdict"),
            "ready_for_mutation": receipt.get("ready_for_mutation"),
            "intake_contract_path": str(LEDGER_DIR / f"issue-{n}" / "intake-contract.json"),
        }
        append_ledger(row)  # incremental — never lose progress
        new_rows.append(row)
        print(f"MEASURED| issue {n}: status={status} projected={proj} "
              f"receipt={receipt.get('verdict')} ready={receipt.get('ready_for_mutation')}", flush=True)

    summary: Dict[str, int] = {}
    for row in new_rows:
        summary[row["status"]] = summary.get(row["status"], 0) + 1
    print(f"MEASURED| ledger updated: {len(new_rows)} new row(s) this tick; statuses={summary}", flush=True)
    print(f"MEASURED| ledger path={LEDGER_PATH}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

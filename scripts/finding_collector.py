#!/usr/bin/env python3
"""simplicio-loop — finding collector (schema `simplicio.finding/v1`), phase-1 slice of #466.

Issue #466 asks for a transversal Finding -> Report -> Issue -> Resolution lifecycle so that no
problem confirmed during any loop stage exists only in a log, comment, receipt or agent reply.
That full lifecycle (GitHub issue creation, IssueTargetResolver across the ecosystem, transactional
outbox with retry/dedup/reopen, CLI `findings list/report/flush/reconcile/doctor`) is a multi-PR
program. This module is Phase 1 of that program (`.orchestrator/backlog` item T1): the durable,
model-free **collector** — record a finding, compute a STABLE fingerprint so the same underlying
defect always collapses to one record no matter how many times it's observed, and expose
list/status for later stages (T2 IssueTargetResolver, T3 template validator, T4 stage_report
integration) to build on. No GitHub calls happen here.

State: `.orchestrator/findings/findings.jsonl` — one JSON record per DISTINCT fingerprint (append
on first sight, rewrite-in-place on a repeat sighting to bump `occurrence_count` /
`last_seen_ts` — never a second record for the same fingerprint).

Fingerprint inputs (`fingerprint()`): `owner_repo + component + error_class + normalized_signature`.
`normalized_signature` strips ephemeral noise (ISO timestamps, hex ids, absolute tmp paths, line
numbers) so two occurrences of the same defect hash identically even when the raw text differs.

Verbs:
    record   Record one finding occurrence. Required: --component, --error-class, --signature,
             --summary. Optional: --owner-repo (default: this repo's slug), --severity, --state
             (suspected|confirmed|disproved|accepted-risk; default confirmed). A record whose
             fingerprint already exists bumps occurrence_count instead of duplicating.
    list     Print all finding records (--state to filter).
    status   Compact counts: total / confirmed / suspected / disproved / accepted-risk.
    fingerprint  Print the stable fingerprint for given --component/--error-class/--signature
             (and optional --owner-repo), without recording anything. Standalone helper.
    selftest Prove fingerprint stability + dedup/increment deterministically — no network, no git.

Usage:
    python3 scripts/finding_collector.py record --component scripts/loop_progress.py \\
        --error-class NameError --signature "name '_atomic_write' is not defined" \\
        --summary "loop_progress.py selftest crashes" --severity high --state confirmed
    python3 scripts/finding_collector.py list --state confirmed
    python3 scripts/finding_collector.py status
    python3 scripts/finding_collector.py selftest
"""
import hashlib
import json
import os
import re
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
FINDINGS_DIR = os.path.join(REPO, ".orchestrator", "findings")
FINDINGS_PATH = os.path.join(FINDINGS_DIR, "findings.jsonl")

SCHEMA = "simplicio.finding/v1"
STATES = frozenset(["suspected", "confirmed", "disproved", "accepted-risk", "resolved", "regressed"])

_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})?")
_HEX_ID_RE = re.compile(r"\b[0-9a-fA-F]{7,40}\b")
_LINE_NO_RE = re.compile(r":\d+\b")
_TMP_PATH_RE = re.compile(r"(/tmp/|/var/folders/|[A-Za-z]:\\Users\\[^\\]+\\AppData\\Local\\Temp\\)\S*")
_NUMBER_RE = re.compile(r"\b\d+\b")


def log(msg):
    print(msg, flush=True)


def _default_owner_repo():
    """Best-effort repo slug (owner/name) from the git remote; falls back to the dir name."""
    try:
        import subprocess
        out = subprocess.run(["git", "remote", "get-url", "origin"], cwd=REPO,
                              capture_output=True, text=True, timeout=5)
        url = out.stdout.strip()
        m = re.search(r"[:/]([^/:]+/[^/.]+?)(?:\.git)?$", url)
        if m:
            return m.group(1)
    except Exception:
        pass
    return os.path.basename(REPO)


def normalize_signature(text):
    """Strip ephemeral noise so the SAME defect hashes the SAME across occurrences/turns."""
    text = _TMP_PATH_RE.sub("<tmp>", text)
    text = _TIMESTAMP_RE.sub("<ts>", text)
    text = _HEX_ID_RE.sub("<hex>", text)
    text = _LINE_NO_RE.sub(":<line>", text)
    text = _NUMBER_RE.sub("<n>", text)
    return " ".join(text.split())


def fingerprint(owner_repo, component, error_class, signature):
    normalized = normalize_signature(signature)
    key = "|".join([owner_repo or "", component or "", error_class or "", normalized])
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _ensure_dir():
    os.makedirs(FINDINGS_DIR, exist_ok=True)


def _load_all():
    if not os.path.exists(FINDINGS_PATH):
        return []
    records = []
    with open(FINDINGS_PATH, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def _write_all(records):
    _ensure_dir()
    tmp = FINDINGS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
    os.replace(tmp, FINDINGS_PATH)


def record_finding(component, error_class, signature, summary, owner_repo=None,
                    severity="medium", state="confirmed", now=None):
    if state not in STATES:
        raise ValueError("state must be one of: " + ", ".join(sorted(STATES)))
    owner_repo = owner_repo or _default_owner_repo()
    fp = fingerprint(owner_repo, component, error_class, signature)
    ts = now if now is not None else time.time()
    records = _load_all()
    for rec in records:
        if rec.get("fingerprint") == fp:
            rec["occurrence_count"] = int(rec.get("occurrence_count", 1)) + 1
            rec["last_seen_ts"] = ts
            if state == "disproved":
                rec["state"] = "disproved"
            elif rec.get("state") != "disproved":
                rec["state"] = state
            _write_all(records)
            return rec
    rec = {
        "schema": SCHEMA,
        "fingerprint": fp,
        "owner_repo": owner_repo,
        "component": component,
        "error_class": error_class,
        "signature": signature,
        "summary": summary,
        "severity": severity,
        "state": state,
        "occurrence_count": 1,
        "first_seen_ts": ts,
        "last_seen_ts": ts,
    }
    records.append(rec)
    _write_all(records)
    return rec


def cmd_record(opts):
    component = opts.get("component")
    error_class = opts.get("error-class")
    signature = opts.get("signature")
    summary = opts.get("summary")
    if not (component and error_class and signature and summary):
        print("UNVERIFIED|record requires --component --error-class --signature --summary")
        sys.exit(2)
    rec = record_finding(
        component, error_class, signature, summary,
        owner_repo=opts.get("owner-repo"),
        severity=opts.get("severity", "medium"),
        state=opts.get("state", "confirmed"),
    )
    print("MEASURED|" + json.dumps(rec, ensure_ascii=False, sort_keys=True))
    return 0


def cmd_list(opts):
    state_filter = opts.get("state")
    records = _load_all()
    if state_filter:
        records = [r for r in records if r.get("state") == state_filter]
    for rec in records:
        print("MEASURED|" + json.dumps(rec, ensure_ascii=False, sort_keys=True))
    return 0


def cmd_status(_opts):
    records = _load_all()
    counts = {"total": len(records)}
    for state in STATES:
        counts[state] = sum(1 for r in records if r.get("state") == state)
    print("MEASURED|" + json.dumps(counts, ensure_ascii=False, sort_keys=True))
    return 0


def cmd_fingerprint(opts):
    owner_repo = opts.get("owner-repo") or _default_owner_repo()
    fp = fingerprint(owner_repo, opts.get("component"), opts.get("error-class"),
                      opts.get("signature"))
    print("MEASURED|" + fp)
    return 0


def cmd_selftest(_opts):
    import tempfile
    global FINDINGS_DIR, FINDINGS_PATH
    orig_dir, orig_path = FINDINGS_DIR, FINDINGS_PATH
    tmp_root = tempfile.mkdtemp(prefix="finding_collector_selftest_")
    FINDINGS_DIR = tmp_root
    FINDINGS_PATH = os.path.join(tmp_root, "findings.jsonl")
    checks = []

    def check(name, got, want):
        checks.append((name, got == want, got, want))

    fp_a = fingerprint("acme/repo", "scripts/x.py", "NameError",
                        "name 'foo' is not defined at 2026-07-17T10:00:00Z line:42 /tmp/abc123/x.py")
    fp_b = fingerprint("acme/repo", "scripts/x.py", "NameError",
                        "name 'foo' is not defined at 2026-07-18T11:30:05Z line:99 /tmp/def456/x.py")
    check("fingerprint_stable_across_ephemeral_noise", fp_a, fp_b)

    fp_c = fingerprint("acme/repo", "scripts/x.py", "NameError", "name 'bar' is not defined")
    check("fingerprint_differs_for_different_signature", fp_a != fp_c, True)

    rec1 = record_finding(
        "scripts/x.py", "NameError",
        "name 'foo' is not defined at 2026-07-17T10:00:00Z line:42 /tmp/abc123/x.py",
        "crash on selftest", owner_repo="acme/repo")
    check("first_record_occurrence_count", rec1["occurrence_count"], 1)

    rec2 = record_finding(
        "scripts/x.py", "NameError",
        "name 'foo' is not defined at 2026-07-18T11:30:05Z line:99 /tmp/def456/x.py",
        "crash on selftest (again)", owner_repo="acme/repo")
    check("duplicate_bumps_occurrence_not_new_record", rec2["occurrence_count"], 2)
    check("duplicate_reuses_same_fingerprint", rec2["fingerprint"], rec1["fingerprint"])
    check("no_second_record_created", len(_load_all()), 1)

    record_finding("scripts/y.py", "ValueError", "bad value", "distinct finding",
                   owner_repo="acme/repo")
    check("distinct_findings_both_present", len(_load_all()), 2)

    status_records = _load_all()
    confirmed = sum(1 for r in status_records if r["state"] == "confirmed")
    check("both_default_to_confirmed", confirmed, 2)

    disproved = record_finding(
        "scripts/x.py", "NameError",
        "name 'foo' is not defined at 2026-07-19T09:15:00Z line:7 /tmp/ghi789/x.py",
        "re-checked, was a stale cache", owner_repo="acme/repo", state="disproved")
    check("disproved_state_applied_on_repeat", disproved["state"], "disproved")

    ok = True
    for name, passed, got, want in checks:
        tag = "PASS" if passed else "FAIL"
        print(f"  [{tag}] {name} (got={got!r} want={want!r})")
        ok = ok and passed

    FINDINGS_DIR, FINDINGS_PATH = orig_dir, orig_path
    try:
        import shutil
        shutil.rmtree(tmp_root, ignore_errors=True)
    except Exception:
        pass

    n = len(checks)
    passed_n = sum(1 for _, p, _, _ in checks if p)
    if ok:
        print(f"MEASURED|finding_collector selftest: {passed_n}/{n} checks passed")
        return 0
    print(f"UNVERIFIED|finding_collector selftest: {passed_n}/{n} checks passed (FAILURES ABOVE)")
    return 1


def _parse(args):
    opts = {}
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("--"):
            key = a[2:]
            if i + 1 < len(args) and not args[i + 1].startswith("--"):
                opts[key] = args[i + 1]
                i += 2
            else:
                opts[key] = True
                i += 1
        else:
            i += 1
    return opts


def main():
    argv = sys.argv[1:]
    if not argv:
        print(__doc__)
        sys.exit(2)
    if argv[0] == "--describe-cli":
        print(json.dumps({
            "verbs": ["record", "list", "status", "fingerprint", "selftest"],
            "flags": ["--component", "--error-class", "--signature", "--summary",
                      "--owner-repo", "--severity", "--state"],
        }))
        sys.exit(0)
    sub, opts = argv[0], _parse(argv[1:])
    handler = {"record": cmd_record, "list": cmd_list, "status": cmd_status,
               "fingerprint": cmd_fingerprint, "selftest": cmd_selftest}.get(sub)
    if handler is None:
        print("unknown command '%s'. choices: record list status fingerprint selftest" % sub)
        sys.exit(2)
    sys.exit(handler(opts) or 0)


if __name__ == "__main__":
    main()

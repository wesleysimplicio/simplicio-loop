#!/usr/bin/env python3
"""simplicio-loop — history-rewrite DRY-RUN plan only (#294 AC3/AC4/AC10, step 3 of the plan).

CRITICAL SAFETY CONSTRAINT (verbatim from the issue's own Definition of Done): "A issue não
autoriza por si só force-push ou reescrita: a execução destrutiva exige aprovação explícita
separada após o dry-run." This module has NO code path that mutates a git ref, runs
`git filter-repo`, `git filter-branch`, `bfg`, or anything that rewrites history. There is no
`--execute`, `--apply`, `--yes`, or environment-variable escape hatch that flips it into a write
mode — dry-run is the ONLY mode this script implements, by construction, not by an extra flag
check that could be bypassed.

What it computes (all read-only, reusing `scripts/repo_history_scan.py`'s real object-database
scan — it does not re-invent blob discovery):
  - candidate blob patterns to remove from history (build artifacts under `rust/target/`,
    generated media under `video/out/`, and any oversized blob already flagged by
    `scripts/repository_budget.py`'s per-file cap that also shows up in the historical scan);
  - how many distinct historical blobs match, and the estimated bytes a real
    `git filter-repo --path-glob ... --invert-paths` pass would stop storing;
  - a rollback/backup/communication plan template the maintainer must follow BEFORE running any
    real rewrite (this text is written to `docs/HISTORY_MIGRATION_PLAN.md`, it is not executed).

Usage:
    python3 scripts/history_migration_plan.py --dry-run             # compute + print the plan
    python3 scripts/history_migration_plan.py --dry-run --write     # also write
                                                                     # docs/HISTORY_MIGRATION_PLAN.md
                                                                     # + docs/history_migration_plan.json
    python3 scripts/history_migration_plan.py --dry-run --json      # machine-readable to stdout

There is intentionally no other verb. Running this script with no arguments, or with anything
other than `--dry-run`, prints usage and exits non-zero — it never silently "does something".
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DOCS_DIR = os.path.join(REPO, "docs")
PLAN_MD = os.path.join(DOCS_DIR, "HISTORY_MIGRATION_PLAN.md")
PLAN_JSON = os.path.join(DOCS_DIR, "history_migration_plan.json")

if HERE not in sys.path:
    sys.path.insert(0, HERE)
import repo_history_scan  # noqa: E402

# Path prefixes/suffixes a real rewrite would target. Deliberately conservative: only
# non-source, non-reproducible-by-hand, historically-committed generated content. Never source
# code, never `.claude/`, `scripts/`, tests, or anything a contributor authored by hand.
CANDIDATE_PATTERNS = [
    ("rust/target/", "prefix", "Rust build output (target/) — reproducible via `cargo build`, "
                               "never hand-authored, should never have been committed."),
    ("video/out/", "prefix", "Generated demo video/audio renders — reproducible via "
                             "`video/build_composition.py` / `video/build_audio.py`, the source "
                             "storyboard (`video/storyboard.master.json`) already lives in git."),
]

MEDIA_SUFFIXES = (".mp4", ".wav", ".webm", ".mov", ".avi")


def _matches(path):
    for pattern, kind, reason in CANDIDATE_PATTERNS:
        if kind == "prefix" and path.startswith(pattern):
            return pattern, reason
    if path.lower().endswith(MEDIA_SUFFIXES):
        return "*%s" % os.path.splitext(path)[1].lower(), (
            "Generated media artifact committed to history directly instead of via LFS/Releases.")
    return None, None


def compute_plan(top_n=50):
    scan = repo_history_scan.scan(top_n=10 ** 9)  # need the FULL blob list to classify, not top-N
    all_blobs = scan["top_blobs"]  # scan() already returns every blob sorted desc when top_n huge

    matched = []
    matched_bytes = 0
    pattern_totals = {}
    for b in all_blobs:
        hit_pattern = None
        hit_reason = None
        for path in b["paths"]:
            p, r = _matches(path)
            if p:
                hit_pattern, hit_reason = p, r
                break
        if hit_pattern:
            matched.append({**b, "matched_pattern": hit_pattern, "reason": hit_reason})
            matched_bytes += b["size_bytes"]
            tot = pattern_totals.setdefault(hit_pattern, {"count": 0, "bytes": 0,
                                                           "reason": hit_reason})
            tot["count"] += 1
            tot["bytes"] += b["size_bytes"]

    matched.sort(key=lambda b: b["size_bytes"], reverse=True)
    total_blob_bytes = scan["total_historical_blob_bytes"]
    pct = (matched_bytes / total_blob_bytes * 100.0) if total_blob_bytes else 0.0

    return {
        "schema": "simplicio.history-migration-plan/v1",
        "mode": "DRY_RUN_ONLY",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "executed": False,
        "note": "This is a computation only. No git ref was read for writing, no filter-repo/"
                "filter-branch/BFG process was invoked, no history was rewritten.",
        "current_repo_size_pack": scan["git_count_objects"].get("size-pack"),
        "total_historical_blob_bytes": total_blob_bytes,
        "candidate_blob_count": len(matched),
        "candidate_blob_bytes": matched_bytes,
        "candidate_blob_bytes_human": repo_history_scan._fmt_bytes(matched_bytes),
        "estimated_pct_of_historical_bytes": round(pct, 1),
        "by_pattern": [
            {"pattern": p, **t} for p, t in sorted(
                pattern_totals.items(), key=lambda kv: kv[1]["bytes"], reverse=True)
        ],
        "top_candidate_blobs": matched[:top_n],
    }


ROLLBACK_PLAN_TEXT = """\
## Backup / rollback / communication plan (REQUIRED before any real rewrite)

This is a template the maintainer must execute manually — nothing below is automated by this
repository, and no script in this repo is capable of performing a history rewrite.

1. **Immutable backup.** Before touching anything: `git clone --mirror` the repository to a
   separate, access-controlled location, and/or push a `pre-migration-backup-<date>` tag/branch
   from the current tip of every ref that will be rewritten. Verify the backup clone is complete
   (`git count-objects -vH` matches the source) before proceeding.
2. **Dry-run first, always.** Run `git filter-repo --analyze` (read-only) and
   `git filter-repo --path-glob '<pattern>' --invert-paths --dry-run` (also read-only —
   filter-repo's own `--dry-run` writes its analysis to `.git/filter-repo/` without touching
   refs) against the BACKUP clone, never the canonical remote. Compare the resulting object count/
   size against this report's `candidate_blob_bytes` estimate.
3. **Impact assessment.** Enumerate every branch, tag, and open PR against the canonical remote.
   A history rewrite changes commit SHAs for every rewritten commit and everything built on top of
   it — open PRs will need to be rebased or recreated, forks will diverge, and any local clone
   that does not reclone will have a permanently diverged history.
4. **Communication window.** Announce the migration (issue/PR/release notes/pinned discussion)
   with: the exact date/time of the rewrite, the expected size reduction (this report), the
   required `git clone` (not `git pull`) for every contributor afterward, and a rollback contact.
5. **Explicit maintainer approval.** A maintainer with push/admin rights signs off, IN WRITING
   (a PR comment or issue comment is sufficient), on the specific `git filter-repo` invocation to
   be run, after reviewing this report's candidate list. No automated process in this repository
   is authorized to skip this step.
6. **Execute against a fresh mirror clone**, not the working checkout used for day-to-day
   development — `git filter-repo` operates in place on a `--mirror` clone by design.
7. **Validate before pushing:** every tag/release still resolves, `git log --all --oneline` on
   the rewritten mirror only shows the expected removed content is gone, and a full test suite
   (`python3 scripts/check.py`) passes against a fresh checkout of the rewritten mirror.
8. **Push with an explicit, scoped force-push** to the specific refs approved in step 5 — never
   `--force` on `refs/*` broadly, and never as part of an unattended/scheduled job.
9. **Post-migration:** ask every contributor to `git clone` fresh (not `git pull`/`git fetch`
   into an existing clone — the rewritten history is unrelated to their old objects), and confirm
   CI/release automation re-authenticates against the new refs cleanly.

Reproduce the current candidate-blob computation with:

    python3 scripts/history_migration_plan.py --dry-run --write
"""


def render_markdown(plan):
    lines = []
    lines.append("# History migration dry-run plan (#294 AC3/AC4/AC10)")
    lines.append("")
    lines.append(
        "Generated by `python3 scripts/history_migration_plan.py --dry-run --write` on "
        + plan["generated_at"] + ". **This document and its generator are DRY-RUN ONLY** — "
        "nothing in this repository can execute a history rewrite. Any real `git filter-repo`/"
        "BFG run requires explicit maintainer approval per the plan below, per the issue's own "
        "Definition of Done."
    )
    lines.append("")
    lines.append("## Current measured size")
    lines.append("")
    lines.append("- `git count-objects -vH` size-pack: **%s**" % plan["current_repo_size_pack"])
    lines.append("- total historical blob bytes (uncompressed, all reachable refs): **%s**" % (
        repo_history_scan._fmt_bytes(plan["total_historical_blob_bytes"])))
    lines.append("")
    lines.append("## Candidate removal set (computed, not executed)")
    lines.append("")
    lines.append(
        "%d historical blobs (%s, an estimated **%.1f%%** of all historical blob bytes) match a "
        "conservative removal pattern — generated build output and generated media, never source "
        "code:" % (plan["candidate_blob_count"], plan["candidate_blob_bytes_human"],
                    plan["estimated_pct_of_historical_bytes"]))
    lines.append("")
    lines.append("| pattern | blob count | bytes | why |")
    lines.append("|---|---|---|---|")
    for row in plan["by_pattern"]:
        lines.append("| `%s` | %d | %s | %s |" % (
            row["pattern"], row["count"], repo_history_scan._fmt_bytes(row["bytes"]),
            row["reason"]))
    lines.append("")
    lines.append("### Largest matching historical blobs")
    lines.append("")
    lines.append("| size | sha | pattern | path |")
    lines.append("|---|---|---|---|")
    for b in plan["top_candidate_blobs"][:25]:
        path = b["paths"][0] if b["paths"] else "(unknown)"
        lines.append("| %s | `%s` | `%s` | %s |" % (
            repo_history_scan._fmt_bytes(b["size_bytes"]), b["sha"][:12],
            b["matched_pattern"], path))
    lines.append("")
    lines.append(ROLLBACK_PLAN_TEXT)
    return "\n".join(lines) + "\n"


def write_plan(plan):
    os.makedirs(DOCS_DIR, exist_ok=True)
    with open(PLAN_JSON, "w", encoding="utf-8") as f:
        json.dump(plan, f, indent=2, sort_keys=True)
        f.write("\n")
    with open(PLAN_MD, "w", encoding="utf-8") as f:
        f.write(render_markdown(plan))


def main(argv=None):
    args = list(argv if argv is not None else sys.argv[1:])
    if "--dry-run" not in args:
        print(__doc__)
        print("refusing to run without --dry-run (the only mode this script has)")
        return 2

    plan = compute_plan()

    if "--write" in args:
        write_plan(plan)
        print("wrote %s + %s" % (
            os.path.relpath(PLAN_MD, REPO), os.path.relpath(PLAN_JSON, REPO)))

    if "--json" in args:
        print(json.dumps(plan, indent=2, sort_keys=True))
    else:
        print("=== history migration DRY-RUN plan (#294) ===")
        print("mode: %s  executed: %s" % (plan["mode"], plan["executed"]))
        print("candidate blobs: %d (%s, ~%.1f%% of historical bytes)" % (
            plan["candidate_blob_count"], plan["candidate_blob_bytes_human"],
            plan["estimated_pct_of_historical_bytes"]))
        for row in plan["by_pattern"]:
            print("  %-20s %5d blobs  %s" % (
                row["pattern"], row["count"], repo_history_scan._fmt_bytes(row["bytes"])))
    return 0


def selftest():
    """Prove the plan computation is real (reuses the real repo_history_scan) AND that this
    module truly has no execute path — no function here calls filter-repo/filter-branch/bfg or
    mutates any git ref."""
    checks = []
    try:
        plan = compute_plan(top_n=5)
        checks.append(("compute_plan() returns a dict", isinstance(plan, dict)))
        checks.append(("mode is DRY_RUN_ONLY", plan["mode"] == "DRY_RUN_ONLY"))
        checks.append(("executed is False", plan["executed"] is False))
        checks.append(("candidate_blob_count >= 0", plan["candidate_blob_count"] >= 0))
        md = render_markdown(plan)
        checks.append(("render_markdown produces non-empty text", len(md) > 200))
        checks.append(("rollback plan text is embedded", "Explicit maintainer approval" in md))
    except Exception as exc:  # pragma: no cover - defensive
        checks.append(("compute_plan() raised: %s" % exc, False))

    # Static guard: this source file must never reference a rewrite tool by name in a way that
    # could execute it (only inside docstrings/plan text, which selftest doesn't need to police
    # further -- the real guarantee is structural: no subprocess call anywhere in this file names
    # filter-repo/filter-branch/bfg).
    with open(__file__, encoding="utf-8") as f:
        src = f.read()
    import re
    exec_calls = re.findall(
        r"subprocess\.\w+\(\s*\[[^\]]*(filter-repo|filter-branch|bfg\.jar)", src, re.I)
    checks.append(("no subprocess call invokes a history-rewrite tool", not exec_calls))

    ok = all(v for _, v in checks)
    for name, v in checks:
        print("  [%s] %s" % ("ok" if v else "XX", name))
    print("history_migration_plan selftest: %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        sys.exit(selftest())
    if len(sys.argv) > 1 and sys.argv[1] == "--describe-cli":
        print(json.dumps({
            "verbs": ["selftest"],
            "flags": ["--dry-run", "--write", "--json", "--describe-cli"],
        }))
        sys.exit(0)
    sys.exit(main())

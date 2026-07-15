#!/usr/bin/env python3
"""simplicio-loop — historical blob inventory (#294 AC1, step 1 "measure and classify").

`scripts/repository_budget.py` (#323) already measures the CURRENT tracked working tree. It
deliberately never looks at git history. #294's own audit is precisely about the GAP between
those two numbers: GitHub reports ~1.47 GB for this repository while the tracked tree is only
~18 MB — the difference is blobs that were committed at some point in history and never removed,
even though a later commit deleted or replaced them. This script is the "measure history" half
the issue calls for, and it is READ-ONLY: it walks `git rev-list --objects --all` +
`git cat-file --batch-check` (no `git filter-repo`, no rewrite, no ref mutation of any kind) and
reports the largest blobs ever committed to any reachable ref, classified by extension, together
with the packed/loose object totals `git count-objects -vH` reports for the whole repository.

The issue's Definition of Done is explicit that a history rewrite needs a SEPARATE, later,
explicitly-approved step (backup, dry-run, communicated window) — this script produces the
"measure and classify" report the plan says must be published BEFORE any of that; it has no
write path to git objects/refs at all.

Usage:
    python3 scripts/repo_history_scan.py                  # scan + print top blobs report
    python3 scripts/repo_history_scan.py --top 30          # N largest historical blobs (default 30)
    python3 scripts/repo_history_scan.py --write-report    # also (re)write
                                                            # docs/REPO_SIZE_REPORT.md +
                                                            # docs/repo_size_report.json
    python3 scripts/repo_history_scan.py --json            # machine-readable report to stdout

Exit codes: 0 on a successful scan (this is a reporting tool, not a gate — it never fails a
build; `scripts/repository_budget.py` is the gate for NEW growth).
"""
import json
import os
import subprocess
import sys
import time
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DOCS_DIR = os.path.join(REPO, "docs")
REPORT_MD = os.path.join(DOCS_DIR, "REPO_SIZE_REPORT.md")
REPORT_JSON = os.path.join(DOCS_DIR, "repo_size_report.json")

DEFAULT_TOP_N = 30


def _run_git(args, **kw):
    r = subprocess.run(["git"] + args, cwd=REPO, capture_output=True, text=True, **kw)
    if r.returncode != 0:
        raise RuntimeError("git %s failed: %s" % (" ".join(args), r.stderr.strip()))
    return r.stdout


def _fmt_bytes(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024.0:
            return "%.1f%s" % (n, unit)
        n /= 1024.0
    return "%.1fTB" % n


def count_objects():
    """Parse `git count-objects -vH` into a dict — the REAL packed+loose totals for the whole
    object database (what GitHub's reported clone size is derived from), not an estimate."""
    out = _run_git(["count-objects", "-v", "-H"])
    data = {}
    for line in out.splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        data[k.strip()] = v.strip()
    return data


def _parse_size(s):
    """`git count-objects -H` prints sizes like '1.40 GiB' — parse back to bytes for the JSON
    report (kept alongside the human string, never replacing it)."""
    try:
        num_s, unit = s.split()
        num = float(num_s)
    except (ValueError, AttributeError):
        return None
    mult = {"B": 1, "KiB": 1024, "MiB": 1024 ** 2, "GiB": 1024 ** 3, "TiB": 1024 ** 4}
    return int(num * mult.get(unit, 1))


def iter_all_blob_paths():
    """Yield (sha, path) for every (blob, path) pair ever reachable from any local ref.

    `git rev-list --objects --all` walks every commit/tree reachable from every ref (branches,
    tags — NOT just HEAD's history), emitting `<sha> <path>` for blobs and trees. This is how the
    real historical footprint is measured — a blob deleted from HEAD long ago but still reachable
    from an old tag/branch still counts against clone size, and this walk still finds it.
    """
    out = _run_git(["rev-list", "--objects", "--all"])
    for line in out.splitlines():
        line = line.rstrip("\n")
        if not line:
            continue
        parts = line.split(" ", 1)
        sha = parts[0]
        path = parts[1] if len(parts) > 1 else ""
        if path:  # blank path = a commit or root tree object, not a blob-with-a-name
            yield sha, path


def batch_check_sizes(shas):
    """Resolve a list of object SHAs to (sha, type, size) via a single `git cat-file
    --batch-check` streaming call — this is the O(1)-subprocess way to size tens of thousands of
    objects; per-object subprocess calls would be far too slow for a repo this size."""
    if not shas:
        return {}
    proc = subprocess.Popen(
        ["git", "cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)"],
        cwd=REPO, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    stdin_payload = "\n".join(shas) + "\n"
    stdout, _ = proc.communicate(stdin_payload)
    result = {}
    for line in stdout.splitlines():
        bits = line.split()
        if len(bits) != 3:
            continue
        sha, otype, size_s = bits
        try:
            size = int(size_s)
        except ValueError:
            continue
        result[sha] = (otype, size)
    return result


def scan(top_n=DEFAULT_TOP_N):
    # Map each blob sha -> the set of paths it was ever committed under (a blob can be renamed;
    # we report every historical path it carried, so the reader can trace "what file was this").
    blob_paths = defaultdict(set)
    for sha, path in iter_all_blob_paths():
        blob_paths[sha].add(path)

    sized = batch_check_sizes(list(blob_paths.keys()))
    blobs = []
    total_blob_bytes = 0
    ext_totals = defaultdict(lambda: {"count": 0, "bytes": 0})
    for sha, (otype, size) in sized.items():
        if otype != "blob":
            continue
        total_blob_bytes += size
        paths = sorted(blob_paths.get(sha, ()))
        primary_path = paths[0] if paths else "(unknown)"
        ext = os.path.splitext(primary_path)[1].lower() or "(no ext)"
        ext_totals[ext]["count"] += 1
        ext_totals[ext]["bytes"] += size
        blobs.append({"sha": sha, "size_bytes": size, "paths": paths})

    blobs.sort(key=lambda b: b["size_bytes"], reverse=True)
    top_blobs = blobs[:top_n]

    ext_report = sorted(
        ({"ext": ext, **v} for ext, v in ext_totals.items()),
        key=lambda e: e["bytes"], reverse=True,
    )

    counts = count_objects()
    size_pack_bytes = _parse_size(counts.get("size-pack", ""))
    size_loose_bytes = _parse_size(counts.get("size", ""))

    return {
        "schema": "simplicio.repo-history-scan/v1",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "distinct_blobs_ever_committed": len(blobs),
        "total_historical_blob_bytes": total_blob_bytes,
        "total_historical_blob_human": _fmt_bytes(total_blob_bytes),
        "git_count_objects": counts,
        "git_count_objects_parsed_bytes": {
            "size_pack_bytes": size_pack_bytes,
            "size_loose_bytes": size_loose_bytes,
        },
        "top_blobs": top_blobs,
        "by_extension": ext_report,
    }


def render_markdown(report):
    lines = []
    lines.append("# Repository size report (#294)")
    lines.append("")
    lines.append(
        "Generated by `python3 scripts/repo_history_scan.py --write-report` on "
        + report["generated_at"] + ". Read-only measurement — this report never runs a history "
        "rewrite (`git filter-repo`/BFG); see `docs/HISTORY_MIGRATION_PLAN.md` for the "
        "separately-approved dry-run plan."
    )
    lines.append("")
    lines.append("## Whole-repository object database (`git count-objects -vH`)")
    lines.append("")
    counts = report["git_count_objects"]
    lines.append("| metric | value |")
    lines.append("|---|---|")
    for k in ("count", "size", "in-pack", "packs", "size-pack", "prune-packable",
              "garbage", "size-garbage"):
        if k in counts:
            lines.append("| %s | %s |" % (k, counts[k]))
    lines.append("")
    lines.append(
        "`size-pack` is the number GitHub's reported clone size is dominated by — it is the REAL "
        "measured packed-object size of this repository's `.git`, not an estimate."
    )
    lines.append("")
    lines.append("## Historical blob footprint (every blob ever reachable from any local ref)")
    lines.append("")
    lines.append("- distinct blobs ever committed: **%d**" % report["distinct_blobs_ever_committed"])
    lines.append("- sum of their (uncompressed) sizes: **%s**" % report["total_historical_blob_human"])
    lines.append("")
    lines.append(
        "(Uncompressed blob-content sum, not the same number as `size-pack` above — pack "
        "compression + delta-encoding make `size-pack` smaller than the raw sum of blob "
        "contents. Both numbers are reported because the audit needs both: `size-pack` is what a "
        "clone downloads, the uncompressed sum is what a rewrite would actually stop storing.)"
    )
    lines.append("")
    lines.append("## Top %d largest blobs ever committed" % len(report["top_blobs"]))
    lines.append("")
    lines.append("| size | sha | path(s) |")
    lines.append("|---|---|---|")
    for b in report["top_blobs"]:
        paths = ", ".join(b["paths"][:3]) + (" ..." if len(b["paths"]) > 3 else "")
        lines.append("| %s | `%s` | %s |" % (_fmt_bytes(b["size_bytes"]), b["sha"][:12], paths))
    lines.append("")
    lines.append("## Historical bytes by file extension")
    lines.append("")
    lines.append("| extension | blob count | total bytes |")
    lines.append("|---|---|---|")
    for e in report["by_extension"][:20]:
        lines.append("| %s | %d | %s |" % (e["ext"], e["count"], _fmt_bytes(e["bytes"])))
    lines.append("")
    lines.append(
        "## Reproducing this report\n\n"
        "```\npython3 scripts/repo_history_scan.py --write-report\n```\n\n"
        "This regenerates both this file and `docs/repo_size_report.json` from the CURRENT git "
        "object database of the clone it runs in — the numbers will differ slightly between "
        "clones only if the underlying refs/objects differ (e.g. a shallow clone). Run it from a "
        "full, unshallowed clone for numbers that match the canonical remote.\n"
    )
    return "\n".join(lines) + "\n"


def write_report(report):
    os.makedirs(DOCS_DIR, exist_ok=True)
    with open(REPORT_JSON, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, sort_keys=True)
        f.write("\n")
    with open(REPORT_MD, "w", encoding="utf-8") as f:
        f.write(render_markdown(report))


def main(argv=None):
    args = list(argv if argv is not None else sys.argv[1:])
    top_n = DEFAULT_TOP_N
    if "--top" in args:
        idx = args.index("--top")
        if idx + 1 < len(args):
            try:
                top_n = int(args[idx + 1])
            except ValueError:
                pass
    as_json = "--json" in args
    do_write = "--write-report" in args

    report = scan(top_n=top_n)

    if do_write:
        write_report(report)
        print("wrote %s + %s" % (
            os.path.relpath(REPORT_MD, REPO), os.path.relpath(REPORT_JSON, REPO)))

    if as_json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print("=== repo history scan (#294 AC1) ===")
        counts = report["git_count_objects"]
        print("size-pack: %s   count: %s   in-pack: %s" % (
            counts.get("size-pack", "?"), counts.get("count", "?"), counts.get("in-pack", "?")))
        print("distinct blobs ever committed: %d, total (uncompressed) %s" % (
            report["distinct_blobs_ever_committed"], report["total_historical_blob_human"]))
        print("--- top %d largest historical blobs ---" % len(report["top_blobs"]))
        for b in report["top_blobs"]:
            paths = ", ".join(b["paths"][:2])
            print("  %10s  %s  %s" % (_fmt_bytes(b["size_bytes"]), b["sha"][:12], paths))
    return 0


def selftest():
    """Cheap in-process sanity check that the scan plumbing works against THIS repo's real
    object database (no mocking — the whole point of this tool is real numbers)."""
    checks = []
    try:
        report = scan(top_n=5)
        checks.append(("scan() returns a report dict", isinstance(report, dict)))
        checks.append(("distinct_blobs_ever_committed > 0",
                        report["distinct_blobs_ever_committed"] > 0))
        checks.append(("total_historical_blob_bytes > 0",
                        report["total_historical_blob_bytes"] > 0))
        checks.append(("top_blobs sorted descending", all(
            report["top_blobs"][i]["size_bytes"] >= report["top_blobs"][i + 1]["size_bytes"]
            for i in range(len(report["top_blobs"]) - 1)
        )))
        checks.append(("git_count_objects has size-pack or size",
                        "size-pack" in report["git_count_objects"]
                        or "size" in report["git_count_objects"]))
        md = render_markdown(report)
        checks.append(("render_markdown produces non-empty text", len(md) > 100))
    except Exception as exc:  # pragma: no cover - defensive
        checks.append(("scan() raised: %s" % exc, False))
    ok = all(v for _, v in checks)
    for name, v in checks:
        print("  [%s] %s" % ("ok" if v else "XX", name))
    print("repo_history_scan selftest: %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        sys.exit(selftest())
    if len(sys.argv) > 1 and sys.argv[1] == "--describe-cli":
        print(json.dumps({
            "verbs": ["selftest"],
            "flags": ["--top", "--json", "--write-report", "--describe-cli"],
        }))
        sys.exit(0)
    sys.exit(main())

#!/usr/bin/env python3
"""simplicio-loop — PR review against the DoD + issue acceptance criteria.

When an issue is already claimed by another agent/session (see `scripts/coordinator.py`'s
DEFER_ACTIVE_CLAIM), the highest-leverage thing a session can do instead of duplicating work is
review the OPEN PRs against this repo's own bar: CLAUDE.md's 7-dimension Definition of Done
(implementação, testes unitários, testes de integração, testes de sistema, testes de regressão,
benchmark de performance, cobertura mínima) and the specific issue's frozen acceptance criteria
(the `- [ ]` checklist in the issue body). A PR that's missing one of those isn't ready — this
module produces a mechanical, reproducible verdict instead of a vibe-based "LGTM", and a concrete
list of what the CLAIMING agent still needs to add.

This is deliberately a TEXT-level check (regex over the PR body + issue body), not a diff/coverage-
tool integration — it catches the common failure mode (a PR that never mentions perf/coverage/
regression at all) without needing to actually execute the other session's test suite. A dimension
is considered addressed if its keyword appears in the PR body, OR the PR explicitly says it doesn't
apply (CLAUDE.md's own escape hatch: "skip only the ones that genuinely do not apply ... and say
why"). Any AC checklist item still unchecked (`- [ ]`) in the ISSUE at review time is reported as
unresolved, unless the PR body explicitly claims it verified (evidence phrase nearby).

Verbs:
    review    Pure function over --pr-body-file/--issue-body-file (or stdin JSON
              {"pr_body", "issue_body"}) -> a DoD + AC verdict. No network.
    check     Network-touching CLI: fetch a PR + its referenced issue via `gh`, run `review`,
              and print the verdict. `--post` additionally posts it as a PR comment via `gh pr
              comment`. Fail-open: if `gh` is unavailable, prints UNVERIFIED and exits 0 (the
              review core itself never depends on network availability).
    selftest  Prove the review logic deterministically against fixture bodies — no network.

Usage:
    python3 scripts/pr_dod_review.py check --repo wesleysimplicio/simplicio-loop --pr 481
    python3 scripts/pr_dod_review.py check --repo wesleysimplicio/simplicio-loop --pr 481 --post
    python3 scripts/pr_dod_review.py selftest
"""
import json
import os
import re
import subprocess
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

# CLAUDE.md's 7-dimension Definition of Done (this repo's own gate, not invented here).
DOD_DIMENSIONS = [
    ("implementacao", r"##\s*(summary|resumo)|##\s*(what|mudan)", "the change itself, present and described"),
    ("testes_unitarios", r"\bunit tests?\b|testes? unit[aá]rios?|pytest\b", "unit-level coverage of the new/changed logic"),
    ("testes_integracao", r"\bintegration tests?\b|testes? de integra[cç][aã]o", "verified against real collaborators, no mocks for the seam under test"),
    ("testes_sistema", r"\b(system test|e2e|end-to-end)\b|testes? de sistema", "an end-to-end pass through the real command/CLI/API surface"),
    ("testes_regressao", r"\bregression\b|regress[aã]o", "the existing suite still green"),
    ("benchmark_performance", r"\bbenchmark\b|\bperformance\b|lat[eê]ncia|throughput", "a measured number for any change touching a hot path"),
    ("cobertura_minima", r"\bcoverage\b|cobertura\b|\b8[5-9]%|\b9[0-9]%", "line/branch coverage >= 85% for touched files"),
]

NOT_APPLICABLE_RE = re.compile(
    r"not applicable|n\.?/?a\.?\b|n[ãa]o se aplica|not in scope|out of scope|"
    r"n[ãa]o aplic[aá]vel|fora do escopo", re.I)

AC_LINE_RE = re.compile(r"^\s*-\s*\[( |x|X)\]\s*(.+)$", re.M)

EVIDENCE_NEAR_RE = re.compile(
    r"(passed|passou|verified|verificad[oa]|evidence|evid[eê]ncia|PASS\b)", re.I)


def log(msg):
    print(msg, flush=True)


def extract_ac_items(issue_body):
    """Return [{"text": ..., "checked": bool}, ...] from an issue body's checklist lines."""
    items = []
    for m in AC_LINE_RE.finditer(issue_body or ""):
        checked = m.group(1).strip().lower() == "x"
        items.append({"text": m.group(2).strip(), "checked": checked})
    return items


def review(pr_body, issue_body=""):
    pr_body = pr_body or ""
    issue_body = issue_body or ""

    dod_results = []
    for key, pattern, description in DOD_DIMENSIONS:
        addressed = bool(re.search(pattern, pr_body, re.I))
        skipped_with_reason = False
        if not addressed and NOT_APPLICABLE_RE.search(pr_body):
            skipped_with_reason = True
        dod_results.append({
            "dimension": key,
            "description": description,
            "addressed": addressed,
            "skipped_with_reason": skipped_with_reason,
        })

    missing_dod = [d["dimension"] for d in dod_results
                   if not d["addressed"] and not d["skipped_with_reason"]]

    ac_items = extract_ac_items(issue_body)
    unresolved_acs = []
    for item in ac_items:
        if item["checked"]:
            continue
        # a still-unchecked AC in the issue counts as unresolved unless the PR body itself
        # claims evidence for that exact text nearby (loose substring + evidence-word check).
        snippet = item["text"][:40]
        if snippet and snippet.lower() in pr_body.lower():
            idx = pr_body.lower().find(snippet.lower())
            window = pr_body[max(0, idx - 80): idx + 160]
            if EVIDENCE_NEAR_RE.search(window):
                continue
        unresolved_acs.append(item["text"])

    verdict = "COMPLIANT" if not missing_dod and not unresolved_acs else "GAPS_FOUND"
    return {
        "verdict": verdict,
        "dod": dod_results,
        "missing_dod": missing_dod,
        "ac_items_total": len(ac_items),
        "unresolved_acs": unresolved_acs,
    }


def render_comment(result, pr_number=None, issue_number=None):
    lines = []
    header = "## 🤖 DoD + AC review"
    if pr_number:
        header += f" — PR #{pr_number}"
    lines.append(header)
    lines.append("")
    lines.append(f"**Verdict: {result['verdict']}**")
    lines.append("")
    lines.append("### Definition of Done (CLAUDE.md, 7 dimensions)")
    for d in result["dod"]:
        if d["addressed"]:
            mark = "✅"
        elif d["skipped_with_reason"]:
            mark = "⚠️ (marked not-applicable in PR body)"
        else:
            mark = "❌ MISSING"
        lines.append(f"- {mark} **{d['dimension']}** — {d['description']}")
    lines.append("")
    if issue_number:
        lines.append(f"### Acceptance criteria (issue #{issue_number})")
        if result["ac_items_total"] == 0:
            lines.append("_No `- [ ]` checklist found in the issue body._")
        elif not result["unresolved_acs"]:
            lines.append(f"All {result['ac_items_total']} AC line(s) checked or evidenced in the PR body.")
        else:
            lines.append(f"{len(result['unresolved_acs'])}/{result['ac_items_total']} unresolved:")
            for text in result["unresolved_acs"]:
                lines.append(f"- [ ] {text}")
    lines.append("")
    if result["verdict"] == "GAPS_FOUND":
        lines.append("**Action needed before merge:** add the missing dimensions/ACs above, "
                     "or explicitly note why each doesn't apply.")
    else:
        lines.append("No mechanical gaps found — DoD dimensions and issue ACs are all "
                      "addressed or explicitly marked not-applicable.")
    return "\n".join(lines)


def cmd_review(opts):
    pr_body_file = opts.get("pr-body-file")
    issue_body_file = opts.get("issue-body-file")
    if pr_body_file or issue_body_file:
        pr_body = open(pr_body_file, encoding="utf-8").read() if pr_body_file else ""
        issue_body = open(issue_body_file, encoding="utf-8").read() if issue_body_file else ""
    else:
        payload = json.loads(sys.stdin.read())
        pr_body = payload.get("pr_body", "")
        issue_body = payload.get("issue_body", "")
    result = review(pr_body, issue_body)
    tag = "MEASURED|"
    print(tag + json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


def _gh(*args):
    try:
        out = subprocess.run(["gh"] + list(args), capture_output=True,
                              encoding="utf-8", errors="replace", timeout=30, cwd=REPO)
        if out.returncode != 0:
            return None
        return out.stdout
    except Exception:
        return None


def cmd_check(opts):
    repo = opts.get("repo")
    pr_number = opts.get("pr")
    if not repo or not pr_number:
        print("UNVERIFIED|pr_dod_review check: --repo and --pr are required")
        return 2
    pr_raw = _gh("pr", "view", str(pr_number), "--repo", repo, "--json", "number,title,body")
    if pr_raw is None:
        print(json.dumps({"schema": "simplicio.pr-dod-review/v1", "status": "UNVERIFIED",
                          "reason_code": "gh_unavailable"}, ensure_ascii=False))
        return 0
    pr = json.loads(pr_raw)
    pr_body = pr.get("body") or ""
    title = pr.get("title") or ""
    m = re.search(r"#(\d+)", title) or re.search(r"#(\d+)", pr_body)
    issue_number = int(m.group(1)) if m else None
    issue_body = ""
    if issue_number:
        issue_raw = _gh("issue", "view", str(issue_number), "--repo", repo, "--json", "body")
        if issue_raw:
            issue_body = json.loads(issue_raw).get("body") or ""

    result = review(pr_body, issue_body)
    comment = render_comment(result, pr_number=pr_number, issue_number=issue_number)
    print("MEASURED|" + json.dumps(result, ensure_ascii=False, sort_keys=True))
    print(comment)

    if opts.get("post"):
        post_out = _gh("pr", "comment", str(pr_number), "--repo", repo, "--body", comment)
        if post_out is None:
            print("UNVERIFIED|failed to post comment (gh unavailable or PR comment rejected)")
            return 1
        print("MEASURED|comment posted")
    return 0


def cmd_selftest(_opts):
    checks = []

    def chk(name, got, want):
        checks.append((name, got == want, got, want))

    complete_pr_body = """## Summary
Adds X.

Unit tests cover Y. Integration tests exercise the real DB. Ran an end-to-end (e2e) smoke test.
Regression: the existing suite is green. Benchmark: latency dropped from 12ms to 4ms.
Coverage: 92% on touched files.
"""
    r = review(complete_pr_body, "")
    chk("complete_body_is_compliant_with_no_issue", r["verdict"], "COMPLIANT")
    chk("complete_body_has_no_missing_dod", r["missing_dod"], [])

    minimal_pr_body = "## Summary\nAdds X.\n"
    r = review(minimal_pr_body, "")
    chk("minimal_body_flags_missing_dimensions", len(r["missing_dod"]) > 0, True)
    chk("minimal_body_is_gaps_found", r["verdict"], "GAPS_FOUND")

    na_pr_body = ("## Summary\nDocs-only change, no runtime code touched. "
                 "Not applicable for this PR — no new logic, no cross-service seam, "
                 "no full run flow, nothing that could regress, no hot path, nothing to measure.\n")
    r = review(na_pr_body, "")
    chk("not_applicable_dimensions_are_not_missing", r["missing_dod"], [])
    chk("not_applicable_marks_skipped_with_reason", all(
        d["skipped_with_reason"] for d in r["dod"] if d["dimension"] != "implementacao"), True)

    issue_body_with_acs = """## Critérios de aceite
- [x] first thing done
- [ ] second thing not done
- [ ] third thing not done
"""
    r = review(minimal_pr_body, issue_body_with_acs)
    chk("ac_extraction_counts_all_items", r["ac_items_total"], 3)
    chk("unresolved_acs_lists_unchecked_only", r["unresolved_acs"],
        ["second thing not done", "third thing not done"])

    pr_body_with_evidence_for_ac = (
        "## Summary\nAdds X.\n\nsecond thing not done -> verified with test_foo.py, PASS.\n")
    r = review(pr_body_with_evidence_for_ac, issue_body_with_acs)
    chk("ac_resolved_when_pr_cites_evidence_nearby", "second thing not done" in r["unresolved_acs"], False)
    chk("ac_still_unresolved_without_evidence", "third thing not done" in r["unresolved_acs"], True)

    r = review(complete_pr_body, issue_body_with_acs)
    chk("gaps_found_when_ac_unresolved_even_if_dod_complete", r["verdict"], "GAPS_FOUND")

    empty_issue = review(complete_pr_body, "")
    chk("no_issue_body_means_zero_ac_items", empty_issue["ac_items_total"], 0)

    comment = render_comment(review(minimal_pr_body, issue_body_with_acs), pr_number=1, issue_number=2)
    chk("comment_mentions_missing_and_pr_number", "PR #1" in comment and "MISSING" in comment, True)

    ok = True
    for name, passed, got, want in checks:
        tag = "PASS" if passed else "FAIL"
        print(f"  [{tag}] {name} (got={got!r} want={want!r})")
        ok = ok and passed

    n = len(checks)
    passed_n = sum(1 for _, p, _, _ in checks if p)
    if ok:
        print(f"MEASURED|pr_dod_review selftest: {passed_n}/{n} checks passed")
        return 0
    print(f"UNVERIFIED|pr_dod_review selftest: {passed_n}/{n} checks passed (FAILURES ABOVE)")
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
            "verbs": ["review", "check", "selftest"],
            "flags": ["--pr-body-file", "--issue-body-file", "--repo", "--pr", "--post"],
        }))
        sys.exit(0)
    sub, opts = argv[0], _parse(argv[1:])
    handler = {"review": cmd_review, "check": cmd_check, "selftest": cmd_selftest}.get(sub)
    if handler is None:
        print("unknown command '%s'. choices: review check selftest" % sub)
        sys.exit(2)
    sys.exit(handler(opts) or 0)


if __name__ == "__main__":
    main()

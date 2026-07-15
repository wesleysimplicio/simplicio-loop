#!/usr/bin/env python3
"""simplicio-loop — single source of truth for quantitative claims (#96).

Every quantitative number in README/SKILLs/AGENTS.md must either:
  a) point to a receipt artifact (file path under REPO), or
  b) be explicitly marked as "unverified" in the text.

This manifest declares every known quantitative claim and its status.
Imported by claims_audit.py (check 8).

No behavior of its own — pure data, imported by `claims_audit.py`.
"""

import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

# Regex patterns for quantitative claims in Markdown docs.
# A "quantitative claim" = a number that implies a measured outcome:
#   - percentages:  "93%", "up to 90%", "40-60%"
#   - token counts: "870-line file → 65 lines"
#   - "Nx" multipliers
#   - "up to N" patterns
# We deliberately EXCLUDE:
#   - version numbers
#   - line numbers in code examples
#   - badge counts (skills-7, runtimes-11) — those are structural, not quantitative outcomes
#   - URL-encoded %XX sequences (e.g. %20, %2B) — those are encoding, not percentages
QUANT_RE = re.compile(
    r"\b(\d{1,3}%|up\s+to\s+\d{1,3}%\b|"
    r"\d{1,4}%\s+fewer|\d{1,3}–\d{1,3}%\b|"
    r"\d{1,3}x\b(?!\s*more|\s*fewer))",
    re.I,
)

# ---- claim manifest ----
# Each entry:
#   id:         unique claim identifier
#   doc:        relative path to the doc containing the claim
#   text_glob:  substring the claim extractor found (for reporting)
#   status:     "verified" | "unverified"
#   receipt:    path to the receipt artifact (relative to REPO), or None
#   note:       explanation
CLAIMS = [
    {
        "id": "signatures-93pct-saved",
        "doc": "README.md",
        "text_glob": "93% saved",
        "status": "unverified",
        "receipt": None,
        "note": (
            "Claim: '870-line file → 65 lines (93% saved)'. "
            "No receipt snapshot in .orchestrator/savings/snapshots.jsonl. "
            "Must produce a measured receipt before marking verified."
        ),
    },
    {
        "id": "capture-proxy-60-95pct",
        "doc": "README.md",
        "text_glob": "60-95% fewer tokens",
        "status": "unverified",
        "receipt": None,
        "note": (
            "Claim: '60-95% fewer tokens on tool outputs via a transparent compression daemon'. "
            "Labelled explicitly as unverified in the README. "
            "No receipt snapshot exists."
        ),
    },
    {
        "id": "response-cache-100pct",
        "doc": "README.md",
        "text_glob": "100% on hit",
        "status": "unverified",
        "receipt": None,
        "note": (
            "Claim: 'Native response cache — 100% on hit'. "
            "Structural property of a cache, not a measured outcome. "
            "Kept in manifest for completeness; labelled unverified per policy."
        ),
    },
    {
        "id": "lmcache-40-70pct-ttft",
        "doc": "README.md",
        "text_glob": "40-70% TTFT",
        "status": "unverified",
        "receipt": None,
        "note": (
            "Claim: 'LMCache — 40-70% TTFT reduction'. "
            "Third-party claim, not measured in this repo. "
            "Labelled unverified."
        ),
    },
    {
        "id": "simplicio-compress-40-60pct",
        "doc": "README.md",
        "text_glob": "40-60% fewer",
        "status": "unverified",
        "receipt": None,
        "note": (
            "Claim: 'simplicio-compress — 40-60% fewer' tokens. "
            "No receipt snapshot exists. Marked unverified."
        ),
    },
    {
        "id": "dashboard-98pct-providers",
        "doc": "README.md",
        "text_glob": "98%",
        "status": "unverified",
        "receipt": None,
        "note": (
            "Claim: '141/144 providers (98%) we intercept'. "
            "Dashboard marketing copy. No receipt artifact exists."
        ),
    },
    {
        "id": "deterministic-edit-100pct",
        "doc": "README.md",
        "text_glob": "100% of edit tokens",
        "status": "unverified",
        "receipt": None,
        "note": (
            "Claim: 'deterministic_edit (L0) — 100% of edit tokens'. "
            "Structural property: file written mechanically. "
            "Kept in manifest for completeness; labelled unverified."
        ),
    },
    {
        "id": "runtimes-badge-12pct",
        "doc": "README.md",
        "text_glob": "12%",
        "status": "unverified",
        "receipt": None,
        "note": (
            "False-positive match: the shields.io badge URL "
            "'runtimes-12%20(3%20garantidos...)' has '12' followed by the URL-encoded space "
            "'%20', not a percentage — the '12' is the runtime count. Kept in manifest so the "
            "extractor's known false positive is documented rather than silently ignored."
        ),
    },
    {
        "id": "coverage-283-baseline-fase-b",
        "doc": "README.md",
        "text_glob": "16.6% / 9.4% to 28.45% / 24.02%",
        "status": "verified",
        "receipt": "quality/coverage-baseline.json",
        "note": (
            "Claim: '#283 measured coverage raised from 16.6% / 9.4% to 28.45% / 24.02% "
            "(global / critical) on the widened scope.' Real numbers from "
            "scripts/coverage_gate.py, receipt at quality/coverage-baseline.json "
            "(global_pct/critical_pct + previous_baseline.global_pct/critical_pct), bound to "
            "commit d37b28d2b1f67b776dcc06a5acd7348369abe150 (PR #407)."
        ),
    },
    {
        "id": "infographic-90pct-fewer-tokens",
        "doc": "README.md",
        "text_glob": "90%",
        "status": "unverified",
        "receipt": None,
        "note": (
            "Claim: infographic alt text 'up to 90% fewer tokens'. No receipt snapshot exists. "
            "Marked unverified."
        ),
    },
]

# Docs to scan for quantitative claims
CLAIM_DOCS = ["README.md", "AGENTS.md"]


def extract_claims(doc_root=REPO):
    """Scan CLAIM_DOCS for QUANT_RE matches and cross-reference with manifest.

    Returns list of (doc_rel, match_text) for claims NOT in the manifest
    (i.e. undocumented quantitative claims that must be added or labelled).
    """
    found = []
    manifest_texts = set(c["text_glob"].lower() for c in CLAIMS)
    for doc_rel in CLAIM_DOCS:
        doc_path = os.path.join(doc_root, doc_rel)
        if not os.path.exists(doc_path):
            continue
        with open(doc_path, encoding="utf-8", errors="replace") as f:
            text = f.read()
        for m in QUANT_RE.finditer(text):
            match_lower = m.group(0).lower()
            # Skip common non-claims
            if match_lower in ("100%", "50%", "0%"):
                continue
            # Check if any manifest entry substring matches
            matched = False
            for mtext in manifest_texts:
                if mtext in match_lower or match_lower in mtext:
                    matched = True
                    break
            if not matched:
                found.append((doc_rel, m.group(0)))
    return found


def selftest():
    """Prove the manifest + extractor logic deterministically."""
    checks = []
    # Each claim has required fields
    for c in CLAIMS:
        for field in ("id", "doc", "text_glob", "status", "receipt"):
            if field not in c:
                checks.append((f"claim {c.get('id', '?')} missing {field}", False))
    # Status must be valid
    for c in CLAIMS:
        valid = c["status"] in ("verified", "unverified")
        checks.append((f"claim {c['id']} status '{c['status']}'", valid))
    # No duplicate IDs
    ids = [c["id"] for c in CLAIMS]
    checks.append(("no duplicate claim IDs", len(ids) == len(set(ids))))
    # All claims non-empty
    checks.append(("claims list non-empty", len(CLAIMS) > 0))
    # Extractor works (should find at least 1 claim in README.md)
    unknown = extract_claims()
    checks.append(("extractor runs without crashing", True))
    if unknown:
        checks.append((
            "unknown claims found — add to manifest or label 'unverified'",
            False,
        ))
        for doc_rel, match in unknown:
            checks.append((f"  {doc_rel}: '{match}' — not in manifest", False))
    ok = all(v for _, v in checks)
    for name, v in checks:
        print("  [%s] %s" % ("ok" if v else "XX", name))
    print("claims_manifest selftest: %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        sys.exit(selftest())
    if len(sys.argv) > 1 and sys.argv[1] == "--describe-cli":
        import json
        print(json.dumps({
            "verbs": ["selftest"],
            "flags": ["--help"],
        }))
        sys.exit(0)
    print(__doc__)

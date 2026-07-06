"""Unit tests for `scripts/claims_manifest.py` — the single source of truth for quantitative
claims (#96), imported by `claims_audit.py` check 8. In-process, no subprocess.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))
import claims_manifest  # noqa: E402


def test_every_claim_has_required_fields():
    for c in claims_manifest.CLAIMS:
        for field in ("id", "doc", "text_glob", "status", "receipt", "note"):
            assert field in c, "claim %s missing %s" % (c.get("id", "?"), field)


def test_claim_ids_are_unique():
    ids = [c["id"] for c in claims_manifest.CLAIMS]
    assert len(ids) == len(set(ids))


def test_claim_status_is_valid():
    for c in claims_manifest.CLAIMS:
        assert c["status"] in ("verified", "unverified"), c


def test_unverified_claim_without_receipt_has_no_receipt_path():
    for c in claims_manifest.CLAIMS:
        if c["status"] == "unverified":
            assert c["receipt"] is None or isinstance(c["receipt"], str)


def test_quant_re_matches_percentages():
    assert claims_manifest.QUANT_RE.search("93% saved")
    assert claims_manifest.QUANT_RE.search("up to 90%")
    assert claims_manifest.QUANT_RE.search("40-60% fewer")


def test_quant_re_does_not_match_url_encoding():
    assert not claims_manifest.QUANT_RE.search("path%20with%20spaces")


def test_extract_claims_runs_against_real_repo_without_crashing():
    unknown = claims_manifest.extract_claims()
    assert isinstance(unknown, list)


def test_extract_claims_finds_nothing_new_in_an_empty_repo(tmp_path):
    unknown = claims_manifest.extract_claims(doc_root=str(tmp_path))
    assert unknown == []


def test_extract_claims_flags_undocumented_claim(tmp_path):
    (tmp_path / "README.md").write_text("We saw a 77% improvement in latency.\n", encoding="utf-8")
    unknown = claims_manifest.extract_claims(doc_root=str(tmp_path))
    assert any("77%" in match for _doc, match in unknown)


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _selfrun import run_module
    run_module(globals(), "test_claims_manifest_unit")

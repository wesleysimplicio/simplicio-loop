"""Epic #568 cross-repo Prototype-First conformance suite.

Epic #568's DAG requires "todos os adapters + Loop #568 -> conformance
cross-repo -> FULL/delivery"; its AC includes "Todos os projetos consumidores
passam contract/conformance tests." This test module is the schema owner's
half: it runs ``scripts/prototype_conformance_suite.py`` against the 8
sibling repos that have built a Prototype-First adapter and asserts on the
REAL structural relationship between what each sibling emits and this repo's
4 canonical schemas (``simplicio_loop/prototype_gate.py``).

Important: most siblings genuinely DO NOT conform to the canonical field
shape today (different field names, missing fields, or a schema-string
collision with an incompatible payload). That is real, current drift, not a
suite bug -- these tests PIN the currently-known drift as an explicit
regression fixture, matching this repo's own DoD ("clearly report real drift
findings as intentional failures/warnings, not silent skips"). If a sibling
later closes its gap with canonical, the corresponding assertion here will
start failing loudly and MUST be updated alongside that fix (not silently
loosened) -- the pin is deliberate, not a rubber stamp.

Siblings that were never checked out on this host (the common case for an
ordinary single-repo CI runner) are reported BLOCKED by the suite and the
corresponding assertions are skipped explicitly, never silently -- see
``_maybe_skip_blocked``.

Run: python3 -m pytest tests/test_cross_repo_conformance_prototype.py -q
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SUITE = REPO / "scripts" / "prototype_conformance_suite.py"


def _run(args):
    return subprocess.run(
        [sys.executable, str(SUITE), *args],
        cwd=str(REPO), capture_output=True, text=True,
    )


@pytest.fixture(scope="module")
def report():
    out = REPO / "prototype-conformance-report.json"
    proc = _run(["--json", str(out)])
    assert proc.returncode == 0, proc.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    out.unlink()
    return data


def _by_repo(report_data, name):
    return next(r for r in report_data["results"] if r["repo"] == name)


def _finding(sibling_report, schema_target):
    """Return the finding dict for a given canonical target ('plan'/'candidate'/
    'decision'/'receipt'), or None if the sibling never claims that schema."""
    for f in sibling_report["findings"]:
        if f["canonical_target"] == schema_target:
            return f
    return None


def _maybe_skip_blocked(sibling_report):
    if not sibling_report["available"]:
        pytest.skip(f"{sibling_report['repo']} not checked out on this host: "
                    f"{sibling_report['reason']}")


# --- suite-level shape ---------------------------------------------------------------------------

def test_report_schema_and_shape(report):
    assert report["schema"] == "simplicio.prototype-conformance/v1"
    assert report["issue"] == 568
    assert report["total_siblings"] == 8
    assert len(report["results"]) == 8
    assert set(report["canonical_fields"].keys()) == {"plan", "candidate", "decision", "receipt"}
    # Canonical field sets must be non-trivial and must include the self-hash field.
    assert "plan_hash" in report["canonical_fields"]["plan"]
    assert "candidate_hash" in report["canonical_fields"]["candidate"]
    assert "decision_hash" in report["canonical_fields"]["decision"]
    assert "receipt_hash" in report["canonical_fields"]["receipt"]


def test_all_eight_siblings_present(report):
    names = {r["repo"] for r in report["results"]}
    assert names == {
        "simplicio-mapper", "simplicio-runtime", "simplicio-dev-cli", "simplicio-agent",
        "simplicio-loop-oss", "simplicio-loop-marketing", "simplicio-sprint", "simplicio-prompt",
    }


def test_unknown_sibling_is_rejected():
    proc = _run(["not-a-sibling"])
    assert proc.returncode == 2
    assert "unknown sibling" in proc.stderr


# --- distinct-by-design schemas: never graded against canonical, never marked non-conformant -----

@pytest.mark.parametrize("name,expected_reason_snippet", [
    ("simplicio-mapper", "prototype-context/v1"),
    ("simplicio-loop-marketing", "marketing-prototype-gate/v1"),
    ("simplicio-sprint", "sprint-prototype/v1"),
])
def test_distinct_schema_siblings_are_not_graded_against_canonical(report, name, expected_reason_snippet):
    r = _by_repo(report, name)
    _maybe_skip_blocked(r)
    assert expected_reason_snippet in r["reason"]
    for f in r["findings"]:
        assert f["classification"] in ("distinct", "unresolved")
        assert f["canonical_target"] is None
        assert f["conformant"] is None


def test_loop_oss_emits_no_schema_tagged_payload_at_all(report):
    """loop-oss's prototype_gate.py implements the workflow discipline
    (reproducer/read-only-guard/judge-verdict) but never builds a
    schema-tagged JSON object -- it does not participate in the JSON
    contract at all, contrary to a naive assumption that it "mirrors" the
    canonical module closely."""
    r = _by_repo(report, "simplicio-loop-oss")
    _maybe_skip_blocked(r)
    assert r["findings"] == []


# --- claims-canonical schemas: real, current drift is pinned explicitly --------------------------

def test_runtime_candidate_schema_string_matches_but_shape_is_receipt_like(report):
    """simplicio-runtime/src/prototype_gate.rs stamps its execution-receipt
    struct with the exact canonical CANDIDATE_SCHEMA string
    ('simplicio.prototype-candidate/v1') while the struct itself
    (tier/sandbox_root/actions/artifacts/content_hash/...) has none of the
    canonical candidate's authoring-time fields (strategy/agent_id/
    artifact_hash/...). This is a real schema-string collision with an
    incompatible payload shape -- flagged here, not silently accepted."""
    r = _by_repo(report, "simplicio-runtime")
    _maybe_skip_blocked(r)
    f = _finding(r, "candidate")
    assert f is not None, "expected simplicio-runtime to claim the canonical candidate schema string"
    assert f["conformant"] is False
    for required in ("strategy", "agent_id", "artifact_hash"):
        assert required in f["fields_missing"]
    # The struct DOES happen to have a field literally named `status` (present, not
    # missing) -- but its values ("ok"/"blocked"/"quota_exceeded"/"killed") are disjoint
    # from canonical's CANDIDATE_STATUSES ("proposed"/"validated"/"rejected"/"accepted"/
    # "abandoned"). Field-name presence alone is not proof of semantic conformance; this
    # suite compares field NAMES (source-level, in both languages), not enum values,
    # which is a genuine limitation documented here rather than glossed over.
    assert "status" in f["fields_found"]


def test_dev_cli_plan_is_a_strict_subset_of_canonical(report):
    """simplicio-dev-cli's plan omits work_item_id/level/budget_fraction/
    estimated_budget/context_pack_hash/negative_space by default (only
    populated if the caller supplies them via --input, which is not part of
    the schema literal itself)."""
    r = _by_repo(report, "simplicio-dev-cli")
    _maybe_skip_blocked(r)
    f = _finding(r, "plan")
    assert f is not None
    assert f["conformant"] is False
    for required in ("work_item_id", "level", "budget_fraction", "estimated_budget"):
        assert required in f["fields_missing"]


def test_dev_cli_decision_self_hash_field_is_misnamed(report):
    """dev-cli's decision payload calls its own content hash `receipt_hash`,
    not `decision_hash` -- a field-naming collision with canonical, which
    reserves `receipt_hash` for the receipt schema's own self-hash field."""
    r = _by_repo(report, "simplicio-dev-cli")
    _maybe_skip_blocked(r)
    f = _finding(r, "decision")
    assert f is not None
    assert f["conformant"] is False
    assert "decision_hash" in f["fields_missing"]
    assert "judge_id" in f["fields_missing"]
    assert "judge_independent" in f["fields_missing"]


def test_dev_cli_receipt_is_a_scaffold_record_not_a_hash_chain(report):
    """dev-cli's 'receipt' is a filesystem-scaffold/validation record keyed
    by a tree-content candidate_hash -- a different concept entirely from
    canonical's decision_hash/stage_hashes/attempt/fence hash-chain."""
    r = _by_repo(report, "simplicio-dev-cli")
    _maybe_skip_blocked(r)
    f = _finding(r, "receipt")
    assert f is not None
    assert f["conformant"] is False
    for required in ("decision_hash", "stage_hashes", "attempt", "fence", "receipt_hash"):
        assert required in f["fields_missing"]


@pytest.mark.parametrize("target,required_missing", [
    ("plan", ("plan_hash", "work_item_id", "source_sha", "goal")),
    ("candidate", ("candidate_id", "plan_hash", "strategy", "agent_id", "artifact_hash")),
    ("decision", ("plan_hash", "candidate_hash", "decision_hash")),
])
def test_agent_reuses_three_canonical_schema_strings_with_unrelated_shapes(report, target, required_missing):
    """simplicio-agent/agent/prototype_first_gate.py reuses 3 of the 4
    canonical schema strings (plan/candidate/decision) but with a
    structurally unrelated dataclass shape (hypothesis/approach_id/claims/
    RoleIdentity) -- none of the hash-binding fields survive."""
    r = _by_repo(report, "simplicio-agent")
    _maybe_skip_blocked(r)
    f = _finding(r, target)
    assert f is not None, f"expected simplicio-agent to claim the canonical {target} schema string"
    assert f["conformant"] is False
    for required in required_missing:
        assert required in f["fields_missing"]


def test_prompt_plan_mirror_is_missing_two_optional_canonical_fields(report):
    """simplicio-prompt's build_plan documents itself as a 'byte-for-byte
    mirror' of simplicio_loop.prototype_gate.build_plan but omits
    budget_fraction and negative_space, which the upstream canonical
    build_plan always adds."""
    r = _by_repo(report, "simplicio-prompt")
    _maybe_skip_blocked(r)
    f = _finding(r, "plan")
    assert f is not None
    assert f["conformant"] is False
    assert f["fields_missing"] == ["budget_fraction", "negative_space"]


def test_prompt_decision_mirror_predates_the_optional_judge_fields(report):
    """simplicio-prompt's build_decision mirrors only the P0-era canonical
    decision shape (schema/plan_hash/source_sha/candidate_hash/decision/
    reason/decision_hash) and predates the later judge/ranking/AC-coverage
    optional fields upstream added."""
    r = _by_repo(report, "simplicio-prompt")
    _maybe_skip_blocked(r)
    f = _finding(r, "decision")
    assert f is not None
    assert f["conformant"] is False
    for required in ("judge_id", "judge_independent", "ranked_candidates", "ac_coverage"):
        assert required in f["fields_missing"]
    # But the hash-chain fields it DOES claim to mirror must actually be present.
    for present in ("schema", "plan_hash", "source_sha", "candidate_hash", "decision", "decision_hash"):
        assert present in f["fields_found"]


def test_no_sibling_is_falsely_reported_fully_conformant(report):
    """Sanity/anti-fabrication check for this suite itself: as of this
    writing every sibling that claims a canonical schema string has at
    least one missing required field. If this ever flips green for all of
    them, it is real news (a sibling actually caught up to canonical) and
    the specific pinned tests above must be updated to match -- it must
    never flip green silently via a suite bug."""
    claims_canonical = [
        f for r in report["results"] for f in r["findings"]
        if f["classification"] == "claims-canonical"
    ]
    assert claims_canonical, "expected at least one sibling to claim a canonical schema string"
    assert all(f["conformant"] is False for f in claims_canonical), (
        "a sibling now reports as fully conformant -- update the pinned drift "
        "tests above to reflect the real, improved state instead of leaving "
        "them stale"
    )

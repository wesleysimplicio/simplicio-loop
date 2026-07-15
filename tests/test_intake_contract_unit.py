"""Unit tests for simplicio_loop/intake_contract.py (#284).

Covers:
- compile_intake hard gates (objective empty, delivery_target invalid,
  no ACs, AC vague, AC without verification, scope both empty,
  impact missing / not_applicable without justification)
- normalize_ac: stable IDs, origin tagging, source_ref enforcement
- normalize_impact_entry: all branches
- make_source_snapshot: field presence
- lint_intake: catches schema drift, duplicate AC IDs, snapshot missing
- freeze_intake: deterministic hash, idempotent on double-freeze
- Regression: original ACs preserved (not weakened), derived ACs tagged
"""
import copy
import json
import pytest

from simplicio_loop.intake_contract import (
    INTAKE_SCHEMA,
    IMPACT_CATEGORIES,
    DELIVERY_TARGETS,
    VERDICT_COMPLETE,
    VERDICT_BLOCKED,
    VERDICT_AWAITING_DECISION,
    VERDICT_STALE_SOURCE,
    VERDICT_LEASE_LOST,
    IntakeBlockedError,
    compile_intake,
    freeze_intake,
    lint_intake,
    make_ac_id,
    make_source_snapshot,
    normalize_ac,
    normalize_impact_entry,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _minimal_impact_map():
    """All categories set to not_applicable with justification."""
    return {cat: {"status": "not_applicable", "justification": "n/a for unit test"} for cat in IMPACT_CATEGORIES}


def _valid_ac(index=1, *, origin="source"):
    return {
        "text": f"The system must do thing {index}",
        "verification": f"run pytest -k test_{index}",
        "source_ref": f"issue body line {index}",
        "origin": origin,
    }


def _valid_snapshot():
    return make_source_snapshot(
        provider="github",
        repo="wesleysimplicio/simplicio-loop",
        item_id="284",
        url="https://github.com/wesleysimplicio/simplicio-loop/issues/284",
        revision="2024-01-01T00:00:00Z",
        snapshot_hash="abc123",
    )


def _compile(**kwargs):
    """Compile a valid minimal intake, overriding any fields via kwargs."""
    defaults = dict(
        run_id="run-1",
        work_item_id="284",
        attempt_id="attempt-1",
        source_snapshot=_valid_snapshot(),
        title="feat: deep intake gate",
        objective="Implement the deep intake contract for #284",
        delivery_target="verified",
        scope_in=["simplicio_loop/intake_contract.py"],
        scope_out=["simplicio_loop/runner.py (no changes this PR)"],
        acceptance_criteria=[_valid_ac(1), _valid_ac(2)],
        impact_map=_minimal_impact_map(),
    )
    defaults.update(kwargs)
    return compile_intake(**defaults)


# ---------------------------------------------------------------------------
# make_ac_id
# ---------------------------------------------------------------------------

def test_make_ac_id_zero_padded():
    assert make_ac_id(1) == "AC-001"
    assert make_ac_id(42) == "AC-042"
    assert make_ac_id(100) == "AC-100"


# ---------------------------------------------------------------------------
# normalize_ac hard gates
# ---------------------------------------------------------------------------

def test_normalize_ac_stable_id_from_index():
    ac = normalize_ac({"text": "do it", "verification": "run it", "source_ref": "line 1"}, 5)
    assert ac["id"] == "AC-005"


def test_normalize_ac_preserves_explicit_id():
    ac = normalize_ac({"id": "AC-007", "text": "do it", "verification": "run it", "source_ref": "L1"}, 1)
    assert ac["id"] == "AC-007"


def test_normalize_ac_blocks_on_empty_text():
    with pytest.raises(IntakeBlockedError, match="ac_text_empty"):
        normalize_ac({"text": "", "verification": "run it", "source_ref": "L1"}, 1)


def test_normalize_ac_blocks_on_missing_verification():
    with pytest.raises(IntakeBlockedError, match="ac_verification_missing"):
        normalize_ac({"text": "do it", "verification": "", "source_ref": "L1"}, 1)


def test_normalize_ac_status_defaults_to_pending():
    ac = normalize_ac({"text": "do it", "verification": "run it", "source_ref": "L1"}, 1)
    assert ac["status"] == "pending"


def test_normalize_ac_derived_origin_tagging():
    ac = normalize_ac({"text": "do it", "verification": "run it"}, 1, origin="derived")
    assert ac["origin"] == "derived"


# ---------------------------------------------------------------------------
# normalize_impact_entry
# ---------------------------------------------------------------------------

def test_normalize_impact_entry_not_applicable_with_justification():
    entry = normalize_impact_entry("code", {"status": "not_applicable", "justification": "no code changes"})
    assert entry["status"] == "not_applicable"
    assert entry["justification"] == "no code changes"


def test_normalize_impact_entry_none_raises():
    with pytest.raises(IntakeBlockedError, match="impact_category_missing:code"):
        normalize_impact_entry("code", None)


def test_normalize_impact_entry_not_applicable_no_justification_raises():
    with pytest.raises(IntakeBlockedError, match="impact_not_applicable_no_justification:code"):
        normalize_impact_entry("code", {"status": "not_applicable", "justification": ""})


def test_normalize_impact_entry_plain_string_treated_as_justification():
    entry = normalize_impact_entry("tests", "covered by existing test suite")
    assert entry["status"] == "not_applicable"
    assert "existing test" in entry["justification"]


def test_normalize_impact_entry_plain_not_applicable_string_raises():
    with pytest.raises(IntakeBlockedError, match="impact_not_applicable_no_justification"):
        normalize_impact_entry("code", "not_applicable")


# ---------------------------------------------------------------------------
# compile_intake hard gates
# ---------------------------------------------------------------------------

def test_compile_intake_happy_path_returns_envelope():
    env = _compile()
    assert env["schema"] == INTAKE_SCHEMA
    assert env["task"]["delivery_target"] == "verified"
    assert len(env["acceptance_criteria"]) == 2


def test_compile_intake_blocks_on_empty_objective():
    with pytest.raises(IntakeBlockedError, match="objective_empty"):
        _compile(objective="")


def test_compile_intake_blocks_on_invalid_delivery_target():
    with pytest.raises(IntakeBlockedError, match="delivery_target_invalid"):
        _compile(delivery_target="done")


def test_compile_intake_blocks_on_no_acs():
    with pytest.raises(IntakeBlockedError, match="no_acceptance_criteria"):
        _compile(acceptance_criteria=[])


def test_compile_intake_blocks_on_source_ac_without_source_ref():
    ac = {"text": "do it", "verification": "run it", "origin": "source"}
    with pytest.raises(IntakeBlockedError, match="source_ac_missing_ref"):
        _compile(acceptance_criteria=[ac])


def test_compile_intake_allows_derived_ac_without_source_ref():
    ac = {"text": "do it", "verification": "run it", "origin": "derived"}
    env = _compile(acceptance_criteria=[ac])
    assert env["acceptance_criteria"][0]["origin"] == "derived"


def test_compile_intake_blocks_when_scope_both_empty():
    with pytest.raises(IntakeBlockedError, match="scope_both_empty"):
        _compile(scope_in=[], scope_out=[])


def test_compile_intake_allows_scope_in_only():
    env = _compile(scope_in=["intake_contract.py"], scope_out=[])
    assert env["task"]["scope_in"] == ["intake_contract.py"]
    assert env["task"]["scope_out"] == []


def test_compile_intake_blocks_when_impact_category_missing():
    impact = _minimal_impact_map()
    del impact["security"]
    with pytest.raises(IntakeBlockedError, match="impact_category_missing:security"):
        _compile(impact_map=impact)


def test_compile_intake_blocks_on_impact_not_applicable_without_justification():
    impact = _minimal_impact_map()
    impact["performance"] = {"status": "not_applicable", "justification": ""}
    with pytest.raises(IntakeBlockedError, match="impact_not_applicable_no_justification:performance"):
        _compile(impact_map=impact)


def test_compile_intake_all_delivery_targets_accepted():
    for target in DELIVERY_TARGETS:
        env = _compile(delivery_target=target)
        assert env["task"]["delivery_target"] == target


# ---------------------------------------------------------------------------
# AC preservation: source ACs must not be weakened
# ---------------------------------------------------------------------------

def test_source_acs_are_preserved_verbatim():
    """Original AC text must survive compile without weakening."""
    original_text = "The mutation authority MUST be invalidated on plan drift"
    ac = {"text": original_text, "verification": "run test_planning_gate", "source_ref": "issue #284 body §5"}
    env = _compile(acceptance_criteria=[ac])
    assert env["acceptance_criteria"][0]["text"] == original_text


def test_source_acs_are_not_merged_with_derived():
    """Two source ACs must remain two ACs after compile."""
    acs = [_valid_ac(1), _valid_ac(2)]
    env = _compile(acceptance_criteria=acs)
    assert len(env["acceptance_criteria"]) == 2
    assert env["acceptance_criteria"][0]["id"] != env["acceptance_criteria"][1]["id"]


# ---------------------------------------------------------------------------
# lint_intake
# ---------------------------------------------------------------------------

def test_lint_intake_valid_envelope():
    env = _compile()
    result = lint_intake(env)
    assert result["valid"] is True
    assert result["errors"] == []


def test_lint_intake_schema_invalid():
    env = _compile()
    env["schema"] = "wrong-schema"
    result = lint_intake(env)
    assert "schema_invalid" in result["errors"]


def test_lint_intake_duplicate_ac_ids():
    env = _compile()
    # Force duplicate IDs
    env["acceptance_criteria"][1]["id"] = env["acceptance_criteria"][0]["id"]
    result = lint_intake(env)
    assert any("duplicate_ac_id" in e for e in result["errors"])


def test_lint_intake_missing_impact_category():
    env = _compile()
    del env["impact_map"]["code"]
    result = lint_intake(env)
    assert "impact_category_missing:code" in result["errors"]


def test_lint_intake_source_snapshot_revision_missing():
    env = _compile()
    env["source_snapshot"]["revision"] = ""
    result = lint_intake(env)
    assert "source_snapshot_revision_missing" in result["errors"]


def test_lint_intake_ac_done_without_evidence_is_warning():
    env = _compile()
    env["acceptance_criteria"][0]["status"] = "done"
    result = lint_intake(env)
    assert any("ac_done_without_evidence" in w for w in result["warnings"])


# ---------------------------------------------------------------------------
# freeze_intake
# ---------------------------------------------------------------------------

def test_freeze_intake_attaches_hash():
    env = _compile()
    frozen, h = freeze_intake(env)
    assert frozen["intake_hash"] == h
    assert len(h) == 64  # sha256 hex


def test_freeze_intake_is_deterministic():
    env1 = _compile()
    env2 = _compile()
    _, h1 = freeze_intake(env1)
    _, h2 = freeze_intake(env2)
    assert h1 == h2  # identical inputs → identical hash


def test_freeze_intake_changes_on_content_change():
    env1 = _compile(objective="do thing A")
    env2 = _compile(objective="do thing B")
    _, h1 = freeze_intake(env1)
    _, h2 = freeze_intake(env2)
    assert h1 != h2


def test_freeze_intake_is_idempotent():
    """Double-freezing must produce the same hash."""
    env = _compile()
    frozen1, h1 = freeze_intake(env)
    frozen2, h2 = freeze_intake(frozen1)
    assert h1 == h2


# ---------------------------------------------------------------------------
# make_source_snapshot
# ---------------------------------------------------------------------------

def test_make_source_snapshot_fields():
    snap = make_source_snapshot(
        provider="github",
        repo="owner/repo",
        item_id="42",
        revision="2024-12-01T10:00:00Z",
        snapshot_hash="deadbeef",
    )
    assert snap["provider"] == "github"
    assert snap["revision"] == "2024-12-01T10:00:00Z"
    assert snap["snapshot_hash"] == "deadbeef"
    assert "observed_at" in snap  # auto-filled


# ---------------------------------------------------------------------------
# Verdict constants exported
# ---------------------------------------------------------------------------

def test_verdict_constants_are_strings():
    for v in [VERDICT_COMPLETE, VERDICT_BLOCKED, VERDICT_AWAITING_DECISION,
              VERDICT_STALE_SOURCE, VERDICT_LEASE_LOST]:
        assert isinstance(v, str)

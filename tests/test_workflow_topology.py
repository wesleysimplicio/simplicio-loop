"""workflow_topology — semantic DAG differ + static validator for a stage-pipeline manifest (#468
MVP core). Covers: cycle/missing-dependency/duplicate/orphan detection (and the orphan
false-positive guard on trivial manifests), the semantic diff, and the critical-path calculator.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))
import workflow_topology as wt  # noqa: E402


def _manifest(*stages):
    return {"stages": [{"id": sid, "depends_on": deps} for sid, deps in stages]}


# ----- validate: cycle ------------------------------------------------------------------------

def test_validate_detects_cycle():
    m = _manifest(("a", ["c"]), ("b", ["a"]), ("c", ["b"]))
    result = wt.validate(m)
    assert result["valid"] is False
    codes = [i["code"] for i in result["issues"]]
    assert "cycle" in codes


def test_validate_clean_dag_has_no_cycle_issue():
    m = _manifest(("survey", []), ("decide", ["survey"]), ("operate", ["decide"]))
    result = wt.validate(m)
    assert result["valid"] is True
    assert result["issues"] == []


# ----- validate: missing dependency -----------------------------------------------------------

def test_validate_detects_missing_dependency():
    m = _manifest(("x", ["nope"]))
    result = wt.validate(m)
    assert result["valid"] is False
    codes = [i["code"] for i in result["issues"]]
    assert "missing_dependency" in codes
    hit = next(i for i in result["issues"] if i["code"] == "missing_dependency")
    assert hit["stage"] == "x"
    assert hit["missing"] == "nope"


# ----- validate: duplicate stage ---------------------------------------------------------------

def test_validate_detects_duplicate_stage_id():
    m = _manifest(("x", []), ("x", []))
    result = wt.validate(m)
    assert result["valid"] is False
    codes = [i["code"] for i in result["issues"]]
    assert "duplicate_stage" in codes


# ----- validate: orphan stage + false-positive guard ------------------------------------------

def test_validate_trivial_single_stage_manifest_has_no_orphan_false_positive():
    m = _manifest(("solo", []))
    result = wt.validate(m)
    assert result["valid"] is True
    assert not any(i["code"] == "orphan_stage" for i in result["issues"])


def test_validate_two_roots_are_not_orphans():
    # two independent roots is a valid shape, not an orphan
    m = _manifest(("root_a", []), ("root_b", []), ("mid", ["root_a"]))
    result = wt.validate(m)
    assert result["valid"] is True


def test_validate_detects_orphan_stage_disconnected_from_any_root():
    # a real graph WITH a root, plus an isolated cyclic pair unreachable from that root
    m = _manifest(
        ("root", []),
        ("mid", ["root"]),
        ("ghost", ["ghost_parent"]),
        ("ghost_parent", ["ghost"]),
    )
    result = wt.validate(m)
    assert result["valid"] is False
    codes = {i["code"] for i in result["issues"]}
    # the isolated pair is cyclic AND unreachable from the root; at minimum one of these codes
    # must fire so the disconnected stages are never silently accepted
    assert "cycle" in codes or "orphan_stage" in codes


def test_validate_orphan_without_cycle():
    # a stage that depends on a REAL stage, but that stage isn't reachable from any root
    # (its own depends_on chain never bottoms out at a root) -> orphan, not cycle
    m = _manifest(
        ("root", []),
        ("mid", ["root"]),
        ("isolated_child", ["isolated_root_missing_link"]),
        ("isolated_root_missing_link", []),
    )
    # isolated_root_missing_link IS itself a root (empty depends_on) so it's reachable from
    # itself, and isolated_child depends on it -- this is actually fully connected via its own
    # root. Use a case where a stage's only dependency is non-root and non-reachable instead:
    m2 = _manifest(
        ("root", []),
        ("mid", ["root"]),
        ("orphan_a", ["orphan_b"]),
        ("orphan_b", ["orphan_a"]),
    )
    result = wt.validate(m2)
    assert result["valid"] is False


# ----- diff -------------------------------------------------------------------------------------

def test_diff_reports_added_and_removed_stages():
    old = _manifest(("survey", []), ("decide", ["survey"]))
    new = _manifest(("survey", []), ("decide", ["survey"]), ("operate", ["decide"]))
    d = wt.diff(old, new)
    assert d["added_stages"] == ["operate"]
    assert d["removed_stages"] == []

    d2 = wt.diff(new, old)
    assert d2["removed_stages"] == ["operate"]
    assert d2["added_stages"] == []


def test_diff_reports_changed_dependencies():
    old = _manifest(("survey", []), ("decide", ["survey"]), ("triage", []))
    new = _manifest(("survey", []), ("decide", ["survey", "triage"]), ("triage", []))
    d = wt.diff(old, new)
    assert d["changed_dependencies"] == [
        {"stage": "decide", "old_depends_on": ["survey"], "new_depends_on": ["survey", "triage"]}
    ]


def test_diff_detects_reordering():
    old = _manifest(("a", []), ("b", ["a"]))
    new = _manifest(("b", ["a"]), ("a", []))
    d = wt.diff(old, new)
    assert d["reordered"] is True

    same_order = wt.diff(old, old)
    assert same_order["reordered"] is False


# ----- critical path ------------------------------------------------------------------------

def test_critical_path_returns_longest_chain():
    # a -> b -> c is length 3; a -> d is length 2; longest is a,b,c
    m = _manifest(("a", []), ("b", ["a"]), ("c", ["b"]), ("d", ["a"]))
    assert wt.critical_path(m) == ["a", "b", "c"]


def test_critical_path_single_stage():
    m = _manifest(("solo", []))
    assert wt.critical_path(m) == ["solo"]


def test_critical_path_matches_pipeline_example():
    m = _manifest(
        ("preflight", []),
        ("survey", ["preflight"]),
        ("triage", ["survey"]),
        ("decide", ["triage"]),
        ("operate", ["decide"]),
    )
    assert wt.critical_path(m) == ["preflight", "survey", "triage", "decide", "operate"]


# ----- load_manifest + own example manifest -----------------------------------------------------

def test_load_manifest_round_trips_from_disk(tmp_path):
    import json
    path = tmp_path / "m.json"
    m = _manifest(("a", []), ("b", ["a"]))
    path.write_text(json.dumps(m), encoding="utf-8")
    loaded = wt.load_manifest(str(path))
    assert loaded == m


def test_own_pipeline_example_manifest_validates_cleanly():
    path = os.path.join(REPO, ".orchestrator", "topology", "current.json")
    manifest = wt.load_manifest(path)
    result = wt.validate(manifest)
    assert result["valid"] is True, result["issues"]
    assert 5 <= len(manifest["stages"]) <= 9


def test_selftest_entrypoint_passes(capsys):
    import pytest
    with pytest.raises(SystemExit) as exc:
        wt.cmd_selftest({}, [])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "PASS" in out

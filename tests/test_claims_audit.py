"""Unit tests for the claims_audit.py checks that were fixed by #70/#72/#76: authoritative
extension-point counting, bidirectional bundle-parity, and skill-count consistency.

These build a small throwaway fixture repo and monkeypatch claims_audit's module-level path
globals to point at it, so the checks run against a KNOWN state instead of the real repo (which
is already exercised end-to-end by `scripts/check.py` in CI).
"""
import json
import os
import shutil
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))
import claims_audit  # noqa: E402


def _write(path, content=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _patched(fixture_root):
    """Context-manager-free monkeypatch: returns a restore() callable."""
    saved = {
        "REPO": claims_audit.REPO,
        "EXTENSION_POINTS_DOC": claims_audit.EXTENSION_POINTS_DOC,
    }
    claims_audit.REPO = fixture_root
    claims_audit.EXTENSION_POINTS_DOC = os.path.join(
        fixture_root, ".claude", "skills", "simplicio-tasks", "references", "extension-points.md")

    def restore():
        claims_audit.REPO = saved["REPO"]
        claims_audit.EXTENSION_POINTS_DOC = saved["EXTENSION_POINTS_DOC"]
    return restore


EXT_TABLE = """# Extension points — the 2 named binding points

| Extension point | What it does | LLM fallback (always available) |
|---|---|---|
| `orient` | a | b |
| `execute` | a | b |
"""


def test_extension_count_matches_actual_table():
    with tempfile.TemporaryDirectory() as tmp:
        _write(os.path.join(tmp, "README.md"), "badge: 2 extension points")
        _write(os.path.join(tmp, "CLAUDE.md"), "2 named binding points")
        _write(os.path.join(tmp, ".claude", "skills", "simplicio-tasks", "references",
                             "extension-points.md"), EXT_TABLE)
        restore = _patched(tmp)
        try:
            ok, detail = claims_audit.check_extension_count()
            assert ok, detail
            assert "2" in detail
        finally:
            restore()


def test_extension_count_flags_claim_disagreeing_with_table():
    # #72: docs agreeing with EACH OTHER is not enough — a uniformly wrong count must fail now
    # that the check compares against the actual table row count.
    with tempfile.TemporaryDirectory() as tmp:
        _write(os.path.join(tmp, "README.md"), "badge: 5 extension points")
        _write(os.path.join(tmp, "CLAUDE.md"), "5 named binding points")
        _write(os.path.join(tmp, ".claude", "skills", "simplicio-tasks", "references",
                             "extension-points.md"), EXT_TABLE)  # table actually has 2 rows
        restore = _patched(tmp)
        try:
            ok, detail = claims_audit.check_extension_count()
            assert not ok, "a claim (5) disagreeing with the real table (2) must fail"
            assert "2" in detail and "5" in detail
        finally:
            restore()


def test_extension_count_catches_named_extension_points_phrasing():
    # The phrasing that escaped the original regex (#72 evidence: AGENTS.md "48 named extension
    # points").
    with tempfile.TemporaryDirectory() as tmp:
        _write(os.path.join(tmp, "AGENTS.md"), "This repo has 5 named extension points.")
        _write(os.path.join(tmp, ".claude", "skills", "simplicio-tasks", "references",
                             "extension-points.md"), EXT_TABLE)  # table has 2 rows
        restore = _patched(tmp)
        try:
            ok, detail = claims_audit.check_extension_count()
            assert not ok, "the 'named extension points' phrasing must now be audited: %s" % detail
        finally:
            restore()


def _skills_tree(root, names):
    for n in names:
        _write(os.path.join(root, ".claude", "skills", n, "SKILL.md"), "---\n---\nbody\n")


def test_skill_count_matches_tree():
    with tempfile.TemporaryDirectory() as tmp:
        _skills_tree(tmp, ["a", "b", "c"])
        _write(os.path.join(tmp, "README.md"), "badge: skills-3-blue")
        restore = _patched(tmp)
        try:
            ok, detail = claims_audit.check_skill_count()
            assert ok, detail
        finally:
            restore()


def test_skill_count_flags_stale_claim():
    # #76: "11 skills" badge vs an actual 6-directory tree — the drift class this check exists for.
    with tempfile.TemporaryDirectory() as tmp:
        _skills_tree(tmp, ["a", "b", "c"])
        _write(os.path.join(tmp, "README.md"), "badge: skills-11-blue")
        restore = _patched(tmp)
        try:
            ok, detail = claims_audit.check_skill_count()
            assert not ok, "a stale skill-count claim (11) vs the real tree (3) must fail: %s" % detail
        finally:
            restore()


def _bundle_pairs_fixture(tmp):
    src_scripts = os.path.join(tmp, "scripts")
    bun_scripts = os.path.join(tmp, "simplicio_loop", "_bundle", "scripts")
    src_tests = os.path.join(tmp, "tests")
    bun_tests = os.path.join(tmp, "simplicio_loop", "_bundle", "tests")
    for d in (os.path.join(tmp, ".claude", "skills"),
              os.path.join(tmp, "simplicio_loop", "_bundle", "skills"),
              os.path.join(tmp, "hooks"),
              os.path.join(tmp, "simplicio_loop", "_bundle", "hooks")):
        os.makedirs(d, exist_ok=True)
    _write(os.path.join(src_scripts, "cross_agent_wiki.py"), "x = 1\n")
    _write(os.path.join(bun_scripts, "cross_agent_wiki.py"), "x = 1\n")
    _write(os.path.join(src_tests, "test_loop_e2e.py"), "y = 1\n")
    _write(os.path.join(bun_tests, "test_loop_e2e.py"), "y = 1\n")
    return src_scripts, bun_scripts, src_tests, bun_tests


def test_bundle_parity_passes_when_mirrored():
    with tempfile.TemporaryDirectory() as tmp:
        _bundle_pairs_fixture(tmp)
        restore = _patched(tmp)
        try:
            ok, detail = claims_audit.check_bundle_parity()
            assert ok, detail
        finally:
            restore()


def test_bundle_parity_catches_orphan_in_bundle():
    # #70 repro (from the issue): rename/delete a source script -> the OLD copy is left behind in
    # `_bundle/` and previously passed the forward-only check while still shipping in the wheel.
    with tempfile.TemporaryDirectory() as tmp:
        src_scripts, bun_scripts, _, _ = _bundle_pairs_fixture(tmp)
        os.remove(os.path.join(src_scripts, "cross_agent_wiki.py"))  # simulate the rename/delete
        restore = _patched(tmp)
        try:
            ok, detail = claims_audit.check_bundle_parity()
            assert not ok, "an orphan file left in _bundle/ with no source must be caught"
            assert "orphan" in detail
        finally:
            restore()


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _selfrun import run_module
    run_module(globals(), "test_claims_audit")

"""Repository governance gates (#294 P2 acceptance tests, step 2 "Corrigir novos commits").

These are pure-logic tests against scripts/repository_budget.py — no subprocess, no git
mutation, no network. They lock in the two P2 acceptance criteria the issue names
explicitly, plus the pre-existing per-file cap:

  - "mídia em caminho proibido bloqueia"  -> a large media file committed to a forbidden
    path (video/out/, rust/target/, node_modules/, dist/, build/) FAILS regardless of LFS.
  - "asset LFS permitido passa"           -> a large-media suffix (.mp4/.zip/...) is allowed
    ONLY when .gitattributes routes it to LFS (it becomes a small pointer, not a raw blob).
  - "blob acima do limite bloqueia"       -> any tracked file over MAX_SINGLE_FILE_BYTES that
    is not grandfathered by the committed baseline FAILS.

The gate is read-only by construction (it only reads `git ls-files` + the stats of files
already on disk + .gitattributes); these tests never call the git path — they feed
synthetic (rel, size) tuples straight into the pure classification helpers.
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import repository_budget as rb  # noqa: E402


# -- forbidden-path rule (must block even if LFS-routable) ------------------

@pytest.mark.parametrize("rel", [
    "video/out/demo.mp4",
    "video/out/render-001.webm",
    "rust/target/debug/simplicio-loop",
    "node_modules/.cache/big.bin",
    "dist/simplicio-loop-1.0.0.tar.gz",
    "build/artifact.zip",
])
def test_forbidden_prefix_is_always_blocked(rel):
    # A forbidden PREFIX is blocked whether the blob is raw or would be LFS-routable.
    assert rb._new_forbidden_raw_media([(rel, 50 * 1024 * 1024)]) != []


def test_forbidden_prefix_blocks_lfs_suffixed_file():
    # Even though `*.mp4` is globally LFS-routed, video/out/ is a forbidden PATH and
    # must never be committed at all.
    flagged = rb._new_forbidden_raw_media([("video/out/demo.mp4", 10_000_000)])
    assert len(flagged) == 1
    assert flagged[0][2] == "forbidden-path"


# -- LFS-exemption rule (large-media suffix only allowed when LFS-routed) ----

def test_large_media_suffix_without_lfs_is_blocked():
    # `evil.mp4` at repo root is NOT covered by any LFS filter -> raw blob -> blocked.
    flagged = rb._new_forbidden_raw_media([("evil.mp4", 8 * 1024 * 1024)])
    assert len(flagged) == 1
    assert flagged[0][2] == "forbidden-raw-media"


def test_lfs_routed_media_suffix_is_exempt():
    # `assets/_lfs/demo.mp4` matches the `*.mp4 filter=lfs` rule declared in .gitattributes,
    # so it is a small pointer, not a raw blob -> allowed (AC "asset LFS permitido passa").
    assert rb._new_forbidden_raw_media([("assets/_lfs/demo.mp4", 200 * 1024 * 1024)]) == []
    assert rb._is_lfs_exempt("assets/_lfs/demo.mp4") is True


def test_lfs_routed_zip_suffix_is_exempt():
    assert rb._is_lfs_exempt("assets/_lfs/release.zip") is True
    assert rb._new_forbidden_raw_media([("assets/_lfs/release.zip", 999_999_999)]) == []


def test_plain_source_files_are_not_lfs_exempt():
    assert rb._is_lfs_exempt("scripts/check.py") is False
    assert rb._is_lfs_exempt("README.md") is False


# -- no false positives on the real tracked tree ----------------------------

def test_real_tracked_source_assets_are_not_flagged():
    real_entries = [
        ("README.md", 4096),
        ("scripts/check.py", 12106),
        ("assets/simplicio-loop-logo.png", 4924 * 1024),  # grandfathered hero image
        ("docs/REPO_SIZE_REPORT.md", 2048),
    ]
    assert rb._new_forbidden_raw_media(real_entries) == []


# -- per-file size cap (pre-existing gate, still enforced) ------------------

def test_new_oversized_file_without_baseline_is_flagged():
    # No baseline -> nothing is grandfathered; a 3 MiB file is plainly over the 2 MiB cap.
    entries = [("big_new_asset.bin", 3 * 1024 * 1024)]
    flagged = rb._new_oversized_files(entries, baseline=None)
    assert len(flagged) == 1
    assert flagged[0][0] == "big_new_asset.bin"


def test_grandfathered_oversized_file_is_not_flagged():
    baseline = {
        "known_oversized_files": {"assets/hero.png": 2_400_000},
        "total_bytes": 18_000_000,
        "threshold_growth": rb.THRESHOLD_GROWTH,
        "max_single_file_bytes": rb.MAX_SINGLE_FILE_BYTES,
    }
    # Same size as grandfathered -> within tolerance -> not flagged.
    entries = [("assets/hero.png", 2_400_000)]
    assert rb._new_oversized_files(entries, baseline=baseline) == []


def test_grandfathered_file_grown_past_threshold_is_flagged():
    baseline = {
        "known_oversized_files": {"assets/hero.png": 2_400_000},
        "total_bytes": 18_000_000,
        "threshold_growth": rb.THRESHOLD_GROWTH,
        "max_single_file_bytes": rb.MAX_SINGLE_FILE_BYTES,
    }
    grown = int(2_400_000 * (1 + rb.THRESHOLD_GROWTH) + 1)  # just past +25%
    flagged = rb._new_oversized_files([("assets/hero.png", grown)], baseline=baseline)
    assert len(flagged) == 1


# -- gate is read-only ------------------------------------------------------

def test_gate_module_has_no_git_write_or_rewrite_calls():
    # The repository budget gate must be purely observational. It may call `git ls-files`
    # / `git count-objects` (read-only), but must NEVER invoke a history-rewrite tool or
    # any ref-mutating command.
    import re
    src = open(os.path.join(SCRIPTS, "repository_budget.py"), encoding="utf-8").read()
    forbidden = re.findall(
        r"subprocess\.\w+\(\s*\[[^]]*(filter-repo|filter-branch|bfg|git push|git branch -f|git update-ref)",
        src, re.I)
    assert not forbidden, "repository_budget.py must not contain rewrite/push calls: %r" % forbidden

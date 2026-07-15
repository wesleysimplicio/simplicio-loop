import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.release_rehearsal import run_rehearsal

REPO_ROOT = Path(__file__).resolve().parents[1]


# Real, slow, end-to-end: exports the tracked tree, bumps a scratch-only version, builds a real
# wheel, checksums/signs/SBOMs/provenance-statements it, and clean-room install-smokes it — no
# mocking, per the #292 mandate against fabricated supply-chain proof. Never touches this repo's
# real version files, never publishes anywhere.
def test_run_rehearsal_chains_every_local_link_end_to_end():
    result = run_rehearsal(REPO_ROOT, keep=False)

    assert result["scope"].startswith("local-rehearsal-only")
    assert result["steps"]["export"]["ok"] is True
    assert result["steps"]["version_bump"]["ok"] is True
    assert result["rehearsal_version"].count("+rehearsal") == 1
    assert result["steps"]["build"]["ok"] is True
    assert result["steps"]["checksums"]["ok"] is True
    assert result["steps"]["sbom"]["ok"] is True
    assert result["steps"]["provenance"]["ok"] is True
    assert result["steps"]["install_smoke"]["ok"] is True
    assert result["steps"]["install_smoke"]["observed_version"] == result["rehearsal_version"]
    # Signing is best-effort: whichever way it goes, the state reflects it truthfully instead of
    # silently upgrading a blocked signature to "signed".
    assert result["state"] in ("smoke-verified",)
    assert result["ok"] is True
    # Never mutates the real repo checkout.
    assert (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8").count(result["rehearsal_version"]) == 0


def test_run_rehearsal_fails_closed_on_invalid_explicit_version():
    result = run_rehearsal(REPO_ROOT, version="not-a-version", keep=False)

    assert result["ok"] is False
    assert result["reason_code"] == "version_bump_failed"


def test_run_rehearsal_require_signing_blocks_without_a_key():
    result = run_rehearsal(REPO_ROOT, require_signing=True, keep=False)

    # This environment has no configured gpg secret key (verified during development); requiring
    # signing must fail closed rather than silently proceed.
    if not result["steps"].get("sign", {}).get("ok"):
        assert result["ok"] is False
        assert result["reason_code"] == "signing_required_but_blocked"

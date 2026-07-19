#!/usr/bin/env python3
"""#458 item 2 — parity between contracts/stage-agents/v1/ (source of truth) and the
pip-bundle mirror simplicio_loop/_contracts/stage-agents/v1/.

These tests prove the new contract-parity gate (claims_audit check 14) actually detects
drift and passes when in sync. They do NOT depend on network or the full suite.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "scripts"))

import claims_audit  # noqa: E402
import sync_plugin  # noqa: E402

SRC = sync_plugin.SRC_CONTRACTS
DST = sync_plugin.DST_CONTRACTS


def _sync():
    """Regenerate the bundle mirror so the test starts from a known-good state."""
    if os.path.isdir(DST):
        import shutil
        shutil.rmtree(DST)
    sync_plugin.sync()


def test_contract_parity_passes_when_synced():
    _sync()
    ok, detail = claims_audit.check_contract_parity()
    assert ok, "expected contract-parity to pass after sync, got: %s" % detail


def test_contract_parity_catches_missing_in_bundle():
    _sync()
    # remove a file from the bundle mirror to simulate drift
    import shutil
    some_file = None
    for r, _dirs, names in os.walk(DST):
        if names:
            some_file = os.path.join(r, names[0])
            break
    assert some_file is not None, "bundle mirror unexpectedly empty"
    os.remove(some_file)
    ok, detail = claims_audit.check_contract_parity()
    assert not ok, "expected contract-parity to FAIL on missing-in-bundle, got ok"
    assert "missing in bundle" in detail or "orphan" in detail or "differs" in detail, detail


def test_check_contracts_helper_detects_orphan():
    _sync()
    # add an orphan file into the bundle that has no source counterpart
    orphan = os.path.join(DST, "_orphan_marker.json")
    with open(orphan, "w") as f:
        f.write("{}")
    drift = sync_plugin.check_contracts()
    assert any("orphan" in d for d in drift), "expected orphan detection, got: %s" % drift
    os.remove(orphan)

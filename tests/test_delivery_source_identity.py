import json

from simplicio_loop.delivery import build_delivery_receipt, source_fingerprint, validate_delivery_receipt


def test_source_fingerprint_is_order_independent_and_changes_on_observation_change():
    left = {"checks": {"green": True}, "reviews": {"approvals": 1, "open_threads": 0}}
    right = {"reviews": {"open_threads": 0, "approvals": 1}, "checks": {"green": True}}
    assert source_fingerprint(left) == source_fingerprint(right)
    changed = {"checks": {"green": False}, "reviews": {"approvals": 1, "open_threads": 0}}
    assert source_fingerprint(left) != source_fingerprint(changed)


def test_built_receipt_binds_external_observation_and_detects_stale_payload(tmp_path):
    (tmp_path / "manifest.json").write_text(json.dumps({"run_id": "r1"}), encoding="utf-8")
    payload = {"pr": {"url": "https://example/pr/1", "head_sha": "a", "base_sha": "b", "evidence": "source-query"}}
    receipt = build_delivery_receipt(str(tmp_path), "pr-open", "pr-open", "github", payload)
    assert receipt["source_fingerprint"] == source_fingerprint(payload)
    assert validate_delivery_receipt(receipt, target="pr-open")["ok"] is True
    stale = dict(receipt)
    stale["source_payload"] = {"pr": {"url": "https://example/pr/1", "head_sha": "changed", "base_sha": "b"}}
    result = validate_delivery_receipt(stale, target="pr-open")
    assert result["ok"] is False
    assert any(g["reason_code"] == "source_fingerprint_mismatch" for g in result["gates"])

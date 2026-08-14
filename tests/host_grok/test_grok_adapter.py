from adapters.grok.adapter import (
    capabilities,
    decide,
    detect,
    normalize_tool_calls,
    redact,
    verify_shipped_hooks,
)


def test_detects_grok_signals(tmp_path):
    found = detect({"GROK": "1"}, tmp_path)
    assert found["detected"] is True
    assert found["live_api"] is False
    live = detect({"GROK": "1", "SIMPLICIO_GROK_LIVE": "1"}, tmp_path)
    assert live["live_api"] is True


def test_no_native_hooks_and_no_default_live_api():
    assert verify_shipped_hooks()["claimed_native"] == []
    matrix = capabilities()
    assert matrix["native_interception"] is False
    assert matrix["default_live_api"] is False
    assert matrix["stores_credentials"] is False
    assert matrix["live_status"] == "UNVERIFIED"
    assert matrix["self_paced"] is True


def test_unknown_tool_does_not_fall_back_to_shell():
    bundle = normalize_tool_calls([
        {"id": "1", "name": "run_shell", "arguments": {"cmd": "rm -rf /"}},
        {"id": "2", "name": "simplicio_map", "arguments": {"repo": "."}},
    ])
    assert bundle["shell_fallback"] is False
    assert bundle["accepted"][0]["name"] == "simplicio_map"
    assert bundle["rejected"][0]["reason"] == "unknown_tool"
    blocked = decide({"stage": "PreToolUse", "tool_calls": [{"name": "bash"}]})
    assert blocked["decision"] == "block"


def test_parallel_and_invalid_calls():
    bundle = normalize_tool_calls([
        {"id": "a", "name": "simplicio_search", "arguments": {"q": "x"}},
        {"id": "b", "name": "simplicio_edit", "via_runtime": False},
    ])
    assert [item["id"] for item in bundle["accepted"]] == ["a"]
    assert bundle["rejected"][0]["reason"] == "mutation_requires_runtime"


def test_redacts_credentials_and_does_not_invent_tokens():
    assert "***" in redact("xai_key=sk-live-secret")
    decision = decide({"stage": "Stop"})
    assert decision["tokens"] is None
    assert decision["tokens_reason"] == "provider_tokens_absent"

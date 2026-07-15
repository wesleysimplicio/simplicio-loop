import json
import os
import shutil
import sys

import pytest

from simplicio_loop.model_registry import (
    ModelCapabilityRegistry,
    ModelRegistryError,
    REASON_CODES,
)


def _entry(**overrides):
    base = {
        "runtime": "local-devcli",
        "provider": "openai-compatible",
        "model_id": "local/q4",
        "aliases": [],
        "capabilities": ["coding", "patch", "tests"],
        "context_window": 8192,
        "os": [],
        "arch": [],
        "probe": {"kind": "stub", "target": ""},
    }
    base.update(overrides)
    return base


def test_registry_hash_is_stable_across_reload_and_reorder(tmp_path):
    entries = [
        _entry(runtime="claude", provider="anthropic", model_id="claude-sonnet-5", aliases=["sonnet"]),
        _entry(runtime="codex", provider="openai", model_id="gpt-5.4", aliases=["codex-default"]),
    ]
    path = tmp_path / "registry.json"
    path.write_text(json.dumps({"registry_version": "1", "entries": entries}), encoding="utf-8")
    reg1 = ModelCapabilityRegistry.load(path)
    reg2 = ModelCapabilityRegistry(list(reversed(entries)), registry_version="1")
    assert reg1.registry_hash == reg2.registry_hash
    # Mutating a declared field changes the hash.
    mutated = [dict(e) for e in entries]
    mutated[0]["capabilities"] = mutated[0]["capabilities"] + ["review"]
    reg3 = ModelCapabilityRegistry(mutated, registry_version="1")
    assert reg3.registry_hash != reg1.registry_hash


def test_registry_rejects_duplicate_entries_and_ambiguous_aliases():
    dup = [_entry(), _entry()]
    with pytest.raises(ModelRegistryError, match="duplicate"):
        ModelCapabilityRegistry(dup)
    ambiguous = [
        _entry(runtime="claude", provider="anthropic", model_id="claude-a", aliases=["shared"]),
        _entry(runtime="claude", provider="anthropic", model_id="claude-b", aliases=["shared"]),
    ]
    with pytest.raises(ModelRegistryError, match="ambiguous alias"):
        ModelCapabilityRegistry(ambiguous)


def test_registry_rejects_malformed_entries():
    with pytest.raises(ModelRegistryError, match="missing required"):
        ModelCapabilityRegistry([{"runtime": "claude"}])
    with pytest.raises(ModelRegistryError):
        ModelCapabilityRegistry([_entry(context_window=-1)])


def test_probe_binary_on_path_is_real_and_non_mutating():
    real_binary = "python" if shutil.which("python") else ("python3" if shutil.which("python3") else None)
    if real_binary is None:
        pytest.skip("no python binary discoverable on PATH in this environment")
    entry = _entry(probe={"kind": "binary_on_path", "target": real_binary})
    reg = ModelCapabilityRegistry([entry])
    result = reg.probe(reg.entries[0])
    assert result["status"] == "MEASURED"
    assert result["available"] is True

    missing = _entry(model_id="local/missing", probe={"kind": "binary_on_path", "target": "definitely-not-a-real-binary-xyz"})
    reg2 = ModelCapabilityRegistry([missing])
    result2 = reg2.probe(reg2.entries[0])
    assert result2["status"] == "MEASURED"
    assert result2["available"] is False


def test_probe_env_var_present():
    os.environ.pop("SIMPLICIO_TEST_PROBE_VAR", None)
    entry = _entry(probe={"kind": "env_var_present", "target": "SIMPLICIO_TEST_PROBE_VAR"})
    reg = ModelCapabilityRegistry([entry])
    result = reg.probe(reg.entries[0])
    assert result["status"] == "MEASURED"
    assert result["available"] is False

    os.environ["SIMPLICIO_TEST_PROBE_VAR"] = "1"
    try:
        result2 = reg.probe(reg.entries[0])
        assert result2["available"] is True
    finally:
        os.environ.pop("SIMPLICIO_TEST_PROBE_VAR", None)


def test_probe_stub_never_fabricates_success():
    entry = _entry(probe={"kind": "stub", "target": ""})
    reg = ModelCapabilityRegistry([entry])
    result = reg.probe(reg.entries[0])
    assert result["status"] == "UNVERIFIED"
    assert result["available"] is False


def test_probe_hook_is_pluggable_for_future_codex_claude_probes():
    entry = _entry(runtime="codex", provider="openai", model_id="gpt-5.4", probe={"kind": "codex_native", "target": "codex"})

    def fake_codex_probe(e):
        return {"status": "MEASURED", "available": True, "detail": "fake codex probe for test"}

    reg = ModelCapabilityRegistry([entry], probe_hooks={"codex": fake_codex_probe})
    result = reg.probe(reg.entries[0])
    assert result["status"] == "MEASURED"
    assert result["available"] is True

    reg_no_hook = ModelCapabilityRegistry([entry])
    result2 = reg_no_hook.probe(reg_no_hook.entries[0])
    assert result2["status"] == "UNVERIFIED"


def test_eligible_candidates_triggers_each_reason_code():
    entries = [
        _entry(runtime="claude", provider="anthropic", model_id="claude-sonnet-5",
               capabilities=["coding", "review"], context_window=200000,
               probe={"kind": "stub", "target": ""}),
        _entry(runtime="missing-cap", provider="p", model_id="m-missing-cap",
               capabilities=["review"]),
        _entry(runtime="tiny-context", provider="p", model_id="m-tiny",
               capabilities=["coding"], context_window=100),
        _entry(runtime="unavailable-binary", provider="p", model_id="m-unavail",
               capabilities=["coding"],
               probe={"kind": "binary_on_path", "target": "definitely-not-a-real-binary-xyz"}),
        _entry(runtime="no-auth", provider="p", model_id="m-noauth",
               capabilities=["coding"],
               probe={"kind": "env_var_present", "target": "SIMPLICIO_TEST_PROBE_ABSENT_VAR"}),
        _entry(runtime="wrong-device", provider="p", model_id="m-device",
               capabilities=["coding"], os=["linux"], arch=["arm64"]),
        _entry(runtime="denied-provider", provider="denied-co", model_id="m-denied",
               capabilities=["coding"]),
    ]
    os.environ.pop("SIMPLICIO_TEST_PROBE_ABSENT_VAR", None)
    reg = ModelCapabilityRegistry(entries)
    requirements = {
        "required_capabilities": ["coding"],
        "context_window_min": 4096,
        "os": "win32",
        "arch": "x86_64",
        "denied_providers": ["denied-co"],
        "require_probe_available": True,
    }
    result = reg.eligible_candidates(requirements)
    reasons = {item["model_id"]: item["reason_code"] for item in result["eliminated"]}
    assert reasons["m-missing-cap"] == "missing_capability"
    assert reasons["m-tiny"] == "context_limit"
    assert reasons["m-unavail"] == "runtime_unavailable"
    assert reasons["m-noauth"] == "auth_missing"
    assert reasons["m-device"] == "device_incompatible"
    assert reasons["m-denied"] == "policy_denied"
    assert set(reasons.values()).issubset(REASON_CODES)
    eligible_ids = {e["model_id"] for e in result["eligible"]}
    assert eligible_ids == {"claude-sonnet-5"}

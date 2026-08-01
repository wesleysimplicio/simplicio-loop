from __future__ import annotations

from simplicio_loop.conformance_cache import ConformanceCache


def test_cache_key_changes_when_semantic_inputs_change(tmp_path) -> None:
    cache = ConformanceCache(tmp_path / "conformance.json")
    provider = {"name": "rust", "build": "a", "binary_digest": "sha256:a"}
    key = cache.key(provider=provider, corpus_digest="sha256:corpus",
                   policy_digest="sha256:policy", schema="v1")
    receipt = {"passed": True, "receipt_hash": "sha256:receipt"}
    cache.put(key, receipt)
    assert cache.get(key) == receipt
    changed = cache.key(provider=provider, corpus_digest="sha256:changed",
                        policy_digest="sha256:policy", schema="v1")
    assert cache.get(changed) is None
    assert cache.invalidate(key) is True
    assert cache.get(key) is None


def test_malformed_or_failed_entries_are_cache_misses(tmp_path) -> None:
    path = tmp_path / "conformance.json"
    path.write_text('{"schema":"wrong","entries":{}}', encoding="utf-8")
    cache = ConformanceCache(path)
    assert cache.get("sha256:key") is None
    cache.put("sha256:key", {"passed": False})
    assert cache.get("sha256:key") is None

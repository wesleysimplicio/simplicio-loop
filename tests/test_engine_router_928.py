from __future__ import annotations

from simplicio_loop.engine_router import probe_optional_backend, route_backend


def test_missing_provider_is_an_explicit_python_fallback() -> None:
    probe = probe_optional_backend()
    assert probe["available"] is False
    receipt, observed = route_backend("auto")
    assert receipt.selected_engine == "python-loop"
    assert observed["probe_hash"].startswith("sha256:")


def test_probe_is_called_without_effect_and_conformance_selects_rust() -> None:
    calls: list[str] = []

    def probe():
        calls.append("probe")
        return {"available": True, "compatible": True, "conformance_passed": True,
                "protocol": "simplicio.loop-engine/v1",
                "operations": ["single", "batch", "prism", "recovery", "delivery"],
                "build": "rust-test"}

    receipt, observed = route_backend("auto", probe=probe, attempt_id="a")
    assert calls == ["probe"]
    assert receipt.selected_engine == "rust"
    assert observed["build"] == "rust-test"


def test_available_provider_with_invalid_abi_is_rejected() -> None:
    receipt, observed = route_backend(
        "auto", probe=lambda: {"available": True, "compatible": True,
                                "conformance_passed": True, "protocol": "wrong"}
    )
    assert observed["abi_valid"] is False
    assert observed["reason_code"] == "provider_abi_invalid"
    assert receipt.selected_engine == "python-loop"

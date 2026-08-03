"""Economy-parallel profile: token path + bounded parallel defaults."""
from __future__ import annotations

from simplicio_loop import economy_profile as ep
from simplicio_loop import strict_mode


def test_worker_bounds_scale_with_cpu():
    assert ep.recommend_operator_workers(1) == 2
    assert ep.recommend_operator_workers(4) == 3  # 75% of 4 = 3
    assert ep.recommend_operator_workers(32) == 12  # clamp


def test_economy_env_enables_fan_out_and_latest():
    env = ep.economy_parallel_env(runtime_operational=True, prism_slots=4, operator_workers=6)
    assert env["SIMPLICIO_LOOP_AUTO_FAN_OUT"] == "1"
    assert env["SIMPLICIO_OPERATOR_ALWAYS_LATEST"] == "1"
    assert env["SIMPLICIO_PRISM_SLOTS"] == "4"
    assert env["SIMPLICIO_LOOP_OPERATOR_WORKERS"] == "6"
    assert env["SIMPLICIO_FAST_MODE"] == "required"
    assert env["SIMPLICIO_REQUIRE_MCP"] == "1"
    assert env["SIMPLICIO_MCP_FORCE"] == "1"
    assert env["SIMPLICIO_EXECUTION_PROFILE"] == "auto"


def test_economy_env_without_runtime_disables_mcp_force():
    env = ep.economy_parallel_env(runtime_operational=False)
    assert env["SIMPLICIO_REQUIRE_MCP"] == "0"
    assert env["SIMPLICIO_MCP_FORCE"] == "0"
    assert env["SIMPLICIO_EXECUTION_PROFILE"] == "standalone"


def test_recommended_env_uses_economy_when_enabled(monkeypatch):
    monkeypatch.setenv("SIMPLICIO_ECONOMY_PARALLEL", "1")
    monkeypatch.setattr(
        strict_mode,
        "runtime_status",
        lambda env=None: {
            "operational": True,
            "present": True,
            "version": "Simplicio Runtime 3.5.7",
            "path": "x",
        },
    )
    monkeypatch.setattr(
        strict_mode,
        "fast_status",
        lambda env=None: {"operational": True, "present": True, "version": "2.0.23"},
    )
    rec = strict_mode.recommended_env({})
    assert rec["SIMPLICIO_LOOP_AUTO_FAN_OUT"] == "1"
    assert rec["SIMPLICIO_OPERATOR_ALWAYS_LATEST"] == "1"
    assert "SIMPLICIO_PRISM_SLOTS" in rec


def test_recommended_env_opt_out_minimal(monkeypatch):
    monkeypatch.setenv("SIMPLICIO_ECONOMY_PARALLEL", "0")
    monkeypatch.setattr(
        strict_mode,
        "runtime_status",
        lambda env=None: {"operational": False, "present": False, "version": ""},
    )
    monkeypatch.setattr(
        strict_mode,
        "fast_status",
        lambda env=None: {"operational": False, "present": False, "version": ""},
    )
    rec = strict_mode.recommended_env({})
    assert rec["SIMPLICIO_LOOP"] == "1"
    # minimal path still prefers auto fan-out + always-latest
    assert rec["SIMPLICIO_LOOP_AUTO_FAN_OUT"] == "1"

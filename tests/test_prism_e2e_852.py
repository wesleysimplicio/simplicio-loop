from __future__ import annotations

import asyncio
import json

import pytest

from bench.prism_benchmark_852 import SCHEMA, benchmark, main


def test_measured_e2e_proves_all_loads_and_explicit_runtime_fallback():
    receipt = asyncio.run(
        benchmark(
            repetitions=10,
            physical_cap=20,
            delay_seconds=0.00001,
            runtime_binary="definitely-missing-simplicio-runtime",
        )
    )
    assert receipt["schema"] == SCHEMA
    assert receipt["measurement"] == "measured"
    assert receipt["projection"] is False
    assert set(receipt["loads"]) == {"1x10", "4x10", "20x10"}
    for load in receipt["loads"].values():
        assert load["S0_serial"]["correct"] is True
        assert load["S1_legacy"]["correct"] is True
        assert load["S2_prism_python"]["correct"] is True
        assert load["S3_prism_runtime_rust"]["measured"] is False
        assert load["S3_prism_runtime_rust"]["null_reason"] == (
            "RUNTIME_BINARY_NOT_FOUND"
        )
        assert load["S4_python_fallback"]["correct"] is True
        for row in load["S2_prism_python"]["raw"]:
            assert row["lost_tasks"] == []
            assert row["duplicate_or_missing_invocations"] == []
            assert row["max_temporal_overlap"] <= load["physical_cap"]


def test_cli_writes_raw_receipt(tmp_path):
    output = tmp_path / "receipt.json"
    assert (
        main(
            [
                "--repetitions",
                "10",
                "--physical-cap",
                "4",
                "--delay-seconds",
                "0.00001",
                "--runtime-binary",
                "definitely-missing-simplicio-runtime",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    receipt = json.loads(output.read_text(encoding="utf-8"))
    assert receipt["loads"]["20x10"]["physical_cap"] == 4
    assert len(receipt["loads"]["20x10"]["S2_prism_python"]["raw"]) == 10


@pytest.mark.parametrize(
    "kwargs",
    [
        {"repetitions": 9},
        {"physical_cap": 0},
        {"physical_cap": 201},
        {"delay_seconds": 0},
    ],
)
def test_benchmark_rejects_invalid_methodology(kwargs):
    with pytest.raises(ValueError):
        asyncio.run(benchmark(**kwargs))

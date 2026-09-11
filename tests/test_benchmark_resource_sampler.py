import importlib.util
from pathlib import Path
import sys

import pytest

pytest.importorskip("psutil")
_spec = importlib.util.spec_from_file_location("resource_sampler", Path(__file__).parents[1] / "bench/measure_command.py")
_sampler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_sampler)


def test_real_cpu_memory_and_output(tmp_path):
    result = _sampler.measure([sys.executable, "-c", "import time; data=bytearray(8000000); sum(i*i for i in range(1000000)); time.sleep(.15); print('verified')"], tmp_path, tmp_path / "measurement", .02, 5)
    assert result["exit_code"] == 0
    assert result["reaped_children_cpu_seconds"] > 0
    assert result["peak_tree_rss_bytes_sampled"] > 8000000
    assert len(result["samples"]) > 1
    assert (tmp_path / "measurement/stdout.txt").read_text().strip() == "verified"


def test_failure_is_not_reported_as_success(tmp_path):
    result = _sampler.measure([sys.executable, "-c", "raise SystemExit(7)"], tmp_path, tmp_path / "failed", .02, 5)
    assert result["exit_code"] == 7


def test_previous_measurement_is_not_overwritten(tmp_path):
    with pytest.raises(FileExistsError):
        _sampler.measure([sys.executable, "-V"], tmp_path, tmp_path)

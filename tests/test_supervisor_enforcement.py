import importlib.util
import json
import os
import sys
import pathlib

import pytest

SCRIPT_PATH = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "supervisor_enforcement.py"
spec = importlib.util.spec_from_file_location("supervisor_enforcement", SCRIPT_PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


@pytest.fixture
def state_file(tmp_path, monkeypatch):
    path = str(tmp_path / "supervisor_enforcement.json")
    monkeypatch.setenv("SIMPLICIO_SUPERVISOR_STATE_FILE", path)
    monkeypatch.delenv("SIMPLICIO_SUPERVISOR_I_UNDERSTAND", raising=False)
    return path


class Opts:
    pass


def test_default_state_is_disabled(state_file):
    state = mod.load_state(state_file)
    assert state["enabled"] is False
    assert state["rollout"]["mode"] == "shadow"


def test_status_reports_disabled_when_no_state_file(state_file, capsys):
    opts = Opts()
    opts.json = True
    rc = mod.cmd_status(opts)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["enabled"] is False


def test_enable_without_guard_is_refused(state_file):
    opts = Opts()
    opts.i_understand = False
    rc = mod.cmd_enable(opts)
    assert rc == 2
    assert mod.load_state(state_file)["enabled"] is False


def test_enable_with_guard_flag_succeeds(state_file):
    opts = Opts()
    opts.i_understand = True
    rc = mod.cmd_enable(opts)
    assert rc == 0
    assert mod.load_state(state_file)["enabled"] is True


def test_enable_with_env_guard_succeeds(state_file, monkeypatch):
    monkeypatch.setenv("SIMPLICIO_SUPERVISOR_I_UNDERSTAND", "1")
    opts = Opts()
    opts.i_understand = False
    rc = mod.cmd_enable(opts)
    assert rc == 0
    assert mod.load_state(state_file)["enabled"] is True


def test_disable_always_allowed(state_file):
    enable_opts = Opts()
    enable_opts.i_understand = True
    mod.cmd_enable(enable_opts)
    rc = mod.cmd_disable(Opts())
    assert rc == 0
    assert mod.load_state(state_file)["enabled"] is False


def test_detect_flags_unsupervised_simplicio_process():
    processes = [
        "simplicio-mapper --survey",
        "python3 unrelated_tool.py",
    ]
    flagged = mod.detect_unsupervised(processes)
    assert len(flagged) == 1
    assert flagged[0]["argv0"] == "simplicio-mapper --survey".split()[0]


def test_detect_ignores_supervised_process_by_env_marker():
    processes = [{"argv": ["mapper", "--survey"], "env": {"SIMPLICIO_SUPERVISED": "1"}}]
    assert mod.detect_unsupervised(processes) == []


def test_detect_ignores_supervised_process_by_marker_file(tmp_path):
    marker = tmp_path / "marker"
    marker.write_text("x")
    processes = [{"argv": ["mapper", "--survey"], "marker_file": str(marker)}]
    assert mod.detect_unsupervised(processes) == []


def test_detect_never_kills_anything(monkeypatch):
    killed = []
    monkeypatch.setattr(os, "kill", lambda *a, **k: killed.append(a))
    processes = ["simplicio-mapper --survey"]
    mod.detect_unsupervised(processes)
    assert killed == []


def test_rollout_rejects_unknown_mode(state_file):
    opts = Opts()
    opts.mode = "yolo"
    opts.percent = 0
    opts.allow = []
    rc = mod.cmd_rollout(opts)
    assert rc == 2


@pytest.mark.parametrize("mode", ["shadow", "canary", "full"])
def test_rollout_accepts_known_modes(state_file, mode):
    opts = Opts()
    opts.mode = mode
    opts.percent = 10
    opts.allow = []
    rc = mod.cmd_rollout(opts)
    assert rc == 0
    assert mod.load_state(state_file)["rollout"]["mode"] == mode


def test_rollout_persists_canary_percent_and_allowlist(state_file):
    opts = Opts()
    opts.mode = "canary"
    opts.percent = 30
    opts.allow = ["ws-a", "ws-b"]
    mod.cmd_rollout(opts)
    state = mod.load_state(state_file)
    assert state["rollout"]["canary_percent"] == 30
    assert state["rollout"]["canary_allowlist"] == ["ws-a", "ws-b"]


def test_missing_state_file_falls_back_to_disabled_safely(state_file):
    assert not os.path.exists(state_file)
    state = mod.load_state(state_file)
    assert state["enabled"] is False
    processes = ["simplicio-dev-cli --execute"]
    assert len(mod.detect_unsupervised(processes)) == 1


def test_corrupt_state_file_falls_back_to_disabled(state_file):
    with open(state_file, "w", encoding="utf-8") as handle:
        handle.write("not json{{{")
    state = mod.load_state(state_file)
    assert state["enabled"] is False


def test_selftest_passes():
    assert mod.cmd_selftest(Opts()) == 0

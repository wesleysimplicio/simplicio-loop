from pathlib import Path

from adapters.kiro.adapter import capabilities, decide, detect, drift, verify_shipped_hooks

REPO = Path(__file__).resolve().parents[2]


def test_detects_kiro_steering(tmp_path: Path):
    (tmp_path / ".kiro" / "steering").mkdir(parents=True)
    (tmp_path / ".kiro" / "steering" / "guide.md").write_text("x\n", encoding="utf-8")
    assert detect({}, tmp_path)["detected"] is True
    assert detect({}, tmp_path / "empty")["detected"] is False


def test_no_invented_hooks_when_directory_empty():
    assert verify_shipped_hooks()["claimed_native"] == []
    matrix = capabilities(REPO)
    assert matrix["native_interception"] is False
    assert matrix["stages"]["PreToolUse"]["enforcement"] == "self_paced"
    assert matrix["spec_is_not_sot"] is True


def test_spec_does_not_close_on_ambiguous_receipt():
    blocked = decide({"close_spec": True, "runtime_receipt": "UNVERIFIED", "stage": "Stop"})
    assert blocked["decision"] == "block"
    assert blocked["spec_is_not_sot"] is True
    ok = decide({"close_spec": True, "runtime_receipt": "MEASURED", "stage": "Stop"})
    assert ok["decision"] == "continue"


def test_drift_repair_hint(tmp_path: Path):
    report = drift(tmp_path)
    assert report["status"] == "drift"
    assert report["repair"]
    (tmp_path / ".kiro" / "steering").mkdir(parents=True)
    (tmp_path / ".kiro" / "steering" / "a.md").write_text("ok\n", encoding="utf-8")
    assert drift(tmp_path)["status"] == "ok"

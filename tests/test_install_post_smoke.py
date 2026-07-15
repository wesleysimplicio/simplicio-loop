"""#293 §6: richer post-install smoke — real `--help`, doctor, preflight, and a real minimal
task run against the ACTUAL copies an install put on disk, not just "the file exists/parses".

Exercises `scripts/install_post_smoke.run_post_install_smoke()` against a target that went
through a real `install_executor.apply()` transactional install (in-process, not a subprocess),
so every assertion below is checking genuinely-installed, genuinely-executed script output.
"""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


sys.path.insert(0, str(SCRIPTS))
install_lib = _load("install_lib", "install_lib.py")
install_plan = _load("install_plan", "install_plan.py")
install_executor = _load("install_executor", "install_executor.py")
install_post_smoke = _load("install_post_smoke", "install_post_smoke.py")


def _installed_target(tmp_path):
    target = tmp_path / "project"
    target.mkdir()
    receipt = install_executor.apply("claude", target=str(target), is_global=False)
    assert receipt["status"] == "APPLIED"
    return target


def test_doctor_help_produces_real_usage_text(tmp_path):
    target = _installed_target(tmp_path)
    result = install_post_smoke._check_help(
        "doctor --help", str(target / "scripts" / "doctor.py"), str(target))
    assert result["ok"] is True, result
    assert result["returncode"] == 0
    assert any("usage" in line.lower() for line in result["stdout_tail"])


def test_preflight_help_produces_real_usage_text(tmp_path):
    target = _installed_target(tmp_path)
    result = install_post_smoke._check_help(
        "preflight --help", str(target / "scripts" / "preflight.py"), str(target))
    assert result["ok"] is True, result


def test_doctor_json_produces_real_component_checks(tmp_path):
    target = _installed_target(tmp_path)
    result = install_post_smoke._check_doctor_json(str(target))
    assert result["ok"] is True, result
    assert result["component_count"] > 0
    assert "REQUIRED" in result["tiers_seen"] or "RECOMMENDED" in result["tiers_seen"] \
        or "OPTIONAL" in result["tiers_seen"]


def test_task_anchor_selftest_is_a_real_minimal_task_run(tmp_path):
    target = _installed_target(tmp_path)
    result = install_post_smoke._check_task_anchor_selftest(str(target))
    assert result["ok"] is True, result
    assert result["checks_total"] and result["checks_total"] > 0
    assert result["checks_passed"] == result["checks_total"]


def test_full_post_install_smoke_suite_passes_on_a_real_install(tmp_path):
    target = _installed_target(tmp_path)
    receipt = install_post_smoke.run_post_install_smoke(str(target))
    assert receipt["schema"] == "simplicio.install-post-smoke/v1"
    assert receipt["ok"] is True, receipt
    assert len(receipt["checks"]) == 4
    assert all(c["ok"] for c in receipt["checks"]), receipt


def test_missing_script_fails_closed_not_silently(tmp_path):
    target = tmp_path / "empty_target"
    target.mkdir()
    receipt = install_post_smoke.run_post_install_smoke(str(target))
    assert receipt["ok"] is False
    assert any(c.get("reason") == "script_missing" for c in receipt["checks"])


if __name__ == "__main__":
    import os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _selfrun import run_module
    run_module(globals(), "test_install_post_smoke")

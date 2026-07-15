"""Unit tests for #293 mode `ci` version pinning ("instalação não interativa ... com versões
fixadas") — `install_lib.resolve_pinned_version()` / `ensure_operators(pin_versions=...)` /
`_pip_install(upgrade=...)`, plus the `--ci` CLI flag wiring in `install_lib.py main()`.

No real network/pip calls: every test here monkeypatches `_pip_install`/`subprocess.run`/
`importlib.metadata.version`, so nothing in this file can actually install or query PyPI.
"""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("install_lib", ROOT / "scripts" / "install_lib.py")
install_lib = importlib.util.module_from_spec(SPEC)
sys.modules["install_lib"] = install_lib
SPEC.loader.exec_module(install_lib)  # type: ignore[union-attr]


# ── resolve_pinned_version() ────────────────────────────────────────────────────────────────────

def test_resolve_pinned_version_prefers_already_installed(monkeypatch):
    import importlib.metadata as _real_metadata
    monkeypatch.setattr(_real_metadata, "version", lambda pkg: "1.2.3")
    assert install_lib.resolve_pinned_version("simplicio-cli") == "1.2.3"


def test_resolve_pinned_version_falls_back_to_pip_index_when_not_installed(monkeypatch):
    import importlib.metadata as _real_metadata

    def _raise(pkg):
        raise ModuleNotFoundError(pkg)

    monkeypatch.setattr(_real_metadata, "version", _raise)

    class _FakeResult:
        returncode = 0
        stdout = "simplicio-cli (9.9.9)\nAvailable versions: 9.9.9, 9.9.8\n"
        stderr = ""

    monkeypatch.setattr(install_lib.subprocess, "run", lambda *a, **k: _FakeResult())
    assert install_lib.resolve_pinned_version("simplicio-cli") == "9.9.9"


def test_resolve_pinned_version_returns_none_never_fabricated_when_offline(monkeypatch):
    import importlib.metadata as _real_metadata

    def _raise(pkg):
        raise ModuleNotFoundError(pkg)

    monkeypatch.setattr(_real_metadata, "version", _raise)

    def _boom(*a, **k):
        raise TimeoutError("no network in this sandbox")

    monkeypatch.setattr(install_lib.subprocess, "run", _boom)
    assert install_lib.resolve_pinned_version("simplicio-cli") is None


# ── ensure_operators(pin_versions=...) ──────────────────────────────────────────────────────────

def test_ensure_operators_pins_and_drops_upgrade_flag_when_resolved(monkeypatch):
    calls = []
    monkeypatch.setattr(install_lib, "resolve_pinned_version", lambda pkg, **k: "4.5.6")

    def _fake_pip_install(pkgs, **kwargs):
        calls.append((tuple(pkgs), kwargs))
        return True, "plain"

    monkeypatch.setattr(install_lib, "_pip_install", _fake_pip_install)
    monkeypatch.setattr(install_lib, "_link_operator_bins", lambda: None)
    monkeypatch.setattr(install_lib.shutil, "which", lambda name: "/usr/bin/" + name)
    install_lib.ensure_operators(skip_install=False, pin_versions=True)
    assert calls, "ensure_operators(pin_versions=True) must still call _pip_install"
    pkgs, kwargs = calls[0]
    assert pkgs == ("simplicio-cli==4.5.6",)
    assert kwargs.get("upgrade") is False


def test_ensure_operators_falls_back_to_floating_when_pin_unresolvable(monkeypatch):
    calls = []
    monkeypatch.setattr(install_lib, "resolve_pinned_version", lambda pkg, **k: None)

    def _fake_pip_install(pkgs, **kwargs):
        calls.append((tuple(pkgs), kwargs))
        return True, "plain"

    monkeypatch.setattr(install_lib, "_pip_install", _fake_pip_install)
    monkeypatch.setattr(install_lib, "_link_operator_bins", lambda: None)
    monkeypatch.setattr(install_lib.shutil, "which", lambda name: "/usr/bin/" + name)
    install_lib.ensure_operators(skip_install=False, pin_versions=True)
    pkgs, kwargs = calls[0]
    # Never a fabricated pin — falls back to the normal floating spec + upgrade=True.
    assert pkgs == (install_lib.OPERATOR_PACKAGE,)
    assert kwargs.get("upgrade", True) is True


def test_ensure_operators_default_is_floating_unpinned(monkeypatch):
    calls = []

    def _fake_pip_install(pkgs, **kwargs):
        calls.append((tuple(pkgs), kwargs))
        return True, "plain"

    monkeypatch.setattr(install_lib, "_pip_install", _fake_pip_install)
    monkeypatch.setattr(install_lib, "_link_operator_bins", lambda: None)
    monkeypatch.setattr(install_lib.shutil, "which", lambda name: "/usr/bin/" + name)
    install_lib.ensure_operators(skip_install=False)  # pin_versions defaults False
    pkgs, kwargs = calls[0]
    assert pkgs == (install_lib.OPERATOR_PACKAGE,)
    assert kwargs.get("upgrade", True) is True


# ── _pip_install(upgrade=...) builds the right pip argv ─────────────────────────────────────────

def test_pip_install_upgrade_false_omits_dash_u_flag(monkeypatch):
    captured = {}

    class _FakeCompleted:
        returncode = 0
        stderr = ""

    def _fake_run(argv, **kwargs):
        captured["argv"] = argv
        return _FakeCompleted()

    monkeypatch.setattr(install_lib.subprocess, "run", _fake_run)
    install_lib._pip_install(["simplicio-cli==1.0.0"], upgrade=False)
    assert "-U" not in captured["argv"]
    assert "simplicio-cli==1.0.0" in captured["argv"]


def test_pip_install_upgrade_true_keeps_dash_u_flag_default(monkeypatch):
    captured = {}

    class _FakeCompleted:
        returncode = 0
        stderr = ""

    def _fake_run(argv, **kwargs):
        captured["argv"] = argv
        return _FakeCompleted()

    monkeypatch.setattr(install_lib.subprocess, "run", _fake_run)
    install_lib._pip_install(["simplicio-cli"])
    assert "-U" in captured["argv"]


# ── `--ci` CLI flag wiring (install_lib.py main()) ──────────────────────────────────────────────
import json
import os
import subprocess

SCRIPTS = ROOT / "scripts"


def _safe_env(tmp_home):
    env = dict(os.environ)
    env["HOME"] = str(tmp_home)
    env["APPDATA"] = str(tmp_home / "AppData" / "Roaming")
    env["XDG_CONFIG_HOME"] = str(tmp_home / ".config")
    return env


def test_ci_flag_dry_run_plan_is_ci_mode_pinned_and_no_service(tmp_path):
    target = tmp_path / "project"
    target.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    r = subprocess.run(
        [sys.executable, str(SCRIPTS / "install_lib.py"), "claude", "--target", str(target),
         "--ci", "--dry-run"],
        capture_output=True, text=True, timeout=60, env=_safe_env(home),
        stdin=subprocess.DEVNULL,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    plan = json.loads(r.stdout)
    assert plan["mode"] == "ci"
    assert plan["version_pinning"] == "pinned"
    assert plan["status"] == "PLANNED"
    assert "service" not in plan["permissions_required"]
    assert "proxy" not in plan["permissions_required"]


def test_ci_flag_implies_no_service_same_as_minimal(tmp_path):
    target = tmp_path / "project"
    target.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    r = subprocess.run(
        [sys.executable, str(SCRIPTS / "install_lib.py"), "claude", "--target", str(target),
         "--ci", "--skip-operators"],
        capture_output=True, text=True, timeout=120, env=_safe_env(home),
        stdin=subprocess.DEVNULL,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "token monitor SKIPPED" in r.stdout
    assert not (home / ".config" / "systemd").exists()
    assert not (home / "AppData").exists()


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _selfrun import run_module
    run_module(globals(), "test_install_ci_pinning")

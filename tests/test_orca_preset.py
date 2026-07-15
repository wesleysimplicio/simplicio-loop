"""Contract and isolated integration coverage for the portable Orca preset."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PRESET = ROOT / "app" / "orca"


def test_manifest_declares_canonical_roles():
    manifest = json.loads((PRESET / "manifest.json").read_text())
    assert manifest["executors"] == ["simplicio_agent", "hermes"]
    assert manifest["reviewer"]["agent"] == "codex"
    assert manifest["conflicts"]["agent"] == "claude"
    assert manifest["final_reviewer"] == "codex-fresh-session"
    assert manifest["secrets_included"] is False


def test_readme_explains_validation_and_both_platforms():
    text = (PRESET / "README.md").read_text().lower()
    assert "windows" in text and "macos" in text
    assert "validation" in text


def test_macos_installer_configures_an_isolated_home(tmp_path: Path):
    bin_dir, home = tmp_path / "bin", tmp_path / "home"
    bin_dir.mkdir(); home.mkdir()
    log = tmp_path / "calls.log"
    for command in ("hermes", "simplicio_agent", "claude"):
        path = bin_dir / command
        path.write_text(f"#!/bin/sh\necho \"$0 $@\" >> '{log}'\n")
        path.chmod(0o755)
    codex = bin_dir / "codex"
    codex.write_text("#!/bin/sh\nexit 0\n"); codex.chmod(0o755)
    env = {**os.environ, "HOME": str(home), "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    subprocess.run(["bash", str(PRESET / "install.sh")], check=True, env=env, capture_output=True, text=True)
    assert "tencent/hy3:free" in log.read_text()
    assert "cron_mode approve" in log.read_text()
    assert 'gpt-5.6-terra' in (home / ".codex" / "config.toml").read_text()
    assert json.loads((home / ".claude" / "settings.json").read_text())["model"] == "sonnet"

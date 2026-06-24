#!/usr/bin/env python3
"""Cross-platform installer for the Simplicio token-economy services.

Registers the three always-on, auto-start services on whichever OS you run it:
  - capture proxy        (intercepts LLM calls, logs tokens saved)
  - token monitor :9090  (the Simplicio Token Monitor web dashboard)
  - menu-bar / tray app  (live tokens saved)

Backends:
  macOS    → launchd LaunchAgents      (setup_simplicio.sh also does this)
  Linux    → systemd --user units
  Windows  → Startup-folder launchers (pythonw, no console window)

Usage:
  python3 scripts/install_services.py install     # register + start all services
  python3 scripts/install_services.py uninstall   # stop + remove them
  python3 scripts/install_services.py status       # report
"""
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HOME = Path.home()
PY = sys.executable or shutil.which("python3") or "python3"
PROXY_PORT = os.environ.get("SIMPLICIO_PROXY_PORT", "8788")
MONITOR_PORT = os.environ.get("SIMPLICIO_MONITOR_PORT", "9090")
OPENAI_URL = os.environ.get("SIMPLICIO_PROXY_UPSTREAM", "https://api.deepseek.com/v1")


def engine_bin():
    """Resolve the capture-engine binary cross-platform (no full-home scan)."""
    exe = "headroom.exe" if os.name == "nt" else "headroom"
    found = shutil.which(exe)
    if found:
        return found
    cands = [
        HOME / "Projetos" / "ai" / "hermes-agent" / "venv" / ("Scripts" if os.name == "nt" else "bin") / exe,
        HOME / ".local" / "bin" / exe,
        HOME / "AppData" / "Roaming" / "Python" / "Scripts" / exe,
    ]
    for c in cands:
        if c.exists():
            return str(c)
    return exe  # last resort: rely on PATH at runtime


PROXY = [engine_bin(), "proxy", "--port", PROXY_PORT, "--openai-api-url", OPENAI_URL, "--host", "127.0.0.1"]
MONITOR = [PY, str(REPO / "hooks" / "simplicio_dashboard.py")]
TRAY = [PY, str(REPO / "app" / "simplicio_tray.py")]
SERVICES = {"proxy": PROXY, "token-monitor": MONITOR, "tray": TRAY}
ENVS = {"PORT": MONITOR_PORT, "SIMPLICIO_PROXY_PORT": PROXY_PORT, "SIMPLICIO_MONITOR_PORT": MONITOR_PORT}


# ── Linux: systemd --user ────────────────────────────────────────────────────
def _systemd_dir():
    d = Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config")) / "systemd" / "user"
    d.mkdir(parents=True, exist_ok=True)
    return d


def linux_install():
    d = _systemd_dir()
    for name, cmd in SERVICES.items():
        env_lines = "\n".join(f"Environment={k}={v}" for k, v in ENVS.items())
        unit = (
            "[Unit]\nDescription=Simplicio %s\nAfter=network.target\n\n"
            "[Service]\nExecStart=%s\nRestart=always\nRestartSec=3\n%s\n\n"
            "[Install]\nWantedBy=default.target\n"
            % (name, " ".join(_q(c) for c in cmd), env_lines)
        )
        (d / f"simplicio-{name}.service").write_text(unit)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    for name in SERVICES:
        subprocess.run(["systemctl", "--user", "enable", "--now", f"simplicio-{name}.service"], check=False)
    print("✅ systemd --user services installed:", ", ".join(f"simplicio-{n}" for n in SERVICES))


def linux_uninstall():
    d = _systemd_dir()
    for name in SERVICES:
        subprocess.run(["systemctl", "--user", "disable", "--now", f"simplicio-{name}.service"], check=False)
        (d / f"simplicio-{name}.service").unlink(missing_ok=True)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    print("✅ systemd --user services removed")


# ── Windows: Startup folder launchers (pythonw, no console) ───────────────────
def _startup_dir():
    return Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def windows_install():
    startup = _startup_dir()
    startup.mkdir(parents=True, exist_ok=True)
    pyw = PY.replace("python.exe", "pythonw.exe")
    for name, cmd in SERVICES.items():
        exe = pyw if cmd[0] == PY else cmd[0]
        args = " ".join(f'"{a}"' for a in cmd[1:])
        env_set = " & ".join(f"set {k}={v}" for k, v in ENVS.items())
        bat = startup / f"simplicio-{name}.bat"
        bat.write_text(f'@echo off\r\n{env_set}\r\nstart "" /b "{exe}" {args}\r\n')
    print("✅ Windows Startup launchers written to:", startup)
    for name, cmd in SERVICES.items():
        subprocess.Popen([startup / f"simplicio-{name}.bat"], shell=True)  # start now
    print("   (also launched now)")


def windows_uninstall():
    startup = _startup_dir()
    for name in SERVICES:
        (startup / f"simplicio-{name}.bat").unlink(missing_ok=True)
    print("✅ Windows Startup launchers removed (running instances stay until reboot/kill)")


# ── macOS: launchd is handled by setup_simplicio.sh ──────────────────────────
def macos_note():
    print("macOS uses launchd — run:  bash scripts/setup_simplicio.sh")
    print("(services: ai.simplicio.proxy / ai.simplicio.token-monitor / ai.simplicio.tray)")


def _q(s):
    return f'"{s}"' if " " in str(s) else str(s)


def _shell_profile():
    shell = os.environ.get("SHELL", "")
    if "zsh" in shell:
        return HOME / ".zshrc"
    if "bash" in shell:
        return HOME / ".bashrc"
    return HOME / ".profile"


def cmd_wire(on=True):
    """Always-capture: route OpenAI-compatible clients through the local capture proxy."""
    target = f"http://127.0.0.1:{PROXY_PORT}/v1"
    if os.name == "nt":
        if on:
            subprocess.run(["setx", "OPENAI_BASE_URL", target], check=False)
            print(f"✅ OPENAI_BASE_URL -> {target} (new terminals capture; reopen your tools)")
        else:
            subprocess.run(["setx", "OPENAI_BASE_URL", ""], check=False)
            print("✅ OPENAI_BASE_URL cleared")
        return
    prof = _shell_profile()
    txt = prof.read_text() if prof.exists() else ""
    import re
    txt = re.sub(r"(?m)^export OPENAI_BASE_URL=.*$", "", txt).rstrip()
    if on:
        txt += f"\nexport OPENAI_BASE_URL={target}\nexport SIMPLICIO_CAPTURE=on\n"
    prof.write_text(txt + "\n")
    print(f"✅ {prof}: OPENAI_BASE_URL {'->' if on else 'cleared;'} {target if on else ''}".rstrip())


def cmd_status():
    import socket
    print(f"⬡ Simplicio services · {platform.system()}")
    for port, what in ((PROXY_PORT, "capture proxy"), (MONITOR_PORT, "token monitor")):
        try:
            socket.create_connection(("127.0.0.1", int(port)), 0.5).close()
            print(f"  ● {what:14} :{port} live")
        except OSError:
            print(f"  ○ {what:14} :{port} offline")


def main():
    action = sys.argv[1] if len(sys.argv) > 1 else "status"
    osname = platform.system()
    if action == "status":
        return cmd_status()
    if action == "wire":
        return cmd_wire(True)
    if action == "unwire":
        return cmd_wire(False)
    if osname == "Darwin":
        return macos_note()
    if osname == "Linux":
        return linux_install() if action == "install" else linux_uninstall()
    if osname == "Windows":
        return windows_install() if action == "install" else windows_uninstall()
    print(f"unsupported OS: {osname}", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()

"""CLI for simplicio-loop: install the bundled skills + hooks into a runtime."""
from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import webbrowser
from pathlib import Path

from . import __version__

BUNDLE = Path(__file__).resolve().parent / "_bundle"
DASHBOARD = BUNDLE / "hooks" / "simplicio_dashboard.py"
# Cross-platform temp dir (Windows has no /tmp) — must match hooks/simplicio_dashboard.py.
PID_FILE = Path(tempfile.gettempdir()) / "simplicio-token-monitor.pid"
DEFAULT_DASH_PORT = int(os.environ.get("SIMPLICIO_MONITOR_PORT", "9090"))


def _gui_available() -> bool:
    """True only when opening a browser is safe + non-blocking. On headless Linux (no DISPLAY),
    webbrowser.open() may launch a text browser that inherits stdin and blocks forever — and a
    blocking wait() is NOT caught by try/except — so we must skip it there."""
    if sys.platform == "darwin" or os.name == "nt":
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _copy_tree(src: Path, dst: Path) -> int:
    """Copy every file under src into dst, preserving structure. Returns file count."""
    count = 0
    for item in src.rglob("*"):
        if item.is_file():
            out = dst / item.relative_to(src)
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, out)
            count += 1
    return count


def install(target: Path, globally: bool) -> int:
    base = (Path.home() / ".claude") if globally else (target / ".claude")
    skills_dst = base / "skills"
    hooks_dst = (base / "hooks") if globally else (target / "hooks")

    if not (BUNDLE / "skills").is_dir():
        print("error: bundled skills not found in the installed package.", flush=True)
        return 1

    n_skills = _copy_tree(BUNDLE / "skills", skills_dst)
    n_hooks = _copy_tree(BUNDLE / "hooks", hooks_dst)

    print(f"simplicio-loop {__version__} installed:")
    print(f"  skills -> {skills_dst}  ({n_skills} files)")
    print(f"  hooks  -> {hooks_dst}  ({n_hooks} files)")
    print("")
    print("Use it in your agent runtime (Claude Code, Cursor, ...):")
    print("  /simplicio-tasks finish all the open issues")
    return 0


def _port_up(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), 0.5):
            return True
    except OSError:
        return False


def _stop_dashboard() -> int:
    """Best-effort stop: kill the PID the dashboard recorded, then any stray server."""
    killed = False
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 15)
        killed = True
    except (OSError, ValueError):
        pass
    if os.name != "nt":  # pkill catches a server started outside this CLI
        try:
            subprocess.run(["pkill", "-f", "simplicio_dashboard.py"], check=False)
            killed = True
        except OSError:
            pass
    try:
        PID_FILE.unlink()
    except OSError:
        pass
    print("⬡ Token Monitor stopped." if killed else "⬡ dashboard was not running.")
    return 0


def dashboard(port: int, open_browser: bool, stop: bool) -> int:
    """Open (or stop) the Simplicio Token Monitor dashboard — works from anywhere after a pip
    install, no repo checkout needed. Starts the bundled server detached if it's not already up,
    then opens the browser. The dashboard is on-demand: nothing keeps it open, close it freely."""
    if stop:
        return _stop_dashboard()
    url = f"http://127.0.0.1:{port}"
    if not DASHBOARD.is_file():
        print("error: bundled dashboard not found in the installed package.", flush=True)
        return 1
    if not _port_up(port):
        logdir = Path.home() / ".simplicio" / "logs"
        logdir.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "PORT": str(port)}
        # Detach so the server outlives this CLI process (own session / no console window).
        kw = {"start_new_session": True} if os.name != "nt" else {
            "creationflags": 0x00000008 | 0x00000200}  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        with open(logdir / "token-monitor.log", "ab") as logf:
            try:
                subprocess.Popen([sys.executable or "python3", str(DASHBOARD)],
                                 env=env, stdout=logf, stderr=logf, stdin=subprocess.DEVNULL, **kw)
            except OSError as e:
                print(f"error: could not start the dashboard ({e}).", flush=True)
                return 1
        for _ in range(25):  # wait up to ~5s for the port to come up
            if _port_up(port):
                break
            time.sleep(0.2)
    if not _port_up(port):
        print(f"⬡ failed to start the dashboard — see ~/.simplicio/logs/token-monitor.log", flush=True)
        return 1
    print(f"⬡ Simplicio Token Monitor → {url}")
    if open_browser and _gui_available():
        try:
            webbrowser.open(url)
        except Exception:
            pass
    print("   stop it any time:  simplicio-loop dashboard --stop")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="simplicio-loop",
        description=(
            "Install the simplicio-loop super-plugin (6 AI-orchestration skills + "
            "loop/token-economy hooks) into a runtime's skills location, or open the "
            "Token Monitor dashboard."
        ),
    )
    parser.add_argument(
        "command", nargs="?", default="install", choices=["install", "dashboard"],
        help="action to run: install (default) or dashboard (open the Token Monitor)",
    )
    parser.add_argument(
        "--target", default=".",
        help="project directory to install into (default: current directory)",
    )
    parser.add_argument(
        "--global", dest="globally", action="store_true",
        help="install into ~/.claude instead of the project",
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_DASH_PORT,
        help="dashboard port (default: %(default)s)",
    )
    parser.add_argument(
        "--no-browser", dest="no_browser", action="store_true",
        help="dashboard: start the server but do not open a browser",
    )
    parser.add_argument(
        "--stop", action="store_true", help="dashboard: stop a running Token Monitor",
    )
    parser.add_argument(
        "-V", "--version", action="version", version=f"simplicio-loop {__version__}",
    )
    args = parser.parse_args(argv)
    if args.command == "dashboard":
        return dashboard(args.port, not args.no_browser, args.stop)
    return install(Path(args.target).resolve(), args.globally)


if __name__ == "__main__":
    raise SystemExit(main())

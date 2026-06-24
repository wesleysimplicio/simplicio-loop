#!/usr/bin/env python3
"""Simplicio Token Monitor — macOS menu-bar tray + widget for token savings.

Lives in the menu bar showing live tokens saved; the dropdown IS the widget
(lifetime + session detail, proxy status, open the full dashboard). It reads the
Simplicio capture proxy's proxy_savings.json — it generates no traffic of its own.

Run:   python3 app/simplicio_tray.py
Deps:  pip install --user rumps   (pulls pyobjc; macOS only)
"""
import json
import os
import socket
import webbrowser
from pathlib import Path

import rumps

HOME = os.path.expanduser("~")
REPO = Path(__file__).resolve().parents[1]
ICON = str(REPO / "assets" / "tray-icon.png")
SAVINGS_CANDIDATES = [
    Path(HOME) / ".simplicio" / "proxy_savings.json",
    Path(HOME) / ".headroom" / "proxy_savings.json",
]
PROXY_PORT = int(os.environ.get("SIMPLICIO_PROXY_PORT", os.environ.get("HEADROOM_PORT", "8788")))
MONITOR_PORT = os.environ.get("SIMPLICIO_MONITOR_PORT", "9090")
DASH_URL = f"http://127.0.0.1:{MONITOR_PORT}"


def _read_savings():
    for p in SAVINGS_CANDIDATES:
        if p.exists():
            try:
                return json.loads(p.read_text(errors="replace"))
            except (ValueError, OSError):
                pass
    return {}


def _port_up(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def _compact(n):
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


class TrayApp(rumps.App):
    def __init__(self):
        icon = ICON if os.path.exists(ICON) else None
        super().__init__("—", icon=icon, template=False, quit_button="Quit Simplicio Monitor")
        self.m_saved = rumps.MenuItem("Tokens saved: —")
        self.m_usd = rumps.MenuItem("$ saved: —")
        self.m_pct = rumps.MenuItem("Reduction: —")
        self.m_req = rumps.MenuItem("Requests: —")
        self.m_sess = rumps.MenuItem("This session: —")
        self.m_proxy = rumps.MenuItem("Capture proxy: —")
        self.menu = [
            self.m_saved, self.m_usd, self.m_pct, self.m_req,
            None,
            self.m_sess, self.m_proxy,
            None,
            rumps.MenuItem("Open Token Monitor…", callback=self._open_dash),
            rumps.MenuItem("Refresh now", callback=lambda _: self.update()),
        ]
        self._timer = rumps.Timer(self._tick, 4)
        self._timer.start()
        self.update()

    def _tick(self, _):
        self.update()

    def _open_dash(self, _):
        webbrowser.open(DASH_URL)

    def update(self):
        d = _read_savings()
        life = d.get("lifetime", {}) if isinstance(d, dict) else {}
        sess = d.get("display_session", {}) if isinstance(d, dict) else {}

        saved = int(life.get("tokens_saved", 0) or 0)
        after = int(life.get("total_input_tokens", 0) or 0)
        before = after + saved
        pct = round(saved / before * 100, 1) if before else 0.0
        usd = float(life.get("compression_savings_usd", 0) or 0)
        req = int(life.get("requests", 0) or 0)
        up = _port_up(PROXY_PORT)

        self.title = f" {_compact(saved)}" if up else f" {_compact(saved)} ○"
        self.m_saved.title = f"Tokens saved: {saved:,}"
        self.m_usd.title = f"$ saved: ${usd:.3f}"
        self.m_pct.title = f"Reduction: {pct}%"
        self.m_req.title = f"Requests: {req:,}"
        ss = int(sess.get("tokens_saved", 0) or 0)
        sp = sess.get("savings_percent", 0)
        self.m_sess.title = f"This session: {ss:,} ({sp}%)"
        self.m_proxy.title = f"Capture proxy: {'● live :' + str(PROXY_PORT) if up else '○ offline'}"


if __name__ == "__main__":
    TrayApp().run()

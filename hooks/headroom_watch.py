#!/usr/bin/env python3
"""headroom-watch — token monitoring dashboard for simplicio-loop.

Usage:
    python3 hooks/headroom_watch.py status    # show proxy + savings status
    python3 hooks/headroom_watch.py start     # start headroom proxy
    python3 hooks/headroom_watch.py stop      # stop headroom proxy
"""
import json
import os
import subprocess
import sys

HOME = os.path.expanduser("~")
LOGS = os.path.join(HOME, ".hermes", "logs")


def log(msg):
    print(msg)


def run(cmd, timeout=10):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip(), r.returncode
    except Exception as e:
        return str(e), -1


def status():
    out, rc = run(["lsof", "-i", ":8787"])
    if "headroom" in out:
        log("✅ headroom proxy — RUNNING (port 8787)")
    else:
        log("❌ headroom proxy — NOT RUNNING")
    out2, _ = run(["headroom", "memory", "stats"])
    for line in out2.split("\n"):
        if "Total" in line or "Database" in line:
            log(f"  {line.strip()}")
    out3, _ = run(["headroom", "output-savings"])
    if "No output-savings data yet" in out3:
        log("  📊 Output savings: no data yet (seed with headroom learn)")
    else:
        log(f"  📊 Output savings: {out3[:80]}")
    # Savings ledger
    ledger = os.path.join(HOME, "projetos", "ai", "simplicio-loop",
                          ".simplicio", "ledger", "savings-events.jsonl")
    if os.path.isfile(ledger):
        total = sum(1 for _ in open(ledger))
        log(f"  💰 Savings ledger: {total} events")
    log(f"  🪵 Logs: {LOGS}/headroom.log")
    return 0 if "RUNNING" in out2 else 1


def start():
    log("Starting headroom proxy on port 8787...")
    log("  Use: headroom proxy --port 8787")
    log("  Then: launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.simplicio.headroom.plist")
    return 0


def stop():
    log("Stopping headroom proxy...")
    run(["launchctl", "bootout", f"gui/{os.getuid()}/ai.simplicio.headroom"])
    log("  Stopped.")
    return 0


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    dispatch = {"status": status, "start": start, "stop": stop}
    if cmd in dispatch:
        sys.exit(dispatch[cmd]())
    print(f"Usage: {sys.argv[0]} {{status|start|stop}}")
    sys.exit(1)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""headroom-dashboard — web dashboard + monitor for headroom token savings.

Usage:
    python3 hooks/headroom_dashboard.py          # start web server
    python3 hooks/headroom_dashboard.py --port 9090
"""
import http.server
import json
import os
import subprocess
import time
from pathlib import Path

HOME = os.path.expanduser("~")
LOG = Path(HOME) / ".headroom" / "logs" / "proxy.log"
HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Headroom Monitor — simplicio-loop</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, 'SF Mono', monospace; background: #0a0a0a; color: #e0e0e0; padding: 20px; }}
  h1 {{ color: #d4a574; font-size: 1.4em; margin-bottom: 16px; }}
  h1 small {{ color: #666; font-size: 0.6em; font-weight: normal; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px,1fr)); gap: 12px; margin-bottom: 20px; }}
  .card {{ background: #141414; border: 1px solid #222; border-radius: 8px; padding: 16px; }}
  .card .label {{ color: #888; font-size: 0.75em; text-transform: uppercase; letter-spacing: 0.5px; }}
  .card .value {{ color: #d4a574; font-size: 1.8em; font-weight: bold; margin-top: 4px; }}
  .card .value.green {{ color: #4ade80; }}
  .card .value.amber {{ color: #fbbf24; }}
  .card .value.red {{ color: #f87171; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.85em; }}
  th {{ text-align: left; color: #888; padding: 8px 12px; border-bottom: 1px solid #222; font-size: 0.75em; text-transform: uppercase; }}
  td {{ padding: 6px 12px; border-bottom: 1px solid #1a1a1a; }}
  td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .badge {{ display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 0.8em; }}
  .badge.ok {{ background: #166534; color: #86efac; }}
  .badge.warn {{ background: #713f12; color: #fde68a; }}
  .badge.fail {{ background: #7f1d1d; color: #fca5a5; }}
  .log {{ background: #0d0d0d; border: 1px solid #1a1a1a; border-radius: 6px; padding: 12px; font-size: 0.75em; max-height: 300px; overflow-y: auto; }}
  .log line {{ display: block; color: #666; }}
  .log line:nth-child(odd) {{ background: #0a0a0a; }}
  .refresh {{ color: #555; font-size: 0.75em; margin-top: 10px; }}
</style>
</head>
<body>
<h1>🧠 Headroom Monitor <small>simplicio-loop</small></h1>
<div class="grid" id="stats"></div>
<h2 style="color:#888;font-size:0.9em;margin-bottom:8px;">Recent Proxy Log</h2>
<div class="log" id="log"></div>
<p class="refresh" id="ts"></p>
<script>
async function refresh() {{
  try {{
    const r = await fetch('/api/status');
    const d = await r.json();
    document.getElementById('stats').innerHTML = `
      <div class="card"><div class="label">Proxy</div><div class="value ${d.proxy_running?'green':'red'}">${d.proxy_running?'RUNNING':'STOPPED'}</div></div>
      <div class="card"><div class="label">Port</div><div class="value">${d.port}</div></div>
      <div class="card"><div class="label">Uptime</div><div class="value">${d.uptime}</div></div>
      <div class="card"><div class="label">Requests</div><div class="value">${d.requests}</div></div>
      <div class="card"><div class="label">Tokens Before</div><div class="value">${d.tokens_before}</div></div>
      <div class="card"><div class="label">Tokens After</div><div class="value ${d.tokens_saved > 0 ? 'green' : ''}">${d.tokens_after}</div></div>
      <div class="card"><div class="label">Tokens Saved</div><div class="value green">${d.tokens_saved} (${d.savings_pct}%)</div></div>
      <div class="card"><div class="label">Cache Hit %</div><div class="value">${d.cache_hit_pct}%</div></div>
      <div class="card"><div class="label">Memories</div><div class="value amber">${d.memories}</div></div>
      <div class="card"><div class="label">Savings Ledger</div><div class="value">${d.ledger_events}</div></div>
    `;
    document.getElementById('log').innerHTML = d.log_lines.map(l => `<line>${l}</line>`).join('');
    document.getElementById('ts').textContent = `Last updated: ${d.timestamp}`;
  }} catch(e) {{ document.getElementById('stats').innerHTML = '<div class="card"><div class="label">Error</div><div class="value red">' + e.message + '</div></div>'; }}
}}
setInterval(refresh, 3000);
refresh();
</script>
</body>
</html>"""


def get_status():
    proxy_running = False
    port = "8788"
    uptime = "0s"
    requests = 0
    tok_before = 0
    tok_after = 0
    tok_saved = 0
    cache_hit = 0
    log_lines = []

    # Check proxy
    r = subprocess.run(["lsof", "-i", f":{port}"], capture_output=True, text=True, timeout=3)
    proxy_running = "LISTEN" in r.stdout
    if proxy_running:
        # Parse log for stats
        if LOG.exists():
            text = LOG.read_text(errors="replace")
            lines = text.strip().split("\n")[-50:]
            log_lines = lines
            for line in lines:
                if "PERF" in line:
                    requests += 1
                    # tok_before=XX tok_after=XX tok_saved=XX cache_hit_pct=XX
                    for part in line.split():
                        if part.startswith("tok_before="):
                            tok_before += int(part.split("=")[1])
                        elif part.startswith("tok_after="):
                            tok_after += int(part.split("=")[1])
                        elif part.startswith("cache_hit_pct="):
                            try:
                                cache_hit = float(part.split("=")[1])
                            except:
                                pass
            tok_saved = tok_before - tok_after
            # Check proxy process uptime
            r2 = subprocess.run(["ps", "-o", "etime=", "-p", str(48563)], capture_output=True, text=True, timeout=2)
            uptime = r2.stdout.strip() or "running"

    # Memories
    mr = subprocess.run(["headroom", "memory", "stats"], capture_output=True, text=True, timeout=5)
    mem = 0
    for hl in mr.stdout.split("\n"):
        if "Total Memories" in hl:
            try:
                mem = int(hl.split(":")[1].strip())
            except:
                pass

    # Savings ledger
    ledger = Path(HOME) / "projetos" / "ai" / "simplicio-loop" / ".simplicio" / "ledger" / "savings-events.jsonl"
    lc = sum(1 for _ in open(ledger)) if ledger.exists() else 0

    savings_pct = round((tok_saved / max(tok_before, 1)) * 100, 1)
    return {
        "proxy_running": proxy_running,
        "port": port,
        "uptime": uptime,
        "requests": requests,
        "tokens_before": tok_before,
        "tokens_after": tok_after,
        "tokens_saved": tok_saved,
        "savings_pct": savings_pct,
        "cache_hit_pct": cache_hit,
        "memories": mem,
        "ledger_events": lc,
        "log_lines": log_lines[-20:],
        "timestamp": time.strftime("%H:%M:%S"),
    }


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/status":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(get_status()).encode())
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(HTML.encode())

    def log_message(self, *a):
        pass


def main():
    port = int(os.environ.get("PORT", "9090"))
    srv = http.server.HTTPServer(("127.0.0.1", port), Handler)
    print(f"🌐 Headroom Monitor: http://127.0.0.1:{port}")
    print(f"   Refresh: 3s · API: /api/status")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.server_close()


if __name__ == "__main__":
    main()

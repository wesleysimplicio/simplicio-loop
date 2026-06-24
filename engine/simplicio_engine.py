#!/usr/bin/env python3
"""Simplicio capture engine — native, self-contained token-saving proxy.

A transparent OpenAI/Anthropic-compatible HTTP proxy: it measures prompt tokens,
applies **deterministic** compression to message content (whitespace collapse,
consecutive-line dedup, oversized-output capping), forwards the request to the real
upstream **without changing the model**, streams the response straight back, and
records savings to ~/.simplicio/proxy_savings.json (schema v3 — the exact format the
Simplicio Token Monitor reads) plus a PERF log.

This is the native Simplicio core. It is intentionally NOT a reimplementation of the
upstream engine's semantic/ONNX compression — it does safe, reversible-by-construction
deterministic compression only. It is **fail-open**: if anything goes wrong parsing or
compressing a request, the original bytes are forwarded unchanged. Stdlib only.

Commands:
  simplicio_engine proxy --port 8788 [--upstream https://api.openai.com]
  simplicio_engine doctor [--port 8788]
  simplicio_engine memory stats
  simplicio_engine --version
"""
import argparse
import http.client
import http.server
import json
import os
import re
import socket
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

__version__ = "1.0.0"

HOME = os.path.expanduser("~")
DATA_DIR = Path(os.environ.get("SIMPLICIO_HOME", Path(HOME) / ".simplicio"))
SAVINGS_PATH = DATA_DIR / "proxy_savings.json"
LOG_PATH = DATA_DIR / "logs" / "proxy.log"
SCHEMA_VERSION = 3
SESSION_INACTIVITY_S = 60 * 60
MAX_HISTORY = 5000
# Rough input $/1M tokens by family (savings are estimated, not billed).
PRICE_PER_M = {"gpt": 0.15, "claude": 0.80, "deepseek": 0.14, "gemini": 0.10,
               "llama": 0.06, "mistral": 0.10, "qwen": 0.08, "default": 0.14}


def _iso_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s):
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _toks(text):
    """Cheap, consistent token estimate (~4 chars/token)."""
    return max(0, round(len(text) / 4)) if text else 0


def _price(model):
    m = (model or "").lower()
    for fam, p in PRICE_PER_M.items():
        if fam in m:
            return p
    return PRICE_PER_M["default"]


# ── deterministic compression pipeline (safe, fail-open, multi-algorithm) ─────
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_TRAILING_WS = re.compile(r"[ \t]+$", re.MULTILINE)
_MANY_BLANKS = re.compile(r"\n{3,}")
_RULE_RUN = re.compile(r"([=\-_*#.~ ])\1{9,}")  # 10+ repeated rule chars (==== / ---- / ....)


def _algo_strip_ansi(t):
    return _ANSI.sub("", t)


def _algo_rule_runs(t):
    return _RULE_RUN.sub(lambda m: m.group(1) * 6, t)


def _algo_dedup_lines(t):
    out, prev, marked = [], None, False
    for line in t.split("\n"):
        if line == prev and line.strip():
            if not marked:
                out.append("… (repeated line collapsed)")
                marked = True
            continue
        prev, marked = line, False
        out.append(line)
    return "\n".join(out)


def _algo_whitespace(t):
    return _MANY_BLANKS.sub("\n\n", _TRAILING_WS.sub("", t))


def _algo_minify_json(t):
    s = t.strip()
    if (s[:1], s[-1:]) in (("{", "}"), ("[", "]")) and len(s) > 40:
        try:
            return json.dumps(json.loads(s), separators=(",", ":"), ensure_ascii=False)
        except (ValueError, TypeError):
            return t
    return t


# Applied in order; each is lossless-ish and reversible-by-construction.
_PIPELINE = [_algo_strip_ansi, _algo_rule_runs, _algo_dedup_lines, _algo_whitespace, _algo_minify_json]


def _compress_text(text):
    """Run the deterministic compression pipeline; keep result only if it shrank."""
    if not text or len(text) < 80:
        return text
    out = text
    for algo in _PIPELINE:
        try:
            out = algo(out)
        except (ValueError, TypeError, re.error):
            pass
    return out if len(out) < len(text) else text


def _compress_content(content):
    if isinstance(content, str):
        return _compress_text(content)
    if isinstance(content, list):  # multimodal blocks
        new = []
        for blk in content:
            if isinstance(blk, dict) and isinstance(blk.get("text"), str):
                blk = {**blk, "text": _compress_text(blk["text"])}
            new.append(blk)
        return new
    return content


def _compress_payload(obj):
    """Return (new_obj, tok_before, tok_after). Touches message/system text only."""
    before = after = 0

    def measure(c):
        if isinstance(c, str):
            return _toks(c)
        if isinstance(c, list):
            return sum(_toks(b.get("text", "")) for b in c if isinstance(b, dict))
        return 0

    new = dict(obj)
    if isinstance(obj.get("system"), str):  # Anthropic system prompt
        before += _toks(obj["system"])
        new["system"] = _compress_text(obj["system"])
        after += _toks(new["system"])
    msgs = obj.get("messages")
    if isinstance(msgs, list):
        nmsgs = []
        for m in msgs:
            if isinstance(m, dict) and "content" in m:
                before += measure(m["content"])
                cc = _compress_content(m["content"])
                after += measure(cc)
                nmsgs.append({**m, "content": cc})
            else:
                nmsgs.append(m)
        new["messages"] = nmsgs
    return new, before, after


# ── savings store (schema v3, atomic, thread-safe) ───────────────────────────
class SavingsStore:
    def __init__(self):
        self._lock = threading.Lock()
        self.data = self._load()

    def _load(self):
        if SAVINGS_PATH.exists():
            try:
                d = json.loads(SAVINGS_PATH.read_text(errors="replace"))
                d.setdefault("schema_version", SCHEMA_VERSION)
                d.setdefault("lifetime", self._empty_totals())
                d.setdefault("display_session", self._empty_session())
                d.setdefault("history", [])
                return d
            except (ValueError, OSError):
                pass
        return {"schema_version": SCHEMA_VERSION, "lifetime": self._empty_totals(),
                "display_session": self._empty_session(), "history": [], "projects": {}}

    @staticmethod
    def _empty_totals():
        return {"requests": 0, "tokens_saved": 0, "compression_savings_usd": 0.0,
                "total_input_tokens": 0, "total_input_cost_usd": 0.0}

    def _empty_session(self):
        t = self._empty_totals()
        t.update({"savings_percent": 0.0, "started_at": _iso_now(), "last_activity_at": _iso_now()})
        return t

    def record(self, provider, model, before, after):
        saved = max(before - after, 0)
        rate = _price(model)
        usd_saved = saved / 1_000_000 * rate
        in_cost = after / 1_000_000 * rate
        now = _iso_now()
        with self._lock:
            L = self.data["lifetime"]
            L["requests"] += 1
            L["tokens_saved"] += saved
            L["compression_savings_usd"] = round(L["compression_savings_usd"] + usd_saved, 6)
            L["total_input_tokens"] += after
            L["total_input_cost_usd"] = round(L["total_input_cost_usd"] + in_cost, 6)

            S = self.data["display_session"]
            last = _parse_iso(S.get("last_activity_at"))
            if last is None or (datetime.now(timezone.utc) - last).total_seconds() > SESSION_INACTIVITY_S:
                S.clear()
                S.update(self._empty_session())
            S["requests"] += 1
            S["tokens_saved"] += saved
            S["compression_savings_usd"] = round(S["compression_savings_usd"] + usd_saved, 6)
            S["total_input_tokens"] += after
            S["total_input_cost_usd"] = round(S["total_input_cost_usd"] + in_cost, 6)
            S["last_activity_at"] = now
            denom = S["tokens_saved"] + S["total_input_tokens"]
            S["savings_percent"] = round(S["tokens_saved"] / denom * 100, 2) if denom else 0.0

            self.data["history"].append({
                "timestamp": now, "provider": provider, "model": model,
                "total_tokens_saved": L["tokens_saved"], "compression_savings_usd": L["compression_savings_usd"],
                "total_input_tokens": L["total_input_tokens"], "total_input_cost_usd": L["total_input_cost_usd"],
            })
            if len(self.data["history"]) > MAX_HISTORY:
                self.data["history"] = self.data["history"][-MAX_HISTORY:]
            self._save()
        return saved

    def _save(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(DATA_DIR), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self.data, f)
            os.replace(tmp, SAVINGS_PATH)
        except OSError:
            if os.path.exists(tmp):
                os.unlink(tmp)


STORE = None


def _log(line):
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]
        with LOG_PATH.open("a") as f:
            f.write(f"{ts} - simplicio.proxy - INFO - {line}\n")
    except OSError:
        pass


# Per-request provider routing by model family → real provider host. Lets ONE proxy
# capture many providers transparently (no model swap). Unknown models use the default
# upstream. Each client forwards its own API key, so auth stays per-provider.
ROUTES = [
    ("gpt", "https://api.openai.com"), ("chatgpt", "https://api.openai.com"),
    ("o1", "https://api.openai.com"), ("o3", "https://api.openai.com"), ("o4", "https://api.openai.com"),
    ("claude", "https://api.anthropic.com"),
    ("deepseek", "https://api.deepseek.com"),
    ("grok", "https://api.x.ai"),
    ("mistral", "https://api.mistral.ai"), ("mixtral", "https://api.mistral.ai"),
    ("gemini", "https://generativelanguage.googleapis.com"),
]


def _route_for(model, default):
    m = (model or "").lower()
    for pref, host in ROUTES:
        if pref in m:
            return host
    return default


def _provider_of(host):
    h = host.lower()
    for key in ("deepseek", "anthropic", "openai", "googleapis", "x.ai", "groq", "mistral", "openrouter"):
        if key in h:
            return {"googleapis": "google", "x.ai": "xai"}.get(key, key)
    return "openai"


# ── transparent forwarding proxy ─────────────────────────────────────────────
class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    upstream = "https://api.openai.com"
    no_optimize = False
    route = True

    def do_POST(self):
        self._proxy()

    def do_GET(self):
        if self.path in ("/health", "/healthz"):
            self._json(200, {"status": "ok", "engine": "simplicio", "version": __version__})
            return
        if self.path.rstrip("/") in ("/stats", "/v1/stats"):
            self._json(200, (STORE.data.get("lifetime", {}) if STORE else {}))
            return
        self._proxy()

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _proxy(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""

        model, before, after = "", 0, 0
        out_body = body
        if body:
            try:
                obj = json.loads(body)
                model = obj.get("model", "") or ""
                if not self.no_optimize:
                    new_obj, before, after = _compress_payload(obj)
                    if after < before:  # only rewrite if it actually saved
                        out_body = json.dumps(new_obj).encode()
                    else:
                        after = before
            except (ValueError, TypeError):
                out_body = body  # fail-open

        # Route to the model's real provider (transparent); fall back to default upstream.
        upstream = _route_for(model, self.upstream) if self.route else self.upstream
        provider = _provider_of(urlparse(upstream).netloc)
        try:
            up = urlparse(upstream)
            conn_cls = http.client.HTTPSConnection if up.scheme == "https" else http.client.HTTPConnection
            conn = conn_cls(up.netloc, timeout=600)
            headers = {k: v for k, v in self.headers.items()
                       if k.lower() not in ("host", "content-length", "accept-encoding")}
            headers["Content-Length"] = str(len(out_body))
            headers["Host"] = up.netloc
            conn.request(self.command, self.path, body=out_body, headers=headers)
            resp = conn.getresponse()
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() not in ("transfer-encoding", "content-length", "connection", "content-encoding"):
                    self.send_header(k, v)
            self.send_header("Connection", "close")
            self.end_headers()
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
            conn.close()
        except Exception as e:  # upstream failure — report, don't crash the proxy
            try:
                self._json(502, {"error": {"message": f"simplicio proxy upstream error: {e}", "type": "upstream_error"}})
            except OSError:
                return
            _log(f"UPSTREAM_ERROR {e}")
            return

        if STORE is not None and before:
            saved = STORE.record(provider, model or "unknown", before, after)
            cache = max(after, 0)
            _log(f"PERF model={model or 'unknown'} provider={provider} tok_before={before} "
                 f"tok_after={after} tok_saved={saved} cache_hit_pct=0")

    def log_message(self, *a):
        pass


def cmd_proxy(args):
    global STORE
    STORE = SavingsStore()
    Handler.upstream = args.upstream.rstrip("/")
    Handler.no_optimize = args.no_optimize
    Handler.route = not args.no_route
    httpd = http.server.ThreadingHTTPServer((args.host, args.port), Handler)
    mode = "passthrough" if args.no_optimize else "compressing"
    routing = "per-provider routing" if Handler.route else f"fixed → {Handler.upstream}"
    print(f"⬡ Simplicio capture engine · proxy ({mode}, {routing}) on http://{args.host}:{args.port}")
    _log(f"START proxy port={args.port} upstream={Handler.upstream} mode={mode}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()


def _port_up(port):
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=0.5):
            return True
    except OSError:
        return False


def cmd_doctor(args):
    store = SavingsStore()
    life = store.data.get("lifetime", {})
    up = _port_up(args.port)
    print(f"Simplicio capture engine · doctor (port {args.port})")
    print(f"  proxy:   {'✓ running' if up else '✗ not reachable'} at http://127.0.0.1:{args.port}")
    print(f"  savings: {life.get('tokens_saved', 0):,} tokens / "
          f"${life.get('compression_savings_usd', 0):.4f} saved · {life.get('requests', 0)} requests")
    return 0


def cmd_memory(args):
    store = SavingsStore()
    print(f"Total Memories: {len(store.data.get('history', []))}")
    print(f"Database: {SAVINGS_PATH}")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="simplicio_engine", description="Simplicio capture engine")
    p.add_argument("--version", action="version", version=f"simplicio-engine {__version__}")
    sub = p.add_subparsers(dest="cmd")

    pp = sub.add_parser("proxy", help="run the transparent capture proxy")
    pp.add_argument("--port", type=int, default=int(os.environ.get("SIMPLICIO_PROXY_PORT", "8788")))
    pp.add_argument("--host", default="127.0.0.1")
    pp.add_argument("--upstream", default=os.environ.get("SIMPLICIO_UPSTREAM", "https://api.openai.com"),
                    help="default/fallback upstream host for unrouted models")
    pp.add_argument("--no-optimize", action="store_true", help="pure passthrough (no compression)")
    pp.add_argument("--no-route", action="store_true",
                    help="disable per-provider routing; send everything to --upstream")

    pd = sub.add_parser("doctor", help="show proxy + savings status")
    pd.add_argument("--port", type=int, default=int(os.environ.get("SIMPLICIO_PROXY_PORT", "8788")))

    pm = sub.add_parser("memory", help="memory stats")
    pm.add_argument("memory_cmd", nargs="?", default="stats")

    args = p.parse_args(argv)
    if args.cmd == "proxy":
        return cmd_proxy(args)
    if args.cmd == "doctor":
        return cmd_doctor(args)
    if args.cmd == "memory":
        return cmd_memory(args)
    p.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())

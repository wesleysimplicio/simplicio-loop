#!/usr/bin/env python3
"""Simplicio wrap — launch a coding client with its LLM traffic routed through the
local Simplicio capture proxy for that run (mirrors `headroom wrap <client>`).

It does NOT patch the client: it just injects the proxy base-URL env vars into the
child process environment and execs the client binary. The proxy (run separately via
`simplicio-cli proxy` / `simplicio_engine proxy`) measures + compresses the traffic.

Env injected into the client:
  OPENAI_BASE_URL = http://127.0.0.1:<port>/v1
  OPENAI_API_BASE = http://127.0.0.1:<port>/v1   (some SDKs read this name)
  ANTHROPIC_BASE_URL = http://127.0.0.1:<port>   (only for the `claude` client)

Port comes from $SIMPLICIO_PROXY_PORT (default 8788).

Usage:
  simplicio_wrap <client> [-- extra args...]
  simplicio_wrap claude
  simplicio_wrap codex --require-proxy -- chat
  SIMPLICIO_PROXY_PORT=9000 simplicio_wrap aider -- --model gpt-4o

Flags:
  --require-proxy   abort (exit 3) if the proxy is not listening on the port
  --port <n>        override $SIMPLICIO_PROXY_PORT for this run

Testability:
  $SIMPLICIO_WRAP_BIN overrides the resolved client binary (real feature: point
  `wrap` at a wrapper/shim, or a fake client in tests). When set, it is used as-is
  instead of the known-name map + shutil.which resolution.

Stdlib only.
"""
import os
import shutil
import socket
import subprocess
import sys

__version__ = "1.0.0"

DEFAULT_PORT = 8788

# Known client name -> binary name. Unknown clients fall back to their own name.
CLIENT_BINS = {
    "claude": "claude",
    "codex": "codex",
    "cursor": "cursor",
    "opencode": "opencode",
    "aider": "aider",
}

# Clients that speak the Anthropic message format and therefore also need
# ANTHROPIC_BASE_URL pointed at the proxy root (the engine routes Anthropic-format
# requests to the anthropic upstream).
ANTHROPIC_CLIENTS = {"claude"}


def resolve_binary(client, env=None):
    """Resolve the executable to launch for `client`.

    $SIMPLICIO_WRAP_BIN wins (used as-is). Otherwise map the known client name to a
    binary name and resolve it on PATH via shutil.which. Returns the path/command to
    exec, or None if it cannot be found.
    """
    env = os.environ if env is None else env
    override = env.get("SIMPLICIO_WRAP_BIN")
    if override:
        return override
    binary = CLIENT_BINS.get(client, client)
    return shutil.which(binary)


def build_capture_env(client, port, base=None):
    """Return a fresh env dict with the capture base-URL vars injected."""
    env = dict(os.environ if base is None else base)
    openai_base = f"http://127.0.0.1:{port}/v1"
    env["OPENAI_BASE_URL"] = openai_base
    env["OPENAI_API_BASE"] = openai_base
    if client in ANTHROPIC_CLIENTS:
        env["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{port}"
    return env


def proxy_listening(port, host="127.0.0.1", timeout=0.4):
    """True if something is accepting TCP connections on host:port."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _parse_args(argv):
    """Split argv into (client, port_override, require_proxy, extra_args).

    Everything after a literal `--` is passed verbatim to the client. `--port` and
    `--require-proxy` are wrap-level flags consumed before `--`.
    """
    client = None
    port_override = None
    require_proxy = False
    extra = []
    i = 0
    n = len(argv)
    while i < n:
        tok = argv[i]
        if tok == "--":
            extra = list(argv[i + 1:])
            break
        if tok == "--require-proxy":
            require_proxy = True
        elif tok == "--port":
            i += 1
            if i >= n:
                raise ValueError("--port requires a value")
            port_override = int(argv[i])
        elif tok.startswith("--port="):
            port_override = int(tok.split("=", 1)[1])
        elif tok in ("-h", "--help"):
            return ("--help", None, False, [])
        elif tok == "--version":
            return ("--version", None, False, [])
        elif client is None:
            client = tok
        else:
            # Bare extra arg before `--`: treat as client arg too (lenient).
            extra.append(tok)
        i += 1
    return (client, port_override, require_proxy, extra)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        client, port_override, require_proxy, extra = _parse_args(argv)
    except ValueError as e:
        print(f"simplicio-cli wrap: {e}", file=sys.stderr)
        return 2

    if client == "--version":
        print(__version__)
        return 0
    if client == "--help" or client is None:
        print(__doc__)
        return 0 if client == "--help" else 2

    port = port_override
    if port is None:
        port = int(os.environ.get("SIMPLICIO_PROXY_PORT", str(DEFAULT_PORT)))

    binary = resolve_binary(client)
    if not binary:
        print(
            f"simplicio-cli wrap: client '{client}' not found on PATH "
            f"(looked for '{CLIENT_BINS.get(client, client)}'). "
            f"Set $SIMPLICIO_WRAP_BIN to override.",
            file=sys.stderr,
        )
        return 127

    up = proxy_listening(port)
    if not up:
        msg = (
            f"simplicio-cli wrap: capture proxy not running on :{port} — run: simplicio-cli proxy"
        )
        if require_proxy:
            print(msg + " (aborting: --require-proxy)", file=sys.stderr)
            return 3
        print(msg, file=sys.stderr)
        print("simplicio-cli wrap: launching anyway (traffic will NOT be captured).", file=sys.stderr)

    env = build_capture_env(client, port)
    cmd = [binary] + extra

    try:
        return subprocess.run(cmd, env=env).returncode
    except FileNotFoundError:
        print(f"simplicio-cli wrap: failed to exec '{binary}'", file=sys.stderr)
        return 127
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())

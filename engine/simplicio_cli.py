#!/usr/bin/env python3
"""simplicio — unified command-line dispatcher.

Single entry point so users type `simplicio <cmd>` instead of
`python3 engine/simplicio_engine.py <cmd>`. Stdlib only.

Engine-backed commands (proxy, doctor, memory, mcp, init) are forwarded to the
sibling `simplicio_engine.py` via os.execv (process replacement, zero overhead).
`compress` and `version`/`help` are handled inline.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.join(HERE, "simplicio_engine.py")

# commands forwarded verbatim to the engine module
ENGINE_CMDS = ("proxy", "doctor", "memory", "mcp", "init")


def _version():
    """Read __version__ from the engine if importable, else fallback."""
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    try:
        import simplicio_engine  # type: ignore
        return getattr(simplicio_engine, "__version__", "1.0.0")
    except Exception:
        return "1.0.0"


def _help():
    return (
        "simplicio — unified CLI for the simplicio-loop engine\n"
        "\n"
        "Usage: simplicio <command> [args...]\n"
        "\n"
        "Commands:\n"
        "  proxy [...]    run the transparent capture proxy\n"
        "  doctor [...]   show proxy + savings status\n"
        "  memory [...]   memory: stats | remember/recall/forget/list\n"
        "  mcp [...]      run the native MCP server\n"
        "  init [...]     register Simplicio into a client\n"
        "  compress       read stdin, print compressed text to stdout\n"
        "  version        print the engine version\n"
        "  help           show this help\n"
    )


def _compress():
    """Read stdin, print simplicio_compress.compress(stdin) to stdout."""
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    import simplicio_compress  # type: ignore
    data = sys.stdin.read()
    sys.stdout.write(simplicio_compress.compress(data))
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)

    if not argv or argv[0] in ("help", "-h", "--help"):
        sys.stdout.write(_help())
        return 0

    cmd, rest = argv[0], argv[1:]

    if cmd in ("version", "--version", "-V"):
        sys.stdout.write("simplicio %s\n" % _version())
        return 0

    if cmd == "compress":
        return _compress()

    if cmd in ENGINE_CMDS:
        # replace this process with the engine running the same subcommand
        os.execv(sys.executable, [sys.executable, ENGINE, cmd, *rest])
        # os.execv never returns on success
        return 0

    sys.stderr.write("simplicio: unknown command %r\n\n" % cmd)
    sys.stderr.write(_help())
    return 2


if __name__ == "__main__":
    sys.exit(main())

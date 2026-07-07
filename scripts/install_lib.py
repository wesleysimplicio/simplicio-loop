#!/usr/bin/env python3
"""simplicio-loop — universal installer (one logic, all runtimes).

Copies the 6 skills + hooks into a target, wires the loop where the runtime supports it,
ensures the runtime's entry/instructions file references the skill, and prints the MCP-bind
line. Pure Python ->identical on Windows/macOS/Linux. Safe: create-or-merge, never clobbers
unrelated config; idempotent marker blocks.

Also installs+verifies the two REQUIRED loop operators (simplicio-mapper, simplicio-cli) unless
--skip-operators is passed.

Usage:
    python3 scripts/install_lib.py <runtime> [--global] [--target DIR] [--skip-operators] [--lite]
    <runtime> ∈ claude codex vscode cursor antigravity kiro opencode gemini aider hermes openclaw
    omit <runtime> to auto-detect.

--lite mode:
    Installs skills + hooks instantly (<30s) and spawns operator install in background.
    Loop runs in LITE mode with native tool fallbacks until operators are available.
    Upgrade to FULL automatic at turn boundary via preflight check.
    Use --strict to preserve the current hard-BLOCK behavior (operators required immediately).
"""
import json
import os
import shutil
import subprocess
import sys

try:  # Windows consoles default to cp1252 and choke on non-ASCII — force UTF-8.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE = os.path.dirname(HERE)
HOME = os.path.expanduser("~")
SKILLS = ["simplicio-tasks", "simplicio-loop", "simplicio-orient",
          "simplicio-review", "simplicio-compress", "simplicio-learn"]
# The simplicio-loop drive REQUIRES two operators (see simplicio-loop/SKILL.md § Bound operators):
#   simplicio-mapper -> repo survey (binds `orient`); binary: simplicio-mapper
#   simplicio-cli    -> action operator (binds `execute`/`deterministic_edit`); binary: simplicio-dev-cli
# (the bare `simplicio` command is reserved for the separate `simplicio-runtime`, not this operator.)
OPERATORS = [("simplicio-mapper", "simplicio-mapper"), ("simplicio-cli", "simplicio-dev-cli")]
LEGACY_MARK_A, LEGACY_MARK_B = "<!-- simplicio-tasks:begin -->", "<!-- simplicio-tasks:end -->"
MARK_A, MARK_B = "<!-- simplicio-loop:begin -->", "<!-- simplicio-loop:end -->"


def entry_block(runtime=None):
    """Build the entry-file block. On a FORCED_BIND_RUNTIMES host, append a REQUIRED line so the
    host's own standing-instructions file (AGENTS.md/GEMINI.md/copilot-instructions.md/steering)
    states the native-bind + full-protocol requirement, not just an installer-side log line."""
    body = (
        MARK_A + "\n"
        "## simplicio-loop — Unified Core + Loop\n\n"
        "Load and follow the protocol in `.claude/skills/simplicio-loop/SKILL.md` and its "
        "companion skills (`simplicio-tasks` as legacy alias, `simplicio-orient`, "
        "`simplicio-review`, `simplicio-compress`, `simplicio-learn`) IN FULL — every step, "
        "no partial subset. Run "
        "commands for real; clamp heavy output via `python3 hooks/orient_clamp.py -- <cmd>`; "
        "never close work without a merged PR or concrete evidence; honor the irreversible-op "
        "human gate and explicit STOP/cancel path.\n"
    )
    if runtime in FORCED_BIND_RUNTIMES:
        body += (
            "\nNative bind to `simplicio-runtime` (MCP or CLI) is REQUIRED on this host, not "
            "optional — verify with `simplicio doctor --json` before starting; if unreachable, "
            "STOP and report the gap rather than silently degrading to the unbound LLM-only "
            "path (install: `pip install -U simplicio-installer && simplicio install --global`).\n"
        )
    body += "\nInvoke with: `/simplicio-loop <the body of work>`\n" + MARK_B
    return body

# entry file + MCP client id per runtime; None entry = no instructions file needed
RUNTIMES = {
    "claude":      {"entry": None,                              "mcp": "claude-code", "hooks": "claude"},
    "codex":       {"entry": "AGENTS.md",                       "mcp": "codex",       "hooks": None},
    "vscode":      {"entry": ".github/copilot-instructions.md", "mcp": "vscode",      "hooks": None},
    "cursor":      {"entry": None,                              "mcp": "cursor",      "hooks": "cursor"},
    "antigravity": {"entry": "AGENTS.md",                       "mcp": "antigravity", "hooks": None},
    "kiro":        {"entry": ".kiro/steering/simplicio-loop.md", "mcp": "kiro",       "hooks": None},
    "opencode":    {"entry": "AGENTS.md",                       "mcp": "opencode",    "hooks": None},
    "gemini":      {"entry": "GEMINI.md",                       "mcp": "gemini",      "hooks": None},
    "aider":       {"entry": "CONVENTIONS.md",                  "mcp": None,          "hooks": None},
    "hermes":      {"entry": None,                              "mcp": None,          "hooks": "native"},
    "openclaw":    {"entry": None,                              "mcp": None,          "hooks": "native"},
}


def log(msg):
    print("  " + msg)


def copy_skills(target):
    dst_root = os.path.join(target, ".claude", "skills")
    os.makedirs(dst_root, exist_ok=True)
    for s in SKILLS:
        src = os.path.join(SOURCE, ".claude", "skills", s)
        if not os.path.isdir(src):
            log("! missing source skill: %s (skipped)" % s)
            continue
        shutil.copytree(src, os.path.join(dst_root, s), dirs_exist_ok=True)
    log("skills -> %s" % dst_root)


def hooks_dir(target, is_global):
    # global → keep hooks tidy under ~/.claude/hooks; project → ./hooks at the repo root
    return os.path.join(target, ".claude", "hooks") if is_global else os.path.join(target, "hooks")


def copy_hooks(target, is_global):
    src = os.path.join(SOURCE, "hooks")
    dst = hooks_dir(target, is_global)
    if os.path.abspath(dst) == os.path.abspath(src):
        return  # already here (project install inside this repo)
    if os.path.isdir(src):
        shutil.copytree(src, dst, dirs_exist_ok=True)
        log("hooks -> %s" % dst)


def install_git_precommit_hook(target):
    """Wire `hooks/pre-commit.py` as the target repo's git pre-commit hook (#98).

    Auto-syncs `plugin/` + `simplicio_loop/_bundle/` from source on every commit that touches a
    watched path (`scripts/mirror_manifest.py` WATCHED_SOURCE_DIRS is the single source of truth
    for which paths). Project-local concept only — `.git/hooks/` lives per-repo, so this is a
    no-op for a `--global` install (target has no `.git`) or a target that isn't a git repo at
    all. Fail-open, like every other hook in this repo: never raises, never aborts the install;
    an existing foreign `pre-commit` hook is left untouched (logged instead of clobbered).
    """
    git_dir = os.path.join(target, ".git")
    if not os.path.isdir(git_dir):
        return  # not a git repo (or a --global target) — nothing to wire
    src_hook = os.path.join(hooks_dir(target, False), "pre-commit.py")
    if not os.path.exists(src_hook):
        src_hook = os.path.join(SOURCE, "hooks", "pre-commit.py")  # fallback: this repo's copy
    if not os.path.exists(src_hook):
        return
    try:
        hooks_out = os.path.join(git_dir, "hooks")
        os.makedirs(hooks_out, exist_ok=True)
        dst = os.path.join(hooks_out, "pre-commit")
        if os.path.exists(dst):
            existing = ""
            try:
                with open(dst, encoding="utf-8", errors="replace") as f:
                    existing = f.read()
            except OSError:
                pass
            if "simplicio-loop" not in existing and "pre-commit.py" not in existing:
                log("! .git/hooks/pre-commit already exists and isn't ours — leaving it alone. "
                    "Wire manually: see hooks/README.md")
                return
        shebang = "#!/bin/sh\n" if os.name != "nt" else "#!/usr/bin/env sh\n"
        body = shebang + (
            "# simplicio-loop: auto-sync plugin/ + simplicio_loop/_bundle/ on commit (#98)\n"
            'exec python3 "%s" "$@"\n' % os.path.abspath(src_hook).replace("\\", "/")
        )
        with open(dst, "w", encoding="utf-8", newline="\n") as f:
            f.write(body)
        try:
            os.chmod(dst, 0o755)
        except OSError:
            pass  # e.g. some Windows filesystems — git only needs the file to be runnable via sh
        log("git pre-commit hook installed -> %s (auto-syncs plugin/+_bundle/, #98)" % dst)
    except OSError as e:
        log("! could not install git pre-commit hook (fail-open, non-fatal): %s" % e)


def ensure_entry(target, rel, runtime=None):
    if not rel:
        return
    block = entry_block(runtime)
    path = os.path.join(target, rel)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    existing = ""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            existing = f.read()
    begin = end = None
    if MARK_A in existing and MARK_B in existing:
        begin, end = MARK_A, MARK_B
    elif LEGACY_MARK_A in existing and LEGACY_MARK_B in existing:
        begin, end = LEGACY_MARK_A, LEGACY_MARK_B
    if begin and end:
        # refresh the block in place, migrating legacy markers to the new public command block
        pre = existing.split(begin)[0]
        post = existing.split(end, 1)[1]
        new = pre.rstrip() + "\n\n" + block + post
    else:
        new = (existing.rstrip() + "\n\n" if existing.strip() else "") + block + "\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(new)
    log("entry -> %s" % rel)


def merge_claude_hooks(target, is_global):
    path = os.path.join(target, ".claude", "settings.json")
    data = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            log("! .claude/settings.json unreadable — printing snippet instead")
            return print_claude_snippet()
    hooks = data.setdefault("hooks", {})

    def has(event, needle):
        for grp in hooks.get(event, []):
            for h in grp.get("hooks", []):
                if needle in h.get("command", ""):
                    return True
        return False

    # Global install: cwd varies per session, so reference hooks by ABSOLUTE path
    # (forward slashes work on Windows too). Project install: relative ./hooks (portable).
    def cmd(name):
        if is_global:
            return 'python3 "%s"' % os.path.abspath(
                os.path.join(hooks_dir(target, True), name)).replace("\\", "/")
        return "python3 ./hooks/%s" % name

    if not has("Stop", "loop_stop.py"):
        hooks.setdefault("Stop", []).append({"hooks": [
            {"type": "command", "command": cmd("loop_stop.py")},
        ]})
    wired = "Stop"
    # orient_rewrite rewrites Bash calls; only wire it project-locally (opt-in), never
    # globally — a global PreToolUse would touch every session on the machine.
    if not is_global and not has("PreToolUse", "orient_rewrite.py"):
        hooks.setdefault("PreToolUse", []).append({
            "matcher": "Bash",
            "hooks": [{"type": "command", "command": cmd("orient_rewrite.py")}],
        })
        wired = "Stop + PreToolUse"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    log("hooks wired -> %s settings.json (%s)" % ("global" if is_global else ".claude", wired))


def print_claude_snippet():
    log("add to .claude/settings.json manually — see adapters/claude/README.md")


def ensure_operators(skip_install=False):
    """Install + verify the two REQUIRED loop operators (simplicio-mapper, simplicio-cli).

    The simplicio-loop drive surveys via `simplicio-mapper` and acts via `simplicio-dev-cli` instead of
    the LLM, so both must be present. pip-install (unless skipped), then verify the binaries are on
    PATH. Missing binary after install is a hard error — the loop would BLOCK at runtime otherwise.
    """
    pkgs = [pkg for pkg, _ in OPERATORS]
    if not skip_install:
        base = [sys.executable, "-m", "pip", "install", "-U"]
        try:
            subprocess.run(base + pkgs, check=True)
            log("operators installed -> %s" % ", ".join(pkgs))
        except Exception:
            # PEP 668 / externally-managed env (Homebrew/Debian python): retry into the user site.
            try:
                subprocess.run(base + ["--user", "--break-system-packages"] + pkgs, check=True)
                log("operators installed (user site) -> %s" % ", ".join(pkgs))
            except Exception as e:
                # Try uv tool install if pip failed (uv-managed Python)
                try:
                    uv_path = shutil.which("uv")
                    if uv_path:
                        subprocess.run([uv_path, "tool", "install"] + pkgs, check=True)
                        log("operators installed (uv tool) -> %s" % ", ".join(pkgs))
                    else:
                        raise
                except Exception as e2:
                    log("! pip install of operators failed (%s) — install manually: pip install %s"
                        % (e, " ".join(pkgs)))
    # A --user install can land the console-scripts in a dir not on PATH (e.g. macOS
    # ~/Library/Python/X.Y/bin). Find each operator binary and symlink it into ~/.local/bin.
    _link_operator_bins()
    missing = [b for _, b in OPERATORS if shutil.which(b) is None]
    if missing:
        log("! REQUIRED loop operators NOT on PATH: %s" % ", ".join(missing))
        log("  the simplicio-loop drive will BLOCK until present — run: pip install %s"
            % " ".join(pkgs))
    else:
        log("operators verified on PATH: %s" % ", ".join(b for _, b in OPERATORS))


def _link_console_script(name, kind="bin"):
    """Symlink a console-script into ~/.local/bin (commonly on PATH) when a --user install dropped
    it somewhere off PATH (macOS ~/Library/Python/X.Y/bin · Windows %APPDATA%/Python/*/Scripts).
    Idempotent; best-effort (never raises). Returns True if it's reachable afterward."""
    import glob
    if shutil.which(name):
        return True  # already on PATH
    local_bin = os.path.join(HOME, ".local", "bin")
    cand_dirs = [local_bin, os.path.dirname(sys.executable)]
    cand_dirs += glob.glob(os.path.join(HOME, "Library", "Python", "*", "bin"))   # macOS user scheme
    cand_dirs += glob.glob(os.path.join(HOME, "AppData", "Roaming", "Python", "*", "Scripts"))  # Windows
    for d in cand_dirs:
        src = os.path.join(d, name + (".exe" if os.name == "nt" else ""))
        if os.path.isfile(src):
            try:
                os.makedirs(local_bin, exist_ok=True)
                dst = os.path.join(local_bin, os.path.basename(src))
                if os.path.islink(dst) or os.path.exists(dst):
                    os.remove(dst)
                os.symlink(src, dst)
                log("%s %s -> linked into ~/.local/bin" % (kind, name))
            except OSError:
                pass
            return os.path.isfile(os.path.join(local_bin, os.path.basename(src)))
    return False


def _link_operator_bins():
    """Symlink the two operator console-scripts into ~/.local/bin (best-effort)."""
    for _, b in OPERATORS:
        _link_console_script(b, kind="operator")


# Runtimes where native bind to simplicio-runtime (MCP or CLI) is a REQUIRED policy, not an
# optional nicety: the loop and ALL simplicio-tasks/simplicio-loop/simplicio-review directives
# must run bound, never silently degrade to the full LLM-only fallback on these hosts.
FORCED_BIND_RUNTIMES = {"claude", "codex", "cursor", "vscode", "antigravity",
                        "kiro", "opencode", "hermes"}
# Runtimes simplicio-runtime's own `simplicio install --global` auto-detects and registers
# end-to-end (writes the MCP config for each, idempotent to re-run) — see
# `simplicio install --global --dry-run --json` -> assistant_adapters.
RUNTIME_AUTO_BIND = {"claude", "codex", "cursor", "vscode", "kiro"}


def ensure_runtime_bind(runtime, cfg):
    """Force the simplicio-runtime native bind for FORCED_BIND_RUNTIMES (REQUIRED, not optional).

    Mirrors `ensure_operators`'s severity: best-effort registration now, loud unmissable warning
    if `simplicio` is missing or registration can't be confirmed — the loop is expected to BLOCK
    on an unbound forced-bind runtime rather than quietly running the degraded LLM-only fallback.
    No-op for runtimes outside FORCED_BIND_RUNTIMES (gemini/aider/openclaw keep the old optional
    log line further down in main()).
    """
    if runtime not in FORCED_BIND_RUNTIMES:
        return
    simplicio_path = shutil.which("simplicio")
    if not simplicio_path:
        log("! simplicio-runtime is REQUIRED for '%s' (project policy) and is NOT installed." % runtime)
        log("  install it: pip install -U simplicio-installer && simplicio install --global")
        log("  until then simplicio-tasks/simplicio-loop/simplicio-review MUST refuse to claim")
        log("  native-bind status and should block rather than silently run the LLM-only fallback.")
        return
    if runtime in RUNTIME_AUTO_BIND:
        try:
            subprocess.run([simplicio_path, "install", "--global"], check=False,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            log("native bind REQUIRED+applied for '%s' -> simplicio install --global" % runtime)
        except Exception as e:
            log("! 'simplicio install --global' failed for '%s': %s" % (runtime, e))
            log("  retry manually: simplicio install --global")
    elif runtime == "opencode":
        pass  # merge_opencode_mcp() in main() already does this, with the same forced severity
    else:
        # antigravity, hermes: no config schema this installer can safely auto-write (Antigravity's
        # MCP config path/format isn't verified here; Hermes binds natively, no MCP file at all).
        # Verify the runtime itself is healthy so the requirement is at least checked, not assumed.
        try:
            import re as _re
            r = subprocess.run([simplicio_path, "doctor", "--json"], check=False,
                                capture_output=True, text=True, timeout=30)
            # doctor --json pretty-prints across lines, so regex over the whole blob rather
            # than assuming one-JSON-object-per-line (that only holds for the progress events).
            m = _re.search(r'"overall_status"\s*:\s*"(\w+)"', r.stdout or "")
            healthy = bool(m) and m.group(1) in ("ok", "warning")
        except Exception:
            healthy = False
        if healthy:
            log("simplicio-runtime reachable for '%s' — follow adapters/%s/README.md to finish "
                "the native bind (no safe auto-write for this host's config format)." % (runtime, runtime))
        else:
            log("! simplicio doctor did not report healthy for '%s' — native bind is REQUIRED "
                "here; fix it (simplicio doctor --repair) before relying on simplicio-loop." % runtime)


def detect():
    for rt, mark in [("cursor", ".cursor"), ("claude", ".claude"),
                     ("kiro", ".kiro"), ("vscode", ".github"), ("gemini", ".gemini"),
                     ("opencode", ".opencode")]:
        if os.path.isdir(os.path.join(os.getcwd(), mark)):
            return rt
    # Also check for opencode.json in parent dirs
    try:
        result = subprocess.run(["opencode", "--version"], capture_output=True, text=True)
        if result.returncode == 0:
            return "opencode"
    except Exception:
        pass
    return "claude"


OPCODE_CONFIG = os.path.join(HOME, ".config", "opencode", "opencode.json")
OPCODE_SKILLS = os.path.join(HOME, ".config", "opencode", "skills")


def copy_skills_opencode():
    """Copy skills to OpenCode's skill directory (~/.config/opencode/skills/)."""
    dst_root = OPCODE_SKILLS
    os.makedirs(dst_root, exist_ok=True)
    for s in SKILLS:
        src = os.path.join(SOURCE, ".claude", "skills", s)
        if not os.path.isdir(src):
            continue
        dst = os.path.join(dst_root, s)
        if os.path.exists(dst):
            log("opencode skill already exists: %s" % s)
            continue
        shutil.copytree(src, dst)
    log("opencode skills -> %s" % dst_root)


def merge_opencode_mcp():
    """Register simplicio MCP server in opencode.json.
    Best-effort: any failure logs a warning and prints the manual command."""
    try:
        data = {}
        if os.path.exists(OPCODE_CONFIG):
            with open(OPCODE_CONFIG, encoding="utf-8") as f:
                data = json.load(f)
        mcp = data.setdefault("mcp", {})
        if "simplicio" in mcp:
            log("opencode MCP already registered")
            return
        # Find simplicio CLI on PATH
        simplicio_path = shutil.which("simplicio")
        if not simplicio_path:
            # If not on PATH, try uv tool
            import glob
            uv_tools = os.path.join(HOME, ".local", "share", "uv", "tools")
            cand = glob.glob(os.path.join(uv_tools, "simplicio-loop", "*", "bin", "simplicio"))
            cand += glob.glob(os.path.join(uv_tools, "simplicio-loop", "*", "bin", "simplicio.exe"))
            if cand:
                simplicio_path = cand[0]
        if not simplicio_path:
            # Check for simplicio-loop console script
            candidates = [os.path.join(HOME, ".local", "bin", "simplicio"),
                          os.path.join(HOME, ".local", "bin", "simplicio-loop"),
                          shutil.which("simplicio-loop")]
            for c in candidates:
                if c:
                    simplicio_path = c
                    break
        if not simplicio_path:
            log("! simplicio CLI not found — MCP registration skipped. "
                "Install: uv tool install simplicio-loop")
            log("  Then manually add to opencode.json: see adapters/opencode/README.md")
            return
        mcp["simplicio"] = {
            "type": "local",
            "command": [simplicio_path, "serve", "--mcp", "--stdio"],
            "enabled": True
        }
        with open(OPCODE_CONFIG, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
        log("opencode MCP registered -> %s" % OPCODE_CONFIG)
    except Exception as e:
        log("! opencode MCP registration failed: %s" % e)
        log('  manually add to opencode.json: {"mcp":{"simplicio":{"type":"local",'
            '"command":["simplicio","serve","--mcp","--stdio"]}}}')


def _pip(args_):
    """pip install with a PEP-668 fallback into the user site. Best-effort (never raises)."""
    base = [sys.executable or "python3", "-m", "pip", "install", "-U"]
    for extra in ([], ["--user", "--break-system-packages"]):
        try:
            subprocess.run(base + extra + args_, check=True, cwd=SOURCE)
            return True
        except Exception:
            continue
    return False


def install_all_deps():
    """MANDATORY full install — every capability in simplicio-loop, not opt-in. Installs the package
    with ALL extras (the ONNX models backend: onnxruntime + huggingface_hub + tokenizers + pillow) so
    `simplicio-cli kompress/router/embed/image` work, plus the menu-bar tray dep. Heavy but complete;
    `--minimal` skips it. Best-effort: a single heavy dep failing won't abort the install."""
    spec = ".[onnx]" if os.path.exists(os.path.join(SOURCE, "pyproject.toml")) else "simplicio-loop[onnx]"
    log("full install: package + ONNX models backend (%s)..." % spec)
    _pip([spec]) or log("! full-stack pip failed — run manually: pip install '%s'" % spec)
    tray = ["rumps"] if sys.platform == "darwin" else ["pystray", "pillow"]
    _pip(tray)


def _open_dashboard_first_run():
    """Open the Token Monitor dashboard ONCE, on the first install, so the user sees it works.

    Guarded by a marker (~/.simplicio/.dashboard_shown): a re-install/update does NOT reopen it —
    the dashboard is on-demand, never forced open. Opt out entirely with SIMPLICIO_NO_DASHBOARD=1
    (headless/CI). Best-effort: any failure (no browser, no display) is swallowed — never blocks.
    """
    if os.environ.get("SIMPLICIO_NO_DASHBOARD") == "1":
        return
    marker = os.path.join(HOME, ".simplicio", ".dashboard_shown")
    if os.path.exists(marker):
        log("dashboard already shown once — open it any time:  simplicio-loop dashboard")
        return
    # Headless box (no GUI)? Don't auto-open: there's nothing to show, and webbrowser.open() on
    # headless Linux can BLOCK forever. Mark first-run done so we never retry; print the reopen line.
    gui = sys.platform == "darwin" or os.name == "nt" \
        or bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    if not gui:
        try:
            os.makedirs(os.path.dirname(marker), exist_ok=True)
            open(marker, "w").close()
        except OSError:
            pass
        log("headless — dashboard not auto-opened. Open it any time:  simplicio-loop dashboard")
        return
    import socket as _socket
    import time as _time
    import webbrowser as _wb
    port = int(os.environ.get("SIMPLICIO_MONITOR_PORT", "9090"))
    dash = os.path.join(SOURCE, "hooks", "simplicio_dashboard.py")
    url = "http://127.0.0.1:%d" % port

    def _up():
        try:
            with _socket.create_connection(("127.0.0.1", port), 0.5):
                return True
        except OSError:
            return False

    try:
        if not _up() and os.path.exists(dash):
            logdir = os.path.join(HOME, ".simplicio", "logs")
            os.makedirs(logdir, exist_ok=True)
            env = {**os.environ, "PORT": str(port)}
            kw = {"start_new_session": True} if os.name != "nt" else {"creationflags": 0x208}
            with open(os.path.join(logdir, "token-monitor.log"), "ab") as lf:
                subprocess.Popen([sys.executable or "python3", dash], env=env,
                                 stdout=lf, stderr=lf, stdin=subprocess.DEVNULL, **kw)
            for _ in range(25):
                if _up():
                    break
                _time.sleep(0.2)
        if _up():
            if os.environ.get("SIMPLICIO_NO_BROWSER") != "1":
                try:
                    _wb.open(url)
                except Exception:
                    pass
            log("Token Monitor opened once → %s" % url)
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        open(marker, "w").close()   # mark first-run done so we never auto-open again
    except Exception:
        pass


def setup_monitor(enable):
    """Token monitor = machine-level capture proxy + dashboard + tray + always-capture wiring.

    Default-on (the install is complete by default; `--minimal` disables it). Registers the
    always-on capture proxy (launchd via setup_simplicio.sh on macOS · systemd/Startup via
    install_services.py elsewhere) and routes Claude + Codex + Hermes through the proxy. The
    dashboard opens ONCE on the first install (then on-demand); the tray is on-demand.
    """
    svc = os.path.join(HERE, "install_services.py")
    setup_sh = os.path.join(HERE, "setup_simplicio.sh")
    if not enable:
        log("token monitor SKIPPED (--minimal). Enable later: bash scripts/setup_simplicio.sh")
        return
    py = sys.executable or "python3"
    log("token capture: always-on proxy + always-capture wiring (dashboard/tray are on-demand)...")
    if sys.platform == "darwin" and os.path.exists(setup_sh):
        subprocess.run(["bash", setup_sh], check=False)   # registers the proxy (auto) + wires
    elif os.path.exists(svc):
        subprocess.run([py, svc, "install"], check=False)
        subprocess.run([py, svc, "wire"], check=False)
    _open_dashboard_first_run()   # show the dashboard once on a fresh install (marker-guarded)
    log("capture proxy always-on · Claude+Codex+Hermes measured. Re-open the UI any time:")
    log("  dashboard: simplicio-loop dashboard   (or: bash scripts/simplicio-economy.sh monitor)")
    log("  tray:      bash scripts/simplicio-economy.sh tray   ·   or just ask the agent to open it")


def main():
    args = sys.argv[1:]
    is_global = "--global" in args
    skip_operators = "--skip-operators" in args
    # The install is COMPLETE by default — operators, full deps (ONNX models), monitor, tray, wiring.
    # `--minimal` (alias `--no-monitor`) is the only opt-out, for headless/CI.
    # `--lite`: install skills+hooks instantly (<30s), spawn operator install in background.
    # `--strict`: preserve the current hard-BLOCK behavior (operators required immediately).
    lite = "--lite" in args
    strict = "--strict" in args
    minimal = "--minimal" in args or "--no-monitor" in args
    args = [a for a in args if a not in
            ("--global", "--skip-operators", "--with-monitor", "--minimal", "--no-monitor", "--lite", "--strict")]
    target = None
    if "--target" in args:
        i = args.index("--target")
        target = args[i + 1]
        del args[i:i + 2]
    runtime = args[0] if args else detect()
    if runtime not in RUNTIMES:
        print("unknown runtime '%s'. choices: %s" % (runtime, " ".join(RUNTIMES)))
        sys.exit(2)

    cfg = RUNTIMES[runtime]
    if is_global:
        target = {"claude": HOME, "cursor": HOME}.get(runtime, HOME)
    elif not target:
        cwd = os.getcwd()
        target = cwd if os.path.abspath(cwd) != os.path.abspath(SOURCE) else SOURCE

    print("simplicio-loop installer - runtime=%s - target=%s" % (runtime, target))
    if lite:
        log("LITE mode: installing skills+hooks instantly, operators in background...")
        # Skip operators now, spawn in background
        import threading as _threading
        def _bg_install():
            ensure_operators(skip_install=False)
        _threading.Thread(target=_bg_install, daemon=True).start()
        log("LITE mode: operator install running in background. Loop will auto-upgrade at turn boundary.")
    elif not skip_operators:
        ensure_operators(skip_install=False)
    if not minimal:
        install_all_deps()
    # Make the `simplicio-loop` console-script typeable on PATH (so `simplicio-loop dashboard` works);
    # a --user install can drop it in a dir off PATH (macOS ~/Library/Python/*/bin). Best-effort.
    _link_console_script("simplicio-loop", kind="cli")
    copy_skills(target)
    copy_hooks(target, is_global)
    install_git_precommit_hook(target)
    ensure_entry(target, cfg["entry"], runtime)
    if cfg["hooks"] == "claude":
        merge_claude_hooks(target, is_global)
    elif cfg["hooks"] == "cursor":
        log("loop hooks active via hooks/hooks.json (Cursor format)")
    elif cfg["hooks"] == "native":
        log("native runtime — extension points bind directly (no shell hooks needed)")
    else:
        log("loop runs self-paced (no stop-hook) — see adapters/%s/README.md" % runtime)
    if runtime == "opencode":
        copy_skills_opencode()
        merge_opencode_mcp()
    ensure_runtime_bind(runtime, cfg)
    if cfg["mcp"] and runtime not in FORCED_BIND_RUNTIMES:
        log("optional native bind:  simplicio install --global   (or: simplicio serve --mcp --stdio)")
    setup_monitor(not minimal)
    if lite:
        log("LITE mode active. Run `python3 scripts/doctor.py` to check install status.")
        log("When operators are ready, next loop turn auto-promotes to FULL.")
        if strict:
            log("Note: --strict has no effect in --lite mode (operators already in background).")
    log("verify / repair anytime:  python3 scripts/doctor.py --repair  (optional pieces never block)")
    print("done. use:  /simplicio-loop finish all the open issues")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""simplicio-loop — universal installer (one logic, all runtimes).

Copies the 6 skills + hooks into a target, wires the loop where the runtime supports it,
ensures the runtime's entry/instructions file references the skill, and prints the MCP-bind
line. Pure Python ->identical on Windows/macOS/Linux. Safe: create-or-merge, never clobbers
unrelated config; idempotent marker blocks.

Also installs+verifies the REQUIRED loop operator package (`simplicio-cli`) and the runtime bins
it exposes (`simplicio-dev-cli`, `simplicio-mapper`) unless --skip-operators is passed.

Usage:
    python3 scripts/install_lib.py <runtime> [--global] [--target DIR] [--skip-operators] [--lite]
    <runtime> ∈ claude codex vscode cursor antigravity kiro opencode gemini aider simplicio_agent
               openclaw orca (hermes accepted as a legacy alias for simplicio_agent)
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
# The simplicio-loop drive REQUIRES the operator package `simplicio-cli`; it exposes
# `simplicio-dev-cli` and also brings the survey binary `simplicio-mapper` transitively.
# (the bare `simplicio` command is reserved for the separate `simplicio-runtime`, not this operator.)
OPERATOR_PACKAGE = "simplicio-cli"
OPERATOR_BINS = ("simplicio-dev-cli", "simplicio-mapper")
LEGACY_MARK_A, LEGACY_MARK_B = "<!-- simplicio-tasks:begin -->", "<!-- simplicio-tasks:end -->"
MARK_A, MARK_B = "<!-- simplicio-loop:begin -->", "<!-- simplicio-loop:end -->"


def entry_block(runtime=None):
    """Build the runtime entry-file block.

    A Simplicio Runtime bind is an optional acceleration: the loop remains usable with its
    standard-tool fallback when the runtime is absent or unreachable.
    """
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
    "simplicio_agent": {"entry": None,                          "mcp": None,          "hooks": "native"},
    "hermes":      {"entry": None,                              "mcp": None,          "hooks": "native"},  # legacy alias — see simplicio_agent
    "openclaw":    {"entry": None,                              "mcp": None,          "hooks": "native"},
    # Orca (onorca.dev) — worktree IDE hosting inner agent CLIs (Claude Code/Codex/Cursor);
    # skills + AGENTS.md land in the repo and every Orca worktree sees them; loop drive is the
    # inner agent's hook where it has one, else Orca scheduled automations (self-paced).
    "orca":        {"entry": "AGENTS.md",                       "mcp": "orca",        "hooks": None},
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


def scripts_dir(target, is_global):
    # global → keep workers tidy under ~/.claude/scripts; project → ./scripts at the repo root
    return os.path.join(target, ".claude", "scripts") if is_global else os.path.join(target, "scripts")


def copy_scripts(target, is_global):
    """Copy the worker scripts (#303 AC5) into the installed target.

    SKILL.md, the hooks, and `loop_progress.py` itself are all invoked as `python3
    scripts/<worker>.py ...` — a path relative to the runtime's cwd. Without this, installing
    into a project OTHER than this source checkout (`--target <other-project>`, which is exactly
    what every runtime except a self-hosted `claude`/`cursor` project install does) leaves those
    references dangling: the skill loads, but every worker call 404s and the progress surface
    (N2 turn-header, N3 PROGRESS.md) can never actually render. Mirrors `copy_hooks`: create-or-
    merge, skip when the target already IS this source repo (nothing to copy over itself);
    `__pycache__`/`*.pyc` are never copied (stale bytecode from a different Python build).
    """
    src = os.path.join(SOURCE, "scripts")
    dst = scripts_dir(target, is_global)
    if os.path.abspath(dst) == os.path.abspath(src):
        return  # already here (project install inside this repo)
    if os.path.isdir(src):
        shutil.copytree(src, dst, dirs_exist_ok=True,
                         ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        log("scripts -> %s" % dst)


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


def _is_externally_managed_error(stderr):
    """Detect PEP 668's specific `externally-managed-environment` refusal in a pip stderr blob,
    as opposed to some unrelated failure (network down, bad package name, permission denied for
    a different reason). Only THIS specific signal should ever justify offering
    `--break-system-packages` — escalating on every failure was the unconditional blanket
    behavior issue #293 §3 asks to remove."""
    return "externally-managed-environment" in (stderr or "").lower()


def _pip_install(pkgs, *, allow_break_system_packages=False, extra_args=None, cwd=None):
    """Run `pip install -U <pkgs>` with a PEP-668-aware fallback ladder. Never passes
    `--break-system-packages` unless BOTH (a) pip's own stderr identifies this specific
    externally-managed-environment refusal and (b) the caller explicitly opted in via
    `allow_break_system_packages=True` (CLI `--allow-break-system-packages` or
    `SIMPLICIO_ALLOW_BREAK_SYSTEM_PACKAGES=1`). Returns (ok, detail) where detail is a short
    string describing which path succeeded, for logging/receipts."""
    base = [sys.executable, "-m", "pip", "install", "-U"] + list(extra_args or [])
    run_kw = {"capture_output": True, "text": True}
    if cwd:
        run_kw["cwd"] = cwd
    plain = subprocess.run(base + pkgs, **run_kw)
    if plain.returncode == 0:
        return True, "plain"
    if _is_externally_managed_error(plain.stderr):
        if not allow_break_system_packages:
            log("! pip refused (PEP 668 externally-managed environment). NOT applying "
                "--break-system-packages without explicit consent.")
            log("  safe options: use a venv/pipx/uvx, OR re-run with "
                "--allow-break-system-packages (or SIMPLICIO_ALLOW_BREAK_SYSTEM_PACKAGES=1) "
                "if you understand the risk.")
            return False, "externally_managed_blocked"
        user_run = subprocess.run(base + ["--user", "--break-system-packages"] + pkgs, **run_kw)
        if user_run.returncode == 0:
            log("! installed with --break-system-packages (explicit consent given) — this "
                "bypasses your OS package manager's protection; prefer a venv/pipx/uvx next time.")
            return True, "break_system_packages_consented"
        return False, "break_system_packages_failed"
    # Some other failure (network, permissions unrelated to PEP 668, ...): retry into the user
    # site WITHOUT --break-system-packages first — that's always safe to attempt.
    user_run = subprocess.run(base + ["--user"] + pkgs, **run_kw)
    if user_run.returncode == 0:
        return True, "user_site"
    return False, "failed"


def ensure_operators(skip_install=False, allow_break_system_packages=False):
    """Install + verify the REQUIRED operator package and the runtime bins it exposes.

    The loop still invokes `simplicio-mapper` and `simplicio-dev-cli` directly, so both binaries
    must be present on PATH at runtime; but the supported install surface is the single package
    `simplicio-cli`, which brings `simplicio-mapper` transitively.

    `allow_break_system_packages`: only when True (CLI `--allow-break-system-packages` or
    `SIMPLICIO_ALLOW_BREAK_SYSTEM_PACKAGES=1`) may a PEP-668 externally-managed refusal escalate
    to `--break-system-packages` — see `_pip_install`. Without it, a blocked install falls
    through to the `uv tool install` path or a manual-install message; it never silently mutates
    the system Python's protected package set.
    """
    pkgs = [OPERATOR_PACKAGE]
    allow_bsp = allow_break_system_packages or os.environ.get(
        "SIMPLICIO_ALLOW_BREAK_SYSTEM_PACKAGES") == "1"
    if not skip_install:
        ok, detail = _pip_install(pkgs, allow_break_system_packages=allow_bsp)
        if ok:
            log("operators installed (%s) -> %s" % (detail, ", ".join(pkgs)))
        else:
            # Try uv tool install if pip failed (uv-managed Python)
            try:
                uv_path = shutil.which("uv")
                if uv_path:
                    subprocess.run([uv_path, "tool", "install"] + pkgs, check=True)
                    log("operators installed (uv tool) -> %s" % ", ".join(pkgs))
                else:
                    raise RuntimeError("uv not on PATH")
            except Exception as e2:
                log("! pip install of operators failed (%s) — install manually: pip install %s"
                    % (detail, " ".join(pkgs)))
    # A --user install can land the console-scripts in a dir not on PATH (e.g. macOS
    # ~/Library/Python/X.Y/bin). Find each operator binary and symlink it into ~/.local/bin.
    _link_operator_bins()
    missing = [b for b in OPERATOR_BINS if shutil.which(b) is None]
    if missing:
        log("! REQUIRED operator runtime bins NOT on PATH: %s" % ", ".join(missing))
        log("  the simplicio-loop drive will BLOCK until present — run: pip install %s"
            % " ".join(pkgs))
    else:
        log("operator runtime bins verified on PATH: %s" % ", ".join(OPERATOR_BINS))


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
    """Symlink the operator console-scripts into ~/.local/bin (best-effort)."""
    for b in OPERATOR_BINS:
        _link_console_script(b, kind="operator")


# Compatibility export retained for callers that inspect the matrix. Native binds are optional
# on every runtime; an unavailable simplicio-runtime must never block a loop drive.
FORCED_BIND_RUNTIMES = set()


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


def _pip(args_, allow_break_system_packages=None):
    """pip install with a PEP-668-aware fallback into the user site. Best-effort (never raises).

    Delegates to `_pip_install` so `install_all_deps()` gets the same hardening as
    `ensure_operators()`: `--break-system-packages` is only ever attempted when pip's stderr
    specifically names the externally-managed-environment refusal AND the caller opted in.
    `allow_break_system_packages=None` reads the `SIMPLICIO_ALLOW_BREAK_SYSTEM_PACKAGES=1` env
    var (same convention as `ensure_operators`)."""
    allow_bsp = allow_break_system_packages
    if allow_bsp is None:
        allow_bsp = os.environ.get("SIMPLICIO_ALLOW_BREAK_SYSTEM_PACKAGES") == "1"
    ok, _detail = _pip_install(args_, allow_break_system_packages=allow_bsp, cwd=SOURCE)
    return ok


def install_all_deps(allow_break_system_packages=False):
    """MANDATORY full install — every capability in simplicio-loop, not opt-in. Installs the package
    with ALL extras (the ONNX models backend: onnxruntime + huggingface_hub + tokenizers + pillow) so
    `simplicio-cli kompress/router/embed/image` work, plus the menu-bar tray dep. Heavy but complete;
    `--minimal` skips it. Best-effort: a single heavy dep failing won't abort the install."""
    spec = ".[onnx]" if os.path.exists(os.path.join(SOURCE, "pyproject.toml")) else "simplicio-loop[onnx]"
    log("full install: package + ONNX models backend (%s)..." % spec)
    _pip([spec], allow_break_system_packages=allow_break_system_packages) or \
        log("! full-stack pip failed — run manually: pip install '%s'" % spec)
    tray = ["rumps"] if sys.platform == "darwin" else ["pystray", "pillow"]
    _pip(tray, allow_break_system_packages=allow_break_system_packages)


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
    install_services.py elsewhere) and routes Claude + Codex + Simplicio Agent through the proxy. The
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
    log("capture proxy always-on · Claude+Codex+Simplicio Agent measured. Re-open the UI any time:")
    log("  dashboard: simplicio-loop dashboard   (or: bash scripts/simplicio-economy.sh monitor)")
    log("  tray:      bash scripts/simplicio-economy.sh tray   ·   or just ask the agent to open it")


def _main_rollback(args):
    """`install_lib.py rollback <transaction_id> --target DIR` — undo a prior --transactional
    install from its persisted receipt. See scripts/install_executor.py."""
    target = None
    if "--target" in args:
        i = args.index("--target")
        target = args[i + 1]
        del args[i:i + 2]
    if not args:
        print("usage: install_lib.py rollback <transaction_id> --target DIR")
        sys.exit(2)
    transaction_id = args[0]
    target = target or os.getcwd()
    sys.path.insert(0, HERE)
    import install_executor
    try:
        receipt = install_executor.rollback(transaction_id, target)
    except FileNotFoundError as e:
        print("! %s" % e)
        sys.exit(3)
    print(json.dumps(receipt, indent=2, sort_keys=True, default=str))
    sys.exit(0)


def main():
    args = sys.argv[1:]
    if args and args[0] == "rollback":
        _main_rollback(args[1:])
        return
    is_global = "--global" in args
    skip_operators = "--skip-operators" in args
    # The install is COMPLETE by default — operators, full deps (ONNX models), monitor, tray, wiring.
    # `--minimal` (alias `--no-monitor`) is the only opt-out, for headless/CI.
    # `--lite`: install skills+hooks instantly (<30s), spawn operator install in background.
    # `--strict`: preserve the current hard-BLOCK behavior (operators required immediately).
    # `--dry-run`: build and print/return the simplicio.install-transaction/v1 plan (#293
    #   first slice) and exit WITHOUT any mutation — no skills copy, no operator install,
    #   no hooks/entry wiring, no monitor setup.
    # `--transactional`: apply the file effects (skills/hooks/scripts/entry/settings) through
    #   scripts/install_executor.py — backup-before-mutate + a persisted receipt + automatic
    #   rollback of everything already applied if any step fails partway (#293 step 5).
    lite = "--lite" in args
    strict = "--strict" in args
    minimal = "--minimal" in args or "--no-monitor" in args
    dry_run = "--dry-run" in args
    transactional = "--transactional" in args
    # #293 §3 hardening: --break-system-packages is NEVER applied unconditionally. This flag
    # (or SIMPLICIO_ALLOW_BREAK_SYSTEM_PACKAGES=1) is the one explicit opt-in that lets a PEP-668
    # externally-managed refusal escalate to it; see _pip_install()/ensure_operators() above.
    allow_break_system_packages = "--allow-break-system-packages" in args
    test_fail_step = None
    if "--test-fail-step" in args:
        i = args.index("--test-fail-step")
        test_fail_step = args[i + 1]
        del args[i:i + 2]
    args = [a for a in args if a not in
            ("--global", "--skip-operators", "--with-monitor", "--minimal", "--no-monitor",
             "--lite", "--strict", "--dry-run", "--transactional", "--allow-break-system-packages")]
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

    if dry_run:
        import json as _json
        sys.path.insert(0, HERE)
        from install_plan import build_plan  # local import: keeps install_plan.py import-light/standalone
        plan = build_plan(runtime, mode="minimal", scope=("user" if is_global else "project"), target=target)
        print(_json.dumps(plan, indent=2, sort_keys=True))
        sys.exit(0 if plan["status"] != "BLOCKED" else 3)

    print("simplicio-loop installer - runtime=%s - target=%s" % (runtime, target))
    if lite:
        log("LITE mode: installing skills+hooks instantly, operators in background...")
        # Skip operators now, spawn in background
        import threading as _threading
        def _bg_install():
            ensure_operators(skip_install=False,
                            allow_break_system_packages=allow_break_system_packages)
        _threading.Thread(target=_bg_install, daemon=True).start()
        log("LITE mode: operator install running in background. Loop will auto-upgrade at turn boundary.")
    elif not skip_operators:
        ensure_operators(skip_install=False,
                        allow_break_system_packages=allow_break_system_packages)
    if not minimal:
        install_all_deps(allow_break_system_packages=allow_break_system_packages)
    # Make the `simplicio-loop` console-script typeable on PATH (so `simplicio-loop dashboard` works);
    # a --user install can drop it in a dir off PATH (macOS ~/Library/Python/*/bin). Best-effort.
    _link_console_script("simplicio-loop", kind="cli")
    if transactional:
        sys.path.insert(0, HERE)
        import install_executor
        try:
            receipt = install_executor.apply(runtime, target=target, is_global=is_global,
                                            mode="minimal", fail_step=test_fail_step)
        except install_executor.InstallTransactionError as e:
            log("! transaction ROLLED_BACK (no partial state left): %s" % e)
            log("  receipt: %s" % os.path.join(target, ".simplicio", "receipts",
                                                e.receipt["transaction_id"] + ".json"))
            sys.exit(4)
        if receipt["status"] == "BLOCKED":
            log("! plan BLOCKED before any mutation — missing consent: %s"
                % ", ".join(receipt["permissions_required"]))
            sys.exit(3)
        log("transactional install APPLIED -> %s (transaction %s)"
            % (target, receipt["transaction_id"]))
        log("  rollback anytime:  python3 scripts/install_lib.py rollback %s --target %s"
            % (receipt["transaction_id"], target))
    else:
        copy_skills(target)
        copy_hooks(target, is_global)
        copy_scripts(target, is_global)
        ensure_entry(target, cfg["entry"], runtime)
        if cfg["hooks"] == "claude":
            merge_claude_hooks(target, is_global)
    install_git_precommit_hook(target)
    if cfg["hooks"] == "cursor":
        log("loop hooks active via hooks/hooks.json (Cursor format)")
    elif cfg["hooks"] == "native":
        log("native runtime — extension points bind directly (no shell hooks needed)")
    elif cfg["hooks"] != "claude":
        log("loop runs self-paced (no stop-hook) — see adapters/%s/README.md" % runtime)
    if runtime == "opencode":
        copy_skills_opencode()
        merge_opencode_mcp()
    if cfg["mcp"]:
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

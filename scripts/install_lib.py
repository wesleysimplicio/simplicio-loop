#!/usr/bin/env python3
"""simplicio-tasks — universal installer (one logic, all runtimes).

Copies the 6 skills + hooks into a target, wires the loop where the runtime supports it,
ensures the runtime's entry/instructions file references the skill, and prints the MCP-bind
line. Pure Python ->identical on Windows/macOS/Linux. Safe: create-or-merge, never clobbers
unrelated config; idempotent marker blocks.

Also installs+verifies the two REQUIRED loop operators (simplicio-mapper, simplicio-cli) unless
--skip-operators is passed.

Usage:
    python3 scripts/install_lib.py <runtime> [--global] [--target DIR] [--skip-operators]
    <runtime> ∈ claude codex vscode cursor antigravity kiro opencode gemini aider hermes openclaw
    omit <runtime> to auto-detect.
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
#   simplicio-cli    -> action operator (binds `execute`/`deterministic_edit`); binary: simplicio
OPERATORS = [("simplicio-mapper", "simplicio-mapper"), ("simplicio-cli", "simplicio")]
MARK_A, MARK_B = "<!-- simplicio-tasks:begin -->", "<!-- simplicio-tasks:end -->"
ENTRY_BLOCK = (
    MARK_A + "\n"
    "## simplicio-tasks — Universal Looping Orchestrator\n\n"
    "Load and follow the protocol in `.claude/skills/simplicio-tasks/SKILL.md` and its "
    "companion skills (`simplicio-loop`, `simplicio-orient`, `simplicio-review`, "
    "`simplicio-compress`, `simplicio-learn`). Run commands for real; clamp heavy output via "
    "`python3 hooks/orient_clamp.py -- <cmd>`; never close work without a merged PR or "
    "concrete evidence; honor the cost kill-switch and the irreversible-op human gate.\n\n"
    "Invoke with: `/simplicio-tasks <the body of work>`\n"
    + MARK_B
)

# entry file + MCP client id per runtime; None entry = no instructions file needed
RUNTIMES = {
    "claude":      {"entry": None,                              "mcp": "claude-code", "hooks": "claude"},
    "codex":       {"entry": "AGENTS.md",                       "mcp": "codex",       "hooks": None},
    "vscode":      {"entry": ".github/copilot-instructions.md", "mcp": "vscode",      "hooks": None},
    "cursor":      {"entry": None,                              "mcp": "cursor",      "hooks": "cursor"},
    "antigravity": {"entry": "AGENTS.md",                       "mcp": "antigravity", "hooks": None},
    "kiro":        {"entry": ".kiro/steering/simplicio-tasks.md","mcp": "kiro",       "hooks": None},
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


def ensure_entry(target, rel):
    if not rel:
        return
    path = os.path.join(target, rel)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    existing = ""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            existing = f.read()
    if MARK_A in existing:
        # refresh the block in place
        pre = existing.split(MARK_A)[0]
        post = existing.split(MARK_B, 1)[1] if MARK_B in existing else ""
        new = pre.rstrip() + "\n\n" + ENTRY_BLOCK + post
    else:
        new = (existing.rstrip() + "\n\n" if existing.strip() else "") + ENTRY_BLOCK + "\n"
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
            {"type": "command", "command": cmd("learn_stop.py")},
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

    The simplicio-loop drive surveys via `simplicio-mapper` and acts via `simplicio` instead of
    the LLM, so both must be present. pip-install (unless skipped), then verify the binaries are on
    PATH. Missing binary after install is a hard error — the loop would BLOCK at runtime otherwise.
    """
    pkgs = [pkg for pkg, _ in OPERATORS]
    if not skip_install:
        try:
            subprocess.run([sys.executable, "-m", "pip", "install", "-U", *pkgs],
                           check=True)
            log("operators installed -> %s" % ", ".join(pkgs))
        except Exception as e:
            log("! pip install of operators failed (%s) — install manually: pip install %s"
                % (e, " ".join(pkgs)))
    missing = [b for _, b in OPERATORS if shutil.which(b) is None]
    if missing:
        log("! REQUIRED loop operators NOT on PATH: %s" % ", ".join(missing))
        log("  the simplicio-loop drive will BLOCK until present — run: pip install %s"
            % " ".join(pkgs))
    else:
        log("operators verified on PATH: %s" % ", ".join(b for _, b in OPERATORS))


def detect():
    for rt, mark in [("cursor", ".cursor"), ("claude", ".claude"),
                     ("kiro", ".kiro"), ("vscode", ".github"), ("gemini", ".gemini")]:
        if os.path.isdir(os.path.join(os.getcwd(), mark)):
            return rt
    return "claude"


def main():
    args = sys.argv[1:]
    is_global = "--global" in args
    skip_operators = "--skip-operators" in args
    args = [a for a in args if a not in ("--global", "--skip-operators")]
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

    print("simplicio-tasks installer - runtime=%s - target=%s" % (runtime, target))
    ensure_operators(skip_install=skip_operators)
    copy_skills(target)
    copy_hooks(target, is_global)
    ensure_entry(target, cfg["entry"])
    if cfg["hooks"] == "claude":
        merge_claude_hooks(target, is_global)
    elif cfg["hooks"] == "cursor":
        log("loop hooks active via hooks/hooks.json (Cursor format)")
    elif cfg["hooks"] == "native":
        log("native runtime — extension points bind directly (no shell hooks needed)")
    else:
        log("loop runs self-paced (no stop-hook) — see adapters/%s/README.md" % runtime)
    if cfg["mcp"]:
        log("optional native bind:  simplicio mcp register --client %s" % cfg["mcp"])
    print("done. use:  /simplicio-tasks finish all the open issues")


if __name__ == "__main__":
    main()

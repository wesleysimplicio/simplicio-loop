#!/usr/bin/env python3
"""simplicio-tasks / simplicio-loop — video_evidence worker (demo-video proof via hyperframes).

The runnable form of the `video_evidence` extension point documented in
`.claude/skills/simplicio-tasks/references/video-evidence.md`. Produces a **deterministic MP4**
demonstration video of a screen/feature with **hyperframes** (https://github.com/heygen-com/hyperframes)
and records it as evidence that a change works. Evidence is ALWAYS a file path + a boolean verdict —
never the video bytes, frames, or HTML are fed back into the model context (token economy).

Why hyperframes: it renders HTML/CSS/media compositions to MP4 deterministically — "same input,
same frames, same output" — so the demo video is reproducible in CI and is a trustworthy artifact
of "works, not just compiles" (Step 4b). No API keys; local render via headless Chrome + FFmpeg.

Five verbs:

  detect    Cheap intent gate (no toolchain). Decide whether a goal/work-item is a
            video-creation request (e.g. `/simplicio-tasks make a demo video of screen X`).
            Pass the request text via --goal. Exit 0 + "video-task" when it is; exit 0 + "skip"
            otherwise. Pass --exit-code to instead exit 10 when it IS a video task (for CI `if:`).
  scaffold  `npx hyperframes init <project>` then write a composition that turns the captured
            walkthrough screenshots (--frames DIR or --shots a.png,b.png) into a timed MP4 demo.
  render    `npx hyperframes render` the composition to MP4 under the evidence dir.
  lint      `npx hyperframes lint` — validate the composition before rendering.
  verify    scaffold (if needed) → render → append a ledger row → print the MACHINE-tier verdict.
            This is the gate Step 4b / the loop's evidence gate calls. A missing toolchain
            (Node 22+, FFmpeg, hyperframes) yields BLOCKED, never a fake pass.

Pairs with web_verify.py: web_verify drives the real UI and captures per-step screenshots under
`.orchestrator/tee/web/`; video_evidence assembles those exact screenshots into a narrated,
deterministic MP4 walkthrough — the demo video the user asked for AND the on-screen proof.

Usage:
    python3 scripts/video_evidence.py detect --goal "make a demo video of the login screen" [--exit-code]
    python3 scripts/video_evidence.py scaffold --name login-demo \\
        --frames .orchestrator/tee/web --title "Login screen" [--seconds 2.0]
    python3 scripts/video_evidence.py render --name login-demo [--issue 12]
    python3 scripts/video_evidence.py verify --name login-demo --frames .orchestrator/tee/web \\
        --title "Login screen" [--issue 12] [--upload --pr N]
"""
import glob
import os
import re
import shutil
import subprocess
import sys

try:  # Windows consoles default to cp1252 and choke on non-ASCII — force UTF-8.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DEFAULT_OUT = os.path.join(REPO, ".orchestrator", "tee", "video")
# Multilingual intent matcher for "create a demonstration video of screen/feature X".
# Matches a video noun near a make/show/demo verb (EN/PT/ES). Deliberately broad but anchored
# on an explicit video noun so a normal code task never trips it.
VIDEO_NOUN = r"v[ií]deo|video|screencast|screen[- ]?recording|walkthrough|demo\s+reel|gif"
VIDEO_VERB = (r"fa[çc]a|fazer|crie|criar|gere|gerar|grav[ae]r?|demonstr|"
              r"make|create|record|generate|produce|show|capture|demo")
VIDEO_RE = re.compile(r"(?=.*(%s))(?=.*(%s))" % (VIDEO_NOUN, VIDEO_VERB), re.I | re.S)


def log(msg):
    print("  " + msg)


def _exe(name):
    """Resolve an executable on PATH (finds npx.cmd/ffmpeg.exe on Windows); fall back to the name."""
    return shutil.which(name) or name


def _run(argv, **kw):
    """Run a command WITHOUT a shell. Returns the CompletedProcess, or None if the exe is absent."""
    try:
        return subprocess.run([_exe(argv[0])] + argv[1:], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", **kw)
    except FileNotFoundError:
        return None


def is_video_request(text):
    """True when the goal/work-item text asks for a demonstration video (terminal, not LLM)."""
    return bool(text) and bool(VIDEO_RE.search(text))


def cmd_detect(opts):
    goal = opts.get("goal", "") or os.environ.get("SIMPLICIO_GOAL", "")
    if is_video_request(goal):
        print("video-task: hyperframes demo-video request detected")
        log("goal matched the video-evidence intent — route to the video_evidence producer")
        if opts.get("exit-code"):
            sys.exit(10)
    else:
        print("skip: not a video-creation request")


def _proj_dir(name, out):
    return os.path.join(out, name)


def _node_major():
    r = _run(["node", "--version"])
    if r is None or r.returncode != 0:
        return None
    m = re.search(r"v(\d+)\.", r.stdout or "")
    return int(m.group(1)) if m else None


def _collect_shots(opts):
    """Resolve the ordered list of screenshot PNGs to assemble into the demo."""
    shots = []
    if opts.get("shots"):
        shots = [s.strip() for s in str(opts["shots"]).split(",") if s.strip()]
    elif opts.get("frames"):
        frames = opts["frames"]
        if not os.path.isabs(frames):
            frames = os.path.join(REPO, frames)
        shots = sorted(glob.glob(os.path.join(frames, "*.png")))
    return [s for s in shots if os.path.exists(s)]


# hyperframes composition (schema v0.7.x): the project's `index.html` carries a `#root` wrapper
# with `data-composition-id` + `data-duration`/`data-width`/`data-height`; each screenshot is one
# timed `.clip` (the framework gates a clip's visibility by its `data-start`/`data-duration`
# window), and a paused GSAP timeline registered on `window.__timelines[<id>]` fades each scene in.
# Deterministic: no Date.now()/Math.random()/runtime fetches (GSAP is a static CDN <script>).
COMPOSITION = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width={width}, height={height}" />
  <script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"></script>
  <style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    html, body {{ width: {width}px; height: {height}px; overflow: hidden; background: #0b0f17; }}
    body {{ font-family: "Inter", system-ui, Segoe UI, Roboto, sans-serif; color: #e6edf3; }}
    .clip {{ position: absolute; inset: 0; display: flex; flex-direction: column;
             align-items: center; justify-content: center; gap: 22px; }}
    .clip img {{ max-width: 90%; max-height: 78%; border-radius: 12px;
                 box-shadow: 0 16px 50px rgba(0,0,0,.55); border: 1px solid #1f2733; }}
    .cap {{ font-size: 27px; font-weight: 600; letter-spacing: .2px;
            padding: 0 48px; text-align: center; }}
    .brand {{ position: absolute; left: 26px; bottom: 18px; font-size: 14px; opacity: .5; }}
  </style>
</head>
<body>
  <div id="root" data-composition-id="main" data-start="0" data-duration="{duration}"
       data-width="{width}" data-height="{height}">
{clips}
    <div class="brand">{brand}</div>
  </div>
  <script>
    window.__timelines = window.__timelines || {{}};
    const tl = gsap.timeline({{ paused: true }});
{fades}
    window.__timelines["main"] = tl;
  </script>
</body>
</html>
"""

CLIP = ('      <div class="clip" id="s{n}" data-start="{start}" data-duration="{dur}" '
        'data-track-index="1">\n'
        '        <img src="assets/{asset}" alt="{alt}" />\n'
        '        <div class="cap">{cap}</div>\n'
        '      </div>')

FADE = '    tl.from("#s{n}", {{ opacity: 0, duration: 0.4 }}, {start});'


def cmd_scaffold(opts):
    name = opts.get("name", "simplicio-demo")
    out = opts.get("out", DEFAULT_OUT)
    title = opts.get("title", "Demo")
    seconds = float(opts.get("seconds", 2.0))
    os.makedirs(out, exist_ok=True)
    proj = _proj_dir(name, out)

    shots = _collect_shots(opts)
    if not shots:
        log("! no screenshots found (--frames DIR / --shots a.png,b.png). Run web_verify first "
            "to capture per-step shots into .orchestrator/tee/web/.")
        sys.exit(2)

    # `npx hyperframes init` scaffolds the project skeleton; best-effort (we still write our own
    # index.html + assets below, so the worker is useful even if init is unavailable).
    if not os.path.isdir(proj):
        r = _run(["npx", "--yes", "hyperframes", "init", name, "--non-interactive",
                  "--skip-skills", "--skip-transcribe"], cwd=out)
        if r is None:
            log("! npx not found — install Node.js 22+ to use hyperframes")
    assets = os.path.join(proj, "assets")
    os.makedirs(assets, exist_ok=True)

    # hyperframes renders the project over a local file server, so every asset MUST live inside
    # the project tree (a ../.. path back to the capture dir would not resolve). Copy each shot
    # into assets/ in capture order; each becomes one timed .clip in the index.html timeline.
    clips, fades, start = [], [], 0.0
    for i, s in enumerate(shots, 1):
        asset = "frame%02d.png" % i
        shutil.copy2(s, os.path.join(assets, asset))
        cap = _pretty_cap(title, s)
        clips.append(CLIP.format(n=i, start=_fmt(start), dur=_fmt(seconds), asset=asset,
                                 alt=_esc(os.path.splitext(asset)[0]), cap=_esc(cap)))
        fades.append(FADE.format(n=i, start=_fmt(start)))
        start = round(start + seconds, 3)
    width = int(opts.get("width", 1280))
    height = int(opts.get("height", 720))
    brand = opts.get("brand", "github.com/wesleysimplicio/simplicio-loop")
    html = COMPOSITION.format(width=width, height=height, duration=_fmt(start),
                              clips="\n".join(clips), fades="\n".join(fades), brand=_esc(brand))
    comp_path = os.path.join(proj, "index.html")
    with open(comp_path, "w", encoding="utf-8") as f:
        f.write(html)
    log("scaffolded %d-scene composition (%.1fs) -> %s" % (len(shots), start, comp_path))
    print("scaffolded")


def _esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _fmt(x):
    """Seconds → compact HTML attr value (2.0 -> '2', 12.0 -> '12', 2.5 -> '2.5')."""
    return "%g" % float(x)


def _pretty_cap(title, path):
    """Caption from the shot filename: drop the ordering prefix, humanize, keep ACRONYMS."""
    base = os.path.splitext(os.path.basename(path))[0]
    base = re.sub(r"^\d+[-_.]+", "", base)            # strip a leading "03-" ordering prefix
    label = re.sub(r"[-_]+", " ", base).strip()
    label = " ".join(w if w.isupper() else w.capitalize() for w in label.split())
    return ("%s · %s" % (title, label)) if (title and label) else (label or title)


def _append_ledger(out, line):
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "ledger.txt"), "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _blocked(out, msg):
    _append_ledger(out, "video_evidence: BLOCKED — " + msg)
    print("blocked")
    log(msg)
    sys.exit(3)


def cmd_lint(opts):
    name = opts.get("name", "simplicio-demo")
    out = opts.get("out", DEFAULT_OUT)
    proj = _proj_dir(name, out)
    r = _run(["npx", "--yes", "hyperframes", "lint"], cwd=proj)
    if r is None:
        _blocked(out, "npx not found — install Node.js 22+ for hyperframes")
    ok = r.returncode == 0
    print("done" if ok else "fail")
    log((r.stdout or r.stderr or "").strip()[:400])
    sys.exit(0 if ok else 1)


def cmd_render(opts):
    name = opts.get("name", "simplicio-demo")
    out = opts.get("out", DEFAULT_OUT)
    issue = str(opts.get("issue", "x"))
    proj = _proj_dir(name, out)
    comp = os.path.join(proj, "index.html")

    # Preflight the toolchain — BLOCK (never fake-pass) when it is missing.
    if _exe("npx") == "npx" and shutil.which("npx") is None:
        _blocked(out, "Node.js/npx not found — hyperframes needs Node 22+ "
                      "(https://github.com/heygen-com/hyperframes)")
    nm = _node_major()
    if nm is not None and nm < 22:
        _blocked(out, "Node %d detected — hyperframes requires Node 22+" % nm)
    if shutil.which("ffmpeg") is None:
        _blocked(out, "FFmpeg not found — hyperframes renders MP4 via FFmpeg; install it first")
    if not os.path.exists(comp):
        _blocked(out, "no composition at %s — run `scaffold` first" % comp)

    mp4 = os.path.join(out, "%s-%s.mp4" % (name, issue))
    fps = str(opts.get("fps", 30))
    quality = str(opts.get("quality", "standard"))
    # hyperframes render takes the PROJECT DIR positionally and renders its index.html.
    cmd = ["npx", "--yes", "hyperframes", "render", proj, "-o", mp4, "-f", fps, "-q", quality]
    log("rendering: %s" % " ".join(cmd))
    r = _run(cmd, cwd=proj)
    if r is None:
        _blocked(out, "npx not found — install Node.js 22+ for hyperframes")
    stderr = (r.stderr or "").lower()
    if r.returncode != 0 and ("hyperframes" in stderr and ("not found" in stderr or "404" in stderr)):
        _blocked(out, "hyperframes package not resolvable — check network/registry access")
    ok = r.returncode == 0 and os.path.exists(mp4)
    verdict = "video_evidence: %s — demo MP4 (hyperframes) project=%s mp4=%s" % (
        "PASS" if ok else "FAIL", name, mp4 if ok else "(not produced)")
    _append_ledger(out, verdict)
    print("done" if ok else "fail")
    log(verdict)
    if ok and opts.get("upload") and opts.get("pr"):
        _upload(out, str(opts["pr"]), mp4)
    sys.exit(0 if ok else 1)


def _upload(out, pr, mp4):
    """Attach the demo video to the PR as a LINK (never bytes). Best-effort."""
    tag = "evidence-pr%s" % pr
    rel = _run(["gh", "release", "create", tag, "--notes", "video_evidence demo", mp4], cwd=REPO)
    if rel is None:
        log("! gh not found — skipping upload (MP4 remains at %s)" % mp4)
        return
    if rel.returncode != 0:  # release may already exist — upload into it
        _run(["gh", "release", "upload", tag, mp4, "--clobber"], cwd=REPO)
    body = ("🎬 video_evidence ✅  demo video rendered with "
            "[hyperframes](https://github.com/heygen-com/hyperframes) attached to release `%s`" % tag)
    _run(["gh", "pr", "comment", pr, "--body", body], cwd=REPO)
    log("uploaded demo video -> release %s, commented on PR #%s" % (tag, pr))


def cmd_verify(opts):
    out = opts.get("out", DEFAULT_OUT)
    name = opts.get("name", "simplicio-demo")
    proj = _proj_dir(name, out)
    if not os.path.exists(os.path.join(proj, "index.html")):
        cmd_scaffold(opts)  # exits non-zero if no shots are available
    cmd_render(opts)


def _parse(args):
    opts = {}
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("--"):
            key = a[2:]
            if i + 1 < len(args) and not args[i + 1].startswith("--"):
                opts[key] = args[i + 1]
                i += 2
            else:
                opts[key] = True
                i += 1
        else:
            i += 1
    return opts


def main():
    argv = sys.argv[1:]
    if not argv:
        print(__doc__)
        sys.exit(2)
    sub, opts = argv[0], _parse(argv[1:])
    {"detect": cmd_detect, "scaffold": cmd_scaffold, "render": cmd_render,
     "lint": cmd_lint, "verify": cmd_verify}.get(
        sub, lambda _o: (print("unknown command '%s'. choices: detect scaffold render lint verify"
                               % sub), sys.exit(2)))(opts)


if __name__ == "__main__":
    main()

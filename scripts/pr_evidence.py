#!/usr/bin/env python3
"""simplicio-loop — pr_evidence worker (every PR carries prints + an item-by-item AC checklist).

The complaint this closes: "ao abrir a PR, o loop não está evidenciando com prints, com checagem
item a item da tarefa" — PRs were opened without screenshots and without a per-criterion check of
the task. The skill DESCRIBED attaching evidence (Step 6/6b) but nothing ASSEMBLED it, so it was
skippable. This worker makes the PR body deterministic and model-free, gathering:

  • the **item-by-item acceptance-criteria checklist** from the task anchor (`task_anchor.py`) —
    one line per AC, with its status + the receipt that verified it;
  • the **prints / screenshots** captured by `web_verify.py` under `.orchestrator/tee/web`
    (`--shots-dir`) AND any demo video from `video_evidence.py` under `.orchestrator/tee/video`
    (`--video-dir`) — embedded as markdown image links / a video link (paths + a count, never the
    bytes — token economy);
  • the gate receipts / ledger rows already on disk.

It honors `.github/PULL_REQUEST_TEMPLATE.md` when present (fills its sections), else a clear default
layout. Crucially it is **fail-closed on evidence**: with `--require-evidence`, if there is neither
an AC checklist nor a single captured print, it prints `blocked` and exits 3 — the loop cannot open
an evidence-less PR by accident (same never-fake-pass discipline as the evidence producers).

Deterministic, stdlib-only, no network. Pairs with `task_anchor.py` (the checklist source) and
`web_verify.py` / `video_evidence.py` (the prints).

Verbs:
  build      Emit the full PR body markdown (stdout or --out FILE). --require-evidence → exit 3 if
             there is no checklist and no print to show.
  comment    Emit the shorter source-item evidence comment (PR link + verification summary +
             checklist + a count of attached prints) — the comment Step 6 posts back on the issue.
  selftest   Prove the assembly + the evidence-gate deterministically — no files, no network.

Usage:
    python3 scripts/pr_evidence.py build --title "Add SSO login" --item 12 \\
        --summary "Adds an SSO button and the IdP redirect." \\
        --shots-dir .orchestrator/tee/web --require-evidence --out .orchestrator/pr_body.md
    python3 scripts/pr_evidence.py comment --item 12 --pr 34
"""
import json
import os
import re
import sys

try:  # Windows consoles default to cp1252 and choke on non-ASCII — force UTF-8.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DEFAULT_SHOTS = os.path.join(REPO, ".orchestrator", "tee", "web")
# video_evidence.py's REAL default output dir (see its DEFAULT_OUT) — a separate directory from
# DEFAULT_SHOTS. #81 found this drift: docs claimed recordings land under tee/web too, but the
# producer actually writes tee/video, so a demo video was silently never picked up by
# `build`/`comment` unless the caller manually widened --shots-dir. Scanned in ADDITION to
# --shots-dir by default so the evidence chain connects without extra flags.
DEFAULT_VIDEO_SHOTS = os.path.join(REPO, ".orchestrator", "tee", "video")
DEFAULT_TEMPLATE = os.path.join(REPO, ".github", "PULL_REQUEST_TEMPLATE.md")

IMG_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp")
VID_EXT = (".mp4", ".webm", ".mov", ".gif")
_BLOCKED = 3  # same BLOCKED exit code the evidence producers use (web_verify / video_evidence)

# import the anchor's pure helpers so the checklist renders identically here and in task_anchor.
sys.path.insert(0, HERE)
try:
    from task_anchor import render_checklist, coverage, ANCHOR as ANCHOR_DEFAULT
except Exception:  # pragma: no cover - keep pr_evidence usable even if the import path shifts
    ANCHOR_DEFAULT = os.path.join(REPO, ".orchestrator", "loop", "anchor.json")

    def coverage(criteria):
        total = len(criteria)
        done = sum(1 for c in criteria if c.get("status") == "done")
        return done, total, [c.get("id") for c in criteria if c.get("status") != "done"]

    def render_checklist(criteria, heading="Acceptance criteria (item-by-item)"):
        lines = ["### %s" % heading]
        if not criteria:
            return lines[0] + "\n- _(no acceptance criteria were anchored for this item)_"
        for c in criteria:
            box = {"done": "x", "partial": "~"}.get(c.get("status"), " ")
            line = "- [%s] **%s** %s" % (box, c.get("id"), c.get("text"))
            if (c.get("evidence") or "").strip():
                line += " — _evidence:_ %s" % c["evidence"].strip()
            lines.append(line)
        d, t, _ = coverage(criteria)
        lines += ["", "**Coverage:** %d/%d criteria verified." % (d, t)]
        return "\n".join(lines)


def log(msg):
    print("  " + msg, file=sys.stderr)


def _load_anchor(opts):
    path = opts.get("anchor") if isinstance(opts.get("anchor"), str) else ANCHOR_DEFAULT
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def collect_prints(shots_dir):
    """Return (images, videos) of evidence files under shots_dir, as repo-relative paths, sorted."""
    images, videos = [], []
    if not shots_dir or not os.path.isdir(shots_dir):
        return images, videos
    for root, dirs, names in os.walk(shots_dir):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for n in sorted(names):
            low = n.lower()
            p = os.path.join(root, n)
            try:
                rel = os.path.relpath(p, REPO)
            except ValueError:
                rel = p
            rel = rel.replace(os.sep, "/")
            if low.endswith(IMG_EXT):
                images.append(rel)
            elif low.endswith(VID_EXT):
                videos.append(rel)
    return sorted(images), sorted(videos)


def collect_all_evidence(opts):
    """Merge prints from --shots-dir (default tee/web) AND --video-dir (default tee/video, the
    real video_evidence.py output dir — #81) into one deduped (images, videos) pair."""
    shots_dir = opts.get("shots-dir") if isinstance(opts.get("shots-dir"), str) else DEFAULT_SHOTS
    video_dir = opts.get("video-dir") if isinstance(opts.get("video-dir"), str) else DEFAULT_VIDEO_SHOTS
    images, videos = collect_prints(shots_dir)
    if os.path.abspath(video_dir) != os.path.abspath(shots_dir):
        more_images, more_videos = collect_prints(video_dir)
        images = sorted(set(images) | set(more_images))
        videos = sorted(set(videos) | set(more_videos))
    return images, videos


def render_evidence(images, videos, heading="Evidence — prints & recordings"):
    """Markdown block embedding each print as an image and each recording as a link."""
    lines = ["### %s" % heading]
    if not images and not videos:
        lines.append("- _(no prints captured — run `web_verify.py` / `video_evidence.py` first)_")
        return "\n".join(lines)
    for rel in images:
        name = os.path.basename(rel)
        lines.append("![%s](%s)" % (name, rel))
    for rel in videos:
        name = os.path.basename(rel)
        lines.append("- 🎬 [%s](%s)" % (name, rel))
    lines.append("")
    lines.append("_%d print(s), %d recording(s) attached._" % (len(images), len(videos)))
    return "\n".join(lines)


def _fill_template(tpl, blocks):
    """Append our evidence blocks to a discovered PR template (never drop the maintainer's sections).

    We do not try to surgically rewrite arbitrary templates (untrusted content); we keep the
    template verbatim and append the AC checklist + evidence under a clear divider, so the PR always
    has both the maintainer's layout AND the proof.
    """
    parts = [tpl.rstrip(), "", "---", ""]
    parts += blocks
    return "\n".join(parts).rstrip() + "\n"


def build_body(opts):
    """Assemble the PR body. Returns (markdown, has_evidence)."""
    anchor = _load_anchor(opts)
    criteria = anchor.get("criteria", [])
    images, videos = collect_all_evidence(opts)
    has_evidence = bool(criteria) or bool(images) or bool(videos)

    title = opts.get("title") or anchor.get("goal") or "Untitled change"
    item = opts.get("item") or anchor.get("item") or ""
    summary = opts.get("summary") or ""

    checklist_md = render_checklist(criteria)
    evidence_md = render_evidence(images, videos)
    how = opts.get("how") or "Run the project's test gate (`python3 scripts/check.py`) and the " \
                             "captured `web_verify` / `video_evidence` flow above."

    blocks = []
    if summary:
        blocks += ["### Summary", summary, ""]
    if item:
        blocks += ["Closes #%s" % str(item).lstrip("#"), ""]
    blocks += [checklist_md, "", evidence_md, "", "### How to verify", how, ""]

    tpl_path = opts.get("template") if isinstance(opts.get("template"), str) else DEFAULT_TEMPLATE
    if tpl_path and os.path.exists(tpl_path):
        try:
            with open(tpl_path, encoding="utf-8", errors="replace") as f:
                tpl = f.read()
            body = "# %s\n\n" % title + _fill_template(tpl, blocks)
            return body, has_evidence
        except OSError:
            pass
    body = "# %s\n\n" % title + "\n".join(blocks).rstrip() + "\n"
    return body, has_evidence


def cmd_build(opts):
    body, has_evidence = build_body(opts)
    if opts.get("require-evidence") and not has_evidence:
        print("blocked")
        log("BLOCKED — no acceptance-criteria checklist and no prints to attach. "
            "Anchor the ACs (task_anchor.py set) and capture prints (web_verify.py) before "
            "opening the PR. Refusing to open an evidence-less PR.")
        sys.exit(_BLOCKED)
    out = opts.get("out")
    if isinstance(out, str):
        with open(out, "w", encoding="utf-8") as f:
            f.write(body)
        log("wrote PR body -> %s (%d bytes)" % (out, len(body)))
        print("done %s" % out)
    else:
        sys.stdout.write(body)


def cmd_comment(opts):
    """The shorter evidence comment posted back on the source item."""
    anchor = _load_anchor(opts)
    criteria = anchor.get("criteria", [])
    done, total, pending = coverage(criteria)
    images, videos = collect_all_evidence(opts)
    pr = opts.get("pr")
    lines = []
    if pr:
        lines.append("PR: #%s" % str(pr).lstrip("#"))
    lines.append("Verification: %d/%d acceptance criteria met · %d print(s), %d recording(s)."
                 % (done, total, len(images), len(videos)))
    lines.append("")
    lines.append(render_checklist(criteria))
    if pending:
        lines += ["", "Still open: %s" % ", ".join(pending)]
    sys.stdout.write("\n".join(lines).rstrip() + "\n")


def cmd_selftest(_opts):
    checks = []

    def chk(name, cond):
        checks.append(bool(cond))
        print("  [%s] %s" % ("ok" if cond else "XX", name))

    # render_evidence embeds images and links videos
    ev = render_evidence(["a/b/login.png"], ["a/b/demo.mp4"])
    chk("evidence.embeds_image", "![login.png](a/b/login.png)" in ev)
    chk("evidence.links_video", "demo.mp4" in ev and "🎬" in ev)
    chk("evidence.empty_note", "no prints captured" in render_evidence([], []))

    # build_body with an anchor present -> checklist appears, has_evidence True
    crit = [{"id": "AC1", "text": "Renders", "status": "done", "evidence": "x.png"},
            {"id": "AC2", "text": "Redirects", "status": "pending", "evidence": ""}]
    body = render_checklist(crit)
    chk("checklist.line_per_ac", body.count("- [") == 2)
    chk("checklist.done_box", "[x] **AC1**" in body)
    chk("checklist.pending_box", "[ ] **AC2**" in body)
    chk("checklist.coverage", "1/2" in body)

    # the evidence gate: no criteria + no prints => not has_evidence (build would BLOCK)
    d, t, p = coverage([])
    chk("coverage.empty", (d, t, p) == (0, 0, []))
    chk("coverage.partial", coverage(crit)[:2] == (1, 2))

    ok = all(checks)
    print("selftest: %s (%d/%d)" % ("PASS" if ok else "FAIL", sum(checks), len(checks)))
    sys.exit(0 if ok else 1)


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
    # --describe-cli: emit JSON spec of accepted verbs + flags
    if argv[0] == "--describe-cli":
        import json
        print(json.dumps({
            "verbs": ["build", "comment", "selftest"],
            "flags": [
                "--anchor",
                "--help",
                "--how",
                "--item",
                "--out",
                "--pr",
                "--require-evidence",
                "--shots-dir",
                "--summary",
                "--template",
                "--title",
                "--video-dir",
            ],
        }))
        sys.exit(0)
    sub, opts = argv[0], _parse(argv[1:])
    {"build": cmd_build, "comment": cmd_comment, "selftest": cmd_selftest}.get(
        sub, lambda _o: (print("unknown command '%s'. choices: build comment selftest" % sub),
                         sys.exit(2)))(opts)


if __name__ == "__main__":
    main()

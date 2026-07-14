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
  progress-comment  Publish/update ONE idempotent progress comment on an issue (#301), marked with
             an invisible HTML anchor so a re-run edits the SAME comment instead of spamming new
             ones. Rate-limited (default 60s between remote updates) and fully fail-open: no `gh`
             CLI / network / token ⇒ exit 0, silent log, never blocks the loop.
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
import shutil
import subprocess
import sys
import time

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

try:
    from task_backlog import render_backlog_table
except Exception:  # pragma: no cover
    render_backlog_table = None


def log(msg):
    print("  " + msg, file=sys.stderr)


def _emit_progress(status, outcome=None, detail=""):
    """Fail-open progress-feedback hook (#301) — never raises, never blocks pr_evidence."""
    try:
        import loop_progress
        loop_progress.emit_event("evidence", status=status, outcome=outcome, detail=detail,
                                 source="pr_evidence.py")
    except Exception:
        pass


def render_progress_section():
    """`## Progresso do run` — auto-included whenever a backlog/anchor exists on disk. Never
    fabricates a %: with neither source, prints the converge-mode ACs x/y line, no invented number
    (#301 AC2)."""
    try:
        import loop_progress
        snap = loop_progress.build_snapshot()
        header = loop_progress.render_turn_header(snap)
    except Exception:
        return ""
    lines = ["### Progresso do run", "", header, ""]
    return "\n".join(lines)


# ----- progress-comment (idempotent, rate-limited, fail-open) --------------------------------

PROGRESS_COMMENT_MARKER = "<!-- simplicio-loop:progress -->"
PROGRESS_COMMENT_STATE = os.path.join(REPO, ".orchestrator", "loop", "progress_comment_state.json")
DEFAULT_MIN_INTERVAL_S = 60.0


def _gh_run(cmd, timeout=30):
    """Injectable gh-CLI runner — tests pass a fake in place of this. None on any failure."""
    try:
        return subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None


def build_progress_comment_body():
    try:
        import loop_progress
        snap = loop_progress.build_snapshot()
        header = loop_progress.render_turn_header(snap)
    except Exception:
        header = "UNVERIFIED|pct=?"
    return "\n".join([
        PROGRESS_COMMENT_MARKER, "",
        "**simplicio-loop progress**", "",
        header, "",
        "_updated: %s_" % time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    ]) + "\n"


def _rate_limited(min_interval=DEFAULT_MIN_INTERVAL_S, now=None, state_path=None):
    state_path = state_path or PROGRESS_COMMENT_STATE
    now = now if now is not None else time.time()
    last = 0.0
    try:
        with open(state_path, encoding="utf-8") as f:
            last = float(json.load(f).get("last_posted_at") or 0)
    except Exception:
        last = 0.0
    return (now - last) < min_interval


def _record_post(now=None, state_path=None):
    state_path = state_path or PROGRESS_COMMENT_STATE
    now = now if now is not None else time.time()
    try:
        os.makedirs(os.path.dirname(state_path), exist_ok=True)
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump({"last_posted_at": now}, f)
    except Exception:
        pass


def find_existing_progress_comment(issue, runner=None):
    """Return the comment id whose body contains the anchor marker, or None. `runner` is
    injectable so tests never shell out to a real `gh`."""
    runner = runner or _gh_run
    r = runner(["gh", "api", "repos/:owner/:repo/issues/%s/comments" % issue, "--paginate"])
    if r is None or r.returncode != 0:
        return None
    try:
        comments = json.loads(r.stdout or "[]")
    except ValueError:
        return None
    if not isinstance(comments, list):
        return None
    for c in comments:
        if isinstance(c, dict) and PROGRESS_COMMENT_MARKER in (c.get("body") or ""):
            return c.get("id")
    return None


def cmd_progress_comment(opts):
    """Publish/update ONE idempotent progress comment on an issue (#301 § 3). Fail-open: no `gh`,
    no network, or any error -> exit 0, silent log, never blocks the loop."""
    issue = opts.get("issue")
    if not issue:
        print("blocked")
        log("progress-comment requires --issue")
        return
    try:
        min_interval = float(opts.get("min-interval") or DEFAULT_MIN_INTERVAL_S)
    except (TypeError, ValueError):
        min_interval = DEFAULT_MIN_INTERVAL_S
    if not shutil.which("gh"):
        print("skip")
        log("gh CLI not found — progress-comment is a no-op (fail-open)")
        return
    if _rate_limited(min_interval):
        print("skip")
        log("rate-limited — last update <%.0fs ago" % min_interval)
        return
    body = build_progress_comment_body()
    ok = False
    try:
        existing = find_existing_progress_comment(issue)
        if existing:
            r = _gh_run(["gh", "api", "-X", "PATCH",
                        "repos/:owner/:repo/issues/comments/%s" % existing,
                        "-f", "body=%s" % body])
        else:
            r = _gh_run(["gh", "api", "repos/:owner/:repo/issues/%s/comments" % issue,
                        "-f", "body=%s" % body])
        ok = r is not None and r.returncode == 0
    except Exception:
        ok = False
    _record_post()
    tag = "MEASURED" if ok else "UNVERIFIED"
    print("%s|progress-comment %s" % (tag, "updated" if ok else "attempted (see stderr for gh output)"))


def _load_anchor(opts):
    path = opts.get("anchor") if isinstance(opts.get("anchor"), str) else ANCHOR_DEFAULT
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _load_backlog(opts):
    if render_backlog_table is None:
        return None, []
    path = (opts.get("backlog") if isinstance(opts.get("backlog"), str) else
            os.environ.get("SIMPLICIO_BACKLOG_FILE") or
            os.path.join(REPO, ".orchestrator", "backlog", "backlog.jsonl"))
    if not os.path.exists(path):
        return None, []
    master = None
    items = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                s = line.strip()
                if not s:
                    continue
                obj = json.loads(s)
                if obj.get("kind") == "master":
                    master = obj
                elif obj.get("kind") == "item":
                    items.append(obj)
    except (OSError, ValueError):
        return None, []
    return master, items


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
    backlog_master, backlog_items = _load_backlog(opts)
    images, videos = collect_all_evidence(opts)
    has_evidence = bool(criteria) or bool(backlog_items) or bool(images) or bool(videos)

    title = opts.get("title") or anchor.get("goal") or "Untitled change"
    item = opts.get("item") or anchor.get("item") or ""
    summary = opts.get("summary") or ""

    backlog_md = ""
    if backlog_items and render_backlog_table is not None:
        backlog_md = render_backlog_table(backlog_master, backlog_items, anchor=anchor)
    checklist_md = render_checklist(criteria)
    evidence_md = render_evidence(images, videos)
    how = opts.get("how") or "Run the project's test gate (`python3 scripts/check.py`) and the " \
                             "captured `web_verify` / `video_evidence` flow above."

    progress_md = render_progress_section()

    blocks = []
    if summary:
        blocks += ["### Summary", summary, ""]
    if item:
        blocks += ["Closes #%s" % str(item).lstrip("#"), ""]
    if progress_md:
        blocks += [progress_md, ""]
    if backlog_md:
        blocks += [backlog_md, ""]
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
    _emit_progress("begin", detail="pr_evidence.py build")
    body, has_evidence = build_body(opts)
    if opts.get("require-evidence") and not has_evidence:
        print("blocked")
        log("BLOCKED — no acceptance-criteria checklist and no prints to attach. "
            "Anchor the ACs (task_anchor.py set) and capture prints (web_verify.py) before "
            "opening the PR. Refusing to open an evidence-less PR.")
        _emit_progress("blocked", outcome="blocked", detail="no checklist and no prints")
        sys.exit(_BLOCKED)
    out = opts.get("out")
    if isinstance(out, str):
        with open(out, "w", encoding="utf-8") as f:
            f.write(body)
        log("wrote PR body -> %s (%d bytes)" % (out, len(body)))
        print("done %s" % out)
        _emit_progress("end", outcome="pass", detail="PR body -> %s" % out)
    else:
        sys.stdout.write(body)
        _emit_progress("end", outcome="pass", detail="PR body -> stdout (%d bytes)" % len(body))


def cmd_comment(opts):
    """The shorter evidence comment posted back on the source item."""
    anchor = _load_anchor(opts)
    criteria = anchor.get("criteria", [])
    done, total, pending = coverage(criteria)
    backlog_master, backlog_items = _load_backlog(opts)
    images, videos = collect_all_evidence(opts)
    pr = opts.get("pr")
    lines = []
    if pr:
        lines.append("PR: #%s" % str(pr).lstrip("#"))
    lines.append("Verification: %d/%d acceptance criteria met · %d print(s), %d recording(s)."
                 % (done, total, len(images), len(videos)))
    if backlog_items:
        done_items = sum(1 for item in backlog_items if item.get("status") == "done")
        skipped = sum(1 for item in backlog_items if item.get("status") == "skipped")
        lines.append("Body of work: %d/%d done · %d skipped." %
                     (done_items, len(backlog_items), skipped))
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
    if render_backlog_table is not None:
        table = render_backlog_table({"kind": "master", "goal": "Phase 0"},
                                     [{"kind": "item", "id": "T1", "goal": "Fix | pipes",
                                       "goal_fp": "fp1", "acs": ["x"], "status": "done",
                                       "evidence": ["shot.png"], "done_criteria": 1,
                                       "total_criteria": 1, "skip_reason": ""}], anchor={})
        chk("backlog.heading", "Body of work" in table)
        chk("backlog.row", "T1" in table and "1/1" in table)
        chk("backlog.escaping", r"Fix \| pipes" in table)
    chk("backlog.fail_open", _load_backlog({"backlog": "definitely-missing.jsonl"}) == (None, []))

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
            "verbs": ["build", "comment", "progress-comment", "selftest"],
            "flags": [
                "--anchor",
                "--backlog",
                "--help",
                "--how",
                "--issue",
                "--item",
                "--min-interval",
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
    {"build": cmd_build, "comment": cmd_comment, "progress-comment": cmd_progress_comment,
     "selftest": cmd_selftest}.get(
        sub, lambda _o: (print("unknown command '%s'. choices: build comment progress-comment "
                               "selftest" % sub), sys.exit(2)))(opts)


if __name__ == "__main__":
    main()

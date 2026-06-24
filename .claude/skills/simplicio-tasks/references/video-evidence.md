# Video evidence — `video_evidence` via hyperframes (demo-video proof)

Concrete implementation of the `video_evidence` extension point: render a **deterministic MP4**
demonstration video of a screen/feature with **hyperframes** and record it as evidence that a
change works. Two jobs, one producer:

1. **On request** — when a work-item or the skill argument asks for a demo video
   (`/simplicio-tasks faça um vídeo demonstrativo da tela X`), this is the producer that fulfils it.
2. **As evidence** — the north star from Step 4b ("works, not just compiles") taken one step past a
   screenshot: a reproducible MP4 walkthrough of the change actually running on screen.

Credit: **hyperframes** by HeyGen — <https://github.com/heygen-com/hyperframes>. Open-source,
renders HTML/CSS/media/animation compositions to MP4 **deterministically** ("same input, same
frames, same output"), built for AI agents and CI. No API keys; local render via headless Chrome +
FFmpeg. Optional AWS Lambda for distributed render.

## When it fires (cheap gate — terminal, not LLM)
Two triggers, both decided by the terminal worker, never by the model:

```bash
# (a) explicit request — the goal/work-item asks for a video
python3 scripts/video_evidence.py detect --goal "<the skill argument or issue title/body>"
# (b) UI change wants a moving proof — reuse the web_verify FE-diff gate
python3 scripts/web_verify.py detect --base origin/main   # fe-changed? then a demo is worthwhile
```
`detect` matches a video noun (`vídeo`/`video`/`screencast`/`walkthrough`/`gif`…) near a
make/show verb (`faça`/`crie`/`grave`/`make`/`create`/`record`/`demo`…), EN/PT/ES. No match →
SKIP. This is intent classification by regex, not the LLM.

## The pipeline (screenshots → deterministic MP4)
hyperframes renders an HTML composition; the natural source of "the screen" is the per-step
screenshots `web_verify` already captures. So the two producers chain:

1. **`web_verify run`** drives the real UI through the happy path and writes per-step PNGs into
   `.orchestrator/tee/web/` (existing behavior — see `web-evidence.md`).
2. **`video_evidence scaffold`** turns those exact PNGs into a hyperframes composition (one timed
   scene per shot, captioned), under `.orchestrator/tee/video/<name>/composition.html`.
3. **`video_evidence render`** runs `npx hyperframes render` → a deterministic MP4 walkthrough.

A pure-synthetic demo (no live UI — e.g. an architecture animation) skips step 1 and feeds
`--shots a.png,b.png` directly.

## How to drive hyperframes
Requirements (preflighted by the worker; BLOCK if absent, never a fake pass): **Node.js 22+** and
**FFmpeg**. The CLI is invoked through `npx` — no global install needed.

```bash
npx hyperframes init <project>     # scaffold a composition project
npx hyperframes preview            # live-reload preview in a browser (local authoring only)
npx hyperframes lint               # validate the composition before render
npx hyperframes render             # render the composition to MP4 (deterministic)
npx hyperframes inspect            # examine composition details
```

Agent-skill install (optional, on hosts that support it): `npx skills add heygen-com/hyperframes`.

## Capture into the evidence ledger
All artifacts write to `.orchestrator/tee/video/` (project under `<name>/`, MP4
`<name>-<issue>.mp4`). Append a ledger row recording **path + a one-line verdict** — never the
bytes:
```
video_evidence: PASS — demo MP4 (hyperframes) project=login-demo mp4=.orchestrator/tee/video/login-demo-12.mp4
```
The ledger stores the path, never the video.

## Attach to the PR (link, don't paste)
```bash
# CI: prefer actions/upload-artifact; locally a release works
gh release upload "evidence-pr<N>" .orchestrator/tee/video/login-demo-12.mp4
gh pr comment <N> --body "🎬 video_evidence ✅  demo video rendered with hyperframes: <url>"
```

## Token economy (critical)
- NEVER feed video bytes, frames, or the composition HTML into context. Evidence = **file path/URL
  + boolean verdict**, exactly like `web_verify`.
- Reuse the screenshots `web_verify` already produced — do not re-capture or re-render the UI just
  to make the video.
- Clamp `npx hyperframes` output through the orient catalog (tee to file; feed back only the
  verdict + first N error lines — same rtk-style clamp as build/test output).
- Determinism is the point: the same composition renders the same MP4, so the video is a
  CI-reproducible artifact, not a one-off recording.

## Enforcement (simplicio-review rubric line)
At MEDIUM+, when the work-item was a **video request**, `simplicio-review` REQUIRES a
`video_evidence` ledger entry with an MP4 path and a PASS verdict, else FAIL. `video_evidence` is
the producer; `simplicio-review` is the enforcer; `pr`/`evidence` attaches. For an ordinary UI
change the video is OPTIONAL (the screenshot+trace from `web_verify` is sufficient proof); the
video is REQUIRED only when the deliverable itself is the demo video.

## Runnable worker (`scripts/video_evidence.py`)
The prose above is the contract; `scripts/video_evidence.py` is the runnable form. Five verbs:
```bash
python3 scripts/video_evidence.py detect   --goal "<request text>" [--exit-code]
python3 scripts/video_evidence.py scaffold --name NAME --frames .orchestrator/tee/web \
    --title "Screen X" [--seconds 2.0] [--fps 30]
python3 scripts/video_evidence.py lint     --name NAME
python3 scripts/video_evidence.py render   --name NAME [--issue N] [--upload --pr N]
python3 scripts/video_evidence.py verify   --name NAME --frames DIR --title "Screen X" [--issue N]
```
`verify` = scaffold (if needed) + render; it writes the MP4 under `.orchestrator/tee/video/`,
appends the ledger row, and prints the MACHINE-tier verdict (`done|fail|skip|blocked`). A missing
toolchain (Node 22+, FFmpeg, hyperframes) yields **BLOCKED**, never a fake pass — identical
discipline to `web_verify`.

## Scope (v1 — don't over-engineer)
Build: intent detector · screenshot→composition scaffolder · `render`/`lint`/`verify` verbs ·
ledger row schema · the review rubric line · PR link upload. The default composition is a
captioned slideshow walkthrough of the captured screens — deterministic and sufficient as proof.
Skip for v1: GSAP/Lottie/Three.js authored animations, Lambda distributed render, audio
narration, multi-composition reels. A single deterministic MP4 of the screens is sufficient
demonstration + evidence.

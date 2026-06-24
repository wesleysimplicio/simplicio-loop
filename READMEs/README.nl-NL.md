# 🔁 simplicio-tasks — De universele lussende AI-orkestrator

<p align="center">
  <img src="../assets/simplicio-loop-hero.jpg" alt="simplicio-loop" width="920" />
</p>

<p align="center">
  <a href="https://github.com/wesleysimplicio/simplicio-loop/stargazers"><img src="https://img.shields.io/github/stars/wesleysimplicio/simplicio-loop?style=social" alt="Stars"></a>
  <a href="#-de-10-skills--accelerators"><img src="https://img.shields.io/badge/skills-10-7C3AED" alt="10 skills"></a>
  <a href="#-source-adapters"><img src="https://img.shields.io/badge/source%20adapters-5-00E08A" alt="5 source adapters"></a>
  <a href="#-11-runtimes-één-protocol"><img src="https://img.shields.io/badge/runtimes-11-2563EB" alt="11 runtimes"></a>
  <a href="#-de-43-uitbreidingspunten"><img src="https://img.shields.io/badge/extension%20points-43-00E08A" alt="43 extension points"></a>
  <a href="#-token-economie"><img src="https://img.shields.io/badge/tokens-up%20to%2096%25%20fewer-green" alt="Up to 96% fewer tokens"></a>
  <a href="../LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue" alt="License"></a>
</p>

<p align="center">
  <a href="#-tldr">TL;DR</a> ·
  <a href="#-de-10-skills--accelerators">10 Skills</a> ·
  <a href="#-source-adapters">Source Adapters</a> ·
  <a href="#-11-runtimes-één-protocol">11 Runtimes</a> ·
  <a href="#-de-lus">De lus</a> ·
  <a href="#-token-economie">Token-economie</a> ·
  <a href="#-token-economie">Capture Engine</a> ·
  <a href="#-installeren--gebruiken">Installeren</a>
</p>

<p align="center">
  <strong>🌍 Languages:</strong><br>
  <a href="../README.md">🇬🇧 English</a> |
  <a href="README.pt-BR.md">🇧🇷 Português</a> |
  <a href="README.es-ES.md">🇪🇸 Español</a> |
  <a href="README.fr-FR.md">🇫🇷 Français</a> |
  <a href="README.de-DE.md">🇩🇪 Deutsch</a> |
  <a href="README.it-IT.md">🇮🇹 Italiano</a> |
  <a href="README.ja-JP.md">🇯🇵 日本語</a> |
  <a href="README.ko-KR.md">🇰🇷 한국어</a> |
  <a href="README.zh-CN.md">🇨🇳 简体中文</a> |
  <a href="README.ru-RU.md">🇷🇺 Русский</a> |
  <a href="README.pl-PL.md">🇵🇱 Polski</a> |
  <a href="README.tr-TR.md">🇹🇷 Türkçe</a> |
  <a href="README.nl-NL.md">🇳🇱 Nederlands</a> |
  <a href="README.hi-IN.md">🇮🇳 हिन्दी</a> |
  <a href="README.ar-SA.md">🇸🇦 العربية</a>
</p>

---

## ⚡ TL;DR

**simplicio-tasks** is een runtime-onafhankelijk **super-plugin** — één autonome lussende
orkestrator (aangeroepen als **`/simplicio-tasks`**) plus **vijf satelliet-skills** — dat elke
sterke LLM (Claude, Codex, Copilot, Gemini, Cursor, lokale modellen) verandert in een zelfsturende
worker. Je wijst hem op een hoeveelheid werk — *"maak alle open issues af"*, *"werk de CI-wachtrij
weg"*, *"leeg het Jira-board"* — en hij draait de hele levenscyclus helemaal zelf:

> **ontdekken → begrijpen → beslissen → handelen → verifiëren → corrigeren → vastleggen → herhalen**

Hij ontdekt werk uit elke bron (GitHub Issues, Jira, Azure DevOps, agentsview-sessies, en meer),
ontdubbelt, schaalt automatisch een agentvloot op naar jouw machine, implementeert elk item via een
kwaliteitslus die **de code uitvoert (niet alleen compileert)**, opent PR's, verwerkt
CI-/reviewfeedback, merget, en blijft **24/7** speuren naar nieuw werk — allemaal achter
veiligheidspoorten en een harde kostennoodstop.

```text
/simplicio-tasks termine as issues abertas
→ identity + pre-flight (kill-switch, auth, watcher)
→ discover 50 issues · dedup · build dependency DAG
→ autoscale fleet = 14 · pipeline implement→review→merge
→ each item: read body+ACs → orient code → plan → edit → run → verify → PR
→ merge · close with evidence · rollback if main breaks
→ keep looping every ~2 min until the queue is dry (evidence-gated, never a false "done")
```

Drie dingen maken het anders: het is een **super-plugin van toegespitste skills**, het draait
**hetzelfde protocol op 11 runtimes**, en het doet dit alles met **agressieve, eerlijke
token-economie**.

---

## 🧠 De 10 skills & accelerators

De orkestrator-kern + vijf satellieten + vier accelerators. Elke satelliet is **optioneel** —
wanneer geladen, delegeert de orkestrator eraan (rijker + goedkoper); wanneer afwezig, dekt het
inline-protocol 100%. Accelerators worden **automatisch gedetecteerd** — aanwezig = gebruikt,
afwezig = LLM-fallback.

| # | Capaciteit | Neemt over van | Wat het doet | Token-impact |
|---|---|---|---|---|
| 1 | 🔁 **simplicio-tasks** | — | De orkestrator-lus: 43 uitbreidingspunten, dual-path-router, zelfaudit-convergentie | Kern |
| 2 | ♾️ **simplicio-loop** | [ralph-loop](https://github.com/cursor/plugins/tree/main/ralph-loop) | Geharde Ralph-lus: bewijs-gepoorte `<promise>`-uitgang, max_iterations-plafond | Lusaandrijving |
| 3 | 🧱 **simplicio-orient** | [rtk](https://github.com/rtk-ai/rtk) + [caveman](https://github.com/JuliusBrussee/caveman) | Terminal-first uitvoering, output-reductiecatalogus, tee-cache, signatures-read | L0 deterministisch |
| 4 | 🔥 **simplicio-review** | [thermos](https://github.com/cursor/plugins/tree/main/thermos) | Parallelle adversariële review op afzonderlijke rubrieken → gededupliceerd oordeel | Kwaliteitspoort |
| 5 | 🗜️ **simplicio-compress** | [caveman](https://github.com/JuliusBrussee/caveman) | Output- + geheugencompressie, fail-closed `transform_guard` | 40-60% minder |
| 6 | 🎓 **simplicio-learn** | [teaching](https://github.com/cursor/plugins/tree/main/teaching) | Post-run-retrospectief → duurzame, gededupliceerde lessen in het geheugen | Slimmer per run |
| 7 | 🧭 **Understand Anything** | [Egonex-AI](https://github.com/Egonex-AI/Understand-Anything) | Kennisgrafiek-oriëntatie: semantisch zoeken, geleide tours, afhankelijkheidsgrafiek | **L0 nul tokens** |
| 8 | 📊 **agentsview** | [kenn-io](https://github.com/kenn-io/agentsview) | Sessie-analyse, kostenregistratie, ontdekking van vastgelopen sessies | **L1** alleen SQL |
| 9 | ⚡ **LMCache** | [LMCache](https://github.com/LMCache/LMCache) | KV-cache tussen lusbeurten — 40-70% TTFT-reductie op lokale modellen | GPU-tijd ↓ |
| 10 | 🗜️ **Simplicio capture engine** | `engine/simplicio_engine.py` (native, alleen-stdlib; savings-schema compatibel met het OSS-project [headroom](https://github.com/headroomlabs-ai/headroom)) | Transparante capture-proxy: stuurt door naar de echte provider, meet + comprimeert deterministisch, schrijft `proxy_savings.json` | **deterministisch** |

Elke skill leeft onder [`.claude/skills/`](../.claude/skills); elke accelerator heeft een
referentiedocument onder `.claude/skills/simplicio-tasks/references/`.

---

## 📡 Source adapters

De orkestrator ontdekt werk uit elke bron via pluggable adapters. Elke biedt zes werkwoorden:
`list_ready`, `get_details`, `claim`, `update_status`, `attach_evidence`, `close`.

| Bron | Adapter | Doel |
|---|---|---|
| GitHub Issues/PR's | `gh` CLI (native) | Primaire bron voor werkitems |
| Jira / Asana / ClickUp / Linear / Notion | host-connector | Board-/projectbeheer |
| Trello / Azure DevOps | `az boards`-adapter | Azure work tracking |
| **agentsview-sessies** | `scripts/agentsview_adapter.py` | Herstel van vastgelopen sessies + kostenzichtbaarheid |
| Lokale bestanden / CI-wachtrij | filesystem / CI API | Intern werkbeheer |

Zie het referentiedocument van elke adapter onder `.claude/skills/simplicio-tasks/references/`.

|---

## 🌐 11 runtimes, één protocol

Eén universele skill-kern + één set hooks drijft elke runtime aan. Een adapter is dun: hij vertelt
een runtime *waar de skills te laden*, *hoe de lus scherp te stellen* en *hoe de native snelheid te
binden*. **De skill noemt geen enkele runtime; de runtime detecteert de skill.**

| Runtime | Skill-laden | Lusaandrijving | Native binding |
|---|---|---|---|
| **Claude Code** | `.claude/skills/` + plugin | `Stop`-hook | MCP |
| **Codex** | `AGENTS.md` | zelf-getimed | MCP / adapter |
| **VS Code (Copilot)** | `copilot-instructions.md` | tasks | MCP |
| **Cursor** | `.cursor-plugin/` | `stop`+`afterAgentResponse` | MCP / rules |
| **Antigravity** | rules / `AGENTS.md` | zelf-getimed | MCP |
| **Kiro** | `.kiro/steering/` | specs | MCP |
| **OpenCode** | `AGENTS.md` | zelf-getimed | MCP |
| **Gemini** | `GEMINI.md` | zelf-getimed | MCP / adapter |
| **Aider** | `CONVENTIONS.md` | zelf-getimed | — (LLM-fallback) |
| **Hermes** | native recall | native lus | **native** |
| **OpenClaw** | plugin SDK | native scheduler | **native** |

De belofte: **hetzelfde protocol, dezelfde poorten, dezelfde veiligheid op alle 11 — alleen de
snelheid verschilt.** `orient_clamp.py` (token-economie) werkt op elke runtime zonder enige
bedrading. Zie [`adapters/MATRIX.md`](../adapters/MATRIX.md).

---

## 🗺️ De volledige flow — van vraag tot oplevering

Elke laag waarop de orkestrator inwerkt, op volgorde — van het lezen van de vraag (issues, taken,
toewijzingen) tot het opleveren van gemerged, onderbouwd werk, en dan 24/7 lussen voor meer.

```mermaid
flowchart TD
  subgraph SRC["1 · Demand sources (any adapter)"]
    direction LR
    S1["GitHub Issues / PRs / CI"]
    S2["Jira · Azure DevOps · Linear · ClickUp · Notion · agentsview · Understand Anything (orient)"]
    S3["Assigns · TODO/FIXME · CVE · local files · LMCache (inference accelerator)"]
  end
  SRC --> PF
  subgraph PF["2 · Pre-flight gates"]
    direction LR
    P1["cost kill-switch budget · agentsview cost check"]
    P2["source auth + scopes"]
    P3["arm 24/7 watcher"]
  end
  PF --> DISC
  subgraph DISC["3 · Discover + normalize"]
    direction LR
    D1["source_adapter: list metadata only"]
    D2["normalize to canonical schema"]
    D3["dedup id+title+fingerprint+branch/PR"]
    D4["dependency DAG"]
  end
  DISC --> INTK
  subgraph INTK["4 · Deep intake (per item)"]
    direction LR
    I1["body + ALL comments"]
    I2["extract acceptance criteria"]
    I3["orient code · signatures-only reads or Understand Anything knowledge graph"]
    I4["plan + AC checklist + complexity"]
  end
  INTK --> RT{"5 · Route"}
  RT -->|"small and every item complexity at most 3"| FAST["Fast-path: solo, one targeted test"]
  RT -->|"large queue or any medium+"| POOL
  subgraph POOL["6 · Continuous worker pool (autoscaled, conflict-aware)"]
    direction LR
    W1["claim · branch · worktree if overlap"]
    W2["deterministic_edit"]
    W3["quality loop: edit-lint-test-fix"]
  end
  FAST --> QG
  POOL --> QG
  subgraph QG["7 · Quality gates"]
    direction LR
    Q1["AC gate = real DoD"]
    Q2["WORKS not just compiles · web_verify (Playwright)"]
    Q3["adversarial review · thermos rubrics"]
  end
  QG --> SG
  subgraph SG["8 · Safety gates (non-negotiable)"]
    direction LR
    G1["secret-scan"]
    G2["irreversible-op human gate"]
    G3["4-state verdict · attestation"]
  end
  SG --> DEL
  subgraph DEL["9 · Deliver"]
    direction LR
    L1["commit · push · Draft PR"]
    L2["close in-source + evidence"]
    L3["verify reality, not self-report"]
  end
  DEL --> FB
  subgraph FB["10 · Feedback loop to merge-ready"]
    direction LR
    F1["CI fail -> fix root cause"]
    F2["review comments -> adjust"]
    F3["branch behind main -> additive rebase"]
  end
  FB -->|"merged and closed"| DONE(["done + evidence + savings line"])
  WATCH["11 · 24/7 watcher · simplicio-loop evidence-gated promise · max-iterations cap · cost kill-switch · LMCache KV cache warm"]
  FB -. "poll new work / comments / checks" .-> WATCH
  DONE -. "idle until new work" .-> WATCH
  WATCH -. "re-feed the goal" .-> DISC
```

---

## 🔁 De lus

De **bewijs-gepoorte lus** is het kernmechanisme. Hij voert hetzelfde doel elke beurt opnieuw in
zodat de agent zijn eigen eerdere werk ziet. Uitgang is ALLEEN via:

1. **Bewijs-gepoorte `<promise>`** — de beurt die de belofte uitzendt MOET ook concreet bewijs
   dragen (geslaagde test, gemergede PR, herbevraging van gesloten item). Een belofte zonder bewijs
   = genegeerd.
2. **`max_iterations`-plafond** — harde veiligheidsbackstop
3. **Budgetnoodstop** — `daily_usd_ceiling` legt de lus stil zodra het besteed is
4. **STOP-signaal** — `.orchestrator/STOP` of kanaalcommando

Tussen beurten cachet LMCache (indien beschikbaar) de KV-toestand zodat herinvoer bijna-nul prefill
kost.

---

## 📊 Token-economie

| Techniek | Besparing |
|---|---|
| `deterministic_edit` (L0) | 100% van de edit-tokens (bestand mechanisch geschreven, nooit door de LLM) |
| Terminal-first uitvoering | Feiten uit de shell, geen LLM-hallucinatie |
| Output-reductiecatalogus | Plafonds per commandotype (`CAP_ERRORS=20`, `CAP_WARNINGS=10`, `CAP_LIST=20`) — `orient_clamp.py` |
| Tee+CCR-cache bij falen | Voer een gefaald commando nooit opnieuw uit — lees de gecachete output |
| Signatures-only leesmodus | `simplicio signatures <file>` — bestand van 870 regels → 65 regels (**93% bespaard**), bodies weggelaten |
| `simplicio-compress` | Beknopte proza + eenmalige geheugencompactie |
| `orient_clamp.py` | Clamp + tee op elk shell-commando, zonder bedrading |
| Native response-cache | herhaald deterministisch (temp=0) verzoek → bediend vanuit de cache, slaat de LLM-call over (**100% bij hit**) — `simplicio cache`, standaard aan (`SIMPLICIO_CACHE=0` om uit te zetten) |
| Simplicio capture-proxy + MCP | 60-95% minder tokens op tool-outputs via een transparante compressiedaemon |

Besparingen tellen alleen bij een geverifieerd-correcte uitkomst. Baseline = het goedkoopste
verstandige niet-georkestreerde pad naar hetzelfde resultaat. Zie `references/token-economy.md`.

### 📈 Simplicio Token Monitor

Een live, altijd-aan zicht op de besparingen:

- **Web-dashboard** — `http://127.0.0.1:9090` — realtime token-grafiek, besparingsmeter, de
  LLMs/runtimes en **141/144 providers (98%)** die we onderscheppen, en een live proxy-log.
- **Menubalk- / tray-widget** — live bespaarde tokens in de systeemtray (macOS rumps · Windows/Linux pystray).
- **Eén module** — `scripts/simplicio-economy.sh {status|up|wire}` brengt de capture-proxy + monitor
  + tray + de deterministische `simplicio-dev-cli`-operator op en rapporteert de hele stack.

De installatie registreert alle drie als auto-start-services (macOS launchd · Linux systemd ·
Windows Startup) via `scripts/setup_simplicio.sh`, of de cross-platform
`python3 scripts/install_services.py install`. Na installatie draaien de monitor + capture **zonder
de lus aan te roepen** — zie `references/token-capture.md`.

### 🛠️ De capture engine — één native module, elk commando

[`engine/simplicio_engine.py`](../engine/simplicio_engine.py) is de native Simplicio capture engine
(alleen-stdlib, fail-open) — een **volledige herimplementatie van het upstream
[headroom](https://github.com/headroomlabs-ai/headroom)-oppervlak zonder externe afhankelijkheid**.
Voer elk commando uit via de [`scripts/simplicio-engine`](../scripts/simplicio-engine)-wrapper
(bijv. `simplicio-engine doctor`):

| Commando | Wat het doet |
|---|---|
| `proxy` | de transparante capture-proxy — routeert elk model naar zijn **echte** provider, comprimeert + meet + cachet (geen model-swap) |
| `doctor` | bereikbaarheid van de proxy + levenslange besparingen |
| `cache` | native response-cache (`stats`/`clear`) — een herhaald deterministisch verzoek wordt vanuit de cache bediend en slaat de LLM-call over |
| `signatures` | signatures-only weergave van een bronbestand (bodies weggelaten, ~93% minder tokens om code te lezen) |
| `semantic` | omkeerbare extractieve (semantic-lite) compressie |
| `kompress` | **ONNX** semantisch token-snoeien via het echte `kompress-v2-base`-model |
| `detect` | content-type-detectie + slimme routering per blok |
| `rag` | TF-IDF (of `--ml` embedding) retrieval over de CCR-geheugenopslag |
| `memory` | CCR compress-cache-retrieve-opslag (`remember`/`recall`/`forget`/`list`/`stats`) |
| `mcp` | native stdio MCP-server (compress / retrieve / stats tools) |
| `init` / `wrap` | registreer Simplicio in een client (Claude / Codex / Copilot / OpenClaw) · draai een client met capture-routering |
| `report` / `audit` / `capture` / `evals` | besparingsrapport · audit een boom op compressiekansen · dry-run van een verzoek · compressie-regressiepoort |

### 🧠 Optionele echte ML-modellen — `pip install "simplicio-loop[onnx]"`

Vier **echte**, publieke (Apache-2.0) ONNX-modellen draaien native — dezelfde modellen die de
upstream gebruikt. Zonder de extra dekt het deterministische stdlib-pad alles; modellen worden bij
het eerste gebruik gedownload.

| Model | Commando | Gebruik |
|---|---|---|
| `kompress-v2-base` | `simplicio kompress` | semantisch token-snoeien |
| `technique-router-onnx` | `simplicio router` | techniekroutering |
| `all-MiniLM-L6-v2-onnx` | `simplicio embed` · `rag --ml` | embeddings + semantische RAG |
| `siglip-image-encoder-onnx` | `simplicio image` | content-verifier voor beeldcompressie |

### ⚙️ Native Rust performance-kern (optioneel)

[`rust/`](../rust) levert vier crates die geport + omgedoopt zijn vanuit de upstream (Apache-2.0;
`NOTICE` crediteert het): `simplicio-core` (compressors + smart-crusher), `simplicio-py`
(PyO3-bindingen), `simplicio-proxy` (axum reverse proxy), `simplicio-parity`
(Rust↔Python-pariteitsharness). Bouw met `maturin` — de Python-engine werkt volledig zonder hen; de
crates voegen alleen native snelheid toe.

|---

## 🏛️ Ontwerppijlers (in detail)

Vier mechanismen dragen de orkestratiekracht:

| Pijler | Focus | Leeft in |
|---|---|---|
| **DAG + pipeline** | parallellisme per afhankelijkheid, gefaseerd per item | `references/orchestration.md` (Stap 3 pool + pipeline) |
| **Worktree-isolatie** | parallelle edits zonder de boom te corrumperen, merge-gepoort | `references/orchestration.md` |
| **Adversariële verificatie** | een panel van sceptici vóór "delivered" | `references/quality-safety-delivery.md` · skill `simplicio-review` |
| **Lusbudgetplafond** | anti-oneindige-lus, dubbele uitgang | `references/standing-loop-247.md` · skill `simplicio-loop` |

---

## 🚀 Installeren & gebruiken

```bash
git clone https://github.com/wesleysimplicio/simplicio-loop
cd simplicio-loop

# install for your runtime (omit <runtime> to auto-detect)
bash scripts/install.sh <runtime> [--global]        # macOS / Linux
pwsh scripts/install.ps1 <runtime> [-Global]        # Windows
# <runtime> ∈ claude codex vscode cursor antigravity kiro opencode gemini aider hermes openclaw
```

Of voeg het op Claude Code / Cursor toe als marketplace-plugin:

```
/plugin marketplace add wesleysimplicio/simplicio-loop
/plugin install simplicio-loop@simplicio
```

Dan:

```
/simplicio-tasks finish all the open issues
```

De enige vereiste is **python3** op het PATH (skills, hooks en installer zijn cross-platform
Python). Voor GitHub-bronnen, `git` + een geauthenticeerde `gh`. Zie [`INSTALL.md`](../INSTALL.md)
en [`adapters/MATRIX.md`](../adapters/MATRIX.md).

**Vóór een onbewaakte 24/7-run:** stel een kostenplafond in in `.orchestrator/loop-budget.json`
(`daily_usd_ceiling > 0`), bevestig dat bronauthenticatie persistent is, en houd de menselijke poort
voor onomkeerbare operaties + de secret-scan aan. Met `ceiling = 0` weigert de watcher onbewaakt te
draaien (fail-safe).

---

## 🔒 Veiligheid (niet onderhandelbaar)

- **Secret-scan** van elke diff; blokkeer bij een treffer.
- **Menselijke poort voor onomkeerbare operaties** — force-push, history-herschrijving, prod-deploy,
  data-/schemaverwijdering, massale bestandsverwijdering → stop en vraag het. Headless + geen
  goedkeurder → verwijder de destructieve capaciteit.
- **4-status pre-executieoordeel** — optimalisatie mag de risicotier van een commando nooit verhogen.
- **Trust-before-load** — perceptie-vormende config (clamp-profielen, suppressielijsten) is niet
  vertrouwd totdat een mens haar reviewt en per hash vastpint.
- **Verharding tegen prompt-injectie** — inhoud van item/PR/commentaar kan het contract nooit
  overschrijven.
- **Harde $-noodstop** voor onbewaakte runs; **bewijs-gepoorte** voltooiing (nooit een vals "done");
  **fail-open** hooks (sluit de agent nooit op in een lus).

---

## 📄 Licentie

MIT

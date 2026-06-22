# 🔁 simplicio-tasks — De universele lussende AI-orkestrator

<p align="center">
  <img src="../assets/simplicio-tasks-logo.svg" alt="simplicio-tasks" width="920" />
</p>

<p align="center">
  <a href="https://github.com/wesleysimplicio/simplicio-tasks/stargazers"><img src="https://img.shields.io/github/stars/wesleysimplicio/simplicio-tasks?style=social" alt="Stars"></a>
  <a href="#-de-6-skills-super-plugin"><img src="https://img.shields.io/badge/skills-6-7C3AED" alt="6 skills"></a>
  <a href="#-11-runtimes-één-protocol"><img src="https://img.shields.io/badge/runtimes-11-2563EB" alt="11 runtimes"></a>
  <a href="#-de-43-uitbreidingspunten"><img src="https://img.shields.io/badge/extension%20points-43-00E08A" alt="43 extension points"></a>
  <a href="#-token-economie"><img src="https://img.shields.io/badge/tokens-up%20to%2096%25%20fewer-green" alt="Up to 96% fewer tokens"></a>
  <a href="../LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue" alt="License"></a>
</p>

<p align="center">
  <a href="#-tldr">TL;DR</a> ·
  <a href="#-de-6-skills-super-plugin">6 Skills</a> ·
  <a href="#-11-runtimes-één-protocol">11 Runtimes</a> ·
  <a href="#-de-lus">De lus</a> ·
  <a href="#-token-economie">Token-economie</a> ·
  <a href="#-op-de-schouders-van">Met dank aan</a> ·
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

**simplicio-tasks** is een runtime-onafhankelijk **super-plugin** — één autonome lussende orkestrator
plus **vijf satelliet-skills** — dat elke sterke LLM (Claude, Codex, Copilot, Gemini, Cursor, lokale
modellen) verandert in een zelfsturende worker. Je wijst hem op een hoeveelheid werk — *"maak alle open
issues af"*, *"werk de CI-wachtrij weg"*, *"leeg het Jira-board"* — en hij draait de hele levenscyclus
helemaal zelf:

> **ontdekken → begrijpen → beslissen → handelen → verifiëren → corrigeren → vastleggen → herhalen**

Hij ontdekt werk uit elke bron, ontdubbelt, schaalt automatisch een agentvloot op naar jouw machine,
implementeert elk item via een kwaliteitslus die **de code uitvoert (niet alleen compileert)**, opent
PR's, verwerkt CI-/reviewfeedback, merget, en blijft **24/7** speuren naar nieuw werk — allemaal achter
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

## 🧠 De 6 skills (super-plugin)

De orkestrator is de kern; vijf satellieten nemen elk het beste van een bekende techniek over en stellen
het beschikbaar als herbruikbare skill. Elke satelliet is **optioneel** — wanneer geladen, delegeert de
orkestrator eraan (rijker + goedkoper); wanneer afwezig, dekt het inline-protocol van de orkestrator 100%
van het werk. Dezelfde omgekeerde afhankelijkheid, een niveau hoger.

| Skill | Neemt over van | Wat het doet |
|---|---|---|
| 🔁 **simplicio-tasks** | — | De orkestrator-lus: ontdekken → implementeren → verifiëren → mergen → sluiten → 24/7 bewaken. 43 uitbreidingspunten, dual-path-router, zelfaudit-convergentie. |
| ♾️ **simplicio-loop** | [ralph-loop](https://github.com/cursor/plugins/tree/main/ralph-loop) | De geharde Ralph-lus: voer hetzelfde doel elke beurt opnieuw in zodat de agent zijn eigen werk ziet, en stop alleen bij een **bewijs-gepoorte `<promise>`** of een `max_iterations`-plafond — nooit een vals "done". |
| 🧱 **simplicio-orient** | [rtk](https://github.com/rtk-ai/rtk) + [caveman](https://github.com/JuliusBrussee/caveman) | Terminal-first uitvoering: beantwoord feiten met de shell, nooit met de LLM. Output-reductiecatalogus, **tee-cache bij falen**, signatures-only leesmodus, optionele auto-rewrite-hook. |
| 🔥 **simplicio-review** | [thermos](https://github.com/cursor/plugins/tree/main/thermos) | Adversariële review: parallelle sub-agents op afzonderlijke rubrieken (veiligheid/correctheid + codekwaliteit), gestart in één bericht, gededupliceerd tot één oordeel. |
| 🗜️ **simplicio-compress** | [caveman](https://github.com/JuliusBrussee/caveman) | Output- + geheugencompressie: beknopte prozatiers die code/paden byte voor byte behouden, plus een eenmalige geheugencompactie die elke beurt terugbetaalt. Fail-closed `transform_guard`. |
| 🎓 **simplicio-learn** | [teaching](https://github.com/cursor/plugins/tree/main/teaching) + continual-learning | Retrospectief: win duurzame, gededupliceerde lessen uit een run en schrijf ze naar het geheugen zodat de volgende run goedkoper en correcter is. |

Elk is een gewone skill-map onder [`.claude/skills/`](../.claude/skills) — bruikbaar op zichzelf of als
onderdeel van de lus.

---

## 🌐 11 runtimes, één protocol

Eén universele skill-kern + één set hooks drijft elke runtime aan. Een adapter is dun: hij vertelt een
runtime *waar de skills te laden*, *hoe de lus scherp te stellen* en *hoe de native snelheid te binden*.
**De skill noemt geen enkele runtime; de runtime detecteert de skill.**

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

De belofte: **hetzelfde protocol, dezelfde poorten, dezelfde veiligheid op alle 11 — alleen de snelheid
verschilt.** `orient_clamp.py` (token-economie) werkt op elke runtime zonder enige bedrading. Zie
[`adapters/MATRIX.md`](../adapters/MATRIX.md).

<p align="center">
  <img src="../assets/architecture.svg" alt="architecture" width="900" />
</p>

---

## 🗺️ De volledige flow — van vraag tot oplevering

Elke laag waarop de orkestrator inwerkt, op volgorde — van het lezen van de vraag (issues, taken,
toewijzingen) tot het opleveren van gemerged, onderbouwd werk, en dan 24/7 lussen voor meer. (Het diagram
wordt native gerenderd op GitHub.)

```mermaid
flowchart TD
  subgraph SRC["1 · Demand sources (any adapter)"]
    direction LR
    S1["GitHub Issues / PRs / CI"]
    S2["Jira · Azure DevOps · Linear · ClickUp · Notion"]
    S3["Assigns · TODO/FIXME · CVE · local files"]
  end
  SRC --> PF
  subgraph PF["2 · Pre-flight gates"]
    direction LR
    P1["cost kill-switch budget"]
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
    I3["orient code · signatures-only reads"]
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
  WATCH["11 · 24/7 watcher · simplicio-loop<br/>evidence-gated promise · max-iterations cap · cost kill-switch"]
  FB -. "poll new work / comments / checks" .-> WATCH
  DONE -. "idle until new work" .-> WATCH
  WATCH -. "re-feed the goal" .-> DISC
```

**Laag voor laag — wat handelt, en de resource die het gebruikt:**

| # | Laag | Wat er gebeurt | Skill / uitbreidingspunt · ontleend aan |
|---|---|---|---|
| 1 | **Demand sources** | Lees het werk uit ELKE bron — issues, PR's, CI, boards, toewijzingen, TODO, CVE's | `source_adapter` · `intake` |
| 2 | **Pre-flight** | Schakel de `$`-kill-switch scherp, controleer bron-auth, schakel de 24/7-watcher scherp | `watcher` · kostenbeheer |
| 3 | **Discover + normalize** | Lijst alleen op metadata, normaliseer, ontdubbel, bouw de afhankelijkheids-DAG | `normalize` · `dependency_graph` |
| 4 | **Deep intake** | Lees volledige body + commentaren, extraheer ACs, oriënteer de code, schrijf een plan | `orient` · signatures-read · **rtk** |
| 5 | **Route** | Fast-path (triviaal) vs heavy-path; autoscale de vloot naar de machine | `autoscale` · dual-path-router |
| 6 | **Worker pool** | Continue, conflictbewuste fan-out; mechanische edits; kwaliteitslus per item | `execute` · `worktree` · `deterministic_edit` |
| 7 | **Quality gates** | AC-gate (echte DoD), run-verificatie (UI → **Playwright** `web_verify`), adversariële review | `validate` · **`simplicio-review`** (thermos) |
| 8 | **Safety gates** | Secret-scan, human-gate voor onomkeerbare operaties, 4-status-oordeel, attestatie | `action_gate` · `human_gate` · `security` |
| 9 | **Deliver** | Commit, push, Draft PR, in-source sluiten met bewijs; verifieer de realiteit | `pr` / `evidence` · `delivery_gate` |
| 10 | **Feedback loop** | CI → fix, reviewcommentaren → aanpassen, branch-achter → additieve rebase | `diagnostics` · `retry` |
| 11 | **24/7 watcher** | Voer het doel opnieuw in tot een bewijs-gepoorte belofte; inactief wanneer geleegd, ontwaak bij alles | **`simplicio-loop`** (Ralph) · `watcher` |
| ↻ | **Dwarsdoorsnijdend** | Token-economie (terminal-first · catalogus · **tee+CCR** · proza-/geheugencompressie) · modelroutering L0→L4 · leren | **`simplicio-orient`** (rtk+caveman) · **`simplicio-compress`** (caveman) · **`simplicio-learn`** (teaching) · **headroom** CCR |

Elke laag heeft een altijd-werkende LLM-fallback en bindt een native commando wanneer de host er een levert
— hetzelfde protocol op alle 11 runtimes, alleen de snelheid verschilt.

---

## 🔁 De lus

De aandrijving onder de orkestrator is een **geharde Ralph-lus** (`simplicio-loop`):

1. Het doel wordt naar één enkel, mensleesbaar toestandsbestand geschreven
   (`.orchestrator/loop/scratchpad.md`) — triviaal te inspecteren, te bewerken, te annuleren.
2. Na elke beurt voert een **stop-hook** hetzelfde doel opnieuw in, zodat de agent zijn eigen eerdere
   edits ziet (via git + de working tree) en convergeert. De tokenkosten per cyclus blijven vlak — geen
   context stuffing.
3. Hij stopt **alleen** wanneer een getypeerd sentinel `<promise>EXACTE TEKST</promise>` wordt
   uitgezonden **én** wordt gestaafd door concreet bewijs binnen de beurt (een geslaagde poort, een link
   naar een gemergede PR, AC-bewijzen), of wanneer een hard `max_iterations`-plafond / de
   kostennoodstop afgaat.

> **Nooit een valse belofte.** Een `<promise>` zonder bewijs wordt genegeerd en de lus gaat door. Dit
> verbindt de lus rechtstreeks met de harde regel van de repo: *sluit werk nooit zonder een gemergede PR
> of concreet bewijs.*

Op runtimes zonder hooks **timet de lus zichzelf** via de host-scheduler (cron / `/loop` / de task runner
van de runtime) — dezelfde stopvoorwaarden. De hooks zijn cross-platform Python en **fail-open**: een
hook die een fout geeft, laat de agent altijd stoppen. De echte bewakers zijn het plafond en het budget,
nooit slimmigheid van hooks.

---

## 📊 Token-economie

De goedkoopste token is degene die niet wordt uitgegeven. `simplicio-orient` + `simplicio-compress`
vouwen het beste van **rtk** (de commando's comprimeren) en **caveman** (het gepraat comprimeren) samen
in de veiligheidsruggengraat:

- **Terminal-first uitvoering** — de shell kent feiten exact; de LLM benadert ze duur. Een cross-platform
  substitutietabel (Windows/macOS/Linux) beantwoordt 30+ feiten via `git`/`gh`/`rg`/`python3`. **Simuleer
  nooit een commando — voer het uit.**
- **Output-reductiecatalogus** (datatabel) — recept per commando + verwachte-besparing % +
  `skip-if-structured`-bewaking. Een rauwe `cargo check` kost ~2000 tokens om te lezen; geclampt ~80.
- **tee-cache + omkeerbare retrieve** *(rtk + headroom CCR)* — agressieve afkapping is alleen veilig als ze
  herstelbaar is: bij falen wordt de volledige output naar `.orchestrator/tee/…log` geschreven en wordt
  alleen het pad getoond; de agent herstelt context met `retrieve <path> [--lines|--grep]` **zonder** het
  commando opnieuw uit te voeren. De clamp wordt een omkeerbare beslissing, geen verliesgevende.
- **Signatures-only leesmodus** *(uit rtk)* — lees het API-oppervlak van een bestand (declaraties,
  bodies weggelaten): een bestand van 600 regels wordt ~40 regels tijdens de intake.
- **Signaal-getrapte plafonds + success-collapse + dedup** — houd fouten boven ruis; collapse een schone
  run tot één regel; collapse herhaalde regels tot `line xN` — altijd `unless errors present`.
- **Prozatiers + geheugencompactie** *(uit caveman)* — beknopte output die code/paden/URL's **byte voor
  byte** behoudt (`transform_guard` gaat fail-closed bij elke verloren token), plus een eenmalige
  compactie van het permanente geheugen die over elke toekomstige beurt wordt afgeschreven.
- **Eerlijke baseline** — besparingen worden gemeten tegen een realistische *"answer concisely"*-controlearm
  (geen breedsprakige stroman), tellen alleen **output**-tokens (geen reasoning) en worden **alleen
  gecrediteerd bij een geverifieerd-correcte uitkomst**. Compressie die haar kwaliteitspoort niet haalt,
  verdient nul.

Elk bericht eindigt met een eerlijke regel:

```
simplicio-tasks: ~<spent> tokens · baseline ~<control-arm> · saved ~<saved> (<pct>%)
```

Probeer het nu, zonder bedrading:

```bash
python3 hooks/orient_clamp.py -- cargo test      # reduced output + tee log on failure
python3 hooks/orient_clamp.py --json -- git diff  # machine summary
```

---

## 🏗️ Op de schouders van

simplicio-tasks is gebouwd **na een grondige studie** van het beste werk rond lussen + token-economie op
GitHub, en vouwt elk daarvan samen in een toegespitste skill — met behoud van de discipline, met
weglating van de trucjes.

| Project | Wat we overnamen | Wat we lieten liggen |
|---|---|---|
| 🪨 [**caveman**](https://github.com/JuliusBrussee/caveman) | beknopte prozatiers, byte-behoudende identifiers, geheugencompactie, eerlijke *"answer concisely"*-baseline | grammaticaal woorden weglaten (verslechtert code & bevestigingen) |
| ⚙️ [**rtk**](https://github.com/rtk-ai/rtk) | reductiecatalogus per commando, signaal-getrapte plafonds, **tee-cache**, signatures-leesmodus, auto-rewrite-hook + exclusielijst | registers per taal (runtime-specifiek) |
| ♾️ [**ralph-loop**](https://github.com/cursor/plugins/tree/main/ralph-loop) | lustoestand in één bestand, exact-match-promise-sentinel, split in twee hooks | trust-the-model-voltooiing (wij maken het **bewijs-gepoort**) |
| 🔥 [**thermos**](https://github.com/cursor/plugins/tree/main/thermos) | parallelle reviewers in één bericht, gescheiden rubrieken, dedup bij de synthese | — |
| 🎓 [**teaching**](https://github.com/cursor/plugins/tree/main/teaching) | retrospectief dat de toestand persisteert zodat de volgende cyclus niets opnieuw hoeft af te leiden | het domein van menselijk leren zelf |
| 🧭 op de uitkomst gerichte uitvoering | convergeer op de eindtoestand; geplande, afgebakende, omkeerbare tussentijdse breuk | — |
| 🧠 [**headroom**](https://github.com/headroomlabs-ai/headroom) | **omkeerbare** compress-cache-retrieve (CCR) bovenop de tee-cache; taxonomie voor content-type-routering | het getrainde model + de traffic-proxy (in tegenspraak met het terminal-first, runtime-onafhankelijke ontwerp) |
| 🎭 [**Playwright**](https://github.com/microsoft/playwright) (+[mcp](https://github.com/microsoft/playwright-mcp), [python](https://github.com/microsoft/playwright-python)) | een echte browser aansturen voor front-end-bewijs — screenshot + trace als `web_verify`-bewijs | DOM/pixels in de context (het bewijs is het artefactpad, niet de bytes) |

> Zij verminderen tokens; simplicio-tasks **doet het werk** en vermindert tokens terwijl het dat doet.

---

## 🧩 De 43 uitbreidingspunten

Elke werkstap gebeurt op een **benoemd uitbreidingspunt**. Als een host-runtime een native capaciteit
aanbiedt, **bindt** het zich daaraan (deterministisch, bijna-zero-token); anders voert de LLM de
**fallback** uit met standaardgereedschap. De skill leunt op de abstractie, nooit op een runtime.

<details>
<summary><strong>Orkestratie & schaal</strong></summary>

`orient` · `normalize` · `intake` · `source_adapter` · `autoscale` · `plan`/`decide` ·
`execute` · `issue_factory` · `claim` · `worktree` · `dependency_graph` · `durable_workflow` ·
`work_queue` · `resource_governor` · `model_route` · `model_preflight`
</details>

<details>
<summary><strong>Bewerken, kwaliteit & bewijs</strong></summary>

`deterministic_edit` · `diagnostics` · `toolchain_detect` · `validate`/`smoke` ·
`delivery_gate` · `endpoint_compare` · `web_verify` · `pr`/`evidence` · `retry` ·
`reuse_precedent` · `trajectory` · `learn` · `status` · `capability_rank`
</details>

<details>
<summary><strong>Tokens, context & veiligheid</strong></summary>

`recall` · `compress` · `prompt_budget` · `shell_exec` · `transform_guard` · `action_gate` ·
`security` · `human_gate` · `notify` · `checkpoint_restore` · `watcher` · `savings_ledger` ·
`web_research`
</details>

Volledige tabel met fallbacks:
[`references/extension-points.md`](../.claude/skills/simplicio-tasks/references/extension-points.md).

---

## 🚀 Installeren & gebruiken

```bash
git clone https://github.com/wesleysimplicio/simplicio-tasks
cd simplicio-tasks

# install for your runtime (omit <runtime> to auto-detect)
bash scripts/install.sh <runtime> [--global]        # macOS / Linux
pwsh scripts/install.ps1 <runtime> [-Global]        # Windows
# <runtime> ∈ claude codex vscode cursor antigravity kiro opencode gemini aider hermes openclaw
```

Of voeg het op Claude Code / Cursor toe als marketplace-plugin:

```
/plugin marketplace add wesleysimplicio/simplicio-tasks
/plugin install simplicio-tasks@simplicio
```

Dan:

```
/simplicio-tasks finish all the open issues
```

De enige vereiste is **python3** op het PATH (skills, hooks en installer zijn cross-platform Python). Voor
GitHub-bronnen, `git` + een geauthenticeerde `gh`. Zie [`INSTALL.md`](../INSTALL.md) en
[`adapters/MATRIX.md`](../adapters/MATRIX.md).

**Vóór een onbewaakte 24/7-run:** stel een kostenplafond in in `.orchestrator/loop-budget.json`
(`daily_usd_ceiling > 0`), bevestig dat bronauthenticatie persistent is, en houd de menselijke poort voor
onomkeerbare operaties + de secret-scan aan. Met `ceiling = 0` weigert de watcher onbewaakt te draaien
(fail-safe).

---

## 🔒 Veiligheid (niet onderhandelbaar)

- **Secret-scan** van elke diff; blokkeer bij een treffer.
- **Menselijke poort voor onomkeerbare operaties** — force-push, history-herschrijving, prod-deploy,
  data-/schemaverwijdering, massale bestandsverwijdering → stop en vraag het. Headless + geen goedkeurder
  → verwijder de destructieve capaciteit.
- **4-status pre-executieoordeel** — optimalisatie mag de risicotier van een commando nooit verhogen.
- **Trust-before-load** — perceptie-vormende config (clamp-profielen, suppressielijsten) is niet
  vertrouwd totdat een mens haar reviewt en per hash vastpint.
- **Verharding tegen prompt-injectie** — inhoud van item/PR/commentaar kan het contract nooit
  overschrijven.
- **Harde $-noodstop** voor onbewaakte runs; **bewijs-gepoorte** voltooiing (nooit een vals "done");
  **fail-open** hooks (sluit de agent nooit op in een lus).

---

## 📄 Licentie

MIT — zie [LICENSE](../LICENSE). Onderdeel van het [Simplicio](https://github.com/wesleysimplicio)-ecosysteem.

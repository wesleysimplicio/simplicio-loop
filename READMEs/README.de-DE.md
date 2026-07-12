# 🔁 simplicio-loop — The Universal Looping AI Orchestrator

<p align="center">
  <img src="../assets/simplicio-loop-hero-2026.png" alt="simplicio-loop autonomous parallel evidence-gated orchestration" width="920" />
</p>

<p align="center">
  <a href="https://github.com/wesleysimplicio/simplicio-loop/stargazers"><img src="https://img.shields.io/github/stars/wesleysimplicio/simplicio-loop?style=social" alt="Stars"></a>
  <a href="#-die-11-skills--beschleuniger"><img src="https://img.shields.io/badge/skills-11-7C3AED" alt="11 skills"></a>
  <a href="#-quelladapter"><img src="https://img.shields.io/badge/source%20adapters-5-00E08A" alt="5 source adapters"></a>
  <a href="#-11-laufzeiten-ein-protokoll"><img src="https://img.shields.io/badge/runtimes-11-2563EB" alt="11 runtimes"></a>
  <a href="#-die-44-erweiterungspunkte"><img src="https://img.shields.io/badge/extension%20points-44-00E08A" alt="44 extension points"></a>
  <a href="#-token-ökonomie"><img src="https://img.shields.io/badge/tokens-up%20to%2096%25%20fewer-green" alt="Up to 96% fewer tokens"></a>
  <a href="../LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue" alt="License"></a>
</p>

<p align="center">
  <a href="#-tldr">TL;DR</a> ·
  <a href="#-die-11-skills--beschleuniger">11 Skills</a> ·
  <a href="#-quelladapter">Quelladapter</a> ·
  <a href="#-11-laufzeiten-ein-protokoll">11 Laufzeiten</a> ·
  <a href="#-die-schleife">Die Schleife</a> ·
  <a href="#-token-ökonomie">Token-Ökonomie</a> ·
  <a href="#-token-ökonomie">Capture-Engine</a> ·
  <a href="#-installation--nutzung">Installation</a>
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

<!-- visual-story:start -->
## 🚀 Die neue Generation — ein Betriebssystem für verifizierte Agentenarbeit

**simplicio-loop ist weit mehr als ein Prompt, der bis zum Abschluss wiederholt wird.** Es übersetzt Absicht in einen eingefrorenen Task-Vertrag, kartiert das Repository, plant nach Abhängigkeiten, verteilt die Ausführung auf isolierte Worktrees, sammelt strukturierte Belege, verifiziert unabhängig, rollt sicher zurück, merkt sich jeden Versuch und hält die maßgebliche Quelle bis zur Auslieferung synchron.

- **Vertrag zuerst** — Akzeptanzkriterien, Abhängigkeiten, Risiken, Quellenstatus und Completion Oracle sind vor der Ausführung explizit.
- **Parallel ohne Korruption** — bereite Tasks laufen in isolierten Lanes/Worktrees und konvergieren über ein operatives Ledger.
- **Beweis vor Abschluss** — Tests, Impact-/Flow-Prüfungen, Watcher-Challenges, Lieferbelege und HBP-Evidenz weisen falsche Done-Zustände zurück.
- **Gedächtnis, das Verhalten ändert** — Journal, Stall Detector, Checkpoints und Cross-Agent-Wiki verhindern Oszillation und machen Handoffs dauerhaft.

<p align="center">
  <img src="../assets/simplicio-loop-parallel-worktrees.png" alt="simplicio-loop parallel isolated worktree execution" width="920" />
</p>

<p align="center"><em>Abhängigkeitsgesteuerter Fan-out: isolierte Worker arbeiten parallel, liefern Evidenz zurück und konvergieren zu einer verifizierten Lieferung.</em></p>

<p align="center">
  <img src="../assets/simplicio-loop-lifecycle-2026.svg" alt="simplicio-loop lifecycle from intake to durable memory" width="920" />
</p>

<p align="center"><em>Jede Stufe ist explizit, begrenzt, beobachtbar und reversibel.</em></p>

<p align="center">
  <img src="../assets/simplicio-loop-evidence-memory.png" alt="simplicio-loop evidence memory verification rollback and completion" width="920" />
</p>

<p align="center"><em>Evidenz und Gedächtnis sind Teil des Ausführungspfads — kein nachträglich geschriebener Bericht.</em></p>

Diese Architektur macht aus einem Ziel ein gesteuertes Liefersystem: von einer schwierigen Aufgabe bis zum gesamten Backlog, über Sessions und Runtimes hinweg, mit Local-first-Operatoren und Belegen, die Menschen, CI oder andere Agenten auditieren können.

<p align="center">
  <img src="../assets/simplicio-loop-architecture-2026.svg" alt="simplicio-loop control execution evidence and delivery planes" width="920" />
</p>
<!-- visual-story:end -->

## ⚡ TL;DR

**simplicio-loop** ist ein laufzeitunabhängiges **Super-Plugin** — ein autonomer, schleifenfähiger
Orchestrator (aufgerufen als **`/simplicio-loop`**) plus **fünf Satelliten-Skills** — das jedes
starke LLM (Claude, Codex, Copilot, Gemini, Cursor, lokale Modelle) in einen selbstfahrenden Worker
verwandelt. Du richtest es auf einen Arbeitsumfang aus — *„schließe alle offenen Issues ab"*,
*„arbeite die CI-Warteschlange ab"*, *„leere das Jira-Board"* — und es durchläuft den gesamten
Lebenszyklus eigenständig:

> **entdecken → verstehen → entscheiden → handeln → verifizieren → korrigieren → festhalten → wiederholen**

Es entdeckt Arbeit aus jeder beliebigen Quelle (GitHub Issues, Jira, Azure DevOps, agentsview-Sessions
und mehr), entfernt Duplikate, skaliert eine Agentenflotte automatisch auf deine Maschine, setzt jedes
Element über eine Qualitätsschleife um, die **den Code ausführt (nicht nur kompiliert)**, eröffnet PRs,
löst CI-/Review-Feedback auf, merged und behält **rund um die Uhr** neue Arbeit im Blick — alles hinter
Sicherheits-Gates und einem expliziten STOP/Cancel-Pfad.

```text
/simplicio-loop finish all open issues
→ identity + pre-flight (auth, runtime, STOP path)
→ discover 50 issues · dedup · build dependency DAG
→ autoscale fleet = 14 · pipeline implement→review→merge
→ each item: read body+ACs → orient code → plan → edit → run → verify → PR
→ merge · close with evidence · rollback if main breaks
→ keep looping every ~2 min until the queue is dry (evidence-gated, never a false "done")
```

Drei Dinge machen es anders: es ist ein **Super-Plugin aus fokussierten Skills**, es führt **dasselbe
Protokoll auf 11 Laufzeiten** aus, und es tut all das mit **aggressiver, ehrlicher Token-Ökonomie**.

---

## 📘 Offizielles Fähigkeitenverzeichnis

Das vollständige, offizielle Verzeichnis dessen, was `simplicio-loop` mitbringt — jede Fähigkeit unten ist
**real, ausführbar und getestet** (`python3 scripts/check.py`: Claims-Audit 4/4 + 28 Tests). Jede verlinkt
auf ihren ausführlichen Abschnitt und ihren Worker.

| Fähigkeit | Was sie tut | Nachweis / Worker | Details |
|---|---|---|---|
| 🎬 **Video-Belege** (`video_evidence`) | Zeichnet die **echte Browser-Session** als bewegten Beleg auf, dass eine UI-Änderung funktioniert (Playwright, Standard); rendert für eine explizite Erklärvideo-Anfrage (`/simplicio-loop make a video of screen X`) eine **deterministische, beschriftete MP4** mit [hyperframes](https://github.com/heygen-com/hyperframes) | `scripts/video_evidence.py` · BLOCKIERT (niemals Fake-Pass) ohne die Toolchain | [§ Video-Belege](#-video-belege--playwright-standardmäßig-hyperframes-auf-anfrage) |
| 🧠 **Versuchsgedächtnis + Stall-Detektor** | Ein dauerhaftes Run-Journal (`.orchestrator/loop/journal.jsonl`) + ein Stall-Detektor, sodass die Schleife **die Strategie ändert, statt zu oszillieren**; inkrementelle Triage (`since`) liest jede Runde nur das Delta | `scripts/loop_journal.py` · `selftest` 9/9 | [§ Anti-Oszillation](#-versuchsgedächtnis--stall-detektor-anti-oszillation) |
| 🔒 **Fail-closed-Sicherheits-Gate** (`action_gate`) | Ein `PreToolUse`-/git-pre-push-Hook, der Force-Push, History-Rewrite, Massenlöschung, destruktives DDL, Infra-Teardown und secret-behaftete Commits/Pushes **mechanisch blockiert** — Schritt 5 ausführbar gemacht, nicht als Prosa | `hooks/action_gate.py` · `selftest` 15/15 | [§ Sicherheit](#-sicherheit-nicht-verhandelbar) |
| 🔬 **Lokale Verifikation** | Eine Test-Suite (Worker-Selftests + ein **E2E des Schleifentreibers**, der den nachweis-gegateten Ausgang beweist) + ein **Claims-Audit** (referenzierte Skripte existieren · Zählungen konsistent · `_bundle ≡ source`) — alles lokal, **kein bezahltes CI** | `scripts/check.py` · `scripts/claims_audit.py` · `tests/` | [§ Tests & lokale Prüfungen](#-tests--lokale-prüfungen-kein-bezahltes-ci) |
| ✅ **Ehrliche Einsparungen** | Die Einsparungszeile ist nun **nachweis-gegatet, nicht obligatorisch** — eine Zahl wird nur mit einem gemessenen Beleg gezeigt (Clamp/Signaturen/Cache/`deterministic_edit`/Ledger); niemals erfunden | Token-Ökonomie-Vertrag | [§ Token-Ökonomie](#-token-ökonomie) |

Zwei Schleifen-**Modi** machen die Beendigung explizit: **converge** (eine einzelne harte Aufgabe — endet
mit dem nachweis-gegateten `<promise>` oder einer Stall-Eskalation) vs. **drain** (eine Warteschlange — endet,
wenn die erneute Quellabfrage K Runden lang leer bleibt). Beide befolgen weiterhin die universellen Ausgänge
Both modes are still governed by universal exits: promise+evidence, `max_iterations`, and STOP.

> Loop-Bewertung über diese Arbeitslinie: **7,5** (starkes Design, unbewiesen) → **9** (Versuchsgedächtnis +
> Anti-Oszillation) → **9,5** (reproduzierbarer lokaler Beweis) → **~10** (erzwungene Sicherheit + vollständige
> Schleifensemantik). Die Verifikationsinfrastruktur fängt nun die eigenen Regressionen des Projekts ab, während es wächst.

---

## 🧠 Die 11 Skills & Beschleuniger

Der Orchestrator-Kern + fünf Satelliten + fünf Beschleuniger/Integrationen. Jeder Satellit ist
**optional** — wenn geladen, delegiert der Orchestrator an ihn (reichhaltiger + günstiger); wenn nicht
vorhanden, deckt das Inline-Protokoll 100 % ab. Beschleuniger werden **automatisch erkannt** — vorhanden
= genutzt, fehlend = LLM-Fallback.

| # | Fähigkeit | Greift auf | Was sie tut | Token-Auswirkung |
|---|---|---|---|---|
| 1 | 🔁 **simplicio-loop** | — | Unified public entrypoint: orchestrator core + hardened loop behind one command | Core + loop |
| 2 | ↩️ **simplicio-tasks** | legacy alias | Compatibility shim for older installs and saved prompts | Legacy alias |
| 3 | 🧱 **simplicio-orient** | [rtk](https://github.com/rtk-ai/rtk) + [caveman](https://github.com/JuliusBrussee/caveman) | Terminal-first-Ausführung, Ausgabe-Reduktionskatalog, tee-Cache, Signaturen-Lesemodus | L0 deterministisch |
| 4 | 🔥 **simplicio-review** | [thermos](https://github.com/cursor/plugins/tree/main/thermos) | Parallele adversariale Review auf eigenen Rubriken → dedupliziertes Urteil | Qualitäts-Gate |
| 5 | 🗜️ **simplicio-compress** | [caveman](https://github.com/JuliusBrussee/caveman) | Ausgabe- + Memory-Kompression, fail-closed `transform_guard` | 40–60 % weniger |
| 6 | 🎓 **simplicio-learn** | [teaching](https://github.com/cursor/plugins/tree/main/teaching) | Post-Run-Retrospektive → dauerhafte, deduplizierte Lektionen im Memory | Klüger pro Lauf |
| 7 | 🧭 **Understand Anything** | [Egonex-AI](https://github.com/Egonex-AI/Understand-Anything) | Knowledge-Graph-Orientierung: semantische Suche, geführte Touren, Abhängigkeitsgraph | **L0 null Tokens** |
| 8 | 📊 **agentsview** | [kenn-io](https://github.com/kenn-io/agentsview) | Session-Analytik, Kostenverfolgung, Erkennung blockierter Sessions | **L1** nur SQL |
| 9 | ⚡ **LMCache** | [LMCache](https://github.com/LMCache/LMCache) | KV-Cache zwischen Schleifenrunden — 40–70 % TTFT-Reduktion bei lokalen Modellen | GPU-Zeit ↓ |
| 10 | 🗜️ **Simplicio-Capture-Engine** | `engine/simplicio_engine.py` (nativ, nur stdlib) | Transparenter Capture-Proxy: leitet an den echten Provider weiter, misst + komprimiert deterministisch, schreibt `proxy_savings.json` | **deterministisch** |
| 11 | 🎬 **video_evidence** | Playwright (Standard) · [hyperframes](https://github.com/heygen-com/hyperframes) (auf Anfrage) | Zeichnet die **echte Session** als bewegten Beleg einer UI-Änderung auf (Playwright); rendert mit hyperframes ein **deterministisches, beschriftetes MP4**-Erklärvideo, wenn das Video selbst das Liefergut IST | Beleg-Produzent |

Jeder Skill liegt unter [`.claude/skills/`](../.claude/skills); jeder Beschleuniger hat ein Referenzdokument
unter `.claude/skills/simplicio-loop/references/` (der Video-Produzent:
[`video-evidence.md`](../.claude/skills/simplicio-loop/references/video-evidence.md), Worker
[`scripts/video_evidence.py`](../scripts/video_evidence.py)).

---

## 📡 Quelladapter

Der Orchestrator entdeckt Arbeit aus jeder Quelle über einsteckbare Adapter. Jeder bietet sechs Verben:
`list_ready`, `get_details`, `claim`, `update_status`, `attach_evidence`, `close`.

| Quelle | Adapter | Zweck |
|---|---|---|
| GitHub Issues/PRs | `gh` CLI (nativ) | Primäre Arbeitselement-Quelle |
| Jira / Asana / ClickUp / Linear / Notion | Host-Connector | Board-/Projektverwaltung |
| Trello / Azure DevOps | `az boards`-Adapter | Azure-Arbeitsverfolgung |
| **agentsview-Sessions** | `scripts/agentsview_adapter.py` | Wiederherstellung blockierter Sessions + Kostentransparenz |
| Lokale Dateien / CI-Warteschlange | Dateisystem / CI-API | Interne Arbeitsverfolgung |

Siehe das Referenzdokument jedes Adapters unter `.claude/skills/simplicio-loop/references/`.

---

## 🌐 11 Laufzeiten, ein Protokoll

Ein universeller Skill-Kern + ein Satz Hooks treibt jede Laufzeit an. Ein Adapter ist dünn: er sagt
einer Laufzeit, *wo die Skills geladen werden*, *wie die Schleife scharfgeschaltet wird* und *wie die
native Geschwindigkeit gebunden wird*. **Die Skill benennt keine Laufzeit; die Laufzeit erkennt die
Skill.**

| Laufzeit | Skill-Laden | Schleifenantrieb | Native Bindung |
|---|---|---|---|
| **Claude Code** | `.claude/skills/` + plugin | `Stop`-Hook | MCP |
| **Codex** | `AGENTS.md` | selbstgetaktet | MCP / Adapter |
| **VS Code (Copilot)** | `copilot-instructions.md` | tasks | MCP |
| **Cursor** | `.cursor-plugin/` | `stop`+`afterAgentResponse` | MCP / rules |
| **Antigravity** | rules / `AGENTS.md` | selbstgetaktet | MCP |
| **Kiro** | `.kiro/steering/` | specs | MCP |
| **OpenCode** | `AGENTS.md` | selbstgetaktet | MCP |
| **Gemini** | `GEMINI.md` | selbstgetaktet | MCP / Adapter |
| **Aider** | `CONVENTIONS.md` | selbstgetaktet | — (LLM-Fallback) |
| **Simplicio Agent** | native recall | native Schleife | **nativ** |
| **OpenClaw** | plugin SDK | nativer Scheduler | **nativ** |

Das Versprechen: **dasselbe Protokoll, dieselben Gates, dieselbe Sicherheit auf allen 11 — nur die
Geschwindigkeit unterscheidet sich.** `orient_clamp.py` (Token-Ökonomie) funktioniert auf jeder
Laufzeit ohne jegliche Verdrahtung. Siehe [`adapters/MATRIX.md`](../adapters/MATRIX.md).

---

## 🗺️ Der vollständige Ablauf — von der Anforderung zur Auslieferung

Jede Ebene, auf die der Orchestrator einwirkt, der Reihe nach — vom Lesen der Anforderung (Issues, Tasks,
Zuweisungen) bis zur Auslieferung gemergter, belegter Arbeit, dann das Schleifen rund um die Uhr für mehr.

```mermaid
flowchart LR
  IN["Intent: issue · task · queue"] --> CONTRACT["1 · Freeze task contract"]
  CONTRACT --> MAP["2 · Map source + normalize"]
  MAP --> PLAN["3 · Dependency DAG + acceptance criteria"]
  PLAN --> ROUTE{"4 · Ready task?"}
  ROUTE -->|"solo / small"| SOLO["Targeted lane"]
  ROUTE -->|"parallel / medium+"| FAN["Bounded fan-out"]
  FAN --> A["Isolated worktree A"]
  FAN --> B["Isolated worktree B"]
  FAN --> C["Isolated worktree C"]
  SOLO --> VERIFY["5 · Test + impact/flow evidence"]
  A --> VERIFY
  B --> VERIFY
  C --> VERIFY
  VERIFY --> RECEIPT["Watcher challenge + evidence receipt"]
  RECEIPT --> ORACLE{"6 · Completion oracle"}
  ORACLE -->|"pending / blocked"| RECOVER["Journal · checkpoint · rollback · backlog-only maintenance"]
  RECOVER --> PLAN
  ORACLE -->|"verified / measured"| DELIVER["7 · Source sync · PR · merge"]
  DELIVER --> MEMORY["8 · Ledger · wiki · durable attempt memory"]
  MEMORY --> WATCH["9 · Re-feed · watcher · STOP path"]
  WATCH -->|"new work"| IN
```

---

## 🔁 Die Schleife

Die **nachweis-gegatete Schleife** ist der Kernmechanismus. Sie speist dasselbe Ziel in jeder Runde
erneut ein, sodass der Agent seine eigene frühere Arbeit sieht. Der Ausgang erfolgt NUR über:

1. **Nachweis-gegateter `<promise>`** — die Runde, die das Versprechen ausgibt, MUSS auch konkrete
   Belege tragen (bestandener Test, gemergter PR, erneute Abfrage des geschlossenen Elements). Ein
   Versprechen ohne Belege = ignoriert.
2. **`max_iterations`-Obergrenze** — harter Sicherheitsanschlag
3. **STOP/cancel path** — explicit STOP file or channel command stops unattended runs
4. **STOP-Signal** — `.orchestrator/STOP` oder Kanalbefehl

Zwischen den Runden cached LMCache (sofern verfügbar) den KV-Zustand, sodass das erneute Einspeisen
nahezu null Prefill kostet.

### 🧠 Versuchsgedächtnis + Stall-Detektor (Anti-Oszillation)

Eine Re-Feed-Schleife, die sich nichts merkt, oszilliert — versuche X, scheitere, versuche X erneut — bis
die Obergrenze verbrennt. simplicio-loop führt ein **dauerhaftes Run-Journal**
(`.orchestrator/loop/journal.jsonl`, nur anhängend: `iteration · action · hypothesis · gate ·
error-fingerprint`) und einen **Stall-Detektor** ([`scripts/loop_journal.py`](../scripts/loop_journal.py),
deterministisch + modellfrei):

- **Fehler-Fingerprint** — die Ausgabe des fehlgeschlagenen Gates wird auf einen stabilen Hash reduziert,
  wobei Zeilennummern, Pfade, Hex/UUIDs, Zeitstempel und Dauern wegnormalisiert werden, sodass *derselbe*
  Bug über Runden hinweg erkannt wird, selbst wenn der nebensächliche Text abweicht.
- **Stall = K identische Fingerprint-Fehler in Folge** (Standard K=3). Ein wechselnder Fingerprint bedeutet,
  dass die Schleife sich bewegt (PROGRESS); derselbe K-mal bedeutet, dass sie sich im Kreis dreht (STALLED).
- Bei STALLED speist die Schleife **nicht** dasselbe Ziel erneut ein — sie benennt die zu vermeidenden
  **Sackgassen-Aktionen**, **wechselt dann die Strategie** oder **eskaliert an das Human-Gate** mit dem
  Fingerprint.
- `loop_journal.py resume` wird zu Beginn jeder Runde gelesen, sodass ein frischer Prozess fortfährt, ohne
  frühere Versuche neu herzuleiten (echtes Resume) und niemals eine bekannte Sackgasse erneut versucht.

```bash
loop_journal.py resume                       # what was tried + dead-ends to avoid
loop_journal.py record --iteration N --action "…" --gate fail --gate-output test.log
loop_journal.py stall --k 3 --exit-code      # PROGRESS → re-feed · STALLED → switch/escalate
```

---

## 🎬 Video-Belege — Playwright standardmäßig, hyperframes auf Anfrage

Die Schleife erzeugt **Demonstrationsvideos** als Beleg, dass eine Änderung funktioniert — **zwei Engines**,
ein `video_evidence`-Erweiterungspunkt (Worker
[`scripts/video_evidence.py`](../scripts/video_evidence.py), Vertrag
[`references/video-evidence.md`](../.claude/skills/simplicio-loop/references/video-evidence.md)):

1. **Standard — der normale Beleg-Ablauf nutzt Playwright.** Nach einer UI-Änderung zeichnet
   `video_evidence` die **echte Browser-Session** auf, die den Bildschirm steuert (natives Playwright-Video →
   `.webm`, → `.mp4` mit FFmpeg) — der stärkste „funktioniert, nicht nur kompiliert"-Beleg (Schritt 4b) und
   ein gültiger nachweis-gegateter `<promise>`.

   ```bash
   python3 scripts/video_evidence.py verify --url http://localhost:3000/login \
       --name login-demo --expect "Sign in" --issue 42 [--upload --pr 42]
   ```

2. **Auf Anfrage — ein personalisiertes Erklärvideo nutzt hyperframes.** Wenn das Liefergut selbst ein
   Video IST („make an explainer video of screen X"), rendert der Orchestrator eine **deterministische,
   beschriftete Diashow** der `web_verify`-Screenshots mit
   [**hyperframes**](https://github.com/heygen-com/hyperframes) (von HeyGen — „gleiche Eingabe, gleiche
   Frames, gleiche Ausgabe", CI-reproduzierbar, keine API-Keys, lokales Rendering via Headless-Chrome +
   FFmpeg).

   ```text
   /simplicio-loop make an explainer video of the system login screen
   → detect: video-creation request → web_verify captures the screens
   → video_evidence verify --engine hyperframes → deterministic MP4 → attached to the PR
   ```

Beide Engines: ein Video, das nie aufgezeichnet/gerendert wurde, ergibt **BLOCKED**, niemals einen Fake-Pass.
Der Beleg ist stets ein **Dateipfad + boolesches Urteil** — niemals Video-Bytes im Kontext (Token-Ökonomie).

---

## 📊 Token-Ökonomie

| Technik | Einsparung |
|---|---|
| `deterministic_edit` (L0) | 100 % der Edit-Tokens (Datei mechanisch geschrieben, niemals vom LLM) |
| Terminal-first-Ausführung | Fakten aus der Shell, nicht aus LLM-Halluzination |
| Ausgabe-Reduktionskatalog | Obergrenzen pro Befehlstyp (`CAP_ERRORS=20`, `CAP_WARNINGS=10`, `CAP_LIST=20`) — `orient_clamp.py` |
| Tee+CCR-Cache bei Fehler | Niemals einen fehlgeschlagenen Befehl erneut ausführen — die gecachte Ausgabe lesen |
| Signaturen-only-Lesemodus | `simplicio-cli signatures <file>` — 870-Zeilen-Datei → 65 Zeilen (**93 % gespart**), Bodies entfernt |
| `simplicio-compress` | Knappe Prosa + einmalige Memory-Verdichtung |
| `orient_clamp.py` | Klemmung + tee bei jedem Shell-Befehl, ohne Verdrahtung |
| Nativer Response-Cache | wiederholte deterministische (temp=0) Anfrage → aus dem Cache bedient, überspringt den LLM-Aufruf (**100 % bei Treffer**) — `simplicio-cli cache`, standardmäßig aktiv (`SIMPLICIO_CACHE=0` zum Deaktivieren) |
| Simplicio-Capture-Proxy + MCP | 60–95 % weniger Tokens bei Tool-Ausgaben über einen transparenten Kompressions-Daemon |

Einsparungen zählen nur bei einem verifiziert-korrekten Ergebnis. Baseline = der günstigste sinnvolle
nicht-orchestrierte Weg zum selben Resultat. **Die Einsparungsmeldung ist nachweis-gegatet, nicht
obligatorisch:** eine Einsparungszahl wird nur gezeigt, wenn eine Runde tatsächlich einen ökonomie-
erzeugenden Befehl ausgeführt hat und die Zahl auf einen gemessenen Beleg zurückführbar ist (Clamp-tee,
Signaturen-Lesemodus, Cache-Treffer, `deterministic_edit`, `savings_ledger`). Keine gemessene Ökonomie →
keine Einsparungszeile; der Orchestrator erfindet niemals eine Baseline oder einen Prozentsatz. Siehe
`references/token-economy.md`.

### 🔎 `simplicio-loop` ausführen: Ökonomie vs. Messung (pro Laufzeit)

Zwei verschiedene Dinge passieren, wenn du **`simplicio-loop`** aufrufst, und sie verhalten sich pro Laufzeit unterschiedlich:

- **Ökonomie** — Kompression, Ausgabe-Klemmungen, Signaturen-only-Lesemodus, `deterministic_edit` — greift
  **jedes Mal, wenn die Skill läuft und `simplicio-orient` / `simplicio-compress` lädt, auf jeder Laufzeit.**
  Es ist das Verhalten der Skill plus die Hooks (am stärksten dort, wo Hooks existieren: `orient_clamp.py`
  klemmt automatisch auf Claude und Cursor; anderswo ist es instruktionsgetrieben).
- **Messung** — die Live-Zahlen des Token Monitors — zählt nur Traffic, der **durch den Capture-Proxy**
  fließt.

| Laufzeit | Ökonomie (Skill) | Messung (Monitor) |
|---|---|---|
| **Simplicio Agent** | ✓ | ✓ **automatisch** — bereits durch den Proxy geroutet (`base_url → :8788`) |
| **Claude** | ✓ (Skill + Hooks) | ✗ standardmäßig — Claude spricht direkt mit `api.anthropic.com`; gemessen erst nach Routing (`simplicio-cli wrap claude` oder `ANTHROPIC_BASE_URL → http://127.0.0.1:8788`) |
| **Codex** | ✓ (Skill) | ✗ standardmäßig — `simplicio-cli init codex` fügt die MCP-Tools hinzu, routet aber keinen LLM-Traffic; gemessen mit `simplicio-cli wrap codex` oder einer OpenAI-base-url, die auf den Proxy zeigt |

Also: die **Einsparungen passieren auf jeder Laufzeit**; der **Monitor zählt sie automatisch auf Simplicio Agent** und
auf Claude/Codex nach einem **einmaligen Routing-Schritt** (`simplicio-cli wrap …` / base-url → `:8788`). Ohne
Routing greift die Ökonomie weiterhin — der Monitor zählt diese Tokens nur nicht. `scripts/simplicio-economy.sh wire`
erledigt dieses Routing für OpenAI-kompatible Clients zur Installationszeit.

### 📈 Simplicio Token Monitor

Eine Live-Ansicht der Einsparungen, immer aktiv:

- **Web-Dashboard** — `http://127.0.0.1:9090` — Echtzeit-Token-Chart, Einsparungs-Anzeige, die LLMs/Laufzeiten
  und **141/144 Provider (98 %)**, die wir abfangen, plus ein Live-Proxy-Log.
- **Menüleisten-/Tray-Widget** — live eingesparte Tokens im System-Tray (macOS rumps · Windows/Linux pystray).
- **Ein Modul** — `scripts/simplicio-economy.sh {status|up|wire}` startet den Capture-Proxy + Monitor + Tray +
  den deterministischen `simplicio-dev-cli`-Operator und meldet den gesamten Stack.

Die Installation registriert alle drei als Autostart-Dienste (macOS launchd · Linux systemd · Windows Startup)
über `scripts/setup_simplicio.sh` oder das plattformübergreifende `python3 scripts/install_services.py install`.
Nach der Installation laufen Monitor + Capture **ohne die Schleife aufzurufen** — siehe `references/token-capture.md`.

### 🛠️ Die Capture-Engine — ein natives Modul, jeder Befehl

[`engine/simplicio_engine.py`](../engine/simplicio_engine.py) ist die native Simplicio-Capture-Engine
(nativ, nur stdlib, fail-open, ohne externe Abhängigkeit). Führe jeden Befehl
über den [`scripts/simplicio-engine`](../scripts/simplicio-engine)-Wrapper aus (z. B. `simplicio-engine doctor`):

| Befehl | Was er tut |
|---|---|
| `proxy` | der transparente Capture-Proxy — leitet jedes Modell an seinen **echten** Provider, komprimiert + misst + cached (kein Modellwechsel) |
| `doctor` | Proxy-Erreichbarkeit + Lebenszeit-Einsparungen |
| `cache` | nativer Response-Cache (`stats`/`clear`) — eine wiederholte deterministische Anfrage wird aus dem Cache bedient und überspringt den LLM-Aufruf |
| `signatures` | Signaturen-only-Ansicht einer Quelldatei (Bodies entfernt, ~93 % weniger Tokens zum Lesen von Code) |
| `semantic` | umkehrbare extraktive (semantic-lite) Kompression |
| `detect` | Content-Type-Erkennung + intelligentes Routing pro Block |
| `rag` | TF-IDF (oder `--ml`-Embedding) Retrieval über den CCR-Memory-Store |
| `memory` | CCR-Compress-Cache-Retrieve-Store (`remember`/`recall`/`forget`/`list`/`stats`) |
| `mcp` | nativer stdio-MCP-Server (compress / retrieve / stats Tools) |
| `init` / `wrap` | Simplicio in einen Client registrieren (Claude / Codex / Copilot / OpenClaw) · einen Client mit Capture-Routing ausführen |
| `report` / `audit` / `capture` / `evals` | Einsparungsbericht · einen Baum auf Kompressionspotenzial prüfen · eine Anfrage als Trockenlauf · Kompressions-Regressions-Gate |

---

## 🏛️ Designsäulen (im Detail)

Vier Mechanismen tragen die Orchestrierungskraft:

| Säule | Fokus | Lebt in |
|---|---|---|
| **DAG + Pipeline** | Parallelität nach Abhängigkeit, gestaffelt pro Element | `references/orchestration.md` (Schritt 3 Pool + Pipeline) |
| **Worktree-Isolation** | parallele Edits ohne den Baum zu beschädigen, merge-gegatet | `references/orchestration.md` |
| **Adversariale Verifikation** | ein Gremium von Skeptikern vor „delivered" | `references/quality-safety-delivery.md` · Skill `simplicio-review` |
| **Bounded loop cap** | anti-infinite-loop, evidence-gated exit | `references/standing-loop-247.md` · skill `simplicio-loop` |

---

## 🚀 Installation & Nutzung

```bash
git clone https://github.com/wesleysimplicio/simplicio-loop
cd simplicio-loop

# install for your runtime (omit <runtime> to auto-detect)
bash scripts/install.sh <runtime> [--global]        # macOS / Linux
pwsh scripts/install.ps1 <runtime> [-Global]        # Windows
# <runtime> ∈ claude codex vscode cursor antigravity kiro opencode gemini aider simplicio_agent openclaw
```

Oder installiere es auf Claude Code / Cursor direkt aus dem neuesten GitHub-Release (kein Marketplace):

```bash
gh release download --repo wesleysimplicio/simplicio-loop --archive tar.gz
tar xzf simplicio-loop-*.tar.gz && cd simplicio-loop-*/
bash scripts/install.sh claude    # or: bash scripts/install.sh cursor
```

Dann:

```
/simplicio-loop finish all the open issues
```

Die einzige Voraussetzung ist **python3** auf dem PATH (Skills, Hooks und Installer sind
plattformübergreifendes Python). Für GitHub-Quellen `git` + ein authentifiziertes `gh`. Siehe
[`INSTALL.md`](../INSTALL.md) und [`adapters/MATRIX.md`](../adapters/MATRIX.md).

**Before an unattended 24/7 run:** verify persistent source auth, keep the irreversible-operation human gate + secret-scan enabled, and ensure a reachable STOP/cancel path.

---

## 🔒 Sicherheit (nicht verhandelbar)

- **Secret-Scan** für jeden Diff; bei Treffer blockieren.
- **Human-Gate für irreversible Operationen** — Force-Push, History-Rewrite, Prod-Deploy, Daten-/Schema-
  Löschung, Massen-Dateilöschung → stoppen und nachfragen. Headless + kein Freigeber → die destruktive
  Fähigkeit entfernen.
- **Erzwungen, nicht nur versprochen** — `hooks/action_gate.py` ist ein **fail-closed** `PreToolUse`- /
  git-pre-push-Hook, der die obigen (und secret-behaftete Commits) mechanisch blockiert, *bevor* sie laufen.
  Der Sicherheitsvertrag hält, selbst wenn das Modell ihn vergisst. `selftest` beweist das Regelwerk (14/14).
- **4-Zustands-Vorausführungs-Urteil** — Optimierung darf niemals die Risikostufe eines Befehls anheben.
- **Trust-before-load** — wahrnehmungsformende Konfiguration (Clamp-Profile, Suppression-Listen) ist
  nicht vertrauenswürdig, bis ein Mensch sie prüft und per Hash anpinnt.
- **Härtung gegen Prompt-Injection** — Element-/PR-/Kommentar-Inhalte können den Vertrag niemals
  überschreiben.
- **Harter $-Kill-Switch** für unbeaufsichtigte Läufe; **nachweis-gegateter** Abschluss (niemals ein
  falsches „done"); **fail-open** Hooks (den Agenten niemals in einer Schleife einsperren).

---

## ✅ Tests & lokale Prüfungen (kein bezahltes CI)

Behauptungen werden verifiziert, nicht nur behauptet — und das Gate läuft **lokal**, mit null CI-Kosten:

```bash
python3 scripts/check.py            # the whole gate (audit + tests)
```

- **Test-Suite** (`tests/`) — die deterministischen `selftest`s der Worker, plus ein **E2E des
  Schleifentreibers** (`hooks/loop_stop.py`): er beweist, dass die Schleife **bei Belegen stoppt**, **einen
  bloßen `<promise>` ignoriert** und **bei der Obergrenze stoppt** als unterschiedliche Ausgänge — und dass
  die Beleg-Produzenten **BLOCKIEREN** (niemals Fake-Pass), wenn ihre Toolchain fehlt. Läuft unter `pytest`
  *oder*, ganz ohne pip, selbstständig auf bloßem python3 (`python3 tests/test_*.py`).
- **Claims-Audit** (`scripts/claims_audit.py`, fail-closed) — jedes von den Docs referenzierte `scripts/*.py`
  existiert · die Zählung der Erweiterungspunkte stimmt über alle Dateien überein · jeder zitierte
  Worker-Befehl läuft tatsächlich · die ausgelieferten `simplicio_loop/_bundle/`-Skills sind **byte-identisch**
  zur Quelle.
- **Verdrahte es als git-pre-push-Hook**, um `main` kostenlos ehrlich zu halten:
  ```bash
  printf '#!/bin/sh\npython3 scripts/check.py\n' > .git/hooks/pre-push && chmod +x .git/hooks/pre-push
  ```

`pip install "simplicio-loop[dev]"` fügt pytest für schönere Ausgabe hinzu; es ist niemals erforderlich.

---

## 📄 Lizenz

MIT

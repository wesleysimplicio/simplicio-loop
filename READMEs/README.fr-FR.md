# 🔁 simplicio-tasks — L'orchestrateur d'IA en boucle universel

<p align="center">
  <img src="../assets/simplicio-loop-hero.jpg" alt="simplicio-loop" width="920" />
</p>

<p align="center">
  <a href="https://github.com/wesleysimplicio/simplicio-loop/stargazers"><img src="https://img.shields.io/github/stars/wesleysimplicio/simplicio-loop?style=social" alt="Stars"></a>
  <a href="#-les-10-skills--accélérateurs"><img src="https://img.shields.io/badge/skills-10-7C3AED" alt="10 skills"></a>
  <a href="#-adaptateurs-de-source"><img src="https://img.shields.io/badge/source%20adapters-5-00E08A" alt="5 source adapters"></a>
  <a href="#-11-runtimes-un-protocole"><img src="https://img.shields.io/badge/runtimes-11-2563EB" alt="11 runtimes"></a>
  <a href="#-les-43-points-dextension"><img src="https://img.shields.io/badge/extension%20points-43-00E08A" alt="43 extension points"></a>
  <a href="#-économie-de-tokens"><img src="https://img.shields.io/badge/tokens-up%20to%2096%25%20fewer-green" alt="Up to 96% fewer tokens"></a>
  <a href="../LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue" alt="License"></a>
</p>

<p align="center">
  <a href="#-tldr">TL;DR</a> ·
  <a href="#-les-10-skills--accélérateurs">10 Skills</a> ·
  <a href="#-adaptateurs-de-source">Adaptateurs de source</a> ·
  <a href="#-11-runtimes-un-protocole">11 Runtimes</a> ·
  <a href="#-la-boucle">La boucle</a> ·
  <a href="#-économie-de-tokens">Économie de tokens</a> ·
  <a href="#-économie-de-tokens">Moteur de capture</a> ·
  <a href="#-installation--utilisation">Installation</a>
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

**simplicio-tasks** est un **super-plugin** indépendant du runtime — un unique orchestrateur autonome
fonctionnant en boucle (invoqué via **`/simplicio-tasks`**) plus **cinq skills satellites** — qui
transforme n'importe quel LLM performant (Claude, Codex, Copilot, Gemini, Cursor, modèles locaux) en un
worker autonome. Vous le pointez vers un corps de travail — *« termine toutes les issues ouvertes »*,
*« vide la file d'attente CI »*, *« épuise le tableau Jira »* — et il exécute l'ensemble du cycle de vie
tout seul :

> **découvrir → comprendre → décider → agir → vérifier → corriger → enregistrer → répéter**

Il découvre le travail à partir de n'importe quelle source (GitHub Issues, Jira, Azure DevOps, sessions
agentsview, et plus encore), déduplique, met à l'échelle automatiquement une flotte d'agents adaptée à
votre machine, implémente chaque élément via une boucle de qualité qui **exécute le code (et ne se
contente pas de le compiler)**, ouvre des PR, résout les retours CI/revue, fusionne, et reste à l'affût
**24h/24, 7j/7** de nouveau travail — le tout derrière des garde-fous de sécurité et un coupe-circuit de
coût strict.

```text
/simplicio-tasks termine as issues abertas
→ identity + pre-flight (kill-switch, auth, watcher)
→ discover 50 issues · dedup · build dependency DAG
→ autoscale fleet = 14 · pipeline implement→review→merge
→ each item: read body+ACs → orient code → plan → edit → run → verify → PR
→ merge · close with evidence · rollback if main breaks
→ keep looping every ~2 min until the queue is dry (evidence-gated, never a false "done")
```

Trois choses le rendent différent : c'est un **super-plugin de skills ciblées**, il exécute le **même
protocole sur 11 runtimes**, et il fait tout cela avec une **économie de tokens agressive et honnête**.

---

## 🧠 Les 10 skills & accélérateurs

Le cœur de l'orchestrateur + cinq satellites + quatre accélérateurs. Chaque satellite est **optionnel** —
lorsqu'il est chargé, l'orchestrateur lui délègue (plus riche + moins coûteux) ; lorsqu'il est absent, le
protocole inline couvre 100 %. Les accélérateurs sont **auto-détectés** — présent = utilisé, absent =
repli LLM.

| # | Capacité | Absorbe | Ce qu'elle fait | Impact en tokens |
|---|---|---|---|---|
| 1 | 🔁 **simplicio-tasks** | — | La boucle de l'orchestrateur : 43 points d'extension, routeur à deux voies, convergence par auto-audit | Cœur |
| 2 | ♾️ **simplicio-loop** | [ralph-loop](https://github.com/cursor/plugins/tree/main/ralph-loop) | Boucle Ralph durcie : sortie `<promise>` adossée à une preuve, plafond max_iterations | Moteur de boucle |
| 3 | 🧱 **simplicio-orient** | [rtk](https://github.com/rtk-ai/rtk) + [caveman](https://github.com/JuliusBrussee/caveman) | Exécution terminal-first, catalogue de réduction de sortie, tee-cache, lectures signatures | L0 déterministe |
| 4 | 🔥 **simplicio-review** | [thermos](https://github.com/cursor/plugins/tree/main/thermos) | Revue adverse parallèle sur des grilles distinctes → verdict dédupliqué | Gate de qualité |
| 5 | 🗜️ **simplicio-compress** | [caveman](https://github.com/JuliusBrussee/caveman) | Compression de sortie + mémoire, `transform_guard` fail-closed | 40-60 % de moins |
| 6 | 🎓 **simplicio-learn** | [teaching](https://github.com/cursor/plugins/tree/main/teaching) | Rétrospective post-run → leçons durables et dédupliquées en mémoire | Plus malin à chaque run |
| 7 | 🧭 **Understand Anything** | [Egonex-AI](https://github.com/Egonex-AI/Understand-Anything) | Orientation par graphe de connaissances : recherche sémantique, visites guidées, graphe de dépendances | **L0 zéro token** |
| 8 | 📊 **agentsview** | [kenn-io](https://github.com/kenn-io/agentsview) | Analytique de session, suivi des coûts, découverte de sessions bloquées | **L1** SQL uniquement |
| 9 | ⚡ **LMCache** | [LMCache](https://github.com/LMCache/LMCache) | Cache KV entre tours de boucle — réduction de 40-70 % du TTFT sur les modèles locaux | Temps GPU ↓ |
| 10 | 🗜️ **Moteur de capture Simplicio** | `engine/simplicio_engine.py` (natif, stdlib uniquement ; schéma d'économies compatible avec le projet OSS [headroom](https://github.com/headroomlabs-ai/headroom)) | Proxy de capture transparent : transmet au vrai fournisseur, mesure + compresse de façon déterministe, écrit `proxy_savings.json` | **déterministe** |

Chaque skill vit sous [`.claude/skills/`](../.claude/skills) ; chaque accélérateur dispose d'un document de
référence sous `.claude/skills/simplicio-tasks/references/`.

---

## 📡 Adaptateurs de source

L'orchestrateur découvre le travail à partir de n'importe quelle source via des adaptateurs enfichables.
Chacun expose six verbes : `list_ready`, `get_details`, `claim`, `update_status`, `attach_evidence`,
`close`.

| Source | Adaptateur | Objet |
|---|---|---|
| GitHub Issues/PRs | `gh` CLI (natif) | Source primaire d'éléments de travail |
| Jira / Asana / ClickUp / Linear / Notion | connecteur de l'hôte | Gestion de tableaux/projets |
| Trello / Azure DevOps | adaptateur `az boards` | Suivi de travail Azure |
| **sessions agentsview** | `scripts/agentsview_adapter.py` | Récupération de session bloquée + observabilité des coûts |
| Fichiers locaux / file CI | système de fichiers / API CI | Suivi de travail interne |

Voir le document de référence de chaque adaptateur sous `.claude/skills/simplicio-tasks/references/`.

|---

## 🌐 11 runtimes, un protocole

Un unique cœur de skill universel + un unique jeu de hooks pilote chaque runtime. Un adaptateur est
mince : il indique à un runtime *où charger les skills*, *comment armer la boucle* et *comment lier la
vitesse native*. **La skill ne nomme aucun runtime ; c'est le runtime qui détecte la skill.**

| Runtime | Chargement de la skill | Pilotage de la boucle | Liaison native |
|---|---|---|---|
| **Claude Code** | `.claude/skills/` + plugin | Hook `Stop` | MCP |
| **Codex** | `AGENTS.md` | self-paced | MCP / adaptateur |
| **VS Code (Copilot)** | `copilot-instructions.md` | tasks | MCP |
| **Cursor** | `.cursor-plugin/` | `stop`+`afterAgentResponse` | MCP / rules |
| **Antigravity** | rules / `AGENTS.md` | self-paced | MCP |
| **Kiro** | `.kiro/steering/` | specs | MCP |
| **OpenCode** | `AGENTS.md` | self-paced | MCP |
| **Gemini** | `GEMINI.md` | self-paced | MCP / adaptateur |
| **Aider** | `CONVENTIONS.md` | self-paced | — (repli LLM) |
| **Hermes** | recall natif | boucle native | **natif** |
| **OpenClaw** | plugin SDK | scheduler natif | **natif** |

La promesse : **même protocole, mêmes garde-fous, même sécurité sur les 11 — seule la vitesse
diffère.** `orient_clamp.py` (économie de tokens) fonctionne sur tous les runtimes sans aucun câblage.
Voir [`adapters/MATRIX.md`](../adapters/MATRIX.md).

---

## 🗺️ Le flux complet — de la demande à la livraison

Chaque couche sur laquelle agit l'orchestrateur, dans l'ordre — depuis la lecture de la demande (issues,
tâches, affectations) jusqu'à la livraison d'un travail fusionné et prouvé, puis la boucle 24h/24 pour en
chercher davantage.

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

## 🔁 La boucle

La **boucle adossée à la preuve** (Evidence-Gated Loop) est le mécanisme central. Elle réinjecte le même
objectif à chaque tour afin que l'agent voie son propre travail antérieur. La sortie se fait UNIQUEMENT
via :

1. **`<promise>` adossée à une preuve** — le tour qui émet la promesse DOIT aussi porter une preuve
   concrète (test qui passe, PR fusionnée, re-requête d'élément clôturé). Une promesse sans preuve =
   ignorée.
2. **Plafond `max_iterations`** — garde-fou de sécurité strict
3. **Coupe-circuit de budget** — `daily_usd_ceiling` arrête la boucle une fois le budget dépensé
4. **Signal STOP** — `.orchestrator/STOP` ou commande de canal

Entre les tours, LMCache (lorsqu'il est disponible) met en cache l'état KV afin que la réinjection coûte
un prefill quasi nul.

---

## 📊 Économie de tokens

| Technique | Économie |
|---|---|
| `deterministic_edit` (L0) | 100 % des tokens d'édition (fichier écrit mécaniquement, jamais par le LLM) |
| Exécution terminal-first | Les faits viennent du shell, pas d'une hallucination du LLM |
| Catalogue de réduction de sortie | Plafonds par type de commande (`CAP_ERRORS=20`, `CAP_WARNINGS=10`, `CAP_LIST=20`) — `orient_clamp.py` |
| Cache Tee+CCR en cas d'échec | Ne jamais réexécuter une commande échouée — lire la sortie en cache |
| Lectures signatures seules | `simplicio signatures <file>` — fichier de 870 lignes → 65 lignes (**93 % économisés**), corps élidés |
| `simplicio-compress` | Prose concise + compaction unique de la mémoire |
| `orient_clamp.py` | Bride + tee sur chaque commande shell, sans câblage |
| Cache natif de réponses | requête déterministe (temp=0) répétée → servie depuis le cache, saute l'appel LLM (**100 % en cas de hit**) — `simplicio cache`, activé par défaut (`SIMPLICIO_CACHE=0` pour désactiver) |
| Proxy de capture Simplicio + MCP | 60-95 % de tokens en moins sur les sorties d'outils via un démon de compression transparent |

Les économies ne comptent que sur un résultat vérifié-correct. La baseline = le chemin non orchestré le
plus économique et raisonnable vers le même résultat. Voir `references/token-economy.md`.

### 📈 Simplicio Token Monitor

Une vue en direct, toujours active, des économies :

- **Tableau de bord web** — `http://127.0.0.1:9090` — graphe de tokens en temps réel, jauge d'économies, les
  LLMs/runtimes et **141/144 fournisseurs (98 %)** que nous interceptons, et un journal de proxy en direct.
- **Widget barre de menus / zone de notification** — tokens économisés en direct dans la barre système (macOS rumps · Windows/Linux pystray).
- **Un seul module** — `scripts/simplicio-economy.sh {status|up|wire}` démarre le proxy de capture + le moniteur +
  le widget + l'opérateur déterministe `simplicio-dev-cli` et rend compte de l'ensemble de la stack.

L'installation enregistre les trois comme services à démarrage automatique (macOS launchd · Linux systemd · Windows Startup) via
`scripts/setup_simplicio.sh`, ou via le multiplateforme `python3 scripts/install_services.py install`. Après
l'installation, le moniteur + la capture tournent **sans invoquer la boucle** — voir `references/token-capture.md`.

### 🛠️ Le moteur de capture — un module natif, chaque commande

[`engine/simplicio_engine.py`](engine/simplicio_engine.py) est le moteur de capture natif Simplicio
(stdlib uniquement, fail-open) — une **réimplémentation complète de la surface amont
[headroom](https://github.com/headroomlabs-ai/headroom) sans aucune dépendance externe**. Exécutez n'importe quelle
commande via le wrapper [`scripts/simplicio-engine`](scripts/simplicio-engine) (par ex. `simplicio-engine doctor`) :

| Commande | Ce qu'elle fait |
|---|---|
| `proxy` | le proxy de capture transparent — route chaque modèle vers son **vrai** fournisseur, compresse + mesure + met en cache (pas de substitution de modèle) |
| `doctor` | accessibilité du proxy + économies cumulées |
| `cache` | cache natif de réponses (`stats`/`clear`) — une requête déterministe répétée est servie depuis le cache, sautant l'appel LLM |
| `signatures` | vue signatures seules d'un fichier source (corps élidés, ~93 % de tokens en moins pour lire le code) |
| `semantic` | compression extractive réversible (semantic-lite) |
| `kompress` | élagage sémantique de tokens **ONNX** via le vrai modèle `kompress-v2-base` |
| `detect` | détection du type de contenu + routage intelligent par bloc |
| `rag` | récupération TF-IDF (ou embedding `--ml`) sur le store mémoire CCR |
| `memory` | store CCR compress-cache-retrieve (`remember`/`recall`/`forget`/`list`/`stats`) |
| `mcp` | serveur MCP stdio natif (outils compress / retrieve / stats) |
| `init` / `wrap` | enregistrer Simplicio dans un client (Claude / Codex / Copilot / OpenClaw) · exécuter un client avec routage de capture |
| `report` / `audit` / `capture` / `evals` | rapport d'économies · auditer un arbre pour les occasions de compression · simuler une requête (dry-run) · gate de régression de compression |

### 🧠 Modèles ML réels optionnels — `pip install "simplicio-loop[onnx]"`

Quatre modèles ONNX **réels**, publics (Apache-2.0) s'exécutent nativement — les mêmes modèles que ceux
de l'amont. Sans l'extra, le chemin déterministe en stdlib couvre tout ; les modèles se téléchargent à la
première utilisation.

| Modèle | Commande | Usage |
|---|---|---|
| `kompress-v2-base` | `simplicio kompress` | élagage sémantique de tokens |
| `technique-router-onnx` | `simplicio router` | routage des techniques |
| `all-MiniLM-L6-v2-onnx` | `simplicio embed` · `rag --ml` | embeddings + RAG sémantique |
| `siglip-image-encoder-onnx` | `simplicio image` | vérificateur de contenu pour la compression d'image |

### ⚙️ Cœur de performance natif en Rust (optionnel)

[`rust/`](rust) embarque quatre crates portés + rebrandés de l'amont (Apache-2.0 ; `NOTICE` le crédite) :
`simplicio-core` (compresseurs + smart-crusher), `simplicio-py` (bindings PyO3), `simplicio-proxy`
(reverse proxy axum), `simplicio-parity` (harnais de parité Rust↔Python). Construire avec `maturin` — le moteur
Python fonctionne pleinement sans eux ; les crates n'ajoutent que la vitesse native.

|---

## 🏛️ Piliers de conception (en détail)

Quatre mécanismes portent la puissance d'orchestration :

| Pilier | Objet | Vit dans |
|---|---|---|
| **DAG + pipeline** | parallélisme par dépendance, étagé par élément | `references/orchestration.md` (Étape 3 pool + pipeline) |
| **Isolation par worktree** | éditions parallèles sans corrompre l'arbre, fusion contrôlée par gate | `references/orchestration.md` |
| **Vérification adverse** | un panel de sceptiques avant « livré » | `references/quality-safety-delivery.md` · skill `simplicio-review` |
| **Plafond de budget de la boucle** | anti-boucle-infinie, double sortie | `references/standing-loop-247.md` · skill `simplicio-loop` |

---

## 🚀 Installation et utilisation

```bash
git clone https://github.com/wesleysimplicio/simplicio-loop
cd simplicio-loop

# install for your runtime (omit <runtime> to auto-detect)
bash scripts/install.sh <runtime> [--global]        # macOS / Linux
pwsh scripts/install.ps1 <runtime> [-Global]        # Windows
# <runtime> ∈ claude codex vscode cursor antigravity kiro opencode gemini aider hermes openclaw
```

Ou, sur Claude Code / Cursor, ajoutez-le comme plugin de marketplace :

```
/plugin marketplace add wesleysimplicio/simplicio-loop
/plugin install simplicio-loop@simplicio
```

Puis :

```
/simplicio-tasks finish all the open issues
```

La seule exigence est **python3** dans le PATH (skills, hooks et installeur sont du Python
multiplateforme). Pour les sources GitHub, `git` + un `gh` authentifié. Voir [`INSTALL.md`](../INSTALL.md)
et [`adapters/MATRIX.md`](../adapters/MATRIX.md).

**Avant un run 24h/24 sans surveillance :** fixez un plafond de coût dans
`.orchestrator/loop-budget.json` (`daily_usd_ceiling > 0`), confirmez que l'authentification source est
persistante, et gardez activés le gate humain pour op irréversible + le scan de secrets. Avec
`ceiling = 0`, le watcher refuse de tourner sans surveillance (fail-safe).

---

## 🔒 Sécurité (non négociable)

- **Scan de secrets** sur chaque diff ; blocage en cas de détection.
- **Gate humain pour op irréversible** — force-push, réécriture d'historique, déploiement en prod,
  suppression de données/schéma, suppression massive de fichiers → s'arrêter et demander. Headless +
  aucun approbateur → retirer la capacité destructrice.
- **Verdict à 4 états pré-exécution** — l'optimisation ne peut jamais élever le palier de risque d'une
  commande.
- **Trust-before-load** — la config qui façonne la perception (profils de clamp, listes de suppression)
  n'est pas de confiance tant qu'un humain ne l'a pas relue et figée par hash.
- **Durcissement contre l'injection de prompt** — le contenu d'un élément/d'une PR/d'un commentaire ne
  peut jamais supplanter le contrat.
- **Kill-switch $ strict** pour les runs sans surveillance ; complétion **adossée à une preuve** (jamais
  un faux « done ») ; hooks **fail-open** (ne jamais piéger l'agent dans une boucle).

---

## 📄 Licence

MIT

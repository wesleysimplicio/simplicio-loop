# 🔁 simplicio-tasks — El orquestador de IA universal en bucle

<p align="center">
  <img src="../assets/simplicio-loop-hero.jpg" alt="simplicio-loop" width="920" />
</p>

<p align="center">
  <a href="https://github.com/wesleysimplicio/simplicio-loop/stargazers"><img src="https://img.shields.io/github/stars/wesleysimplicio/simplicio-loop?style=social" alt="Stars"></a>
  <a href="#-las-11-skills--aceleradores"><img src="https://img.shields.io/badge/skills-11-7C3AED" alt="11 skills"></a>
  <a href="#-adaptadores-de-fuente"><img src="https://img.shields.io/badge/source%20adapters-5-00E08A" alt="5 source adapters"></a>
  <a href="#-11-runtimes-un-protocolo"><img src="https://img.shields.io/badge/runtimes-11-2563EB" alt="11 runtimes"></a>
  <a href="#-los-44-puntos-de-extensión"><img src="https://img.shields.io/badge/extension%20points-44-00E08A" alt="44 extension points"></a>
  <a href="#-economía-de-tokens"><img src="https://img.shields.io/badge/tokens-up%20to%2096%25%20fewer-green" alt="Up to 96% fewer tokens"></a>
  <a href="../LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue" alt="License"></a>
</p>

<p align="center">
  <a href="#-tldr">TL;DR</a> ·
  <a href="#-las-11-skills--aceleradores">11 Skills</a> ·
  <a href="#-adaptadores-de-fuente">Adaptadores de fuente</a> ·
  <a href="#-11-runtimes-un-protocolo">11 Runtimes</a> ·
  <a href="#-el-bucle">El bucle</a> ·
  <a href="#-economía-de-tokens">Economía de tokens</a> ·
  <a href="#-economía-de-tokens">Motor de captura</a> ·
  <a href="#-instalación--uso">Instalación</a>
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

**simplicio-tasks** es un **super-plugin** independiente del runtime — un único orquestador
autónomo en bucle (invocado como **`/simplicio-tasks`**) más **cinco skills satélite** — que
convierte cualquier LLM potente (Claude, Codex, Copilot, Gemini, Cursor, modelos locales) en un
worker que se conduce solo. Lo apuntas a un cuerpo de trabajo — *«termina todas las issues
abiertas»*, *«vacía la cola de CI»*, *«drena el tablero de Jira»* — y ejecuta todo el ciclo de vida
por sí solo:

> **descubrir → entender → decidir → actuar → verificar → corregir → registrar → repetir**

Descubre trabajo desde cualquier fuente (GitHub Issues, Jira, Azure DevOps, sesiones de agentsview y
más), elimina duplicados, autoescala una flota de agentes según tu máquina, implementa cada elemento
a través de un bucle de calidad que **ejecuta el código (no solo lo compila)**, abre PRs, resuelve el
feedback de CI/revisión, hace merge y sigue vigilando **24/7** en busca de trabajo nuevo — todo ello
tras barreras de seguridad y un interruptor de corte de coste estricto.

```text
/simplicio-tasks finish all open issues
→ identity + pre-flight (kill-switch, auth, watcher)
→ discover 50 issues · dedup · build dependency DAG
→ autoscale fleet = 14 · pipeline implement→review→merge
→ each item: read body+ACs → orient code → plan → edit → run → verify → PR
→ merge · close with evidence · rollback if main breaks
→ keep looping every ~2 min until the queue is dry (evidence-gated, never a false "done")
```

Tres cosas lo hacen diferente: es un **super-plugin de skills enfocadas**, ejecuta el **mismo
protocolo en 11 runtimes** y hace todo esto con una **economía de tokens agresiva y honesta**.

---

## 📘 Registro oficial de capacidades

El listado completo y oficial de lo que incluye `simplicio-tasks` — cada capacidad de abajo es
**real, ejecutable y testeada** (`python3 scripts/check.py`: claims-audit 4/4 + 28 tests). Cada una
enlaza con su sección detallada y su worker.

| Capacidad | Qué hace | Prueba / worker | Detalles |
|---|---|---|---|
| 🎬 **Evidencia en vídeo** (`video_evidence`) | Graba la **sesión real del navegador** como prueba en movimiento de que un cambio de UI funciona (Playwright, por defecto); renderiza un **MP4 determinista con subtítulos** con [hyperframes](https://github.com/heygen-com/hyperframes) para una petición explícita de vídeo explicativo (`/simplicio-tasks make a video of screen X`) | `scripts/video_evidence.py` · BLOQUEADO (nunca fake-pass) sin el toolchain | [§ Evidencia en vídeo](#-evidencia-en-vídeo--playwright-por-defecto-hyperframes-bajo-demanda) |
| 🧠 **Memoria de intentos + detector de estancamiento** | Un run-journal duradero (`.orchestrator/loop/journal.jsonl`) + un detector de estancamiento para que el bucle **cambie de estrategia en lugar de oscilar**; el triaje incremental (`since`) lee solo el delta de cada turno | `scripts/loop_journal.py` · `selftest` 9/9 | [§ Anti-oscilación](#-memoria-de-intentos--detector-de-estancamiento-anti-oscilación) |
| 🔒 **Gate de seguridad fail-closed** (`action_gate`) | Un hook `PreToolUse`/git-pre-push que **bloquea mecánicamente** force-push, reescritura de historial, borrado masivo, DDL destructivo, teardown de infra y commits/pushes con secretos — el Paso 5 hecho ejecutable, no prosa | `hooks/action_gate.py` · `selftest` 15/15 | [§ Seguridad](#-seguridad-innegociable) |
| 🔬 **Verificación local** | Una suite de tests (selftests de workers + un **e2e del driver del bucle** que prueba la salida ligada a evidencia) + una **claims-audit** (los scripts referenciados existen · counts consistentes · `_bundle ≡ source`) — todo local, **sin CI de pago** | `scripts/check.py` · `scripts/claims_audit.py` · `tests/` | [§ Tests y comprobaciones locales](#-tests-y-comprobaciones-locales-sin-ci-de-pago) |
| ✅ **Ahorro honesto** | La línea de ahorro ahora es **ligada a evidencia, no obligatoria** — solo se muestra un número con un recibo medido (clamp/firmas/caché/`deterministic_edit`/ledger); nunca se fabrica | contrato de economía de tokens | [§ Economía de tokens](#-economía-de-tokens) |

Dos **modos** del bucle hacen explícita la terminación: **converge** (una sola tarea dura — termina
con el `<promise>` ligado a evidencia o una escalada por estancamiento) vs **drain** (una cola —
termina cuando la reconsulta de la fuente sigue vacía K rondas). Ambos siguen obedeciendo las
salidas universales (promise+evidencia, `max_iterations`, presupuesto, STOP).

> Puntuación del bucle a lo largo de esta línea de trabajo: **7.5** (diseño sólido, no probado) →
> **9** (memoria de intentos + anti-oscilación) → **9.5** (prueba local reproducible) → **~10**
> (seguridad forzada + semántica de bucle completa). La infraestructura de verificación ya atrapa
> las propias regresiones del proyecto a medida que crece.

---

## 🧠 Las 11 skills y aceleradores

El núcleo del orquestador + cinco satélites + cinco aceleradores/integraciones. Cada satélite es
**opcional** — cuando se carga, el orquestador le delega (más rico + más barato); cuando está
ausente, el protocolo inline cubre el 100%. Los aceleradores se **autodetectan** — presente = usado,
ausente = fallback por LLM.

| # | Capacidad | Absorbe | Qué hace | Impacto en tokens |
|---|---|---|---|---|
| 1 | 🔁 **simplicio-tasks** | — | El bucle del orquestador: 44 puntos de extensión, enrutador de doble vía, convergencia por autoauditoría | Núcleo |
| 2 | ♾️ **simplicio-loop** | [ralph-loop](https://github.com/cursor/plugins/tree/main/ralph-loop) | Bucle Ralph endurecido: salida con `<promise>` ligada a evidencia, tope de max_iterations | Drive del bucle |
| 3 | 🧱 **simplicio-orient** | [rtk](https://github.com/rtk-ai/rtk) + [caveman](https://github.com/JuliusBrussee/caveman) | Ejecución terminal-first, catálogo de reducción de salida, tee-cache, lecturas solo-firmas | L0 determinista |
| 4 | 🔥 **simplicio-review** | [thermos](https://github.com/cursor/plugins/tree/main/thermos) | Revisión adversarial paralela sobre rúbricas distintas → veredicto deduplicado | Gate de calidad |
| 5 | 🗜️ **simplicio-compress** | [caveman](https://github.com/JuliusBrussee/caveman) | Compresión de salida + memoria, `transform_guard` fail-closed | 40-60% menos |
| 6 | 🎓 **simplicio-learn** | [teaching](https://github.com/cursor/plugins/tree/main/teaching) | Retrospectiva post-ejecución → lecciones duraderas y deduplicadas en memoria | Más listo en cada ejecución |
| 7 | 🧭 **Understand Anything** | [Egonex-AI](https://github.com/Egonex-AI/Understand-Anything) | Orientación por grafo de conocimiento: búsqueda semántica, tours guiados, grafo de dependencias | **L0 cero tokens** |
| 8 | 📊 **agentsview** | [kenn-io](https://github.com/kenn-io/agentsview) | Analítica de sesiones, seguimiento de coste, descubrimiento de sesiones estancadas | **L1** solo SQL |
| 9 | ⚡ **LMCache** | [LMCache](https://github.com/LMCache/LMCache) | Caché KV entre turnos del bucle — 40-70% menos de TTFT en modelos locales | Tiempo de GPU ↓ |
| 10 | 🗜️ **Motor de captura Simplicio** | `engine/simplicio_engine.py` (nativo, solo stdlib) | Proxy de captura transparente: reenvía al proveedor real, mide + comprime de forma determinista, escribe `proxy_savings.json` | **determinista** |
| 11 | 🎬 **video_evidence** | Playwright (por defecto) · [hyperframes](https://github.com/heygen-com/hyperframes) (bajo demanda) | Graba la **sesión real** como prueba en movimiento de un cambio de UI (Playwright); renderiza un **MP4 explicativo determinista con subtítulos** con hyperframes cuando el vídeo ES el entregable | Productor de evidencia |

Cada skill vive bajo [`.claude/skills/`](../.claude/skills); cada acelerador tiene un documento de
referencia bajo `.claude/skills/simplicio-tasks/references/` (el productor de vídeo:
[`video-evidence.md`](../.claude/skills/simplicio-tasks/references/video-evidence.md), worker
[`scripts/video_evidence.py`](../scripts/video_evidence.py)).

---

## 📡 Adaptadores de fuente

El orquestador descubre trabajo desde cualquier fuente mediante adaptadores conectables. Cada uno
expone seis verbos: `list_ready`, `get_details`, `claim`, `update_status`, `attach_evidence`,
`close`.

| Fuente | Adaptador | Propósito |
|---|---|---|
| GitHub Issues/PRs | `gh` CLI (nativo) | Fuente primaria de elementos de trabajo |
| Jira / Asana / ClickUp / Linear / Notion | conector del host | Gestión de tableros/proyectos |
| Trello / Azure DevOps | adaptador `az boards` | Seguimiento de trabajo en Azure |
| **sesiones de agentsview** | `scripts/agentsview_adapter.py` | Recuperación de sesiones estancadas + observabilidad de coste |
| Archivos locales / cola de CI | sistema de archivos / API de CI | Seguimiento de trabajo interno |

Consulta el documento de referencia de cada adaptador bajo
`.claude/skills/simplicio-tasks/references/`.

---

## 🌐 11 runtimes, un protocolo

Un único núcleo de skill universal + un único conjunto de hooks conduce cada runtime. Un adaptador es
fino: le dice a un runtime *dónde cargar las skills*, *cómo armar el bucle* y *cómo enlazar la
velocidad nativa*. **La skill no nombra ningún runtime; el runtime detecta la skill.**

| Runtime | Carga de la skill | Drive del bucle | Enlace nativo |
|---|---|---|---|
| **Claude Code** | `.claude/skills/` + plugin | Hook `Stop` | MCP |
| **Codex** | `AGENTS.md` | self-paced | MCP / adaptador |
| **VS Code (Copilot)** | `copilot-instructions.md` | tasks | MCP |
| **Cursor** | `.cursor-plugin/` | `stop`+`afterAgentResponse` | MCP / rules |
| **Antigravity** | rules / `AGENTS.md` | self-paced | MCP |
| **Kiro** | `.kiro/steering/` | specs | MCP |
| **OpenCode** | `AGENTS.md` | self-paced | MCP |
| **Gemini** | `GEMINI.md` | self-paced | MCP / adaptador |
| **Aider** | `CONVENTIONS.md` | self-paced | — (fallback por LLM) |
| **Hermes** | recall nativo | bucle nativo | **nativo** |
| **OpenClaw** | plugin SDK | scheduler nativo | **nativo** |

La promesa: **mismo protocolo, mismas barreras, misma seguridad en los 11 — solo cambia la
velocidad.** `orient_clamp.py` (economía de tokens) funciona en todos los runtimes sin ningún
cableado. Consulta [`adapters/MATRIX.md`](../adapters/MATRIX.md).

---

## 🗺️ El flujo completo — de la demanda a la entrega

Cada capa sobre la que actúa el orquestador, en orden — desde leer la demanda (issues, tareas,
asignaciones) hasta entregar trabajo mergeado y con evidencia, y luego el bucle 24/7 en busca de más.

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
    Q2["WORKS not just compiles · web_verify (Playwright) · video_evidence (Playwright recording · hyperframes on request)"]
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
  FB -->|"merged and closed"| DONE(["done + evidence + measured savings (only if a receipt exists)"])
  WATCH["11 · 24/7 watcher · simplicio-loop evidence-gated promise · max-iterations cap · cost kill-switch · LMCache KV cache warm"]
  FB -. "poll new work / comments / checks" .-> WATCH
  DONE -. "idle until new work" .-> WATCH
  WATCH -. "re-feed the goal" .-> DISC
```

---

## 🔁 El bucle

El **bucle ligado a evidencia** es el mecanismo central. Realimenta el mismo objetivo en cada turno
para que el agente vea su propio trabajo previo. La salida es ÚNICAMENTE vía:

1. **`<promise>` ligada a evidencia** — el turno que emite la promesa DEBE además aportar prueba
   concreta (un test que pasa, un PR mergeado, una reconsulta del elemento cerrado). Una promesa sin
   evidencia = ignorada.
2. **Tope de `max_iterations`** — barrera estricta de seguridad
3. **Kill-switch de presupuesto** — `daily_usd_ceiling` detiene el bucle cuando se gasta
4. **Señal STOP** — `.orchestrator/STOP` o un comando de canal

Entre turnos, LMCache (cuando está disponible) cachea el estado KV para que la realimentación cueste
un prefill casi nulo.

### 🧠 Memoria de intentos + detector de estancamiento (anti-oscilación)

Un bucle de realimentación que no recuerda nada oscila — prueba X, falla, prueba X de nuevo — hasta
que el tope se consume. simplicio-loop mantiene un **run-journal duradero**
(`.orchestrator/loop/journal.jsonl`, solo-append:
`iteration · action · hypothesis · gate · error-fingerprint`) y un **detector de estancamiento**
([`scripts/loop_journal.py`](../scripts/loop_journal.py), determinista + sin modelo):

- **Error fingerprint** — la salida del gate fallido se reduce a un hash estable con los números de
  línea, rutas, hex/uuids, timestamps y duraciones normalizados, de modo que el *mismo* bug se
  reconoce a lo largo de los turnos aunque el texto incidental difiera.
- **Estancamiento = K fallos consecutivos con la misma fingerprint** (por defecto K=3). Una
  fingerprint que cambia significa que el bucle avanza (PROGRESS); la misma K veces significa que
  está dando vueltas (STALLED).
- En STALLED el bucle **no** realimenta el mismo objetivo — nombra las **acciones sin salida** a
  evitar, y luego **cambia de estrategia** o **escala al gate humano** con la fingerprint.
- `loop_journal.py resume` se lee al inicio de cada turno, de modo que un proceso nuevo continúa sin
  re-derivar intentos previos (resume real) y nunca reintenta un callejón sin salida conocido.

```bash
loop_journal.py resume                       # what was tried + dead-ends to avoid
loop_journal.py record --iteration N --action "…" --gate fail --gate-output test.log
loop_journal.py stall --k 3 --exit-code      # PROGRESS → re-feed · STALLED → switch/escalate
```

---

## 🎬 Evidencia en vídeo — Playwright por defecto, hyperframes bajo demanda

El bucle produce **vídeos demostrativos** como prueba de que un cambio funciona — **dos motores**, un
único punto de extensión `video_evidence` (worker
[`scripts/video_evidence.py`](../scripts/video_evidence.py), contrato
[`references/video-evidence.md`](../.claude/skills/simplicio-tasks/references/video-evidence.md)):

1. **Por defecto — el flujo normal de evidencia usa Playwright.** Tras un cambio de UI,
   `video_evidence` graba la **sesión real del navegador** manejando la pantalla (vídeo nativo de
   Playwright → `.webm`, → `.mp4` con FFmpeg) — el recibo más fuerte de «funciona, no solo compila»
   (Paso 4b) y un `<promise>` válido ligado a evidencia.

   ```bash
   python3 scripts/video_evidence.py verify --url http://localhost:3000/login \
       --name login-demo --expect "Sign in" --issue 42 [--upload --pr 42]
   ```

2. **Bajo demanda — un explicativo personalizado usa hyperframes.** Cuando el entregable ES un vídeo
   («make an explainer video of screen X»), el orquestador renderiza una **presentación determinista
   con subtítulos** de las capturas de `web_verify` con
   [**hyperframes**](https://github.com/heygen-com/hyperframes) (de HeyGen — «misma entrada, mismos
   frames, misma salida», reproducible en CI, sin claves de API, render local vía Chrome headless +
   FFmpeg).

   ```text
   /simplicio-tasks make an explainer video of the system login screen
   → detect: video-creation request → web_verify captures the screens
   → video_evidence verify --engine hyperframes → deterministic MP4 → attached to the PR
   ```

Cualquiera de los dos motores: un vídeo que nunca se grabó/renderizó produce **BLOQUEADO**, nunca un
falso pase. La evidencia es siempre una **ruta de archivo + veredicto booleano** — nunca bytes de
vídeo en contexto (economía de tokens).

---

## 📊 Economía de tokens

| Técnica | Ahorro |
|---|---|
| `deterministic_edit` (L0) | 100% de los tokens de edición (el archivo se escribe mecánicamente, nunca por el LLM) |
| Ejecución terminal-first | Los hechos vienen del shell, no de la alucinación del LLM |
| Catálogo de reducción de salida | Topes por tipo de comando (`CAP_ERRORS=20`, `CAP_WARNINGS=10`, `CAP_LIST=20`) — `orient_clamp.py` |
| Caché Tee+CCR en caso de fallo | Nunca reejecutar un comando fallido — leer la salida cacheada |
| Lecturas solo-firmas | `simplicio-cli signatures <file>` — un archivo de 870 líneas → 65 líneas (**93% ahorrado**), cuerpos eliminados |
| `simplicio-compress` | Prosa concisa + compactación única de memoria |
| `orient_clamp.py` | Clamp + tee en cada comando de shell, sin cableado |
| Caché de respuesta nativa | una petición determinista repetida (temp=0) → servida desde caché, omite la llamada al LLM (**100% en acierto**) — `simplicio-cli cache`, activada por defecto (`SIMPLICIO_CACHE=0` para desactivar) |
| Proxy de captura Simplicio + MCP | 60-95% menos de tokens en las salidas de herramientas vía un daemon de compresión transparente |

El ahorro solo cuenta sobre un resultado verificado-correcto. Línea base = el camino no orquestado
más barato y sensato hacia el mismo resultado. **El reporte de ahorro es ligado a evidencia, no
obligatorio:** solo se muestra una cifra de ahorro cuando un turno realmente ejecutó un comando
productor de economía y el número rastrea hasta un recibo medido (tee de clamp, lectura de firmas,
acierto de caché, `deterministic_edit`, `savings_ledger`). Sin economía medida → sin línea de
ahorro; el orquestador nunca fabrica una línea base ni un porcentaje. Consulta
`references/token-economy.md`.

### 🔎 Ejecutar `simplicio-tasks`: economía vs medición (por runtime)

Cuando llamas a **`simplicio-tasks`** ocurren dos cosas distintas, y se comportan de forma diferente
por runtime:

- **Economía** — compresión, clamps de salida, lecturas solo-firmas, `deterministic_edit` — aplica
  **cada vez que la skill se ejecuta y carga `simplicio-orient` / `simplicio-compress`, en cualquier
  runtime.** Es el comportamiento de la skill más los hooks (más fuerte donde existen hooks:
  `orient_clamp.py` auto-clampa en Claude y Cursor; en otros lugares es dirigido por instrucciones).
- **Medición** — los números en vivo del Token Monitor — solo cuenta el tráfico que fluye **a través
  del proxy de captura.**

| Runtime | Economía (skill) | Medición (monitor) |
|---|---|---|
| **Hermes** | ✓ | ✓ **automática** — ya enrutado a través del proxy (`base_url → :8788`) |
| **Claude** | ✓ (skill + hooks) | ✗ por defecto — Claude habla directamente con `api.anthropic.com`; medido solo una vez enrutado (`simplicio-cli wrap claude`, o `ANTHROPIC_BASE_URL → http://127.0.0.1:8788`) |
| **Codex** | ✓ (skill) | ✗ por defecto — `simplicio-cli init codex` añade las herramientas MCP pero no enruta el tráfico del LLM; medido con `simplicio-cli wrap codex` o una base-url de OpenAI apuntando al proxy |

Así que: el **ahorro ocurre en cada runtime**; el **monitor lo contabiliza automáticamente en
Hermes**, y en Claude/Codex tras un **paso de enrutamiento único** (`simplicio-cli wrap …` / base-url →
`:8788`). Sin enrutamiento, la economía igual aplica — el monitor simplemente no contará esos tokens.
`scripts/simplicio-economy.sh wire` hace este enrutamiento para clientes compatibles con OpenAI en el
momento de la instalación.

### 📈 Simplicio Token Monitor

Una vista en vivo, siempre activa, del ahorro:

- **Dashboard web** — `http://127.0.0.1:9090` — gráfico de tokens en tiempo real, medidor de ahorro,
  los LLMs/runtimes y los **141/144 proveedores (98%)** que interceptamos, y un log de proxy en vivo.
- **Widget de barra de menús / bandeja** — tokens ahorrados en vivo en la bandeja del sistema (macOS rumps · Windows/Linux pystray).
- **Un módulo** — `scripts/simplicio-economy.sh {status|up|wire}` levanta el proxy de captura + monitor +
  bandeja + el operador determinista `simplicio-dev-cli` e informa de toda la pila.

La instalación registra los tres como servicios de auto-arranque (macOS launchd · Linux systemd · Windows Startup) vía
`scripts/setup_simplicio.sh`, o el multiplataforma `python3 scripts/install_services.py install`. Tras la
instalación el monitor + la captura corren **sin invocar el bucle** — consulta `references/token-capture.md`.

### 🛠️ El motor de captura — un módulo nativo, cada comando

[`engine/simplicio_engine.py`](../engine/simplicio_engine.py) es el motor de captura Simplicio nativo
(nativo, solo stdlib, fail-open, sin ninguna dependencia externa). Ejecuta
cualquier comando vía el wrapper [`scripts/simplicio-engine`](../scripts/simplicio-engine) (p. ej. `simplicio-engine doctor`):

| Comando | Qué hace |
|---|---|
| `proxy` | el proxy de captura transparente — enruta cada modelo a su proveedor **real**, comprime + mide + cachea (sin cambiar de modelo) |
| `doctor` | alcanzabilidad del proxy + ahorro acumulado |
| `cache` | caché de respuesta nativa (`stats`/`clear`) — una petición determinista repetida se sirve desde caché, omitiendo la llamada al LLM |
| `signatures` | vista solo-firmas de un archivo fuente (cuerpos eliminados, ~93% menos tokens para leer código) |
| `semantic` | compresión extractiva reversible (semantic-lite) |
| `detect` | detección de tipo de contenido + enrutamiento inteligente por bloque |
| `rag` | recuperación TF-IDF (o embeddings con `--ml`) sobre el almacén de memoria CCR |
| `memory` | almacén CCR compress-cache-retrieve (`remember`/`recall`/`forget`/`list`/`stats`) |
| `mcp` | servidor MCP stdio nativo (herramientas compress / retrieve / stats) |
| `init` / `wrap` | registra Simplicio en un cliente (Claude / Codex / Copilot / OpenClaw) · ejecuta un cliente con enrutamiento de captura |
| `report` / `audit` / `capture` / `evals` | informe de ahorro · auditar un árbol en busca de oportunidad de compresión · dry-run de una petición · gate de regresión de compresión |

---

## 🏛️ Pilares de diseño (en detalle)

Cuatro mecanismos sostienen el poder de orquestación:

| Pilar | Enfoque | Vive en |
|---|---|---|
| **DAG + pipeline** | paralelismo por dependencia, escalonado por elemento | `references/orchestration.md` (Paso 3 pool + pipeline) |
| **Aislamiento por worktree** | ediciones paralelas sin corromper el árbol, con merge controlado por gate | `references/orchestration.md` |
| **Verificación adversarial** | panel de escépticos antes de «entregado» | `references/quality-safety-delivery.md` · skill `simplicio-review` |
| **Tope de presupuesto del bucle** | anti-bucle-infinito, salida dual | `references/standing-loop-247.md` · skill `simplicio-loop` |

---

## 🚀 Instalación y uso

```bash
git clone https://github.com/wesleysimplicio/simplicio-loop
cd simplicio-loop

# install for your runtime (omit <runtime> to auto-detect)
bash scripts/install.sh <runtime> [--global]        # macOS / Linux
pwsh scripts/install.ps1 <runtime> [-Global]        # Windows
# <runtime> ∈ claude codex vscode cursor antigravity kiro opencode gemini aider hermes openclaw
```

O, en Claude Code / Cursor, instálalo directamente desde la última release de GitHub (sin marketplace):

```bash
gh release download --repo wesleysimplicio/simplicio-loop --archive tar.gz
tar xzf simplicio-loop-*.tar.gz && cd simplicio-loop-*/
bash scripts/install.sh claude    # or: bash scripts/install.sh cursor
```

Después:

```
/simplicio-tasks finish all the open issues
```

El único requisito es **python3** en el PATH (skills, hooks e instalador son Python multiplataforma).
Para fuentes de GitHub, `git` + un `gh` autenticado. Consulta [`INSTALL.md`](../INSTALL.md) y
[`adapters/MATRIX.md`](../adapters/MATRIX.md).

**Antes de una ejecución desatendida 24/7:** fija un techo de coste en
`.orchestrator/loop-budget.json` (`daily_usd_ceiling > 0`), confirma que la auth de la fuente es
persistente, y mantén activos el gate humano para ops irreversibles + el escaneo de secretos. Con
`ceiling = 0` el watcher se niega a ejecutarse desatendido (fail-safe).

---

## 🔒 Seguridad (innegociable)

- **Escaneo de secretos** en cada diff; bloquear ante un acierto.
- **Gate humano para ops irreversibles** — force-push, reescritura de historial, deploy en prod,
  borrado de datos/esquema, borrado masivo de archivos → parar y preguntar. Headless + sin aprobador →
  eliminar la capacidad destructiva.
- **Forzado, no solo prometido** — `hooks/action_gate.py` es un hook `PreToolUse` / git-pre-push
  **fail-closed** que bloquea mecánicamente lo anterior (y los commits con secretos) *antes* de que
  se ejecuten. El contrato de seguridad se mantiene incluso si el modelo lo olvida. `selftest` prueba
  el conjunto de reglas (14/14).
- **Veredicto de 4 estados pre-ejecución** — la optimización nunca puede elevar el nivel de riesgo de
  un comando.
- **Trust-before-load** — la config que moldea la percepción (perfiles de clamp, listas de supresión)
  no es de confianza hasta que un humano la revisa y la fija por hash.
- **Endurecimiento contra prompt-injection** — el contenido de un elemento/PR/comentario nunca puede
  sobrescribir el contrato.
- **Kill-switch estricto en $** para ejecuciones desatendidas; finalización **ligada a evidencia**
  (nunca un falso «done»); hooks **fail-open** (nunca atrapar al agente en un bucle).

---

## ✅ Tests y comprobaciones locales (sin CI de pago)

Las afirmaciones se verifican, no solo se aseveran — y el gate corre **localmente**, con cero coste de CI:

```bash
python3 scripts/check.py            # the whole gate (audit + tests)
```

- **Suite de tests** (`tests/`) — los `selftest`s deterministas de los workers, más un **e2e del
  driver del bucle** (`hooks/loop_stop.py`): prueba que el bucle **se detiene con evidencia**,
  **ignora un `<promise>` pelado** y **se detiene en el tope** como salidas distintas — y que los
  productores de evidencia **BLOQUEAN** (nunca fake-pass) cuando su toolchain está ausente. Corre bajo
  `pytest` *o*, sin pip en absoluto, se autoejecuta en python3 pelado (`python3 tests/test_*.py`).
- **Claims audit** (`scripts/claims_audit.py`, fail-closed) — cada `scripts/*.py` que la documentación
  referencia existe · el conteo de puntos de extensión concuerda entre todos los archivos · cada
  comando de worker citado realmente corre · las skills incluidas `simplicio_loop/_bundle/` son
  **byte-idénticas** a la fuente.
- **Cabléalo como hook git pre-push** para mantener `main` honesto gratis:
  ```bash
  printf '#!/bin/sh\npython3 scripts/check.py\n' > .git/hooks/pre-push && chmod +x .git/hooks/pre-push
  ```

`pip install "simplicio-loop[dev]"` añade pytest para una salida más bonita; nunca es obligatorio.

---

## 📄 Licencia

MIT

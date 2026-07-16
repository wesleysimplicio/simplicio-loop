# 🔁 simplicio-loop — The Universal Looping AI Orchestrator

<p align="center">
  <img src="../assets/simplicio-loop-hero-2026.png" alt="simplicio-loop autonomous parallel evidence-gated orchestration" width="920" />
</p>

<p align="center">
  <a href="https://github.com/wesleysimplicio/simplicio-loop/stargazers"><img src="https://img.shields.io/github/stars/wesleysimplicio/simplicio-loop?style=social" alt="Stars"></a>
  <a href="#-11-个-skill-与加速器"><img src="https://img.shields.io/badge/skills-11-7C3AED" alt="11 skills"></a>
  <a href="#-来源适配器"><img src="https://img.shields.io/badge/source%20adapters-5-00E08A" alt="5 source adapters"></a>
  <a href="#-11-个运行时一套协议"><img src="https://img.shields.io/badge/runtimes-11-2563EB" alt="11 runtimes"></a>
  <a href="#-11-个运行时一套协议"><img src="https://img.shields.io/badge/extension%20points-44-00E08A" alt="44 extension points"></a>
  <a href="#-token-经济"><img src="https://img.shields.io/badge/tokens-up%20to%2096%25%20fewer-green" alt="Up to 96% fewer tokens"></a>
  <a href="../LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue" alt="License"></a>
</p>

<p align="center">
  <a href="#-tldr">摘要</a> ·
  <a href="#-11-个-skill-与加速器">11 个 Skill</a> ·
  <a href="#-来源适配器">来源适配器</a> ·
  <a href="#-11-个运行时一套协议">11 个运行时</a> ·
  <a href="#-循环">循环</a> ·
  <a href="#-token-经济">Token 经济</a> ·
  <a href="#-token-经济">捕获引擎</a> ·
  <a href="#-安装与使用">安装</a>
</p>

<p align="center">
  <strong>🌍 语言：</strong><br>
  <a href="../README.md">🇬🇧 English</a> |
  <a href="README.pt-BR.md">🇧🇷 Português</a> |
  <a href="README.es-ES.md">🇪🇸 Español</a> |
  <a href="README.fr-FR.md">🇫🇷 Français</a> |
  <a href="README.de-DE.md">🇩🇪 Deutsch</a> |
  <a href="README.it-IT.md">🇮🇹 Italiano</a> |
  <a href="README.ja-JP.md">🇯🇵 日本語</a> |
  <a href="README.ko-KR.md">🇰🇷 한국어</a> |
  <strong>🇨🇳 简体中文</strong> |
  <a href="README.ru-RU.md">🇷🇺 Русский</a> |
  <a href="README.pl-PL.md">🇵🇱 Polski</a> |
  <a href="README.tr-TR.md">🇹🇷 Türkçe</a> |
  <a href="README.nl-NL.md">🇳🇱 Nederlands</a> |
  <a href="README.hi-IN.md">🇮🇳 हिन्दी</a> |
  <a href="README.ar-SA.md">🇸🇦 العربية</a>
</p>

---

<!-- visual-story:start -->
## 🚀 新一代 — 面向可验证智能体工作的操作系统

**simplicio-loop 已远不只是一个重复提示直到完成的工具。** 它把意图编译成冻结的任务契约，映射代码仓库，按依赖关系调度，把执行分发到隔离的 worktree，收集结构化凭证，进行独立验证和安全 rollback，记住每次尝试，并在交付前持续同步 source of record。

- **契约优先** — 在执行前明确验收标准、依赖、风险、来源状态和完成判定器。
- **并行而不破坏** — 就绪任务在隔离的 lane/worktree 中运行，并通过操作 ledger 汇聚。
- **先证明，再完成** — test、impact/flow 检查、watcher challenge、交付凭证和 HBP evidence 会拒绝虚假的 done 状态。
- **能改变行为的记忆** — journal、stall detector、checkpoint 和 cross-agent wiki 防止重复振荡，并让 handoff 持久可靠。

<p align="center">
  <img src="../assets/simplicio-loop-parallel-worktrees.png" alt="simplicio-loop parallel isolated worktree execution" width="920" />
</p>

<p align="center"><em>依赖感知的 fan-out：隔离 worker 并行执行、返回证据，并汇聚为一个已验证交付物。</em></p>

<p align="center">
  <img src="../assets/simplicio-loop-lifecycle-2026.svg" alt="simplicio-loop lifecycle from intake to durable memory" width="920" />
</p>

<p align="center"><em>每个阶段都明确、有界、可观测、可回滚。</em></p>

<p align="center">
  <img src="../assets/simplicio-loop-evidence-memory.png" alt="simplicio-loop evidence memory verification rollback and completion" width="920" />
</p>

<p align="center"><em>证据和记忆属于执行路径本身，而不是事后编写的报告。</em></p>

这套架构能把一个目标变成受治理的交付系统：从单个高难任务到整个 backlog，跨 session 与 runtime 运行，使用 local-first operator，并留下可供人、CI 或其他智能体审计的 receipt。

<p align="center">
  <img src="../assets/simplicio-loop-architecture-2026.svg" alt="simplicio-loop control execution evidence and delivery planes" width="920" />
</p>
<!-- visual-story:end -->

## ⚡ TL;DR

**simplicio-loop** 是一个与运行时无关的**超级插件** —— 一个自主循环式编排器
（以 **`/simplicio-loop`** 调用），外加**五个卫星 skill** —— 它能把任何强大的 LLM
（Claude、Codex、Copilot、Gemini、Cursor、本地模型）变成一个自动驾驶的工作者。你只需
把它指向一批工作 —— *“完成所有未关闭的 issue”*、*“清空 CI 队列”*、*“清干净 Jira 看板”* ——
它就会自行运转完整的生命周期：

> **发现 → 理解 → 决策 → 行动 → 验证 → 纠正 → 记录 → 重复**

它会从任意来源（GitHub Issues、Jira、Azure DevOps、agentsview 会话等）发现工作、去重、
按你的机器自动伸缩一支智能体队伍，通过一个**真正运行代码（而不仅仅是编译）**的质量循环来
实现每一项工作，开 PR、处理 CI/评审反馈、合并，并持续 **7×24** 监视新工作 —— 这一切都在
安全门控和一个硬性成本急停开关的背后进行。

```text
/simplicio-loop finish all open issues
→ identity + pre-flight (auth, runtime, STOP path)
→ discover 50 issues · dedup · build dependency DAG
→ autoscale fleet = 14 · pipeline implement→review→merge
→ each item: read body+ACs → orient code → plan → edit → run → verify → PR
→ merge · close with evidence · rollback if main breaks
→ keep looping every ~2 min until the queue is dry (evidence-gated, never a false "done")
```

让它与众不同的有三点：它是一个**由专注型 skill 组成的超级插件**，它在 **11 个运行时上运行
同一套协议**，而且它在做这一切时贯彻着**激进而诚实的 token 经济**。

---

## 📘 官方能力清单

`simplicio-loop` 所交付内容的完整、官方名册 —— 下面的每一项能力都是**真实、可运行、
经过测试的**（`python3 scripts/check.py`：claims-audit 4/4 + 28 项测试）。每一项都链接到它的
深入小节与它的 worker。

| 能力 | 它做什么 | 证明 / worker | 详情 |
|---|---|---|---|
| 🎬 **视频证据**（`video_evidence`） | 录制**真实浏览器会话**，作为 UI 改动确实可用的动态证明（Playwright，默认）；当显式索取讲解视频时（`/simplicio-loop make a video of screen X`），用 [hyperframes](https://github.com/heygen-com/hyperframes) 渲染一段 CI 可复现的**确定性带字幕 MP4** | `scripts/video_evidence.py` · 缺少工具链时 BLOCKED（绝不假装通过） | [§ 视频证据](#-视频证据--默认-playwright应请求时-hyperframes) |
| 🧠 **尝试记忆 + 停滞检测器** | 一份耐久的运行日志（`.orchestrator/loop/journal.jsonl`）+ 一个停滞检测器，让循环**改变策略而非来回振荡**；增量分诊（`since`）每轮只读取增量部分 | `scripts/loop_journal.py` · `selftest` 9/9 | [§ 防振荡](#-尝试记忆--停滞检测器防振荡) |
| 🔒 **失败即关闭的安全门**（`action_gate`） | 一个 `PreToolUse`/git-pre-push 钩子，**以机械方式阻断** force-push、历史重写、批量删除、破坏性 DDL、基础设施拆除以及携带密钥的提交/推送 —— 把第 5 步从散文变成可执行 | `hooks/action_gate.py` · `selftest` 15/15 | [§ 安全](#-安全不可妥协) |
| 🔬 **本地验证** | 一套测试套件（worker selftest + 一个证明经证据门控退出的**循环驱动器 e2e**）+ 一份 **claims-audit**（被引用的脚本存在 · 计数一致 · `_bundle ≡ source`）—— 全部本地、**无需付费 CI** | `scripts/check.py` · `scripts/claims_audit.py` · `tests/` | [§ 测试与本地检查](#-测试与本地检查无需付费-ci) |
| ✅ **诚实的节省** | 节省那一行现在是**经证据门控的，而非强制的** —— 只有在拿到一份实测凭据（clamp/signatures/cache/`deterministic_edit`/ledger）时才会显示数字；绝不编造 | token 经济契约 | [§ Token 经济](#-token-经济) |

两种循环**模式**让终止变得明确：**converge**（单个硬任务 —— 在经证据门控的 `<promise>`
或一次停滞升级时结束）vs **drain**（一个队列 —— 当来源重新查询连续 K 轮保持为空时结束）。
Both modes are still governed by universal exits: promise+evidence, `max_iterations`, and STOP.

> 这条工作线上的循环评分：**7.5**（设计强，但未经证明）→ **9**（尝试记忆 + 防振荡）→
> **9.5**（可复现的本地证明）→ **~10**（强制安全 + 完整的循环语义）。验证基础设施现在
> 会随着项目成长而捕获其自身的回归。

---

## 🧠 11 个 skill 与加速器

编排器核心 + 五个卫星 + 五个加速器/集成。每个卫星都是**可选的** —— 加载后，编排器会委派给它
（更丰富、更便宜）；缺席时，内联协议覆盖 100% 的工作。加速器是**自动探测**的 ——
存在即使用，缺席则回退到 LLM 兜底。

| # | 能力 | 吸收自 | 它做什么 | Token 影响 |
|---|---|---|---|---|
| 1 | 🔁 **simplicio-loop** | — | Unified public entrypoint: orchestrator core + hardened loop behind one command | Core + loop |
| 2 | ↩️ **simplicio-tasks** | legacy alias | Compatibility shim for older installs and saved prompts | Legacy alias |
| 3 | 🧱 **simplicio-orient** | [rtk](https://github.com/rtk-ai/rtk) + [caveman](https://github.com/JuliusBrussee/caveman) | 终端优先执行、输出缩减目录、tee-cache、仅签名读取 | L0 确定性 |
| 4 | 🔥 **simplicio-review** | [thermos](https://github.com/cursor/plugins/tree/main/thermos) | 按不同评分标准并行对抗式评审 → 去重裁决 | 质量门控 |
| 5 | 🗜️ **simplicio-compress** | [caveman](https://github.com/JuliusBrussee/caveman) | 输出 + 记忆压缩、fail-closed 的 `transform_guard` | 减少 40-60% |
| 6 | 🎓 **simplicio-learn** | [teaching](https://github.com/cursor/plugins/tree/main/teaching) | 运行后复盘 → 写入记忆的耐久、去重经验 | 每次运行更聪明 |
| 7 | 🧭 **Understand Anything** | [Egonex-AI](https://github.com/Egonex-AI/Understand-Anything) | 知识图谱定向：语义搜索、引导式游览、依赖图 | **L0 零 token** |
| 8 | 📊 **agentsview** | [kenn-io](https://github.com/kenn-io/agentsview) | 会话分析、成本追踪、停滞会话发现 | **L1** 仅 SQL |
| 9 | ⚡ **LMCache** | [LMCache](https://github.com/LMCache/LMCache) | 循环各轮之间的 KV 缓存 —— 本地模型 TTFT 降低 40-70% | GPU 时间 ↓ |
| 10 | 🗜️ **Simplicio 捕获引擎** | `engine/simplicio_engine.py`（原生，仅依赖标准库） | 透明捕获代理：转发到真实供应商，度量 + 确定性压缩，写入 `proxy_savings.json` | **确定性** |
| 11 | 🎬 **video_evidence** | Playwright（默认）· [hyperframes](https://github.com/heygen-com/hyperframes)（应请求） | 录制**真实会话**，作为 UI 改动的动态证明（Playwright）；当视频本身就是交付物时，用 hyperframes 渲染一段**确定性带字幕 MP4** 讲解视频 | 证据生产者 |

每个 skill 都位于 [`.claude/skills/`](../.claude/skills) 下；每个加速器在
`.claude/skills/simplicio-loop/references/` 下都有一份参考文档（视频生产者：
[`video-evidence.md`](../.claude/skills/simplicio-loop/references/video-evidence.md)，worker
[`scripts/video_evidence.py`](../scripts/video_evidence.py)）。

---

## 📡 来源适配器

编排器通过可插拔的适配器从任意来源发现工作。每个适配器都暴露六个动词：
`list_ready`、`get_details`、`claim`、`update_status`、`attach_evidence`、`close`。

| 来源 | 适配器 | 用途 |
|---|---|---|
| GitHub Issues/PRs | `gh` CLI（原生） | 主要工作项来源 |
| Jira / Asana / ClickUp / Linear / Notion | 宿主连接器 | 看板/项目管理 |
| Trello / Azure DevOps | `az boards` 适配器 | Azure 工作追踪 |
| **agentsview 会话** | `scripts/agentsview_adapter.py` | 停滞会话恢复 + 成本可观测性 |
| 本地文件 / CI 队列 | 文件系统 / CI API | 内部工作追踪 |

参见每个适配器在 `.claude/skills/simplicio-loop/references/` 下的参考文档。

---

## 🌐 11 个运行时，一套协议

一个通用的 skill 内核 + 一套钩子驱动每一个运行时。适配器很薄：它告诉运行时*去哪里加载
skill*、*如何武装循环*、*如何绑定原生速度*。**skill 不指名任何运行时；是运行时来探测 skill。**

| 运行时 | Skill 加载 | 循环驱动 | 原生绑定 |
|---|---|---|---|
| **Claude Code** | `.claude/skills/` + plugin | `Stop` 钩子 | MCP |
| **Codex** | `AGENTS.md` | 自定步 | MCP / adapter |
| **VS Code (Copilot)** | `copilot-instructions.md` | tasks | MCP |
| **Cursor** | `.cursor-plugin/` | `stop`+`afterAgentResponse` | MCP / rules |
| **Antigravity** | rules / `AGENTS.md` | 自定步 | MCP |
| **Kiro** | `.kiro/steering/` | specs | MCP |
| **OpenCode** | `AGENTS.md` | 自定步 | MCP |
| **Gemini** | `GEMINI.md` | 自定步 | MCP / adapter |
| **Aider** | `CONVENTIONS.md` | 自定步 | ——（LLM 兜底） |
| **Simplicio Agent** | native recall | native loop | **native** |
| **OpenClaw** | plugin SDK | native scheduler | **native** |

承诺是：**同一套协议、同一组门控、同样的安全性，在全部 11 个上 —— 唯一的区别是速度。**
`orient_clamp.py`（token 经济）在每个运行时上零接线即可工作。参见
[`adapters/MATRIX.md`](../adapters/MATRIX.md)。

---

## 🗺️ 完整流程 —— 从需求到交付

编排器按顺序作用的每一层 —— 从读取需求（issue、任务、指派）开始，到交付已合并、有证据
支撑的成果，随后再以 7×24 循环寻找更多工作。

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

## 🔁 循环

**经证据门控的循环**是核心机制。它在每一轮重新投喂同一目标，于是智能体能看见自己
先前的工作。退出仅通过：

1. **经证据门控的 `<promise>`** —— 发出该承诺的那一轮必须同时携带具体证据（通过的测试、
   已合并的 PR、已关闭项的重新查询）。没有证据的承诺 = 被忽略。
2. **`max_iterations` 上限** —— 硬性安全防线
3. **STOP/cancel path** — explicit STOP file or channel command stops unattended runs
4. **STOP 信号** —— `.orchestrator/STOP` 或通道命令

在各轮之间，LMCache（可用时）会缓存 KV 状态，于是重新投喂的 prefill 成本接近于零。

### 🧠 尝试记忆 + 停滞检测器（防振荡）

一个什么都记不住的重新投喂循环会振荡 —— 试 X、失败、再试 X —— 直到把上限烧光。
simplicio-loop 维护一份**耐久的运行日志**（`.orchestrator/loop/journal.jsonl`，仅追加：
`iteration · action · hypothesis · gate · error-fingerprint`）和一个**停滞检测器**
（[`scripts/loop_journal.py`](../scripts/loop_journal.py)，确定性 + 无需模型）：

- **错误指纹** —— 失败门控的输出被归约为一个稳定哈希，其中行号、路径、hex/uuid、时间戳和
  耗时都被归一化掉，于是即使附带文本有别，*同一个* bug 也能跨轮被识别出来。
- **停滞 = 连续 K 次相同指纹的失败**（默认 K=3）。变化的指纹意味着循环在前进
  （PROGRESS）；同一个出现 K 次则意味着它在空转（STALLED）。
- 一旦 STALLED，循环**不会**重新投喂同一目标 —— 它会点名应避开的**死胡同动作**，然后
  **切换策略**或带着指纹**升级到人工门控**。
- `loop_journal.py resume` 在每一轮开头被读取，于是一个全新进程无需重新推导先前的尝试
  即可继续（真正的恢复），且绝不重试一个已知的死胡同。

```bash
loop_journal.py resume                       # what was tried + dead-ends to avoid
loop_journal.py record --iteration N --action "…" --gate fail --gate-output test.log
loop_journal.py stall --k 3 --exit-code      # PROGRESS → re-feed · STALLED → switch/escalate
```

---

## 🎬 视频证据 —— 默认 Playwright，应请求时 hyperframes

循环会生成**演示视频**作为某个改动可用的证明 —— **两种引擎**，共用一个 `video_evidence`
扩展点（worker [`scripts/video_evidence.py`](../scripts/video_evidence.py)，契约
[`references/video-evidence.md`](../.claude/skills/simplicio-loop/references/video-evidence.md)）：

1. **默认 —— 常规证据流程使用 Playwright。** 在一次 UI 改动之后，`video_evidence` 录制
   **真实浏览器会话**驱动该屏幕的过程（Playwright 原生视频 → `.webm`，再用 FFmpeg →
   `.mp4`）—— 这是最强的“可用，而不仅仅是编译通过”凭据（第 4b 步），也是一个有效的、
   经证据门控的 `<promise>`。

   ```bash
   python3 scripts/video_evidence.py verify --url http://localhost:3000/login \
       --name login-demo --expect "Sign in" --issue 42 [--upload --pr 42]
   ```

2. **应请求 —— 个性化讲解视频使用 hyperframes。** 当交付物本身就是一段视频时
   （“make an explainer video of screen X”），编排器会用
   [**hyperframes**](https://github.com/heygen-com/hyperframes)（来自 HeyGen —— “相同输入、
   相同帧、相同输出”，CI 可复现，无需 API 密钥，通过无头 Chrome + FFmpeg 本地渲染）把
   `web_verify` 的截图渲染成一段**确定性的带字幕幻灯片**。

   ```text
   /simplicio-loop make an explainer video of the system login screen
   → detect: video-creation request → web_verify captures the screens
   → video_evidence verify --engine hyperframes → deterministic MP4 → attached to the PR
   ```

无论哪种引擎：一段从未录制/渲染出来的视频都会得到 **BLOCKED**，绝不假装通过。证据始终是一个
**文件路径 + 布尔裁决** —— 绝不把视频字节放入上下文（token 经济）。

---

## 📊 Token 经济

| 技术 | 节省 |
|---|---|
| `deterministic_edit`（L0） | 100% 的编辑 token（文件由机械方式写入，绝不由 LLM 写入） |
| 终端优先执行 | 事实来自 shell，而非 LLM 臆造 |
| 输出缩减目录 | 按命令类型设上限（`CAP_ERRORS=20`、`CAP_WARNINGS=10`、`CAP_LIST=20`）—— `orient_clamp.py` |
| 失败时 Tee+CCR 缓存 | 绝不重跑失败的命令 —— 读取已缓存的输出 |
| 仅签名读取 | `simplicio-cli signatures <file>` —— 870 行文件 → 65 行（**节省 93%**），剥离函数体 |
| `simplicio-compress` | 精简散文 + 一次性记忆压实 |
| `orient_clamp.py` | 对每条 shell 命令钳制 + tee，零接线 |
| 原生响应缓存 | 重复的确定性（temp=0）请求 → 从缓存返回，跳过 LLM 调用（**命中即 100%**）—— `simplicio-cli cache`，默认开启（`SIMPLICIO_CACHE=0` 可禁用） |
| Simplicio 捕获代理 + MCP | 通过一个透明压缩守护进程，工具输出 token 减少 60-95% |

只有在结果经验证为正确时才计入节省。基线 = 通向同一结果的最便宜、合理且未经编排的路径。
**节省的上报是经证据门控的，而非强制的：** 只有当某一轮确实运行了一条产生经济效益的命令、
且该数字可追溯到一份实测凭据（clamp tee、仅签名读取、缓存命中、`deterministic_edit`、
`savings_ledger`）时，才会显示一个节省数字。没有实测的经济效益 → 没有节省那一行；编排器
绝不编造基线或百分比。参见 `references/token-economy.md`。

### 🔎 运行 `simplicio-loop`：经济 vs 度量（按运行时）

当你调用 **`simplicio-loop`** 时会发生两件不同的事，它们在各运行时上的行为也不同：

- **经济** —— 压缩、输出钳制、仅签名读取、`deterministic_edit` —— 只要 skill 运行并加载了
  `simplicio-orient` / `simplicio-compress`，**在任何运行时上每一次都会生效。** 它是 skill 的
  行为加上钩子（在有钩子的地方最强：`orient_clamp.py` 在 Claude 和 Cursor 上自动钳制；其他地方
  则由指令驱动）。
- **度量** —— Token 监视器的实时数字 —— 只统计流经**捕获代理**的流量。

| 运行时 | 经济（skill） | 度量（监视器） |
|---|---|---|
| **Simplicio Agent** | ✓ | ✓ **自动** —— 已经经由代理路由（`base_url → :8788`） |
| **Claude** | ✓（skill + 钩子） | ✗ 默认 —— Claude 直接与 `api.anthropic.com` 通信；只有在路由之后才被度量（`simplicio-cli wrap claude`，或 `ANTHROPIC_BASE_URL → http://127.0.0.1:8788`） |
| **Codex** | ✓（skill） | ✗ 默认 —— `simplicio-cli init codex` 会添加 MCP 工具但不路由 LLM 流量；用 `simplicio-cli wrap codex` 或一个指向代理的 OpenAI base-url 来度量 |

所以：**节省在每个运行时上都会发生**；**监视器在 Simplicio Agent 上会自动统计它们**，并在 Claude/Codex 上
经过一次**一次性路由步骤**（`simplicio-cli wrap …` / base-url → `:8788`）后统计。没有路由，经济
依然生效 —— 只是监视器不会统计那些 token。`scripts/simplicio-economy.sh wire` 会在安装时为
OpenAI 兼容客户端完成这一路由。

### 📈 Simplicio Token 监视器

一个实时、始终在线的节省视图：

- **Web 仪表盘** —— `http://127.0.0.1:9090` —— 实时 token 图表、节省仪表、我们拦截的
  LLM/运行时与 **141/144 个供应商（98%）**，以及一份实时代理日志。
- **菜单栏 / 托盘小组件** —— 在系统托盘中实时显示已节省的 token（macOS rumps · Windows/Linux pystray）。
- **一个模块** —— `scripts/simplicio-economy.sh {status|up|wire}` 启动捕获代理 + 监视器 +
  托盘 + `simplicio-dev-cli` 确定性操作器，并汇报整套栈。

安装时会通过 `scripts/setup_simplicio.sh`（或跨平台的
`python3 scripts/install_services.py install`）把这三者全部注册为开机自启服务
（macOS launchd · Linux systemd · Windows 启动项）。安装后，监视器 + 捕获**无需调用循环**
即可运行 —— 参见 `references/token-capture.md`。

### 🛠️ 捕获引擎 —— 一个原生模块，覆盖每条命令

[`engine/simplicio_engine.py`](../engine/simplicio_engine.py) 是原生的 Simplicio 捕获引擎
（仅依赖标准库、fail-open、无任何外部依赖）。通过
[`scripts/simplicio-engine`](../scripts/simplicio-engine) 包装器运行任意命令
（例如 `simplicio-engine doctor`）：

| 命令 | 它做什么 |
|---|---|
| `proxy` | 透明捕获代理 —— 把每个模型路由到它**真实的**供应商，压缩 + 度量 + 缓存（不替换模型） |
| `doctor` | 代理可达性 + 终身节省 |
| `cache` | 原生响应缓存（`stats`/`clear`）—— 重复的确定性请求从缓存返回，跳过 LLM 调用 |
| `signatures` | 源文件的仅签名视图（剥离函数体，读代码所需 token 减少约 93%） |
| `semantic` | 可逆的抽取式（semantic-lite）压缩 |
| `detect` | 内容类型检测 + 按块的智能路由 |
| `rag` | 在 CCR 记忆库上进行 TF-IDF（或 `--ml` 嵌入）检索 |
| `memory` | CCR compress-cache-retrieve 库（`remember`/`recall`/`forget`/`list`/`stats`） |
| `mcp` | 原生 stdio MCP 服务器（compress / retrieve / stats 工具） |
| `init` / `wrap` | 把 Simplicio 注册进客户端（Claude / Codex / Copilot / OpenClaw）· 以捕获路由运行客户端 |
| `report` / `audit` / `capture` / `evals` | 节省报告 · 审计一棵树的压缩机会 · 干跑一个请求 · 压缩回归门控 |

---

## 🏛️ 设计支柱（详解）

支撑起编排能力的机制有四个：

| 支柱 | 焦点 | 所在 |
|---|---|---|
| **DAG + 流水线** | 按依赖并行，逐项分阶段 | `references/orchestration.md`（Step 3 池 + 流水线） |
| **Worktree 隔离** | 不破坏工作树的并行编辑，受合并门控 | `references/orchestration.md` |
| **对抗式验证** | 在“交付”之前来一组怀疑者 | `references/quality-safety-delivery.md` · skill `simplicio-review` |
| **Bounded loop cap** | anti-infinite-loop, evidence-gated exit | `references/standing-loop-247.md` · skill `simplicio-loop` |

---

## 🚀 安装与使用

```bash
git clone https://github.com/wesleysimplicio/simplicio-loop
cd simplicio-loop

# install for your runtime (omit <runtime> to auto-detect)
bash scripts/install.sh <runtime> [--global]        # macOS / Linux
pwsh scripts/install.ps1 <runtime> [-Global]        # Windows
# <runtime> ∈ claude codex vscode cursor antigravity kiro opencode gemini aider simplicio_agent openclaw
```

或者，在 Claude Code / Cursor 上，直接从最新的 GitHub 发布版安装（无需市场）：

```bash
gh release download --repo wesleysimplicio/simplicio-loop --archive tar.gz
tar xzf simplicio-loop-*.tar.gz && cd simplicio-loop-*/
bash scripts/install.sh claude    # or: bash scripts/install.sh cursor
```

然后：

```
/simplicio-loop finish all the open issues
```

唯一的要求是 PATH 上有 **python3**（skill、钩子和安装器都是跨平台的 Python）。对于 GitHub
来源，需要 `git` + 一个已认证的 `gh`。参见 [`INSTALL.md`](../INSTALL.md) 和
[`adapters/MATRIX.md`](../adapters/MATRIX.md)。

**Before an unattended 24/7 run:** verify persistent source auth, keep the irreversible-operation human gate + secret-scan enabled, and ensure a reachable STOP/cancel path.

---

## 🔒 安全（不可妥协）

- 对每个 diff 进行**密钥扫描**；命中即阻断。
- **不可逆操作人工门控** —— force-push、历史重写、生产部署、数据/schema 删除、批量文件删除
  → 停下来询问。无头 + 无审批者 → 移除该破坏性能力。
- **强制执行，而不仅是承诺** —— `hooks/action_gate.py` 是一个 **fail-closed** 的 `PreToolUse` /
  git-pre-push 钩子，它在上述操作（以及携带密钥的提交）*运行之前*以机械方式阻断它们。
  即使模型忘记了，安全契约依然成立。`selftest` 证明了该规则集（14/14）。
- **四态执行前裁决** —— 优化绝不能抬高一条命令的风险等级。
- **先信任后加载** —— 塑造感知的配置（钳制配置档、抑制列表）在人类审查并以哈希钉死之前一律
  视为不可信。
- **提示注入加固** —— 工作项/PR/评论内容绝不能覆盖契约。
- 面向无人值守运行的**硬性 $ 急停开关**；**经证据门控**的完成（绝不虚假“完成”）；**fail-open**
  的钩子（绝不把智能体困在循环里）。

---

## ✅ 测试与本地检查（无需付费 CI）

声明都经过验证，而不仅仅是断言 —— 而且这道门控**在本地**运行，零 CI 成本：

```bash
python3 scripts/check.py            # the whole gate (audit + tests)
```

- **测试套件**（`tests/`）—— worker 的确定性 `selftest`，外加一个**循环驱动器
  （`hooks/loop_stop.py`）的 e2e**：它证明该循环**在证据上停止**、**忽略一个裸的
  `<promise>`**、并在**上限处停止**，三者是不同的退出路径 —— 还证明证据生产者在其工具链
  缺席时会 **BLOCK**（绝不假装通过）。在 `pytest` 下运行*或*在完全没有 pip 时在裸 python3
  上自运行（`python3 tests/test_*.py`）。
- **声明审计**（`scripts/claims_audit.py`，fail-closed）—— 文档引用的每个 `scripts/*.py`
  都存在 · 扩展点计数在所有文件中一致 · 每条被引用的 worker 命令确实能运行 · 随附的
  `simplicio_loop/_bundle/` skill 与源码**逐字节相同**。
- **把它接成 git pre-push 钩子**，免费保持 `main` 诚实：
  ```bash
  printf '#!/bin/sh\npython3 scripts/check.py\n' > .git/hooks/pre-push && chmod +x .git/hooks/pre-push
  ```

`pip install "simplicio-loop[dev]"` 会加入 pytest 以获得更友好的输出；但它从不是必需的。

---

## 📄 许可证

MIT

<!-- simplicio-loop:github-comment-coordination:v1 -->
## 🌐 通过 GitHub 评论在不同运行时之间协调

`simplicio-loop` 可以同时运行在 Claude Code、Codex、Cursor、Gemini 和 Hermes 中。绑定到 GitHub issue 的运行会在规范评论中幂等地发布认领、计划、进度、证据、PR 和关闭状态。不同机器上的 agent 可以通过同一个 GitHub 讨论串协调，而不需要共享本地文件系统。

```powershell
pwsh scripts/install.ps1 claude -Global
pwsh scripts/install.ps1 codex -Global
pwsh scripts/install.ps1 cursor -Global
pwsh scripts/install.ps1 gemini -Global
pwsh scripts/install.ps1 hermes -Global   # simplicio_agent 的旧别名
```

本地队列、lease、worktree、heartbeat 和证据仍在每台机器上运行；GitHub 评论是共享协调投影。此流程仅支持 GitHub，Jira、Azure DevOps 及其他 tracker 不会收到评论。GitHub 不可用时 loop 仍可本地运行并记录同步失败，不会伪造远程确认。请为每个 runtime 提供 GitHub 权限并使用同一个 `source_issue`。

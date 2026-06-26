# 🔁 simplicio-tasks — The Universal Looping AI Orchestrator

<p align="center">
  <img src="../assets/simplicio-loop-hero.jpg" alt="simplicio-loop" width="920" />
</p>

<p align="center">
  <a href="https://github.com/wesleysimplicio/simplicio-loop/stargazers"><img src="https://img.shields.io/github/stars/wesleysimplicio/simplicio-loop?style=social" alt="Stars"></a>
  <a href="#-the-11-skills--accelerators"><img src="https://img.shields.io/badge/skills-11-7C3AED" alt="11 skills"></a>
  <a href="#-source-adapters"><img src="https://img.shields.io/badge/source%20adapters-5-00E08A" alt="5 source adapters"></a>
  <a href="#-11-runtimes-one-protocol"><img src="https://img.shields.io/badge/runtimes-11-2563EB" alt="11 runtimes"></a>
  <a href="#-the-44-extension-points"><img src="https://img.shields.io/badge/extension%20points-44-00E08A" alt="44 extension points"></a>
  <a href="#-token-economy"><img src="https://img.shields.io/badge/tokens-up%20to%2096%25%20fewer-green" alt="Up to 96% fewer tokens"></a>
  <a href="../LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue" alt="License"></a>
</p>

<p align="center">
  <a href="#-tldr">TL;DR</a> ·
  <a href="#-the-11-skills--accelerators">11のスキル</a> ·
  <a href="#-source-adapters">ソースアダプタ</a> ·
  <a href="#-11-runtimes-one-protocol">11のランタイム</a> ·
  <a href="#-the-loop">ループ</a> ·
  <a href="#-token-economy">トークンエコノミー</a> ·
  <a href="#-token-economy">キャプチャエンジン</a> ·
  <a href="#-install--use">インストール</a>
</p>

<p align="center">
  <strong>🌍 Languages:</strong><br>
  <a href="../README.md">🇬🇧 English</a> |
  <a href="README.pt-BR.md">🇧🇷 Português</a> |
  <a href="README.es-ES.md">🇪🇸 Español</a> |
  <a href="README.fr-FR.md">🇫🇷 Français</a> |
  <a href="README.de-DE.md">🇩🇪 Deutsch</a> |
  <a href="README.it-IT.md">🇮🇹 Italiano</a> |
  🇯🇵 <strong>日本語</strong> |
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

**simplicio-tasks** はランタイム非依存の**スーパープラグイン**です——自律的にループする
オーケストレーター1つ（**`/simplicio-tasks`** として起動）と、**5つのサテライトスキル**から成り、
あらゆる高性能LLM（Claude、Codex、Copilot、Gemini、Cursor、ローカルモデル）を自走する
ワーカーへと変えます。作業のまとまり——*「開いているissueを全部片付けて」*、*「CIキューを空にして」*、
*「Jiraボードを消化して」*——を指定すれば、ライフサイクル全体を自力で回します。

> **発見 → 理解 → 決定 → 実行 → 検証 → 修正 → 記録 → 繰り返し**

任意のソース（GitHub Issues、Jira、Azure DevOps、agentsviewセッションなど）から作業を発見し、
重複を排除し、マシンに合わせてエージェント群を自動スケールし、**コードをコンパイルするだけでなく
実際に実行する**品質ループを通して各項目を実装し、PRを開き、CI／レビューのフィードバックを解消し、
マージし、新しい作業がないか**24時間365日**監視し続けます——そのすべてを安全ゲートと強制的な
コストキルスイッチの背後で行います。

```text
/simplicio-tasks finish all open issues
→ identity + pre-flight (kill-switch, auth, watcher)
→ discover 50 issues · dedup · build dependency DAG
→ autoscale fleet = 14 · pipeline implement→review→merge
→ each item: read body+ACs → orient code → plan → edit → run → verify → PR
→ merge · close with evidence · rollback if main breaks
→ keep looping every ~2 min until the queue is dry (evidence-gated, never a false "done")
```

これを他と分けるのは3点です。**焦点を絞ったスキルのスーパープラグイン**であること、**同じ
プロトコルを11のランタイムで**走らせること、そしてそのすべてを**積極的かつ誠実なトークン
エコノミー**で行うことです。

---

## 📘 公式ケイパビリティ記録

`simplicio-tasks` が提供するものの完全かつ公式な一覧です——以下のすべての機能は**実在し、実行可能で、
テスト済み**です（`python3 scripts/check.py`：claims-audit 4/4 ＋ 28テスト）。各項目は対応する詳細
セクションとワーカーへのリンクを持ちます。

| 機能 | 何をするか | 証明／ワーカー | 詳細 |
|---|---|---|---|
| 🎬 **Video evidence**（`video_evidence`） | UI変更が動作することの動く証明として**実際のブラウザセッションを録画**する（Playwright、既定）；明示的な解説動画の依頼（`/simplicio-tasks make a video of screen X`）には [hyperframes](https://github.com/heygen-com/hyperframes) で**決定論的なキャプション付きMP4**をレンダリングする | `scripts/video_evidence.py` · ツールチェーンがなければ BLOCKED（決してフェイクで通さない） | [§ Video evidence](#-video-evidence--playwright-by-default-hyperframes-on-request) |
| 🧠 **試行メモリ＋ストール検出器** | 耐久性のあるランジャーナル（`.orchestrator/loop/journal.jsonl`）＋ストール検出器により、ループが**振動する代わりに戦略を変える**；増分トリアージ（`since`）は毎ターン差分のみを読む | `scripts/loop_journal.py` · `selftest` 9/9 | [§ 振動防止](#-attempt-memory--stall-detector-anti-oscillation) |
| 🔒 **フェイルクローズの安全ゲート**（`action_gate`） | force-push、履歴の書き換え、大量削除、破壊的なDDL、インフラの解体、シークレットを含むコミット／プッシュを**機械的にブロック**する `PreToolUse`／git-pre-push フック——Step 5 を散文ではなく実行可能にしたもの | `hooks/action_gate.py` · `selftest` 15/15 | [§ Safety](#-safety-non-negotiable) |
| 🔬 **ローカル検証** | テストスイート（ワーカーのselftest＋エビデンスゲートでの終了を証明する**ループドライバのe2e**）＋**claims-audit**（参照されるスクリプトが存在 · カウントが一貫 · `_bundle ≡ source`）——すべてローカル、**有料CIなし** | `scripts/check.py` · `scripts/claims_audit.py` · `tests/` | [§ Tests & local checks](#-tests--local-checks-no-paid-ci) |
| ✅ **誠実な節約** | 節約の行は**エビデンスゲート付きで、必須ではない**ものになった——数値は計測されたレシート（clamp／signatures／cache／`deterministic_edit`／ledger）がある場合にのみ表示される；決して捏造しない | token-economy contract | [§ Token economy](#-token-economy) |

2つのループ**モード**が終了を明示します。**converge**（単一のハードタスク——エビデンスゲートを通った
`<promise>` またはストールのエスカレーションで終了）と **drain**（キュー——ソース再クエリがKラウンド
空のままで終了）。どちらも普遍的な出口（promise＋evidence、`max_iterations`、予算、STOP）に従います。

> このライン全体でのループの採点：**7.5**（強い設計、未実証）→ **9**（試行メモリ＋振動防止）→
> **9.5**（再現可能なローカル証明）→ **~10**（強制された安全性＋完全なループセマンティクス）。検証
> インフラは、プロジェクトの成長に伴い、プロジェクト自身のリグレッションを捕捉するようになりました。

---

## 🧠 The 11 skills & accelerators

オーケストレーターの中核＋5つのサテライト＋5つのアクセラレーター／インテグレーション。各サテライトは
**オプション**です——読み込まれていれば、オーケストレーターはそこに委譲し（より豊かで、より安価）、
なければインラインプロトコルが作業の100%をカバーします。アクセラレーターは**自動検出**されます——
あれば使われ、なければLLMフォールバックになります。

| # | 機能 | 取り込み元 | 何をするか | トークンへの影響 |
|---|---|---|---|---|
| 1 | 🔁 **simplicio-tasks** | — | オーケストレーターのループ：44個の拡張ポイント、デュアルパスルーター、自己監査による収束 | コア |
| 2 | ♾️ **simplicio-loop** | [ralph-loop](https://github.com/cursor/plugins/tree/main/ralph-loop) | 強化されたRalphループ：エビデンスゲートを通った `<promise>` 終了、max_iterations 上限 | ループの駆動 |
| 3 | 🧱 **simplicio-orient** | [rtk](https://github.com/rtk-ai/rtk) + [caveman](https://github.com/JuliusBrussee/caveman) | ターミナル優先の実行、出力削減カタログ、tee-cache、シグネチャ読み込み | L0 決定論的 |
| 4 | 🔥 **simplicio-review** | [thermos](https://github.com/cursor/plugins/tree/main/thermos) | 別々のルーブリックでの並列敵対的レビュー → 重複排除済み判定 | 品質ゲート |
| 5 | 🗜️ **simplicio-compress** | [caveman](https://github.com/JuliusBrussee/caveman) | 出力＋メモリの圧縮、フェイルクローズの `transform_guard` | 40〜60%削減 |
| 6 | 🎓 **simplicio-learn** | [teaching](https://github.com/cursor/plugins/tree/main/teaching) | 実行後の振り返り → 耐久性のある重複排除済みの教訓をメモリへ | 実行ごとに賢く |
| 7 | 🧭 **Understand Anything** | [Egonex-AI](https://github.com/Egonex-AI/Understand-Anything) | ナレッジグラフによるorient：セマンティック検索、ガイドツアー、依存グラフ | **L0 ゼロトークン** |
| 8 | 📊 **agentsview** | [kenn-io](https://github.com/kenn-io/agentsview) | セッション分析、コスト追跡、停滞セッションの発見 | **L1** SQLのみ |
| 9 | ⚡ **LMCache** | [LMCache](https://github.com/LMCache/LMCache) | ループターン間のKVキャッシュ — ローカルモデルでTTFTを40〜70%削減 | GPU時間 ↓ |
| 10 | 🗜️ **Simplicio capture engine** | `engine/simplicio_engine.py`（ネイティブ、stdlibのみ） | 透過的なキャプチャプロキシ：実プロバイダへ転送し、計測＋決定論的に圧縮し、`proxy_savings.json` を書き込む | **決定論的** |
| 11 | 🎬 **video_evidence** | Playwright（既定） · [hyperframes](https://github.com/heygen-com/hyperframes)（依頼時） | UI変更が動作することの動く証明として**実際のセッションを録画**する（Playwright）；動画こそが成果物のときは hyperframes で**決定論的なキャプション付きMP4**の解説動画をレンダリングする | エビデンスのプロデューサー |

各スキルは [`.claude/skills/`](../.claude/skills) 配下にあり、各アクセラレーターは
`.claude/skills/simplicio-tasks/references/` 配下にリファレンスドキュメントを持ちます（動画プロデューサー：
[`video-evidence.md`](../.claude/skills/simplicio-tasks/references/video-evidence.md)、ワーカー
[`scripts/video_evidence.py`](../scripts/video_evidence.py)）。

---

## 📡 Source adapters

オーケストレーターは、差し替え可能なアダプタを通して任意のソースから作業を発見します。各アダプタは
6つの動詞を公開します：`list_ready`、`get_details`、`claim`、`update_status`、`attach_evidence`、`close`。

| ソース | アダプタ | 目的 |
|---|---|---|
| GitHub Issues/PRs | `gh` CLI（ネイティブ） | 主要な作業項目ソース |
| Jira / Asana / ClickUp / Linear / Notion | host connector | ボード／プロジェクト管理 |
| Trello / Azure DevOps | `az boards` adapter | Azureの作業追跡 |
| **agentsview sessions** | `scripts/agentsview_adapter.py` | 停滞セッションの回復＋コスト可観測性 |
| Local files / CI queue | filesystem / CI API | 内部の作業追跡 |

各アダプタのリファレンスドキュメントは `.claude/skills/simplicio-tasks/references/` 配下を参照してください。

---

## 🌐 11 runtimes, one protocol

1つの汎用スキルコア＋1セットのフックが、あらゆるランタイムを駆動します。アダプタは薄い層です——
ランタイムに*どこでスキルを読み込むか*、*どうループを起動するか*、*どうネイティブの高速性に
バインドするか*を伝えるだけ。**スキルはランタイムを名指ししない。ランタイムがスキルを検出する。**

| ランタイム | スキルの読み込み | ループの駆動 | ネイティブバインド |
|---|---|---|---|
| **Claude Code** | `.claude/skills/` + plugin | `Stop` フック | MCP |
| **Codex** | `AGENTS.md` | self-paced | MCP / adapter |
| **VS Code (Copilot)** | `copilot-instructions.md` | tasks | MCP |
| **Cursor** | `.cursor-plugin/` | `stop`+`afterAgentResponse` | MCP / rules |
| **Antigravity** | rules / `AGENTS.md` | self-paced | MCP |
| **Kiro** | `.kiro/steering/` | specs | MCP |
| **OpenCode** | `AGENTS.md` | self-paced | MCP |
| **Gemini** | `GEMINI.md` | self-paced | MCP / adapter |
| **Aider** | `CONVENTIONS.md` | self-paced | —（LLMフォールバック） |
| **Hermes** | native recall | native loop | **native** |
| **OpenClaw** | plugin SDK | native scheduler | **native** |

約束はこうです。**同じプロトコル、同じゲート、同じ安全性を11すべてで——違うのは速度だけ。**
`orient_clamp.py`（トークンエコノミー）は配線ゼロであらゆるランタイムで動きます。
[`adapters/MATRIX.md`](../adapters/MATRIX.md) を参照してください。

---

## 🗺️ 全体フロー — 需要から提供まで

オーケストレーターが作用するすべてのレイヤーを順に——需要（issue、タスク、アサイン）を読むところから、
マージされエビデンスで裏付けられた成果を提供し、その後さらに作業を求めて24時間365日ループするところまで。

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

## 🔁 The loop

**エビデンスゲート付きループ**が中核の仕組みです。毎ターン同じゴールを再投入するので、エージェントは
自分の以前の作業を見られます。終了するのは次のいずれかのときだけです：

1. **エビデンスゲートを通った `<promise>`** — 約束を出すターンは、同時に具体的な証拠（合格した
   テスト、マージ済みPR、クローズ済み項目の再クエリ）も必ず携えていなければなりません。エビデンスの
   ない約束＝無視されます。
2. **`max_iterations` 上限** — 強制的な安全のバックストップ
3. **予算キルスイッチ** — 支出が `daily_usd_ceiling` に達するとループを停止します
4. **STOPシグナル** — `.orchestrator/STOP` またはチャネルコマンド

ターンの間、LMCache（利用可能な場合）はKV状態をキャッシュするので、再投入のプレフィルコストは
ほぼゼロになります。

### 🧠 Attempt memory + stall detector (anti-oscillation)

何も覚えていない再投入ループは振動します——Xを試し、失敗し、またXを試す——上限を使い切るまで。
simplicio-loop は**耐久性のあるランジャーナル**（`.orchestrator/loop/journal.jsonl`、追記専用：
`iteration · action · hypothesis · gate · error-fingerprint`）と**ストール検出器**
（[`scripts/loop_journal.py`](../scripts/loop_journal.py)、決定論的でモデル不要）を保持します：

- **エラーフィンガープリント** — 失敗したゲートの出力は、行番号、パス、hex/uuid、タイムスタンプ、
  実行時間を正規化して除いた安定したハッシュに縮約されるので、付随的なテキストが異なっても、ターンを
  またいで*同じ*バグが認識されます。
- **ストール＝同一フィンガープリントの失敗がK回連続**（既定 K=3）。フィンガープリントが変わって
  いればループは前進しており（PROGRESS）、同じものがK回続けばそれは空回りです（STALLED）。
- STALLED のとき、ループは同じゴールを再投入**しません**——避けるべき**行き止まりのアクション**を
  名指しし、**戦略を切り替える**か、フィンガープリント付きで**人間ゲートへエスカレートします**。
- `loop_journal.py resume` は毎ターンの冒頭で読まれるので、新しいプロセスは以前の試行を再導出せずに
  続行でき（真のresume）、既知の行き止まりを決して再試行しません。

```bash
loop_journal.py resume                       # what was tried + dead-ends to avoid
loop_journal.py record --iteration N --action "…" --gate fail --gate-output test.log
loop_journal.py stall --k 3 --exit-code      # PROGRESS → re-feed · STALLED → switch/escalate
```

---

## 🎬 Video evidence — 既定は Playwright、依頼時は hyperframes

ループは変更が動作することの証明として**デモ動画を生成**します——**2つのエンジン**があり、
`video_evidence` という1つの拡張ポイントを共有します（ワーカー
[`scripts/video_evidence.py`](../scripts/video_evidence.py)、契約
[`references/video-evidence.md`](../.claude/skills/simplicio-tasks/references/video-evidence.md)）：

1. **既定——通常のエビデンスフローは Playwright を使う。** UI変更の後、`video_evidence` は画面を
   駆動する**実際のブラウザセッションを録画**します（Playwright ネイティブ動画 → `.webm`、FFmpeg で
   → `.mp4`）——「コンパイルするだけでなく動作する」ことの最も強いレシート（Step 4b）であり、
   有効なエビデンスゲート付き `<promise>` です。

   ```bash
   python3 scripts/video_evidence.py verify --url http://localhost:3000/login \
       --name login-demo --expect "Sign in" --issue 42 [--upload --pr 42]
   ```

2. **依頼時——パーソナライズされた解説動画は hyperframes を使う。** 成果物そのものが動画のとき
   （「画面Xの解説動画を作って」）、オーケストレーターは `web_verify` のスクリーンショットから
   **決定論的でキャプション付きのスライドショー**を
   [**hyperframes**](https://github.com/heygen-com/hyperframes) でレンダリングします（HeyGen製——
   「同じ入力、同じフレーム、同じ出力」、CI再現可能、APIキー不要、ヘッドレスChrome＋FFmpegによる
   ローカルレンダリング）。

   ```text
   /simplicio-tasks make an explainer video of the system login screen
   → detect: video-creation request → web_verify captures the screens
   → video_evidence verify --engine hyperframes → deterministic MP4 → attached to the PR
   ```

いずれのエンジンでも、録画／レンダリングされなかった動画は **BLOCKED** をもたらし、決してフェイクで
通りません。エビデンスは常に**ファイルパス＋真偽の判定**です——動画のバイト列をコンテキストに入れる
ことは決してありません（トークンエコノミー）。

---

## 📊 Token economy

| 技法 | 節約 |
|---|---|
| `deterministic_edit`（L0） | 編集トークンの100%（ファイルは機械的に書かれ、LLMが書くことは決してない） |
| ターミナル優先の実行 | 事実はLLMの幻覚ではなくシェルから |
| 出力削減カタログ | コマンド種別ごとの上限（`CAP_ERRORS=20`、`CAP_WARNINGS=10`、`CAP_LIST=20`）— `orient_clamp.py` |
| 失敗時のTee+CCRキャッシュ | 失敗したコマンドを再実行しない——キャッシュされた出力を読む |
| シグネチャのみ読み込み | `simplicio-cli signatures <file>` — 870行のファイル → 65行（**93%節約**）、本体は省略 |
| `simplicio-compress` | 簡潔な散文＋一回限りのメモリコンパクション |
| `orient_clamp.py` | あらゆるシェルコマンドでクランプ＋tee、配線ゼロ |
| ネイティブレスポンスキャッシュ | 繰り返される決定論的（temp=0）リクエスト → キャッシュから提供し、LLM呼び出しをスキップ（**ヒット時100%**）— `simplicio-cli cache`、既定で有効（`SIMPLICIO_CACHE=0` で無効化） |
| Simplicioキャプチャプロキシ + MCP | 透過的な圧縮デーモンによりツール出力のトークンを60〜95%削減 |

節約は、検証で正しいと確認された結果に対してのみ加点されます。ベースライン＝同じ結果に至る、
最も安価で妥当なオーケストレーションなしの経路。**節約の報告はエビデンスゲート付きで、必須では
ありません：** 節約の数値は、ターンが実際に経済を生むコマンドを走らせ、その数値が計測された
レシート（clamp tee、signatures-read、cacheヒット、`deterministic_edit`、`savings_ledger`）に
たどれる場合にのみ表示されます。計測された経済がなければ節約の行はなく、オーケストレーターは
ベースラインやパーセンテージを決して捏造しません。`references/token-economy.md` を参照してください。

### 🔎 `simplicio-tasks` の実行：経済 vs 計測（ランタイムごと）

**`simplicio-tasks`** を呼ぶと2つの異なることが起き、それらはランタイムごとに異なる挙動をします：

- **経済** — 圧縮、出力クランプ、シグネチャのみ読み込み、`deterministic_edit` —— は、**スキルが
  走り `simplicio-orient` ／ `simplicio-compress` を読み込むたびに、どのランタイムでも**適用されます。
  これはスキルの挙動＋フックです（フックがあるところで最も強い：`orient_clamp.py` は Claude と Cursor
  で自動クランプし、それ以外では指示駆動）。
- **計測** — Token Monitor のライブ数値 — は、**キャプチャプロキシを通過する**トラフィックだけを
  数えます。

| ランタイム | 経済（スキル） | 計測（モニター） |
|---|---|---|
| **Hermes** | ✓ | ✓ **自動** — すでにプロキシ経由でルーティング済み（`base_url → :8788`） |
| **Claude** | ✓（スキル＋フック） | ✗ 既定では — Claude は `api.anthropic.com` と直接やり取り；ルーティングして初めて計測（`simplicio-cli wrap claude`、または `ANTHROPIC_BASE_URL → http://127.0.0.1:8788`） |
| **Codex** | ✓（スキル） | ✗ 既定では — `simplicio-cli init codex` は MCP ツールを追加するが LLM トラフィックはルーティングしない；`simplicio-cli wrap codex` またはプロキシを指す OpenAI base-url で計測 |

つまり、**節約はあらゆるランタイムで起きます**；**モニターは Hermes では自動的に集計し**、Claude/Codex
では**一度きりのルーティング手順**（`simplicio-cli wrap …` ／ base-url → `:8788`）の後に集計します。
ルーティングなしでも経済は適用されます——モニターがそのトークンを数えないだけです。
`scripts/simplicio-economy.sh wire` がインストール時に OpenAI 互換クライアント向けにこのルーティングを
行います。

### 📈 Simplicio Token Monitor

節約を、常時稼働でライブに見られるビューです：

- **Webダッシュボード** — `http://127.0.0.1:9090` — リアルタイムのトークンチャート、節約ゲージ、
  傍受しているLLM／ランタイムと **141/144プロバイダ（98%）**、そしてライブのプロキシログ。
- **メニューバー／トレイウィジェット** — システムトレイに節約したトークンをライブ表示（macOS rumps · Windows/Linux pystray）。
- **1つのモジュール** — `scripts/simplicio-economy.sh {status|up|wire}` がキャプチャプロキシ＋モニター＋
  トレイ＋`simplicio-dev-cli` 決定論的オペレーターを立ち上げ、スタック全体を報告します。

インストールは、`scripts/setup_simplicio.sh`、またはクロスプラットフォームの
`python3 scripts/install_services.py install` を介して、3つすべてを自動起動サービス（macOS launchd · Linux systemd · Windows Startup）として登録します。
インストール後、モニター＋キャプチャは**ループを起動せずに**動作します——`references/token-capture.md` を参照してください。

### 🛠️ The capture engine — one native module, every command

[`engine/simplicio_engine.py`](../engine/simplicio_engine.py) はネイティブのSimplicioキャプチャエンジンです
——**ネイティブ、stdlibのみ、フェイルオープンで、外部依存はありません**。任意の
コマンドを [`scripts/simplicio-engine`](../scripts/simplicio-engine) ラッパー経由で実行します（例：`simplicio-engine doctor`）：

| コマンド | 何をするか |
|---|---|
| `proxy` | 透過的なキャプチャプロキシ——各モデルを**実**プロバイダへルーティングし、圧縮＋計測＋キャッシュ（モデルの差し替えなし） |
| `doctor` | プロキシの到達性＋累計の節約 |
| `cache` | ネイティブレスポンスキャッシュ（`stats`/`clear`）——繰り返される決定論的リクエストはキャッシュから提供され、LLM呼び出しをスキップ |
| `signatures` | ソースファイルのシグネチャのみ表示（本体は省略、コードを読むトークンを約93%削減） |
| `semantic` | 可逆的な抽出型（セマンティックライト）圧縮 |
| `detect` | コンテンツタイプ検出＋ブロック単位のスマートルーティング |
| `rag` | CCRメモリストア上でのTF-IDF（または `--ml` 埋め込み）検索 |
| `memory` | CCR compress-cache-retrieve ストア（`remember`/`recall`/`forget`/`list`/`stats`） |
| `mcp` | ネイティブstdio MCPサーバー（compress / retrieve / stats ツール） |
| `init` / `wrap` | Simplicioをクライアント（Claude / Codex / Copilot / OpenClaw）へ登録 · キャプチャルーティング付きでクライアントを実行 |
| `report` / `audit` / `capture` / `evals` | 節約レポート · 圧縮機会のためのツリー監査 · リクエストのドライラン · 圧縮回帰ゲート |

---

## 🏛️ Design pillars (in detail)

オーケストレーションの力を担う仕組みは4つあります：

| 柱 | 焦点 | 所在 |
|---|---|---|
| **DAG＋パイプライン** | 依存関係による並列性、項目ごとに段階化 | `references/orchestration.md`（Step 3 プール＋パイプライン） |
| **Worktree分離** | ツリーを壊さない並列編集、マージゲート付き | `references/orchestration.md` |
| **敵対的検証** | 「提供」の前に懐疑者のパネル | `references/quality-safety-delivery.md` · スキル `simplicio-review` |
| **ループ予算上限** | 無限ループ防止、二重の出口 | `references/standing-loop-247.md` · スキル `simplicio-loop` |

---

## 🚀 Install & use

```bash
git clone https://github.com/wesleysimplicio/simplicio-loop
cd simplicio-loop

# install for your runtime (omit <runtime> to auto-detect)
bash scripts/install.sh <runtime> [--global]        # macOS / Linux
pwsh scripts/install.ps1 <runtime> [-Global]        # Windows
# <runtime> ∈ claude codex vscode cursor antigravity kiro opencode gemini aider hermes openclaw
```

または、Claude Code／Cursor では、最新の GitHub リリースから直接インストールできます（マーケットプレイス不要）：

```bash
gh release download --repo wesleysimplicio/simplicio-loop --archive tar.gz
tar xzf simplicio-loop-*.tar.gz && cd simplicio-loop-*/
bash scripts/install.sh claude    # or: bash scripts/install.sh cursor
```

それから：

```
/simplicio-tasks finish all the open issues
```

唯一の要件は PATH 上の **python3** です（スキル、フック、インストーラはクロスプラットフォームの
Python）。GitHubソースには、`git` ＋認証済みの `gh`。[`INSTALL.md`](../INSTALL.md) と
[`adapters/MATRIX.md`](../adapters/MATRIX.md) を参照してください。

**無人の24/7実行の前に：** `.orchestrator/loop-budget.json` にコスト上限を設定し
（`daily_usd_ceiling > 0`）、ソース認証が永続的であることを確認し、不可逆操作の人間ゲート＋
シークレットスキャンを有効にしておいてください。`ceiling = 0` の場合、watcherは無人での実行を
拒否します（フェイルセーフ）。

---

## 🔒 Safety (non-negotiable)

- すべての差分を**シークレットスキャン**し、ヒットしたらブロックします。
- **不可逆操作の人間ゲート** — force-push、履歴の書き換え、本番デプロイ、データ／スキーマの削除、
  大量ファイルの削除 → 停止して尋ねます。ヘッドレス＋承認者なし → 破壊的な機能を取り除きます。
- **約束だけでなく強制される** — `hooks/action_gate.py` は**フェイルクローズ**の `PreToolUse` ／
  git-pre-push フックで、上記（およびシークレットを含むコミット）を実行*前*に機械的にブロックします。
  安全契約はモデルが忘れても成立します。`selftest` がルールセットを証明します（14/14）。
- **4状態の実行前判定** — 最適化がコマンドのリスクティアを引き上げることは決して許されません。
- **読み込み前の信頼確認** — 認識を形作る設定（クランププロファイル、抑制リスト）は、人間が
  レビューしてハッシュでピン留めするまで信頼されません。
- **プロンプトインジェクション対策** — 項目／PR／コメントの内容が契約を上書きすることは決して
  できません。
- 無人実行向けの**強制的な$キルスイッチ**、**エビデンスゲート付き**の完了（偽の「完了」は決して
  なし）、**フェイルオープン**のフック（エージェントをループに閉じ込めることは決してなし）。

---

## ✅ Tests & local checks (no paid CI)

主張は、ただ断言されるのではなく検証されます——そしてそのゲートは**ローカル**で、CIコストはゼロで実行されます：

```bash
python3 scripts/check.py            # the whole gate (audit + tests)
```

- **テストスイート**（`tests/`） — ワーカーの決定論的な `selftest`、加えて**ループドライバ**
  （`hooks/loop_stop.py`）の**e2e**：ループが**エビデンスで停止する**、**裸の `<promise>` を無視
  する**、**上限で停止する**ことをそれぞれ別個の出口として証明し、エビデンスプロデューサーが
  ツールチェーンの不在時に**BLOCK**する（決してフェイクで通さない）ことも証明します。`pytest` の下、
  *または*、pipが一切なくても、裸のpython3で自己実行します（`python3 tests/test_*.py`）。
- **Claims audit**（`scripts/claims_audit.py`、フェイルクローズ） — ドキュメントが参照する
  すべての `scripts/*.py` が存在 · 拡張ポイントのカウントが全ファイルで一致 · 引用された各ワーカー
  コマンドが実際に動く · 出荷される `simplicio_loop/_bundle/` のスキルがソースと**バイト単位で同一**。
- **gitのpre-pushフックとして配線**して、`main` を無料で誠実に保ちます：
  ```bash
  printf '#!/bin/sh\npython3 scripts/check.py\n' > .git/hooks/pre-push && chmod +x .git/hooks/pre-push
  ```

`pip install "simplicio-loop[dev]"` はより見やすい出力のためにpytestを追加します；必須ではありません。

---

## 📄 License

MIT

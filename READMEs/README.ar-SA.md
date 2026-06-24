<div dir="rtl">

# 🔁 simplicio-loop — منسّق الذكاء الاصطناعي العالمي ذو الحلقة المتكررة

</div>

<p align="center">
  <img src="../assets/simplicio-loop-hero.jpg" alt="simplicio-loop" width="920" />
</p>

<p align="center">
  <a href="https://github.com/wesleysimplicio/simplicio-loop/stargazers"><img src="https://img.shields.io/github/stars/wesleysimplicio/simplicio-loop?style=social" alt="Stars"></a>
  <a href="#-المهارات-والمسرّعات-العشرة"><img src="https://img.shields.io/badge/skills-10-7C3AED" alt="10 skills"></a>
  <a href="#-محوّلات-المصادر"><img src="https://img.shields.io/badge/source%20adapters-5-00E08A" alt="5 source adapters"></a>
  <a href="#-11-بيئة-تشغيل-بروتوكول-واحد"><img src="https://img.shields.io/badge/runtimes-11-2563EB" alt="11 runtimes"></a>
  <a href="#-نقاط-التوسعة-الـ-43"><img src="https://img.shields.io/badge/extension%20points-43-00E08A" alt="43 extension points"></a>
  <a href="#-اقتصاد-الرموز"><img src="https://img.shields.io/badge/tokens-up%20to%2096%25%20fewer-green" alt="Up to 96% fewer tokens"></a>
  <a href="../LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue" alt="License"></a>
</p>

<p align="center">
  <a href="#-الخلاصة">الخلاصة</a> ·
  <a href="#-المهارات-والمسرّعات-العشرة">10 مهارات</a> ·
  <a href="#-محوّلات-المصادر">محوّلات المصادر</a> ·
  <a href="#-11-بيئة-تشغيل-بروتوكول-واحد">11 بيئة تشغيل</a> ·
  <a href="#-الحلقة">الحلقة</a> ·
  <a href="#-اقتصاد-الرموز">اقتصاد الرموز</a> ·
  <a href="#-اقتصاد-الرموز">محرّك الالتقاط</a> ·
  <a href="#-التثبيت-والاستخدام">التثبيت</a>
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

<div dir="rtl">

## ⚡ الخلاصة

**simplicio-loop** هو **سوبر-بلجن** مستقلّ عن بيئة التشغيل — منسّق واحد ذاتي الحركة يعمل
بحلقة متكررة (يُستدعى بوصفه **`/simplicio-tasks`**)، إضافةً إلى **خمس مهارات تابعة** — يحوّل أي
نموذج لغوي قوي (Claude أو Codex أو Copilot أو Gemini أو Cursor أو النماذج المحلية) إلى عاملٍ
ذاتي القيادة. توجّهه نحو مجموعة من الأعمال — *"أنهِ كل القضايا المفتوحة"*، *"أفرغ طابور الـ CI"*،
*"صفّ لوحة Jira"* — وهو يدير دورة الحياة كاملةً بنفسه:

> **اكتشف ← افهم ← قرّر ← نفّذ ← تحقّق ← صحّح ← سجّل ← كرّر**

يكتشف الأعمال من أي مصدر (GitHub Issues وJira وAzure DevOps وجلسات agentsview وغيرها)،
ويزيل التكرارات، ويوسّع تلقائياً أسطولاً من الوكلاء بما يناسب جهازك، ثم ينفّذ كل عنصر عبر حلقة
جودة **تُشغّل الشيفرة (لا تكتفي بتصريفها)**، ويفتح طلبات الدمج، ويعالج ملاحظات الـ CI والمراجعة،
ويدمج، ويواصل المراقبة **على مدار الساعة طوال أيام الأسبوع** بحثاً عن أعمال جديدة — كل ذلك خلف
بوابات أمان ومفتاح إيقاف صارم للتكلفة.

</div>

```text
/simplicio-tasks termine as issues abertas
→ identity + pre-flight (kill-switch, auth, watcher)
→ discover 50 issues · dedup · build dependency DAG
→ autoscale fleet = 14 · pipeline implement→review→merge
→ each item: read body+ACs → orient code → plan → edit → run → verify → PR
→ merge · close with evidence · rollback if main breaks
→ keep looping every ~2 min until the queue is dry (evidence-gated, never a false "done")
```

<div dir="rtl">

ثلاثة أمور تجعله مختلفاً: فهو **سوبر-بلجن من مهارات مركّزة**، ويشغّل **البروتوكول نفسه على 11
بيئة تشغيل**، ويفعل كل ذلك بـ**اقتصاد رموز جريء وصادق**.

</div>

---

<div dir="rtl">

## 🧠 المهارات والمسرّعات العشرة

نواة المنسّق + خمسة توابع + أربعة مسرّعات. كل تابع **اختياري** — فعند تحميله يفوّض إليه المنسّق
(أغنى + أرخص)؛ وعند غيابه يغطّي البروتوكول المضمّن 100%. والمسرّعات **تُكتشف تلقائياً** —
موجودة = تُستخدَم، غائبة = بديل احتياطي بالنموذج اللغوي.

| # | القدرة | ماذا تستوعب | ماذا تفعل | أثر الرموز |
|---|---|---|---|---|
| 1 | 🔁 **simplicio-tasks** | — | حلقة المنسّق: 43 نقطة توسعة، موجِّه ثنائي المسار، تقارب بالتدقيق الذاتي | النواة |
| 2 | ♾️ **simplicio-loop** | [ralph-loop](https://github.com/cursor/plugins/tree/main/ralph-loop) | حلقة Ralph المُتينة: خروج بـ`<promise>` مرتبط بالأدلة، سقف max_iterations | محرّك الحلقة |
| 3 | 🧱 **simplicio-orient** | [rtk](https://github.com/rtk-ai/rtk) + [caveman](https://github.com/JuliusBrussee/caveman) | تنفيذ مُوجَّه نحو الطرفية أولاً، كتالوج تقليل المُخرَجات، tee-cache، قراءة signatures | L0 حتميّ |
| 4 | 🔥 **simplicio-review** | [thermos](https://github.com/cursor/plugins/tree/main/thermos) | مراجعة تخاصمية متوازية على معايير متمايزة ← حُكم واحد بلا تكرار | بوابة الجودة |
| 5 | 🗜️ **simplicio-compress** | [caveman](https://github.com/JuliusBrussee/caveman) | ضغط المُخرَجات + الذاكرة، `transform_guard` يفشل آمناً | 40-60% أقل |
| 6 | 🎓 **simplicio-learn** | [teaching](https://github.com/cursor/plugins/tree/main/teaching) | مراجعة استرجاعية بعد التشغيل ← دروس مُتينة بلا تكرار في الذاكرة | أذكى في كل تشغيل |
| 7 | 🧭 **Understand Anything** | [Egonex-AI](https://github.com/Egonex-AI/Understand-Anything) | توجيه برسم بياني معرفي: بحث دلالي، جولات موجَّهة، رسم بياني للتبعيات | **L0 بلا رموز** |
| 8 | 📊 **agentsview** | [kenn-io](https://github.com/kenn-io/agentsview) | تحليلات الجلسات، تتبّع التكلفة، اكتشاف الجلسات المتوقّفة | **L1** SQL فقط |
| 9 | ⚡ **LMCache** | [LMCache](https://github.com/LMCache/LMCache) | تخزين KV بين دورات الحلقة — تقليل TTFT بنسبة 40-70% على النماذج المحلية | وقت GPU ↓ |
| 10 | 🗜️ **محرّك التقاط Simplicio** | `engine/simplicio_engine.py` (أصيل، مكتبة معيارية فقط؛ مخطّط التوفير متوافق مع مشروع [headroom](https://github.com/headroomlabs-ai/headroom) مفتوح المصدر) | وكيل التقاط شفّاف: يمرّر إلى المزوّد الحقيقي، يقيس + يضغط حتميّاً، يكتب `proxy_savings.json` | **حتميّ** |

كل مهارة تقيم ضمن [`.claude/skills/`](../.claude/skills)؛ ولكل مسرّع مستند مرجعي ضمن
`.claude/skills/simplicio-tasks/references/`.

</div>

---

<div dir="rtl">

## 📡 محوّلات المصادر

يكتشف المنسّق الأعمال من أي مصدر عبر محوّلات قابلة للتركيب. ويُظهِر كلٌّ منها ستة أفعال:
`list_ready` و`get_details` و`claim` و`update_status` و`attach_evidence` و`close`.

| المصدر | المحوّل | الغرض |
|---|---|---|
| GitHub Issues/PRs | `gh` CLI (أصيل) | المصدر الأساسي لعناصر العمل |
| Jira / Asana / ClickUp / Linear / Notion | موصِّل المضيف | إدارة اللوحات/المشاريع |
| Trello / Azure DevOps | محوّل `az boards` | تتبّع أعمال Azure |
| **جلسات agentsview** | `scripts/agentsview_adapter.py` | استرداد الجلسات المتوقّفة + رصد التكلفة |
| الملفات المحلية / طابور CI | نظام الملفات / واجهة CI | تتبّع العمل الداخلي |

راجع المستند المرجعي لكل محوّل ضمن `.claude/skills/simplicio-tasks/references/`.

</div>

|---

<div dir="rtl">

## 🌐 11 بيئة تشغيل، بروتوكول واحد

نواة مهارة عالمية واحدة + مجموعة خطّافات واحدة تقود كل بيئة تشغيل. والمحوّل رفيع: فهو يخبر بيئة
التشغيل *أين تحمّل المهارات*، و*كيف تسلّح الحلقة*، و*كيف تربط السرعة الأصيلة*. **المهارة لا
تسمّي أي بيئة تشغيل؛ بل بيئة التشغيل هي التي تكتشف المهارة.**

| بيئة التشغيل | تحميل المهارة | قيادة الحلقة | الربط الأصيل |
|---|---|---|---|
| **Claude Code** | `.claude/skills/` + plugin | `Stop` hook | MCP |
| **Codex** | `AGENTS.md` | self-paced | MCP / adapter |
| **VS Code (Copilot)** | `copilot-instructions.md` | tasks | MCP |
| **Cursor** | `.cursor-plugin/` | `stop`+`afterAgentResponse` | MCP / rules |
| **Antigravity** | rules / `AGENTS.md` | self-paced | MCP |
| **Kiro** | `.kiro/steering/` | specs | MCP |
| **OpenCode** | `AGENTS.md` | self-paced | MCP |
| **Gemini** | `GEMINI.md` | self-paced | MCP / adapter |
| **Aider** | `CONVENTIONS.md` | self-paced | — (LLM fallback) |
| **Hermes** | native recall | native loop | **native** |
| **OpenClaw** | plugin SDK | native scheduler | **native** |

الوعد: **البروتوكول نفسه، والبوابات نفسها، والأمان نفسه على كل البيئات الإحدى عشرة — لا يختلف
إلا السرعة.** ويعمل `orient_clamp.py` (اقتصاد الرموز) على كل بيئة تشغيل دون أي توصيل. راجع
[`adapters/MATRIX.md`](../adapters/MATRIX.md).

</div>

---

<div dir="rtl">

## 🗺️ المسار الكامل — من الطلب إلى التسليم

كل طبقة يعمل عليها المنسّق، بالترتيب — من قراءة الطلب (issues وtasks وassigns) إلى تسليم عملٍ
مدموج ومدعوم بالأدلة، ثم التكرار على مدار الساعة طلباً للمزيد.

</div>

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

<div dir="rtl">

## 🔁 الحلقة

**الحلقة المرتبطة بالأدلة** هي الآلية الأساسية. فهي تُعيد تغذية الهدف نفسه في كل دورة كي يرى
الوكيل عمله السابق. والخروج لا يكون **إلا** عبر:

1. **`<promise>` مرتبط بالأدلة** — فالدورة التي تنبعث منها الوعد **يجب** أن تحمل أيضاً برهاناً
   ملموساً (اختبار ناجح، طلب دمج مدموج، إعادة استعلام عن عنصر مُغلق). والوعد بلا أدلة = يُتجاهَل.
2. **سقف `max_iterations`** — حاجز أمان صارم
3. **مفتاح إيقاف الميزانية** — `daily_usd_ceiling` يوقف الحلقة عند استنفاد الإنفاق
4. **إشارة STOP** — `.orchestrator/STOP` أو أمر القناة

بين الدورات، يخزّن LMCache (حين يتوفّر) حالة KV فتقترب كلفة إعادة التغذية من صفر prefill.

---

## 📊 اقتصاد الرموز

| التقنية | التوفير |
|---|---|
| `deterministic_edit` (L0) | 100% من رموز التحرير (الملف يُكتب آلياً، لا بالنموذج اللغوي أبداً) |
| التنفيذ المُوجَّه نحو الطرفية أولاً | حقائق من الصدفة، لا هلوسة النموذج اللغوي |
| كتالوج تقليل المُخرَجات | سقوف لكل نوع أمر (`CAP_ERRORS=20`، `CAP_WARNINGS=10`، `CAP_LIST=20`) — `orient_clamp.py` |
| tee+CCR cache عند الفشل | لا تُعِد تشغيل أمرٍ فاشل أبداً — اقرأ المُخرَجات المخزّنة |
| قراءات signatures-only | `simplicio signatures <file>` — ملف من 870 سطراً ← 65 سطراً (**93% توفير**)، المتون محذوفة |
| `simplicio-compress` | نثر مقتضب + ضغط ذاكرة لمرة واحدة |
| `orient_clamp.py` | تقليم + tee على كل أمر صدفة، بلا توصيل |
| Native response cache | طلب حتميّ متكرّر (temp=0) ← يُخدَم من الذاكرة المؤقتة، يتخطّى استدعاء النموذج اللغوي (**100% عند الإصابة**) — `simplicio cache`، مُفعَّل افتراضياً (`SIMPLICIO_CACHE=0` للتعطيل) |
| وكيل التقاط Simplicio + MCP | 60-95% رموز أقل على مُخرَجات الأدوات عبر خادم ضغط شفّاف |

التوفير لا يُحتسب إلا عند نتيجة مُتحقَّق من صحتها. والأساس المرجعي = أرخص مسار معقول غير مُنسَّق
يبلغ النتيجة نفسها. راجع `references/token-economy.md`.

### 📈 مراقب رموز Simplicio (Simplicio Token Monitor)

عرض حيّ ودائم التشغيل للتوفير:

- **لوحة معلومات على الويب** — `http://127.0.0.1:9090` — مخطّط رموز آنيّ، مقياس توفير، النماذج
  اللغوية/بيئات التشغيل و**141/144 مزوّداً (98%)** نعترضها، وسجلّ وكيل حيّ.
- **عنصر شريط القوائم / الصينية** — الرموز المُوفَّرة آنياً في صينية النظام (macOS rumps · Windows/Linux pystray).
- **وحدة واحدة** — `scripts/simplicio-economy.sh {status|up|wire}` يشغّل وكيل الالتقاط + المراقب +
  الصينية + المُشغِّل الحتميّ `simplicio-dev-cli` ويُبلِّغ عن المنظومة كاملةً.

يسجّل التثبيت الثلاثة جميعاً كخدمات ذاتية البدء (macOS launchd · Linux systemd · Windows Startup) عبر
`scripts/setup_simplicio.sh`، أو عبر المتعدّد المنصات `python3 scripts/install_services.py install`. وبعد
التثبيت يعمل المراقب + الالتقاط **دون استدعاء الحلقة** — راجع `references/token-capture.md`.

### 🛠️ محرّك الالتقاط — وحدة أصيلة واحدة، لكل أمر

[`engine/simplicio_engine.py`](../engine/simplicio_engine.py) هو محرّك التقاط Simplicio الأصيل
(مكتبة معيارية فقط، fail-open) — **إعادة تنفيذ كاملة لسطح
[headroom](https://github.com/headroomlabs-ai/headroom) الأصلي دون أي تبعية خارجية**. شغّل أي
أمر عبر غلاف [`scripts/simplicio-engine`](../scripts/simplicio-engine) (مثلاً `simplicio-engine doctor`):

| الأمر | ماذا يفعل |
|---|---|
| `proxy` | وكيل الالتقاط الشفّاف — يوجّه كل نموذج إلى مزوّده **الحقيقي**، يضغط + يقيس + يخزّن مؤقتاً (بلا استبدال نموذج) |
| `doctor` | إمكانية الوصول إلى الوكيل + التوفير مدى العمر |
| `cache` | ذاكرة الاستجابة الأصيلة (`stats`/`clear`) — الطلب الحتميّ المتكرّر يُخدَم من الذاكرة، فيتخطّى استدعاء النموذج اللغوي |
| `signatures` | عرض signatures-only لملف مصدري (المتون محذوفة، ~93% رموز أقل لقراءة الشيفرة) |
| `semantic` | ضغط استخراجي قابل للعكس (دلالي خفيف) |
| `kompress` | تقليم رموز دلاليّ بـ**ONNX** عبر نموذج `kompress-v2-base` الحقيقي |
| `detect` | اكتشاف نوع المحتوى + توجيه ذكيّ لكل كتلة |
| `rag` | استرجاع TF-IDF (أو تضمين `--ml`) فوق مخزن ذاكرة CCR |
| `memory` | مخزن CCR للضغط-التخزين-الاسترجاع (`remember`/`recall`/`forget`/`list`/`stats`) |
| `mcp` | خادم MCP أصيل عبر stdio (أدوات compress / retrieve / stats) |
| `init` / `wrap` | تسجيل Simplicio في عميل (Claude / Codex / Copilot / OpenClaw) · تشغيل عميل بتوجيه الالتقاط |
| `report` / `audit` / `capture` / `evals` | تقرير التوفير · تدقيق شجرة بحثاً عن فرصة ضغط · تجربة طلب على الجاف · بوابة انحدار الضغط |

### 🧠 نماذج ML حقيقية اختيارية — `pip install "simplicio-loop[onnx]"`

أربعة نماذج ONNX **حقيقية** عامة (Apache-2.0) تعمل أصيلاً — النماذج نفسها التي يستخدمها الأصل.
وبدون الإضافة، يغطّي مسار المكتبة المعيارية الحتميّ كل شيء؛ والنماذج تُنزَّل عند أول استخدام.

| النموذج | الأمر | الاستخدام |
|---|---|---|
| `kompress-v2-base` | `simplicio kompress` | تقليم رموز دلاليّ |
| `technique-router-onnx` | `simplicio router` | توجيه التقنيات |
| `all-MiniLM-L6-v2-onnx` | `simplicio embed` · `rag --ml` | تضمينات + RAG دلاليّ |
| `siglip-image-encoder-onnx` | `simplicio image` | مُتحقِّق محتوى ضغط الصور |

### ⚙️ نواة أداء أصيلة بلغة Rust (اختيارية)

تشحن [`rust/`](../rust) أربع حُزَم (crates) منقولة + مُعاد تسميتها من الأصل (Apache-2.0؛ ملف `NOTICE` يعترف به):
`simplicio-core` (الضواغط + smart-crusher)، `simplicio-py` (روابط PyO3)، `simplicio-proxy`
(وكيل عكسيّ axum)، `simplicio-parity` (مِحَكّ تكافؤ Rust↔Python). ابنِها بـ`maturin` — يعمل محرّك
Python بالكامل دونها؛ والحُزَم تضيف السرعة الأصيلة فحسب.

</div>

|---

<div dir="rtl">

## 🏛️ ركائز التصميم (بالتفصيل)

تحمل أربع آليات قوة التنسيق:

| الركيزة | المحور | أين تقيم |
|---|---|---|
| **DAG + خط الأنابيب** | التوازي بحسب التبعية، مُدرَّج لكل عنصر | `references/orchestration.md` (Step 3 pool + pipeline) |
| **عزل Worktree** | تعديلات متوازية دون إفساد الشجرة، مُحكَمة بالدمج | `references/orchestration.md` |
| **التحقق التخاصمي** | لجنة من المتشكّكين قبل "التسليم" | `references/quality-safety-delivery.md` · skill `simplicio-review` |
| **سقف ميزانية الحلقة** | مانع للحلقة اللانهائية، بمخرجين | `references/standing-loop-247.md` · skill `simplicio-loop` |

---

## 🚀 التثبيت والاستخدام

</div>

```bash
git clone https://github.com/wesleysimplicio/simplicio-loop
cd simplicio-loop

# install for your runtime (omit <runtime> to auto-detect)
bash scripts/install.sh <runtime> [--global]        # macOS / Linux
pwsh scripts/install.ps1 <runtime> [-Global]        # Windows
# <runtime> ∈ claude codex vscode cursor antigravity kiro opencode gemini aider hermes openclaw
```

<div dir="rtl">

أو، على Claude Code / Cursor، أضِفه كملحق من السوق:

</div>

```
/plugin marketplace add wesleysimplicio/simplicio-loop
/plugin install simplicio-loop@simplicio
```

<div dir="rtl">

ثم:

</div>

```
/simplicio-tasks finish all the open issues
```

<div dir="rtl">

المتطلّب الوحيد هو **python3** على PATH (المهارات والخطّافات والمثبِّت كلها بلغة Python متعددة
المنصات). أما لمصادر GitHub فيلزم `git` + أداة `gh` موثَّقة الدخول. راجع [`INSTALL.md`](../INSTALL.md)
و[`adapters/MATRIX.md`](../adapters/MATRIX.md).

**قبل تشغيل غير مُشرَف عليه على مدار الساعة:** اضبط سقفاً للتكلفة في
`.orchestrator/loop-budget.json` (`daily_usd_ceiling > 0`)، وتأكّد من أن مصادقة المصدر دائمة،
وأبقِ البوابة البشرية للعمليات غير القابلة للتراجع + فحص الأسرار مُفعَّلين. فإذا كان `ceiling = 0`
رفض المراقب التشغيل دون إشراف (فشل آمن).

---

## 🔒 الأمان (غير قابل للتفاوض)

- **فحص الأسرار** لكل فرق (diff)؛ والإيقاف عند أي إصابة.
- **بوابة بشرية للعمليات غير القابلة للتراجع** — الدفع القسري، إعادة كتابة التاريخ، النشر
  للإنتاج، حذف البيانات/المخطّط، حذف الملفات الجماعي ← توقّف واسأل. وفي وضع بلا واجهة + بلا
  مُوافِق ← أزِل القدرة المدمِّرة.
- **حُكم رباعي الحالات قبل التنفيذ** — لا يجوز للتحسين أبداً أن يرفع فئة خطورة أمرٍ ما.
- **ثقة قبل التحميل** — الإعداد المُشكِّل للإدراك (ملفات التقليم، قوائم الكتم) غير موثوق حتى
  يراجعه إنسان ويثبّته بالبصمة (hash-pin).
- **تحصين ضد حقن التعليمات** — لا يمكن لمحتوى العنصر/طلب الدمج/التعليق أن يتجاوز العقد أبداً.
- **مفتاح إيقاف صارم بالدولار** للتشغيلات دون إشراف؛ وإنجاز **مرتبط بالأدلة** (لا "done" زائف
  أبداً)؛ وخطّافات **fail-open** (لا تحبس الوكيل في حلقة أبداً).

---

## 📄 الترخيص

MIT

</div>

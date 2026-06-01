# Architecture

## 1. 定位

我们做"专家决策规约工厂",不做"某领域专家数字人"。

**为什么不直接做数字人**: 业界现有数字人蒸馏(Character.ai / 心识宇宙 / 商汤如影 / 各家企业 KMS+LLM)有 5 个公开卡点:
1. 数据贫瘠 + 内隐知识不在文本里
2. 评测无法闭环
3. 冷启动悖论(语料 → 配合 → 价值 → 效果死循环)
4. 长尾覆盖反向(蒸馏样本偏高频,但价值在长尾)
5. ROI 单点崩塌(一个数字人 = 一个具体专家,复用率为 0,本质是定制)

**我们对前 4 条的回应**:
- 内隐知识 → 在权衡发生**当下**触发追问,生产新语料而非挖掘旧语料
- 评测 → 让规约可被同岗位人 review,有人在 loop 闭环
- 冷启动 → 触发式不需要先攒一堆语料,渐进产出
- 长尾覆盖 → 触发器本身就抓"非典型/争议/反复"时刻,长尾是高优先级

**对第 5 条**: 这是我们最大风险,**复用率 ≥ 70% 是产品 vs 定制的分水岭**,Month 3 必须验证。

---

## 2. 知识化 — 5 层变换

专家给到的毛坯长这样: "今天 A 工单没走加急。提单部门历史上误报率高;现在不是季度末,没真紧迫。"

这不是知识,是**带语境的判断日记**。要变成知识必须经过 5 层:

| 层 | 形态 | 谁做 |
|---|---|---|
| **L0** | 毛坯判断 / 文档 / 任意自然语言 | 专家产出 |
| **L1** | **抽成 0..N 条知识原子**,每条归到 5 类之一:`decision`(决策) / `fact`(决策性事实) / `case`(案例 / 反例) / `concept`(核心概念) / `relation`(实体关系) | 产品自动 |
| **L2** | **聚类成模式**:同类原子聚出候选(decision → spec / 同 slug 的 fact / case / concept / relation 累加 mention) | 产品自动 + 专家点头 |
| **L3** | **边界标注**:为每条规则补上反例和不适用条件 | **产品主动追问** + 专家答 |
| **L4** | **可执行化**:编译为 agent 可直接消费的知识库(specs/ + facts/ + cases/ + ontology/) | 产品自动 |
| **L5** | **回流**:agent 跑历史 case → 不一致点 → 回流到 L2/L3 | 产品自动 + 专家 review |

> L1 不预设"一段聊天 1 条 / 一篇 SOP N 条"。文本里有多少值得沉淀的原子,就抽多少;混合多 type 也合法。
> 决策五元组 `{情境, 信号, 权衡, 决策, 原因}` 现在只是 `decision` 这一种 type 的字段,不再代表 L1 的全部。

**L3 是产品灵魂**。其他几层是格式转换/聚类/编译/评测,基础设施层,别家也能建。L3"知道怎么逼专家把内隐边界外化"才是有积累价值的产品 IP——它需要对**蒸馏过程本身**的认知积累。

L1/L2/L4/L5 是地基。L3 是护城河。

---

## 3. 5 个 Surface(用户面)

> §3.6 / §3.7 / §3.8 是 Surface 4 内部的扩展模块(routing / memory / ACL),不是新 surface。surface 总数仍是 5。

### Surface 1 — Ingest(扔东西)

| 姿势 | 入口 | 处理 |
|---|---|---|
| 一句话判断 | IM @bot | L1 结构化 → 进 Inbox 等专家确认 |
| 一段语音 | IM 语音 | ASR → L1 → Inbox |
| 几篇文档 | IM 转发 | 切块抽取候选 → 进 Inbox 批量 review |
| 群被动 listen | IM | 默认收下 raw input,**不调 LLM 处理**(成本太重),@bot 或转发才走 LLM |

### Surface 2 — Inbox(我的待办)

专家每周 30 分钟来这里清队列。**4 类待办,按优先级排**:

```
🔴 冲突       ← 检测到新输入和已有规则矛盾,必须裁决
🟡 边界追问   ← 产品想知道某条规则的反例边界
🟢 归类确认   ← 产品猜了归类,等专家点头/纠正
⚪ 新候选    ← 从文档抽的候选规则,可批量过
```

**这个 Inbox 是产品的呼吸节奏**。专家每周清不完 → 产品太烦 → 调追问/检测阈值。

### Surface 3 — Browser(知识库)

像 Obsidian——一个**可读的 MD 文件树**(就是 git repo 本身):

```
ontology/
  ├── entities/        ← 自动建模出来的实体
  └── relationships/

specs/                 ← 决策规约
facts/                 ← 抽出的事实
cases/                 ← 历史 case
```

每个文件是 MD + frontmatter,**人和 LLM 都能读**。文件结构详见 §6。

### Surface 4 — Ask(问数字人)

最朴素的对话框(IM @bot)。回答**不是 RAG 的"找几段贴一起"**,而是**结构化推理 + 不确定性自标 + 引用**:

```
Q: A 部门提了个加急工单,要不要走加急?

A: 倾向不走加急。依据:
   • 规约 #SC-014: 提单部门误报率 < 30% AND 业务紧急期 才走加急
   • 当前: A 部门误报率 47% (#FACT-082) / 不在季度末 (#FACT-019)

不确定性: 中等。规约来自 8 条判断,有 1 条反例(#CASE-203)
建议: 升级专家。要替专家答,推荐"不加急"。
```

关键不是答案对,是**可审计 + 不确定性显式 + 会主动让位**。

**Procedural memory 拼到 SYSTEM_PROMPT**: ask 在答题前会查命中 entity 的 `memories` 表(M5),把活的 directive 拼进 SYSTEM_PROMPT 末尾的 `## 用户偏好` 段(不进检索结果区)。这是"用户教 bot 答题口径"的注入点 — 详见 [`bot/helper/memory/lookup.py`](../bot/helper/memory/lookup.py) 与 §3.7。

**Bot-routing 分支(M6)**: ask 答题前先 LLM judge 当前问题是否命中"涉及 X 去问外部 bot Y"这类 directive;命中则产出 `RouteRequest`(而非 `Answer`),走 [`bot/helper/im/bot_routing.py`](../bot/helper/im/bot_routing.py) 把问题代发给外部 bot,等回执后回贴并加 `@asker 已咨询 @via:` 前缀,原消息 card / rich_text 视觉保真透传。详见 §3.6。

### Surface 5 — Conflict(冲突解决)

专家 A 提的判断和已有(任意类型)知识不一致 → 触发冲突流。3 类:

| 类型 | 例子 | 处理 |
|---|---|---|
| 纯矛盾 | A: 永远 X / B: 永远不 X | 拉群,必须裁决,记录裁决理由 |
| 边界分歧 | A: X 适用 S1 / B: X 适用 S2 | 产品先猜可能不冲突,只是 scope 没对齐 |
| 权威等级 | 同域 A 是 owner / B 是 consultant | A 默认赢,B 视角作为反例附注 |

每次冲突解决产生 [[conflict-resolution-log]],本身高价值。

### 信息修正统一路径(5 类原子 + procedural memory 全打)

> 所有修正都走同一条流水线 — **不区分 fact/case/concept/relation/decision/memory**。
> 任意类型的"新输入和既有不一致"都是冲突,都进 inbox,都由 owner 在
> 「采纳 / 保留 / 都留」三选项里裁决。

```
新 raw → L1 五类抽取 + memory_extract(并行) → 落候选(fact / case / concept / relation / spec_candidate / memory)
                              ↓
            sink._run_consumers 自动跑 conflict.detector
                              ↓
        ┌─────────────────────┴─────────────────────┐
   decision  fact  case  concept  relation  memory
   ─────    ─────  ────  ─────    ────────  ─────
       全部统一走 LLM judge(M6 收口,commit f0ac08a)
       judge 输出: contradicts / scope_diff / dup / orthogonal
       支持 auto_superseded / auto_rejected / auto_coexist
        └─────────────────────┬─────────────────────┘
                              ↓
                      conflict_log 一张表
                  (target_type, target_slug, ...)
                              ↓
              inbox 周报 / `/inbox` 主动触发
                              ↓
      owner 回「采纳 2-N」/「保留 2-N」/「都留 2-N」
                              ↓
   resolve(): superseded → 候选打 superseded_at + git 删 .md + 重建 bundle
              rejected   → 仅记录,既有不动
              coexist    → 仅记录,标注并存
```

落地纪律:
- **conflict_log 不分表** — `target_type ∈ {spec, fact, case, concept, relation, memory}` 字段决定后续处置
- **5 类 + memory 全过 LLM judge**(M6,commit f0ac08a/15ff894):权威/newest-wins/coexist 可自动落定,judge 模型 sonnet
- **superseded 走软删** — 候选行打 `superseded_at` + `superseded_by(raw_id)`,
  retrieve 自动过滤;旧候选审计仍可查
- **bundle 重建是 resolve 的一部分** — 修正完立刻 build_bundle(),agent 下一秒
  看到的就是新版,不等周报 / 不等 cron
- **手动节奏不变** — 自动只产「待裁决」(自动 resolve 也只对低风险冲突,owner 仍可回滚)

### 3.6 Bot-routing(M6)— helper 当个分诊台

helper 不必啥都自己答。命中 procedural memory 里"涉及 X 类问题去问 @Y"这类 directive 时,helper 自动:

1. 私聊外部 bot Y 把问题代发过去(rich_text @ 它)
2. 在原会话(群 / 私聊)发"思考中"卡片占位
3. 收到 Y 的回执后,**前缀** `@asker 已咨询 @Y:` + **原样透传** Y 的卡片 / 富文本(视觉保真)
4. 5 分钟没回 → 卡片更新为"@Y 5 分钟没回,你直接 @ 它再问一次"

落到表 `pending_routings`(target_app_id / via_label / original_chat_id / original_wave_msg_id / tracker_card_msg_id / consumed_at / expired_at)。
**关键边界**:外部 bot 私聊回 helper 的消息**不落 raw_inputs**(避免 cli_xxx 的回执变成"哥的语料")。

### 3.7 Procedural Memory(M5)— 用户教 bot 答题口径

- 现有 5 类原子(decision/fact/case/concept/relation)全是描述客观世界的 **semantic memory**。
- M5 新增 `memories` 表 — **procedural memory**:用户对 bot 行为的指令,如"答哥相关的问题别每次复述身份"、"我喜欢简洁回答"。
- 抽取与 L1 解耦:`bot/helper/memory/extract.py` 用 LLM 按语义判断"是描述世界,还是约束 bot 行为/口径";不靠关键词。
- chat_context 注入:memory_extract 在长路径默认拼最近 16 条 / 1 天历史对话(对齐 ask),解决代词 scope 解析。
- 命中路径:ask 答题前查命中 entity 的活 directive,拼进 SYSTEM_PROMPT 末尾 `## 用户偏好` 段。
- 全公司共享 — `memories` 表无 owner 维度,任何用户教的指令对全员的 ask 都生效。
- **memory 软,ACL 硬** — memory 拼 prompt(LLM 可绕),硬访问控制走 §3.8。memory 管"怎么答",ACL 管"谁能看到"。

### 3.8 Topic ACL(M8)— 话题级硬访问控制

procedural memory 是 prompt 层软指令,会被反诘 / 换皮关键词绕过。涉及隐私 / 花名 / 私下评价的话题需要硬访问控制 — LLM 看不到原文,无从泄漏。

**设计原则**:
- 不靠关键词穷举(隐喻无法列举完全) — 内容入库时 LLM 语义判定 topic → `acl_topic_id` 落值;5 类候选表冗余继承,retrieve 出口纯列过滤,O(1) 不 join。
- 白名单写在 `bot/helper/policy/defaults/topic_acl.yaml` — **ACL 是系统策略,不进 spec repo**(spec repo 只装领域知识)。改 ACL = 改 helper 仓库 + 重启,跟改 SYSTEM_PROMPT 同等待遇。

**4 道闸**(数据流防漏 3 道 + 模型幻觉兜底 1 道,任一命中即生效)。具体位置 / 顺序 / 行为见 [`runtime.md` §2.7](runtime.md)。

代码: `bot/helper/acl/`(policy + tagger)。

---

## 4. 内部模块

```
┌──────────────────────────────────────────────────────────────┐
│                  Surface (1-5)                                │
└──────────────────────────────────────────────────────────────┘
       ↓                                              ↑
┌──────────────────────────────────────────────────────────────┐
│   IM Adapter (Wave webhook in/out)   ·   Browser Web (FastAPI) │
└──────────────────────────────────────────────────────────────┘
       ↓                                              ↑
┌──────────────────────────────────────────────────────────────┐
│                       Bot Core                                │
├──────────────┬──────────────┬──────────────┬────────────────┤
│ Ingest       │ Ontology     │ 追问 Engine  │ Conflict        │
│ Pipeline     │ Engine       │ (灵魂)       │ Detector        │
├──────────────┼──────────────┼──────────────┼────────────────┤
│ Spec Store   │ Compiler     │ Runtime      │ Replay/Eval     │
│ (git repo)   │              │ Agent (Ask)  │                 │
├──────────────┼──────────────┼──────────────┼────────────────┤
│ Memory Layer │ Bot Routing  │ Scheduler    │ Inbox Weekly    │
│ (M5 procedu- │ (M6 分诊)    │ (cron / 自然 │ (周报三段式)    │
│  ral, 软)    │              │  语言定时)   │                 │
├──────────────┼──────────────┼──────────────┴────────────────┤
│ ACL Layer    │ Model Router │   ⏳ extensions/ 自迭代外挂层  │
│ (M8 话题级硬 │              │     (Q2,尚未实装)            │
│  过滤,4 闸) │              │                                │
├──────────────┴──────────────┴────────────────────────────────┤
│ ⏳ Sandbox (Q2,systemd-run + cgroup,尚未实装)              │
└──────────────────────────────────────────────────────────────┘
       ↓                                              ↑
┌──────────────────────────────────────────────────────────────┐
│   Storage (event sourcing)                                    │
├──────────────────────────────────────────────────────────────┤
│ Raw Input Store (sqlite, append-only, 唯一权威源)              │
│   ↓ derived                                                   │
│ Ontology · Spec Store (git) · Vector Index (sqlite-vec) ·     │
│ FTS5 全文索引(jieba 中文分词) · Memories · PendingRoutings ·  │
│ Ask Answers · Inquiry Log · Conflict Log                       │
└──────────────────────────────────────────────────────────────┘
```

**核心纪律**: **Raw Input 是唯一权威源,其他都是 derived**。任何 derived 状态都可以从 raw 重新蒸馏。这是 event sourcing。

---

## 5. 多专家:身份维度 vs 知识维度严格分层

| 层 | 是否带身份 |
|---|---|
| Raw Input Store | **带**(author = 域账号,Wave 入站时直接拿,不需要对接 IAM) |
| Reasoning Log | **带**(谁追问、谁回答、谁裁决) |
| Conflict Log | **带**(双方身份、裁决人) |
| **Ontology / Spec / Fact / Case** | **不带**(只带 provenance 链,可追到 raw) |
| **acl_topic_id**(M8) | **带**(在 raw + 5 类候选上,标记内容属于哪个受控 topic) |

> acl_topic_id 与作者身份正交:作者身份是"这条 raw 是谁说的",acl_topic_id 是"这条 raw 涉及什么受控话题"。访问控制看 acl_topic_id,不看作者身份。

意味着:
- 跨专家冲突解决后,主规约**没有 owner**,只有 provenance 列表
- "A 专家视角"不出现在 spec 里,只作为反例附注 + 反例的 provenance 里有 A
- 知识库的演化 = **匿名共识层**

---

## 6. 决策规约 MD 文件结构

每个 spec 文件是 MD + YAML frontmatter,例:

```markdown
---
id: SC-014
type: decision-spec
domain: 工单                  # 描述性,不是分类硬约束
provenance: [RAW-082, RAW-091, RAW-117, ...]   # 可追到 raw input
sources_count: 8              # 用了多少条 raw 蒸馏
exceptions_count: 1
confidence: medium
last-reviewed: 2026-05-20
---

# 工单加急判定

## 适用情境
当工单提交时,且 ...

## 决策规则
IF 部门误报率 < 30% AND 时间窗口 = 业务紧急期 THEN 加急
ELSE 不加急

## 反例
- [[CASE-203]]: 去年 A 部门虽然误报率高...

## 相关
- 涉及实体: [[entities/部门]] · [[entities/工单]]
- 关联规约: [[specs/部门误报率统计]]
```

**为什么是 MD 不是 DB**:
- 专家能读、能直接编辑、能 git diff
- LLM 直接消费,不需要序列化
- **新岗位接入只需 git clone 这套 MD 走**——这是复用率的载体

---

## 7. 5 个押注(每个都可能炸)

### 押注 1 — Ontology 是涌现的,不是预设的
不预设 schema。第一次专家说"工单",创建 `entities/工单.md`,描述就是专家说的那一句。第二次提到补充字段。出现"加急工单"时,产品问"这是工单的子类还是属性?"专家选。
- **风险**: ontology 演化失控变一坨乱麻
- **对冲**: 周期跑"ontology 体检",发现孤儿/重复/过细分,主动建议合并

### 押注 2 — MD + frontmatter 足够表达决策规约
不用 RDF / OWL / 图数据库。
- **风险**: 复杂规则(嵌套条件、概率推理)表达不出来
- **对冲**: 规约里允许嵌入小段 DSL(只在必要的地方用)

### 押注 3 — 追问 Engine 能把内隐边界问出来
维护"追问策略库"(初始 20 条 → 100+),每条对应一种规则危险信号。专家显式打标命中率,策略库自迭代。
- **风险**: 策略库前 3 个月调不出来,项目死
- **对冲**: 第 1 个月就要看到 5 条以上策略稳定有效

### 押注 4 — 专家级判断 ≈ 规约 + fact + case 的组合查询
**不依赖 fine-tune**。所有"专家性"在 spec store,基础模型只负责检索 + 推理。新模型出来换 runtime,specs 不变。
- **风险**: 模型不够强,即使给规约也答不对
- **对冲**: 模型不够时产品宁可输出"不确定,升级",不输出错答

### 押注 5 — 多专家 = 版本化 + scope 切分,不是合并
不试图合并成"折衷意见"。每条规约只有一条主版本,owner 拥有最终决定权,异见作为反例附注。
- **风险**: 跨域问题数字人分裂、答矛盾
- **对冲**: 跨域强制走主从/投票,Ask 层显式声明"采用 A 视角"

---

## 8. 知识规模管理(防膨胀)

**问题**: 企业内一个名词可能数千个(工单类型 2000+ / 员工数万 / 系统 500+),如果"提到就建 MD",几个月就 10 万级,AI 蒸馏喂进去效果不如关键词搜索。这是大部分 KMS 的归宿。

**对策**: 严格区分"决策性知识"和"事实性数据"。

### 8.1 我们存什么 / 不存什么

| 类型 | 例子 | 存哪 | 数量级 |
|---|---|---|---|
| 决策规约 | 工单加急判定规则 | **本仓库 specs/** | 数百~千/岗位 |
| 反例 / case | 去年 A 部门加急被认定误报 | **本仓库 cases/** | 数千 |
| 决策性事实 | "A 部门误报率 47%" | **本仓库 facts/** | 几百 |
| 核心概念 entity | 工单 / 加急通道 / 误报率 | **本仓库 ontology/entities/** | **100-300** |
| 工单类型全表 | 2000 个工单类型 | 工单系统 API | 不存 |
| 员工列表 | 数万员工 | HR API / Wave 用户接口 | 不存 |
| 系统名全表 | 500 个内部系统 | CMDB API | 不存 |
| 项目列表 | 数千项目 | 项目系统 API | 不存 |

**原则**: 任何在"外部系统已有权威表"的实体,我们只存 `external_id + 简短描述 + 外部 API 端点`,详细信息**即时查外部 API**,不复制。

### 8.2 Entity 晋升机制(只有"配得上"的才建 MD)

```
专家说话提到 X
    ↓ semantic search 找近似 + LLM judge 是否同义
    ↓
   找到 ───────→ 合并到现有 entity,不创建
   没找到 ─────→ 候选状态(只在 raw input 里 inline 出现)
                    ↓
              满足条件: 被 ≥ N 条 raw 引用 (默认 N=3)
                       AND 关联到 ≥1 条 spec/case/fact
                    ↓
                是 → 晋升为正式 entity MD
                否 → 保持 inline,永不建 MD
```

含义:
- 只提一次的名词不建 MD
- 提多次但无决策关联不建 MD
- 只有真正成为**决策概念**的才建

### 8.3 防膨胀的 4 个自动机制

| 机制 | 频率 | 谁做 |
|---|---|---|
| **合并优先** | 每次新输入 | 抽候选 entity 时先 semantic search,LLM judge 同义则合并 |
| **晋升门槛** | 每次新输入 | 必须 ≥ 3 raw 引用 + ≥ 1 决策关联,否则保持 inline |
| **周期体检** | 每周 1 次 | LLM 扫全 ontology,近似 entity 建议合并,孤儿建议归档 |
| **Decay 归档** | 每月 1 次 | 6 月无引用的 entity 标 `archived: true`,降权但不删 |

### 8.4 规模预估

| 项 | 单岗位 | 5 岗位 |
|---|---|---|
| 决策规约 specs | 50-200 | 250-1000 |
| 反例 cases | 100-500 | 500-2500 |
| 决策性 facts | 50-200 | 250-1000 |
| 核心 entities | 50-150 | **150-300**(跨岗位重叠) |
| 关系 relationships | 20-100 | 100-500 |
| **总 MD** | **~300-1100** | **~1300-5000** |

5000 量级 git + ripgrep + LLM 都能扛。10 万就崩。**膨胀的根因都是无脑提取 + 无脑创建** — 我们的核心纪律是 AI 默认合并、默认复用、只有专家显式认可才扩张。

### 8.5 实现要点

- semantic search: 用 `bge-m3` + sqlite-vec,**1024 维 cosine**(中文表现好,存储/计算比 3072 维 OpenAI 模型省 3 倍;走 OpenAI 兼容 /v1/embeddings)
- 全文检索: FTS5 + jieba 中文分词(M7,1000 篇规模设计) — 与向量召回 Jaccard RRF 融合
- 同义 judge / 冲突 judge: Sonnet 4.6 跑(确定性高、便宜)
- 体检: 每周一次 batch job 走 Sonnet
- Decay: SQL 定时任务,只改 frontmatter `archived: true`
- 策略外置: `meta/policies/{knowledge_policy, llm_routing}.yaml` 放 spec git repo,改策略走 PR + git diff,bot 代码不写阈值/路由。ACL 白名单是系统策略不是业务知识,留在 `bot/helper/policy/defaults/topic_acl.yaml`,不进 spec repo

### 8.6 策略可演进 — 知识化策略本身就是元规约

§8.2 / §8.3 描述的是 **当前快照下的默认策略**。这些规则会变 — 不是技术规则的迭代,是**对"什么样的东西配得上建 MD"的判断会演进**。

举例:
- 半年后发现"工单类型"虽然提及多但本质是事实表 → 全类目改为永不晋升
- "决策概念" 这一类的晋升门槛从 `≥3 raw + 1 spec` 改为 `≥2 raw + LLM judge`
- 不同 entity type 配不同 decay 周期(决策概念终身保留,事实性 entity 3 月衰减)

如果策略写死在代码里,每改一次就是一次 bot 升级 + 部署,且**已建的 MD 不会按新策略重新评估**——库会越变越乱。所以架构上把策略外置成配置 + 让它支持回溯。

#### 四件事

**1. 策略外置成 yaml,业务代码不写阈值**

`bot/helper/config/knowledge_policy.yaml`:

```yaml
version: 2026.05.25-v1
entity_promotion:
  default:
    min_raw_refs: 3
    require_spec_relation: true
  by_type:
    decision_concept: { min_raw_refs: 2 }
    system_name:      { promote: never }   # 整个类型禁止晋升
    ticket_type:      { promote: never }
decay:
  default:          { months: 6, action: deprioritize }
  decision_concept: { action: never }      # 决策概念不衰减
merge:
  semantic_similarity_threshold: 0.85
  judge_model: claude-sonnet-4-6
```

代码侧只读策略不存判断:`if policy.should_promote(entity_type, raw_ref_count, has_spec_relation): ...`。

**2. 策略入 git,自身可审计可 diff**

策略文件是 spec repo 里的一员(放 `meta/policies/knowledge_policy.yaml`),改策略走 PR 流程,每次变更都有 commit message 说"为什么改"。后续任何"为什么这个 entity 当时没建 MD"的问题都可以 `git blame` 查到当时的策略版本。

**3. 回溯复评(关键能力)**

策略改了之后必须能扫一遍存量:

```bash
helper policy evaluate --version=2026.05.25-v1 --dry-run
# 输出: 
# 12 个 entity 按新策略应降级回 inline (列表)
# 5 个候选 inline 按新策略应晋升为 entity (列表)
# 30 个 entity 的 decay 周期变更
```

确认无误后 `--apply` 一键迁移,所有变更走一个 git commit,可 revert。**没这个能力,策略升级只对新输入生效,旧库永远停留在旧策略下**。

**4. A/B 双跑(M3 后才用)**

新策略上线前可以双跑评估: 同一批新 raw input 同时按 v1 / v2 策略各自走一遍,产出两份候选 entity 列表,人工对比哪个更合理再切换。M1/M2 暂不做,先把单策略 + 回溯走通。

#### 落到代码的位置

| 角色 | 文件 |
|---|---|
| 策略加载器 | `bot/helper/policy/loader.py` — 读 yaml,缓存,validate |
| 策略判断 API | `bot/helper/policy/knowledge.py` — `should_promote / should_decay / should_merge` |
| 回溯执行器 | `bot/helper/policy/reevaluator.py` — `evaluate(version, dry_run=True)` |
| 配置 | `meta/policies/knowledge_policy.yaml`(在 spec git repo 内) |

---

## 9. Storage 物理布局

```
/var/lib/helper/ (服务器) 或 ./var/helper/ (本地开发)
  ├── git-repo/                ← Spec Store 真实 git 仓库
  │   ├── ontology/
  │   │   ├── entities/
  │   │   └── relationships/
  │   ├── specs/
  │   ├── facts/
  │   ├── cases/
  │   └── meta/policies/       ← knowledge_policy.yaml + llm_routing.yaml(topic_acl 不在此, 在 bot/helper/policy/defaults/)
  ├── helper.db                ← SQLite + sqlite-vec + FTS5,单库;详见下表
  └── (⏳ Q2) extensions/       ← 自迭代沉淀(尚未实装)
```

**helper.db 主要表**(单库不分表):

| 域 | 表 |
|---|---|
| Raw 层 | `raw_inputs`(append-only,唯一权威源)/ `wave_event_dedup`(7.1h 去重窗) |
| L1 派生 | `l1_results` / `l1_items`(5 类原子)/ `fact_candidates` / `case_candidates` / `relation_candidates` / `entity_candidates` / `spec_candidates`(全部带 `acl_topic_id` 列冗余继承自 raw,M8) |
| 推理 / 答题 | `ask_answers`(reasoning log)/ `inquiry_log`(追问)/ `inbox_digest`(周报快照) |
| 冲突 | `conflict_log`(target_type ∈ {spec, fact, case, concept, relation, memory}) |
| Memory(M5) | `memories`(scope_type / scope_ref / directive / superseded_at) |
| Routing(M6) | `pending_routings`(target_app_id / via_label / tracker_card_msg_id / consumed_at) |
| 检索索引 | `vec_items` 系列(向量,1024 维 bge-m3)/ `fts_items` 系列(FTS5 + jieba) |
| Scheduler / 反馈 | `scheduled_tasks` / `schedule_confirm` / `reaction_log` / `identity_cache` |

**为什么 sqlite 不是 postgres**: dogfood 期数据规模(几千~万条索引)用 sqlite-vec + FTS5 完全够,无需独立 DB 进程。本地开发也不需要 docker。规模上来再考虑迁移。

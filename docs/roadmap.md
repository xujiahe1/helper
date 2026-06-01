# Roadmap

## 总原则

- **每个 month 必须能砍掉项目** — 验收不达标就 kill,不要硬上。
- **本地优先** — 服务器是部署目标,不是开发环境。
- **dogfood** — 用户(徐嘉禾)的真实工作 = 第一批 raw input。

---

## Month 1 — IM 优先核心闭环 ✅

- Surface 1 (Ingest) + Surface 4 (Ask) 通过 Wave IM 完整闭环
- Ingest pipeline / Ontology engine v0(entity 晋升)/ Spec store(git)/ Compiler / Runtime agent / Model router / IM Adapter / 部署到 10.234.81.212

**验收**:用户 IM @bot 扔 ≥30 条判断 / 10 题答对 ≥6 / entity ≤ 50。

---

## Month 2 — 加灵魂(追问 + 冲突)✅

- 追问 Engine(20 条策略,命中率打标)
- Conflict Detector + Surface 2 Inbox(IM 周报三段式编号)
- 信息修正统一路径(任意类型不一致 → conflict_log → 「采纳/保留/都留 2-N」)
- Ontology 周期体检 ⏳

**验收**:追问命中率 ≥ 50%;规约 30 → 80 条,反例 ≥ 30。

**Kill 条件**:追问命中率 < 30% → 没护城河。

---

## Month 3 — 多专家协作 + 复用验证 ⏳

- Surface 5 Conflict + 简易 Web 仲裁台 ⏳
- Surface 3 Browser ✅(`/admin/browse`)
- 多用户身份打通 ✅(走 Wave users/get,不对接 IAM)
- 文档批量 ingest ✅(Qwen 后台跑)
- Replay / Eval ✅(`helper.eval.replay`)
- **第二专家接入** ⏳ — Month 3 核心验收

**验收(关键 = 复用率)**:第二个领域 3 周内规约 ≥ 30 / 命中率 ≥ 50% / 改产品代码 ≤ 30%。

**Kill 条件**:第二领域要超 3 周或大量改代码 → 不是产品,是定制项目。

---

## Month 4 — Dogfood 打磨期 ✅

- 群聊 chat_context 注入(长路径默认拼最近 16 条 / 1 天 user+bot 双角色)
- 自然语言定时任务(parser/runner/handlers,进程内 1min 扫)
- webhook 异步队列化(落 raw 立刻 200,LLM 全 fire-and-forget)
- 向量召回 + KM 文档导入(bge-m3 + sqlite-vec + Jaccard RRF;ProseMirror 渲染 → L1)

---

## Month 5 — Procedural Memory ✅

- `memories` 表(scope_type / scope_ref / directive / superseded_at,全公司共享)
- Memory 抽取管线(LLM 按语义识别"描述世界 vs 约束 bot 行为",不靠关键词)
- chat_context 注入(对齐 ask)
- ask 拼接路径(命中 entity 的活 directive → SYSTEM_PROMPT 末尾 `## 用户偏好`)
- 冲突走周报裁决(target_type='memory')

**Kill 条件**:抽取误判率 > 30% → 这条路死。

**Open**:撤销路径(`取消刚才那条`)还没 dogfood 验过。

---

## Month 6 — Conflict 5 类全过 + Bot-routing ✅

- 5 类原子统一过 LLM judge(sonnet,输出 contradicts / scope_diff / dup / orthogonal,支持 auto_superseded / auto_rejected / auto_coexist)
- conflict_judge opus → sonnet
- Bot-routing(命中"X 类问题去问 @Y" → 私聊外部 bot 代发 + `pending_routings` 5min 超时)
- 卡片透传保真(前缀 `@asker 已咨询 @Y:` + 原样透传 msg_type+content)
- bot-to-bot 入站不污染语料(`sender.id_type=app_id` 且非己 → 不落 raw_inputs)

---

## Month 7 — Retrieve 索引化 ✅

- FTS5 + jieba 中文分词(`fts_items` 系列)
- 5 类候选向量化(bge-m3 1024 维)
- FTS5 + 向量 + Jaccard CJK bigram 三路 RRF 融合
- 候选差集过滤(剔除 superseded)

**Open**:1000 篇规模真实压测(P50 < 200ms / P95 < 500ms)。

---

## Month 8 — Topic ACL 强管控层 ✅

- 6 张表加 `acl_topic_id` 列 + auto-migrate
- `helper.acl` 模块 + acl_tag 任务接 `llm_routing.yaml`
- 4 道闸 + 1 道兜底(详见 `runtime.md` §2.7)
- ingest sink 同步打标 + `helper acl-backfill` / `acl-status` CLI
- 白名单 yaml 在 `bot/helper/policy/defaults/topic_acl.yaml` — **ACL 是系统策略不是业务知识,不进 spec repo**;改 yaml = 改 helper 仓库 + 重启,跟改 SYSTEM_PROMPT 同等待遇

---

## Inbox 节奏 — 周报 vs 主动触发

owner 不必等周一才看到待办。两条触发并存:

| 触发 | 谁发起 | 说明 |
|---|---|---|
| Cron 周报 | 系统 | 每周一 09:00 自动 build_digest + send_to(owner) |
| 主动触发 | owner | 私聊 bot 发「/inbox」「inbox」「周报」→ 立刻 build + 推 + snapshot |

回执解析支持两套编号:周报式「采纳 2-N」(N 是 1-based 周报序号)+ 老格式「批准 #spec_id」。

---

## 当前 open 问题

| # | 问题 |
|---|---|
| OP-2 | 第二个领域专家是谁 — Month 3 核心验收,未启动 |
| OP-3 | M5 撤销路径(`取消刚才那条`)还没 dogfood 验证 |
| OP-4 | reply 父消息反查 — 当前只存 `parent_message_id` 不反查内容,实测群里大家不用 reply 语义,优先级低 |

---

## Dogfood 策略

第二领域专家接入是当前唯一未完成的核心 dogfood 节点(Month 3 复用率验证)。除此之外:
- Topic ACL 由 jiahe.xu 出白名单后亲自验
- 第二批 raw input 持续来自真实工作场景,无需另设

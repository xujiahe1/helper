# Roadmap

## 总原则

- **每个 month 必须能砍掉项目** — 验收不达标就 kill,不要硬上
- **本地优先** — Month 1 完全在本地跑通,Wave IM 部署到 Month 2
- **dogfood** — 第一批 raw input 来源就是用户(徐嘉禾)和 Claude 的对话本身,不另找场景

---

## Month 1 — IM 优先核心闭环

> **决定**: 不做"为验收而存在"的临时 Web。直接走 IM 当生产形态。Web 浏览(Surface 3)延到 M3,M1 全部走 Wave IM。

### 做

| Surface | 范围 |
|---|---|
| Surface 1 (Ingest 通过 IM) | @bot 单条判断 / 转发记录(语音 ASR 暂不做) |
| Surface 4 (Ask 通过 IM) | @bot 提问,bot 回答带引用 + 不确定性 |

| 模块 | 范围 |
|---|---|
| Ingest pipeline | 毛坯 → L1 结构化 → 入 raw store + 候选 spec |
| Ontology engine v0 | 涌现 + entity 晋升机制(防膨胀核心) |
| Spec store | git 真实落地,MD + frontmatter |
| Compiler | spec → agent 可消费包 |
| Runtime agent | 检索 + 推理 + 引用,不确定性自标 |
| Model router | Opus 主路径 / Sonnet 次路径 / 走 Athenai |
| **IM Adapter** | Wave webhook 入站(签名 + AES) + Wave 开放平台 HTTP API 出站(app_id+app_secret → access_token) |
| **部署** | 真实部署到 10.234.81.212(systemd + nginx 反代) |

### 周拆分

| 周 | 交付 |
|---|---|
| 周 1 | 本地: bot core + L1(真实 Athenai)+ sqlite + git store + model_router + Wave 回调签名/AES + admin sk + 后台 L1 sink |
| 周 2 | Wave 开放平台 API 出站(`wave_client`: access_token 自续期 + send_message + id_convert)+ webhook 接 ack 回执 + union_id → 域账号反查 + IdentityCache |
| 周 2(剩余) | raw 字段细化 + 部署到服务器(systemd + Wave 回调 URL 配置 + nginx 反代决策) |
| 周 3 | 全流水线 IM 内打通 + Ask runtime + 联调 |
| 周 4 | dogfood: 用户在 IM 实际用,本对话切片作为第一批原料 |

### 周 1 本地验收 SOP

> 前置: 已激活 venv (`source bot/.venv/bin/activate`),`bot/.env` 含 `ATHENAI_API_KEY` 和 `HELPER_ADMIN_SK`。

```bash
# 1. 初始化 / 状态总览
helper init                    # 看到 sqlite / git repo / knowledge / llm_routing / admin / wave 五行状态
helper hello                   # 包能跑

# 2. CLI 路径:扔一条判断,看 L1 结果
helper ingest "上周决定把 PRD 模板的'风险章节'放到首页而不是末页,因为产品经理写到末页时已经累了。"
helper raw-list                # 应看到刚扔的那条,L1 列 = OK
helper raw-show 1              # 看完整 L1 五字段

# 3. 后台 backfill(老数据 / L1 失败的可重跑)
helper l1-backfill

# 4. Wave webhook 路径:启服务,模拟 Wave 平台投一条事件
helper serve --port 8009 &     # 另一个终端跑也行
curl -s http://127.0.0.1:8009/healthz
curl -s -H "X-Helper-Admin-Key: $HELPER_ADMIN_SK" http://127.0.0.1:8009/admin/healthz
curl -s -H "X-Helper-Admin-Key: $HELPER_ADMIN_SK" http://127.0.0.1:8009/admin/raw-inputs | jq .
# Wave webhook 自动 smoke 见 bot 的端到端测试脚本(模拟加密/加签后投递,验证 raw + L1 落库)

# 5. 策略外置可见:决定模型路由 / 晋升规则,看 spec git 历史
ls -la var/helper/git-repo/meta/policies/    # 两个 yaml: knowledge_policy + llm_routing
git -C var/helper/git-repo log --oneline     # init + 后续策略变更
```

**验收点**:
- `helper raw-show <id>` 能看到 L1 五字段(scene/signals/tradeoffs/choice/rationale),且 model 显示 `claude-sonnet-4-6`(走 Athenai)
- Wave webhook 验签错误返 401,event_id 重复返 200 但不重复落库
- Admin 端点不带 sk → 401,sk 错 → 401,sk 对 → 200
- `meta/policies/llm_routing.yaml` 改 `l1_structure` 的 model,重启后 `raw-show` 看到 model 字段对应变化(策略可演进的物证)

### 不做

追问、冲突检测、Inbox(M2)、文档批量(M3)、Replay/Eval(M3)、Web 浏览界面(M3)

### 验收

- 用户 4 周内 IM @bot 扔 ≥ 30 条判断
- 问 10 个问题,数字人答对 ≥ 6 个
- 规约规模: 20-30 条 / entity 数量 ≤ 50(防膨胀第一道考验)
- 用户能说"下次做这事我会先在 IM 问一下 bot"

### Kill 条件

- 答对 < 4 / 10 → Athenai Opus + 规约的路子不成立
- entity 数量 > 200 → 晋升机制失效,会膨胀
- 投入 > 4 周还没跑通 → 架构过设计,需要回炉

---

## Month 2 — 加灵魂(追问 + 冲突)

### 做

| 新增 | 范围 | 状态 |
|---|---|---|
| 追问 Engine | 初始 20 条策略,每次追问命中率打标。追问通过 IM 推送 | ✅ |
| Conflict Detector | LLM judge(decision vs spec)+ 结构判定(fact/case/relation 同 key 不同 value)— 5 类原子统一走 conflict_log,sink 自动挂载 | ✅ |
| Surface 2 (Inbox) | 走 IM 周报形式 — 周一 push 一条卡片消息列出待办;owner 私聊「/inbox」主动触发当下 digest | ✅ |
| 信息修正统一路径 | 任意类型新输入和既有不一致都进 conflict_log,owner 用「采纳 / 保留 / 都留 2-N」三选项裁决;superseded 立刻 build_bundle | ✅ |
| Ontology 周期体检 | 每周一次合并近似 entity / 标记孤儿 | ⏳ |

### 验收

- 追问命中率 ≥ 50%(用户觉得"问得有道理")
- 规约从 30 条扩到 80 条,反例 ≥ 30 条
- IM @bot 能正常对话,被拉进群能 listen
- 你每周清 Inbox ≤ 30 分钟

### Kill 条件

- 追问命中率 < 30% → "把内隐边界 AI native 化"路子死,产品没护城河

---

## Month 3 — 多专家协作 + 复用验证

### 做

| 新增 | 范围 | 状态 |
|---|---|---|
| Surface 5 (Conflict) | IM 群里 @相关专家解决冲突 + 简易 Web 仲裁台 | ⏳ Web 仲裁台 |
| Surface 3 (Browser) | 简易 Web 知识库浏览(只读,git repo 渲染) | ✅ `/admin/browse` |
| 多用户身份打通 | Wave user → 域账号 + 姓名(走 Wave users/get,不对接 IAM),raw input 全部带 author | ✅ |
| 文档批量 ingest | 走 Qwen/GPT-mini,后台跑 | ✅ batch_ingest |
| Replay / Eval | 历史 Q&A replay,版本对比 | ✅ helper.eval.replay |
| **第二专家接入** | 找一个**完全不同领域**的专家,3 周内跑出 30 条规约 | ⏳ |

### 验收(关键: 复用率)

- 第二个领域接入,**3 周内**规约 ≥ 30 条 / Ask 命中率 ≥ 50%
- 接入过程中我们改产品代码量 ≤ 30%(剩余 70% 复用)

### Kill 条件

- 第二个领域要超过 3 周或大量改产品 → **不是产品,是定制项目** → 整个方向需要重新审视

---

## Month 4 — Dogfood 打磨期(2026-05 已完成)

> M1-M3 骨架交付后,2026-05-26 与用户对齐了 4 件"上线前必修"的真实问题。代码全部落地,持续在 dogfood 中修 bug。

### 做

| 新增 | 范围 | 状态 |
|---|---|---|
| 群聊上下文 + 静默回填 | @bot 时拼最近 8 条 / 1 天 user+bot 双角色;群里非 @bot 走静默 L1 + 反查身份不发回复 | ✅ `helper/storage/raw_store.format_context_block` + `wave_webhook.schedule_l1(prefilter=True)` |
| 用户对话创建定时任务 | 自然语言 → cron → bot 复述确认 → 进程内 1min 扫;支持周报/月报/定期 ask/spec 时效提醒 | ✅ `helper/scheduler/`(parser/runner/handlers/tasks) |
| webhook 异步队列化 | 落 raw 后立刻 return 200 ack;L1/intent/inquiry/conflict 全 fire-and-forget | ✅ `wave_webhook.wave_callback` 调度三类后台任务 |
| 向量召回 + KM 文档导入 | bge-m3 embedding + sqlite-vec + Jaccard RRF 融合;KM 走 HTTP API → ProseMirror 渲染 → L1 | ✅ `storage/vector.py` + `im/km_ingest.py` + `im/prosemirror.py` |

### Dogfood 暴露并修掉的具体问题(2026-05)

- ProseMirror JSON 而非 markdown 让 L1 抽 0 atoms → 加渲染 + 长文档按 H2 切片 + JSON salvage 容错
- max_tokens=4K 截断 LLM 输出 → 16K + 广抽取 prompt
- Jaccard 中文按整串 token 永远 0 召回 → CJK bigram 分词
- bge-m3 8192 token 上限被 416K JSON 顶爆 → 输入截断到 6000 字符
- ask LLM 长回答里真实 \n 让 json.loads 失败 → strict=False
- `_candidate_pass` 漏扫 EntityCandidate 让 concept 类原子全不可达 → 补扫

### 验收

- KM 文档真"学进来",ask 答得出文档里写过的内容(2026-05-29 已验证)
- 群里被 listen 的判断进 raw_inputs,周报里能被 review

---

## Month 5 — Procedural Memory 层(规划中)

> 现有 5 类原子(decision/fact/case/concept/relation)全是描述客观世界的 semantic memory。dogfood 暴露:用户也想"教 bot 怎么答",这条通路缺失。

### 要做

| 新增 | 范围 |
|---|---|
| Procedural memory 表 | scope(挂哪个 entity/全局)+ directive(指令文本)+ owner + created_at + superseded_at;全公司共享 |
| Memory 抽取管线 | 与 L1 解耦,LLM 按语义识别"是描述世界,还是约束 bot 行为/口径";不靠关键词 |
| ask 拼接路径 | 命中 entity 的 directive 拼进 SYSTEM_PROMPT 的 `## 用户偏好` 段(不进检索结果区) |
| 冲突走周报裁决 | 复用现有 5 类原子的 conflict_log + inbox 三段式裁决;后写不直接覆盖 |

### 验收

- 用户在 wave 说"答哥的问题别每次复述身份",下次问相关问题 bot 真简化
- 撤销路径:用户说"取消刚才那条" → 周报里能看到失效记录

### Kill 条件

- 抽取误判率 > 30%(把日常话当指令存) → LLM 边界判断不行,这条路死

---

## Inbox 节奏 — 周报 vs 主动触发

owner 不必等周一才看到待办。两条触发路径并存:

| 触发 | 谁发起 | 说明 |
|---|---|---|
| Cron 周报 | 系统 | 每周一 09:00 自动 build_digest + send_to(owner)。`scheduled_tasks` 里登记 task_type=weekly_report |
| 主动触发 | owner | 私聊 bot 发「/inbox」/「inbox」/「周报」 — 立刻 build + 推 + snapshot |

回执解析支持两套编号:
- 周报式: 「批准 1-N」/「采纳 2-N」/「答 3-N ...」(N 是周报里 1-based 序号,从最近一次 InboxDigest 反查真实 ID)
- 老格式: 「批准 #spec_id」/「答 #inquiry_id ...」(给跨周老候选用)

---

## 当前 open 问题(等用户拍)

| # | 问题 | 当前状态 |
|---|---|---|
| OP-1 | Wave 回调端口 | **已解(2026-05-28)** — `:8001` 在服务器入向被中间网络层封掉(本地办公网 8001 通,服务器 IDC 入向 8001 没 SYN);切到 `:8009` 后 Wave 内网出口 `10.231.152.29` 直接握手成功,challenge 通过。生产端口固定 8009,回调 URL = `mhynetcn://10.234.81.212:8009/callback` |
| OP-2 | 第二个领域专家是谁 | Month 2 末再找,Month 3 启动用 |

---

## Dogfood 策略

### 第一批 raw input

用我们(用户 = 徐嘉禾,bot = Helper)的对话本身作为 raw input。

具体: 这次对话产出了 7+ 轮关于"项目方向 / 知识化 / 架构 / 押注"的判断。这些都是**专家在权衡的当下做出的判断**,正符合"触发式 articulation"的语料形态。

把这次对话做后处理:
1. 切分成 N 条原子判断
2. 跑 L1 结构化 → entities (项目方向 / 决策规约 / 押注 / 模型路由 / ...)
3. 跑 L2 聚类 → candidate specs
4. 让我(用户)review,验证 L3 追问是否能问出有价值的边界

**这次 review 本身就是 Month 1 的第一次完整闭环 dogfood**。

### 第二批

如果 Month 1 跑通,Month 2 用户用 IM @bot 在日常工作里持续扔判断,慢慢累积。

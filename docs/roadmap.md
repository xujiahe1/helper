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
helper serve --port 8001 &     # 另一个终端跑也行
curl -s http://127.0.0.1:8001/healthz
curl -s -H "X-Helper-Admin-Key: $HELPER_ADMIN_SK" http://127.0.0.1:8001/admin/healthz
curl -s -H "X-Helper-Admin-Key: $HELPER_ADMIN_SK" http://127.0.0.1:8001/admin/raw-inputs | jq .
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

| 新增 | 范围 |
|---|---|
| 追问 Engine | 初始 20 条策略,每次追问命中率打标。追问通过 IM 推送 |
| Conflict Detector v0 | LLM judge 检测新输入和已有规则的矛盾 |
| Surface 2 (Inbox) | 走 IM 周报形式 — 周一 push 一条卡片消息列出待办 |
| Ontology 周期体检 | 每周一次合并近似 entity / 标记孤儿 |

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

| 新增 | 范围 |
|---|---|
| Surface 5 (Conflict) | IM 群里 @相关专家解决冲突 + 简易 Web 仲裁台 |
| Surface 3 (Browser) | 简易 Web 知识库浏览(只读,git repo 渲染) |
| 多用户身份打通 | Wave user → 域账号 + 姓名(走 Wave users/get,不对接 IAM),raw input 全部带 author |
| 文档批量 ingest | 走 Qwen/GPT-mini,后台跑 |
| Replay / Eval | 历史 Q&A replay,版本对比 |
| **第二专家接入** | 找一个**完全不同领域**的专家,3 周内跑出 30 条规约 |

### 验收(关键: 复用率)

- 第二个领域接入,**3 周内**规约 ≥ 30 条 / Ask 命中率 ≥ 50%
- 接入过程中我们改产品代码量 ≤ 30%(剩余 70% 复用)

### Kill 条件

- 第二个领域要超过 3 周或大量改产品 → **不是产品,是定制项目** → 整个方向需要重新审视

---

## 当前 open 问题(等用户拍)

| # | 问题 | 当前状态 |
|---|---|---|
| OP-1 | Wave bot 应用是否支持 `mhynetcn://` 自定义 scheme | **未解** — Wave 后台保存 `mhynetcn://10.234.81.212:8001/callback` 返回 retcode 25002 "无效的回调地址",在 challenge 推送之前就被拒;原因不明,2026-05-27 切到本地起服务排查 |
| OP-2 | `mhynetcn://` 协议细节(回调具体怎么投递) | 部署前要从 Wave 后台 / KM 5173 文档树读完整 |
| OP-3 | 第二个领域专家是谁 | Month 2 末再找,Month 3 启动用 |
| OP-4 | 服务器 nginx 是否要加 `/wave/webhook` 反代 | 看 OP-1/2 结论;如果 mhynetcn 直连 IP 就不需要 |

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

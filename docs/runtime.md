# Runtime

## 1. 模型分配 (Athenai)

全部 Claude 任务统一 `claude-sonnet-4-6`。Opus 5x 成本但实测增益不显著, 关键路径错误来自召回/注入而非模型能力。Athenai 政策禁用 Claude 大批量打标, 批量抽取走 Qwen。

| 档位 | 模型 | 用途 |
|---|---|---|
| **Claude 主线** | `claude-sonnet-4-6` | ask / elicit / code_plan / intent_classify / l1_structure / synonym_judge / conflict_judge / schedule_parse / memory_extract / acl_tag / restate_bot_reply |
| **L1 预筛 + 批量后台** | `qwen3.6-flash` | 群里"听"路径预筛 / 文档批量 ingest / Replay judge / L2 候选 entity 批量抽取 |
| **Embedding** | `bge-m3` (1024 维) | 检索索引 |
| **Rerank** | `qwen3-rerank` | retrieve > 20 条时启用 |

`bot/helper/llm/router.py` 按 task 名路由, 模型表外置在 spec git repo 的 `meta/policies/llm_routing.yaml`(默认快照 `bot/helper/policy/defaults/llm_routing.yaml`), 改路由走 PR + git diff。

> **新加 task 必须三处同步**:本地 defaults yaml + 本地 spec repo yaml + **服务器 `/var/lib/helper/git-repo/meta/policies/llm_routing.yaml`**(运行时实际读这份)。漏第三处 → router 抛 `Unknown task type` → 调用方静默走兜底。详见 §6。

API 入口: Athenai `https://athenai.mihoyo.com/v1/messages`(Anthropic 原生兼容) + `/v1/chat/completions`(OpenAI 兼容, 跑 Qwen / bge-m3)。`Authorization: Bearer sk-xxx`。

---

## 2. Wave IM 集成

> **不走 MCP**。openapi-mcp(常驻 127.0.0.1:5524)只支持登录用户身份,不支持服务端凭据,所以
> bot 出站、入站、事件订阅管理**全部走 Wave 开放平台 HTTP API**(`https://open.hoyowave.com`),
> 凭服务端 `app_id` + `app_secret` 自换 `access_token`。MCP 那个进程仍跑在服务器上(徐叶佳侧
> 装的,给登录用户身份的 desktop agent 用),但**和 bot 运行时没有任何关系**。

### 2.1 出站(bot 调 Wave)

`bot/helper/im/wave_client.py` 直连 `https://open.hoyowave.com`。`access_token` 在剩余有效期 < 30 分钟时自动续期。

| HTTP 接口 | 客户端方法 | 用途 |
|---|---|---|
| POST `/openapi/auth/v1/access_token/internal` | `get_access_token()` | 自建应用换 token,内部缓存自动续期 |
| POST `/openapi/im/v1/message/send` | `send_message()` | 给用户 / 群发会话消息或通知 |
| POST `/openapi/im/v1/message/reply` | `reply_message()` | 引用回复某条消息(quote) — ask / bot-routing 回贴用 |
| POST `/openapi/im/v1/card/update_active` | `update_card_active()` | 主动更新已发的"思考中"卡片(ask 长路径 / bot-routing 收到回执时替换 thinking 卡片) |
| POST `/openapi/contact/v1/user/id_convert` | `convert_user_ids()` / `open_id_to_domain_account()` | **身份映射: union_id ↔ 域账号** |
| POST `/openapi/contact/v1/users/get` | `users_get()` | 拉用户域账号 + 姓名 + 邮箱(身份反查后回填 raw.author_domain) |

### 2.2 入站(Wave 推回调到 bot)

入站是 Wave 平台主动 POST 到我们的 `/callback` 端点(本来就是 HTTP,跟 MCP 无关)。

**部署形态**: bot 在 `10.234.81.212:8009` 监听 `/callback`。Wave 后台回调 URL 配 `mhynetcn://10.234.81.212:8009/callback`(`mhynetcn` = mihoyo 办公网 http scheme,我们的服务器在办公网内,**协议层合法,不需要 https / 不需要走网关反代**)。

> **端口固化 8009**:服务器 IDC 入向封了 8001(收不到 SYN),8009 通,新部署直接用 8009,不要再试 8001。

> 协议参考: [事件订阅概述](https://km.mihoyo.com/doc/mheo000ok1zs) · [事件办公网推送](https://km.mihoyo.com/doc/mh041f0mt47k)

**Endpoint 必做的 5 件事**(任何一件错都会 Wave 报"URL 验证未通过"或重试风暴):

| # | 责任 | 不做的后果 |
|---|---|---|
| 1 | **AES-256-CBC 解密 body** — key 取 `WAVE_CALLBACK_AES_KEY`,iv = key[:16],PKCS7,base64 解码 | 拿不到事件内容 |
| 2 | **签名校验** — `sha256(hoyowave-open-timestamp + hoyowave-open-nonce + raw_body + WAVE_CALLBACK_SIGN_TOKEN)` 对比 `hoyowave-open-signature` header(注意:用**未反序列化的原始 body 字符串**算,反序列化再算会错) | 接受伪造事件 |
| 3 | **校验事件 1 秒内原样返回 challenge** — 配置 URL 时 Wave 推一条 challenge,body 解密后是 `{"challenge": "xxx", ...}`,必须 1s 内 HTTP 200 返回 `{"challenge": "xxx"}` 原样 | URL 配不上去 |
| 4 | **普通事件 1 秒内 HTTP 200 + body 为 `""` 或 `{}`** — 实际处理走后台 async,不要在响应链上做 LLM | Wave 退避重试(10s/30s/3m/1h/6h × 5),触发事件风暴 |
| 5 | **event_id 去重 7.1 小时窗口** — `header.event_id` 写入 sqlite 表 `(event_id PRIMARY KEY, received_at)`,重复 event 直接 200 不处理 | 重试窗口内同一事件被多次执行(发重复消息 / 重复扣 LLM 配额) |

**实现位置**: `bot/helper/im/wave_webhook.py` — FastAPI router,挂在主 bot 进程下,监听 8009 端口(单进程,见 §3)。

**密钥来源**: 本地开发用 `bot/.env`,服务器生产从 `/etc/helper/wave.env`(chmod 600 root only)读,**绝不入仓库**。

**为什么不分 IM Adapter 独立进程**: bot core / webhook / Browser Web 跑同一个 FastAPI app(同进程不同 router),省内存。流量上来再拆。

**异步调度承重件**: `bot/helper/im/queue.py` 提供 `llm_slot()` 并发限速 + `spawn()` fire-and-forget 调度,所有后台 LLM 任务(L1 / intent / memory_extract / ask / inquiry / conflict)都从这里出。webhook 1s 回调窗口内只能落 raw + return 200,LLM 调用全走 queue。

### 2.3 主流水线 — webhook 触发后做什么

`wave_webhook.wave_callback` 收到一条 IM 消息事件后:

```
落 raw_inputs(同步,< 100ms)
        ↓
  ┌─────┴───────────────────────────────────────┐
  │ A. 单聊 / 群里 @bot                          │
  │    → schedule_memory_extract(raw_id)         │  抽 procedural memory(M5)
  │    → schedule_ask_reply(raw_id, ...)         │  ask 答题 + 引用回复
  │                                               │
  │ B. 群里没 @bot 的消息                         │
  │    → schedule_l1(raw_id, prefilter=True)     │  L1 mini 预筛 → 命中再跑 Sonnet L1
  │    → schedule_post_message(send_ack=False)   │  反查身份回填,不发回复
  └───────────────────────────────────────────────┘
```

A 和 B 都是 fire-and-forget,通过 `helper.im.queue.spawn` 起协程,1s 回调窗口外异步跑。

> **`/clear` 命令**: A 分支内若文本恰好是 `/clear`, 短路 LLM 链路 — 给当前 scope (群=chat_id, 私聊=user:<domain>) 在 `chat_context_cutoffs` 表钉一条 cutoff = 当前最大 raw_id, 之后 `list_chat_history` 用 `RawInput.id > cutoff` 过滤。**只屏蔽上下文加载, 不删数据**, ingest 流水线仍正常处理历史 raw。

> A 分支内部:`schedule_ask_reply` 第一道是 ACL 入口短路(`deny_for_question`)— 命中受控 topic 且 asker 非白名单时直接发 deny_response,不调主路径 LLM。详见 §2.7。

**bot-to-bot 入站分流(M6)**: 当 `event.sender.id_type == "app_id"` 且 sender 不是自己 → 走
`bot_routing.handle_bot_reply` 把外部 bot 的回执贴回 PendingRouting 关联的原会话,**不落 raw_inputs**(避免外部 bot 消息污染语料)。详见 §2.5。

### 2.4 群 listen 边界

- 默认开(因为 bot 不主动加群,**能进的群 = 应该听的群**)
- 群消息 → 进 Raw Input Store 但**不调 LLM 主路径**(只跑 L1 mini 预筛,关键词命中或 mini 判 yes 才升级 Sonnet)
- 触发 LLM 主路径: @bot / 转发消息 / 显式开启某群的"主动处理"

### 2.5 bot-to-bot 路由(M6)

helper 在 procedural memory 里命中"涉及 X 类问题去问外部 bot Y"指令时,会:

1. `dispatch_route()` 私聊外部 bot(rich_text @ 它),DB 落 `pending_routings` 表(target_app_id + via_label + original_chat_id + original_wave_msg_id + tracker_card)
2. 在原会话发一条"思考中"卡片占位
3. 外部 bot 回到 helper 私聊 → webhook 检测 `sender.id_type=app_id` 且非己 → `handle_bot_reply()`:
   - 找最近未消费、未过期的 PendingRouting
   - **前缀**:replace_message 发 `@asker 已咨询 @via:`(私聊不 @ 自己)
   - **LLM 转述**:抽对方原文(text / rich_text / card 内 i18n_text 等)→ `restate_bot_reply` task 重写成 markdown → 用 card `{tag:"flow", elements:[{tag:"markdown",text:...}]}` 发出。**不原样转发** — 外部 bot 的 form/button 组件在 helper 应用下过不了 Wave 校验(retcode 10401069);扁平 i18n_text 也会丢掉表格/换行。
   - 抽空或 LLM 失败 → 兜底 text 直发抽出来的字符串
   - 标 routing.consumed_at
4. 5 分钟没回 → `expire_old_routings()` 标 expired + 推"@via 5 分钟没回,你直接 @ 它再问"

**关键边界**:外部 bot 私聊回 helper 的消息**不落 raw_inputs**(避免 cli_xxx 的回执变成"哥的语料")。

### 2.6 身份映射

**不对接 IAM**。Wave 开放平台 `users/get` 一把就给齐域账号 + 姓名 + 邮箱 + 状态:

```
Wave union_id / user_id
    ↓ POST /openapi/contact/v1/users/get  (Wave 开放平台 API,KM mh1o9t9i2rmy)
{ user_id: "jiahe.xu",
  union_id: "ou_xxx",
  name: "徐嘉禾",
  en_name / nick_name / email / display_status / avatar / tenant_id }
```

缓存进 sqlite 的 `identity_cache` 表(domain_account + name),后台 fire-and-forget
反查,不阻塞 webhook 1s 回调窗口。

> IAM SDK / 认证网关 是给**登录用户身份**(浏览器/桌面 agent)用的,bot 后台 daemon
> 用应用凭据自己换 access_token + 直接拿身份,链路更短。

### 2.7 Topic ACL 数据流(M8)

按数据流顺序 4 道闸,任一命中即生效:

| # | 闸 | 位置 | 行为 |
|---|---|---|---|
| 1 | 入库打标(数据流) | ingest sink → `acl/tagger.py::tag_raw` | LLM 判 topic_id 落 `raw_inputs.acl_topic_id`,同步派生 l1_items / 5 类候选冗余继承 |
| 2 | retrieve 出口(数据流) | `ask/retrieve.py::filter_hits` | asker 不在 `topic.allowed_domains` → 带该 topic 标的 hit 全过滤 |
| 3 | chat_context 过滤(数据流) | `storage/raw_store.py::format_context_block` | 拼群历史按 asker 过滤 — 防白名单连续聊后 outsider 穿插提问拿到敏感上下文 |
| 4 | ask 入口短路(数据流) | `ask/runtime.py::deny_for_question` | 对 (question + chat_context) 跑 acl_tag,命中且非白名单 → 返 `deny_response`,**不调主路径 LLM** |
| 5 | 出口 scrub(模型幻觉兜底) | `ask/runtime.py::scrub_output` | 主路径 answer 文本含 yaml `output_blocklist_terms` 且 asker 非白名单 → 整段替换 deny_response,兜底防 LLM 凭参数知识脑补 |

代码: `bot/helper/acl/`(policy + tagger)+ `bot/helper/policy/loader.py::TopicAcl`。yaml 在 `bot/helper/policy/defaults/topic_acl.yaml` — **ACL 是系统策略不是业务知识,不进 spec repo**。改 yaml = 改 helper 仓库 commit + 重启(进程内 cache,无热加载),跟改 SYSTEM_PROMPT 同等待遇。

CLI: `helper acl-backfill`(批量给存量 raw 打标)/ `helper acl-status`(看白名单 / topic 分布)。

---

## 3. 服务器约束

`10.234.81.212`: **2 vCPU / 15G RAM / 40G 磁盘 / Ubuntu 22.04**

已运行: nginx 1.18 / openapi-mcp(98M,登录用户身份的 desktop agent 用,bot 不依赖)/ monitoring exporters / OS。

### 3.1 关键决策(跟内存解耦,理由是工程权衡)

- **单进程 + asyncio,不开 worker 池** — SQLite 单写锁是真瓶颈,多 worker 收益边际;LLM 调用是 IO 阻塞型,asyncio 已足够
- **不在本地跑模型,走 Athenai API** — 模型供应、版本、限流由租户统一管;本地维护 GPU + 模型不属于这个项目的产品 IP
- **不用 Postgres,用 SQLite + sqlite-vec + FTS5** — dogfood 期几千~万条索引规模,SQLite 完全够,无需独立 DB 进程
- **不用 Docker** — bot 是单 Python 进程 + 一个 systemd unit,容器层只增加复杂度;sandbox 直接 `systemd-run --scope -p MemoryMax=… -p CPUQuota=…` 走 cgroup 隔离
- **LLM 并发上限 5**(`bot/helper/im/queue.py:_DEFAULT_LLM_CONCURRENCY`) — 上游 Athenai 速率才是天花板,不是本地资源

---

## 4. 自迭代边界 ⏳ Q2 计划(尚未实装)

> 本节是 Q2 设计意向,**仓库里目前没有 `extensions/` 目录,也没有 sandbox 调度逻辑**。
> 真要做时,先确认是否仍是产品优先级,不要照抄下面的方案。

bot 能自写代码,但只能改**外挂层**,不能改主 bot 源码。

```
helper/
  bot/                  ← 主 bot 框架,人手改,bot 只读
    helper/
      core/
      ingest/
      ...
  extensions/           ← bot 自写代码的沉淀(B-持久 落地)
    daily_reminder.py
    excel_dedup.py
    .attempts/          ← 失败 sandbox 尝试,归档
```

### 4.1 三种自迭代姿势

| 姿势 | 能干什么 | 风险 |
|---|---|---|
| **A. Tool use** | 调 helper 预定义工具集(走 Anthropic tool_use,不是 MCP) | 安全 |
| **B-临时**. 一次性脚本 | sandbox 内 .py 写完跑掉,产物归档 | 中风险,sandbox 隔离 |
| **B-持久**. extensions 落地 | 写 plugin 进 `extensions/`,bot 启动加载 | 高风险,但**只能动 extensions/,不能动 bot/**,且需经 sandbox 跑通验证 |

### 4.2 Plugin 协议

每个 `extensions/*.py` 必须实现:

```python
def register(bot):
    """Called at bot startup. Register hooks, schedules, commands."""
    bot.schedule.daily(hour=9).run(daily_reminder)
    # ...
```

bot 启动时 `extensions/` 全扫一遍,失败的隔离不影响主 bot。

### 4.3 Sandbox 实现

```bash
systemd-run \
    --user=helper-sandbox \
    --scope \
    -p MemoryMax=500M \
    -p CPUQuota=100% \
    -p TimeoutStopSec=300 \
    /path/to/script.py
```

无 Docker。`helper-sandbox` 是受限 system user,只能读 `/var/lib/helper/sandbox-input/`,只能写 `/var/lib/helper/sandbox-output/`。

---

## 5. 本地开发 vs 服务器部署

| 阶段 | 在哪 | 注意 |
|---|---|---|
| 开发主循环 | **本地** | 全部组件可本地起(sqlite + git + FastAPI) |
| Wave IM 测试 | 本地用 mock 事件 | Wave 推不到 localhost,不要为本地开发改 Wave 回调 URL |
| 集成测试 | 本地 + 临时 ssh tunnel(可选) | `ssh -R 8009:localhost:8009 root@10.234.81.212` 把本地 8009 露给服务器 |
| 部署 | 服务器 | 改 Wave 回调 URL → `mhynetcn://10.234.81.212:8009/callback`,deploy systemd unit |

部署清单详见 [`bot/deploy/README.md`](../bot/deploy/README.md)。

---

## 6. 踩坑笔记 — 防回归

栽过的坑, 列在这里防再犯。每条都是真出过事的根因。

### 6.1 改代码前先加日志

线上行为不符合预期时, **先在外部 IO 边界加 WARN 日志再触发一次**, 不要凭推测改代码 ship。诊断口固定 `/var/log/helper/helper.err`。常踩的边界:

- LLM 调用入参 (system + user) / 出参 (raw text)
- Wave 出站: `wave_client.send_message` / `reply_message` 的 `msg_type + content_str`
- DB 写入路径: 任何 fire-and-forget 落表

凭印象改 prompt / 改协议拼装很容易 ship 错版本, 多花一轮日志就能定位真根因。

### 6.2 新加 LLM task 三处同步

加一个 task (例如 `restate_bot_reply`) 必须三处同步:

1. `bot/helper/policy/defaults/llm_routing.yaml` — 默认 seed
2. `bot/var/helper/git-repo/meta/policies/llm_routing.yaml` — 本地 spec repo 镜像
3. **服务器 `/var/lib/helper/git-repo/meta/policies/llm_routing.yaml`** — 运行时实际读这份

漏第三处 → router 抛 `Unknown task type: 'xxx'` → 调用方 except 静默走兜底, 表面"正常"但功能没生效。改完服务器 yaml 要在 spec repo 内 commit (审计需要)。

### 6.3 Wave card markdown 渲染规则

不要凭印象拼协议。Wave 协议两类 markdown 不一样 (KM 文档 `mh8nt9rfdb4u` / `mhywru0a72y0`):

- **native `msg_type=markdown`**: 不支持标题 / 分割线 / 表格 / 代码块
- **card 内嵌 `{tag:"markdown"}` 组件**: 全部支持 (标题 1-6 级 / 表格 GFM / 代码块 / 分割线 `\n -------------- \n`)

需要表格/标题就发 card `{tag:"flow", elements:[{tag:"markdown", text:...}]}`, 别走 native markdown。

### 6.4 协议先看 KM 文档+实测

外部 API (Wave / KM / IAM) **不要凭印象拼 URL 或假设域名互通**。每次接新接口先在 KM 找官方锚点 + curl 实测一次。host 不可假设互通, token 不可假设共用 (Wave 与 KM 同 host 但 token 各自缓存)。

### 6.5 helper.db 路径固化

服务器运行时路径就是 **`/var/lib/helper/helper.db`** (历史误写过 `helper.sqlite`, 任何脚本/排查命令统一用前者)。

### 6.6 bot 自己回复的 raw 不能进检索池

bot 出去的回复落 raw 时 `source_type=im_wave_bot`, ingest 路径必须打 `skipped:bot_reply` 标; 纯 ack 文案 (`@asker 已咨询 @via:` 之类) 干脆不落 raw。新增 ingest 路径要保这个不变量, 否则 bot 回复会被 L1 抽进语料 → 检索池污染 → ask 引用自己说过的话作为依据。

### 6.7 服务器 IDC 入向封 8001, 固化 8009

新部署直接用 8009, 不要再试 8001。Wave 回调 URL 已固化 `mhynetcn://10.234.81.212:8009/callback`。

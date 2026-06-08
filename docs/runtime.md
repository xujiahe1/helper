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

**EntityAlias 自动回填**(M5 用):`bot/helper/im/wave_user.py::get_user_chinese_names()`
按域账号批量(单次 ≤ 200)调 `/openapi/contact/v1/users/get?uid_type=user_id` 拿
`name`(中文名),写 `entity_alias(name=域账号, canonical=中文名, source='auto')`。
两个触发点:
- ask 路径 lazy 拉:asker_domain 在 alias 表查不到 → `ensure_alias_for_domain()` 当场拉一次,失败不阻塞 ask 主链路
- 一次性 backfill:`scripts/backfill_wave_user_aliases.py` 扫 `ask_answers.asker_domain` + `raw_inputs.author_domain` 全集铺满

落库后,scope=entity:<中文名> 的 directive 在该 asker 下次提问时即可被注入(详见 architecture.md §3.7 命中路径第 3 路)。

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
- **不用 Docker** — bot 是单 Python 进程 + 一个 systemd unit,容器层只增加复杂度。M10 Agent Surface 实装时 agent runtime 与 bot 同进程(`claude-agent-sdk` Python import),不引入容器/sandbox,资源兜底走 bot systemd unit 自身的 `MemoryMax` / `CPUQuota`
- **LLM 并发上限 5**(`bot/helper/im/queue.py:_DEFAULT_LLM_CONCURRENCY`) — 上游 Athenai 速率才是天花板,不是本地资源

---

## 4. Agent Surface — 内置 Claude Agent SDK ⏳ M10 计划

> **方向定调**: helper 不自研 agent 执行层(不做 tool runtime / sandbox / extensions/ 外挂层 / bot 自写代码这些)。Excel / 代码 / 文件 / 数据分析 / 自动化任务等执行能力,由**内置 Claude Agent SDK**(Anthropic 官方 Python 包,走 Athenai 网关)提供。helper 只负责: Wave IM IO 适配 + KB/memory/ACL 注入 + agent 工作目录管理 + 产物回贴 + 执行结果回流为 raw。
>
> **Phase 0 可行性已 2026-06-03 实测全绿**(`bot/scripts/poc_agent_athenai.py`),M10 可以排期实装。

### 4.1 用户视角

输入输出口只有 Wave。用户在群里 @helper "帮我把这份数据按部门汇总成 Excel" / "分析一下这段 log" / "生成一份同 spec 一致的审批表":

```
Wave 用户消息
   ↓
helper bot (intent_classify → tool_task)
   ↓
内置 Claude Agent SDK (在 bot 进程内, Python import)
   · cwd = /var/lib/helper/agent-workdir/<task_id>/
   · system prompt = retrieve 结果 + memory directives + ACL 上下文
   · allowed_tools = Read / Write / Edit / Bash / Glob / Grep
   · base_url = Athenai
   ↓ agent 自决调度 (SDK 内置 tool 调用循环, 我们不实现)
产物落 workdir (.xlsx / .py / .md / 任何文件)
   ↓
Wave 文件出站 API 上传
   ↓
helper bot 引用回复原消息, 附文件
   ↓
agent 执行过程作为 source_type='agent_run' 落 raw_inputs (event sourcing 纪律)
```

### 4.2 Phase 0 已验证参数(2026-06-03,`bot/scripts/poc_agent_athenai.py`)

下面这些是**已实测落锤**的接入参数,M10 实装直接照抄,不需要再验:

| 参数 | 值 | 不能错的原因 |
|---|---|---|
| `ANTHROPIC_BASE_URL` | `https://athenai.mihoyo.com`(**不带 /v1**) | 文档明示 + 实测,带 /v1 SDK 会拼错路径 |
| `ANTHROPIC_AUTH_TOKEN` + `ANTHROPIC_API_KEY` | 同值,Athenai API key | SDK 同时读两个 env,两个都设最稳;只设其一可能在不同 SDK 版本退化 |
| `CLAUDE_CODE_ATTRIBUTION_HEADER` | `"0"` | 默认会发动态 header **击穿 Athenai prompt cache**,性能崩 |
| `ANTHROPIC_MODEL` | `claude-sonnet-4-6` | 与 router agent_runtime task 一致 |
| `anthropic-version` header(裸 HTTP 时) | `2023-06-01` | 与 Anthropic 官方一致 |
| `x-api-key` header(裸 HTTP 时) | Athenai key | **不是** `Authorization: Bearer`(Anthropic 协议规定) |

实测结果汇总:

| 风险点 | 验证手段 | 结果 |
|---|---|---|
| tool_use / tool_result 透传 | 裸 HTTP 双轮 `get_weather` 调用 | ✅ stop_reason=tool_use 正常 + tool_result 接续后拿到最终文本 |
| Claude Agent SDK 走 Athenai | `claude-agent-sdk==0.2.88` 实跑文件写入任务 | ✅ 4 轮 tool 调度(Bash→Write→Bash→Text)16.6s 完成 |
| 长链稳定性 + rate limit | 9 轮 increment tool 调度 | ✅ 0 个 429,30.9s 完成 |
| streaming SSE | `stream:true` + 解析 SSE | ✅ `content_block_delta` + `message_stop` 完整 |

### 4.3 实装边界(强约束,不能踩)

- **agent runtime = `claude-agent-sdk` Python 包**,不接 OpenHands / 不接 Claude Code CLI subprocess / 不接其他框架
- **agent 进程 = bot 进程**,共享文件系统,工作目录 `/var/lib/helper/agent-workdir/<task_id>/`
  - 不上 Docker / 不上 systemd-run cgroup / 不上 helper-sandbox user(这些原 §4 设想已废弃)
  - 资源兜底走 bot systemd unit 自身的 `MemoryMax` / `CPUQuota` 即可
- **不在 bot 依赖里直接装 openpyxl / pandas / xlsx 处理库** — agent 自己按需在 workdir 内 `pip install` 到任务级 venv 或临时环境
- **agent 路径只服务用户单任务**,不做批量(批量打标走 qwen `bulk_extract`,Athenai 政策禁止 Claude 批量打标)
- **不在 Wave 之外开第二个 IO 入口** — 没有终端 / 没有 web 上传,所有 agent 任务从 Wave 来回 Wave 去
- **不让 agent 改 bot 源码** — `allowed_tools` 在 SDK options 里限死 cwd 内文件操作;Bash 工具走 `disallowed_tools` 黑名单(`rm -rf /` / `curl 外网` / `sudo` / 写 bot 仓库路径)
- **不让 bot 自写代码沉淀到仓库** — 原 §4 "B-持久 extensions" 设想已废弃,agent 产物只在 workdir 留存 + raw_inputs 回流,不写代码进 helper 仓库

### 4.4 实装拆分(M10)

#### Phase 1 — 文档纠偏(已完成,见本节 + §6.8)

把 extensions / sandbox / "bot 自写代码"等旧设想从所有文档清掉,Agent Surface 写成"M10 待实装"。代码层不动。

#### Phase 2 — 最薄通路 PoC(预计 2-3 天)

**目标**: Wave 收到 "@bot 出个 Excel 测试一下" → agent 跑 → Wave 文件回贴。**不接 KB,不接 memory,先验通路**。

新增模块 `bot/helper/agent/`:
```
bot/helper/agent/
  ├── runtime.py        # Claude Agent SDK 封装, env 注入 (§4.2 表里的 5 个 env)
  ├── workdir.py        # /var/lib/helper/agent-workdir/<task_id>/ 创建 + TTL 清理(默认 7 天)
  ├── hooks.py          # PreToolUse 黑名单 (rm/curl/sudo) + PostToolUse 落 raw_inputs
  └── wave_files.py     # Wave 文件出站 API 封装 (open.hoyowave.com 文件上传 endpoint)
```

改动点:
- `bot/helper/im/bot_routing.py` / `wave_webhook.py`: 加 `tool_task` 分支路由到 agent
- `bot/helper/ingest/intent_classify.py`: 加第 4 类意图 `tool_task`(出 Excel / 跑分析 / 文件处理 / 自动化)
- `bot/helper/policy/defaults/llm_routing.yaml` + `bot/var/.../llm_routing.yaml` + 服务器 yaml(三处同步): 加
  ```yaml
  agent_runtime: { model: claude-sonnet-4-6, provider: anthropic, max_tokens: 8192 }
  ```
- `bot/pyproject.toml`: 加依赖 `claude-agent-sdk>=0.2.88`

依赖装在 helper venv,不装 openpyxl/pandas(agent 自己装到 workdir)。

#### Phase 3 — KB / memory / ACL 注入(预计 1-2 天)

`runtime.py` 在调 SDK 前先做 retrieve:
- 调 `ask/retrieve.py::retrieve_relevant(question)` 拿 spec / section / decision
- 调 `memory/lookup.py` 拿 directive
- ACL 在 retrieve 出口已经过滤过,直接用结果
- 拼成 system_prompt 喂给 ClaudeSDKClient

不给 agent 注册 `helper_kb_search` 这种自定义 tool — 注入路径保持单一,避免双通路歧义。

#### Phase 4 — 安全 hooks 与回流(预计 1-2 天)

- `PreToolUse` hook: Bash 命令走黑名单(`rm -rf /` / `sudo` / `curl http(s)://[非白名单 host]` / 写 bot 仓库路径)
- `PostToolUse` hook: 每一步 tool 调用落 `raw_inputs` (`source_type='agent_run'`,`acl_topic_id` 继承自任务发起人)
- 长任务进度提示: agent run > 30s 时给 Wave 发"正在处理"占位卡片(复用 bot-routing 的 thinking card 机制)
- Wave 文件出站封装: 查 KM `https://km.mihoyo.com/doc/repo/5173`(KM OpenAPI 总目录)找文件上传 endpoint,实测后写 `wave_files.py`

#### Phase 5(可选) — Replay 与 dogfood

agent 任务进周报(`source_type='agent_run'` 的 raw 单独统计成功率 / 失败原因 / 平均耗时)。

### 4.5 重叠模块的处理(防止旧/新路径打架)

| 模块 | 现有职责 | 新方向下定位 |
|---|---|---|
| `llm/router.py` | task → 模型,直接调 LLM | **不动逻辑**。新增 task `agent_runtime` 仅返模型 ID,真正调用方是 SDK |
| `intent_classify` | knowledge_qa / 闲聊 / ingest | 加第 4 类 `tool_task`,与 knowledge_qa 平级互斥 |
| `ask/runtime.py` | retrieve → synth → reply | **不动**,继续管 knowledge_qa 路径;tool_task 不复用 ask |
| `ask/retrieve.py` | 5 类原子 + memory 三路 RRF | **不动**,agent 路径直接复用 retrieve_relevant |
| `memory/lookup.py` | 给 ask 注 directive | 同时给 agent 注 directive,改 1 行调用,不改语义 |
| `acl/`(M8 4 闸) | 入库 + retrieve 出口 + chat_context + ask 入口 | tool_task 入口同样跑 `deny_for_question`(命中受控 topic 直接 deny) |

### 4.6 不做的事(防止 M10 实装时被带回旧路)

- ❌ 不实现 helper 自己的 tool_use 调度循环 — SDK 内置
- ❌ 不实现 systemd-run sandbox / cgroup 隔离
- ❌ 不实现 `extensions/` 外挂层 / Plugin 协议
- ❌ 不让 bot 自写代码沉淀
- ❌ 不在 bot 依赖里装 openpyxl / pandas / xlsx 库
- ❌ 不接 OpenHands / Claude Code CLI subprocess / 其他 agent 框架
- ❌ 不引入 Docker / 容器层
- ❌ Claude Agent SDK 不走 Anthropic 官方网关 — **必须 Athenai**(`ANTHROPIC_BASE_URL` 强制覆盖)
- ❌ 不在 Wave 之外开第二个 IO 入口

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

### 6.5 helper.db 真实路径 + 关键 schema 速查

服务器运行时**实际**库路径: **`/var/lib/helper/helper.db`** (14 MB+,helper:helper)。

⚠️ 同目录可能存在 0 字节 `helper.sqlite` —— 这是被 `sqlite3 /var/lib/helper/helper.sqlite` 之类的命令**自动创建的空 stub**。任何排查脚本必须用 `helper.db`,不要用 `helper.sqlite`。

⚠️ ORM 逻辑列名 ≠ 物理列名 —— 跑裸 SQL 之前先 `PRAGMA table_info(<表>)`。已知错配:

| ORM 字段 | 实际物理列 | 表 |
|---|---|---|
| `ConflictLog.target_slug` | `spec_slug` | conflict_log (历史改名,SQLAlchemy `mapped_column("spec_slug")` 桥接) |

排查任何"DB 里到底有什么"问题前,**先**:

```bash
ssh root@10.234.81.212 "sqlite3 /var/lib/helper/helper.db '.tables'"
ssh root@10.234.81.212 "sqlite3 /var/lib/helper/helper.db 'PRAGMA table_info(<表名>);'"
```

不要凭代码里 ORM 字段名直接拼 SQL。

### 6.6 bot 自己回复的 raw 不能进检索池

bot 出去的回复落 raw 时 `source_type=im_wave_bot`, ingest 路径必须打 `skipped:bot_reply` 标; 纯 ack 文案 (`@asker 已咨询 @via:` 之类) 干脆不落 raw。新增 ingest 路径要保这个不变量, 否则 bot 回复会被 L1 抽进语料 → 检索池污染 → ask 引用自己说过的话作为依据。

### 6.7 服务器 IDC 入向封 8001, 固化 8009

新部署直接用 8009, 不要再试 8001。Wave 回调 URL 已固化 `mhynetcn://10.234.81.212:8009/callback`。

### 6.8 Claude Agent SDK 走 Athenai — 4 个必设 env

M10 实装内置 agent 时, 下面 4 个环境变量是 Phase 0 实测确认的最小可工作集 (`bot/scripts/poc_agent_athenai.py` 2026-06-03 全绿), 任意一个配错都会出隐形故障:

```bash
ANTHROPIC_BASE_URL=https://athenai.mihoyo.com   # 不带 /v1, 带了 SDK 拼路径会错
ANTHROPIC_AUTH_TOKEN=sk-...                      # Athenai key, Claude Code 风格 env
ANTHROPIC_API_KEY=sk-...                         # 同值, 标准 anthropic SDK 读这个
CLAUDE_CODE_ATTRIBUTION_HEADER=0                 # 不设会发动态 header 击穿 prompt cache, 性能崩
```

为什么 AUTH_TOKEN 和 API_KEY 同时设: Claude Agent SDK 底层是 anthropic Python 库, 不同版本读不同 env, 两个都设不会冲突, 只设其一可能在 SDK 升级后失效。模型 ID 用 `claude-sonnet-4-6`, 通过 `llm_routing.yaml::agent_runtime` 任务路由。

### 6.9 Athenai 限流 — agent 长链是安全的, 批量打标禁用

Phase 0 实测: 用户单任务 9 轮 LLM 调用 0 个 429, 30s 内完成。但 Athenai 政策**禁止 Claude 系列模型用于大批量数据打标**(qwen flash 走 `bulk_extract`)。判断边界:

- 用户在 Wave 触发的单 agent 任务(20+ 轮预算): ✅ 走 Claude
- 后台批量给 raw 打 entity / acl / l1: ❌ 必须走 qwen
- Replay / Eval 全量回放: ❌ 必须走 qwen

加新 LLM task 时如果是"对每条数据都跑一遍"的形态, 默认走 qwen, 不走 Claude。

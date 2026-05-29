# Runtime

## 1. 模型分配 (Athenai)

| 档位 | 模型 | 上下文 | 用途 |
|---|---|---|---|
| **主路径(Pro)** | `claude-opus-4-7` | 1M | Ask 推理 / 追问(elicit)/ 自写代码 planner |
| **次路径(Sonnet)** | `claude-sonnet-4-6` | 1M | L1 结构化 / 意图分类 / 同义 judge / 冲突 judge / 定时任务解析 / memory 抽取 |
| **L1 预筛(mini)** | `qwen3.6-flash` | - | 群聊"听"路径关键词没命中时,让小模型快速判 yes/no(全量跑 Sonnet 太烧) |
| **批量后台** | `qwen3.6-flash` | - | 文档批量 ingest 抽事实候选 / Replay judge / L2 候选 entity 批量抽取 |
| **Embedding** | `bge-m3` | - | 检索索引(1024 维,中文表现好,走 OpenAI 兼容 /v1/embeddings) |
| **Rerank** | `Qwen3 Rerank` | - | retrieve > 20 条时启用 |

> **注**: Athenai 文档明确 Claude 系列**禁用大批量数据打标**,所以批量提取必须走 Qwen。

### 1.1 为什么不全用 Claude
- L1 结构化是 deterministic 任务,Sonnet 准确率上限够,**速度 2-3 倍**对"扔进去多久能在 Inbox 看见"体感影响大
- 大批量打标 Claude 政策禁用

### 1.2 为什么主路径不能降到 Sonnet
追问 / Ask 推理 / code_plan = 产品和"普通 RAG bot"的护城河。Opus 在 boundary reasoning / exception handling / self-uncertainty 上对 Sonnet 的优势可感知,这三件是品控生死线。

> **conflict_judge 从 Opus 降到 Sonnet**(2026-05,M6 commit 15ff894):5 类原子统一过 LLM judge 后,调用量上涨明显,而 conflict_judge 的实质是结构化二选一 + 短摘要,Sonnet 完全够。Opus 性价比不再合理。

### 1.3 实现 — model_router

`bot/helper/llm/router.py` 按 task 名路由,模型表外置在 spec git repo 的
`meta/policies/llm_routing.yaml`(默认快照在 `bot/helper/policy/defaults/llm_routing.yaml`),
改路由走 PR + git diff。当前生效的 task → 模型表(节选):

```yaml
ask, elicit, code_plan:                     claude-opus-4-7   (anthropic)
intent_classify, l1_structure, synonym_judge,
conflict_judge, schedule_parse, memory_extract,
acl_tag:                                    claude-sonnet-4-6 (anthropic, max_tokens=64 + 1 retry)
l1_prefilter, bulk_extract:                 qwen3.6-flash     (openai 兼容)
embed_index:                                bge-m3            (openai 兼容,1024 维)
rerank:                                     qwen3-rerank      (openai 兼容)
```

> `acl_tag`(M8)频次:每条新 raw 入库 1 次 + 每次 ask 1 次。max_tokens=64 因为输出只是 topic_id(`ge` / 空串 / `UNCERTAIN`)。失败重试 1 次,仍失败 fallback 走 yaml `default_on_uncertain`(默认空,不强制安全侧)。

API: 走 Athenai `https://athenai.mihoyo.com/v1/messages`(Anthropic 原生兼容)+
`/v1/chat/completions`(OpenAI 兼容,跑 Qwen / bge-m3)。`Authorization: Bearer sk-xxx` header。

---

## 2. Wave IM 集成

> **不走 MCP**。openapi-mcp(常驻 127.0.0.1:5524)只支持登录用户身份,不支持服务端凭据,所以
> bot 出站、入站、事件订阅管理**全部走 Wave 开放平台 HTTP API**(`https://open.hoyowave.com`),
> 凭服务端 `app_id` + `app_secret` 自换 `access_token`。MCP 那个进程仍跑在服务器上(徐叶佳侧
> 装的,给登录用户身份的 desktop agent 用),但**和 bot 运行时没有任何关系**。

### 2.1 出站(bot 调 Wave)

`bot/helper/im/wave_client.py` 直连 `https://open.hoyowave.com`。`access_token` 在剩余有效期
< 30 分钟时自动续期(KM 文档允许双 token 共存)。

| HTTP 接口 | 客户端方法 | 用途 |
|---|---|---|
| POST `/openapi/auth/v1/access_token/internal` | `get_access_token()` | 自建应用换 token,内部缓存自动续期 |
| POST `/openapi/im/v1/message/send` | `send_message()` | 给用户 / 群发会话消息或通知 |
| POST `/openapi/im/v1/message/reply` | `reply_message()` | 引用回复某条消息(quote) — ask / bot-routing 回贴用 |
| POST `/openapi/im/v1/card/update_active` | `update_card_active()` | 主动更新已发的"思考中"卡片(ask 长路径 / bot-routing 收到回执时替换 thinking 卡片) |
| POST `/openapi/contact/v1/user/id_convert` | `convert_user_ids()` / `open_id_to_domain_account()` | **身份映射: union_id ↔ 域账号** |
| POST `/openapi/contact/v1/users/get` | `users_get()` | 拉用户域账号 + 姓名 + 邮箱(身份反查后回填 raw.author_domain) |

后续要用到的(按需扒文档再加):撤回 / reaction 列表 / 群信息 / 文件上传。

### 2.2 入站(Wave 推回调到 bot)

入站是 Wave 平台主动 POST 到我们的 `/callback` 端点(本来就是 HTTP,跟 MCP 无关)。

**部署形态**: bot 在 `10.234.81.212:8009` 监听 `/callback`。Wave 后台回调 URL 配 `mhynetcn://10.234.81.212:8009/callback`(`mhynetcn` = mihoyo 办公网 http scheme,我们的服务器在办公网内,**协议层合法,不需要 https / 不需要走网关反代**)。

> **端口为啥是 8009 不是 8001**:`:8001` 在这台 IDC 服务器的入向被中间网络层封了(本地办公网 8001 通,服务器 IDC 8001 收不到 SYN),换 8009 后 Wave 内网出口直接握手成功(2026-05-28 验证)。新部署直接用 8009,不要再去试 8001。

> 协议参考: [事件订阅概述](https://km.mihoyo.com/doc/mheo000ok1zs) · [事件办公网推送](https://km.mihoyo.com/doc/mh041f0mt47k)

**Endpoint 必做的 5 件事**(任何一件错都会 Wave 报"URL 验证未通过"或重试风暴):

| # | 责任 | 不做的后果 |
|---|---|---|
| 1 | **AES-256-CBC 解密 body** — key 取 `WAVE_CALLBACK_AES_KEY`,iv = key[:16],PKCS7,base64 解码 | 拿不到事件内容 |
| 2 | **签名校验** — `sha256(hoyowave-open-timestamp + hoyowave-open-nonce + raw_body + WAVE_CALLBACK_SIGN_TOKEN)` 对比 `hoyowave-open-signature` header(注意:用**未反序列化的原始 body 字符串**算,反序列化再算会错) | 接受伪造事件 |
| 3 | **校验事件 1 秒内原样返回 challenge** — 配置 URL 时 Wave 推一条 challenge,body 解密后是 `{"challenge": "xxx", ...}`,必须 1s 内 HTTP 200 返回 `{"challenge": "xxx"}` 原样 | URL 配不上去 |
| 4 | **普通事件 1 秒内 HTTP 200 + body 为 `""` 或 `{}`** — 实际处理走后台 async,不要在响应链上做 LLM | Wave 退避重试(10s/30s/3m/1h/6h × 5),触发事件风暴 |
| 5 | **event_id 去重 7.1 小时窗口** — `header.event_id` 写入 sqlite 表 `(event_id PRIMARY KEY, received_at)`,重复 event 直接 200 不处理 | 重试窗口内同一事件被多次执行(发重复消息 / 重复扣 LLM 配额) |

**实现位置**: `bot/helper/im/wave_webhook.py` — FastAPI router,挂在主 bot 进程下,监听 8009 端口(单进程,见 §3.1)。

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
   - **前缀**:替换 thinking 卡片为 markdown `@asker 已咨询 @via:`(私聊不 @ 自己)
   - **原样透传**:把外部 bot 的原 `msg_type + content` 直接转发回原会话(card / rich_text / text 视觉保真)
   - 失败 → 抽 text 兜底
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

四道闸,从入库到出口逐级过滤:

```
[1. 入库打标]  raw 落 raw_inputs (acl_topic_id="")
                    ↓ ingest sink._run_consumers
                acl.tag_raw(raw_id) ── acl_tag LLM 判 topic_id
                    ↓ 同步派生
              l1_items / *_candidates 全部冗余继承同 topic_id
                    ↓
              落库后 retrieve 出口 / chat_context 拼装时 O(1) 列过滤

[2. retrieve 出口] retrieve_relevant(question, asker_domain="alice")
                    ↓ 三路融合后
                filter_hits(asker_domain, hits) ── 反查每条 hit 对应表的
                    ↓                              acl_topic_id, asker 不在
                allowed only                       allowed_domains 整条丢

[3. ask 入口短路] ask(question, asker_domain="alice", chat_id="oc1")
                    ↓ 拼 chat_context (此时已第 4 道过滤)
                deny_for_question(asker, question, chat_context)
                    ↓ acl_tag 跑 (question + chat_context)
                命中且非白名单 → 返 deny_response, 不调主路径 LLM

[4. chat_context 过滤] format_context_block(chat_id, asker_domain="alice")
                    ↓ list_chat_history 拉同 chat_id 最近 16 条
                按 asker_domain 过滤 ── 跳过 acl_topic_id != "" 且 asker 不在
                    ↓                    allowed_domains 的 raw
                outsider 看到的群历史 = 被 ACL 裁剪过的世界

[5. 出口 scrub_output] ask 主路径生成 answer 后
                    ↓
                scrub_output(asker, answer_text) ── 文本含 yaml output_blocklist_terms
                    ↓                                的词且 asker 非白名单
                整段替换为 deny_response, 兜底防 LLM 凭参数知识脑补
```

任何一道命中即生效,后续闸不再触发。前 4 道是数据流防漏,第 5 道是模型幻觉兜底。

代码: `bot/helper/acl/`(policy.py + tagger.py)+ `bot/helper/policy/loader.py::TopicAcl` + `bot/helper/policy/defaults/topic_acl.yaml`(seed)。yaml 真实生效路径在 spec git repo `meta/policies/topic_acl.yaml`,owner-only 修改靠 git PR + repo 权限。

CLI: `helper acl-backfill`(批量给存量 raw 打标)/ `helper acl-status`(看当前白名单 / topic 列表)。

---

## 3. 服务器约束

`10.234.81.212`: **2 vCPU / 3.6G RAM / 40G 磁盘 / Ubuntu 22.04(不可升配)**

已运行: nginx 1.18 / openapi-mcp(98M,登录用户身份的 desktop agent 用,bot 不依赖)/ monitoring exporters / OS。可用内存 ≈ 2.7G。

### 3.1 内存预算

| 组件 | 预算 | 备注 |
|---|---|---|
| 已有(nginx + openapi-mcp + monitor) | ~250M | 不动 — openapi-mcp 是徐叶佳侧装的,bot 不连它 |
| Bot 主进程(core + IM webhook + Browser Web 同 FastAPI app) | 900M | 单进程 + asyncio,含 apscheduler / cryptography(AES) |
| jieba 字典(M7 FTS5 中文分词) | ~50M | 启动加载,常驻主进程 |
| Sandbox(⏳ Q2) | **峰值 800M / 严格串行 1 个** | systemd-run + cgroup;尚未实装 |
| SQLite + sqlite-vec | 200M | 嵌入主进程 |
| Buffer | 500M | OOM 防护 |
| **合计** | **~2.7G** | 紧但够 |

### 3.2 妥协纪律(为不 OOM)

- 全部单进程 + asyncio,不开 worker 池
- 文档批量 ingest 强制串行(一次一篇)
- LLM / Embedding 全走 Athenai API,不在本地跑模型
- Sandbox 不并发,同时只跑 1 个外挂任务
- 流式处理大文档,不一次性加载

### 3.3 关键决策

- **不用 Docker** — baseline 500M+,占不起。Sandbox 用 `systemd-run --user=helper-sandbox --scope -p MemoryMax=500M -p CPUQuota=100%` 替代
- **不用 Postgres** — 用 SQLite,vector 走 sqlite-vec 扩展
- **不在本地跑模型** — 全走 Athenai API,只做 client

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

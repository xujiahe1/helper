# Runtime

## 1. 模型分配 (Athenai)

| 档位 | 模型 | 上下文 | 用途 |
|---|---|---|---|
| **主路径(Pro)** | `claude-opus-4-7` | 1M | Ask 推理 / 追问 / 冲突 judge / 自写代码 planner / IM @bot 实时答 |
| **次路径(Basic)** | `claude-sonnet-4-6` | 1M | 单条判断 L1 结构化 / 意图分类 / agent loop 常规步骤 |
| **批量后台** | `qwen3.6-flash` 或 `gpt-5.4-mini` | - | 大批量文档 ingest / 候选事实抽取 |
| **Embedding** | `text-embedding-3-large` | - | 检索索引(3072 维) |
| **Rerank** | `Qwen3 Rerank` | - | retrieve > 20 条时启用 |

> **注**: Athenai 文档明确 Claude 系列**禁用大批量数据打标**,所以批量提取必须走 Qwen / GPT-mini。

### 1.1 为什么不全用 Claude
- L1 结构化是 deterministic 任务,Sonnet 准确率上限够,**速度 2-3 倍**对"扔进去多久能在 Inbox 看见"体感影响大
- 大批量打标 Claude 政策禁用

### 1.2 为什么主路径不能降到 Sonnet
追问/冲突 judge/Ask 推理 = 产品和"普通 RAG bot"的护城河。Opus 在 boundary reasoning / exception handling / self-uncertainty 上对 Sonnet 的优势可感知,这三件是品控生死线。

### 1.3 实现 — model_router

`bot/helper/router/model_router.py`(待实现)按 task_type 路由:

```python
TASK_MODEL = {
    "ask":              "claude-opus-4-7",
    "elicit":           "claude-opus-4-7",
    "conflict_judge":   "claude-opus-4-7",
    "code_plan":        "claude-opus-4-7",
    "intent_classify":  "claude-sonnet-4-6",
    "l1_structure":     "claude-sonnet-4-6",
    "bulk_extract":     "qwen3.6-flash",
    "embedding":        "text-embedding-3-large",
    "rerank":           "qwen3-rerank",
}
```

API: 走 Athenai `https://athenai.mihoyo.com/v1/messages`(Anthropic 原生兼容)+ `/v1/chat/completions`(OpenAI 兼容,跑 Qwen/GPT)。`Authorization: Bearer sk-xxx` header。

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
| POST `/openapi/contact/v1/user/id_convert` | `convert_user_ids()` / `open_id_to_domain_account()` | **身份映射: union_id ↔ 域账号** |

后续要用到的(按需扒文档再加):回复消息 / 撤回 / 卡片更新 / reaction 列表 / 群信息 / 文件上传。

### 2.2 入站(Wave 推回调到 bot)

入站是 Wave 平台主动 POST 到我们的 `/callback` 端点(本来就是 HTTP,跟 MCP 无关)。

**部署形态**: bot 在 `10.234.81.212:8001` 监听 `/callback`。Wave 后台回调 URL 配 `mhynetcn://10.234.81.212:8001/callback`(`mhynetcn` = mihoyo 办公网 http scheme,我们的服务器在办公网内,**协议层合法,不需要 https / 不需要走网关反代**)。

> 协议参考: [事件订阅概述](https://km.mihoyo.com/doc/mheo000ok1zs) · [事件办公网推送](https://km.mihoyo.com/doc/mh041f0mt47k)

**Endpoint 必做的 5 件事**(任何一件错都会 Wave 报"URL 验证未通过"或重试风暴):

| # | 责任 | 不做的后果 |
|---|---|---|
| 1 | **AES-256-CBC 解密 body** — key 取 `WAVE_CALLBACK_AES_KEY`,iv = key[:16],PKCS7,base64 解码 | 拿不到事件内容 |
| 2 | **签名校验** — `sha256(hoyowave-open-timestamp + hoyowave-open-nonce + raw_body + WAVE_CALLBACK_SIGN_TOKEN)` 对比 `hoyowave-open-signature` header(注意:用**未反序列化的原始 body 字符串**算,反序列化再算会错) | 接受伪造事件 |
| 3 | **校验事件 1 秒内原样返回 challenge** — 配置 URL 时 Wave 推一条 challenge,body 解密后是 `{"challenge": "xxx", ...}`,必须 1s 内 HTTP 200 返回 `{"challenge": "xxx"}` 原样 | URL 配不上去 |
| 4 | **普通事件 1 秒内 HTTP 200 + body 为 `""` 或 `{}`** — 实际处理走后台 async,不要在响应链上做 LLM | Wave 退避重试(10s/30s/3m/1h/6h × 5),触发事件风暴 |
| 5 | **event_id 去重 7.1 小时窗口** — `header.event_id` 写入 sqlite 表 `(event_id PRIMARY KEY, received_at)`,重复 event 直接 200 不处理 | 重试窗口内同一事件被多次执行(发重复消息 / 重复扣 LLM 配额) |

**实现位置**: `bot/helper/im/wave_webhook.py` — FastAPI router,挂在 IM Adapter 进程下(8001 端口)。

**密钥来源**: 本地开发用 `bot/.env`,服务器生产从 `/etc/helper/wave.env`(chmod 600 root only)读,**绝不入仓库**。

**为什么不分 IM Adapter 独立进程**: 早期版本(M1)bot core 和 webhook 跑同一个 FastAPI app(同进程不同 router),省内存。M2 流量上来再拆。

### 2.3 群 listen 边界

- 默认开(因为 bot 不主动加群,**能进的群 = 应该听的群**)
- 群消息 → 进 Raw Input Store 但**不调 LLM**(成本控制)
- 触发 LLM 处理: @bot / 转发消息 / 显式开启某群的"主动处理"

### 2.4 身份映射

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

---

## 3. 服务器约束

`10.234.81.212`: **2 vCPU / 3.6G RAM / 40G 磁盘 / Ubuntu 22.04(不可升配)**

已运行: nginx 1.18 / openapi-mcp(98M,登录用户身份的 desktop agent 用,bot 不依赖)/ monitoring exporters / OS。可用内存 ≈ 2.7G。

### 3.1 内存预算

| 组件 | 预算 | 备注 |
|---|---|---|
| 已有(nginx + openapi-mcp + monitor) | ~250M | 不动 — openapi-mcp 是徐叶佳侧装的,bot 不连它 |
| Bot Core | 500M | 单进程 + asyncio |
| IM Adapter (FastAPI webhook) | 200M | 单进程 |
| Backend Web (FastAPI + 静态) | 200M | 单进程 |
| Sandbox | **峰值 800M / 严格串行 1 个** | systemd-run + cgroup |
| SQLite + sqlite-vec | 200M | 嵌入 |
| Buffer | 500M | OOM 防护 |
| **合计** | **~2.6G** | 紧但够 |

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

## 4. 自迭代边界(Q2 落地)

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
| 集成测试 | 本地 + 临时 ssh tunnel(可选) | `ssh -R 8001:localhost:8001 root@10.234.81.212` 把本地 8001 露给服务器 |
| 部署 | 服务器 | 改 Wave 回调 URL → `mhynetcn://10.234.81.212:8001/callback`,deploy systemd unit |

部署清单(M2/M3 才用,现在不动):
- 服务器装 Python 3.10+(已有)、sqlite-vec 扩展
- 创建 `/var/lib/helper/`、`helper-sandbox` user
- 部署 `bot/`,起 systemd unit
- nginx 加配置反代 8001/8002

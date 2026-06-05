# Helper 项目

> 内部"专家决策规约工厂"。把企业内专家的内隐判断,通过触发式 articulation + 5 层知识化变换,沉淀为版本化、可审计、可被 agent 直接消费的决策规约,从而让 agent 在该岗位/领域做出近似专家的决策。

> **领域无关**。设计上不为任何具体岗位定制,产品 IP 在"知识化的方法",不在"某个领域的知识"。

---

## 一、定位

- 不和基础大模型比能力 → 模型可替换,我们押 schema / spec 的持久化
- 不和 OSS agent 框架比通用能力 → agent runtime 用最强的现成方案,IP 在 spec store
- 不做领域定制项目 → 跨领域复用率 ≥ 70% 是"产品 vs 定制"的分水岭

详见 `docs/architecture.md` §1。

---

## 二、文档地图

**面向产品**(无技术语言,优先读这份):
- [`docs/产品逻辑.md`](docs/产品逻辑.md) — 业务化全描述 · 流水线 · 5 surfaces · 多专家 · 验收节奏

**面向开发**:
- [`docs/architecture.md`](docs/architecture.md) — 产品形态 · 模块拆分 · 5 层知识化 · 存储设计 · 5 个押注
- [`docs/runtime.md`](docs/runtime.md) — 模型路由 (Athenai) · Wave IM 集成 · 服务器约束 · Agent Surface 接入策略 (M10 内置 Claude Agent SDK)
- [`docs/roadmap.md`](docs/roadmap.md) — Month 1/2/3 节奏 · kill 条件 · 当前 open 问题 · dogfood 策略

代码骨架:
- `bot/` — Python 实现根(本地优先,部署目标是 10.234.81.212)

---

## 三、保留基建(不重建,直接复用)

| 项 | 值 |
|---|---|
| 服务器 | `10.234.81.212` Ubuntu 22.04 / **2C 15G 40G** / SSH: `ssh root@10.234.81.212`(直连 root) |
| Wave bot APP_ID | `cli_d172001413a848689fa9dbe1cf03eafa`(secret/aes/token 在服务器 `/etc/helper/wave.env`,root only,**不入仓库**) |
| Wave 回调 URL | `mhynetcn://10.234.81.212:8009/callback` |
| Wave / KM 开放平台 API | `https://open.hoyowave.com`(出站走这,服务端 app_id+app_secret 自换 access_token;KM 同 host,token 独立缓存) |
| Athenai API | `https://athenai.mihoyo.com`(兼容 Anthropic `/v1/messages`) |

> bot 全链路 0 MCP — Wave/KM 全部走 HTTP API。服务器上的 openapi-mcp 是给登录用户身份的 desktop agent 用的,bot 不连。

---

## 四、协作约定

- 不确定先问,不要假设后执行
- 涉及多文件 / 架构变更先出方案再动手,只有单行级别明确修改可直接动手
- 检查 / 排查任务必须覆盖全部相关文件,做不到要明确说明遗漏范围
- 工具调用经济性: 已知路径直接 Read,≥3 个陌生文件探索用 Agent(Explore),不滥用搜索
- **部署纪律**: 本地开发优先,服务器是部署目标不是开发环境;改 Wave 回调 URL、deploy systemd unit 等线上动作只在准备部署时做
- **领域纪律**: 任何"为某领域优化"的设计都需要先停下来问"这个能复用到其他领域吗",不能则不做
- **诊断先于推测**: 线上行为不符合预期, **先在外部 IO 边界 (LLM 入参出参 / Wave 出站 / DB 写入) 加 WARN 日志再触发一次**, 不要凭推测改代码 ship。诊断口 `/var/log/helper/helper.err`。
- **DB 排查纪律**: 服务器运行时库 = `/var/lib/helper/helper.db` (**不**是 `helper.sqlite` —— 后者是 `sqlite3` 命令意外创建的空 stub,看到了立刻 `rm`)。跑裸 SQL 前先 `.tables` + `PRAGMA table_info(<表>)`,**不要凭 ORM 字段名拼物理列**(已知 `ConflictLog.target_slug` 物理列叫 `spec_slug`)。详见 `docs/runtime.md` §6.5。
- **新 LLM task 三处同步**: 加 task 必须同时改 `bot/helper/policy/defaults/llm_routing.yaml` + `bot/var/helper/git-repo/meta/policies/llm_routing.yaml` + **服务器 `/var/lib/helper/git-repo/meta/policies/llm_routing.yaml`** (运行时实际读这份)。漏第三处 → router 抛 `Unknown task type` → 调用方静默走兜底。
- **完整踩坑清单**: `docs/runtime.md` §6 — 加新功能前过一遍。

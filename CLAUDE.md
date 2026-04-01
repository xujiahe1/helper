# Helper 项目 - 鳕鱼助理

> AI 驱动的产品经理助手工具

## 项目概述

**鳕鱼助理**（`xueyu-assistant`）是一个面向产品经理的 AI 聊天助手，核心能力包括：多角色对话、PRD 知识库构建与实体认知、引导式 PRD 撰写、KM文档读写、Excel 数据处理。

- **技术栈**: React 18 + TypeScript + Vite 5 + Zustand 4 + TailwindCSS 3
- **代码量**: ~16,000 行（55 个源文件）
- **持久化**: localStorage（对话 + PRD 认知层），IndexedDB（附件二进制）

---

## 核心产品逻辑

### 一、对话流程全链路

用户发送一条消息后，系统按以下管线处理：

```
用户输入（文本 + 附件）
  │
  ├── ① 写作意图检测
  │     检测 URL（km.mihoyo.com / hoyowave.com）或 ~30 个写作关键词
  │     命中 → 创建引导式 PRD 会话，自动推断写入目标
  │     ├── 有文档链接 → existing_doc 模式
  │     └── 无链接 → new_doc 模式
  │
  ├── ② System Prompt 拼装（10 个可选段落，按需注入）
  │     ① 输出格式偏好
  │     ② 自定义 Prompt 或预设角色的 systemPrompt
  │     ③ 手动关联文档的检索内容
  │     ④ PRD 认知层自动关联内容（实体匹配 + 文档片段）
  │     ⑤ 影响面分析的实体关系知识
  │     ⑥ Excel 操作指令模板
  │     ⑦ MCP 文档写作规范 + 写作规划模板
  │     ⑧ 超长文档内容定位指南
  │     ⑨ 文档写作意图检测提示
  │     ⑩ 标题生成指令（仅首轮）
  │     各段落之间用 ======== 分隔
  │
  ├── ③ 流式对话执行（最多 100 轮工具循环）
  │     调用 LLM streamChat()
  │     ├── 纯文本回复 → 结束
  │     └── 含 tool_calls → 执行 MCP 工具 → 将结果追加到消息 → 再次调用
  │         每轮检查：循环检测 / 上下文压缩 / 写作规划提取
  │
  └── ④ 后处理
        ├── 标题提取：从 <title> 标签中提取对话标题
        ├── Excel 执行：解析 ```excel-ops 代码块，前端执行数据操作
        └── Agent Action 执行：解析 ```agent-action 代码块，分发引导式 PRD 动作
```

### 二、上下文压缩策略

系统有两套独立的上下文管理策略，分别用于**普通对话**和**引导式写作**：

#### 2.1 普通对话 — 工具调用场景压缩

**触发条件**：工具调用轮次超过 5 轮时自动启用

**分层保留规则**：

| 消息类型 | 处理方式 |
|---------|---------|
| System Prompt | 始终保留；写作规划（`<writing-plan>`）自动追加到末尾 |
| 用户首条消息 | 始终保留（保留原始意图） |
| 最近 5 轮的工具消息 | 完整保留 |
| 超过 5 轮的工具结果 | 按类型差异化压缩 |
| 进度摘要 | 保留最近 10 条操作记录，注入到 system 之后 |

**工具结果压缩规则**：

| 工具类型 | 压缩方式 |
|---------|---------|
| `create*` | 提取 doc_id → `✅ 创建成功，doc_id=xxx` |
| `get_doc*` | 只保留文档目录结构（标题行） |
| `append/update/write/edit` | `✅ 写入成功 (锚点: xxx)` 或 `❌ 写入失败: ...` |
| `retrieve/search` | `🔍 检索到 N 个相关片段：标题1、标题2...` |
| 其他 | 截断到 300 字符 |

**进度摘要**：系统自动维护一个进度列表，格式为 `✅ tool_name (参数) - 成功/失败`，在超过 5 轮后注入到 system 消息之后，帮助 AI 理解已完成的工作。

#### 2.2 引导式写作 — 工作记忆窗口

| 层 | 内容 | 是否压缩 |
|----|------|---------|
| 固定层 | System Prompt（阶段指令 + 模块状态 + 已完成摘要） | 永不压缩 |
| 工作记忆 | 最近 15 轮对话 | 单条 ≤ 4000 字符，agent-action 块清除 |
| 早期压缩 | 超出 15 轮的部分 | 只保留最近 5 条用户消息各 60 字符关键词 |
| 过滤 | 纯卡片消息、空消息 | 不占工作记忆名额 |

#### 2.3 写作规划持久化

AI 可在回复中输出 `<writing-plan>` 标签声明当前写作规划（目标 + 结构大纲 + 关键约束）。系统自动提取后，在后续每轮都注入到 System Prompt 末尾，确保 AI 在长文档写作中不会"迷失方向"。AI 可随时重新输出该标签来更新规划。

### 三、工具调用链（MCP）

#### 3.1 调用流程

```
AI 回复含 tool_calls
  │
  ├── 循环检测（最近 5 次调用窗口，连续 3 次相同签名 → 跳过）
  │     签名 = 工具名 + "::" + 参数JSON(key 排序)
  │     跳过时不终止流程，而是返回反馈让 AI 调整策略
  │
  ├── 执行 MCP 工具（JSON-RPC 2.0 协议）
  │     ├── 普通工具：5 分钟超时
  │     └── 长时工具（文档读写/检索）：60 分钟超时
  │
  ├── 实时进度展示
  │     每秒更新执行耗时（⏳ 执行中… Ns / Nm Ns）
  │     完成后显示 ✅/❌ + 耗时
  │
  └── 结果追加到 messages → 进入下一轮 streamChat
```

#### 3.2 MCP 会话管理

- **协议版本**: `2025-03-26`
- **初始化**: `initialize` → `notifications/initialized`，缓存 session-id
- **重试**: 指数退避，最多 3 次；网络超时/502/503/504/429 可重试
- **会话恢复**: 遇到会话级错误码（`-32001/-32002/-32600`）自动清除会话并重新初始化
- **响应格式**: 兼容 SSE（`event: message\ndata: {...}`）和纯 JSON 两种格式
- **工具格式转换**: MCP 工具 → OpenAI function calling 格式，自动过滤框架内部参数（`context`）

#### 3.3 循环检测详细机制

检测到循环后的行为（不是简单终止）：

- 检查历次相同调用的成功/失败状态
- **全部成功过** → 提示"检查当前文档状态后继续下一部分"
- **有失败记录** → 提示"尝试不同方式（换锚点、拆分内容）"
- 将跳过反馈作为 tool result 返回 AI，让 AI 自主调整策略

### 四、PRD 认知层

PRD 认知层是一个三层实体模型，为对话提供结构化的产品知识背景：

```
原始实体 PrdEntity
  → 归一化实体 NormalizedEntity（多面体模型）
    → 实体关系 EntityRelation（含冲突检测）
```

#### 4.1 文档处理管线（"重新分析"触发）

```
第一阶段：逐文档实体提取
  ├── 获取文档内容（KM API 获取标题 + MCP 获取正文）
  │     ├── 普通文档: get_doc_detail (plain_text)
  │     └── 表格文档: get_spreadsheet_sheets + get_spreadsheet_resource
  ├── 短文档（≤40000 字符）→ 直接 LLM 提取
  └── 长文档（>40000 字符）→ 分块处理
        ├── 按 5 种标题格式拆分（Markdown / 数字序号 / 中文序号 / 括号 / 方括号）
        ├── 小块合并 + 大块二次拆分
        ├── 并行处理所有分块
        └── 同名实体描述合并去重
        
第二阶段：跨文档分析
  ├── 前端计算：提取候选词（2-8 字中文词 + 英文词）
  ├── 筛选：≥30% 文档出现 + 单篇 ≥2 次 + 不在已有实体中
  └── LLM 验证：候选词是否有业务含义 → 补充到对应文档

第三阶段：实体归一化
  ├── 按名称分组构建版本列表
  ├── LLM 同义词识别（如"域账号"↔"AD 账号"）→ 合并
  ├── 恢复用户手动设置（别名、canonicalName、冲突解决）
  └── LLM 冲突检测（多版本描述是否矛盾）

第四阶段：关系提取
  ├── 逐文档 LLM 提取实体间关系 → 双向存储
  ├── 去重合并（相同目标+类型+方向）
  └── LLM 关系冲突检测（同一对实体间的矛盾关系）
```

#### 4.2 多面体模型

每个归一化实体的多个版本按**面向（Aspect）**分类：

| 面向 | 含义 | 冲突判断 |
|------|------|---------|
| `definition` | 基础定义——"X 是什么" | 多个定义矛盾 → 冲突 |
| `usage` | 使用场景——"X 怎么用" | 天然可累加，不视为冲突 |
| `implementation` | 技术实现——"X 如何实现" | 天然可累加，不视为冲突 |
| `config` | 配置项——"X 的参数" | 标记为"上下文相关"（不同场景配置不同） |
| `permission` | 权限角色——"X 的权限" | 按面向判断 |
| `integration` | 集成对接——"X 与 Y 如何对接" | 按面向判断 |
| `history` | 历史变更——"X 的版本演进" | 按面向判断 |
| `other` | 其他 | 兜底分类 |

**冲突分级**：`definition_conflict`（定义矛盾）、`config_conflict`（配置差异）、`context_dependent`（上下文相关），避免一刀切标记冲突。

#### 4.3 知识库管理

- 文档按知识库（`KnowledgeBase`）分区
- **管理视图**：单选一个知识库进行操作（添加文档、归一化、关系提取）
- **对话引用**：多选知识库（默认全选），合并所有选中知识库的文档参与匹配
- 删除知识库时，其下文档一并删除

#### 4.4 手动优先原则

所有自动流程都保留用户的手动操作：
- 手动添加的实体在重新分析时不被覆盖
- 手动设置的别名、canonicalName 在归一化后恢复
- 手动合并/拆分的实体不被自动归一化覆盖
- 用户的冲突解决记录在重新分析后恢复

### 五、对话时的知识引用

用户发送消息时，如果开启了 PRD 认知层，系统会自动检索相关知识注入 System Prompt：

#### 5.1 匹配模式选择

```
检测用户输入
  │
  ├── 条件判断：有影响面关键词 OR（实体有关系 且 实体数 ≥ 2）
  │     ├── 满足 → 语义增强模式
  │     └── 不满足 → 基础增强模式
  │
  ├── 语义增强模式（调用 LLM）
  │     ├── LLM 提取用户意图（概念、问题类型、是否影响分析）
  │     ├── 概念与实体名/别名语义匹配（精确 1.0 / 模糊 0.7）
  │     ├── BFS 沿关系链路扩展（1-2 层，得分衰减 0.2，下限 0.3）
  │     ├── 按文档累加得分排序
  │     └── 按问题类型动态决定召回量（影响分析 10 篇 / 配置 6 篇 / 一般 5 篇）
  │
  └── 基础增强模式（纯前端计算）
        ├── 分词 + N-gram 提取关键词
        ├── 三层匹配：精确名称(1.0) → 别名(1.0) → 模糊包含(0.6)
        ├── 有冲突的实体额外加分(+0.3)
        └── 返回 top 3 匹配文档
```

#### 5.2 文档内容检索

匹配到文档后，按优先级获取内容：

1. **API 检索**：调用KM文档检索 API（`top_k: 20`），用用户输入 + 最近 6 条消息摘要作为 query
2. **本地补充**：未检索到的文档，尝试使用缓存的 `rawContent`（截取前 3000 字符）
3. **MCP 兜底**：表格类文档如果本地无内容，通过 MCP 重新获取

#### 5.3 增强上下文构建

注入 System Prompt 的上下文按面向组织：

```
## 实体：域账号
别名：AD 账号、ActiveDirectory 账号

### 📌 基础定义
用于统一身份认证的账号...
来源：文档 B (2026/3/20)

### 📝 使用场景（3 篇文档提及）
- 用于登录内部系统... — 文档 A
- 用于权限管理... — 文档 C

### ⚙️ 配置项 ⚠️ 不同场景有不同配置
- 超时时间 30s (文档 A)
- 超时时间 60s (文档 D)

### ⚠️ 注意：存在未解决的冲突
- 基础定义：文档 B 和文档 C 对域账号的定义存在矛盾

### 相关关系
- 域账号 → 依赖 → 认证中心
```

面向截断限制：`definition` 不截断、`usage/implementation/permission/integration` 截断 100 字、`config` 截断 150 字。

#### 5.4 影响面分析

当用户询问影响面类问题时（"改了 X 之后会影响什么"），独立触发关系链分析：

```
匹配主实体 → 获取直接关系（第 1 层）
  → 展开关联实体 → 获取间接关系（第 2 层，排除回环，最多 20 条）
    → 格式化为"实体 → 关系类型 → 目标实体（来源文档）"注入 system prompt
```

检测关键词：影响面 / 影响范围 / 改了之后 / 连带影响 / 波及 等。

### 六、引导式 PRD 撰写

当检测到用户的写作意图时，自动进入引导式模式，按阶段驱动 AI 完成 PRD 撰写：

#### 6.1 四阶段流程

```
阶段 1：确定写入目标
  ├── AI 推断 → 输出 set_write_target action
  └── existing_doc 模式：AI 读取文档结构 → 输出 import_doc_structure → 导入已有章节

阶段 2：规划模块
  ├── AI 列出 2-5 个模块 → 输出 register_feature action
  └── 用户可在 UI 上编辑/添加/删除/排序模块

阶段 3：撰写内容
  ├── AI 逐模块撰写
  ├── 写完 → 输出 lock_feature action（保存摘要，状态变为 ✅）
  └── 可选 → 输出 write_doc action → 通过 MCP 写入KM文档

阶段 4：修正对话
  ├── 用户提出修改 → AI 先用 1 个问题确认范围
  ├── 大改先列修改计划 → 用户确认后执行
  └── 改完简短确认："已修改 XXX，你看下是否 OK？"
```

#### 6.2 阶段化 Prompt 注入

System Prompt 根据当前状态动态注入不同指令段，避免 prompt 臃肿：

| 条件 | 注入内容 |
|------|---------|
| 无 writeTarget | "确定写入目标"指令 |
| 有目标，无模块 | "规划写作内容"指令 |
| 有模块，未锁定 | "撰写内容"指令 |
| 有已锁定模块 | "继续撰写或处理修改"指令 |

已完成的模块以摘要形式（~150 字）常驻 System Prompt，不占工作记忆。

#### 6.3 Agent Action 机制

AI 在回复末尾通过 ` ```agent-action ` 代码块输出结构化指令，前端解析后执行：

| Action | 效果 |
|--------|------|
| `set_write_target` | 设置/变更写入目标（变更时弹迁移确认卡片） |
| `register_feature` | 注册模块到进度追踪 |
| `lock_feature` | 锁定模块，保存摘要 |
| `ask` | 生成追问卡片（含问题列表） |
| `confirm_feature` | 生成确认卡片 |
| `write_doc` | 通过 MCP 逐章节写入KM文档（含锚点管理） |
| `update_draft` | 更新右侧预览面板 |
| `import_doc_structure` | 导入已有文档的章节为 locked 模块 |

Agent Action 在下一轮对话中会从历史消息中清除，避免 AI 重复输出。

#### 6.4 写入目标变更

当用户中途更换目标文档时：
1. 检查旧文档已写入章节数
2. 检查新文档是否有同名章节（冲突）
3. 有记录或冲突 → 弹出变更确认卡片
4. 用户选择迁移策略（迁移/不迁移 + 覆盖/跳过冲突章节）
5. 执行实际迁移

#### 6.5 第一轮保护

引导模式第一轮对话会过滤掉写入类 MCP 工具（`edit_document`/`create_document` 等），确保 AI 先完成规划再动手写。

### 七、LLM 调用体系

系统中 LLM 调用分为两大类：

#### 7.1 对话级调用（流式）

使用 `streamChat()`，通过 `AsyncGenerator` 逐 chunk 产出：

| 参数 | 普通模型 | Claude 4.5 系列 |
|------|---------|----------------|
| `max_tokens` | 65536 | 32000 |
| `thinking.budget_tokens` | 64000 | 32000 |
| `temperature`（普通） | 0.7 | 0.7 |
| `temperature`（thinking） | 1 | 1 |

**Tool calls 累积**：通过 Map 按 index 累积分片的 tool call 参数，流结束后一次性返回。

**双层超时**：fetch 请求超时 + 每次 `reader.read()` 的空闲超时。普通对话空闲 2 分钟，MCP 模式空闲 30 分钟。

**重试策略**：仅 429/502/503 触发重试，最多 2 次，间隔递增。

#### 7.2 系统级调用（非流式）

使用 `callLLM()`，默认参数保守（`maxTokens: 2048, temperature: 0.2`），适合结构化输出：

| 调用场景 | 参数覆盖 | 用途 |
|---------|---------|------|
| 实体提取 | maxTokens: 16384, temp: 0.3 | 从文档中提取业务实体 |
| 跨文档验证 | maxTokens: 2048 | 验证候选词是否有业务含义 |
| 同义词识别 | maxTokens: 2048 | 识别实体别名 |
| 冲突检测 | maxTokens: 512 | 判断多版本描述是否矛盾 |
| 关系提取 | 默认参数 | 从文档中提取实体间关系 |
| 关系冲突检测 | maxTokens: 512 | 判断关系是否矛盾 |
| 语义意图提取 | 默认参数 | 理解用户查询意图（概念、问题类型） |

系统级调用使用 `settings.systemModel`（默认 Claude 4.5 Opus），与对话模型独立配置。

#### 7.3 JSON 提取容错

LLM 返回的 JSON 通过 6 种策略依次尝试解析：正则匹配 → Markdown 代码块 → 首尾括号定位 → 注释/尾随逗号清理 → 裸双引号修复 → 截断 JSON 恢复。确保即使 LLM 输出格式不完美也能稳定提取结果。

### 八、其他核心功能

#### 8.1 预设角色

| id | 名称 | 核心能力 |
|----|------|---------|
| `prd` | 撰写 PRD | 含长文档检索策略、写入策略、结构规范 |
| `data` | 数据处理 | Excel 数据分析与处理 |
| `review` | 方案评审 | 商业价值/用户体验/技术可行性/风险控制 |
| `minutes` | 纪要整理 | 录音/笔记 → 结构化纪要 + 行动项 |
| `translate` | 外语翻译 | 中英日韩互译 |

#### 8.2 Excel 处理

AI 在回复中输出 ` ```excel-ops ` 代码块，前端解析并执行数据操作（筛选、排序、聚合等），结果直接更新到消息中的表格预览。

#### 8.3 附件处理

| 类型 | 处理方式 |
|------|---------|
| 图片 | 直接作为 `image_url` 传给多模态模型 |
| PDF | 三级降级链：渲染为图片 → 文本提取 → 提示扫描件 |
| Excel | 解析为工作表摘要文本（表名 + 表头 + 行数据） |

附件二进制存储在 IndexedDB，不进入 localStorage 持久化。

#### 8.4 分屏对话

支持同时打开最多 3 个独立的对话窗口，每个有独立的 conversationId。

#### 8.5 图片生成

使用专用模型（`Google/gemini-3-pro-image-preview`），支持 10 种宽高比和 3 种尺寸（1K/2K/4K），非流式调用 + 重试。

---

## 架构参考

### 目录结构

```
src/
├── App.tsx                        # 应用入口（侧边栏 + 主视图 + 分屏）
├── store.ts                       # 主 Store：对话状态、消息发送、MCP 调用、PRD 匹配
├── types.ts                       # 类型定义 + 预设角色 + 默认配置
│
├── components/
│   ├── ChatArea.tsx               # 聊天主区域（输入框 + 消息列表 + 附件）
│   ├── MessageBubble.tsx          # 消息气泡（Markdown + thinking + 工具进度）
│   ├── SourceIndicator.tsx        # 来源指示器（PRD 匹配 + 文档关联）
│   ├── DocBar.tsx                 # 文档关联栏
│   ├── Sidebar.tsx                # 侧边栏（对话列表 + 视图切换）
│   ├── SettingsModal.tsx          # 设置弹窗
│   ├── MermaidBlock.tsx           # Mermaid 流程图渲染
│   ├── ExcelPreview.tsx           # Excel 表格预览
│   └── prd/                       # PRD 认知层组件
│       ├── PrdManager.tsx         # PRD 管理界面 + 重新分析流程
│       ├── EntityGraph.tsx        # 实体图谱可视化（Canvas）
│       ├── NormalizedEntityDetail.tsx  # 归一化实体详情（面向/冲突/关系）
│       ├── ConflictCenter.tsx     # 冲突中心（实体冲突 + 关系冲突）
│       ├── KnowledgeBaseSelector.tsx   # 知识库管理选择器
│       ├── ChatKnowledgeBaseSelector.tsx  # 对话时知识库多选
│       ├── PrdOutlinePanel.tsx    # 引导式写作大纲面板
│       ├── RightPanel.tsx         # 右侧面板（大纲 + 草稿预览）
│       ├── FeatureConfirmCard.tsx  # 功能确认交互卡片
│       ├── FeatureDraftCard.tsx   # 功能草稿交互卡片（含追问）
│       ├── WriteProgressCard.tsx  # 写入进度卡片
│       └── WriteTargetChangeCard.tsx  # 写入目标变更卡片
│
├── services/
│   ├── llm.ts                     # LLM 调用（流式 + 非流式 + 图片生成）
│   ├── mcp.ts                     # MCP 工具调用（JSON-RPC 2.0）
│   ├── prdMatcher.ts              # PRD 实体匹配 + 增强上下文构建
│   ├── semanticRecall.ts          # 语义召回（意图理解 + 关系扩展）
│   ├── prdService.ts              # PRD 解析（文档提取 + 分块 + 跨文档分析）
│   ├── normalizationService.ts    # 实体归一化（同义词 + 冲突检测）
│   ├── relationExtractionService.ts  # 关系提取 + 关系冲突检测
│   ├── guidedContext.ts           # 引导式写作上下文构建
│   ├── agentAction.ts             # Agent Action 解析与执行
│   ├── document.ts                # KM文档检索 API
│   ├── auth.ts                    # KM认证（access_token）
│   ├── excel.ts                   # Excel 解析与操作执行
│   ├── attachmentStorage.ts       # 附件 IndexedDB 存储
│   └── pdf.ts                     # PDF 解析
│
├── stores/
│   ├── prdStore.ts                # PRD 状态（文档/归一化实体/关系/冲突/知识库）
│   └── guidedPrdStore.ts          # 引导式写作状态（会话/模块/写入目标）
│
├── hooks/
│   └── useGuidedPrd.ts            # 引导式交互 Hook（确认/拒绝功能点）
│
├── prompts/
│   └── guidedPrd.ts               # 引导式 PRD 的 System Prompt 构建
│
└── types/
    └── guided-prd.ts              # 引导式 PRD 类型定义
```

### 核心文件（按行数排序）

| 文件 | 行数 | 职责 |
|------|------|------|
| `store.ts` | 1,933 | 主 Store：对话状态、消息发送全链路、MCP 循环、上下文压缩 |
| `stores/prdStore.ts` | 1,164 | PRD 状态：文档、归一化实体、关系、冲突、知识库 |
| `components/prd/PrdManager.tsx` | 1,124 | PRD 管理界面 + 重新分析流程（实体→归一化→关系） |
| `services/prdService.ts` | 987 | PRD 解析：文档获取、分块、实体提取、跨文档分析 |
| `components/ChatArea.tsx` | 951 | 聊天主区域：输入框、附件、模型切换、PRD 开关 |
| `components/prd/EntityGraph.tsx` | 873 | 实体图谱可视化（Canvas 绘制 + 交互） |
| `components/prd/NormalizedEntityDetail.tsx` | 764 | 归一化实体详情面板 |
| `services/llm.ts` | 748 | LLM 调用（流式/非流式/图片/JSON 提取） |
| `services/excel.ts` | 745 | Excel 解析与操作 |
| `services/agentAction.ts` | 565 | Agent Action 解析与执行（9 种动作） |
| `services/prdMatcher.ts` | 535 | PRD 实体匹配 + 增强上下文 |
| `stores/guidedPrdStore.ts` | 535 | 引导式写作状态管理 |
| `components/MessageBubble.tsx` | 514 | 消息气泡渲染 |
| `services/relationExtractionService.ts` | 445 | 关系提取 + 冲突检测 |
| `services/semanticRecall.ts` | 366 | 语义召回（意图 + BFS 扩展） |

### 关键函数索引

| 函数 | 文件 | 作用 |
|-----|------|-----|
| `sendMessage()` | store.ts | 发送消息主流程：意图检测→Prompt 拼装→PRD 匹配→流式对话→后处理 |
| `buildMessages()` | store.ts | 构建发送给 LLM 的完整消息数组（System Prompt + 历史 + 附件） |
| `compressHistory()` | store.ts | 工具调用场景的历史消息压缩 |
| `doStream()` | store.ts | 流式对话执行：100 轮工具循环 + 压缩 + 写作规划 |
| `handleReanalyzeAll()` | PrdManager.tsx | 重新分析全流程：实体→归一化→关系 |
| `processDocument()` | prdService.ts | 单文档实体提取（含分块） |
| `crossDocumentAnalysis()` | prdService.ts | 跨文档词频分析 + LLM 验证 |
| `runNormalization()` | normalizationService.ts | 实体归一化完整流程（含手动保留） |
| `extractRelationsWithConflictDetection()` | relationExtractionService.ts | 关系提取 + 冲突检测 |
| `matchWithNormalizedEntities()` | prdMatcher.ts | 基础实体匹配（关键词 + 别名） |
| `semanticMatchWithNormalizedEntities()` | prdMatcher.ts | 语义增强匹配（LLM 意图 + 关系扩展） |
| `buildEnhancedContext()` | prdMatcher.ts | 按面向构建注入上下文 |
| `semanticRecall()` | semanticRecall.ts | 语义召回入口：意图理解→匹配→扩展→排序 |
| `streamChat()` | llm.ts | 流式对话（AsyncGenerator） |
| `callLLM()` | llm.ts | 非流式调用（系统任务） |
| `callMcpTool()` | mcp.ts | MCP 工具调用 |
| `buildGuidedContext()` | guidedContext.ts | 引导式写作上下文构建 |
| `buildGuidedSystemPrompt()` | guidedPrd.ts | 阶段化 System Prompt 构建 |
| `parseAgentActions()` | agentAction.ts | 解析 AI 输出的 agent-action |
| `executeWriteDoc()` | agentAction.ts | 通过 MCP 逐章节写入KM文档 |

### Store 结构速查

**主 Store（`store.ts`）**

```typescript
// 关键状态
conversations[]           // 对话列表
activeConversationId      // 当前对话
settings                  // AppSettings（API/模型/服务配置）
streamingIds              // 正在流式输出的对话 ID 集合
splitPaneIds              // 分屏对话 ID 列表（最多 3 个）
activeView                // 当前视图：'chat' | 'prd'

// 关键方法
sendMessage()             // 发送消息（完整管线）
stopStreaming()            // 中止流式输出
setSettings()             // 更新设置
```

**PRD Store（`prdStore.ts`）**

```typescript
// 关键状态
documents[]               // PRD 文档列表
normalizedEntities[]      // 归一化实体
entityRelations[]         // 实体关系
relationConflicts[]       // 关系冲突
knowledgeBases[]          // 知识库列表
activeKnowledgeBaseId     // 管理视图选中的知识库
chatKnowledgeBaseIds[]    // 对话时引用的知识库（多选）

// 关键方法
addDocument()             // 添加文档
setDocumentEntities()     // 设置文档实体（保留手动实体）
setNormalizedEntities()   // 设置归一化实体
setEntityRelations()      // 设置关系
resolveConflict()         // 解决实体冲突
resolveRelationConflict() // 解决关系冲突
getChatSelectedDocs()     // 获取对话时有效的文档列表
```

**引导式写作 Store（`guidedPrdStore.ts`）**

```typescript
// 关键状态
sessions                  // Map<conversationId, GuidedSession>

// 关键方法
initSession()             // 初始化引导会话
setWriteTarget()          // 设置写入目标
registerFeatures()        // 注册模块
lockFeature()             // 锁定模块
consumePendingRegenerate() // 消费待重新生成的功能点
```

---

## 配置信息

### LLM API

| 配置项 | 值 |
|--------|-----|
| API 端点 | `/llm-api/v1`（代理到 `https://llm-open-ai-private.mihoyo.com`） |
| 默认对话模型 | `mihoyo.claude-4-6-opus` |
| 系统级调用模型 | `mihoyo.claude-4-5-opus-20251101-v1:0`（可在设置中修改） |
| 图片生成模型 | `Google/gemini-3-pro-image-preview` |

**可用模型**：

| 模型 ID | 显示名 |
|---------|--------|
| `Google/gemini-3.1-pro-preview` | Gemini 3.1 Pro |
| `Google/gemini-3-flash-preview` | Gemini 3 Flash |
| `Google/gemini-3-pro-image-preview` | Nano Banana 2 |
| `mihoyo.claude-4-6-opus` | Claude 4.6 Opus |
| `mihoyo.claude-4-5-opus-20251101-v1:0` | Claude 4.5 Opus |
| `mihoyo.claude-4-5-haiku-20251001-v1:0` | Claude 4.5 Haiku |

### 超时配置

| 场景 | 超时 |
|------|------|
| 普通对话空闲 | 2 分钟 |
| MCP 模式空闲 | 30 分钟 |
| 图片生成 | 1 分钟 |
| 普通 MCP 调用 | 5 分钟 |
| 长时 MCP 任务 | 60 分钟 |
| 非流式 LLM 调用 | 1 分钟 |
| 重试间隔 | 3 秒 × 第 N 次 |
| 最大重试 | LLM 2 次、MCP 3 次 |

### 工具调用配置

| 配置 | 值 |
|------|-----|
| 最大工具调用轮次 | 100 |
| 保留完整详情的轮数 | 5 |
| 循环检测窗口 | 最近 5 次 |
| 循环判定阈值 | 连续 3 次相同 |

### 内容截断限制

| 位置 | 限制 | 说明 |
|------|------|------|
| 长文档分块 | 40,000 字符/块 | 超长文档按章节拆分 |
| 关系提取文档内容 | 50,000 字符 | 截断 |
| PRD 匹配本地内容注入 | 3,000 字符 | 未检索到时的兜底 |
| 语义召回 token 预算 | 30,000 | 动态调整召回量 |
| 实体描述（归一化） | 500 字符 | 版本描述截断 |
| 增强上下文实体描述 | 100-150 字符 | 按面向不同 |
| 工具结果默认截断 | 300 字符 | 非特殊工具的兜底 |

### KM文档 API

| 配置项 | 值 |
|--------|-----|
| 端点 | `/doc-api`（代理到 `https://open.hoyowave.com`） |
| App ID | `cli_d172001413a848689fa9dbe1cf03eafa` |
| 检索 top_k | 20 |

### MCP 服务

| 配置项 | 值 |
|--------|-----|
| 端点 | `/mcp-api`（代理到 `http://127.0.0.1:5524`） |
| 协议 | JSON-RPC 2.0，protocolVersion `2025-03-26` |
| 长时工具列表 | create/update/append_document、get_doc_detail、read_sheet、retrieve、search 等 |

### 持久化存储

| Key | 存储内容 |
|-----|---------|
| `wave-chat-storage` | 对话列表、设置（不含附件二进制、PRD 匹配结果） |
| `prd-cognition-storage` | 文档列表（不含 rawContent）、归一化实体、关系、冲突、知识库 |
| IndexedDB | 附件二进制数据 |

---

## 修改指引

### 快速定位表

| 修改目标 | 主文件 | 相关文件 |
|---------|-------|---------|
| **对话发送流程** | `store.ts` | `ChatArea.tsx`, `llm.ts`, `MessageBubble.tsx` |
| **上下文压缩** | `store.ts`（compressHistory/compressToolResult） | 无 |
| **写作规划** | `store.ts`（extractWritingPlan + MCP_SEGMENT_PROMPT） | 无 |
| **引导式 PRD** | `prompts/guidedPrd.ts` | `guidedContext.ts`, `guidedPrdStore.ts`, `agentAction.ts` |
| **PRD 文档管理** | `PrdManager.tsx` | `prdStore.ts`, `prdService.ts` |
| **实体归一化** | `normalizationService.ts` | `prdStore.ts`, `NormalizedEntityDetail.tsx` |
| **关系提取** | `relationExtractionService.ts` | `prdStore.ts`, `EntityGraph.tsx` |
| **对话时知识引用** | `prdMatcher.ts` | `semanticRecall.ts`, `store.ts` |
| **实体图谱** | `EntityGraph.tsx` | `prdStore.ts` |
| **冲突处理** | `ConflictCenter.tsx` | `NormalizedEntityDetail.tsx`, `prdStore.ts` |
| **MCP 工具调用** | `mcp.ts` | `store.ts` |
| **Excel 处理** | `excel.ts` | `store.ts`, `ChatArea.tsx` |
| **设置/配置** | `SettingsModal.tsx` | `types.ts`, `store.ts` |
| **LLM 调用参数** | `llm.ts` | 无 |
| **预设角色** | `types.ts`（PRESET_ROLES） | 无 |

### 常见修改场景

**1. 修改对话时的知识注入逻辑**
```
1. store.ts → 搜索 sendMessage，找到 PRD 匹配段落
2. prdMatcher.ts → 修改匹配算法或上下文构建
3. semanticRecall.ts → 修改语义召回逻辑
```

**2. 添加新的 PRD 处理步骤**
```
1. prdStore.ts → 了解数据结构，添加新状态
2. PrdManager.tsx → 找到 handleReanalyzeAll()，添加新步骤
3. services/ 下新建服务文件（参考 normalizationService.ts 模式）
```

**3. 修改上下文压缩策略**
```
读取 store.ts → 搜索 compressHistory 和 compressToolResult
注意：RECENT_ROUNDS_FULL 控制保留完整详情的轮数
```

**4. 修改引导式写作流程**
```
1. prompts/guidedPrd.ts → 修改阶段化 Prompt
2. guidedContext.ts → 修改上下文构建（工作记忆窗口等）
3. agentAction.ts → 修改 Action 解析和执行逻辑
4. guidedPrdStore.ts → 修改状态管理
```

**5. 修改实体图谱展示**
```
读取 EntityGraph.tsx（自包含，873 行）
核心函数：drawGraph()，使用 Canvas 绘制
支持频率过滤：全部 / 中高频(2+) / 高频(3+)
图谱视图上限 60 个实体
```

**6. 添加新的冲突类型**
```
1. prdStore.ts → 添加冲突类型定义
2. normalizationService.ts 或 relationExtractionService.ts → 添加检测逻辑
3. ConflictCenter.tsx → 添加展示
4. NormalizedEntityDetail.tsx → 添加解决 UI
```

## 常用命令

```bash
npm run dev        # 启动开发服务器
npm run build      # TypeScript 编译 + Vite 构建
npx tsc --noEmit   # 类型检查
```

## 注意事项

1. **KM API**: 需要配置 App ID 和 App Secret，通过 access_token 认证
2. **MCP 服务**: 需要本地启动 openapi-mcp 服务（默认端口 5524）
3. **Claude 4.5 限制**: max_tokens 和 thinking.budget_tokens 都限制为 32000
4. **持久化不含**: 附件二进制（存 IndexedDB）、PRD 匹配结果（临时状态）、文档 rawContent（节省空间）
5. **手动操作优先**: PRD 认知层中所有用户手动操作在自动分析后都会保留

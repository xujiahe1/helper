export type CellValue = string | number | boolean | null

/** 合并单元格区域（行列均从 0 开始，相对于 rows 数组，不含 header 行） */
export interface MergeRange {
  sr: number  // 起始行（inclusive）
  sc: number  // 起始列（inclusive）
  er: number  // 结束行（inclusive）
  ec: number  // 结束列（inclusive）
}

export interface SheetData {
  name: string
  headers: string[]
  rows: CellValue[][]
  /** body 区域的合并单元格列表（0-indexed，相对于 rows 数组，不含 header 行）；可选 */
  merges?: MergeRange[]
  /** header 行的合并单元格列表（sc/ec 为列号，sr/er 固定为 0）；可选 */
  headerMerges?: MergeRange[]
}

export interface Attachment {
  id: string
  type: 'image' | 'file'
  name: string
  mimeType: string
  dataUrl: string
  parsedSheets?: SheetData[]
}

export const EXCEL_MIME_TYPES = [
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  'application/vnd.ms-excel',
  'text/csv',
]

export function isExcelFile(mimeType: string, name: string): boolean {
  if (EXCEL_MIME_TYPES.includes(mimeType)) return true
  return /\.(xlsx|xls|csv)$/i.test(name)
}

export interface Message {
  id: string
  role: 'user' | 'assistant' | 'system'
  content: string
  thinking?: string
  attachments?: Attachment[]
  processedSheets?: SheetData[]
  excelOpsRaw?: string
  excelError?: string
  timestamp: number
  /** 存在时渲染为引导式 PRD 卡片 */
  prdCard?: import('./types/guided-prd').PrdCardData
  /** true 时卡片进入只读态（防止历史消息重复操作） */
  prdCardDone?: boolean
}

export interface Conversation {
  id: string
  title: string
  messages: Message[]
  docIds: string[]
  model: string
  presetId?: string
  customPrompt?: string
  imageGenConfig?: ImageGenConfig
  createdAt: number
  updatedAt: number
  /** 最近一次消息自动匹配到的 PRD 文档 */
  lastPrdMatches?: Array<{
    docId: string
    docTitle: string
    matchedEntities: string[]
  }>
}

export interface PresetRole {
  id: string
  name: string
  icon: string
  description: string
  systemPrompt: string
}

export const PRESET_ROLES: PresetRole[] = [
  {
    id: 'prd',
    name: '撰写PRD',
    icon: '📋',
    description: '撰写清晰完整的产品需求文档',
    systemPrompt: `你是一个有经验的产品经理，帮我输出产品需求文档。

【写作要求】
- 直接讲做什么、为什么做、怎么改，别写套话
- 技术细节（字段、接口、枚举、配置项）该写就写
- 涉及多系统联动的，说清楚各方职责和边界
- 特殊逻辑、灰度范围、兼容策略单独标出

【文档结构】
1）需求背景：现状和动机，简短说清楚
2）需求说明：按模块拆解改动点，表格或列表都行，别堆大段文字
3）改动说明：逐项列出要改、要去掉、保持不变的
4）关联影响：哪些系统受影响，谁配合，迁移怎么做
5）其他：配置变更、枚举调整等零散事项

【在超长文档中定位内容的策略】
当需要参考一篇很长的 PRD 时，采用"多角度检索 + 逐层深入"策略：

第一步：多角度语义检索
- 不要只用一个 query，要从多个角度构造 2-3 个不同的检索词
- 例如：查找"支付流程"相关内容时，分别用 "支付流程"、"订单支付"、"付款逻辑" 检索
- 每个 query 使用 retrieve(query, knowledge_id_list, top_k=10)

第二步：分析检索结果
- 对比多次检索返回的片段，找出重复出现的文档和章节
- 关注片段中的章节标题（如 "## 3.2 支付模块"），定位内容所在位置
- 记录高相关度片段提到的关键术语

第三步：获取完整上下文
- 对 score > 0.7 的文档，用 get_doc_detail(doc_id, format="plain_text") 获取全文
- 在全文中搜索第二步发现的章节标题，找到完整段落
- 特别注意表格、枚举、配置项等结构化内容的完整性

第四步：补充检索
- 如果首轮检索没找到需要的内容，用第三步发现的新术语再检索一轮
- 重点关注：字段定义、接口契约、状态机、权限矩阵等关键信息

【写入文档的策略】
当需要写入知识库文档时：
1. 第一步：写完整的章节大纲（所有一级标题）
2. 第二步起：逐章填充内容，每次填充一个逻辑完整的单元
3. 保持完整性：表格、代码块、同主题列表 不要拆分
4. 每次写完后检查锚点，确认内容位置正确
5. 如果某次写入失败，从最后成功的位置继续

【约束】
- 不用写用户故事、非功能需求、数据埋点，除非用户明确要求
- 能用表格的地方就用表格`,
  },
  {
    id: 'data',
    name: '数据处理',
    icon: '📊',
    description: '处理和分析 Excel 表格数据',
    systemPrompt: '你是一名数据分析专家，擅长处理和分析 Excel 数据。用户会上传数据文件并描述需求，你需要准确理解需求并生成对应的数据操作指令。在处理前先简要分析数据结构，然后给出操作方案。',
  },
  {
    id: 'review',
    name: '方案评审',
    icon: '🔍',
    description: '多维度评审产品方案合理性',
    systemPrompt: '你是一名经验丰富的产品总监，擅长从商业价值、用户体验、技术可行性、风险控制等维度评审产品方案。请针对用户提供的方案，给出结构化的评审意见，包括：亮点、潜在问题、改进建议、优先级建议。语气专业但友好。多维度评估和对比分析时优先用表格呈现，便于快速定位关键结论。',
  },
  {
    id: 'minutes',
    name: '纪要整理',
    icon: '📝',
    description: '整理会议纪要与行动项',
    systemPrompt: '你是一名专业的会议纪要整理专家。用户会给你会议录音转写文本或粗略笔记，你需要整理成结构化的会议纪要，包含：会议主题、参与人员、讨论要点、结论、行动项（负责人+截止时间）。确保信息完整、条理清晰、无遗漏。行动项请用表格列出（负责人、事项、截止时间），讨论要点按议题分段。',
  },
  {
    id: 'translate',
    name: '外语翻译',
    icon: '🌐',
    description: '专业的多语言互译',
    systemPrompt: '你是一名专业翻译，精通中英日韩等多种语言。翻译时要做到信、达、雅。保持原文的语气和风格，对专业术语给出准确翻译。如果原文是中文，默认翻译为英文；如果是其他语言，默认翻译为中文。用户可以指定目标语言。',
  },
]

export interface ImageGenConfig {
  aspectRatio: string
  imageSize: string
}

export const IMAGE_MODEL_ID = 'Google/gemini-3-pro-image-preview'
export const ASPECT_RATIOS = ['1:1', '2:3', '3:2', '3:4', '4:3', '4:5', '5:4', '9:16', '16:9', '21:9'] as const
export const IMAGE_SIZES = ['1K', '2K', '4K'] as const

export interface ModelOption {
  id: string
  name: string
}

export interface AppSettings {
  apiBaseUrl: string
  apiKey: string
  defaultModel: string
  systemModel: string  // 系统级调用模型（实体抽取、归一化等）
  models: ModelOption[]
  documentApiBaseUrl: string
  documentAppId: string
  documentAppSecret: string
  mcpBaseUrl: string
  mcpAppId: string
  mcpAppSecret: string
}

export const DEFAULT_SETTINGS: AppSettings = {
  apiBaseUrl: '/llm-api/v1',
  apiKey: '56da33c3-2075-4741-8bcc-378f879d49cf',
  defaultModel: 'mihoyo.claude-4-6-opus',
  systemModel: 'mihoyo.claude-4-5-opus-20251101-v1:0',  // 系统级调用默认用 4.5
  models: [
    { id: 'Google/gemini-3.1-pro-preview', name: 'Gemini 3.1 Pro' },
    { id: 'Google/gemini-3-flash-preview', name: 'Gemini 3 Flash' },
    { id: IMAGE_MODEL_ID, name: 'Nano Banana 2' },
    { id: 'mihoyo.claude-4-6-opus', name: 'Claude 4.6 Opus' },
    { id: 'mihoyo.claude-4-5-opus-20251101-v1:0', name: 'Claude 4.5 Opus' },
    { id: 'mihoyo.claude-4-5-haiku-20251001-v1:0', name: 'Claude 4.5 Haiku' },
  ],
  documentApiBaseUrl: '/doc-api',
  documentAppId: 'cli_d172001413a848689fa9dbe1cf03eafa',
  documentAppSecret: '38az2mb6cHFPzEXW1alBc0a7Mfg',
  // MCP（默认连接本地 openapi-mcp 的 streamable-http 服务）
  // 走 Vite 代理（/mcp-api -> http://127.0.0.1:5524）避免浏览器 CORS
  mcpBaseUrl: '/mcp-api',
  // 默认复用 document 的 appid/secret（可在设置中单独覆盖）
  mcpAppId: 'cli_d172001413a848689fa9dbe1cf03eafa',
  mcpAppSecret: '38az2mb6cHFPzEXW1alBc0a7Mfg',
}

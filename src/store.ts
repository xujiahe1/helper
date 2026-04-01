import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import { v4 as uuidv4 } from 'uuid'
import type { Conversation, Message, AppSettings, Attachment, ImageGenConfig } from './types'
import { DEFAULT_SETTINGS, PRESET_ROLES, isExcelFile } from './types'
import { streamChat, buildApiMessage, isImageModel, generateImage, messageHasPdf, type ChatMessage, type ToolCallInfo } from './services/llm'
import { parseExcelOpsFromContent, executeOperations, executeMultipleOperations } from './services/excel'
import { retrieveDocuments } from './services/document'
import { clearTokenCache } from './services/auth'
import { callMcpTool, resetMcpSession, getMcpToolsAsOpenAI } from './services/mcp'
import { usePrdStore } from './stores/prdStore'
import { matchWithNormalizedEntities, buildEnhancedContext, semanticMatchWithNormalizedEntities, buildSemanticEnhancedContext } from './services/prdMatcher'
import { fetchDocumentContent, parseDocInfoFromUrl } from './services/prdService'
import { saveAttachments, getAttachments, deleteConversationAttachments } from './services/attachmentStorage'
import { useGuidedPrdStore } from './stores/guidedPrdStore'
import { buildGuidedContext } from './services/guidedContext'
import { parseAgentActions, stripAgentActions, dispatchAgentActions } from './services/agentAction'



const abortControllers = new Map<string, AbortController>()

const EXCEL_TOOL_PROMPT = `你拥有 Excel 数据处理能力。你收到的 Excel 信息是 **Schema 摘要**（列信息、唯一值枚举、统计数据、少量样本行），而非全量数据。所有操作指令会在前端对完整数据执行，因此你不需要看到全量数据就能正确生成操作。

当用户要求对数据进行处理（筛选、排序、去重、新增列、删除、替换、聚合、透视、跨表查找等）时，你必须在回复中输出一个 \`\`\`excel-ops 代码块，包含 JSON 操作指令。前端会自动解析并执行这些操作，然后展示处理结果和下载按钮。

输出格式统一使用 JSON 对象（不要输出裸数组）：
\`\`\`excel-ops
{"source":"original","targetSheet":"Sheet1","ops":[{"op":"filter","column":"销售额","operator":"gt","value":"10000"}]}
\`\`\`

顶层字段说明：

source 字段（必填）：
- "original"：基于用户上传的原始数据执行操作。适用于：新的处理需求、更换条件、重新筛选等
- "previous"：基于上一轮处理结果继续操作（链式）。适用于：用户明确说"在此基础上""继续""再过滤一下"等
判断原则：默认用 "original"。只有当用户明确表达要在上一步结果上继续操作时才用 "previous"。

targetSheet 字段（可选）：指定要操作的工作表名。当涉及多个工作表操作（如 vlookup、merge_sheets）时必须指定。例如用户说"把 Sheet1 的域账号匹配到 Sheet2 里"，则 targetSheet 应为 "Sheet2"，fromSheet 为 "Sheet1"。

outputSheet 字段（可选）：**将结果输出到新工作表**，而不是覆盖原表。当用户要求"生成新表""创建新工作表""把结果放到第X张表"时使用。例如：
\`\`\`excel-ops
{"source":"original","targetSheet":"系统账号","outputSheet":"未匹配账号","ops":[{"op":"filter","column":"状态","operator":"eq","value":"未匹配"}]}
\`\`\`
这会对"系统账号"表执行筛选，但结果会创建为新工作表"未匹配账号"，原"系统账号"表保持不变。

可用操作（op）及参数：

【查】
- filter: {column, operator: eq|neq|contains|startswith|endswith|gt|gte|lt|lte|empty|notempty, value}
- sort: {column, ascending} 或多列 {columns: [{column, ascending}, ...]}
- dedup: {columns: ["列名1", "列名2"]}
- select_columns: {columns: ["列名1", "列名2"]}

【增】
- add_column: {name: "新列名", formula: "$列A$ * $列B$"} — formula 是 JS 表达式，用 $列名$ 引用列值
- add_rows: {rows: [[值1, 值2, ...], ...]}
- vlookup: {lookupColumn: "本表(目标表)的key列", fromSheet: "来源工作表名", fromKeyColumn: "来源表的key列", fromValueColumn: "来源表中要取的值列", newColumnName: "新列名"} — 从 fromSheet 中根据 key 匹配查找值，插入到目标表中作为新列

【删】
- delete_columns: {columns: ["列名1", "列名2"]}
- delete_rows: {column, operator, value} — 删除匹配的行
- keep_sheets: {sheets: ["表名1", "表名2"]} — 只保留指定的工作表，删除其他工作表
- delete_sheets: {sheets: ["表名1"]} — 删除指定的工作表

【改】
- rename_columns: {rename: {"旧名":"新名", ...}}
- update_cells: {column, find: "查找值", replace: "替换值"}
- conditional_update: {condition: {column, operator, value}, targetColumn, newValue}
- fill_null: {column, fillValue: "填充值"}
- split_column: {column, separator, newColumns: ["列1", "列2"]}
- concat_columns: {columns: ["列A", "列B"], separator: " ", newColumn: "新列名"}
- merge_cells: {column: "部门", direction: "vertical"}  // 纵向：相邻行值相同则合并单元格
- merge_cells: {direction: "horizontal", row: 0}  // 横向：指定行相邻列值相同则合并（row 可选，不指定则全表横向）
- unmerge_cells: {column: "报销人"}  // 取消指定列的单元格合并（导出时该列不合并）
- unmerge_cells: {}  // 取消全表所有单元格合并

【高级】
- group_aggregate: {groupBy: ["分组列"], aggregates: [{column: "聚合列", func: sum|avg|count|min|max}]}
- pivot: {rowField, columnField, valueField, func: sum|avg|count|min|max}
- merge_sheets: {fromSheet: "工作表名", mode: "union"|"join", joinKey: "关联列"}

规则：
- 所有列名使用 Schema 摘要中的实际列名
- filter/delete_rows 的 value 必须使用列枚举中出现的确切值（注意大小写和空格）
- 可以组合多个操作，按数组顺序执行
- 如果用户只是询问数据内容、做分析讨论，不需要输出 excel-ops，直接基于统计摘要和样本行用文字回答
- 只有当用户明确要求"处理""导出""筛选""清洗""匹配"等需要修改数据的操作时才输出 excel-ops
- 当用户要求"生成新表""创建第X张表""结果放到新工作表"时，务必使用 outputSheet 字段
- 在 excel-ops 代码块之外，用自然语言解释你做了什么操作
- 合并单元格默认行为：上传时自动拆分填充（每个格子都有值），导出时还原原始合并格式。如果用户要求"合并"或"拆分"单元格，使用 merge_cells 或 unmerge_cells 操作`

interface ChatState {
  conversations: Conversation[]
  activeConversationId: string | null
  settings: AppSettings
  // per-conversation streaming: convId -> assistantMessageId
  streamingIds: Record<string, string>
  splitPaneIds: string[]
  highlightMessageId: string | null
  searchHighlightKeyword: string | null
  settingsOpen: boolean
  sidebarOpen: boolean
  activeView: 'chat' | 'prd'

  createConversation: (presetId?: string) => string
  deleteConversation: (id: string) => void
  renameConversation: (id: string, title: string) => void
  setActiveConversation: (id: string | null) => void
  setConversationModel: (id: string, model: string) => void
  setConversationPrompt: (id: string, prompt: string) => void
  addDocToConversation: (id: string, docId: string) => void
  removeDocFromConversation: (id: string, docId: string) => void
  sendMessage: (content: string, attachments?: Attachment[], enableThinking?: boolean, imageGenConfig?: ImageGenConfig, targetConversationId?: string, enableMcp?: boolean, enablePrd?: boolean) => Promise<void>
  editMessage: (messageId: string) => { content: string; attachments?: Attachment[] } | null
  retryMessage: (failedMessageId: string) => Promise<void>
  reExecuteExcelOps: (messageId: string, forceOriginal: boolean) => void
  stopStreaming: (conversationId?: string) => void
  updateSettings: (settings: Partial<AppSettings>) => void
  setHighlightMessage: (id: string | null) => void
  setSearchHighlightKeyword: (keyword: string | null) => void
  addSplitPane: (conversationId: string) => void
  removeSplitPane: (conversationId: string) => void
  toggleSettings: () => void
  toggleSidebar: () => void
  setActiveView: (view: 'chat' | 'prd') => void
  restoreAttachments: () => Promise<void>
  /** 向指定对话追加一条消息（引导式 PRD 用） */
  appendMessage: (conversationId: string, message: Message) => void
  /** 更新指定消息的部分字段（引导式 PRD 用） */
  updateMessage: (conversationId: string, messageId: string, patch: Partial<Message>) => void
  /** 将指定消息的 prdCardDone 置为 true（防止重复操作） */
  markPrdCardDone: (conversationId: string, messageId: string) => void
}

export const useChatStore = create<ChatState>()(
  persist(
    (set, get) => ({
      conversations: [],
      activeConversationId: null,
      settings: DEFAULT_SETTINGS,
      streamingIds: {},
      splitPaneIds: [],
      highlightMessageId: null,
      searchHighlightKeyword: null,
      settingsOpen: false,
      sidebarOpen: true,
      activeView: 'chat',

      createConversation: (presetId) => {
        const id = uuidv4()
        const preset = presetId ? PRESET_ROLES.find(r => r.id === presetId) : undefined
        const conversation: Conversation = {
          id,
          title: preset ? preset.name : '新对话',
          messages: [],
          docIds: [],
          model: get().settings.defaultModel,
          presetId: preset?.id,
          createdAt: Date.now(),
          updatedAt: Date.now(),
        }
        set(state => ({
          conversations: [conversation, ...state.conversations],
          activeConversationId: id,
        }))
        return id
      },

      deleteConversation: (id) => {
        // 先中止该对话的流式请求
        abortControllers.get(id)?.abort()
        abortControllers.delete(id)

        // 同时删除 IndexedDB 中的附件
        deleteConversationAttachments(id).catch(console.error)

        set(state => {
          const conversations = state.conversations.filter(c => c.id !== id)
          const activeId = state.activeConversationId === id
            ? (conversations[0]?.id ?? null)
            : state.activeConversationId
          // 同时清理 streamingIds
          const { [id]: _removed, ...restStreamingIds } = state.streamingIds
          return {
            conversations,
            activeConversationId: activeId,
            splitPaneIds: state.splitPaneIds.filter(pid => pid !== id),
            streamingIds: restStreamingIds,
          }
        })
      },

      renameConversation: (id, title) => {
        set(state => ({
          conversations: state.conversations.map(c =>
            c.id === id ? { ...c, title, updatedAt: Date.now() } : c
          ),
        }))
      },

      setActiveConversation: (id) => {
        set(state => ({
          activeConversationId: id,
          splitPaneIds: id ? state.splitPaneIds.filter(pid => pid !== id) : state.splitPaneIds,
        }))
      },

      setConversationModel: (id, model) => {
        set(state => ({
          conversations: state.conversations.map(c =>
            c.id === id ? { ...c, model } : c
          ),
        }))
      },

      setConversationPrompt: (id, prompt) => {
        set(state => ({
          conversations: state.conversations.map(c =>
            c.id === id ? { ...c, customPrompt: prompt, updatedAt: Date.now() } : c
          ),
        }))
      },

      addDocToConversation: (id, docId) => {
        set(state => ({
          conversations: state.conversations.map(c => {
            if (c.id !== id) return c
            if ((c.docIds || []).includes(docId)) return c
            return { ...c, docIds: [...(c.docIds || []), docId], updatedAt: Date.now() }
          }),
        }))
      },

      removeDocFromConversation: (id, docId) => {
        set(state => ({
          conversations: state.conversations.map(c => {
            if (c.id !== id) return c
            return { ...c, docIds: (c.docIds || []).filter(d => d !== docId), updatedAt: Date.now() }
          }),
        }))
      },

      editMessage: (messageId) => {
        const state = get()
        for (const conv of state.conversations) {
          const idx = conv.messages.findIndex(m => m.id === messageId)
          if (idx < 0) continue
          if (state.streamingIds[conv.id]) return null
          const msg = conv.messages[idx]
          if (msg.role !== 'user') return null
          const saved = { content: msg.content, attachments: msg.attachments }
          set(s => ({
            activeConversationId: conv.id,
            conversations: s.conversations.map(c => {
              if (c.id !== conv.id) return c
              return { ...c, messages: c.messages.slice(0, idx), updatedAt: Date.now() }
            }),
          }))
          return saved
        }
        return null
      },

      sendMessage: async (content, attachments, enableThinking, imageGenConfig, targetConversationId, enableMcp, enablePrd) => {
        let conversationId = targetConversationId || get().activeConversationId
        if (!conversationId) {
          conversationId = get().createConversation()
        }
        if (get().streamingIds[conversationId]) return

        // ── 写文档意图检测：直接触发引导模式，不依赖 AI 输出 agent-action ──
        if (enableMcp && !useGuidedPrdStore.getState().getSession(conversationId)) {
          // 扩展关键词列表，覆盖更多"帮我写文档"的表达方式
          const writeKeywords = [
            // 明确的写入意图
            '写文档', '写到', '写入', '更新文档', '填写', '写prd', '写PRD',
            // 文档类型
            '整理需求', '需求文档', '功能规格', '产品文档', '设计文档', '方案文档',
            // 常见表达
            '帮我写', '帮我整理', '帮我梳理', '输出一份', '生成一份', '写一份', '写一个',
            '整理成文档', '形成文档', '落到文档', '沉淀到', '记录到',
            // 文档操作
            '新建文档', '创建文档', '起草', '撰写', '编写',
          ]
          const hasWriteUrl = /km\.mihoyo\.com|hoyowave\.com/.test(content)
          const hasWriteKeyword = writeKeywords.some(kw => content.includes(kw))

          // 额外检测：用户提到要"给我"/"帮我" + "文档"/"PRD"/"需求"
          const hasDocRequest = /(?:给我|帮我|请|麻烦).{0,10}(?:文档|PRD|prd|需求|方案|设计)/i.test(content)

          if (hasWriteUrl || hasWriteKeyword || hasDocRequest) {
            const conv = get().conversations.find(c => c.id === conversationId)
            const model = conv?.model ?? get().settings.defaultModel
            const msgCount = conv?.messages.length ?? 0
            useGuidedPrdStore.getState().initSession(conversationId, model, msgCount)

            // ── 自动推断 writeTarget ──
            const guidedStore = useGuidedPrdStore.getState()

            // 检测是否有文档链接（existing_doc 模式）
            const docUrlMatch = content.match(/(?:km\.mihoyo\.com|hoyowave\.com)\/wiki\/([a-zA-Z0-9]+)/)
            if (docUrlMatch) {
              guidedStore.setWriteTarget(conversationId, {
                description: '更新已有文档',
                mode: 'existing_doc',
                docId: docUrlMatch[1],
              })
            } else {
              // 尝试提取写作目标描述（new_doc 模式）
              // 匹配模式：帮我写/整理/梳理 + 一个/一份 + XXX
              const targetMatch = content.match(/(?:帮我|请|麻烦)?(?:写|整理|梳理|输出|生成|起草|撰写|编写)(?:一个|一份)?[「『"']?([^「」『』"'，。、\n]{2,20})[」』"']?(?:文档|PRD|prd|需求|方案)?/i)
              if (targetMatch && targetMatch[1]) {
                const desc = targetMatch[1].replace(/(?:文档|PRD|prd|需求|方案)$/, '').trim()
                if (desc.length >= 2) {
                  guidedStore.setWriteTarget(conversationId, {
                    description: desc,
                    mode: 'new_doc',
                  })
                }
              }
            }
          }
        }

        const userMessage: Message = {
          id: uuidv4(),
          role: 'user',
          content,
          attachments: attachments?.length ? attachments : undefined,
          timestamp: Date.now(),
        }

        // 保存用户消息的附件到 IndexedDB
        if (attachments?.length) {
          saveAttachments(conversationId, userMessage.id, attachments).catch(console.error)
        }

        const assistantMessageId = uuidv4()
        const assistantMessage: Message = {
          id: assistantMessageId,
          role: 'assistant',
          content: '',
          timestamp: Date.now(),
        }

        set(state => ({
          streamingIds: { ...state.streamingIds, [conversationId!]: assistantMessageId },
          conversations: state.conversations.map(c => {
            if (c.id !== conversationId) return c
            const isFirst = c.messages.length === 0
            return {
              ...c,
              messages: [...c.messages, userMessage, assistantMessage],
              imageGenConfig: imageGenConfig || c.imageGenConfig,
              title: isFirst
                ? content.slice(0, 30) + (content.length > 30 ? '...' : '')
                : c.title,
              updatedAt: Date.now(),
            }
          }),
        }))

        const conversation = get().conversations.find(c => c.id === conversationId)
        if (!conversation) return

        const rawMessages = conversation.messages.filter(m => m.id !== assistantMessageId)
        const hasPdf = messageHasPdf(rawMessages)

        const MCP_SEGMENT_PROMPT = `当你通过工具写入或更新文档时，遵循以下策略：

【长文档写作规范 - 重要】
开始撰写长文档前，必须先输出写作规划（系统会保留此规划用于后续上下文）：

<writing-plan>
## 目标
[一句话描述文档目标]

## 结构大纲
1. [章节名] - [核心要点]
2. [章节名] - [核心要点]
   2.1 [子章节] - [要点]
...

## 关键约束
- [用户提到的特殊要求]
- [参考文档中需要遵循的规范]
</writing-plan>

如需调整规划，重新输出 <writing-plan> 标签即可。

【分段原则】
- 以逻辑单元为边界，而非机械的字数限制
- 一个逻辑单元 = 一个完整的章节/表格/代码块/主题列表
- 单次调用内容建议 500-1500 字，但保持逻辑完整性优先

【写入顺序】
1. 首次调用：写入完整的章节大纲（所有一级标题）
2. 后续调用：按顺序逐章填充内容
3. 每完成一个章节，用简短文字确认进度

【进度追踪】
- 每次成功写入后，记录当前位置（如："✓ 已完成：需求背景、需求说明"）
- 如果调用失败，从最后成功的章节继续

【完成确认】
- 所有章节写完后，告知用户文档已完成
- 提供文档链接（如果有 doc_url）`

        const buildMessages = async (pdfMode: 'native' | 'images' | 'extract') => {
          const msgs = await Promise.all(rawMessages.map(m => buildApiMessage(m, { pdfMode })))

          const systemParts: string[] = []
          systemParts.push(`输出格式偏好：
- 对比分析、特性罗列、多维度评估、状态/角色/权限矩阵等结构化信息，优先使用 Markdown 表格呈现
- 步骤说明、操作指南等顺序性内容，使用有序列表
- 仅当内容本身具有明确的流程分支、时序交互或层级架构关系时，才使用 Mermaid 语法（放在 \`\`\`mermaid 代码块中）
- 不要为了视觉效果而把本可以用表格或列表表达的内容强行画成图`)

          if (conversation.customPrompt) {
            systemParts.push(conversation.customPrompt)
          } else if (conversation.presetId) {
            const preset = PRESET_ROLES.find(r => r.id === conversation.presetId)
            if (preset) systemParts.push(preset.systemPrompt)
          }

          const docIds = conversation.docIds || []

          // 构造检索 query：拼入最近几轮对话摘要，避免短查询时检索质量差
          let retrieveQuery = content
          if (docIds.length > 0 && rawMessages.length > 1) {
            const recentMsgs = rawMessages.slice(-6)
            const contextParts: string[] = []
            for (const m of recentMsgs) {
              if (m.id === userMessage.id) continue
              contextParts.push(m.content.slice(0, 100))
            }
            if (contextParts.length > 0) {
              retrieveQuery = contextParts.join(' ') + ' ' + content
          }
        }

          if (docIds.length > 0) {
            try {
              const results = await retrieveDocuments(retrieveQuery, docIds, get().settings)
              if (results.length > 0) {
                const ctx = results
                  .map(r => '【' + r.meta.title + '】\n' + r.content)
                  .join('\n\n---\n\n')
                systemParts.push('以下是本次对话关联文档中检索到的相关内容：\n\n' + ctx)
              }
            } catch {
              set(state => ({
                conversations: state.conversations.map(c => {
                  if (c.id !== conversationId) return c
                  return { ...c, messages: c.messages.map(m => m.id === assistantMessageId ? { ...m, thinking: '文档检索失败，将不使用文档上下文回答' } : m) }
                }),
              }))
            }
          }

          // ========== PRD 认知层自动关联（需开启开关） ==========
          if (enablePrd) {
            const prdState = usePrdStore.getState()
            // 使用对话时选中的知识库文档（默认全选）
            const prdDocuments = prdState.getChatSelectedDocs()
            // 只取对话时选中知识库的归一化实体（与 prdDocuments 保持一致）
            const chatKbIds = prdState.chatKnowledgeBaseIds.length === 0
              ? prdState.knowledgeBases.map(kb => kb.id)
              : prdState.chatKnowledgeBaseIds
            const normalizedEntities = prdState.normalizedEntities.filter(e => chatKbIds.includes(e.knowledgeBaseId))

            // 本轮开始时立即清空上一轮残留，避免旧结果在匹配期间闪烁显示
            set(state => ({
              conversations: state.conversations.map(c =>
                c.id !== conversationId ? c : { ...c, lastPrdMatches: undefined }
              )
            }))

            console.log('[PRD Match] enablePrd:', enablePrd)
            console.log('[PRD Match] 知识库数量:', prdState.knowledgeBases.length)
            console.log('[PRD Match] 选中的文档数量:', prdDocuments.length)
            console.log('[PRD Match] 文档列表:', prdDocuments.map(d => ({ id: d.docId, title: d.title, status: d.status, kbId: d.knowledgeBaseId })))

            if (prdDocuments.length > 0) {
              // 先跑基础匹配，无命中则直接跳过，避免无谓的语义 LLM 调用和文档检索
              const basicMatches = matchWithNormalizedEntities(content, prdDocuments, normalizedEntities, 5)

              let prdMatches: ReturnType<typeof matchWithNormalizedEntities>
              let semanticIntent: { intent: string; isImpactAnalysis: boolean } | undefined
              let recalledEntities: Array<{ entityName: string; level: number; relationPath?: string[] }> | undefined

              if (basicMatches.length === 0) {
                // 基础匹配无命中，内容与知识库无关，跳过后续所有处理
                prdMatches = []
              } else {
                // 有命中实体，再判断是否需要升级为语义增强模式
                // 语义模式触发条件：包含影响面关键词（内容驱动），不再以"有无关系数据"作为条件
                const impactKeywords = ['影响面', '影响范围', '影响分析', '改了之后', '改动影响', '牵扯', '连带影响', '波及', '会影响', '关联', '依赖']
                const useSemanticMode = impactKeywords.some(kw => content.includes(kw))

                if (useSemanticMode && normalizedEntities.length >= 2) {
                  // 使用语义增强匹配
                  console.log('[PRD Match] 使用语义增强模式')
                  try {
                    const semanticResult = await semanticMatchWithNormalizedEntities(
                      get().settings,
                      content,
                      prdDocuments,
                      normalizedEntities,
                      { maxDepth: 2, skipIntentExtraction: false }
                    )
                    prdMatches = semanticResult.matches
                    semanticIntent = semanticResult.intent
                    recalledEntities = semanticResult.recalledEntities
                    console.log('[PRD Match] 语义匹配意图:', semanticResult.intent)
                    console.log('[PRD Match] 语义召回实体:', semanticResult.recalledEntities.map(e => e.entityName))
                  } catch (err) {
                    console.warn('[PRD Match] 语义匹配失败，回退到基础匹配结果:', err)
                    prdMatches = basicMatches
                  }
                } else {
                  // 直接复用已有的基础匹配结果，不重复计算
                  prdMatches = basicMatches
                }
              }

              console.log('[PRD Match] 匹配结果:', prdMatches)

              if (prdMatches.length > 0) {
                // 排除已手动添加的文档
                const manualDocIds = new Set(docIds)
                const prdDocIds = prdMatches
                  .map(m => m.docId)
                  .filter(id => !manualDocIds.has(id))

                console.log('[PRD Match] 手动关联的文档 IDs:', docIds)
                console.log('[PRD Match] 需要检索的文档 IDs:', prdDocIds)

              if (prdDocIds.length > 0) {
                try {
                  console.log('[PRD Match] 开始检索文档...')
                  const prdResults = await retrieveDocuments(retrieveQuery, prdDocIds, get().settings)
                  console.log('[PRD Match] 检索结果:', prdResults)

                  // 检索返回的文档 IDs
                  const retrievedDocIds = new Set(prdResults.map(r => r.meta.doc_id))
                  console.log('[PRD Match] 检索到内容的文档 IDs:', [...retrievedDocIds])

                  // 对于检索未返回但有本地内容的文档（如表格），使用本地内容补充
                  // 如果本地也没有内容（刷新页面后），对于表格文档尝试通过 MCP 重新获取
                  const localContentDocs: Array<{ docId: string; title: string; content: string }> = []
                  for (const match of prdMatches) {
                    if (!retrievedDocIds.has(match.docId)) {
                      // 检索没返回这个文档，看看本地有没有内容
                      const localDoc = prdDocuments.find(d => d.docId === match.docId)
                      if (localDoc?.rawContent) {
                        console.log('[PRD Match] 使用本地内容补充:', match.docTitle)
                        localContentDocs.push({
                          docId: match.docId,
                          title: match.docTitle,
                          content: localDoc.rawContent.slice(0, 3000) // 限制长度
                        })
                        retrievedDocIds.add(match.docId) // 加入已检索集合
                      } else if (localDoc?.docUrl) {
                        // 本地没有内容，检查是否是表格文档，尝试通过 MCP 重新获取
                        const docInfo = parseDocInfoFromUrl(localDoc.docUrl)
                        if (docInfo?.sheetId) {
                          console.log('[PRD Match] 表格文档无本地内容，尝试通过 MCP 获取:', match.docTitle)
                          try {
                            const { content } = await fetchDocumentContent(get().settings, localDoc.docId, localDoc.docUrl)
                            if (content) {
                              console.log('[PRD Match] MCP 获取表格内容成功:', match.docTitle)
                              localContentDocs.push({
                                docId: match.docId,
                                title: match.docTitle,
                                content: content.slice(0, 3000) // 限制长度
                              })
                              retrievedDocIds.add(match.docId) // 加入已检索集合
                              // 更新 prdStore 中的 rawContent，避免下次重复获取
                              usePrdStore.getState().updateDocument(localDoc.id, { rawContent: content })
                            }
                          } catch (mcpError) {
                            console.warn('[PRD Match] MCP 获取表格内容失败:', mcpError)
                          }
                        }
                      }
                    }
                  }

                  if (retrievedDocIds.size > 0) {
                    const actualMatches = prdMatches.filter(m => retrievedDocIds.has(m.docId))
                    console.log('[PRD Match] 最终匹配结果:', actualMatches)

                    // 构建增强上下文
                    // 如果有语义增强结果，使用语义增强上下文；否则使用基础增强上下文
                    let enhancedContext: string
                    if (semanticIntent && recalledEntities) {
                      enhancedContext = buildSemanticEnhancedContext(
                        actualMatches,
                        normalizedEntities,
                        semanticIntent as any,
                        recalledEntities as any
                      )
                    } else {
                      // 基础增强上下文（按面向组织，包含变更提示和冲突警告）
                      enhancedContext = buildEnhancedContext(
                        actualMatches,
                        normalizedEntities,
                      )
                    }

                    // 合并检索结果和本地内容
                    let prdCtx = prdResults
                      .map(r => '【' + r.meta.title + '】\n' + r.content)
                      .join('\n\n---\n\n')

                    // 添加本地内容补充的文档
                    if (localContentDocs.length > 0) {
                      const localCtx = localContentDocs
                        .map(d => '【' + d.title + '】\n' + d.content)
                        .join('\n\n---\n\n')
                      prdCtx = prdCtx ? prdCtx + '\n\n---\n\n' + localCtx : localCtx
                    }

                    systemParts.push(`以下是从 PRD 认知层自动关联的相关文档（基于实体匹配）：

${enhancedContext}

## 相关文档内容
${prdCtx}`)

                    // 更新对话的 lastPrdMatches（只保存实际用于回答的文档）
                    set(state => ({
                      conversations: state.conversations.map(c => {
                        if (c.id !== conversationId) return c
                        return {
                          ...c,
                          lastPrdMatches: actualMatches.map(m => ({
                            docId: m.docId,
                            docTitle: m.docTitle,
                            matchedEntities: m.matchedEntities
                          }))
                        }
                      })
                    }))
                  } else {
                    // 检索没有返回内容，清空 lastPrdMatches
                    set(state => ({
                      conversations: state.conversations.map(c => {
                        if (c.id !== conversationId) return c
                        return { ...c, lastPrdMatches: undefined }
                      })
                    }))
                  }
                } catch (e) {
                  console.warn('[PRD Match] 检索失败:', e)
                }
                } else {
                  // 所有匹配文档都已手动添加，清空 lastPrdMatches
                  set(state => ({
                    conversations: state.conversations.map(c => {
                      if (c.id !== conversationId) return c
                      return { ...c, lastPrdMatches: undefined }
                    })
                  }))
                }
              } else {
                // 没有匹配到任何文档，清空 lastPrdMatches
                set(state => ({
                  conversations: state.conversations.map(c => {
                    if (c.id !== conversationId) return c
                    return { ...c, lastPrdMatches: undefined }
                  })
                }))
              }
            }

            // 影响面检测 → 改为基于实体关系的查询
            const impactKeywords = ['影响面', '影响范围', '影响分析', '改了之后', '改动影响', '牵扯', '连带影响', '波及', '会影响']
            const isImpactQuery = impactKeywords.some(kw => content.includes(kw))

            // 收集所有实体关系用于影响面分析
            const allRelations = normalizedEntities.flatMap(e =>
              e.relations.map(r => ({
                ...r,
                sourceEntityId: e.id,
                sourceEntityName: e.canonicalName,
              }))
            )

            if (isImpactQuery && prdDocuments.length > 0 && allRelations.length > 0) {
              const prdMatches2 = matchWithNormalizedEntities(content, prdDocuments, normalizedEntities, 3)
              const mainMatch = prdMatches2[0]
              if (mainMatch?.canonicalName) {
                try {
                  const mainEntity = normalizedEntities.find(e => e.canonicalName === mainMatch.canonicalName)
                  if (mainEntity) {
                    // 第 1 层：直接相关的关系
                    const directRelations = mainEntity.relations

                    if (directRelations.length > 0) {
                      // 第 2 层：展开关联实体的关系
                      const relatedEntityIds = new Set(directRelations.map(r => r.targetEntityId))
                      const indirectRelations: Array<{
                        sourceEntityName: string
                        relationType: string
                        targetEntityName: string
                        sources: typeof directRelations[0]['sources']
                      }> = []

                      for (const entityId of relatedEntityIds) {
                        const relatedEntity = normalizedEntities.find(e => e.id === entityId)
                        if (relatedEntity) {
                          for (const r of relatedEntity.relations) {
                            // 排除指向主实体的关系（避免循环）
                            if (r.targetEntityId !== mainEntity.id) {
                              indirectRelations.push({
                                sourceEntityName: relatedEntity.canonicalName,
                                relationType: r.relationType,
                                targetEntityName: r.targetEntityName,
                                sources: r.sources,
                              })
                            }
                          }
                        }
                      }
                      const limitedIndirectRelations = indirectRelations.slice(0, 20) // 限制数量

                      const knowledgeCtx = [
                        `## 相关知识（${mainMatch.canonicalName}）`,
                        '',
                        '### 直接相关',
                        ...directRelations.map(r => {
                          const src = r.sources[0]
                            ? (r.sources[0].anchor ? `${r.sources[0].docTitle} §${r.sources[0].anchor}` : r.sources[0].docTitle)
                            : ''
                          return `- ${mainEntity.canonicalName} → ${r.relationType} → ${r.targetEntityName}${src ? `（${src}）` : ''}`
                        }),
                      ]

                      if (limitedIndirectRelations.length > 0) {
                        knowledgeCtx.push('', '### 间接相关')
                        knowledgeCtx.push(...limitedIndirectRelations.map(r => {
                          const src = r.sources[0]
                            ? (r.sources[0].anchor ? `${r.sources[0].docTitle} §${r.sources[0].anchor}` : r.sources[0].docTitle)
                            : ''
                          return `- ${r.sourceEntityName} → ${r.relationType} → ${r.targetEntityName}${src ? `（${src}）` : ''}`
                        }))
                      }

                      systemParts.push(`\n${knowledgeCtx.join('\n')}\n\n请基于以上知识回答用户的问题。`)
                    }
                  }
                } catch (e) {
                  console.error('[Knowledge] 知识查询失败:', e)
                }
              }
            }
          } else {
            // PRD 开关关闭时，清空 lastPrdMatches 防止残留
            set(state => ({
              conversations: state.conversations.map(c => {
                if (c.id !== conversationId) return c
                return { ...c, lastPrdMatches: undefined }
              })
            }))
          }

          const hasExcel = rawMessages.some(m =>
            m.attachments?.some(a => a.parsedSheets?.length || isExcelFile(a.mimeType, a.name))
          )
          if (hasExcel) {
            systemParts.push(EXCEL_TOOL_PROMPT)
          }

          if (enableMcp) {
            systemParts.push(MCP_SEGMENT_PROMPT)

            // PRD 任务额外添加历史文档参考指南
            if (conversation.presetId === 'prd') {
              systemParts.push(`【超长文档内容定位指南】
问题：一篇 PRD 可能有上万字，单次 retrieve 只返回片段，容易遗漏关键内容。

解决方案：多角度检索 + 章节定位

1. 多 Query 检索
   - 对同一主题用 2-3 个不同表述分别检索
   - 示例：查"权限"时，同时搜 "权限控制"、"角色权限"、"access control"
   - 每次 retrieve(query, knowledge_id_list, top_k=10)

2. 章节标题定位
   - 检索结果中留意章节标题（如 "## 2.3 权限模块"）
   - 记录这些标题，后续在全文中按标题跳转

3. 获取全文后的处理
   - get_doc_detail(doc_id, format="plain_text") 获取全文
   - 不要逐字阅读，而是：
     a) 先看目录/大纲结构
     b) 跳到第2步定位的章节
     c) 重点提取表格、枚举、接口定义等结构化信息

4. 补充检索
   - 如果全文中发现新的关键词（如某个配置项名称），再用它检索
   - 可能会找到其他文档中的相关定义

工具用法：
- retrieve(query, knowledge_id_list, top_k=10, score_threshold=0.5)
- get_doc_detail(doc_id, format="plain_text")`)
            }

            // 如果还没有进入引导模式，添加 AI 检测触发提示
            const hasGuidedSession = useGuidedPrdStore.getState().getSession(conversationId!)
            if (!hasGuidedSession) {
              systemParts.push(`【文档写作意图检测】
如果你判断用户想要撰写文档（PRD、需求文档、设计文档、方案等），请在回复末尾输出：
\`\`\`agent-action
{"type":"start_guided","reason":"简述判断理由"}
\`\`\`

触发条件（满足任一即可）：
- 用户明确说"帮我写"、"整理成文档"、"输出一份PRD"等
- 用户提供了大量需求信息，期望你整理成文档
- 用户讨论完功能后说"可以写了"、"开始写吧"

不要触发的情况：
- 用户只是在咨询、讨论、分析问题
- 用户已经在写文档只是让你帮忙修改某一段`)
            }
          }

          const isFirstRound = rawMessages.filter(m => m.role === 'user').length === 1
          if (isFirstRound) {
            systemParts.push('请在回复的最末尾另起一行，用 <title>简短标题</title> 给出不超过15字的对话标题（概括用户意图），不要在其他位置使用此标签。')
          }

          if (systemParts.length > 0) {
            msgs.unshift({
              role: 'system',
              content: systemParts.join('\n\n========\n\n'),
            })
          }

          // 调试日志：显示发送给模型的内容
          console.log('[Store] 发送给模型的消息:', {
            messageCount: msgs.length,
            systemPromptLength: msgs[0]?.role === 'system' ? (msgs[0].content as string)?.length : 0,
            systemPromptPreview: msgs[0]?.role === 'system' ? (msgs[0].content as string)?.slice(0, 500) + '...' : '无',
            userMessage: msgs[msgs.length - 1]?.content,
          })

          return msgs
        }

        const abortCtrl = new AbortController()
        abortControllers.set(conversationId!, abortCtrl)
        const useImageGen = isImageModel(conversation.model) && imageGenConfig

        const doStream = async (apiMessages: ChatMessage[], isGuidedFirstRound = false) => {
          const curSettings = get().settings
          let tools: any[] | undefined
          if (enableMcp) {
            try {
              const allTools = await getMcpToolsAsOpenAI(curSettings)

              // 引导模式第一轮：只给读取类工具，禁止写入
              if (isGuidedFirstRound) {
                const writeToolNames = ['edit_document', 'batch_edit_document', 'create_document', 'append_document']
                tools = allTools.filter((t: any) => !writeToolNames.includes(t.function?.name))
              } else {
                tools = allTools
              }
            } catch { /* MCP 不可用，静默降级 */ }
          }

          let fullContent = ''
          let fullThinking = ''
          const currentMessages: ChatMessage[] = [...apiMessages]
          // 写作规划：从 AI 回复中提取并持久保留
          let writingPlan: string | null = null
          // 进度追踪：记录已完成的操作摘要
          const progressSummary: string[] = []
          // 关键资源 ID：创建的文档/表格等
          const resourceIds: Record<string, string> = {}
          // 防止无限循环：允许长任务链路
          const MAX_TOOL_ROUNDS = 100
          // 上下文压缩配置
          const RECENT_ROUNDS_FULL = 5  // 最近 N 轮保留完整详情
          // 循环检测：记录最近的工具调用签名及结果
          const recentToolCalls: Array<{ signature: string; success: boolean }> = []
          const LOOP_DETECTION_WINDOW = 5  // 检测窗口大小
          const LOOP_THRESHOLD = 3         // 连续相同调用次数阈值

          // 提取写作规划
          const extractWritingPlan = (content: string): string | null => {
            const match = content.match(/<writing-plan>([\s\S]*?)<\/writing-plan>/)
            return match ? match[1].trim() : null
          }

          // 从文档内容提取目录结构
          const extractDocStructure = (content: string): string => {
            const lines = content.split('\n')
            const headings: string[] = []
            for (const line of lines) {
              // 匹配 Markdown 标题或带序号的标题
              if (/^#{1,4}\s/.test(line) || /^\d+\.\s/.test(line) || /^\d+\.\d+\s/.test(line)) {
                headings.push(line.trim())
                if (headings.length >= 20) break  // 最多保留 20 个标题
              }
            }
            if (headings.length === 0) {
              // 没有找到标题，返回前 500 字符的摘要
              return content.slice(0, 500) + (content.length > 500 ? '...[内容已截断]' : '')
            }
            return '文档结构：\n' + headings.join('\n')
          }

          // 压缩工具结果
          const compressToolResult = (toolName: string, result: string, args: Record<string, any>): string => {
            // create 类工具：提取资源 ID
            if (toolName.includes('create')) {
              const idMatch = result.match(/[“']?(doc_id|id|document_id)[“']?\s*[:=]\s*[“']?([^”'\s,}]+)[“']?/i)
              if (idMatch) {
                resourceIds[toolName] = idMatch[2]
                return `✅ 创建成功，doc_id=${idMatch[2]}`
              }
              return result.slice(0, 200)
            }
            // get_doc_detail：只保留结构
            if (toolName.includes('get_doc') || toolName.includes('get_document') || toolName === 'get_doc_detail') {
              return extractDocStructure(result)
            }
            // append/update/write：只保留状态
            if (toolName.includes('append') || toolName.includes('update') || toolName.includes('write') || toolName.includes('edit')) {
              const anchor = args.anchor || args.position || ''
              if (result.includes('成功') || result.includes('success') || !result.includes('error') && !result.includes('Error')) {
                return `✅ 写入成功${anchor ? ` (锚点: ${anchor})` : ''}`
              }
              return `❌ 写入失败: ${result.slice(0, 100)}`
            }
            // retrieve/search：只保留检索了哪些
            if (toolName.includes('retrieve') || toolName.includes('search')) {
              const titles = result.match(/【([^】]+)】/g)
              if (titles && titles.length > 0) {
                return `🔍 检索到 ${titles.length} 个相关片段：${titles.slice(0, 3).join('、')}${titles.length > 3 ? '...' : ''}`
              }
              return `🔍 检索完成`
            }
            // 默认：截断
            return result.slice(0, 300) + (result.length > 300 ? '...[已截断]' : '')
          }

          // 压缩历史消息
          const compressHistory = (messages: ChatMessage[], currentRound: number): ChatMessage[] => {
            if (currentRound <= RECENT_ROUNDS_FULL) return messages

            const compressed: ChatMessage[] = []
            let toolRound = 0

            for (let i = 0; i < messages.length; i++) {
              const msg = messages[i]

              // system 消息始终保留
              if (msg.role === 'system') {
                // 如果有写作规划，注入到 system 消息中
                if (writingPlan && typeof msg.content === 'string' && !msg.content.includes('<writing-plan>')) {
                  compressed.push({
                    ...msg,
                    content: msg.content + '\n\n========\n\n【当前写作规划】\n' + writingPlan
                  })
                } else {
                  compressed.push(msg)
                }
                continue
              }

              // 用户的第一条消息始终保留
              if (msg.role === 'user' && i <= 2) {
                compressed.push(msg)
                continue
              }

              // assistant 带 tool_calls 的消息：计算轮次
              if (msg.role === 'assistant' && (msg as any).tool_calls) {
                toolRound++
              }

              // 最近 N 轮：完整保留
              if (currentRound - toolRound <= RECENT_ROUNDS_FULL) {
                compressed.push(msg)
                continue
              }

              // 超过 N 轮的 tool 消息：压缩
              if (msg.role === 'tool') {
                const toolCallId = (msg as any).tool_call_id || ''
                // 找到对应的 tool_call 获取工具名
                const prevAssistant = messages[i - 1]
                let toolName = 'unknown'
                let toolArgs: Record<string, any> = {}
                if (prevAssistant && (prevAssistant as any).tool_calls) {
                  const tc = (prevAssistant as any).tool_calls.find((t: any) => t.id === toolCallId)
                  if (tc) {
                    toolName = tc.function?.name || tc.name || 'unknown'
                    try {
                      toolArgs = JSON.parse(tc.function?.arguments || tc.arguments || '{}')
                    } catch {}
                  }
                }
                compressed.push({
                  ...msg,
                  content: compressToolResult(toolName, typeof msg.content === 'string' ? msg.content : JSON.stringify(msg.content), toolArgs)
                })
                continue
              }

              // 其他消息保留
              compressed.push(msg)
            }

            // 如果有进度摘要，添加到上下文
            if (progressSummary.length > 0 && currentRound > RECENT_ROUNDS_FULL) {
              const summaryMsg: ChatMessage = {
                role: 'system',
                content: `【执行进度摘要】\n${progressSummary.slice(-10).join('\n')}\n\n已完成 ${currentRound} 轮工具调用。`
              }
              // 插入到 system 消息之后
              const systemIdx = compressed.findIndex(m => m.role === 'system')
              if (systemIdx >= 0) {
                compressed.splice(systemIdx + 1, 0, summaryMsg)
              }
            }

            return compressed
          }

          // 生成工具调用签名（名称 + 参数哈希）
          const getToolCallSignature = (name: string, args: string): string => {
            try {
              // 规范化参数顺序，便于比较
              const parsed = JSON.parse(args)
              const normalized = JSON.stringify(parsed, Object.keys(parsed).sort())
              return `${name}::${normalized}`
            } catch {
              return `${name}::${args}`
            }
          }

          // 检测是否存在循环（返回循环信息）
          const detectLoop = (signature: string): { isLoop: boolean; previousResults: boolean[] } => {
            // 检查是否有连续相同的调用
            const recentSame = recentToolCalls.filter(t => t.signature === signature).slice(-LOOP_THRESHOLD)
            if (recentSame.length >= LOOP_THRESHOLD) {
              return { isLoop: true, previousResults: recentSame.map(t => t.success) }
            }
            return { isLoop: false, previousResults: [] }
          }

          // 记录工具调用结果
          const recordToolCall = (signature: string, success: boolean) => {
            recentToolCalls.push({ signature, success })
            // 只保留最近的调用记录
            if (recentToolCalls.length > LOOP_DETECTION_WINDOW * 2) {
              recentToolCalls.shift()
            }
          }

          for (let round = 0; round <= MAX_TOOL_ROUNDS; round++) {
            // 检查对话是否还存在（用户可能已删除）
            if (!get().conversations.find(c => c.id === conversationId)) {
              console.log('[Tool Loop] 对话已被删除，中止工具循环')
              break
            }

            // 超过 RECENT_ROUNDS_FULL 轮后，压缩历史消息
            const messagesToSend = round > RECENT_ROUNDS_FULL
              ? compressHistory(currentMessages, round)
              : currentMessages

            let roundContent = ''
            let toolCalls: ToolCallInfo[] | undefined

            for await (const chunk of streamChat(
              messagesToSend,
              conversation.model,
              curSettings,
              abortCtrl.signal,
              enableThinking,
              tools,
              !!tools?.length,
            )) {
              if (!get().streamingIds[conversationId!]) break
              if (chunk.type === 'thinking') {
                fullThinking += chunk.text
              } else if (chunk.type === 'text') {
                roundContent += chunk.text
              } else if (chunk.type === 'tool_calls') {
                toolCalls = chunk.toolCalls
              }
              const displayContent = fullContent + (fullContent && roundContent ? '\n\n' : '') + roundContent
              set(state => ({
                conversations: state.conversations.map(c => {
                  if (c.id !== conversationId) return c
                  return {
                    ...c,
                    messages: c.messages.map(m =>
                      m.id === assistantMessageId
                        ? { ...m, content: displayContent, thinking: fullThinking || undefined }
                        : m
                    ),
                  }
                }),
              }))
            }

            // 检查并提取写作规划
            if (roundContent) {
              const plan = extractWritingPlan(roundContent)
              if (plan) {
                writingPlan = plan
                console.log('[Writing Plan] 提取到写作规划')
              }
              fullContent += (fullContent ? '\n\n' : '') + roundContent
            }

            if (!toolCalls?.length) break

            // 显示当前轮次（仅在第二轮及以后显示）
            if (round > 0) {
              fullThinking += `\n--- 第 ${round + 1} 轮工具调用 ---`
            }

            for (let tcIndex = 0; tcIndex < toolCalls.length; tcIndex++) {
              const tc = toolCalls[tcIndex]

              // 循环检测
              const signature = getToolCallSignature(tc.name, tc.arguments)
              const loopInfo = detectLoop(signature)

              if (loopInfo.isLoop) {
                // 检查之前的调用是否都成功了
                const allPreviousSuccess = loopInfo.previousResults.every(r => r)

                let skipReason: string
                if (allPreviousSuccess) {
                  skipReason = `⚠️ 操作被跳过：检测到连续 ${LOOP_THRESHOLD} 次相同的调用 (${tc.name})，且前几次均已成功执行。
请勿重复操作，检查当前文档状态后继续下一部分的撰写。`
                } else {
                  skipReason = `⚠️ 操作被跳过：检测到连续 ${LOOP_THRESHOLD} 次相同的调用 (${tc.name})，但之前的尝试未全部成功。
请尝试不同的方式（如更换锚点、拆分内容）继续操作。`
                }

                fullThinking += `\n\n⚠️ 检测到重复调用 (${tc.name})，跳过本次执行\n`

                // 构造跳过反馈，让 AI 知道并调整策略
                const resultText = skipReason

                currentMessages.push({
                  role: 'assistant',
                  content: null,
                  tool_calls: [{ id: tc.id, type: 'function', function: { name: tc.name, arguments: tc.arguments } }],
                })
                currentMessages.push({
                  role: 'tool',
                  tool_call_id: tc.id,
                  content: resultText,
                })

                // 记录为失败（因为被跳过了）
                recordToolCall(signature, false)

                // 添加到进度摘要
                progressSummary.push(`⚠️ ${tc.name} 被跳过（重复调用）`)

                continue  // 跳过本次调用，继续处理下一个
              }

              // 显示工具调用进度（如 [1/3]）
              const progressInfo = toolCalls.length > 1 ? `[${tcIndex + 1}/${toolCalls.length}] ` : ''
              let argsDisplay = ''
              try {
                const args = JSON.parse(tc.arguments)
                argsDisplay = Object.entries(args).map(([k, v]) => '  ' + k + ': ' + JSON.stringify(v)).join('\n')
              } catch {
                argsDisplay = '  ' + tc.arguments
              }
              fullThinking += '\n\n🔧 ' + progressInfo + '调用 ' + tc.name + '\n' + argsDisplay + '\n'

              // 记录开始时间，用于显示执行耗时
              const toolStartTime = Date.now()
              let elapsedTimer: ReturnType<typeof setInterval> | null = null

              // 根据工具名称获取具体操作类型提示
              const getToolTypeHint = (toolName: string): string => {
                if (toolName.includes('document') || toolName.includes('edit') || toolName.includes('write') || toolName.includes('append')) {
                  return '📝 正在写入文档...'
                } else if (toolName === 'retrieve' || toolName.includes('search')) {
                  return '🔍 正在检索文档...'
                } else if (toolName === 'get_doc_detail' || toolName.includes('read') || toolName.includes('get')) {
                  return '📖 正在读取文档...'
                } else if (toolName.includes('create')) {
                  return '✨ 正在创建...'
                } else if (toolName.includes('delete') || toolName.includes('remove')) {
                  return '🗑️ 正在删除...'
                } else if (toolName.includes('update')) {
                  return '🔄 正在更新...'
                }
                return ''
              }

              const toolTypeHint = getToolTypeHint(tc.name)

              // 定时更新执行状态，显示已用时间
              const updateExecutingStatus = (extraInfo?: string) => {
                const elapsed = Math.floor((Date.now() - toolStartTime) / 1000)
                let statusText = elapsed < 60
                  ? `⏳ 执行中… (${elapsed}s)`
                  : `⏳ 执行中… (${Math.floor(elapsed / 60)}m ${elapsed % 60}s)`
                if (toolTypeHint) statusText = toolTypeHint + ' ' + statusText
                if (extraInfo) statusText += ` ${extraInfo}`
                set(state => ({
                  conversations: state.conversations.map(c => {
                    if (c.id !== conversationId) return c
                    return {
                      ...c,
                      messages: c.messages.map(m =>
                        m.id === assistantMessageId
                          ? { ...m, thinking: fullThinking + statusText }
                          : m
                      ),
                    }
                  }),
                }))
              }

              // 立即显示一次，然后每秒更新
              updateExecutingStatus()
              elapsedTimer = setInterval(() => updateExecutingStatus(), 1000)

              let resultText: string
              let toolSuccess = false
              try {
                const args = JSON.parse(tc.arguments)
                const result = await callMcpTool(curSettings, tc.name, args)
                if (result?.content) {
                  resultText = (result.content as Array<{ type: string; text?: string }>)
                    .filter((c: any) => c.type === 'text')
                    .map((c: any) => c.text || '')
                    .join('\n')
                } else {
                  resultText = JSON.stringify(result)
                }
                const elapsed = Math.floor((Date.now() - toolStartTime) / 1000)
                const elapsedStr = elapsed < 60 ? `${elapsed}s` : `${Math.floor(elapsed / 60)}m ${elapsed % 60}s`
                fullThinking += `✅ 完成 (耗时 ${elapsedStr})\n`
                toolSuccess = true

                // 添加到进度摘要
                const argsObj = JSON.parse(tc.arguments)
                const anchorInfo = argsObj.anchor || argsObj.position || ''
                progressSummary.push(`✅ ${tc.name}${anchorInfo ? ` (${anchorInfo})` : ''} - 成功`)
              } catch (e) {
                const errMsg = e instanceof Error ? e.message : '调用失败'
                // 简化错误信息，只保留关键部分
                const shortErrMsg = errMsg.length > 100 ? errMsg.slice(0, 100) + '...' : errMsg
                resultText = 'Error: ' + errMsg
                const elapsed = Math.floor((Date.now() - toolStartTime) / 1000)
                const elapsedStr = elapsed < 60 ? `${elapsed}s` : `${Math.floor(elapsed / 60)}m ${elapsed % 60}s`
                // 根据错误类型给出更友好的提示
                let errorHint = ''
                if (errMsg.includes('超时') || errMsg.includes('timeout')) {
                  errorHint = '（服务响应超时，已自动重试）'
                } else if (errMsg.includes('network') || errMsg.includes('Network')) {
                  errorHint = '（网络连接异常）'
                } else if (errMsg.includes('session') || errMsg.includes('Session')) {
                  errorHint = '（会话已重建）'
                }
                fullThinking += `❌ ${shortErrMsg} ${errorHint}(耗时 ${elapsedStr})\n`
                toolSuccess = false

                // 添加到进度摘要
                progressSummary.push(`❌ ${tc.name} - 失败: ${shortErrMsg}`)
              } finally {
                if (elapsedTimer) clearInterval(elapsedTimer)
              }

              // 记录工具调用结果（用于循环检测）
              recordToolCall(signature, toolSuccess)

              currentMessages.push({
                role: 'assistant',
                content: null,
                tool_calls: [{ id: tc.id, type: 'function', function: { name: tc.name, arguments: tc.arguments } }],
              })
              currentMessages.push({
                role: 'tool',
                tool_call_id: tc.id,
                content: resultText,
              })

              set(state => ({
                conversations: state.conversations.map(c => {
                  if (c.id !== conversationId) return c
                  return {
                    ...c,
                    messages: c.messages.map(m =>
                      m.id === assistantMessageId
                        ? { ...m, thinking: fullThinking || undefined }
                        : m
                    ),
                  }
                }),
              }))
            }
          }
        }

        try {
          // ── 构建发送给模型的消息 ──────────────────────────────
          let apiMessages: ChatMessage[]

          // 检查是否是引导式 PRD 模式
          const guidedSession = useGuidedPrdStore.getState().getSession(conversationId!)
          // 判断是否是引导模式的第一轮（session 刚创建，features 为空）
          const isGuidedFirstRound = !!(guidedSession && guidedSession.features.length === 0)

          if (guidedSession && !useImageGen && !hasPdf) {
            // 引导模式：使用专用的 context 构建
            const rawMsgs = conversation.messages.filter(m => m.id !== assistantMessageId)

            // ── PRD 认知层集成：为引导模式提供业务知识上下文 ──
            let docContext = ''
            const prdStore = usePrdStore.getState()
            // enablePrd 参数来自 UI 开关，undefined 时视为启用
            const prdEnabled = enablePrd !== false
            const prdDocuments = prdStore.documents.filter(d => d.status === 'done')
            const normalizedEntities = prdStore.normalizedEntities

            if (prdEnabled && prdDocuments.length > 0 && normalizedEntities.length > 0) {
              try {
                // 使用 PRD 实体匹配
                const prdMatches = matchWithNormalizedEntities(content, prdDocuments, normalizedEntities, 3)

                if (prdMatches.length > 0) {
                  // 构建增强上下文
                  const enhancedContext = buildEnhancedContext(prdMatches, normalizedEntities)

                  // 检索匹配文档的相关内容
                  const matchedDocIds = prdMatches.map(m => m.docId)
                  const prdResults = await retrieveDocuments(content, matchedDocIds, get().settings).catch(() => [])

                  if (prdResults.length > 0 || enhancedContext) {
                    const parts: string[] = []

                    // 添加实体上下文（包含定义、别名、冲突警告等）
                    if (enhancedContext) {
                      parts.push('### 相关业务实体\n' + enhancedContext)
                    }

                    // 添加检索到的文档内容
                    if (prdResults.length > 0) {
                      const prdCtx = prdResults
                        .map(r => '【' + r.meta.title + '】\n' + r.content)
                        .join('\n\n---\n\n')
                      parts.push('### 相关文档内容\n' + prdCtx)
                    }

                    docContext = parts.join('\n\n')
                  }

                  // 更新 lastPrdMatches 供 UI 展示来源
                  set(state => ({
                    conversations: state.conversations.map(c => {
                      if (c.id !== conversationId) return c
                      return {
                        ...c,
                        lastPrdMatches: prdMatches.map(m => ({
                          docId: m.docId,
                          docTitle: m.docTitle,
                          matchedEntities: m.matchedEntities
                        }))
                      }
                    })
                  }))
                }
              } catch (e) {
                console.warn('[GuidedPRD] PRD 认知层匹配失败:', e)
              }
            }

            // 如果 PRD 认知层没有内容，回退到普通文档检索
            if (!docContext) {
              try {
                const builtMsgs = await buildMessages('native')
                const systemMsg = builtMsgs.find(m => m.role === 'system')
                if (systemMsg && typeof systemMsg.content === 'string') {
                  const docSections = systemMsg.content
                    .split('\n\n')
                    .filter(p =>
                      p.includes('【') ||
                      p.includes('PRD 认知层') ||
                      p.includes('关联文档') ||
                      p.includes('检索到')
                    )
                    .join('\n\n')
                  docContext = docSections
                }
              } catch {
                // 文档检索失败不影响主流程
              }
            }

            // 检查是否有待重新生成的功能点
            const regenerateFeatureId = useGuidedPrdStore.getState().consumePendingRegenerate(conversationId!)

            apiMessages = buildGuidedContext(guidedSession, rawMsgs, docContext, regenerateFeatureId) as ChatMessage[]
          } else if (hasPdf) {
            // PDF 模式
            set(state => ({
              conversations: state.conversations.map(c => {
                if (c.id !== conversationId) return c
                return {
                  ...c,
                  messages: c.messages.map(m =>
                    m.id === assistantMessageId
                      ? { ...m, content: '', thinking: '正在解析 PDF…' }
                      : m
                  ),
                }
              }),
            }))
            apiMessages = await buildMessages('images')
          } else {
            // 普通模式
            apiMessages = await buildMessages('native')
          }

          if (useImageGen) {
            const imgMessages = await buildMessages('extract')
            const result = await generateImage(
              imgMessages,
              conversation.model,
              get().settings,
              imageGenConfig,
              abortCtrl.signal,
              (status) => {
                set(state => ({
                  conversations: state.conversations.map(c => {
                    if (c.id !== conversationId) return c
                    return {
                      ...c,
                      messages: c.messages.map(m =>
                        m.id === assistantMessageId
                          ? { ...m, content: '', thinking: status }
                          : m
                      ),
                    }
                  }),
                }))
              },
            )
            const imageAttachments: Attachment[] = result.images.map(url => ({
              id: uuidv4(),
              type: 'image' as const,
              name: 'generated-image.png',
              mimeType: 'image/png',
              dataUrl: url,
            }))

            // 保存 AI 生成的图片到 IndexedDB
            if (imageAttachments.length) {
              saveAttachments(conversationId!, assistantMessageId, imageAttachments).catch(console.error)
            }

            set(state => ({
              conversations: state.conversations.map(c => {
                if (c.id !== conversationId) return c
                return {
                  ...c,
                  messages: c.messages.map(m =>
                    m.id === assistantMessageId
                      ? {
                          ...m,
                          content: result.text,
                          thinking: undefined,
                          attachments: imageAttachments.length ? imageAttachments : undefined,
                        }
                      : m
                  ),
                }
              }),
            }))
          } else {
            // 普通模式和引导模式都在上面构建好了 apiMessages
            // 引导模式第一轮限制写入工具
            await doStream(apiMessages, isGuidedFirstRound)
          }

          // 提取 AI 生成的标题
          {
            const conv = get().conversations.find(c => c.id === conversationId)
            const aMsg = conv?.messages.find(m => m.id === assistantMessageId)
            if (aMsg?.content) {
              const titleMatch = /<title>(.*?)<\/title>/s.exec(aMsg.content)
              if (titleMatch) {
                const newTitle = titleMatch[1].trim().slice(0, 30)
                const cleanedContent = aMsg.content.replace(/<title>.*?<\/title>/s, '').trimEnd()
                set(state => ({
                  conversations: state.conversations.map(c => {
                    if (c.id !== conversationId) return c
                    return {
                      ...c,
                      title: newTitle || c.title,
                      messages: c.messages.map(m =>
                        m.id === assistantMessageId ? { ...m, content: cleanedContent } : m
                      ),
                    }
                  }),
                }))
              }
            }
          }

          // excel-ops 后处理：解析 AI 输出的操作指令并执行
          const currentConv = get().conversations.find(c => c.id === conversationId)
          const assistantMsg = currentConv?.messages.find(m => m.id === assistantMessageId)
          if (assistantMsg?.content) {
            const parsed = parseExcelOpsFromContent(assistantMsg.content)
            console.log('[Excel-Ops] 解析结果:', parsed)
            if (parsed) {
              console.log('[Excel-Ops] 操作组数量:', parsed.operations.length)
              for (let i = 0; i < parsed.operations.length; i++) {
                console.log(`[Excel-Ops] 操作组 ${i + 1} (目标: ${parsed.operations[i].targetSheet}):`, JSON.stringify(parsed.operations[i].ops, null, 2))
              }
              const allMsgs = currentConv?.messages.filter(m => m.id !== assistantMessageId) || []

              const findOriginalSheets = (): import('./types').SheetData[] | undefined => {
                for (const msg of [...allMsgs].reverse()) {
                  if (msg.attachments?.some(a => a.parsedSheets?.length)) {
                    return msg.attachments.find(a => a.parsedSheets?.length)?.parsedSheets
                  }
                }
                return undefined
              }

              const findPreviousSheets = (): import('./types').SheetData[] | undefined => {
                for (const msg of [...allMsgs].reverse()) {
                  if (msg.role === 'assistant' && msg.processedSheets?.length) {
                    return msg.processedSheets
                  }
                }
                return undefined
              }

              let excelSheets: import('./types').SheetData[] | undefined
              if (parsed.source === 'previous') {
                excelSheets = findPreviousSheets() || findOriginalSheets()
              } else {
                excelSheets = findOriginalSheets() || findPreviousSheets()
              }

              console.log('[Excel-Ops] 找到的数据源:', excelSheets ? `${excelSheets.length} 个工作表` : '无')
              if (excelSheets?.length) {
                console.log('[Excel-Ops] 原始数据 - 工作表:', excelSheets.map(s => `${s.name}(${s.rows.length}行)`).join(', '))
                try {
                  // 使用新的多操作组执行函数
                  const resultSheets = executeMultipleOperations(excelSheets, parsed.operations)
                  console.log('[Excel-Ops] 处理后数据 - 工作表:', resultSheets.map(s => `${s.name}(${s.rows.length}行)`).join(', '))
                  set(state => ({
                    conversations: state.conversations.map(c => {
                      if (c.id !== conversationId) return c
                      return {
                        ...c,
                        messages: c.messages.map(m =>
                          m.id === assistantMessageId
                            ? { ...m, content: parsed.cleanContent, processedSheets: resultSheets, excelOpsRaw: assistantMsg.content }
                            : m
                        ),
                      }
                    }),
                  }))
                } catch (e) {
                  const errMsg = e instanceof Error ? e.message : '未知错误'
                  set(state => ({
                    conversations: state.conversations.map(c => {
                      if (c.id !== conversationId) return c
                      return {
                        ...c,
                        messages: c.messages.map(m =>
                          m.id === assistantMessageId
                            ? { ...m, content: parsed.cleanContent, excelError: '数据处理失败：' + errMsg }
                            : m
                        ),
                      }
                    }),
                  }))
                }
              }
            }
          }

          // ── agent-action 后处理（引导式 PRD + 普通模式都需要）──
          // start_guided 在普通对话里也可能出现，所以不限于 guidedSession 存在时
          {
            const convAfter = get().conversations.find(c => c.id === conversationId)
            const assistantMsgAfter = convAfter?.messages.find(m => m.id === assistantMessageId)
            if (assistantMsgAfter?.content) {
              const actions = parseAgentActions(assistantMsgAfter.content)
              if (actions.length > 0) {
                // 将 agent-action 代码块从消息内容中移除（保留自然语言部分）
                const cleanContent = stripAgentActions(assistantMsgAfter.content)
                set(state => ({
                  conversations: state.conversations.map(c => {
                    if (c.id !== conversationId) return c
                    return {
                      ...c,
                      messages: c.messages.map(m =>
                        m.id === assistantMessageId ? { ...m, content: cleanContent } : m
                      ),
                    }
                  }),
                }))
                // 异步执行 actions（不阻塞 UI）
                dispatchAgentActions(actions, conversationId!, get().settings).catch(
                  (err) => console.error('[AgentAction] dispatch 失败:', err)
                )
              }
            }
          }
        } catch (error) {
          if ((error as Error).name === 'AbortError') return
          const errorMsg = error instanceof Error ? error.message : '发生错误'
          set(state => ({
            conversations: state.conversations.map(c => {
              if (c.id !== conversationId) return c
              return {
                ...c,
                messages: c.messages.map(m =>
                  m.id === assistantMessageId
                    ? { ...m, content: '\u26a0\ufe0f ' + errorMsg, thinking: undefined }
                    : m
                ),
              }
            }),
          }))
        } finally {
          abortControllers.delete(conversationId!)
          set(state => {
            const next = { ...state.streamingIds }
            delete next[conversationId!]
            return { streamingIds: next }
          })
        }
      },

      retryMessage: async (messageId) => {
        const state = get()

        let targetConv: Conversation | undefined
        let assistantIdx = -1
        for (const conv of state.conversations) {
          const idx = conv.messages.findIndex(m => m.id === messageId)
          if (idx >= 0) {
            targetConv = conv
            assistantIdx = idx
            break
          }
        }
        if (!targetConv || assistantIdx < 0) return

        let userMsg: Message | undefined
        for (let i = assistantIdx - 1; i >= 0; i--) {
          if (targetConv.messages[i].role === 'user') {
            userMsg = targetConv.messages[i]
            break
          }
        }
        if (!userMsg) return

        const savedContent = userMsg.content
        const savedAttachments = userMsg.attachments
        const convId = targetConv.id
        const imgConfig = isImageModel(targetConv.model)
          ? (targetConv.imageGenConfig || { aspectRatio: '1:1', imageSize: '2K' })
          : undefined

        // 截断到该轮用户消息之前
        const userIdx = targetConv.messages.indexOf(userMsg)
        set(state => ({
          activeConversationId: convId,
          conversations: state.conversations.map(c => {
            if (c.id !== convId) return c
            return { ...c, messages: c.messages.slice(0, userIdx) }
          }),
        }))

        await get().sendMessage(savedContent, savedAttachments, undefined, imgConfig, convId)
      },

      reExecuteExcelOps: (messageId, forceOriginal) => {
        const state = get()
        let targetConv: Conversation | undefined
        let targetMsg: Message | undefined
        for (const conv of state.conversations) {
          const msg = conv.messages.find(m => m.id === messageId)
          if (msg) { targetConv = conv; targetMsg = msg; break }
        }
        if (!targetConv || !targetMsg?.excelOpsRaw) return

        const parsed = parseExcelOpsFromContent(targetMsg.excelOpsRaw)
        if (!parsed) return

        const msgIdx = targetConv.messages.findIndex(m => m.id === messageId)
        const priorMsgs = targetConv.messages.slice(0, msgIdx)

        const findOriginal = (): import('./types').SheetData[] | undefined => {
          for (const msg of [...priorMsgs].reverse()) {
            if (msg.attachments?.some(a => a.parsedSheets?.length)) {
              return msg.attachments.find(a => a.parsedSheets?.length)?.parsedSheets
            }
          }
          return undefined
        }
        const findPrevious = (): import('./types').SheetData[] | undefined => {
          for (const msg of [...priorMsgs].reverse()) {
            if (msg.role === 'assistant' && msg.processedSheets?.length) return msg.processedSheets
          }
          return undefined
        }

        const sheets = forceOriginal
          ? (findOriginal() || findPrevious())
          : (findPrevious() || findOriginal())
        if (!sheets?.length) return

        try {
          // 使用新的多操作组执行函数
          const resultSheets = executeMultipleOperations(sheets, parsed.operations)
          const convId = targetConv.id
          set(s => ({
            conversations: s.conversations.map(c => {
              if (c.id !== convId) return c
              return {
                ...c,
                messages: c.messages.map(m =>
                  m.id === messageId ? { ...m, processedSheets: resultSheets, excelError: undefined } : m
                ),
              }
            }),
          }))
        } catch (e) {
          const errMsg = e instanceof Error ? e.message : '未知错误'
          const convId = targetConv.id
          set(s => ({
            conversations: s.conversations.map(c => {
              if (c.id !== convId) return c
              return {
                ...c,
                messages: c.messages.map(m =>
                  m.id === messageId ? { ...m, excelError: '数据处理失败：' + errMsg } : m
                ),
              }
            }),
          }))
        }
      },

      stopStreaming: (conversationId) => {
        if (conversationId) {
          abortControllers.get(conversationId)?.abort()
          abortControllers.delete(conversationId)
          set(state => {
            const next = { ...state.streamingIds }
            delete next[conversationId]
            return { streamingIds: next }
          })
        } else {
          for (const [, ctrl] of abortControllers) ctrl.abort()
          abortControllers.clear()
          set({ streamingIds: {} })
        }
      },

      updateSettings: (newSettings) => {
        if (newSettings.documentAppId !== undefined ||
            newSettings.documentAppSecret !== undefined ||
            newSettings.documentApiBaseUrl !== undefined) {
          clearTokenCache()
        }
        if (newSettings.mcpBaseUrl !== undefined ||
            newSettings.mcpAppId !== undefined ||
            newSettings.mcpAppSecret !== undefined) {
          resetMcpSession()
        }
        set(state => ({
          settings: { ...state.settings, ...newSettings },
        }))
      },

      setHighlightMessage: (id) => set({ highlightMessageId: id }),
      setSearchHighlightKeyword: (keyword) => set({ searchHighlightKeyword: keyword }),
      addSplitPane: (conversationId) => {
        set(state => {
          if (state.splitPaneIds.includes(conversationId)) return state
          if (state.activeConversationId === conversationId) return state
          if (state.splitPaneIds.length >= 3) return state
          return { splitPaneIds: [...state.splitPaneIds, conversationId] }
        })
      },
      removeSplitPane: (conversationId) => {
        set(state => ({
          splitPaneIds: state.splitPaneIds.filter(id => id !== conversationId),
        }))
      },
      toggleSettings: () => set(state => ({ settingsOpen: !state.settingsOpen })),
      toggleSidebar: () => set(state => ({ sidebarOpen: !state.sidebarOpen })),
      setActiveView: (view) => set({ activeView: view }),

      // 从 IndexedDB 恢复附件数据
      restoreAttachments: async () => {
        const conversations = get().conversations
        // 收集所有需要恢复的附件 ID
        const attachmentIds: string[] = []
        for (const conv of conversations) {
          for (const msg of conv.messages) {
            if (msg.attachments) {
              for (const att of msg.attachments) {
                if (!att.dataUrl && att.id) {
                  attachmentIds.push(att.id)
                }
              }
            }
          }
        }

        if (attachmentIds.length === 0) return

        // 批量从 IndexedDB 获取
        const dataUrlMap = await getAttachments(attachmentIds)
        if (dataUrlMap.size === 0) return

        // 更新 state
        set(state => ({
          conversations: state.conversations.map(conv => ({
            ...conv,
            messages: conv.messages.map(msg => ({
              ...msg,
              attachments: msg.attachments?.map(att => {
                const dataUrl = dataUrlMap.get(att.id)
                return dataUrl ? { ...att, dataUrl } : att
              }),
            })),
          })),
        }))

        console.log(`[AttachmentStorage] 已恢复 ${dataUrlMap.size} 个附件`)
      },

      appendMessage: (conversationId, message) =>
        set((state) => ({
          conversations: state.conversations.map((c) =>
            c.id === conversationId
              ? { ...c, messages: [...c.messages, message], updatedAt: Date.now() }
              : c
          ),
        })),

      updateMessage: (conversationId, messageId, patch) =>
        set((state) => ({
          conversations: state.conversations.map((c) =>
            c.id === conversationId
              ? {
                  ...c,
                  messages: c.messages.map((m) =>
                    m.id === messageId ? { ...m, ...patch } : m
                  ),
                  updatedAt: Date.now(),
                }
              : c
          ),
        })),

      markPrdCardDone: (conversationId, messageId) =>
        set((state) => ({
          conversations: state.conversations.map((c) =>
            c.id === conversationId
              ? {
                  ...c,
                  messages: c.messages.map((m) =>
                    m.id === messageId ? { ...m, prdCardDone: true } : m
                  ),
                }
              : c
          ),
        })),
    }),
    {
      name: 'wave-chat-storage',
      partialize: (state) => ({
        conversations: state.conversations.map(c => ({
          ...c,
          lastPrdMatches: undefined, // 不持久化 PRD 匹配结果（临时 UI 状态）
          messages: c.messages.map(m => ({
            ...m,
            attachments: m.attachments?.map(a => ({ ...a, dataUrl: '', parsedSheets: undefined })),
            processedSheets: undefined,
          })),
        })),
        activeConversationId: state.activeConversationId,
        settings: state.settings,
        sidebarOpen: state.sidebarOpen,
      }),
      merge: (persisted, current) => {
        const stored = persisted as Partial<ChatState>
        const merged = { ...current, ...stored }
        if (stored.settings) {
          merged.settings = { ...DEFAULT_SETTINGS, ...stored.settings }
          merged.settings.apiBaseUrl = DEFAULT_SETTINGS.apiBaseUrl
          merged.settings.documentApiBaseUrl = DEFAULT_SETTINGS.documentApiBaseUrl
          // MCP 默认值与迁移
          if (!merged.settings.mcpBaseUrl) merged.settings.mcpBaseUrl = DEFAULT_SETTINGS.mcpBaseUrl
          if (!merged.settings.mcpAppId) merged.settings.mcpAppId = merged.settings.documentAppId || DEFAULT_SETTINGS.mcpAppId
          if (!merged.settings.mcpAppSecret) merged.settings.mcpAppSecret = merged.settings.documentAppSecret || DEFAULT_SETTINGS.mcpAppSecret
          // 系统模型默认值（新增字段迁移）
          if (!merged.settings.systemModel) merged.settings.systemModel = DEFAULT_SETTINGS.systemModel
          // 自动补入新增的默认模型
          if (Array.isArray(merged.settings.models)) {
            const existingIds = new Set(merged.settings.models.map(m => m.id))
            for (const dm of DEFAULT_SETTINGS.models) {
              if (!existingIds.has(dm.id)) {
                merged.settings.models.push(dm)
              }
            }
          }
        }
        if (merged.conversations) {
          merged.conversations = merged.conversations.map(c => {
            return {
              ...c,
              docIds: Array.isArray(c.docIds) ? c.docIds : [],
            }
          })
        }
        return merged
      },
    },
  ),
)

import { v4 as uuidv4 } from 'uuid'
import { callMcpTool } from './mcp'
import { useGuidedPrdStore } from '../stores/guidedPrdStore'
import { useChatStore } from '../store'
import type { AppSettings, Message } from '../types'
import type {
  AgentAction,
  AskAction,
  ConfirmFeatureAction,
  LockFeatureAction,
  WriteDocAction,
  UpdateDraftAction,
  RegisterFeatureAction,
  SetWriteTargetAction,
  StartGuidedAction,
  ImportDocStructureAction,
  FeatureDraftCardData,
  FeatureConfirmCardData,
  FeatureLockedCardData,
  WriteProgressCardData,
  WriteTargetChangeCardData,
  LockedFeature,
} from '../types/guided-prd'

// ============================================================
// 解析
// ============================================================

/**
 * 从 AI 输出内容中提取所有 agent-action 代码块并解析。
 * 一次回复可包含多个 agent-action 块。
 */
export function parseAgentActions(content: string): AgentAction[] {
  const actions: AgentAction[] = []
  const regex = /```agent-action\n([\s\S]*?)```/g
  let match: RegExpExecArray | null

  while ((match = regex.exec(content)) !== null) {
    try {
      const raw = match[1].trim()
      const parsed = JSON.parse(raw)
      if (parsed && typeof parsed.type === 'string') {
        actions.push(parsed as AgentAction)
      }
    } catch (e) {
      console.warn('[AgentAction] JSON 解析失败:', match[1], e)
    }
  }

  return actions
}

/**
 * 从 AI 输出内容中移除 agent-action 代码块，返回干净的文本。
 */
export function stripAgentActions(content: string): string {
  return content.replace(/```agent-action\n[\s\S]*?```/g, '').trim()
}

// ============================================================
// 执行
// ============================================================

/**
 * 执行一批 agent actions。
 * 按顺序执行，write_doc 失败时不中断其他 action。
 */
export async function dispatchAgentActions(
  actions: AgentAction[],
  conversationId: string,
  settings: AppSettings,
): Promise<void> {
  for (const action of actions) {
    try {
      await dispatchSingle(action, conversationId, settings)
    } catch (err) {
      console.error(`[AgentAction] 执行失败 type=${action.type}:`, err)
    }
  }
}

async function dispatchSingle(
  action: AgentAction,
  conversationId: string,
  settings: AppSettings,
): Promise<void> {
  const guided = useGuidedPrdStore.getState()
  const chat = useChatStore.getState()

  switch (action.type) {

    // ── 注册功能点 ──────────────────────────────────────────
    case 'register_feature': {
      const a = action as RegisterFeatureAction
      // 注册时带上 outline（如果有）
      guided.registerFeatures(conversationId, a.features.map(f => ({
        featureId: f.featureId,
        title: f.title,
        outline: f.outline,
      })))
      // 不再自动标记为 drilling，保持 pending 状态，等用户确认或 AI 开始写时再变
      break
    }

    // ── 追问用户 ────────────────────────────────────────────
    case 'ask': {
      const a = action as AskAction
      guided.incrementRound(conversationId, a.featureId)
      const cardData: FeatureDraftCardData = {
        type: 'feature_draft',
        featureId: a.featureId,
        featureTitle: a.featureTitle,
        cardId: uuidv4(),
        draft: a.draft,
        questions: a.questions,
        submitted: false,
      }
      const msg: Message = {
        id: uuidv4(),
        role: 'assistant',
        content: '',  // 文字已在主消息里，卡片只做交互
        timestamp: Date.now(),
        prdCard: cardData,
      }
      chat.appendMessage(conversationId, msg)
      break
    }

    // ── 请用户确认 ──────────────────────────────────────────
    case 'confirm_feature': {
      const a = action as ConfirmFeatureAction
      const cardData: FeatureConfirmCardData = {
        type: 'feature_confirm',
        featureId: a.featureId,
        featureTitle: a.featureTitle,
        cardId: uuidv4(),
        summary: a.summary,
        confirmed: false,
      }
      const msg: Message = {
        id: uuidv4(),
        role: 'assistant',
        content: '',
        timestamp: Date.now(),
        prdCard: cardData,
      }
      chat.appendMessage(conversationId, msg)
      break
    }

    // ── 直接锁定功能点 ──────────────────────────────────────
    case 'lock_feature': {
      const a = action as LockFeatureAction
      const locked: LockedFeature = {
        featureId: a.featureId,
        title: a.featureTitle,
        summary: a.summary,
        lockedAt: Date.now(),
      }
      guided.lockFeature(conversationId, a.featureId, locked)
      // 同时保存 summary 作为 generatedContent（用于右侧面板显示）
      guided.updateFeatureContent(conversationId, a.featureId, a.summary)

      const cardData: FeatureLockedCardData = {
        type: 'feature_locked',
        featureId: a.featureId,
        featureTitle: a.featureTitle,
        summary: a.summary,
      }
      const msg: Message = {
        id: uuidv4(),
        role: 'assistant',
        content: '',
        timestamp: Date.now(),
        prdCard: cardData,
      }
      chat.appendMessage(conversationId, msg)
      break
    }

    // ── 写入文档 ────────────────────────────────────────────
    case 'write_doc': {
      const a = action as WriteDocAction
      await executeWriteDoc(a, conversationId, settings)
      break
    }

    // ── 更新右侧草稿 ────────────────────────────────────────
    case 'update_draft': {
      const a = action as UpdateDraftAction
      guided.updatePrdDraft(conversationId, a.content)
      break
    }

    // ── AI 主动触发引导模式 ──────────────────────────────────
    case 'start_guided': {
      const a = action as StartGuidedAction
      if (!guided.getSession(conversationId)) {
        const chatState = useChatStore.getState()
        const conv = chatState.conversations.find(c => c.id === conversationId)
        const model = conv?.model ?? chatState.settings.defaultModel
        const msgCount = conv?.messages.length ?? 0
        guided.initSession(conversationId, model, msgCount)
      }
      console.log('[AgentAction] AI 触发引导模式:', a.reason)
      break
    }

    // ── 固化写入目标 ────────────────────────────────────────
    case 'set_write_target': {
      const a = action as SetWriteTargetAction
      const isChange = guided.setWriteTarget(conversationId, {
        description: a.description,
        mode: a.mode,
        docId: a.docId,
        docTitle: a.docTitle,
      })

      if (isChange) {
        // 目标变更：需要询问用户如何处理旧写入内容 + 新文档已有内容
        await handleWriteTargetChange(a, conversationId, settings)
      }
      // 首次设置无需弹卡片，静默固化即可
      break
    }

    // ── 从文档导入目录结构 ──────────────────────────────────
    case 'import_doc_structure': {
      const a = action as ImportDocStructureAction
      if (a.sections && a.sections.length > 0) {
        guided.importFeaturesFromDoc(conversationId, a.sections)
      }
      break
    }
  }
}

// ============================================================
// 写入文档（MCP）
// ============================================================

async function executeWriteDoc(
  action: WriteDocAction,
  conversationId: string,
  settings: AppSettings,
): Promise<void> {
  const guided = useGuidedPrdStore.getState()
  const chat = useChatStore.getState()

  // 初始化进度卡片
  const progressCardData: WriteProgressCardData = {
    type: 'write_progress',
    docId: action.docId ?? undefined,
    docTitle: action.docTitle,
    sections: action.sections.map((s) => ({
      heading: s.heading,
      status: 'pending',
      anchor: s.anchor,
    })),
  }
  const progressMsgId = uuidv4()
  const progressMsg: Message = {
    id: progressMsgId,
    role: 'assistant',
    content: '',
    timestamp: Date.now(),
    prdCard: progressCardData,
  }
  chat.appendMessage(conversationId, progressMsg)

  let targetDocId = action.docId

  try {
    // 如果需要新建文档
    if (!targetDocId) {
      const createResult = await callMcpTool(
        settings,
        'create_document',
        {
          title: action.docTitle ?? 'PRD 文档',
          knowledge_id: settings.mcpAppId, // 默认知识库
        },
      )
      const resultText = extractMcpText(createResult)
      // 从返回结果中提取 doc_id
      const idMatch = resultText.match(/doc_id[=:]\s*([^\s,}]+)/i)
        ?? resultText.match(/"id":\s*"([^"]+)"/i)
      targetDocId = idMatch?.[1] ?? null

      if (!targetDocId) {
        throw new Error('创建文档失败，未获取到 doc_id')
      }

      // 更新进度卡片的 docId
      chat.updateMessage(conversationId, progressMsgId, {
        prdCard: { ...progressCardData, docId: targetDocId },
      })
    } else {
      // 写入已有文档：先获取锚点信息
      let anchorMap: Record<string, string> = {}
      try {
        const detailResult = await callMcpTool(settings, 'get_doc_detail', {
          doc_id: targetDocId,
          format: 'json',
        })
        anchorMap = extractAnchorMap(extractMcpText(detailResult))
      } catch {
        // 拿不到锚点也继续，退化为追加写入
      }

      // 补全 sections 的 anchor
      action.sections = action.sections.map((s) => ({
        ...s,
        anchor: s.anchor ?? anchorMap[s.heading] ?? undefined,
      }))
    }

    // 逐章节写入
    const edits = action.sections.map((s, idx) => {
      // 更新进度为 writing
      const updatedSections = progressCardData.sections.map((ps, i) =>
        i === idx ? { ...ps, status: 'writing' as const } : ps
      )
      chat.updateMessage(conversationId, progressMsgId, {
        prdCard: { ...progressCardData, docId: targetDocId ?? undefined, sections: updatedSections },
      })

      return {
        anchor: s.anchor,
        action: s.anchor ? 'replace' : 'insert_after',
        content: formatSectionContent(s.heading, s.content),
      }
    })

    // 使用 batch_edit_document 一次性写入（从后往前，避免锚点偏移）
    const batchEdits = edits
      .map((e, i) => ({ ...e, _idx: i }))
      .filter((e) => e.anchor) // 有锚点的先批量更新
      .reverse()

    const appendEdits = edits.filter((e) => !e.anchor) // 无锚点的追加

    if (batchEdits.length > 0) {
      await callMcpTool(settings, 'batch_edit_document', {
        doc_id: targetDocId,
        edits: batchEdits.map((e) => ({
          anchor: e.anchor,
          action: e.action,
          content: e.content,
        })),
      })
    }

    for (const e of appendEdits) {
      await callMcpTool(settings, 'edit_document', {
        doc_id: targetDocId,
        action: 'insert_after',
        content: e.content,
      })
    }

    // 更新所有章节状态为 done
    const doneSections = progressCardData.sections.map((s) => ({
      ...s,
      status: 'done' as const,
    }))
    chat.updateMessage(conversationId, progressMsgId, {
      prdCard: {
        ...progressCardData,
        docId: targetDocId ?? undefined,
        sections: doneSections,
      },
      prdCardDone: true,
    })

    // 回填锚点到各功能点（供后续更新使用）
    const session = guided.getSession(conversationId)
    if (session && targetDocId) {
      for (let i = 0; i < action.sections.length; i++) {
        const section = action.sections[i]
        const feature = session.features.find(
          (f) => f.locked && section.heading.includes(f.title)
        )
        if (feature && section.anchor) {
          guided.updateFeatureAnchor(conversationId, feature.featureId, section.anchor)
        }
        // 记录写入章节（用于目标变更时的迁移判断）
        guided.addWrittenSection(conversationId, {
          docId: targetDocId,
          anchor: section.anchor ?? `section_${i}`,
          heading: section.heading,
          content: section.content,
        })
      }
    }

  } catch (err) {
    console.error('[AgentAction] write_doc 失败:', err)
    const failedSections = progressCardData.sections.map((s) => ({
      ...s,
      status: 'failed' as const,
    }))
    chat.updateMessage(conversationId, progressMsgId, {
      prdCard: {
        ...progressCardData,
        docId: targetDocId ?? undefined,
        sections: failedSections,
      },
    })
  }
}

// ============================================================
// 工具函数
// ============================================================

function extractMcpText(result: unknown): string {
  if (!result || typeof result !== 'object') return ''
  const r = result as { content?: Array<{ type: string; text?: string }> }
  if (!Array.isArray(r.content)) return ''
  return r.content
    .filter((c) => c.type === 'text')
    .map((c) => c.text ?? '')
    .join('\n')
}

/** 从 get_doc_detail 的 JSON 返回中提取 heading → anchor 映射 */
function extractAnchorMap(text: string): Record<string, string> {
  const map: Record<string, string> = {}
  try {
    const json = JSON.parse(text)
    const blocks: Array<{ _anchor?: string; type?: string; text?: string }> =
      Array.isArray(json) ? json : json.blocks ?? json.content ?? []
    for (const block of blocks) {
      if (block._anchor && block.type?.startsWith('heading') && block.text) {
        map[block.text] = block._anchor
      }
    }
  } catch {
    // 解析失败不影响流程
  }
  return map
}

function formatSectionContent(heading: string, content: string): string {
  // 如果 content 已经包含 heading，直接返回
  if (content.trimStart().startsWith('#')) return content
  return `## ${heading}\n\n${content}`
}

// ============================================================
// 写入目标变更处理
// ============================================================

async function handleWriteTargetChange(
  newTarget: SetWriteTargetAction,
  conversationId: string,
  settings: AppSettings,
): Promise<void> {
  const guided = useGuidedPrdStore.getState()
  const chat = useChatStore.getState()
  const session = guided.getSession(conversationId)
  if (!session) return

  const previousTarget = session.writeTarget?.previousTargets?.slice(-1)[0]
  const writtenInOld = previousTarget?.docId
    ? session.writtenSections.filter((w) => w.docId === previousTarget.docId)
    : []

  // 检查新文档已有的章节标题
  let conflictingHeadings: string[] = []
  if (newTarget.mode === 'existing_doc' && newTarget.docId) {
    try {
      const detailResult = await callMcpTool(settings, 'get_doc_detail', {
        doc_id: newTarget.docId,
        format: 'json',
      })
      const anchorMap = extractAnchorMap(extractMcpText(detailResult))
      const existingHeadings = Object.keys(anchorMap)
      const writtenHeadings = new Set(writtenInOld.map((w) => w.heading))
      conflictingHeadings = existingHeadings.filter((h) => writtenHeadings.has(h))
    } catch { /* 读取失败不阻断流程 */ }
  }

  // 旧目标无写入记录且新文档无冲突 → 静默切换
  if (writtenInOld.length === 0 && conflictingHeadings.length === 0) return

  const cardId = uuidv4()
  const cardData: WriteTargetChangeCardData = {
    type: 'write_target_change',
    cardId,
    oldTarget: {
      description: previousTarget?.description ?? '上一个目标',
      docId: previousTarget?.docId,
      docTitle: previousTarget?.docTitle,
    },
    newTarget: {
      description: newTarget.description,
      docId: newTarget.docId,
      docTitle: newTarget.docTitle,
      mode: newTarget.mode,
    },
    writtenSectionCount: writtenInOld.length,
    conflictingHeadings,
    migrateChoice: null,
    conflictChoice: null,
    resolved: false,
  }

  const msg: Message = {
    id: uuidv4(),
    role: 'assistant',
    content: '',
    timestamp: Date.now(),
    prdCard: cardData,
  }
  chat.appendMessage(conversationId, msg)
}

/**
 * 用户在变更卡片上确认后执行迁移，由 PrdCardRenderer 调用。
 */
export async function executeMigration(
  cardData: WriteTargetChangeCardData,
  conversationId: string,
  settings: AppSettings,
): Promise<void> {
  const guided = useGuidedPrdStore.getState()
  const session = guided.getSession(conversationId)
  if (!session) return

  const { migrateChoice, conflictChoice, oldTarget, newTarget } = cardData
  if (migrateChoice !== 'migrate' || !oldTarget.docId || !newTarget.docId) return

  const sectionsToMigrate = session.writtenSections.filter((w) => w.docId === oldTarget.docId)
  if (sectionsToMigrate.length === 0) return

  let anchorMap: Record<string, string> = {}
  try {
    const res = await callMcpTool(settings, 'get_doc_detail', { doc_id: newTarget.docId, format: 'json' })
    anchorMap = extractAnchorMap(extractMcpText(res))
  } catch { /* 忽略 */ }

  const edits = sectionsToMigrate.map((s) => {
    const existingAnchor = anchorMap[s.heading]
    if (existingAnchor && conflictChoice === 'overwrite') {
      return { anchor: existingAnchor, action: 'replace', content: formatSectionContent(s.heading, s.content) }
    }
    if (existingAnchor && conflictChoice === 'skip') return null
    return { anchor: undefined, action: 'insert_after' as const, content: formatSectionContent(s.heading, s.content) }
  }).filter(Boolean) as Array<{ anchor?: string; action: string; content: string }>

  if (edits.length > 0) {
    const withAnchor = edits.filter((e) => e.anchor).reverse()
    const withoutAnchor = edits.filter((e) => !e.anchor)
    if (withAnchor.length > 0) {
      await callMcpTool(settings, 'batch_edit_document', { doc_id: newTarget.docId, edits: withAnchor })
    }
    for (const e of withoutAnchor) {
      await callMcpTool(settings, 'edit_document', { doc_id: newTarget.docId, action: 'insert_after', content: e.content })
    }
  }

  guided.clearWrittenSections(conversationId, oldTarget.docId)
}

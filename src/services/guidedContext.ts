import type { Message } from '../types'
import type { GuidedSession } from '../types/guided-prd'
import { buildGuidedPrdSystemPrompt, buildLockedSummariesContext, buildOutlineContext } from '../prompts/guidedPrd'

// ============================================================
// 分层 Context 构建
//
// 固定层（每轮都有）：system prompt + 已锁定功能摘要 + 当前大纲
// 工作记忆（当前轮）：最近 N 轮对话（剔除 agent-action 卡片消息）
// 知识层（按需）：关联文档内容（由外部 PRD 匹配注入，不在这里处理）
// ============================================================

/** 工作记忆保留的最大轮次 */
const WORKING_MEMORY_MAX_ROUNDS = 15

/** 单条消息的最大字符长度 */
const MAX_MSG_CHARS = 4000

/**
 * 为引导式 PRD 对话构建完整的 messages 数组。
 * 替代普通 sendMessage 里的 currentMessages 构建逻辑。
 *
 * @param session       当前引导会话状态
 * @param conversation  原始对话消息列表
 * @param docContext    关联文档/知识库内容（来自 prdMatcher，外部传入）
 * @param regenerateFeatureId 用户修改摘要后要重新生成的功能点 ID
 */
export function buildGuidedContext(
  session: GuidedSession,
  conversationMessages: Message[],
  docContext: string,
  regenerateFeatureId?: string,
): Array<{ role: string; content: string }> {

  // ── 固定层：system prompt ────────────────────────────────
  const lockedSummaries = buildLockedSummariesContext(session.features)
  const outlineContext = buildOutlineContext(session.features)

  // 写入目标行
  const writeTargetLine = session.writeTarget
    ? `${session.writeTarget.description}（${session.writeTarget.mode === 'existing_doc' ? '更新已有文档' : '新建文档'}${session.writeTarget.docId ? `，docId: ${session.writeTarget.docId}` : ''}）`
    : '尚未确定 —— 请从用户消息推断并输出 set_write_target'

  // 中途接管说明（精简）
  const resumeNote = session.resumedFromMessageCount > 0
    ? `\n（接管已有 ${session.resumedFromMessageCount} 条消息的对话，请理解上文再继续）`
    : ''

  const systemContent = buildGuidedPrdSystemPrompt(
    lockedSummaries,
    docContext,
    writeTargetLine + resumeNote,
    outlineContext,
  )

  const messages: Array<{ role: string; content: string }> = [
    { role: 'system', content: systemContent },
  ]

  // ── 用户指定了要写/重新生成的模块 ─────────────────────────
  if (regenerateFeatureId) {
    const feature = session.features.find(f => f.featureId === regenerateFeatureId)
    if (feature) {
      messages.push({
        role: 'system',
        content: `[任务] 撰写「${feature.title}」模块${feature.outline ? `：${feature.outline}` : ''}。直接开始写，写完输出 lock_feature。`,
      })
    }
  }

  // ── 工作记忆：过滤 + 压缩历史消息 ───────────────────────
  const historyMsgs = conversationMessages
    .filter((m) => m.role !== 'system')
    .filter((m) => !isPrdCardMessage(m))
    .filter((m) => m.content.trim().length > 0)

  // 取最近 N 轮
  const recentMsgs = historyMsgs.slice(-WORKING_MEMORY_MAX_ROUNDS * 2)

  // 更老的消息压缩为摘要
  const olderMsgs = historyMsgs.slice(0, -WORKING_MEMORY_MAX_ROUNDS * 2)
  if (olderMsgs.length > 0) {
    const summary = buildOlderMessagesSummary(olderMsgs)
    if (summary) {
      messages.push({
        role: 'system',
        content: `[早期对话摘要] ${summary}`,
      })
    }
  }

  // 注入最近消息
  for (const msg of recentMsgs) {
    const content = stripAgentActionsFromContent(msg.content)
    if (!content.trim()) continue
    messages.push({
      role: msg.role,
      content: content.length > MAX_MSG_CHARS
        ? content.slice(0, MAX_MSG_CHARS) + '…[截断]'
        : content,
    })
  }

  return messages
}

// ============================================================
// 工具函数
// ============================================================

/** 判断消息是否是纯卡片消息（内容为空，只有 prdCard） */
function isPrdCardMessage(msg: Message): boolean {
  return !!msg.prdCard && msg.content.trim() === ''
}

/** 从消息内容中移除 agent-action 代码块 */
function stripAgentActionsFromContent(content: string): string {
  return content.replace(/```agent-action\n[\s\S]*?```/g, '').trim()
}

/**
 * 将较老的消息列表压缩为摘要文本。
 * 更激进的压缩：只保留关键信息。
 */
function buildOlderMessagesSummary(msgs: Message[]): string {
  if (msgs.length === 0) return ''

  // 只取关键的 user 消息摘要，跳过 AI 的长回复
  const userMsgs = msgs.filter(m => m.role === 'user')
  if (userMsgs.length === 0) return ''

  const snippets = userMsgs.slice(-5).map(m => {
    const text = m.content.slice(0, 60).replace(/\n/g, ' ')
    return text + (m.content.length > 60 ? '…' : '')
  })

  return `用户提过：${snippets.join('；')}`
}

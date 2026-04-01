import { useCallback } from 'react'
import { v4 as uuidv4 } from 'uuid'
import { useGuidedPrdStore } from '../stores/guidedPrdStore'
import { useChatStore } from '../store'
import type { LockedFeature } from '../types/guided-prd'

/**
 * useGuidedPrd — 引导式 PRD 的用户交互响应 Hook
 *
 * 不再驱动流程（流程由 AI 通过 agent-action 自主决策）。
 * 只处理用户在卡片上的操作：提交回答、确认功能点。
 */
export function useGuidedPrd(conversationId: string) {
  const guidedStore = useGuidedPrdStore()
  const chatStore = useChatStore()

  /**
   * 用户在 FeatureDraftCard 上提交回答。
   * 将用户回答作为普通消息发送，AI 会在下一轮继续追问或锁定。
   */
  const submitAnswer = useCallback(
    async (cardMsgId: string, answer: string) => {
      // 将卡片标记为已提交（只读）
      chatStore.markPrdCardDone(conversationId, cardMsgId)

      // 以用户消息身份发送回答，进入普通 sendMessage 流程
      // sendMessage 会检测到 guidedSession 存在，自动使用引导式 context
      await chatStore.sendMessage(
        answer,
        undefined,   // attachments
        undefined,   // thinking
        undefined,   // imageGenConfig
        conversationId,
        false,       // mcpEnabled（AI 自己会通过 agent-action 调 MCP）
        false,       // prdEnabled（引导模式不走普通 PRD 匹配）
      )
    },
    [conversationId, chatStore, guidedStore]
  )

  /**
   * 用户在 FeatureConfirmCard 上点击确认。
   * 将该功能点正式锁定，并继续下一轮对话。
   */
  const confirmFeature = useCallback(
    async (cardMsgId: string, featureId: string, featureTitle: string, summary: string) => {
      // 标记卡片为已确认
      chatStore.markPrdCardDone(conversationId, cardMsgId)

      // 锁定功能点
      const locked: LockedFeature = {
        featureId,
        title: featureTitle,
        summary,
        lockedAt: Date.now(),
      }
      guidedStore.lockFeature(conversationId, featureId, locked)

      // 发送一条确认消息，触发 AI 继续（写入文档或处理下一个功能点）
      await chatStore.sendMessage(
        `「${featureTitle}」功能点已确认，请继续。`,
        undefined,
        undefined,
        undefined,
        conversationId,
        false,
        false,
      )
    },
    [conversationId, chatStore, guidedStore]
  )

  /**
   * 用户拒绝确认，要求 AI 修改。
   */
  const rejectFeature = useCallback(
    async (cardMsgId: string, featureTitle: string, feedback: string) => {
      chatStore.markPrdCardDone(conversationId, cardMsgId)

      await chatStore.sendMessage(
        `「${featureTitle}」需要调整：${feedback}`,
        undefined,
        undefined,
        undefined,
        conversationId,
        false,
        false,
      )
    },
    [conversationId, chatStore]
  )

  return {
    submitAnswer,
    confirmFeature,
    rejectFeature,
  }
}

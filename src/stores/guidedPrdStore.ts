import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import { v4 as uuidv4 } from 'uuid'
import type {
  GuidedSession,
  GuidedPhase,
  FeatureItem,
  LockedFeature,
  WriteTarget,
  WriteTargetMode,
  WrittenSection,
} from '../types/guided-prd'

interface GuidedPrdState {
  sessions: Record<string, GuidedSession>

  getSession: (conversationId: string) => GuidedSession | undefined
  initSession: (conversationId: string, model: string, existingMessageCount?: number) => void
  destroySession: (conversationId: string) => void
  setPhase: (conversationId: string, phase: GuidedPhase) => void

  // 功能点管理
  registerFeatures: (conversationId: string, features: Array<{ featureId: string; title: string; outline?: string }>) => void
  startDrilling: (conversationId: string, featureId: string) => void
  incrementRound: (conversationId: string, featureId: string) => void
  lockFeature: (conversationId: string, featureId: string, locked: LockedFeature) => void
  updateFeatureAnchor: (conversationId: string, featureId: string, anchor: string) => void
  /** 更新功能点的摘要（用户编辑或 AI 生成） */
  updateFeatureOutline: (conversationId: string, featureId: string, outline: string, userEdited?: boolean) => void
  /** 更新功能点的标题 */
  updateFeatureTitle: (conversationId: string, featureId: string, title: string) => void
  /** 更新功能点的生成内容（写入文档后保存） */
  updateFeatureContent: (conversationId: string, featureId: string, content: string) => void
  /** 将功能点状态重置为 pending（用户修改摘要后触发重新生成） */
  resetFeatureStatus: (conversationId: string, featureId: string) => void
  /** 设置待重新生成的功能点 ID */
  setPendingRegenerate: (conversationId: string, featureId: string) => void
  /** 获取并清除待重新生成的功能点 ID */
  consumePendingRegenerate: (conversationId: string) => string | undefined
  /** 添加新模块 */
  addFeature: (conversationId: string, title: string, outline?: string) => void
  /** 删除模块 */
  deleteFeature: (conversationId: string, featureId: string) => void
  /** 调整模块顺序 */
  reorderFeatures: (conversationId: string, fromIndex: number, toIndex: number) => void
  /** 从文档导入模块（已有文档更新场景） */
  importFeaturesFromDoc: (
    conversationId: string,
    features: Array<{ title: string; anchor: string; contentPreview?: string }>
  ) => void
  /** 标记模块为"文档中已存在"（locked 状态，但可重新编辑） */
  markFeatureAsExisting: (conversationId: string, featureId: string) => void

  // 写入目标
  /** 设置（或更新）写入目标，返回是否触发了变更流程（旧目标存在且不同） */
  setWriteTarget: (
    conversationId: string,
    target: { description: string; mode: WriteTargetMode; docId?: string; docTitle?: string }
  ) => boolean
  /** 用户手动修改写入目标描述（UI 直接编辑） */
  updateWriteTargetDescription: (conversationId: string, description: string, docId?: string, docTitle?: string) => void

  // 写入记录
  addWrittenSection: (conversationId: string, section: Omit<WrittenSection, 'writtenAt'>) => void
  /** 迁移完成后清空旧文档的写入记录 */
  clearWrittenSections: (conversationId: string, docId: string) => void

  // 草稿
  updatePrdDraft: (conversationId: string, draft: string) => void
}

export const useGuidedPrdStore = create<GuidedPrdState>()(
  persist(
    (set, get) => ({
      sessions: {},

      getSession: (cid) => get().sessions[cid],

      initSession: (cid, model, existingMessageCount = 0) => {
        if (get().sessions[cid]) return
        const session: GuidedSession = {
          sessionId: cid,
          phase: 'active',
          features: [],
          prdDraft: '',
          model,
          writeTarget: undefined,
          writtenSections: [],
          resumedFromMessageCount: existingMessageCount,
          createdAt: Date.now(),
          updatedAt: Date.now(),
        }
        set((s) => ({ sessions: { ...s.sessions, [cid]: session } }))
      },

      destroySession: (cid) =>
        set((s) => {
          // eslint-disable-next-line @typescript-eslint/no-unused-vars
          const { [cid]: _removed, ...rest } = s.sessions
          return { sessions: rest }
        }),

      setPhase: (cid, phase) =>
        set((s) => ({
          sessions: { ...s.sessions, [cid]: { ...s.sessions[cid], phase, updatedAt: Date.now() } },
        })),

      registerFeatures: (cid, features) =>
        set((s) => {
          const session = s.sessions[cid]
          if (!session) return s
          const existingIds = new Set(session.features.map((f) => f.featureId))
          const newItems: FeatureItem[] = features
            .filter((f) => !existingIds.has(f.featureId))
            .map((f) => ({
              featureId: f.featureId,
              title: f.title,
              status: 'pending' as const,
              roundCount: 0,
              outline: f.outline,
              userEdited: false,
            }))
          return {
            sessions: {
              ...s.sessions,
              [cid]: { ...session, features: [...session.features, ...newItems], updatedAt: Date.now() },
            },
          }
        }),

      startDrilling: (cid, featureId) =>
        set((s) => ({
          sessions: {
            ...s.sessions,
            [cid]: {
              ...s.sessions[cid],
              features: s.sessions[cid].features.map((f) =>
                f.featureId === featureId ? { ...f, status: 'drilling' as const } : f
              ),
              updatedAt: Date.now(),
            },
          },
        })),

      incrementRound: (cid, featureId) =>
        set((s) => ({
          sessions: {
            ...s.sessions,
            [cid]: {
              ...s.sessions[cid],
              features: s.sessions[cid].features.map((f) =>
                f.featureId === featureId ? { ...f, roundCount: f.roundCount + 1 } : f
              ),
              updatedAt: Date.now(),
            },
          },
        })),

      lockFeature: (cid, featureId, locked) =>
        set((s) => ({
          sessions: {
            ...s.sessions,
            [cid]: {
              ...s.sessions[cid],
              features: s.sessions[cid].features.map((f) =>
                f.featureId === featureId
                  ? { ...f, status: 'locked' as const, locked, userEdited: false }  // 完成后清除"已改"标记
                  : f
              ),
              updatedAt: Date.now(),
            },
          },
        })),

      updateFeatureAnchor: (cid, featureId, anchor) =>
        set((s) => {
          const session = s.sessions[cid]
          if (!session) return s
          return {
            sessions: {
              ...s.sessions,
              [cid]: {
                ...session,
                features: session.features.map((f) =>
                  f.featureId === featureId && f.locked
                    ? { ...f, locked: { ...f.locked, anchor } }
                    : f
                ),
                updatedAt: Date.now(),
              },
            },
          }
        }),

      updateFeatureOutline: (cid, featureId, outline, userEdited = false) =>
        set((s) => {
          const session = s.sessions[cid]
          if (!session) return s
          return {
            sessions: {
              ...s.sessions,
              [cid]: {
                ...session,
                features: session.features.map((f) =>
                  f.featureId === featureId
                    ? { ...f, outline, userEdited: userEdited || f.userEdited }
                    : f
                ),
                updatedAt: Date.now(),
              },
            },
          }
        }),

      updateFeatureTitle: (cid, featureId, title) =>
        set((s) => {
          const session = s.sessions[cid]
          if (!session) return s
          return {
            sessions: {
              ...s.sessions,
              [cid]: {
                ...session,
                features: session.features.map((f) =>
                  f.featureId === featureId ? { ...f, title, userEdited: true } : f
                ),
                updatedAt: Date.now(),
              },
            },
          }
        }),

      updateFeatureContent: (cid, featureId, content) =>
        set((s) => {
          const session = s.sessions[cid]
          if (!session) return s
          return {
            sessions: {
              ...s.sessions,
              [cid]: {
                ...session,
                features: session.features.map((f) =>
                  f.featureId === featureId ? { ...f, generatedContent: content } : f
                ),
                updatedAt: Date.now(),
              },
            },
          }
        }),

      resetFeatureStatus: (cid, featureId) =>
        set((s) => {
          const session = s.sessions[cid]
          if (!session) return s
          return {
            sessions: {
              ...s.sessions,
              [cid]: {
                ...session,
                features: session.features.map((f) =>
                  f.featureId === featureId
                    ? { ...f, status: 'pending' as const, locked: undefined, generatedContent: undefined }
                    : f
                ),
                updatedAt: Date.now(),
              },
            },
          }
        }),

      addFeature: (cid, title, outline) =>
        set((s) => {
          const session = s.sessions[cid]
          if (!session) return s
          const newFeature: FeatureItem = {
            featureId: `f_${Date.now()}`,
            title,
            status: 'pending',
            roundCount: 0,
            outline,
            userEdited: false,  // 用户手动添加的模块，没有"原始版本"，不算"已改"
          }
          return {
            sessions: {
              ...s.sessions,
              [cid]: {
                ...session,
                features: [...session.features, newFeature],
                updatedAt: Date.now(),
              },
            },
          }
        }),

      deleteFeature: (cid, featureId) =>
        set((s) => {
          const session = s.sessions[cid]
          if (!session) return s
          return {
            sessions: {
              ...s.sessions,
              [cid]: {
                ...session,
                features: session.features.filter((f) => f.featureId !== featureId),
                updatedAt: Date.now(),
              },
            },
          }
        }),

      reorderFeatures: (cid, fromIndex, toIndex) =>
        set((s) => {
          const session = s.sessions[cid]
          if (!session) return s
          const features = [...session.features]
          const [removed] = features.splice(fromIndex, 1)
          features.splice(toIndex, 0, removed)
          return {
            sessions: {
              ...s.sessions,
              [cid]: {
                ...session,
                features,
                updatedAt: Date.now(),
              },
            },
          }
        }),

      importFeaturesFromDoc: (cid, features) =>
        set((s) => {
          const session = s.sessions[cid]
          if (!session) return s
          // 根据 anchor 去重，避免重复导入
          const existingAnchors = new Set(
            session.features.filter(f => f.docAnchor).map(f => f.docAnchor)
          )
          const newFeatures = features
            .filter(f => !existingAnchors.has(f.anchor))
            .map((f, i) => ({
              featureId: `f_doc_${Date.now()}_${i}`,
              title: f.title,
              status: 'locked' as const,  // 文档中已存在的内容，初始为 locked
              roundCount: 0,
              source: 'doc' as const,
              docAnchor: f.anchor,
              originalContent: f.contentPreview,
              outline: f.contentPreview?.slice(0, 200),  // 用内容摘要作为 outline
              userEdited: false,
            }))
          return {
            sessions: {
              ...s.sessions,
              [cid]: {
                ...session,
                features: [...session.features, ...newFeatures],
                updatedAt: Date.now(),
              },
            },
          }
        }),

      markFeatureAsExisting: (cid, featureId) =>
        set((s) => {
          const session = s.sessions[cid]
          if (!session) return s
          return {
            sessions: {
              ...s.sessions,
              [cid]: {
                ...session,
                features: session.features.map(f =>
                  f.featureId === featureId
                    ? { ...f, status: 'locked' as const, source: 'doc' as const }
                    : f
                ),
                updatedAt: Date.now(),
              },
            },
          }
        }),

      setWriteTarget: (cid, incoming) => {
        const session = get().sessions[cid]
        if (!session) return false

        const existing = session.writeTarget
        // 判断是否真正变更（docId 相同或 description 相同则视为无变化）
        const isSameTarget =
          existing &&
          (existing.docId
            ? existing.docId === incoming.docId
            : existing.description === incoming.description)

        if (isSameTarget) return false // 无变化，不触发变更流程

        const isChange = !!existing // 已有目标 → 变更；无目标 → 首次设置

        const newTarget: WriteTarget = {
          description: incoming.description,
          mode: incoming.mode,
          docId: incoming.docId,
          docTitle: incoming.docTitle,
          confirmedAt: Date.now(),
          previousTargets: existing
            ? [
                ...(existing.previousTargets ?? []),
                {
                  description: existing.description,
                  docId: existing.docId,
                  docTitle: existing.docTitle,
                  changedAt: Date.now(),
                },
              ]
            : [],
        }

        set((s) => ({
          sessions: {
            ...s.sessions,
            [cid]: { ...s.sessions[cid], writeTarget: newTarget, updatedAt: Date.now() },
          },
        }))

        return isChange
      },

      updateWriteTargetDescription: (cid, description, docId, docTitle) =>
        set((s) => {
          const session = s.sessions[cid]
          if (!session?.writeTarget) return s
          return {
            sessions: {
              ...s.sessions,
              [cid]: {
                ...session,
                writeTarget: {
                  ...session.writeTarget,
                  description,
                  docId: docId ?? session.writeTarget.docId,
                  docTitle: docTitle ?? session.writeTarget.docTitle,
                },
                updatedAt: Date.now(),
              },
            },
          }
        }),

      addWrittenSection: (cid, section) =>
        set((s) => {
          const session = s.sessions[cid]
          if (!session) return s
          // 同一 docId + anchor 的章节去重（更新时覆盖旧记录）
          const filtered = session.writtenSections.filter(
            (w) => !(w.docId === section.docId && w.anchor === section.anchor)
          )
          return {
            sessions: {
              ...s.sessions,
              [cid]: {
                ...session,
                writtenSections: [
                  ...filtered,
                  { ...section, writtenAt: Date.now() },
                ],
                updatedAt: Date.now(),
              },
            },
          }
        }),

      clearWrittenSections: (cid, docId) =>
        set((s) => {
          const session = s.sessions[cid]
          if (!session) return s
          return {
            sessions: {
              ...s.sessions,
              [cid]: {
                ...session,
                writtenSections: session.writtenSections.filter((w) => w.docId !== docId),
                updatedAt: Date.now(),
              },
            },
          }
        }),

      updatePrdDraft: (cid, draft) =>
        set((s) => ({
          sessions: {
            ...s.sessions,
            [cid]: { ...s.sessions[cid], prdDraft: draft, updatedAt: Date.now() },
          },
        })),

      setPendingRegenerate: (cid, featureId) =>
        set((s) => {
          const session = s.sessions[cid]
          if (!session) return s
          return {
            sessions: {
              ...s.sessions,
              [cid]: {
                ...session,
                pendingRegenerateFeatureId: featureId,
                updatedAt: Date.now(),
              },
            },
          }
        }),

      consumePendingRegenerate: (cid) => {
        const session = get().sessions[cid]
        if (!session?.pendingRegenerateFeatureId) return undefined

        const featureId = session.pendingRegenerateFeatureId
        set((s) => ({
          sessions: {
            ...s.sessions,
            [cid]: {
              ...s.sessions[cid],
              pendingRegenerateFeatureId: undefined,
              updatedAt: Date.now(),
            },
          },
        }))
        return featureId
      },
    }),
    {
      name: 'guided-prd-store',
      partialize: (state) => ({ sessions: state.sessions }),
    }
  )
)

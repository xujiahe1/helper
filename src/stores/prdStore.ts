import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import { v4 as uuidv4 } from 'uuid'

// ========== 面向类型枚举（固定 8 类）==========
export type EntityAspect =
  | 'definition'       // 基础定义 - "X是什么"
  | 'usage'            // 使用场景 - "X怎么用/业务流程"
  | 'implementation'   // 技术实现 - "X如何实现/架构设计"
  | 'config'           // 配置项 - "X的参数/配置"
  | 'permission'       // 权限角色 - "X的权限/角色"
  | 'integration'      // 集成对接 - "X与Y如何对接"
  | 'history'          // 历史变更 - "X的版本演进"
  | 'other'            // 其他 - 兜底分类

// 面向类型的显示名称映射
export const ASPECT_LABELS: Record<EntityAspect, string> = {
  definition: '定义',
  usage: '使用场景',
  implementation: '技术实现',
  config: '配置项',
  permission: '权限角色',
  integration: '集成对接',
  history: '历史变更',
  other: '其他',
}

// ========== 知识库 ==========
export interface KnowledgeBase {
  id: string
  name: string
  createdAt: number
  updatedAt: number
}

// ========== 原始实体（从文档提取的原始形态）==========
export interface EntitySource {
  docId: string
  method: 'llm' | 'manual'
}

export interface PrdEntity {
  id: string
  name: string
  description: string
  source?: EntitySource
  createdAt?: number
  updatedAt?: number
}

// ========== 归一化实体（合并后的统一实体）==========
export interface EntityVersion {
  rawEntityId: string
  docId: string
  docTitle: string
  docUpdateTime: number      // 文档更新时间（用于排序）
  description: string
  extractedAt: number
  aspect?: EntityAspect           // 面向分类
  aspectConfidence?: number       // 分类置信度 (0-1)
}

// 面向内的冲突信息
export interface AspectConflict {
  aspect: EntityAspect
  versions: string[]  // rawEntityId 列表
  summary: string
}

// 冲突类型
export type ConflictType = 'none' | 'definition_conflict' | 'config_conflict' | 'context_dependent'

// 冲突解决方式
export type ConflictResolution = 'pending' | 'resolved' | 'authoritative' | 'merged' | 'split'

// 关系冲突信息
export interface RelationConflictInfo {
  relationIds: string[]        // 冲突的关系 ID 列表
  conflictSummary: string      // 冲突描述
}

// 冲突解决记录
export interface ConflictResolutionRecord {
  resolvedAt: number
  resolution: ConflictResolution
  // 如果是 authoritative，记录权威版本
  authoritativeVersionId?: string
  // 如果是 merged，记录合并后的描述
  mergedDescription?: string
  // 如果是 split，记录拆分出去的别名
  splitAlias?: string
  // 用户备注
  note?: string
}

export interface NormalizedEntity {
  id: string
  canonicalName: string      // 规范名称
  aliases: string[]          // 别名列表（包含canonicalName）
  manualAliases?: string[]   // 用户手动添加的别名（归一化时保留）
  versions: EntityVersion[]  // 版本历史（按docUpdateTime降序）

  // 所属知识库
  knowledgeBaseId: string
  // 当前定义（最新版本）
  currentDescription: string
  currentDocId: string
  currentDocTitle: string
  currentDocUpdateTime: number

  // 冲突状态（兼容旧字段）
  hasConflict: boolean
  conflictSummary?: string

  // 新增：冲突细化
  conflictType?: ConflictType
  aspectConflicts?: AspectConflict[]

  // 冲突解决
  conflictResolution?: ConflictResolutionRecord

  // 实体关系
  relations: EntityRelation[]

  // 关系冲突列表
  relationConflicts?: RelationConflictInfo[]

  // 归一化方式
  normalizationMethod: 'auto' | 'manual'

  createdAt: number
  updatedAt: number
}

// ========== 实体关系（实体间的关联关系）==========

/** 关系来源信息 */
export interface RelationSource {
  docId: string                 // 来源文档 PrdDocument.docId
  docTitle: string              // 来源文档标题
  anchor?: string               // 来源段落锚点（如"3.2"）
}

/** 实体关系：描述两个归一化实体之间的关联 */
export interface EntityRelation {
  id: string
  targetEntityId: string        // 关联的目标实体 NormalizedEntity.id
  targetEntityName: string      // 目标实体名称（冗余，用于展示）
  relationType: string          // 关系类型（自由文本，如"依赖"、"包含"、"基于…生成"）
  direction: 'outgoing' | 'incoming' | 'bidirectional'  // 关系方向
  description?: string          // 关系描述（可选详细说明）
  sources: RelationSource[]     // 关系来源（可能多个文档提及同一关系）
  confidence: number            // 0-1
  method: 'auto' | 'manual'    // 创建方式
  // 冲突相关字段
  hasConflict?: boolean                    // 是否存在冲突
  conflictWith?: string[]                  // 冲突的关系 ID 列表
  conflictResolution?: ConflictResolutionRecord  // 冲突解决记录
  createdAt: number
  updatedAt: number
}

// ========== 文档 ==========
export type PrdDocStatus = 'pending' | 'parsing' | 'done' | 'error'

export interface PrdDocument {
  id: string
  docId: string
  docUrl: string
  title: string
  status: PrdDocStatus
  errorMessage?: string
  entities: PrdEntity[]      // 原始实体（保持兼容）
  rawContent?: string
  docUpdateTime?: number     // 文档更新时间（从API获取）
  knowledgeBaseId?: string   // 所属知识库 ID
  createdAt: number
  updatedAt: number
}

// ========== Store State ==========
interface PrdState {
  // 知识库
  knowledgeBases: KnowledgeBase[]
  activeKnowledgeBaseId: string | null  // 当前选中的知识库（管理视图）
  chatKnowledgeBaseIds: string[]        // 对话时引用的知识库（默认全选）

  documents: PrdDocument[]
  normalizedEntities: NormalizedEntity[]  // 新增：归一化实体
  selectedDocId: string | null
  selectedEntityId: string | null

  // 知识库操作
  createKnowledgeBase: (name: string) => string
  renameKnowledgeBase: (id: string, name: string) => void
  deleteKnowledgeBase: (id: string) => void
  setActiveKnowledgeBase: (id: string | null) => void
  getKnowledgeBaseById: (id: string) => KnowledgeBase | undefined

  // 对话时知识库选择
  setChatKnowledgeBaseIds: (ids: string[]) => void
  toggleChatKnowledgeBase: (id: string) => void
  selectAllChatKnowledgeBases: () => void
  getChatSelectedDocs: () => PrdDocument[]  // 获取对话时选中的知识库的文档

  // 获取当前知识库的文档
  getDocumentsForKnowledgeBase: (kbId: string) => PrdDocument[]
  // 获取当前活跃知识库的文档（管理视图）
  getActiveKnowledgeBaseDocs: () => PrdDocument[]
  // 获取孤立文档（没有 knowledgeBaseId 的文档）
  getOrphanedDocuments: () => PrdDocument[]
  // 将孤立文档迁移到指定知识库
  migrateOrphanedDocuments: (targetKbId: string) => number
  // 删除所有孤立文档
  deleteOrphanedDocuments: () => number

  // 文档操作
  addDocument: (docUrl: string, docId: string, knowledgeBaseId?: string) => string
  updateDocument: (id: string, updates: Partial<PrdDocument>) => void
  removeDocument: (id: string) => void
  setDocumentStatus: (id: string, status: PrdDocStatus, errorMessage?: string) => void
  setDocumentEntities: (id: string, entities: PrdEntity[]) => void
  updateDocumentTitle: (id: string, title: string) => void
  getDocumentById: (id: string) => PrdDocument | undefined

  // 原始实体操作（保持兼容）
  addEntity: (docId: string, entity: Omit<PrdEntity, 'id'>) => string | null
  updateEntity: (docId: string, entityId: string, updates: Partial<Omit<PrdEntity, 'id'>>) => void
  removeEntity: (docId: string, entityId: string) => void
  getEntityById: (docId: string, entityId: string) => PrdEntity | undefined

  // 归一化实体操作
  setNormalizedEntities: (entities: NormalizedEntity[]) => void
  updateNormalizedEntity: (id: string, updates: Partial<NormalizedEntity>) => void
  mergeNormalizedEntities: (sourceId: string, targetId: string) => void  // 手动合并
  splitNormalizedEntity: (id: string, aliasToSplit: string) => void      // 手动拆分
  getNormalizedEntityByAlias: (alias: string) => NormalizedEntity | undefined

  // 别名管理
  addManualAlias: (entityId: string, alias: string) => void
  removeAlias: (entityId: string, alias: string) => void
  updateCanonicalName: (entityId: string, newName: string) => void

  // 冲突解决操作
  resolveConflict: (entityId: string, resolution: ConflictResolution, options?: {
    authoritativeVersionId?: string
    mergedDescription?: string
    splitAlias?: string
    note?: string
  }) => void
  resetConflictResolution: (entityId: string) => void

  // 版本面向调整
  updateVersionAspect: (entityId: string, versionId: string, newAspect: EntityAspect) => void

  // 实体关系操作
  addEntityRelation: (entityId: string, relation: Omit<EntityRelation, 'id' | 'createdAt' | 'updatedAt'>) => string | null
  removeEntityRelation: (entityId: string, relationId: string) => void
  updateEntityRelation: (entityId: string, relationId: string, updates: Partial<Omit<EntityRelation, 'id' | 'createdAt' | 'updatedAt'>>) => void
  getEntityRelations: (entityId: string) => EntityRelation[]
  setEntityRelations: (entityId: string, relations: EntityRelation[]) => void
  getRelationsBetween: (entityId1: string, entityId2: string) => EntityRelation[]

  // 关系冲突操作
  setRelationConflicts: (entityId: string, conflicts: RelationConflictInfo[]) => void
  resolveRelationConflict: (entityId: string, relationId: string, resolution: ConflictResolution, options?: {
    note?: string
  }) => void
  resetRelationConflictResolution: (entityId: string, relationId: string) => void

  // 选择状态
  setSelectedDoc: (id: string | null) => void
  setSelectedEntity: (entityId: string | null) => void
}

export const usePrdStore = create<PrdState>()(
  persist(
    (set, get) => ({
      knowledgeBases: [],
      activeKnowledgeBaseId: null,
      chatKnowledgeBaseIds: [],  // 对话时引用的知识库，默认空（会在getter中处理为全选）
      documents: [],
      normalizedEntities: [],
      selectedDocId: null,
      selectedEntityId: null,

      // ========== 知识库操作 ==========
      createKnowledgeBase: (name) => {
        const id = uuidv4()
        const now = Date.now()
        const kb: KnowledgeBase = {
          id,
          name,
          createdAt: now,
          updatedAt: now,
        }
        set(state => {
          const newKbs = [...state.knowledgeBases, kb]
          // 如果是第一个知识库，自动设为活跃
          const activeId = state.knowledgeBases.length === 0 ? id : state.activeKnowledgeBaseId
          return {
            knowledgeBases: newKbs,
            activeKnowledgeBaseId: activeId,
            // 新建知识库自动加入对话选择
            chatKnowledgeBaseIds: [...state.chatKnowledgeBaseIds, id],
          }
        })
        return id
      },

      renameKnowledgeBase: (id, name) => {
        set(state => ({
          knowledgeBases: state.knowledgeBases.map(kb =>
            kb.id === id ? { ...kb, name, updatedAt: Date.now() } : kb
          ),
        }))
      },

      deleteKnowledgeBase: (id) => {
        set(state => {
          const newKbs = state.knowledgeBases.filter(kb => kb.id !== id)
          // 删除该知识库下的所有文档
          const newDocs = state.documents.filter(doc => doc.knowledgeBaseId !== id)
          // 如果删除的是活跃知识库，切换到第一个
          let newActiveId = state.activeKnowledgeBaseId
          if (state.activeKnowledgeBaseId === id) {
            newActiveId = newKbs.length > 0 ? newKbs[0].id : null
          }
          // 从对话选择中移除
          const newChatIds = state.chatKnowledgeBaseIds.filter(kbId => kbId !== id)
          return {
            knowledgeBases: newKbs,
            documents: newDocs,
            activeKnowledgeBaseId: newActiveId,
            chatKnowledgeBaseIds: newChatIds,
          }
        })
      },

      setActiveKnowledgeBase: (id) => {
        set({ activeKnowledgeBaseId: id })
      },

      getKnowledgeBaseById: (id) => {
        return get().knowledgeBases.find(kb => kb.id === id)
      },

      // ========== 对话时知识库选择 ==========
      setChatKnowledgeBaseIds: (ids) => {
        set({ chatKnowledgeBaseIds: ids })
      },

      toggleChatKnowledgeBase: (id) => {
        set(state => {
          const { chatKnowledgeBaseIds, knowledgeBases } = state
          // 如果当前为空（表示全选），切换为取消选择该项
          const effectiveIds = chatKnowledgeBaseIds.length === 0
            ? knowledgeBases.map(kb => kb.id)
            : chatKnowledgeBaseIds

          if (effectiveIds.includes(id)) {
            // 至少保留一个选中
            if (effectiveIds.length <= 1) return state
            return { chatKnowledgeBaseIds: effectiveIds.filter(kbId => kbId !== id) }
          } else {
            return { chatKnowledgeBaseIds: [...effectiveIds, id] }
          }
        })
      },

      selectAllChatKnowledgeBases: () => {
        set(state => ({
          chatKnowledgeBaseIds: state.knowledgeBases.map(kb => kb.id)
        }))
      },

      getChatSelectedDocs: () => {
        const { documents, chatKnowledgeBaseIds, knowledgeBases } = get()
        // 如果没有知识库，返回空
        if (knowledgeBases.length === 0) return []
        // 如果 chatKnowledgeBaseIds 为空，表示全选
        const effectiveIds = chatKnowledgeBaseIds.length === 0
          ? knowledgeBases.map(kb => kb.id)
          : chatKnowledgeBaseIds
        return documents.filter(doc => doc.knowledgeBaseId && effectiveIds.includes(doc.knowledgeBaseId))
      },

      getDocumentsForKnowledgeBase: (kbId) => {
        return get().documents.filter(doc => doc.knowledgeBaseId === kbId)
      },

      getActiveKnowledgeBaseDocs: () => {
        const { documents, activeKnowledgeBaseId } = get()
        // 必须选中一个知识库，否则返回空
        if (!activeKnowledgeBaseId) {
          return []
        }
        return documents.filter(doc => doc.knowledgeBaseId === activeKnowledgeBaseId)
      },

      getOrphanedDocuments: () => {
        return get().documents.filter(doc => !doc.knowledgeBaseId)
      },

      migrateOrphanedDocuments: (targetKbId) => {
        const orphaned = get().documents.filter(doc => !doc.knowledgeBaseId)
        if (orphaned.length === 0) return 0

        set(state => ({
          documents: state.documents.map(doc =>
            !doc.knowledgeBaseId ? { ...doc, knowledgeBaseId: targetKbId } : doc
          )
        }))
        return orphaned.length
      },

      deleteOrphanedDocuments: () => {
        const orphaned = get().documents.filter(doc => !doc.knowledgeBaseId)
        if (orphaned.length === 0) return 0

        set(state => ({
          documents: state.documents.filter(doc => doc.knowledgeBaseId)
        }))
        return orphaned.length
      },

      // ========== 文档操作 ==========
      addDocument: (docUrl, docId, knowledgeBaseId) => {
        const id = uuidv4()
        const now = Date.now()
        const state = get()
        // 如果没有指定知识库，使用当前活跃的知识库
        const kbId = knowledgeBaseId || state.activeKnowledgeBaseId || undefined
        const document: PrdDocument = {
          id,
          docId,
          docUrl,
          title: '加载中...',
          status: 'pending',
          entities: [],
          knowledgeBaseId: kbId,
          createdAt: now,
          updatedAt: now,
        }
        set(state => ({
          documents: [document, ...state.documents],
        }))
        return id
      },

      updateDocument: (id, updates) => {
        set(state => ({
          documents: state.documents.map(doc =>
            doc.id === id
              ? { ...doc, ...updates, updatedAt: Date.now() }
              : doc
          ),
        }))
      },

      removeDocument: (id) => {
        set(state => {
          const docToRemove = state.documents.find(doc => doc.id === id)

          // 清理归一化实体中关联的版本
          let updatedNormalizedEntities = state.normalizedEntities
          if (docToRemove) {
            updatedNormalizedEntities = state.normalizedEntities
              .map(entity => {
                // 过滤掉来自该文档的版本
                const remainingVersions = entity.versions.filter(v => v.docId !== id)
                if (remainingVersions.length === entity.versions.length) {
                  return entity  // 无变化
                }
                if (remainingVersions.length === 0) {
                  return null  // 标记为待删除（所有版本都来自该文档）
                }
                // 更新 current 信息为最新版本
                const latest = remainingVersions[0]
                return {
                  ...entity,
                  versions: remainingVersions,
                  currentDescription: latest.description,
                  currentDocId: latest.docId,
                  currentDocTitle: latest.docTitle,
                  currentDocUpdateTime: latest.docUpdateTime,
                  updatedAt: Date.now(),
                }
              })
              .filter((e): e is NormalizedEntity => e !== null)
          }

          return {
            documents: state.documents.filter(doc => doc.id !== id),
            normalizedEntities: updatedNormalizedEntities,
            selectedDocId: state.selectedDocId === id ? null : state.selectedDocId,
          }
        })
      },

      setDocumentStatus: (id, status, errorMessage) => {
        set(state => ({
          documents: state.documents.map(doc =>
            doc.id === id
              ? { ...doc, status, errorMessage, updatedAt: Date.now() }
              : doc
          ),
        }))
      },

      setDocumentEntities: (id, entities) => {
        set(state => ({
          documents: state.documents.map(doc => {
            if (doc.id !== id) return doc
            const manualEntities = doc.entities.filter(e => e.source?.method === 'manual')
            const manualNames = new Set(manualEntities.map(e => e.name))
            const newLlmEntities = entities.filter(e => !manualNames.has(e.name))
            return {
              ...doc,
              entities: [...manualEntities, ...newLlmEntities],
              status: 'done' as PrdDocStatus,
              updatedAt: Date.now(),
            }
          }),
        }))
      },

      updateDocumentTitle: (id, title) => {
        set(state => ({
          documents: state.documents.map(doc =>
            doc.id === id
              ? { ...doc, title, updatedAt: Date.now() }
              : doc
          ),
        }))
      },

      getDocumentById: (id) => {
        return get().documents.find(doc => doc.id === id)
      },

      // ========== 原始实体操作 ==========
      addEntity: (docId, entity) => {
        const doc = get().documents.find(d => d.id === docId)
        if (!doc) return null
        const entityId = uuidv4()
        const now = Date.now()
        const newEntity: PrdEntity = {
          ...entity,
          id: entityId,
          createdAt: now,
          updatedAt: now,
        }
        set(state => ({
          documents: state.documents.map(d =>
            d.id === docId
              ? { ...d, entities: [...d.entities, newEntity], updatedAt: Date.now() }
              : d
          ),
        }))
        return entityId
      },

      updateEntity: (docId, entityId, updates) => {
        const now = Date.now()
        set(state => ({
          documents: state.documents.map(d =>
            d.id === docId
              ? {
                  ...d,
                  entities: d.entities.map(e =>
                    e.id === entityId ? { ...e, ...updates, updatedAt: now } : e
                  ),
                  updatedAt: now,
                }
              : d
          ),
        }))
      },

      removeEntity: (docId, entityId) => {
        set(state => ({
          documents: state.documents.map(d =>
            d.id === docId
              ? {
                  ...d,
                  entities: d.entities.filter(e => e.id !== entityId),
                  updatedAt: Date.now(),
                }
              : d
          ),
          selectedEntityId: state.selectedEntityId === entityId ? null : state.selectedEntityId,
        }))
      },

      getEntityById: (docId, entityId) => {
        const doc = get().documents.find(d => d.id === docId)
        return doc?.entities.find(e => e.id === entityId)
      },

      // ========== 归一化实体操作 ==========
      setNormalizedEntities: (entities) => {
        // 按知识库合并：只替换同一知识库的实体，保留其他知识库的实体不变
        if (entities.length === 0) {
          set({ normalizedEntities: entities })
          return
        }
        const kbId = entities[0].knowledgeBaseId
        set(state => ({
          normalizedEntities: [
            // 保留其他知识库的实体
            ...state.normalizedEntities.filter(e => e.knowledgeBaseId !== kbId),
            // 替换当前知识库的实体
            ...entities,
          ]
        }))
      },

      updateNormalizedEntity: (id, updates) => {
        set(state => ({
          normalizedEntities: state.normalizedEntities.map(e =>
            e.id === id ? { ...e, ...updates, updatedAt: Date.now() } : e
          ),
        }))
      },

      mergeNormalizedEntities: (sourceId, targetId) => {
        set(state => {
          const source = state.normalizedEntities.find(e => e.id === sourceId)
          const target = state.normalizedEntities.find(e => e.id === targetId)
          if (!source || !target) return state

          const mergedAliases = [...new Set([...target.aliases, ...source.aliases])]
          const mergedVersions = [...target.versions, ...source.versions]
            .sort((a, b) => b.docUpdateTime - a.docUpdateTime)
          const latest = mergedVersions[0]

          // 合并关系：
          // 1. source 的关系中指向 target 的关系丢弃（自引用无意义）
          // 2. source 的其余关系迁移到 target
          // 3. target 的关系中指向 source 的关系丢弃
          const sourceRelationsMigrated = source.relations
            .filter(r => r.targetEntityId !== targetId)
            .map(r => ({ ...r, updatedAt: Date.now() }))
          const targetRelationsFiltered = target.relations
            .filter(r => r.targetEntityId !== sourceId)
          const mergedRelations = [...targetRelationsFiltered, ...sourceRelationsMigrated]

          // 其他实体中指向 source 的关系，改为指向 target
          const updatedEntities = state.normalizedEntities
            .filter(e => e.id !== sourceId)
            .map(e => {
              if (e.id === targetId) {
                // target 实体使用合并后的数据
                const merged: NormalizedEntity = {
                  ...target,
                  aliases: mergedAliases,
                  versions: mergedVersions,
                  relations: mergedRelations,
                  currentDescription: latest?.description || target.currentDescription,
                  currentDocId: latest?.docId || target.currentDocId,
                  currentDocTitle: latest?.docTitle || target.currentDocTitle,
                  currentDocUpdateTime: latest?.docUpdateTime || target.currentDocUpdateTime,
                  normalizationMethod: 'manual',
                  updatedAt: Date.now(),
                }
                return merged
              }
              // 其他实体：将指向 source 的关系重定向到 target
              const hasSourceRef = e.relations.some(r => r.targetEntityId === sourceId)
              if (!hasSourceRef) return e
              return {
                ...e,
                relations: e.relations.map(r =>
                  r.targetEntityId === sourceId
                    ? { ...r, targetEntityId: targetId, targetEntityName: target.canonicalName, updatedAt: Date.now() }
                    : r
                ),
                updatedAt: Date.now(),
              }
            })

          return { normalizedEntities: updatedEntities }
        })
      },

      splitNormalizedEntity: (id, aliasToSplit) => {
        set(state => {
          const entity = state.normalizedEntities.find(e => e.id === id)
          if (!entity || entity.aliases.length <= 1) return state

          const versionsToSplit = entity.versions.filter(v => {
            const doc = state.documents.find(d => d.id === v.docId)
            return doc?.entities.some(e => e.name === aliasToSplit)
          })

          if (versionsToSplit.length === 0) return state

          const now = Date.now()
          const newEntityId = uuidv4()
          const latestSplit = versionsToSplit[0]

          // 拆分后新实体继承原实体的所有关系（复制一份）
          const newEntityRelations = entity.relations.map(r => ({
            ...r,
            id: uuidv4(),
            createdAt: now,
            updatedAt: now,
          }))

          const newEntity: NormalizedEntity = {
            id: newEntityId,
            canonicalName: aliasToSplit,
            aliases: [aliasToSplit],
            versions: versionsToSplit,
            relations: newEntityRelations,
            knowledgeBaseId: entity.knowledgeBaseId,
            currentDescription: latestSplit.description,
            currentDocId: latestSplit.docId,
            currentDocTitle: latestSplit.docTitle,
            currentDocUpdateTime: latestSplit.docUpdateTime,
            hasConflict: false,
            normalizationMethod: 'manual',
            createdAt: now,
            updatedAt: now,
          }

          const remainingVersions = entity.versions.filter(v => !versionsToSplit.includes(v))
          const latestRemaining = remainingVersions[0]

          // 拆分后清除冲突状态，等待下次重新分析时重新检测
          // 不再基于 aspect 判断（已移除面向分类功能）
          const updatedEntity: NormalizedEntity = {
            ...entity,
            aliases: entity.aliases.filter(a => a !== aliasToSplit),
            versions: remainingVersions,
            currentDescription: latestRemaining?.description || entity.currentDescription,
            currentDocId: latestRemaining?.docId || entity.currentDocId,
            currentDocTitle: latestRemaining?.docTitle || entity.currentDocTitle,
            currentDocUpdateTime: latestRemaining?.docUpdateTime || entity.currentDocUpdateTime,
            // 拆分后清除冲突状态
            hasConflict: false,
            conflictResolution: undefined,
            conflictSummary: undefined,
            conflictType: undefined,
            aspectConflicts: undefined,
            normalizationMethod: 'manual',
            updatedAt: now,
          }

          return {
            normalizedEntities: [
              ...state.normalizedEntities.map(e => e.id === id ? updatedEntity : e),
              newEntity,
            ],
          }
        })
      },

      getNormalizedEntityByAlias: (alias) => {
        const lowerAlias = alias.toLowerCase()
        return get().normalizedEntities.find(e =>
          e.aliases.some(a => a.toLowerCase() === lowerAlias)
        )
      },

      // ========== 别名管理 ==========
      addManualAlias: (entityId, alias) => {
        set(state => ({
          normalizedEntities: state.normalizedEntities.map(e => {
            if (e.id !== entityId) return e
            const trimmedAlias = alias.trim()
            if (!trimmedAlias || e.aliases.includes(trimmedAlias)) return e
            return {
              ...e,
              aliases: [...e.aliases, trimmedAlias],
              manualAliases: [...(e.manualAliases || []), trimmedAlias],
              normalizationMethod: 'manual' as const,
              updatedAt: Date.now(),
            }
          }),
        }))
      },

      removeAlias: (entityId, alias) => {
        set(state => ({
          normalizedEntities: state.normalizedEntities.map(e => {
            if (e.id !== entityId) return e
            // 不能删除唯一的别名（即 canonicalName）
            if (e.aliases.length <= 1) return e
            // 如果删除的是 canonicalName，需要更新 canonicalName
            const newAliases = e.aliases.filter(a => a !== alias)
            const newCanonicalName = alias === e.canonicalName ? newAliases[0] : e.canonicalName
            return {
              ...e,
              canonicalName: newCanonicalName,
              aliases: newAliases,
              manualAliases: (e.manualAliases || []).filter(a => a !== alias),
              normalizationMethod: 'manual' as const,
              updatedAt: Date.now(),
            }
          }),
        }))
      },

      updateCanonicalName: (entityId, newName) => {
        set(state => ({
          normalizedEntities: state.normalizedEntities.map(e => {
            if (e.id !== entityId) return e
            // 新名称必须在别名列表中
            if (!e.aliases.includes(newName)) return e
            return {
              ...e,
              canonicalName: newName,
              normalizationMethod: 'manual' as const,
              updatedAt: Date.now(),
            }
          }),
        }))
      },

      // ========== 冲突解决操作 ==========
      resolveConflict: (entityId, resolution, options) => {
        // split 解决方式需要特殊处理：拆分实体
        if (resolution === 'split' && options?.splitAlias) {
          // 调用 splitNormalizedEntity 来拆分
          get().splitNormalizedEntity(entityId, options.splitAlias)
          return
        }

        set(state => ({
          normalizedEntities: state.normalizedEntities.map(e => {
            if (e.id !== entityId) return e

            const resolutionRecord: ConflictResolutionRecord = {
              resolvedAt: Date.now(),
              resolution,
              authoritativeVersionId: options?.authoritativeVersionId,
              mergedDescription: options?.mergedDescription,
              splitAlias: options?.splitAlias,
              note: options?.note,
            }

            // 如果选择了权威版本，更新 currentDescription
            let currentDescription = e.currentDescription
            let currentDocId = e.currentDocId
            let currentDocTitle = e.currentDocTitle
            let currentDocUpdateTime = e.currentDocUpdateTime

            if (resolution === 'authoritative' && options?.authoritativeVersionId) {
              const authVersion = e.versions.find(v => v.rawEntityId === options.authoritativeVersionId)
              if (authVersion) {
                currentDescription = authVersion.description
                currentDocId = authVersion.docId
                currentDocTitle = authVersion.docTitle
                currentDocUpdateTime = authVersion.docUpdateTime
              }
            } else if (resolution === 'merged' && options?.mergedDescription) {
              currentDescription = options.mergedDescription
            }

            return {
              ...e,
              conflictResolution: resolutionRecord,
              currentDescription,
              currentDocId,
              currentDocTitle,
              currentDocUpdateTime,
              normalizationMethod: 'manual' as const,
              updatedAt: Date.now(),
            }
          })
        }))
      },

      resetConflictResolution: (entityId) => {
        set(state => ({
          normalizedEntities: state.normalizedEntities.map(e => {
            if (e.id !== entityId) return e

            // 重置为最新版本
            const latest = e.versions[0]
            return {
              ...e,
              conflictResolution: undefined,
              currentDescription: latest?.description || e.currentDescription,
              currentDocId: latest?.docId || e.currentDocId,
              currentDocTitle: latest?.docTitle || e.currentDocTitle,
              currentDocUpdateTime: latest?.docUpdateTime || e.currentDocUpdateTime,
              updatedAt: Date.now(),
            }
          })
        }))
      },

      // ========== 版本面向调整 ==========
      updateVersionAspect: (entityId, versionId, newAspect) => {
        set(state => ({
          normalizedEntities: state.normalizedEntities.map(e => {
            if (e.id !== entityId) return e

            // 更新指定版本的面向
            const updatedVersions = e.versions.map(v =>
              v.rawEntityId === versionId
                ? { ...v, aspect: newAspect, aspectConfidence: 1.0 }  // 手动调整置信度为 1.0
                : v
            )

            // 重新检测该实体的冲突状态（基于新的面向分类）
            // 简单的冲突检测逻辑：如果 definition 面向有多个版本，可能存在冲突
            const definitionVersions = updatedVersions.filter(v => v.aspect === 'definition')
            const configVersions = updatedVersions.filter(v => v.aspect === 'config')

            // 更新冲突状态
            let hasConflict = e.hasConflict
            let conflictType = e.conflictType
            let aspectConflicts = e.aspectConflicts || []

            // 重新计算面向冲突
            const newAspectConflicts: AspectConflict[] = []

            // 检查定义冲突（保留原有判断，但需要有>=2个定义版本）
            if (definitionVersions.length >= 2) {
              const existingDefConflict = aspectConflicts.find(c => c.aspect === 'definition')
              if (existingDefConflict) {
                newAspectConflicts.push({
                  ...existingDefConflict,
                  versions: definitionVersions.map(v => v.rawEntityId),
                })
              }
            }

            // 配置面向：标记为上下文相关
            if (configVersions.length >= 2) {
              newAspectConflicts.push({
                aspect: 'config',
                versions: configVersions.map(v => v.rawEntityId),
                summary: '不同场景下的配置可能不同，需注意区分适用场景',
              })
            }

            // 更新冲突类型
            const hasDefinitionConflict = newAspectConflicts.some(c => c.aspect === 'definition')
            const hasConfigVariation = newAspectConflicts.some(c => c.aspect === 'config')

            if (hasDefinitionConflict) {
              conflictType = 'definition_conflict'
              hasConflict = true
            } else if (hasConfigVariation) {
              conflictType = 'context_dependent'
              hasConflict = false
            } else if (newAspectConflicts.length === 0) {
              conflictType = 'none'
              hasConflict = false
            }

            return {
              ...e,
              versions: updatedVersions,
              aspectConflicts: newAspectConflicts.length > 0 ? newAspectConflicts : undefined,
              conflictType,
              hasConflict,
              normalizationMethod: 'manual' as const,  // 标记为手动调整
              updatedAt: Date.now(),
            }
          })
        }))
      },

      // ========== 实体关系操作 ==========
      addEntityRelation: (entityId, relation) => {
        const entity = get().normalizedEntities.find(e => e.id === entityId)
        if (!entity) return null
        const id = uuidv4()
        const now = Date.now()
        const newRelation: EntityRelation = {
          ...relation,
          id,
          createdAt: now,
          updatedAt: now,
        }
        set(state => ({
          normalizedEntities: state.normalizedEntities.map(e =>
            e.id === entityId
              ? { ...e, relations: [...e.relations, newRelation], updatedAt: now }
              : e
          ),
        }))
        return id
      },

      removeEntityRelation: (entityId, relationId) => {
        const now = Date.now()
        set(state => ({
          normalizedEntities: state.normalizedEntities.map(e =>
            e.id === entityId
              ? { ...e, relations: e.relations.filter(r => r.id !== relationId), updatedAt: now }
              : e
          ),
        }))
      },

      updateEntityRelation: (entityId, relationId, updates) => {
        const now = Date.now()
        set(state => ({
          normalizedEntities: state.normalizedEntities.map(e =>
            e.id === entityId
              ? {
                  ...e,
                  relations: e.relations.map(r =>
                    r.id === relationId ? { ...r, ...updates, updatedAt: now } : r
                  ),
                  updatedAt: now,
                }
              : e
          ),
        }))
      },

      getEntityRelations: (entityId) => {
        const entity = get().normalizedEntities.find(e => e.id === entityId)
        return entity?.relations || []
      },

      setEntityRelations: (entityId, relations) => {
        const now = Date.now()
        set(state => ({
          normalizedEntities: state.normalizedEntities.map(e =>
            e.id === entityId
              ? { ...e, relations, updatedAt: now }
              : e
          ),
        }))
      },

      getRelationsBetween: (entityId1, entityId2) => {
        const entity1 = get().normalizedEntities.find(e => e.id === entityId1)
        const entity2 = get().normalizedEntities.find(e => e.id === entityId2)
        const relations: EntityRelation[] = []
        // 从 entity1 找指向 entity2 的关系
        if (entity1) {
          relations.push(...entity1.relations.filter(r => r.targetEntityId === entityId2))
        }
        // 从 entity2 找指向 entity1 的关系
        if (entity2) {
          relations.push(...entity2.relations.filter(r => r.targetEntityId === entityId1))
        }
        return relations
      },

      // ========== 关系冲突操作 ==========
      setRelationConflicts: (entityId, conflicts) => {
        const now = Date.now()
        set(state => ({
          normalizedEntities: state.normalizedEntities.map(e =>
            e.id === entityId
              ? { ...e, relationConflicts: conflicts, updatedAt: now }
              : e
          ),
        }))
      },

      resolveRelationConflict: (entityId, relationId, resolution, options) => {
        const now = Date.now()
        set(state => ({
          normalizedEntities: state.normalizedEntities.map(e => {
            if (e.id !== entityId) return e

            const updatedRelations = e.relations.map(r => {
              if (r.id !== relationId) return r

              const resolutionRecord: ConflictResolutionRecord = {
                resolvedAt: now,
                resolution,
                note: options?.note,
              }

              return {
                ...r,
                conflictResolution: resolutionRecord,
                updatedAt: now,
              }
            })

            return { ...e, relations: updatedRelations, updatedAt: now }
          }),
        }))
      },

      resetRelationConflictResolution: (entityId, relationId) => {
        const now = Date.now()
        set(state => ({
          normalizedEntities: state.normalizedEntities.map(e => {
            if (e.id !== entityId) return e

            const updatedRelations = e.relations.map(r => {
              if (r.id !== relationId) return r
              const { conflictResolution, ...rest } = r
              return { ...rest, updatedAt: now }
            })

            return { ...e, relations: updatedRelations, updatedAt: now }
          }),
        }))
      },

      // ========== 选择状态 ==========
      setSelectedDoc: (id) => {
        set({ selectedDocId: id, selectedEntityId: null })
      },

      setSelectedEntity: (entityId) => {
        set({ selectedEntityId: entityId })
      },
    }),
    {
      name: 'prd-cognition-storage',
      version: 3,
      migrate: (persistedState: unknown, version: number) => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const state = persistedState as any
        if (version < 2) {
          // v1 → v2 迁移：
          // 1. 删除 knowledgeTriples 相关字段
          // 2. 为 normalizedEntities 添加 relations 字段
          delete state.knowledgeTriples
          delete state.isExtractingKnowledge
          delete state.knowledgeExtractionProgress
          if (Array.isArray(state.normalizedEntities)) {
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            state.normalizedEntities = state.normalizedEntities.map((e: any) => ({
              ...e,
              relations: e.relations || [],
            }))
          }
        }
        if (version < 3) {
          // v2 → v3 迁移：
          // 为 normalizedEntities 添加 knowledgeBaseId 字段
          // 旧数据没有知识库隔离，将其归属到当前激活的知识库（如果有）
          if (Array.isArray(state.normalizedEntities) && state.normalizedEntities.length > 0) {
            const fallbackKbId = state.activeKnowledgeBaseId || ''
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            state.normalizedEntities = state.normalizedEntities.map((e: any) => ({
              ...e,
              knowledgeBaseId: e.knowledgeBaseId || fallbackKbId,
            }))
          }
        }
        return state
      },
      partialize: (state) => ({
        knowledgeBases: state.knowledgeBases,
        activeKnowledgeBaseId: state.activeKnowledgeBaseId,
        chatKnowledgeBaseIds: state.chatKnowledgeBaseIds,
        documents: state.documents.map(doc => ({
          ...doc,
          rawContent: undefined,
        })),
        normalizedEntities: state.normalizedEntities,
      }),
    }
  )
)

import { useState, useCallback, useMemo } from 'react'
import { Network, RefreshCw, FileText, ExternalLink, Trash2, Plus, Search, Unlink, ChevronDown, ChevronUp, Pencil, Check, X, AlertTriangle, Sparkles, ChevronRight } from 'lucide-react'
import { usePrdStore, type NormalizedEntity, type PrdEntity, type RelationConflictInfo } from '../../stores/prdStore'
import { useChatStore } from '../../store'
import { processDocument, crossDocumentAnalysis } from '../../services/prdService'
import { runNormalization } from '../../services/normalizationService'
import { extractRelationsWithConflictDetection, mergeRelations } from '../../services/relationExtractionService'
import { PrdAddInput } from './PrdAddInput'
import { EntityGraph } from './EntityGraph'
import { EntityFormModal } from './EntityFormModal'
import { NormalizedEntityDetail } from './NormalizedEntityDetail'
import { KnowledgeBaseSelector } from './KnowledgeBaseSelector'
import { ConflictCenter } from './ConflictCenter'
import { showToast } from '../Toast'

type TabType = 'graph' | 'docs'

export function PrdManager() {
  const settings = useChatStore(s => s.settings)
  const {
    documents,
    normalizedEntities,
    knowledgeBases,
    activeKnowledgeBaseId,
    addDocument,
    updateDocument,
    removeDocument,
    setDocumentStatus,
    setDocumentEntities,
    setNormalizedEntities,
    getDocumentById,
    getEntityById,
    addEntity,
    updateEntity,
    removeEntity,
    updateDocumentTitle,
    createKnowledgeBase,
    setEntityRelations,
    setRelationConflicts,
    updateNormalizedEntity,
  } = usePrdStore()

  const [activeTab, setActiveTab] = useState<TabType>('graph')
  const [selectedEntityName, setSelectedEntityName] = useState<string | null>(null)
  const [selectedNormalizedId, setSelectedNormalizedId] = useState<string | null>(null)
  const [processingIds, setProcessingIds] = useState<Set<string>>(new Set())
  const [isNormalizing, setIsNormalizing] = useState(false)  // 归一化中
  const [isExtractingRelations, setIsExtractingRelations] = useState(false)  // 关系提取中
  const [docSearchQuery, setDocSearchQuery] = useState('')
  const [showConflictCenter, setShowConflictCenter] = useState(false)

  // 实体编辑/添加弹窗状态
  const [entityFormState, setEntityFormState] = useState<{
    docId: string
    entityId?: string
  } | null>(null)

  // 根据活跃知识库过滤文档
  const filteredDocuments = useMemo(() => {
    // 必须有活跃知识库才显示文档
    if (!activeKnowledgeBaseId) {
      return []
    }
    return documents.filter(doc => doc.knowledgeBaseId === activeKnowledgeBaseId)
  }, [documents, activeKnowledgeBaseId])

  // 根据活跃知识库过滤归一化实体
  const filteredNormalizedEntities = useMemo(() => {
    if (!activeKnowledgeBaseId) return []
    return normalizedEntities.filter(e => e.knowledgeBaseId === activeKnowledgeBaseId)
  }, [normalizedEntities, activeKnowledgeBaseId])

  // 根据选中的实体名称筛选相关文档（图谱视图右侧使用）
  const relatedDocs = useMemo(() => {
    if (!selectedEntityName) return filteredDocuments
    return filteredDocuments.filter(doc =>
      doc.entities.some(e => e.name === selectedEntityName)
    )
  }, [filteredDocuments, selectedEntityName])

  // 文档管理页的搜索过滤
  const filteredDocsForManagement = useMemo(() => {
    let docs = filteredDocuments
    if (docSearchQuery.trim()) {
      const q = docSearchQuery.toLowerCase()
      docs = docs.filter(doc =>
        doc.title.toLowerCase().includes(q) ||
        doc.docId.toLowerCase().includes(q) ||
        doc.docUrl.toLowerCase().includes(q) ||
        doc.entities.some(e => e.name.toLowerCase().includes(q))
      )
    }
    return docs
  }, [filteredDocuments, docSearchQuery])

  // 处理文档
  const processDoc = useCallback(async (id: string, docId: string, isReanalyze = false, docUrl?: string) => {
    if (processingIds.has(id)) return

    setProcessingIds(prev => new Set(prev).add(id))
    setDocumentStatus(id, 'parsing')

    try {
      const { title, entities, content } = await processDocument(settings, docId, undefined, docUrl)
      updateDocument(id, { title, rawContent: content })
      setDocumentEntities(id, entities)
      if (isReanalyze) {
        if (entities.length === 0) {
          showToast(`重新分析完成，未提取到实体`, 'warning')
        } else {
          showToast(`重新分析完成，发现 ${entities.length} 个实体`, 'success')
        }
      } else {
        if (entities.length === 0) {
          showToast(`已解析：${title}（未提取到实体）`, 'warning')
        } else {
          showToast(`已解析：${title}，发现 ${entities.length} 个实体`, 'success')
        }
      }
    } catch (error) {
      const errMsg = error instanceof Error ? error.message : '解析失败'
      // 同时更新标题为错误信息，避免文档卡片一直显示"加载中..."
      updateDocument(id, { title: errMsg })
      setDocumentStatus(id, 'error', errMsg)
      showToast(errMsg, 'error')
    } finally {
      setProcessingIds(prev => {
        const next = new Set(prev)
        next.delete(id)
        return next
      })
    }
  }, [settings, processingIds, setDocumentStatus, setDocumentEntities, updateDocument])

  // 添加单个文档
  const handleAdd = useCallback((docUrl: string, docId: string) => {
    // 只检查当前知识库内是否已存在，允许同一文档加入不同知识库
    const existing = documents.find(d => d.docId === docId && d.knowledgeBaseId === activeKnowledgeBaseId)
    if (existing) {
      showToast('该文档已添加到当前知识库', 'info')
      return
    }

    const id = addDocument(docUrl, docId, activeKnowledgeBaseId || undefined)
    processDoc(id, docId, false, docUrl)
  }, [documents, addDocument, processDoc, activeKnowledgeBaseId])

  // 批量添加文档（串行处理，避免并发导致的限流问题）
  const handleBatchAdd = useCallback(async (items: Array<{ docUrl: string; docId: string }>) => {
    // 只过滤当前知识库内已存在的文档
    const newItems = items.filter(item => !documents.find(d => d.docId === item.docId && d.knowledgeBaseId === activeKnowledgeBaseId))

    if (newItems.length === 0) {
      showToast('所有文档都已添加到当前知识库', 'info')
      return
    }

    if (newItems.length < items.length) {
      showToast(`${items.length - newItems.length} 个文档在当前知识库中已存在，将添加 ${newItems.length} 个新文档`, 'info')
    }

    // 先添加所有文档到列表（显示 pending 状态）
    const docIds: Array<{ id: string; docId: string; docUrl: string }> = []
    for (const item of newItems) {
      const id = addDocument(item.docUrl, item.docId, activeKnowledgeBaseId || undefined)
      docIds.push({ id, docId: item.docId, docUrl: item.docUrl })
    }

    // 串行处理每个文档
    let successCount = 0
    let failCount = 0

    for (const { id, docId, docUrl } of docIds) {
      try {
        setProcessingIds(prev => new Set(prev).add(id))
        setDocumentStatus(id, 'parsing')

        const { title, entities, content } = await processDocument(settings, docId, undefined, docUrl)
        updateDocument(id, { title, rawContent: content })
        setDocumentEntities(id, entities)
        setDocumentStatus(id, 'done')
        successCount++
      } catch (error) {
        const errMsg = error instanceof Error ? error.message : '解析失败'
        setDocumentStatus(id, 'error', errMsg)
        failCount++
        console.error(`[PRD] 文档 ${docId} 处理失败:`, error)
      } finally {
        setProcessingIds(prev => {
          const next = new Set(prev)
          next.delete(id)
          return next
        })
      }
    }

    if (failCount > 0) {
      showToast(`添加完成：${successCount} 成功，${failCount} 失败`, 'warning')
    } else {
      showToast(`已添加 ${successCount} 个文档`, 'success')
    }
  }, [documents, addDocument, processDoc, settings, setDocumentStatus, setDocumentEntities, updateDocument])

  // 重新分析文档
  const handleRetry = useCallback((id: string) => {
    const doc = getDocumentById(id)
    if (doc) {
      processDoc(doc.id, doc.docId, true, doc.docUrl)
    }
  }, [getDocumentById, processDoc])

  // 删除文档（真正删除）
  const handleDeleteDoc = useCallback((id: string) => {
    if (window.confirm('确定要删除这个文档吗？这将同时删除该文档的所有实体关联。')) {
      removeDocument(id)
      showToast('已删除文档', 'success')
    }
  }, [removeDocument])

  // 移除文档与实体的关联（只在图谱视图中使用）
  const handleUnlinkDoc = useCallback((docId: string, entityName: string) => {
    const doc = getDocumentById(docId)
    if (!doc) return

    const entity = doc.entities.find(e => e.name === entityName)
    if (entity) {
      removeEntity(docId, entity.id)
      showToast(`已移除「${doc.title}」与「${entityName}」的关联`, 'success')
    }
  }, [getDocumentById, removeEntity])

  // 触发实体归一化（需要在 handleReanalyzeAll 之前定义）
  const triggerNormalization = useCallback(async () => {
    const doneDocs = filteredDocuments.filter(d => d.status === 'done')
    if (doneDocs.length === 0) return

    setIsNormalizing(true)
    try {
      showToast('正在进行实体归一化...', 'info')
      // 只传入当前知识库的已有归一化实体（避免跨知识库干扰）
      const currentKbEntities = normalizedEntities.filter(e => e.knowledgeBaseId === activeKnowledgeBaseId)
      const normalized = await runNormalization(
        settings,
        doneDocs,
        currentKbEntities,
        (status) => console.log('[Normalize]', status),
        activeKnowledgeBaseId || undefined
      )
      setNormalizedEntities(normalized)

      const conflictCount = normalized.filter(e => e.hasConflict).length
      if (conflictCount > 0) {
        showToast(`归一化完成，发现 ${conflictCount} 个实体存在冲突`, 'warning')
      } else {
        showToast(`归一化完成，共 ${normalized.length} 个实体`, 'success')
      }
    } catch (error) {
      console.error('[Normalize] 归一化失败:', error)
      showToast('实体归一化失败', 'error')
    } finally {
      setIsNormalizing(false)
    }
  }, [filteredDocuments, normalizedEntities, settings, setNormalizedEntities, activeKnowledgeBaseId])

  // 触发关系提取（需要在 handleReanalyzeAll 之前定义）
  const triggerRelationExtraction = useCallback(async () => {
    // 获取最新的 normalizedEntities 和 documents（通过 store，避免闭包问题）
    const allNormalizedEntities = usePrdStore.getState().normalizedEntities
    const currentDocuments = usePrdStore.getState().documents
    // 只取当前知识库的归一化实体
    const currentNormalizedEntities = allNormalizedEntities.filter(e => e.knowledgeBaseId === activeKnowledgeBaseId)

    if (currentNormalizedEntities.length < 2) {
      console.log('[RelationExtraction] 归一化实体不足 2 个，跳过关系提取')
      return
    }

    // 过滤当前知识库的已完成文档（rawContent 在重新分析流程中已被设置）
    const doneDocs = currentDocuments.filter(d =>
      d.knowledgeBaseId === activeKnowledgeBaseId &&
      d.status === 'done' &&
      d.rawContent
    )
    if (doneDocs.length === 0) {
      console.log('[RelationExtraction] 没有可用的文档，跳过关系提取')
      return
    }

    setIsExtractingRelations(true)
    try {
      showToast('正在提取实体关系...', 'info')

      const { relationsByEntity, conflictsByEntity } = await extractRelationsWithConflictDetection(
        settings,
        doneDocs,
        currentNormalizedEntities,
        (status) => console.log('[RelationExtraction]', status)
      )

      // 将提取的关系合并到各实体
      let totalNewRelations = 0
      for (const [entityId, newRelations] of relationsByEntity) {
        const entity = currentNormalizedEntities.find(e => e.id === entityId)
        if (entity) {
          const mergedRelations = mergeRelations(entity.relations || [], newRelations)
          const addedCount = mergedRelations.length - (entity.relations?.length || 0)
          if (addedCount > 0) {
            totalNewRelations += addedCount
          }
          setEntityRelations(entityId, mergedRelations)
        }
      }

      // 存储关系冲突
      let totalConflicts = 0
      for (const [entityId, conflicts] of conflictsByEntity) {
        if (conflicts.length > 0) {
          setRelationConflicts(entityId, conflicts)
          totalConflicts += conflicts.length
        }
      }

      if (totalConflicts > 0) {
        showToast(`关系提取完成，新增 ${totalNewRelations} 条关系，发现 ${totalConflicts} 处冲突`, 'warning')
      } else if (totalNewRelations > 0) {
        showToast(`关系提取完成，新增 ${totalNewRelations} 条关系`, 'success')
      } else {
        showToast('关系提取完成，未发现新关系', 'info')
      }
    } catch (error) {
      console.error('[RelationExtraction] 提取失败:', error)
      showToast('关系提取失败', 'error')
    } finally {
      setIsExtractingRelations(false)
    }
  }, [activeKnowledgeBaseId, settings, setEntityRelations, setRelationConflicts])

  // 批量重新分析（串行处理，避免并发导致的限流问题）
  const handleReanalyzeAll = useCallback(async () => {
    const doneDocs = filteredDocuments.filter(d => d.status === 'done')
    if (doneDocs.length === 0) {
      showToast('没有可重新分析的文档', 'info')
      return
    }

    for (const doc of doneDocs) {
      setProcessingIds(prev => new Set(prev).add(doc.id))
      setDocumentStatus(doc.id, 'parsing')
    }

    try {
      showToast(`开始重新分析 ${doneDocs.length} 个文档...`, 'info')

      // 串行处理每个文档，避免并发导致的 API 限流
      const results: Array<{ docId: string; content: string; entities: PrdEntity[] }> = []
      let successCount = 0
      let failCount = 0

      for (const doc of doneDocs) {
        try {
          const { title, entities, content } = await processDocument(settings, doc.docId, undefined, doc.docUrl)
          updateDocument(doc.id, { title, rawContent: content })
          setDocumentEntities(doc.id, entities)
          setDocumentStatus(doc.id, 'done')
          results.push({ docId: doc.docId, content, entities })
          successCount++
        } catch (error) {
          const errMsg = error instanceof Error ? error.message : '解析失败'
          setDocumentStatus(doc.id, 'error', errMsg)
          failCount++
          console.error(`[PRD] 文档 ${doc.docId} 处理失败:`, error)
        }

        // 从 processingIds 中移除当前文档
        setProcessingIds(prev => {
          const next = new Set(prev)
          next.delete(doc.id)
          return next
        })
      }

      if (failCount > 0) {
        showToast(`分析完成：${successCount} 成功，${failCount} 失败`, failCount === doneDocs.length ? 'error' : 'warning')
      }

      if (results.length >= 2) {
        showToast('正在进行跨文档分析...', 'info')

        const newEntitiesMap = await crossDocumentAnalysis(
          settings,
          results,
          (status) => console.log('[CrossDoc]', status)
        )

        let totalNewEntities = 0
        for (const [docId, newEntities] of newEntitiesMap) {
          const doc = documents.find(d => d.docId === docId)
          if (doc) {
            for (const entity of newEntities) {
              addEntity(doc.id, {
                name: entity.name,
                description: entity.description,
                source: entity.source,
              })
            }
            totalNewEntities += newEntities.length
          }
        }

        if (totalNewEntities > 0) {
          showToast(`跨文档分析完成，新增 ${totalNewEntities} 个实体`, 'success')
        } else if (failCount === 0) {
          showToast('全部文档重新分析完成', 'success')
        }
      } else if (failCount === 0) {
        showToast('全部文档重新分析完成', 'success')
      }

      // 自动触发实体归一化
      if (results.length > 0) {
        await triggerNormalization()

        // 自动触发关系提取（归一化完成后）
        await triggerRelationExtraction()
      }
    } catch (error) {
      const errMsg = error instanceof Error ? error.message : '分析失败'
      showToast(errMsg, 'error')
    } finally {
      setProcessingIds(new Set())
    }
  }, [filteredDocuments, documents, settings, setDocumentStatus, setDocumentEntities, updateDocument, addEntity, triggerNormalization, triggerRelationExtraction])

  // 点击实体
  const handleEntityClick = useCallback((entityName: string) => {
    setSelectedEntityName(prev => prev === entityName ? null : entityName)
  }, [])

  // 点击归一化实体（查看详情）
  const handleNormalizedEntityClick = useCallback((normalizedId: string) => {
    setSelectedNormalizedId(prev => prev === normalizedId ? null : normalizedId)
  }, [])

  // 添加实体到文档
  const handleAddEntity = useCallback((docId: string) => {
    setEntityFormState({ docId })
  }, [])

  // 保存实体
  const handleSaveEntity = useCallback((data: { name: string; description: string }) => {
    if (!entityFormState) return

    if (entityFormState.entityId) {
      updateEntity(entityFormState.docId, entityFormState.entityId, data)
      showToast('已更新实体', 'success')
    } else {
      const doc = getDocumentById(entityFormState.docId)
      addEntity(entityFormState.docId, {
        ...data,
        source: doc ? { docId: doc.docId, method: 'manual' as const } : undefined,
      })
      showToast('已添加实体', 'success')
    }

    setEntityFormState(null)
  }, [entityFormState, updateEntity, addEntity, getDocumentById])

  // 统计（基于过滤后的文档）
  const totalDocs = filteredDocuments.length
  const doneDocsCount = filteredDocuments.filter(d => d.status === 'done').length
  const uniqueEntityNames = new Set<string>()
  for (const doc of filteredDocuments) {
    for (const entity of doc.entities) {
      uniqueEntityNames.add(entity.name)
    }
  }
  const totalEntities = uniqueEntityNames.size

  // 冲突统计（包含实体冲突和关系冲突）
  const { unresolvedConflictCount, resolvedConflictCount, totalConflictCount } = useMemo(() => {
    let unresolved = 0
    let resolved = 0

    for (const entity of filteredNormalizedEntities) {
      // 实体定义冲突
      if (entity.hasConflict) {
        if (entity.conflictResolution) {
          resolved++
        } else {
          unresolved++
        }
      }
      // 关系冲突（暂无解决机制，都算未解决）
      if (entity.relationConflicts && entity.relationConflicts.length > 0) {
        unresolved += entity.relationConflicts.length
      }
    }

    return {
      unresolvedConflictCount: unresolved,
      resolvedConflictCount: resolved,
      totalConflictCount: unresolved + resolved,
    }
  }, [filteredNormalizedEntities])

  // 选中的归一化实体
  const selectedNormalizedEntity = selectedNormalizedId
    ? filteredNormalizedEntities.find(e => e.id === selectedNormalizedId)
    : null

  // 编辑中的实体
  const editingEntity = entityFormState?.entityId
    ? getEntityById(entityFormState.docId, entityFormState.entityId)
    : undefined

  // 获取当前知识库名称
  const activeKbName = knowledgeBases.find(kb => kb.id === activeKnowledgeBaseId)?.name

  // 没有知识库时的空状态
  const hasNoKnowledgeBase = knowledgeBases.length === 0

  // 从冲突中心跳转到实体详情
  const handleResolveFromConflictCenter = useCallback((entityId: string) => {
    setSelectedNormalizedId(entityId)
    setActiveTab('graph')
  }, [])

  return (
    <div className="h-full flex flex-col bg-gray-50">
      {/* Header */}
      <div className="flex-shrink-0 px-6 py-4 bg-white border-b border-gray-200">
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-3">
            <div className="p-2 bg-indigo-50 rounded-lg">
              <Network size={24} className="text-indigo-500" />
            </div>
            <div>
              <h1 className="text-lg font-semibold text-gray-900">PRD 知识库</h1>
              <p className="text-sm text-gray-500">
                添加 PRD 文档，自动提取功能模块、业务概念等实体
              </p>
            </div>
          </div>

          <div className="flex items-center gap-4">
            {/* 知识库选择器 */}
            <KnowledgeBaseSelector />

            {!hasNoKnowledgeBase && activeKnowledgeBaseId && totalDocs > 0 && (
              <div className="flex items-center gap-4 text-sm text-gray-500">
                <span>{doneDocsCount}/{totalDocs} 已解析</span>
                <span className="text-gray-300">|</span>
                <span>{filteredNormalizedEntities.length > 0 ? filteredNormalizedEntities.length : totalEntities} 个实体</span>
                {/* 冲突入口按钮 */}
                {unresolvedConflictCount > 0 ? (
                  <button
                    onClick={() => setShowConflictCenter(true)}
                    className="flex items-center gap-1.5 px-3 py-1.5 bg-amber-50 text-amber-700 hover:bg-amber-100 rounded-lg transition-colors"
                  >
                    <AlertTriangle size={14} />
                    <span>{unresolvedConflictCount} 个冲突待处理</span>
                    <ChevronRight size={14} />
                  </button>
                ) : totalConflictCount > 0 ? (
                  <button
                    onClick={() => setShowConflictCenter(true)}
                    className="flex items-center gap-1.5 px-3 py-1.5 text-green-700 hover:bg-green-50 rounded-lg transition-colors"
                  >
                    <Check size={14} />
                    <span>冲突已处理</span>
                  </button>
                ) : null}
                <div className="flex items-center gap-2">
                  {doneDocsCount > 0 && (
                    <button
                      onClick={handleReanalyzeAll}
                      disabled={processingIds.size > 0 || isNormalizing || isExtractingRelations}
                      className="flex items-center gap-1 px-3 py-1.5 text-sm font-medium text-white bg-indigo-500 hover:bg-indigo-600 disabled:bg-gray-400 rounded-lg transition-colors"
                      title="重新分析所有文档（实体抽取 → 归一化 → 关系提取）"
                    >
                      {processingIds.size > 0 || isNormalizing || isExtractingRelations ? (
                        <>
                          <RefreshCw size={14} className="animate-spin" />
                          {isExtractingRelations ? '提取关系...' : isNormalizing ? '归一化中...' : '分析中...'}
                        </>
                      ) : (
                        <>
                          <Sparkles size={14} />
                          重新分析
                        </>
                      )}
                    </button>
                  )}
                </div>
              </div>
            )}
          </div>
        </div>

        {/* 添加输入框 - 只有选中知识库时才显示 */}
        {!hasNoKnowledgeBase && activeKnowledgeBaseId && (
          <>
            <PrdAddInput onAdd={handleAdd} onBatchAdd={handleBatchAdd} disabled={processingIds.size > 0 || isNormalizing} />

            {/* Tab 切换 */}
            <div className="flex items-center gap-1 mt-4 border-b border-gray-200 -mx-6 px-6">
              <button
                onClick={() => setActiveTab('graph')}
                className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
                  activeTab === 'graph'
                    ? 'text-indigo-600 border-indigo-600'
                    : 'text-gray-500 border-transparent hover:text-gray-700'
                }`}
              >
                实体图谱
              </button>
              <button
                onClick={() => setActiveTab('docs')}
                className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
                  activeTab === 'docs'
                    ? 'text-indigo-600 border-indigo-600'
                    : 'text-gray-500 border-transparent hover:text-gray-700'
                }`}
              >
                文档管理
              </button>
            </div>
          </>
        )}
      </div>

      {/* 没有知识库时的空状态 */}
      {hasNoKnowledgeBase ? (
        <div className="flex-1 flex items-center justify-center">
          <div className="text-center">
            <div className="w-16 h-16 mx-auto mb-4 bg-gray-100 rounded-full flex items-center justify-center">
              <Network size={32} className="text-gray-400" />
            </div>
            <h3 className="text-lg font-medium text-gray-900 mb-2">开始使用 PRD 知识库</h3>
            <p className="text-sm text-gray-500 mb-4">
              请先创建一个知识库，然后添加 PRD 文档
            </p>
          </div>
        </div>
      ) : !activeKnowledgeBaseId ? (
        <div className="flex-1 flex items-center justify-center">
          <div className="text-center">
            <div className="w-16 h-16 mx-auto mb-4 bg-gray-100 rounded-full flex items-center justify-center">
              <Network size={32} className="text-gray-400" />
            </div>
            <h3 className="text-lg font-medium text-gray-900 mb-2">请选择一个知识库</h3>
            <p className="text-sm text-gray-500">
              从上方下拉框中选择要管理的知识库
            </p>
          </div>
        </div>
      ) : activeTab === 'graph' ? (
        /* 图谱 + 双栏布局 */
        <div className="flex-1 flex overflow-hidden">
          {/* 左侧：实体图谱 */}
          <div className="w-1/2 border-r border-gray-200 bg-white">
            <EntityGraph
              documents={filteredDocuments}
              normalizedEntities={filteredNormalizedEntities}
              selectedEntityName={selectedEntityName}
              selectedNormalizedId={selectedNormalizedId}
              onEntityClick={handleEntityClick}
              onNormalizedEntityClick={handleNormalizedEntityClick}
            />
          </div>

          {/* 右侧：关联文档或归一化实体详情 */}
          <div className="w-1/2 flex flex-col bg-white">
            {selectedNormalizedEntity ? (
              /* 归一化实体详情 */
              <NormalizedEntityDetail
                entity={selectedNormalizedEntity}
                documents={filteredDocuments}
                onClose={() => setSelectedNormalizedId(null)}
                onNavigateToEntity={(entityId) => setSelectedNormalizedId(entityId)}
              />
            ) : (
              /* 关联文档列表 */
              <>
                <div className="flex-shrink-0 px-4 py-3 border-b border-gray-100">
                  <div className="flex items-center justify-between">
                    <h2 className="text-sm font-medium text-gray-700">
                      {selectedEntityName ? (
                        <>
                          关联文档
                          <span className="ml-2 px-2 py-0.5 bg-indigo-50 text-indigo-600 rounded text-xs">
                            {selectedEntityName}
                          </span>
                          <span className="ml-1 text-xs text-gray-400">
                            ({relatedDocs.length})
                          </span>
                        </>
                      ) : (
                        <>全部文档 <span className="text-xs text-gray-400">({documents.length})</span></>
                      )}
                    </h2>
                    {selectedEntityName && (
                      <button
                        onClick={() => setSelectedEntityName(null)}
                        className="text-xs text-gray-400 hover:text-gray-600"
                      >
                        清除筛选
                      </button>
                    )}
                  </div>
                </div>

                <div className="flex-1 overflow-y-auto p-4 space-y-3">
                  {relatedDocs.length === 0 ? (
                    <div className="text-center text-gray-400 text-sm py-8">
                      {documents.length === 0 ? '添加文档开始使用' : '没有匹配的文档'}
                    </div>
                  ) : (
                    relatedDocs.map(doc => (
                      <DocCardInGraph
                        key={doc.id}
                        doc={doc}
                        selectedEntityName={selectedEntityName}
                        isProcessing={processingIds.has(doc.id)}
                        onEntityClick={handleEntityClick}
                        onRetry={() => handleRetry(doc.id)}
                        onUnlink={(entityName) => handleUnlinkDoc(doc.id, entityName)}
                        onAddEntity={() => handleAddEntity(doc.id)}
                        onTitleChange={(title) => updateDocumentTitle(doc.id, title)}
                      />
                    ))
                  )}
                </div>
              </>
            )}
          </div>
        </div>
      ) : (
        /* 文档管理页 */
        <div className="flex-1 flex flex-col bg-white overflow-hidden">
          {/* 搜索栏 */}
          <div className="flex-shrink-0 p-4 border-b border-gray-100">
            <div className="relative max-w-md">
              <Search size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" />
              <input
                value={docSearchQuery}
                onChange={e => setDocSearchQuery(e.target.value)}
                placeholder="搜索文档标题、ID 或实体..."
                className="w-full bg-gray-50 text-gray-700 placeholder-gray-400 text-sm rounded-lg pl-10 pr-4 py-2.5 outline-none focus:ring-2 focus:ring-indigo-100 focus:bg-white border border-gray-200 focus:border-indigo-200"
              />
            </div>
          </div>

          {/* 文档列表 */}
          <div className="flex-1 overflow-y-auto p-4">
            {filteredDocsForManagement.length === 0 ? (
              <div className="text-center text-gray-400 text-sm py-8">
                {documents.length === 0 ? '添加文档开始使用' : '没有匹配的文档'}
              </div>
            ) : (
              <div className="space-y-2">
                {filteredDocsForManagement.map(doc => (
                  <DocCardInManagement
                    key={doc.id}
                    doc={doc}
                    isProcessing={processingIds.has(doc.id)}
                    onRetry={() => handleRetry(doc.id)}
                    onDelete={() => handleDeleteDoc(doc.id)}
                    onTitleChange={(title) => updateDocumentTitle(doc.id, title)}
                  />
                ))}
              </div>
            )}
          </div>
        </div>
      )}

      {/* Entity Form Modal */}
      {entityFormState && (
        <EntityFormModal
          entity={editingEntity}
          onSave={handleSaveEntity}
          onClose={() => setEntityFormState(null)}
        />
      )}

      {/* Conflict Center Modal */}
      {showConflictCenter && (
        <ConflictCenter
          entities={filteredNormalizedEntities}
          onResolve={handleResolveFromConflictCenter}
          onClose={() => setShowConflictCenter(false)}
        />
      )}
    </div>
  )
}

// 图谱视图中的文档卡片（带实体标签，可展开）
function DocCardInGraph({
  doc,
  selectedEntityName,
  isProcessing,
  onEntityClick,
  onRetry,
  onUnlink,
  onAddEntity,
  onTitleChange,
}: {
  doc: ReturnType<typeof usePrdStore.getState>['documents'][0]
  selectedEntityName: string | null
  isProcessing: boolean
  onEntityClick: (name: string) => void
  onRetry: () => void
  onUnlink: (entityName: string) => void
  onAddEntity: () => void
  onTitleChange: (title: string) => void
}) {
  const [expanded, setExpanded] = useState(false)
  const [isEditingTitle, setIsEditingTitle] = useState(false)
  const [editTitle, setEditTitle] = useState(doc.title)

  const handleTitleSubmit = () => {
    const trimmed = editTitle.trim()
    if (trimmed && trimmed !== doc.title) {
      onTitleChange(trimmed)
    }
    setIsEditingTitle(false)
  }

  const handleTitleCancel = () => {
    setEditTitle(doc.title)
    setIsEditingTitle(false)
  }

  // 对实体排序：选中的排前面，然后按名称
  const sortedEntities = useMemo(() => {
    return [...doc.entities].sort((a, b) => {
      const aMatch = a.name === selectedEntityName
      const bMatch = b.name === selectedEntityName
      if (aMatch && !bMatch) return -1
      if (!aMatch && bMatch) return 1
      return a.name.localeCompare(b.name)
    })
  }, [doc.entities, selectedEntityName])

  const displayEntities = expanded ? sortedEntities : sortedEntities.slice(0, 6)
  const hasMore = sortedEntities.length > 6

  return (
    <div className="p-4 bg-gray-50 rounded-xl border border-gray-100 hover:border-gray-200 transition-colors">
      {/* 文档标题 */}
      <div className="flex items-start justify-between gap-2 mb-2">
        <div className="flex items-center gap-2 min-w-0 flex-1">
          <FileText size={16} className="text-gray-400 flex-shrink-0" />
          {isEditingTitle ? (
            <div className="flex items-center gap-1 flex-1">
              <input
                value={editTitle}
                onChange={e => setEditTitle(e.target.value)}
                onKeyDown={e => {
                  if (e.key === 'Enter') handleTitleSubmit()
                  if (e.key === 'Escape') handleTitleCancel()
                }}
                className="flex-1 px-2 py-1 text-sm font-medium text-gray-800 border border-indigo-300 rounded outline-none focus:ring-2 focus:ring-indigo-100"
                autoFocus
              />
              <button
                onClick={handleTitleSubmit}
                className="p-1 text-green-600 hover:bg-green-50 rounded"
              >
                <Check size={14} />
              </button>
              <button
                onClick={handleTitleCancel}
                className="p-1 text-gray-400 hover:bg-gray-100 rounded"
              >
                <X size={14} />
              </button>
            </div>
          ) : (
            <div className="flex items-center gap-1 min-w-0 group/title">
              <span className="font-medium text-gray-800 truncate">
                {doc.title || doc.docId}
              </span>
              <button
                onClick={() => { setEditTitle(doc.title); setIsEditingTitle(true) }}
                className="p-1 text-gray-400 hover:text-indigo-600 opacity-0 group-hover/title:opacity-100 transition-opacity"
                title="编辑标题"
              >
                <Pencil size={12} />
              </button>
            </div>
          )}
          {doc.status === 'parsing' && (
            <span className="text-xs text-amber-600 bg-amber-50 px-1.5 py-0.5 rounded flex-shrink-0">
              解析中...
            </span>
          )}
          {doc.status === 'error' && (
            <span
              className="text-xs text-red-600 bg-red-50 px-1.5 py-0.5 rounded flex-shrink-0 max-w-[180px] truncate"
              title={doc.errorMessage || '解析失败'}
            >
              {doc.errorMessage || '解析失败'}
            </span>
          )}
        </div>
        <div className="flex items-center gap-1 flex-shrink-0">
          {doc.docUrl && (
            <a
              href={doc.docUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="p-1.5 rounded-lg text-gray-400 hover:text-indigo-600 hover:bg-indigo-50 transition-colors"
              title="在 KM 中打开"
            >
              <ExternalLink size={14} />
            </a>
          )}
          <button
            onClick={onRetry}
            disabled={isProcessing}
            className="p-1.5 rounded-lg text-gray-400 hover:text-indigo-600 hover:bg-indigo-50 transition-colors disabled:opacity-50"
            title="重新分析"
          >
            <RefreshCw size={14} className={isProcessing ? 'animate-spin' : ''} />
          </button>
        </div>
      </div>

      {/* 实体标签 */}
      {sortedEntities.length > 0 && (
        <div className="flex flex-wrap gap-1.5 mt-2">
          {displayEntities.map(entity => {
            const isSelected = selectedEntityName === entity.name
            return (
              <div key={entity.id} className="group relative">
                <button
                  onClick={() => onEntityClick(entity.name)}
                  className={`
                    px-2 py-1 rounded text-xs transition-colors
                    ${isSelected
                      ? 'bg-indigo-500 text-white'
                      : 'bg-white text-gray-600 border border-gray-200 hover:border-indigo-300 hover:text-indigo-600'
                    }
                  `}
                >
                  {entity.name}
                </button>
                {/* 移除关联按钮 */}
                <button
                  onClick={(e) => { e.stopPropagation(); onUnlink(entity.name) }}
                  className="absolute -top-1 -right-1 w-4 h-4 rounded-full bg-gray-600 text-white flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity text-[10px]"
                  title="移除关联"
                >
                  <Unlink size={10} />
                </button>
              </div>
            )
          })}

          {/* 展开/收起 */}
          {hasMore && (
            <button
              onClick={() => setExpanded(!expanded)}
              className="px-2 py-1 text-xs text-gray-400 hover:text-gray-600 flex items-center gap-0.5"
            >
              {expanded ? (
                <>收起 <ChevronUp size={12} /></>
              ) : (
                <>+{sortedEntities.length - 6} <ChevronDown size={12} /></>
              )}
            </button>
          )}

          {/* 添加实体 */}
          <button
            onClick={onAddEntity}
            className="px-2 py-1 rounded text-xs text-gray-400 border border-dashed border-gray-300 hover:border-indigo-300 hover:text-indigo-600 transition-colors"
          >
            <Plus size={12} className="inline" />
          </button>
        </div>
      )}

      {doc.status === 'done' && doc.entities.length === 0 && (
        <div className="text-xs text-gray-400 mt-2">
          未提取到实体
          <button
            onClick={onAddEntity}
            className="ml-2 text-indigo-500 hover:text-indigo-600"
          >
            手动添加
          </button>
        </div>
      )}
    </div>
  )
}

// 文档管理页中的文档卡片（简洁版，带删除）
function DocCardInManagement({
  doc,
  isProcessing,
  onRetry,
  onDelete,
  onTitleChange,
}: {
  doc: ReturnType<typeof usePrdStore.getState>['documents'][0]
  isProcessing: boolean
  onRetry: () => void
  onDelete: () => void
  onTitleChange: (title: string) => void
}) {
  const [isEditingTitle, setIsEditingTitle] = useState(false)
  const [editTitle, setEditTitle] = useState(doc.title)

  const handleTitleSubmit = () => {
    const trimmed = editTitle.trim()
    if (trimmed && trimmed !== doc.title) {
      onTitleChange(trimmed)
    }
    setIsEditingTitle(false)
  }

  const handleTitleCancel = () => {
    setEditTitle(doc.title)
    setIsEditingTitle(false)
  }

  return (
    <div className="flex items-center gap-4 p-4 bg-gray-50 rounded-xl border border-gray-100 hover:border-gray-200 transition-colors">
      <FileText size={20} className="text-gray-400 flex-shrink-0" />

      <div className="flex-1 min-w-0">
        {isEditingTitle ? (
          <div className="flex items-center gap-1">
            <input
              value={editTitle}
              onChange={e => setEditTitle(e.target.value)}
              onKeyDown={e => {
                if (e.key === 'Enter') handleTitleSubmit()
                if (e.key === 'Escape') handleTitleCancel()
              }}
              className="flex-1 px-2 py-1 text-sm font-medium text-gray-800 border border-indigo-300 rounded outline-none focus:ring-2 focus:ring-indigo-100"
              autoFocus
            />
            <button
              onClick={handleTitleSubmit}
              className="p-1 text-green-600 hover:bg-green-50 rounded"
            >
              <Check size={14} />
            </button>
            <button
              onClick={handleTitleCancel}
              className="p-1 text-gray-400 hover:bg-gray-100 rounded"
            >
              <X size={14} />
            </button>
          </div>
        ) : (
          <div className="flex items-center gap-1 group/title">
            <span className="font-medium text-gray-800 truncate">
              {doc.title || doc.docId}
            </span>
            <button
              onClick={() => { setEditTitle(doc.title); setIsEditingTitle(true) }}
              className="p-1 text-gray-400 hover:text-indigo-600 opacity-0 group-hover/title:opacity-100 transition-opacity"
              title="编辑标题"
            >
              <Pencil size={12} />
            </button>
          </div>
        )}
        <div className="text-xs text-gray-400 mt-0.5 flex items-center gap-2">
          <span className="font-mono">{doc.docId}</span>
          <span>·</span>
          <span>{doc.entities.length} 个实体</span>
          {doc.status === 'parsing' && (
            <span className="text-amber-600">解析中...</span>
          )}
          {doc.status === 'error' && (
            <span
              className="text-red-600 max-w-[200px] truncate"
              title={doc.errorMessage || '解析失败'}
            >
              {doc.errorMessage || '解析失败'}
            </span>
          )}
        </div>
      </div>

      <div className="flex items-center gap-1 flex-shrink-0">
        {doc.docUrl && (
          <a
            href={doc.docUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="p-2 rounded-lg text-gray-400 hover:text-indigo-600 hover:bg-indigo-50 transition-colors"
            title="在 KM 中打开"
          >
            <ExternalLink size={16} />
          </a>
        )}
        <button
          onClick={onRetry}
          disabled={isProcessing}
          className="p-2 rounded-lg text-gray-400 hover:text-indigo-600 hover:bg-indigo-50 transition-colors disabled:opacity-50"
          title="重新分析"
        >
          <RefreshCw size={16} className={isProcessing ? 'animate-spin' : ''} />
        </button>
        <button
          onClick={onDelete}
          className="p-2 rounded-lg text-gray-400 hover:text-red-600 hover:bg-red-50 transition-colors"
          title="删除文档"
        >
          <Trash2 size={16} />
        </button>
      </div>
    </div>
  )
}

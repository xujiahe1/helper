/**
 * 归一化实体详情组件
 * 展示实体的别名、版本历史和冲突状态
 * 支持冲突解决操作和别名管理
 *
 * 简化版：移除面向分类相关 UI，按时间顺序展示版本
 */

import { memo, useState } from 'react'
import { X, AlertTriangle, Tag, FileText, Clock, CheckCircle, Check, RotateCcw, Plus, Trash2, GitBranch, Star, ArrowRight, ArrowLeft, ArrowLeftRight } from 'lucide-react'
import type { NormalizedEntity, PrdDocument, EntityVersion, EntityRelation } from '../../stores/prdStore'
import { usePrdStore } from '../../stores/prdStore'
import { showToast } from '../Toast'

interface NormalizedEntityDetailProps {
  entity: NormalizedEntity
  documents: PrdDocument[]
  onClose: () => void
  onNavigateToEntity?: (entityId: string) => void  // 跳转到关联实体
}

export const NormalizedEntityDetail = memo(function NormalizedEntityDetail({
  entity,
  documents,
  onClose,
  onNavigateToEntity,
}: NormalizedEntityDetailProps) {
  const {
    resolveConflict,
    resetConflictResolution,
    addManualAlias,
    removeAlias,
    updateCanonicalName,
    resolveRelationConflict,
    resetRelationConflictResolution,
  } = usePrdStore()

  const [showResolvePanel, setShowResolvePanel] = useState(false)
  const [resolveMode, setResolveMode] = useState<'authoritative' | 'merged' | 'resolved' | 'split' | null>(null)
  const [selectedVersionId, setSelectedVersionId] = useState<string | null>(null)
  const [mergedText, setMergedText] = useState('')
  const [splitAlias, setSplitAlias] = useState<string | null>(null)
  const [note, setNote] = useState('')

  // 关系冲突解决状态
  const [resolvingRelationId, setResolvingRelationId] = useState<string | null>(null)
  const [relationResolveNote, setRelationResolveNote] = useState('')

  // 别名管理状态
  const [showAliasEditor, setShowAliasEditor] = useState(false)
  const [newAliasInput, setNewAliasInput] = useState('')

  const formatDate = (timestamp: number) => {
    const date = new Date(timestamp)
    return date.toLocaleDateString('zh-CN', {
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
    })
  }

  const getDocUrl = (docId: string) => {
    const doc = documents.find(d => d.id === docId)
    return doc?.docUrl
  }

  // 是否有未解决的冲突
  const hasUnresolvedConflict = entity.hasConflict && !entity.conflictResolution

  // 处理冲突解决
  const handleResolve = () => {
    if (!resolveMode) return

    if (resolveMode === 'authoritative' && !selectedVersionId) {
      showToast('请选择一个权威版本', 'error')
      return
    }

    if (resolveMode === 'merged' && !mergedText.trim()) {
      showToast('请输入合并后的定义', 'error')
      return
    }

    if (resolveMode === 'split' && !splitAlias) {
      showToast('请选择要拆分出去的别名', 'error')
      return
    }

    resolveConflict(entity.id, resolveMode, {
      authoritativeVersionId: selectedVersionId || undefined,
      mergedDescription: mergedText.trim() || undefined,
      splitAlias: splitAlias || undefined,
      note: note.trim() || undefined,
    })

    setShowResolvePanel(false)
    setResolveMode(null)
    setSelectedVersionId(null)
    setMergedText('')
    setSplitAlias(null)
    setNote('')
    showToast(resolveMode === 'split' ? '已拆分为两个实体' : '冲突已解决', 'success')
  }

  // 添加别名
  const handleAddAlias = () => {
    const trimmed = newAliasInput.trim()
    if (!trimmed) return
    if (entity.aliases.includes(trimmed)) {
      showToast('该别名已存在', 'error')
      return
    }
    addManualAlias(entity.id, trimmed)
    setNewAliasInput('')
    showToast('已添加别名', 'success')
  }

  // 删除别名
  const handleRemoveAlias = (alias: string) => {
    if (entity.aliases.length <= 1) {
      showToast('至少保留一个名称', 'error')
      return
    }
    removeAlias(entity.id, alias)
    showToast('已删除别名', 'success')
  }

  // 设为主名称
  const handleSetCanonical = (alias: string) => {
    updateCanonicalName(entity.id, alias)
    showToast('已设为主名称', 'success')
  }

  // 重置冲突解决
  const handleReset = () => {
    if (window.confirm('确定要重置冲突解决状态吗？')) {
      resetConflictResolution(entity.id)
      showToast('已重置', 'success')
    }
  }

  // 解决关系冲突
  const handleResolveRelationConflict = (relationId: string, resolution: 'resolved' | 'merged') => {
    resolveRelationConflict(entity.id, relationId, resolution, {
      note: relationResolveNote.trim() || undefined,
    })
    setResolvingRelationId(null)
    setRelationResolveNote('')
    showToast('关系冲突已标记为已知晓', 'success')
  }

  // 重置关系冲突解决
  const handleResetRelationConflict = (relationId: string) => {
    if (window.confirm('确定要重置此关系的冲突解决状态吗？')) {
      resetRelationConflictResolution(entity.id, relationId)
      showToast('已重置', 'success')
    }
  }

  return (
    <div className="h-full flex flex-col">
      {/* Header */}
      <div className="flex-shrink-0 px-4 py-3 border-b border-gray-100">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2 flex-wrap">
            <h2 className="text-sm font-semibold text-gray-800">{entity.canonicalName}</h2>
            {entity.hasConflict && !entity.conflictResolution && (
              <span className="flex items-center gap-1 px-2 py-0.5 bg-amber-50 text-amber-600 rounded text-xs">
                <AlertTriangle size={12} />
                存在冲突
              </span>
            )}
            {entity.conflictResolution && (
              <span className="flex items-center gap-1 px-2 py-0.5 bg-green-50 text-green-600 rounded text-xs">
                <CheckCircle size={12} />
                已解决
              </span>
            )}
          </div>
          <button
            onClick={onClose}
            className="p-1 text-gray-400 hover:text-gray-600 rounded"
          >
            <X size={16} />
          </button>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto p-4 space-y-4">
        {/* 权威定义展示（已解决冲突时） */}
        {entity.conflictResolution && (
          <section className="p-4 bg-green-50 border-l-4 border-green-500 rounded-r-lg">
            <div className="flex items-center gap-2 text-green-700 font-medium mb-2">
              <Star size={16} className="text-green-600" />
              {entity.conflictResolution.resolution === 'authoritative' && '权威定义'}
              {entity.conflictResolution.resolution === 'merged' && '合并后的定义'}
              {entity.conflictResolution.resolution === 'resolved' && '当前定义'}
            </div>
            <p className="text-sm text-gray-700">{entity.currentDescription}</p>
            {entity.conflictResolution.resolution === 'authoritative' && (
              <p className="text-xs text-gray-500 mt-2">来源：{entity.currentDocTitle}</p>
            )}
            {entity.conflictResolution.note && (
              <p className="text-xs text-gray-500 mt-2 italic">备注：{entity.conflictResolution.note}</p>
            )}
            <button
              onClick={handleReset}
              className="mt-3 text-xs text-gray-500 hover:text-gray-700 flex items-center gap-1"
            >
              <RotateCcw size={12} />
              重置冲突解决
            </button>
          </section>
        )}

        {/* 冲突解决操作区（未解决时） */}
        {entity.hasConflict && !entity.conflictResolution && (
          <section className="p-3 bg-amber-50 border border-amber-100 rounded-lg">
            {showResolvePanel ? (
              // 解决面板
              <div className="space-y-3">
                <div className="flex items-center justify-between">
                  <span className="text-xs font-medium text-amber-700">选择解决方式</span>
                  <button
                    onClick={() => setShowResolvePanel(false)}
                    className="text-xs text-gray-500 hover:text-gray-700"
                  >
                    取消
                  </button>
                </div>

                {/* 解决方式选项 */}
                <div className="space-y-2">
                  <label className="flex items-start gap-2 p-2 bg-white rounded border border-gray-200 cursor-pointer hover:border-indigo-300">
                    <input
                      type="radio"
                      name="resolveMode"
                      checked={resolveMode === 'resolved'}
                      onChange={() => setResolveMode('resolved')}
                      className="mt-0.5"
                    />
                    <div>
                      <span className="text-xs font-medium text-gray-700">标记为已知晓</span>
                      <p className="text-[10px] text-gray-500">冲突已了解，保持现状</p>
                    </div>
                  </label>

                  <label className="flex items-start gap-2 p-2 bg-white rounded border border-gray-200 cursor-pointer hover:border-indigo-300">
                    <input
                      type="radio"
                      name="resolveMode"
                      checked={resolveMode === 'authoritative'}
                      onChange={() => setResolveMode('authoritative')}
                      className="mt-0.5"
                    />
                    <div>
                      <span className="text-xs font-medium text-gray-700">指定权威版本</span>
                      <p className="text-[10px] text-gray-500">选择一个版本作为标准定义</p>
                    </div>
                  </label>

                  <label className="flex items-start gap-2 p-2 bg-white rounded border border-gray-200 cursor-pointer hover:border-indigo-300">
                    <input
                      type="radio"
                      name="resolveMode"
                      checked={resolveMode === 'merged'}
                      onChange={() => setResolveMode('merged')}
                      className="mt-0.5"
                    />
                    <div>
                      <span className="text-xs font-medium text-gray-700">手动编辑合并</span>
                      <p className="text-[10px] text-gray-500">自己写一个统一的定义</p>
                    </div>
                  </label>

                  {/* 澄清分离选项 - 只有别名数量 > 1 时才显示 */}
                  {entity.aliases.length > 1 && (
                    <label className="flex items-start gap-2 p-2 bg-white rounded border border-gray-200 cursor-pointer hover:border-indigo-300">
                      <input
                        type="radio"
                        name="resolveMode"
                        checked={resolveMode === 'split'}
                        onChange={() => setResolveMode('split')}
                        className="mt-0.5"
                      />
                      <div>
                        <span className="text-xs font-medium text-gray-700 flex items-center gap-1">
                          <GitBranch size={12} />
                          澄清分离
                        </span>
                        <p className="text-[10px] text-gray-500">这是两件不同的事，不应该合并</p>
                      </div>
                    </label>
                  )}
                </div>

                {/* 指定权威版本 - 显示所有版本供选择 */}
                {resolveMode === 'authoritative' && (
                  <div className="space-y-2">
                    <span className="text-xs text-gray-600">选择权威版本：</span>
                    {entity.versions.map(v => (
                      <label
                        key={v.rawEntityId}
                        className={`flex items-start gap-2 p-2 rounded border cursor-pointer ${
                          selectedVersionId === v.rawEntityId
                            ? 'bg-indigo-50 border-indigo-300'
                            : 'bg-white border-gray-200 hover:border-gray-300'
                        }`}
                      >
                        <input
                          type="radio"
                          name="authVersion"
                          checked={selectedVersionId === v.rawEntityId}
                          onChange={() => setSelectedVersionId(v.rawEntityId)}
                          className="mt-0.5"
                        />
                        <div className="flex-1 min-w-0">
                          <p className="text-xs text-gray-700 line-clamp-2">{v.description}</p>
                          <p className="text-[10px] text-gray-400 mt-1">来源：{v.docTitle}</p>
                        </div>
                      </label>
                    ))}
                  </div>
                )}

                {/* 手动合并 */}
                {resolveMode === 'merged' && (
                  <div>
                    <span className="text-xs text-gray-600">输入合并后的定义：</span>
                    <textarea
                      value={mergedText}
                      onChange={e => setMergedText(e.target.value)}
                      placeholder="输入统一的定义描述..."
                      className="w-full mt-1 p-2 text-xs border border-gray-200 rounded resize-none focus:outline-none focus:border-indigo-300"
                      rows={3}
                    />
                  </div>
                )}

                {/* 澄清分离 - 选择要拆分的别名 */}
                {resolveMode === 'split' && (
                  <div className="space-y-2">
                    <span className="text-xs text-gray-600">选择要拆分出去的别名：</span>
                    {entity.aliases.filter(a => a !== entity.canonicalName).map(alias => (
                      <label
                        key={alias}
                        className={`flex items-center gap-2 p-2 rounded border cursor-pointer ${
                          splitAlias === alias
                            ? 'bg-indigo-50 border-indigo-300'
                            : 'bg-white border-gray-200 hover:border-gray-300'
                        }`}
                      >
                        <input
                          type="radio"
                          name="splitAlias"
                          checked={splitAlias === alias}
                          onChange={() => setSplitAlias(alias)}
                        />
                        <span className="text-xs text-gray-700">{alias}</span>
                      </label>
                    ))}
                    <p className="text-[10px] text-gray-500">
                      拆分后，选中的别名将成为独立实体，与当前实体分开
                    </p>
                  </div>
                )}

                {/* 备注 */}
                <div>
                  <span className="text-xs text-gray-600">备注（可选）：</span>
                  <input
                    value={note}
                    onChange={e => setNote(e.target.value)}
                    placeholder="记录解决原因或决策依据..."
                    className="w-full mt-1 p-2 text-xs border border-gray-200 rounded focus:outline-none focus:border-indigo-300"
                  />
                </div>

                {/* 确认按钮 */}
                <button
                  onClick={handleResolve}
                  disabled={!resolveMode}
                  className="w-full py-2 text-xs font-medium text-white bg-indigo-500 hover:bg-indigo-600 disabled:bg-gray-300 rounded transition-colors"
                >
                  确认解决
                </button>
              </div>
            ) : (
              // 未解决状态
              <div>
                <div className="flex items-center justify-between">
                  <div>
                    <span className="text-xs font-medium text-amber-700 flex items-center gap-1">
                      <AlertTriangle size={14} />
                      检测到定义冲突
                    </span>
                    {entity.conflictSummary && (
                      <p className="text-xs text-amber-600 mt-1">{entity.conflictSummary}</p>
                    )}
                  </div>
                  <button
                    onClick={() => setShowResolvePanel(true)}
                    className="px-3 py-1.5 text-xs font-medium text-white bg-amber-500 hover:bg-amber-600 rounded transition-colors"
                  >
                    解决冲突
                  </button>
                </div>
              </div>
            )}
          </section>
        )}

        {/* 别名列表 */}
        <section className="pb-4 border-b border-gray-100">
          <div className="flex items-center justify-between mb-2">
            <h3 className="flex items-center gap-1.5 text-xs font-medium text-gray-500">
              <Tag size={14} />
              别名 ({entity.aliases.length})
            </h3>
            <button
              onClick={() => setShowAliasEditor(!showAliasEditor)}
              className="text-xs text-indigo-600 hover:text-indigo-700"
            >
              {showAliasEditor ? '完成' : '管理'}
            </button>
          </div>

          {showAliasEditor ? (
            /* 别名编辑模式 */
            <div className="space-y-2">
              {entity.aliases.map((alias, idx) => (
                <div
                  key={idx}
                  className={`flex items-center gap-2 p-2 rounded border ${
                    alias === entity.canonicalName
                      ? 'bg-indigo-50 border-indigo-200'
                      : 'bg-gray-50 border-gray-200'
                  }`}
                >
                  <span className="flex-1 text-xs text-gray-700">
                    {alias}
                    {entity.manualAliases?.includes(alias) && (
                      <span className="ml-1 text-[10px] text-gray-400">(手动)</span>
                    )}
                  </span>
                  {alias === entity.canonicalName ? (
                    <span className="flex items-center gap-1 px-1.5 py-0.5 bg-indigo-500 text-white rounded text-[10px]">
                      <Star size={10} />
                      主名称
                    </span>
                  ) : (
                    <div className="flex items-center gap-1">
                      <button
                        onClick={() => handleSetCanonical(alias)}
                        className="p-1 text-gray-400 hover:text-indigo-600 hover:bg-indigo-50 rounded"
                        title="设为主名称"
                      >
                        <Star size={12} />
                      </button>
                      <button
                        onClick={() => handleRemoveAlias(alias)}
                        className="p-1 text-gray-400 hover:text-red-600 hover:bg-red-50 rounded"
                        title="删除别名"
                      >
                        <Trash2 size={12} />
                      </button>
                    </div>
                  )}
                </div>
              ))}

              {/* 添加新别名 */}
              <div className="flex items-center gap-2">
                <input
                  value={newAliasInput}
                  onChange={e => setNewAliasInput(e.target.value)}
                  onKeyDown={e => {
                    if (e.key === 'Enter') handleAddAlias()
                  }}
                  placeholder="输入新别名..."
                  className="flex-1 px-2 py-1.5 text-xs border border-gray-200 rounded focus:outline-none focus:border-indigo-300"
                />
                <button
                  onClick={handleAddAlias}
                  disabled={!newAliasInput.trim()}
                  className="p-1.5 text-white bg-indigo-500 hover:bg-indigo-600 disabled:bg-gray-300 rounded transition-colors"
                >
                  <Plus size={14} />
                </button>
              </div>
            </div>
          ) : (
            /* 别名展示模式 */
            <div className="flex flex-wrap gap-1.5">
              {entity.aliases.map((alias, idx) => (
                <span
                  key={idx}
                  className={`px-2 py-1 rounded text-xs ${
                    alias === entity.canonicalName
                      ? 'bg-indigo-100 text-indigo-700 font-medium'
                      : 'bg-gray-100 text-gray-600'
                  }`}
                >
                  {alias}
                  {alias === entity.canonicalName && ' (主名称)'}
                </span>
              ))}
            </div>
          )}
        </section>

        {/* 版本历史（按时间顺序，简化版） */}
        <section className="pb-4 border-b border-gray-100">
          <h3 className="flex items-center gap-1.5 text-xs font-medium text-gray-500 mb-3">
            <FileText size={14} />
            版本历史 ({entity.versions.length} 篇文档)
          </h3>
          <div className="space-y-2">
            {entity.versions.map((version, idx) => (
              <div
                key={version.rawEntityId}
                className={`p-3 rounded-lg border ${
                  idx === 0
                    ? 'bg-green-50 border-green-100'
                    : 'bg-gray-50 border-gray-100'
                }`}
              >
                <div className="flex items-start justify-between gap-2">
                  <p className="text-sm text-gray-700 flex-1">{version.description}</p>
                  {idx === 0 && (
                    <span className="px-1.5 py-0.5 bg-green-500 text-white rounded text-[10px] font-medium flex-shrink-0">
                      最新
                    </span>
                  )}
                </div>
                <div className="mt-2 flex items-center gap-2 text-xs text-gray-500">
                  <FileText size={12} />
                  <a
                    href={getDocUrl(version.docId) || '#'}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-indigo-600 hover:underline"
                  >
                    {version.docTitle}
                  </a>
                  <span className="text-gray-300">·</span>
                  <Clock size={12} />
                  <span>{formatDate(version.docUpdateTime)}</span>
                </div>
              </div>
            ))}
          </div>
        </section>

        {/* 关联关系 */}
        <section className="pb-4 border-b border-gray-100">
          <div className="flex items-center justify-between mb-2">
            <h3 className="flex items-center gap-1.5 text-xs font-medium text-gray-500">
              <GitBranch size={14} />
              关联关系 ({entity.relations?.length || 0})
              {/* 关系冲突提示 */}
              {entity.relationConflicts && entity.relationConflicts.length > 0 && (
                <span className="flex items-center gap-1 px-1.5 py-0.5 bg-amber-50 text-amber-600 rounded text-[10px] ml-2">
                  <AlertTriangle size={10} />
                  {entity.relationConflicts.length} 处冲突
                </span>
              )}
            </h3>
          </div>

          {/* 关系冲突警告区 */}
          {entity.relationConflicts && entity.relationConflicts.length > 0 && (
            <div className="mb-3 space-y-2">
              {entity.relationConflicts.map((conflict, idx) => (
                <div key={idx} className="p-2 bg-amber-50 border border-amber-100 rounded-lg">
                  <div className="flex items-start gap-2">
                    <AlertTriangle size={14} className="text-amber-500 flex-shrink-0 mt-0.5" />
                    <div className="flex-1 min-w-0">
                      <p className="text-xs text-amber-700">{conflict.conflictSummary}</p>
                      <p className="text-[10px] text-amber-600 mt-1">
                        涉及 {conflict.relationIds.length} 条关系
                      </p>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}

          {(!entity.relations || entity.relations.length === 0) ? (
            <p className="text-xs text-gray-400">暂无关联关系，通过「重新分析」自动提取</p>
          ) : (
            <div className="space-y-1.5">
              {entity.relations.map(relation => {
                // 获取方向图标
                const DirectionIcon = relation.direction === 'outgoing'
                  ? ArrowRight
                  : relation.direction === 'incoming'
                    ? ArrowLeft
                    : ArrowLeftRight

                // 是否有冲突
                const hasConflict = relation.hasConflict
                // 是否已解决
                const isResolved = !!relation.conflictResolution
                // 是否正在解决此关系
                const isResolving = resolvingRelationId === relation.id

                return (
                  <div
                    key={relation.id}
                    className={`p-2 rounded-lg border transition-colors ${
                      isResolved
                        ? 'bg-green-50 border-green-200'
                        : hasConflict
                          ? 'bg-amber-50 border-amber-200 hover:border-amber-300'
                          : 'bg-gray-50 border-gray-100 hover:border-indigo-200'
                    }`}
                  >
                    <div className="flex items-start gap-2">
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-1.5 text-xs text-gray-700">
                          <DirectionIcon size={12} className={
                            isResolved ? 'text-green-500' :
                            hasConflict ? 'text-amber-500' : 'text-gray-400'
                          } />
                          <span className={
                            isResolved ? 'text-green-600 font-medium' :
                            hasConflict ? 'text-amber-600 font-medium' : 'text-purple-600 font-medium'
                          }>
                            {relation.relationType}
                          </span>
                          {isResolved && (
                            <CheckCircle size={10} className="text-green-500" />
                          )}
                          {hasConflict && !isResolved && (
                            <span title="存在冲突">
                              <AlertTriangle size={10} className="text-amber-500" />
                            </span>
                          )}
                          <span className="text-gray-400">→</span>
                          {onNavigateToEntity ? (
                            <button
                              onClick={() => onNavigateToEntity(relation.targetEntityId)}
                              className="font-medium text-indigo-600 hover:text-indigo-700 hover:underline"
                            >
                              {relation.targetEntityName}
                            </button>
                          ) : (
                            <span className="font-medium">{relation.targetEntityName}</span>
                          )}
                        </div>
                        {relation.description && (
                          <p className="text-[10px] text-gray-500 mt-0.5 ml-5">{relation.description}</p>
                        )}
                        <div className="flex items-center gap-2 text-[10px] text-gray-400 mt-1 ml-5 flex-wrap">
                          {relation.sources.map((src, idx) => (
                            <span key={idx}>
                              {src.docTitle}{src.anchor ? ` §${src.anchor}` : ''}
                            </span>
                          ))}
                          {relation.confidence < 0.8 && (
                            <span className="px-1 py-0.5 bg-gray-200 rounded">低置信度</span>
                          )}
                          {relation.method === 'manual' && (
                            <span className="px-1 py-0.5 bg-indigo-100 text-indigo-600 rounded">手动</span>
                          )}
                          {isResolved && (
                            <span className="px-1 py-0.5 bg-green-100 text-green-600 rounded">已知晓</span>
                          )}
                          {hasConflict && !isResolved && (
                            <span className="px-1 py-0.5 bg-amber-100 text-amber-600 rounded">有冲突</span>
                          )}
                        </div>

                        {/* 已解决时显示解决信息 */}
                        {isResolved && relation.conflictResolution && (
                          <div className="mt-2 ml-5 flex items-center gap-2">
                            <span className="text-[10px] text-green-600">
                              {relation.conflictResolution.note || '已标记为知晓'}
                            </span>
                            <button
                              onClick={() => handleResetRelationConflict(relation.id)}
                              className="text-[10px] text-gray-400 hover:text-gray-600 flex items-center gap-0.5"
                            >
                              <RotateCcw size={10} />
                              重置
                            </button>
                          </div>
                        )}

                        {/* 有冲突但未解决时，显示解决按钮或解决面板 */}
                        {hasConflict && !isResolved && (
                          <div className="mt-2 ml-5">
                            {isResolving ? (
                              <div className="p-2 bg-white rounded border border-amber-200 space-y-2">
                                <input
                                  value={relationResolveNote}
                                  onChange={e => setRelationResolveNote(e.target.value)}
                                  placeholder="备注（可选）..."
                                  className="w-full px-2 py-1 text-xs border border-gray-200 rounded focus:outline-none focus:border-indigo-300"
                                />
                                <div className="flex items-center gap-2">
                                  <button
                                    onClick={() => handleResolveRelationConflict(relation.id, 'resolved')}
                                    className="px-2 py-1 text-[10px] font-medium text-white bg-amber-500 hover:bg-amber-600 rounded transition-colors"
                                  >
                                    标记为已知晓
                                  </button>
                                  <button
                                    onClick={() => {
                                      setResolvingRelationId(null)
                                      setRelationResolveNote('')
                                    }}
                                    className="px-2 py-1 text-[10px] text-gray-500 hover:text-gray-700"
                                  >
                                    取消
                                  </button>
                                </div>
                              </div>
                            ) : (
                              <button
                                onClick={() => setResolvingRelationId(relation.id)}
                                className="text-[10px] text-amber-600 hover:text-amber-700 font-medium"
                              >
                                处理此冲突
                              </button>
                            )}
                          </div>
                        )}
                      </div>
                    </div>
                  </div>
                )
              })}
            </div>
          )}
        </section>

        {/* 统计信息 */}
        <section className="pt-4 border-t border-gray-100">
          <div className="grid grid-cols-3 gap-3 text-center">
            <div>
              <div className="text-xl font-semibold text-indigo-600">{entity.aliases.length}</div>
              <div className="text-[10px] text-gray-500">别名数</div>
            </div>
            <div>
              <div className="text-xl font-semibold text-indigo-600">{entity.versions.length}</div>
              <div className="text-[10px] text-gray-500">版本数</div>
            </div>
            <div>
              <div className="text-xl font-semibold text-indigo-600">
                {new Set(entity.versions.map(v => v.docId)).size}
              </div>
              <div className="text-[10px] text-gray-500">关联文档</div>
            </div>
          </div>
        </section>
      </div>
    </div>
  )
})

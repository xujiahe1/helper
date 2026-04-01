/**
 * 冲突处理中心组件
 * 集中展示所有实体冲突和关系冲突，便于用户快速定位和处理
 */

import { memo, useMemo, useState } from 'react'
import { X, AlertTriangle, CheckCircle, ChevronRight, ChevronDown, FileText, GitBranch } from 'lucide-react'
import type { NormalizedEntity } from '../../stores/prdStore'

interface ConflictCenterProps {
  entities: NormalizedEntity[]
  onResolve: (entityId: string) => void
  onClose: () => void
}

// 冲突类型
type ConflictType = 'entity' | 'relation'

// 统一的冲突项
interface ConflictItem {
  type: ConflictType
  entity: NormalizedEntity
  isResolved: boolean
  conflictSummary?: string
  relationConflictCount?: number  // 关系冲突数量
}

export const ConflictCenter = memo(function ConflictCenter({
  entities,
  onResolve,
  onClose,
}: ConflictCenterProps) {
  const [showResolved, setShowResolved] = useState(false)
  const [filterType, setFilterType] = useState<'all' | 'entity' | 'relation'>('all')

  // 分类所有冲突
  const { allConflicts, unresolvedCount, resolvedCount } = useMemo(() => {
    const conflicts: ConflictItem[] = []

    for (const entity of entities) {
      // 实体定义冲突
      if (entity.hasConflict) {
        conflicts.push({
          type: 'entity',
          entity,
          isResolved: !!entity.conflictResolution,
          conflictSummary: entity.conflictSummary,
        })
      }

      // 关系冲突
      if (entity.relationConflicts && entity.relationConflicts.length > 0) {
        conflicts.push({
          type: 'relation',
          entity,
          isResolved: false,  // 关系冲突暂无解决机制，都算未解决
          conflictSummary: entity.relationConflicts.map(c => c.conflictSummary).join('；'),
          relationConflictCount: entity.relationConflicts.length,
        })
      }
    }

    // 按类型和状态排序：未解决的在前，同状态下实体冲突在前
    conflicts.sort((a, b) => {
      if (a.isResolved !== b.isResolved) {
        return a.isResolved ? 1 : -1
      }
      if (a.type !== b.type) {
        return a.type === 'entity' ? -1 : 1
      }
      return a.entity.canonicalName.localeCompare(b.entity.canonicalName)
    })

    return {
      allConflicts: conflicts,
      unresolvedCount: conflicts.filter(c => !c.isResolved).length,
      resolvedCount: conflicts.filter(c => c.isResolved).length,
    }
  }, [entities])

  // 过滤后的冲突列表
  const filteredConflicts = useMemo(() => {
    let result = allConflicts
    if (filterType !== 'all') {
      result = result.filter(c => c.type === filterType)
    }
    return result
  }, [allConflicts, filterType])

  const unresolvedConflicts = filteredConflicts.filter(c => !c.isResolved)
  const resolvedConflicts = filteredConflicts.filter(c => c.isResolved)

  const total = allConflicts.length
  const progress = total > 0 ? (resolvedCount / total) * 100 : 0

  // 统计各类型数量
  const entityConflictCount = allConflicts.filter(c => c.type === 'entity').length
  const relationConflictCount = allConflicts.filter(c => c.type === 'relation').length

  if (total === 0) {
    return (
      <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30">
        <div className="bg-white rounded-xl shadow-xl w-full max-w-md mx-4 p-6">
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-lg font-semibold text-gray-800">冲突处理中心</h2>
            <button
              onClick={onClose}
              className="p-1 text-gray-400 hover:text-gray-600 rounded"
            >
              <X size={20} />
            </button>
          </div>
          <div className="text-center py-8">
            <CheckCircle size={48} className="mx-auto text-green-500 mb-4" />
            <p className="text-gray-600">没有需要处理的冲突</p>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30">
      <div className="bg-white rounded-xl shadow-xl w-full max-w-lg mx-4 max-h-[80vh] flex flex-col">
        {/* Header */}
        <div className="flex-shrink-0 px-6 py-4 border-b border-gray-100">
          <div className="flex items-center justify-between">
            <h2 className="text-lg font-semibold text-gray-800">冲突处理中心</h2>
            <button
              onClick={onClose}
              className="p-1 text-gray-400 hover:text-gray-600 rounded"
            >
              <X size={20} />
            </button>
          </div>

          {/* 进度条 */}
          <div className="mt-4">
            <div className="flex items-center justify-between text-sm text-gray-600 mb-2">
              <span>处理进度</span>
              <span>
                {resolvedCount} / {total} 已处理
              </span>
            </div>
            <div className="h-2 bg-gray-100 rounded-full overflow-hidden">
              <div
                className="h-full bg-green-500 transition-all duration-300"
                style={{ width: `${progress}%` }}
              />
            </div>
          </div>

          {/* 类型筛选 */}
          <div className="flex items-center gap-2 mt-4">
            <button
              onClick={() => setFilterType('all')}
              className={`px-3 py-1.5 text-xs font-medium rounded-lg transition-colors ${
                filterType === 'all'
                  ? 'bg-indigo-100 text-indigo-700'
                  : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
              }`}
            >
              全部 ({total})
            </button>
            <button
              onClick={() => setFilterType('entity')}
              className={`flex items-center gap-1 px-3 py-1.5 text-xs font-medium rounded-lg transition-colors ${
                filterType === 'entity'
                  ? 'bg-amber-100 text-amber-700'
                  : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
              }`}
            >
              <FileText size={12} />
              实体冲突 ({entityConflictCount})
            </button>
            <button
              onClick={() => setFilterType('relation')}
              className={`flex items-center gap-1 px-3 py-1.5 text-xs font-medium rounded-lg transition-colors ${
                filterType === 'relation'
                  ? 'bg-purple-100 text-purple-700'
                  : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
              }`}
            >
              <GitBranch size={12} />
              关系冲突 ({relationConflictCount})
            </button>
          </div>
        </div>

        {/* Content */}
        <div className="flex-1 overflow-y-auto p-4 space-y-4">
          {/* 待处理列表 */}
          {unresolvedConflicts.length > 0 && (
            <section>
              <h3 className="flex items-center gap-2 text-sm font-medium text-amber-700 mb-3">
                <AlertTriangle size={16} />
                待处理 ({unresolvedConflicts.length})
              </h3>
              <div className="space-y-2">
                {unresolvedConflicts.map((item, idx) => (
                  <ConflictCard
                    key={`${item.type}-${item.entity.id}-${idx}`}
                    item={item}
                    onResolve={() => {
                      onResolve(item.entity.id)
                      onClose()
                    }}
                  />
                ))}
              </div>
            </section>
          )}

          {/* 已处理列表（可折叠） */}
          {resolvedConflicts.length > 0 && (
            <section>
              <button
                onClick={() => setShowResolved(!showResolved)}
                className="flex items-center gap-2 text-sm font-medium text-green-700 hover:text-green-800 w-full"
              >
                {showResolved ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
                <CheckCircle size={16} />
                已处理 ({resolvedConflicts.length})
              </button>
              {showResolved && (
                <div className="space-y-2 mt-3">
                  {resolvedConflicts.map((item, idx) => (
                    <ConflictCard
                      key={`${item.type}-${item.entity.id}-${idx}`}
                      item={item}
                      onResolve={() => {
                        onResolve(item.entity.id)
                        onClose()
                      }}
                    />
                  ))}
                </div>
              )}
            </section>
          )}

          {/* 无匹配结果 */}
          {filteredConflicts.length === 0 && (
            <div className="text-center py-8 text-gray-500 text-sm">
              没有匹配的冲突
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="flex-shrink-0 px-6 py-4 border-t border-gray-100 bg-gray-50 rounded-b-xl">
          <p className="text-xs text-gray-500 text-center">
            点击"去解决"可跳转到实体详情页进行冲突处理
          </p>
        </div>
      </div>
    </div>
  )
})

// 冲突卡片组件
function ConflictCard({
  item,
  onResolve,
}: {
  item: ConflictItem
  onResolve: () => void
}) {
  const { type, entity, isResolved, conflictSummary, relationConflictCount } = item
  const docCount = new Set(entity.versions.map(v => v.docId)).size

  const isEntityConflict = type === 'entity'

  return (
    <div
      className={`p-4 rounded-lg border ${
        isResolved
          ? 'bg-green-50 border-green-100'
          : isEntityConflict
            ? 'bg-amber-50 border-amber-100'
            : 'bg-purple-50 border-purple-100'
      }`}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="flex-1 min-w-0">
          {/* 冲突类型标签 + 实体名称 */}
          <div className="flex items-center gap-2">
            {isResolved ? (
              <CheckCircle size={16} className="text-green-500 flex-shrink-0" />
            ) : isEntityConflict ? (
              <AlertTriangle size={16} className="text-amber-500 flex-shrink-0" />
            ) : (
              <GitBranch size={16} className="text-purple-500 flex-shrink-0" />
            )}
            <span className={`px-1.5 py-0.5 rounded text-[10px] font-medium ${
              isEntityConflict
                ? 'bg-amber-100 text-amber-700'
                : 'bg-purple-100 text-purple-700'
            }`}>
              {isEntityConflict ? '实体定义' : '关系冲突'}
            </span>
            <span className="font-medium text-gray-800 truncate">
              {entity.canonicalName}
            </span>
          </div>

          {/* 冲突摘要 */}
          {conflictSummary && !isResolved && (
            <p className={`text-sm mt-1 line-clamp-2 ${
              isEntityConflict ? 'text-amber-700' : 'text-purple-700'
            }`}>
              {conflictSummary}
            </p>
          )}

          {/* 已解决信息 */}
          {isResolved && entity.conflictResolution && (
            <p className="text-sm text-green-700 mt-1">
              {entity.conflictResolution.resolution === 'authoritative' && '已指定权威版本'}
              {entity.conflictResolution.resolution === 'merged' && '已手动合并定义'}
              {entity.conflictResolution.resolution === 'resolved' && '已标记为知晓'}
              {entity.conflictResolution.resolution === 'split' && '已拆分为独立实体'}
            </p>
          )}

          {/* 来源信息 */}
          <div className="flex items-center gap-1 text-xs text-gray-500 mt-2 flex-wrap">
            <FileText size={12} />
            <span>来自 {docCount} 篇文档</span>
            {entity.aliases.length > 1 && (
              <>
                <span className="text-gray-300 mx-1">·</span>
                <span>{entity.aliases.length} 个别名</span>
              </>
            )}
            {!isEntityConflict && relationConflictCount && relationConflictCount > 1 && (
              <>
                <span className="text-gray-300 mx-1">·</span>
                <span>{relationConflictCount} 处关系冲突</span>
              </>
            )}
          </div>
        </div>

        {/* 操作按钮 */}
        <button
          onClick={onResolve}
          className={`flex-shrink-0 px-3 py-1.5 rounded-lg text-sm font-medium transition-colors ${
            isResolved
              ? 'text-green-700 hover:bg-green-100'
              : isEntityConflict
                ? 'text-white bg-amber-500 hover:bg-amber-600'
                : 'text-white bg-purple-500 hover:bg-purple-600'
          }`}
        >
          {isResolved ? '查看' : '去解决'}
          <ChevronRight size={14} className="inline ml-0.5" />
        </button>
      </div>
    </div>
  )
}

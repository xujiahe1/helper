import { useState } from 'react'
import { useGuidedPrdStore } from '../../stores/guidedPrdStore'
import { useChatStore } from '../../store'
import {
  CheckCircle2,
  Clock,
  Circle,
  ChevronDown,
  ChevronRight,
  Pencil,
  Check,
  X,
  RefreshCw,
  Plus,
  Trash2,
  GripVertical,
  FileText,
} from 'lucide-react'
import type { FeatureStatus } from '../../types/guided-prd'

interface PrdOutlinePanelProps {
  conversationId: string
}

// 获取模块的显示配置（根据状态和来源）
function getFeatureDisplayConfig(feature: { status: FeatureStatus; source?: string }) {
  // 从文档导入的模块用特殊图标
  if (feature.source === 'doc') {
    if (feature.status === 'locked') {
      return {
        icon: <FileText size={14} className="text-blue-500" />,
        color: 'text-blue-600',
        label: '已存在',
      }
    }
    if (feature.status === 'pending') {
      return {
        icon: <FileText size={14} className="text-amber-500" />,
        color: 'text-amber-600',
        label: '待更新',
      }
    }
  }

  // 默认配置
  const statusConfig: Record<FeatureStatus, { icon: React.ReactNode; color: string; label: string }> = {
    pending: {
      icon: <Circle size={14} className="text-gray-300" />,
      color: 'text-gray-500',
      label: '待处理',
    },
    drilling: {
      icon: <Clock size={14} className="text-blue-500 animate-pulse" />,
      color: 'text-blue-600',
      label: '进行中',
    },
    locked: {
      icon: <CheckCircle2 size={14} className="text-green-500" />,
      color: 'text-green-600',
      label: '已完成',
    },
  }
  return statusConfig[feature.status]
}

export function PrdOutlinePanel({ conversationId }: PrdOutlinePanelProps) {
  const session = useGuidedPrdStore((s) => s.sessions[conversationId])
  const updateFeatureOutline = useGuidedPrdStore((s) => s.updateFeatureOutline)
  const updateFeatureTitle = useGuidedPrdStore((s) => s.updateFeatureTitle)
  const resetFeatureStatus = useGuidedPrdStore((s) => s.resetFeatureStatus)
  const setPendingRegenerate = useGuidedPrdStore((s) => s.setPendingRegenerate)
  const addFeature = useGuidedPrdStore((s) => s.addFeature)
  const deleteFeature = useGuidedPrdStore((s) => s.deleteFeature)
  const reorderFeatures = useGuidedPrdStore((s) => s.reorderFeatures)
  const sendMessage = useChatStore((s) => s.sendMessage)

  const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set())
  const [editingId, setEditingId] = useState<string | null>(null)
  const [editingField, setEditingField] = useState<'title' | 'outline'>('outline')
  const [editValue, setEditValue] = useState('')
  const [isAddingNew, setIsAddingNew] = useState(false)
  const [newTitle, setNewTitle] = useState('')
  const [newOutline, setNewOutline] = useState('')
  const [draggedIndex, setDraggedIndex] = useState<number | null>(null)

  if (!session) {
    return (
      <div className="flex-1 flex items-center justify-center text-gray-300 text-sm px-4 text-center">
        <div>
          <p className="mb-2">AI 会先询问您要写什么内容</p>
          <p className="text-xs text-gray-400">确认后，写作规划将显示在这里</p>
        </div>
      </div>
    )
  }

  const lockedCount = session.features.filter((f) => f.status === 'locked').length
  const total = session.features.length

  const toggleExpand = (featureId: string) => {
    setExpandedIds((prev) => {
      const next = new Set(prev)
      if (next.has(featureId)) {
        next.delete(featureId)
      } else {
        next.add(featureId)
      }
      return next
    })
  }

  const startEditTitle = (featureId: string, currentTitle: string) => {
    setEditingId(featureId)
    setEditingField('title')
    setEditValue(currentTitle)
  }

  const startEditOutline = (featureId: string, currentOutline: string) => {
    setEditingId(featureId)
    setEditingField('outline')
    setEditValue(currentOutline || '')
    setExpandedIds((prev) => new Set(prev).add(featureId))
  }

  const cancelEdit = () => {
    setEditingId(null)
    setEditValue('')
  }

  const saveEdit = (featureId: string) => {
    if (editingField === 'title') {
      if (editValue.trim()) {
        updateFeatureTitle(conversationId, featureId, editValue.trim())
      }
    } else {
      updateFeatureOutline(conversationId, featureId, editValue.trim(), true)
    }
    setEditingId(null)
    setEditValue('')
  }

  const handleAddNew = () => {
    if (!newTitle.trim()) return
    addFeature(conversationId, newTitle.trim(), newOutline.trim() || undefined)
    setNewTitle('')
    setNewOutline('')
    setIsAddingNew(false)
  }

  const handleDelete = (featureId: string) => {
    if (confirm('确定删除这个模块吗？')) {
      deleteFeature(conversationId, featureId)
    }
  }

  const handleDragStart = (index: number) => {
    setDraggedIndex(index)
  }

  const handleDragOver = (e: React.DragEvent, index: number) => {
    e.preventDefault()
    if (draggedIndex === null || draggedIndex === index) return
    reorderFeatures(conversationId, draggedIndex, index)
    setDraggedIndex(index)
  }

  const handleDragEnd = () => {
    setDraggedIndex(null)
  }

  const handleRegenerate = async (featureId: string) => {
    const feature = session.features.find((f) => f.featureId === featureId)
    if (!feature) return

    resetFeatureStatus(conversationId, featureId)
    setPendingRegenerate(conversationId, featureId)

    // 根据模块状态生成不同的消息
    let message: string
    if (feature.status === 'locked') {
      // 已完成的模块要重新生成
      message = `请重新生成「${feature.title}」这个模块的内容。`
    } else if (feature.outline) {
      // 有摘要的模块（包括新增的、修改过的）
      message = `请根据以下要求，撰写「${feature.title}」模块：\n\n${feature.outline}`
    } else {
      // 没有摘要的模块
      message = `请帮我撰写「${feature.title}」这个模块的内容。`
    }

    await sendMessage(message, undefined, false, undefined, conversationId, true, true)
  }

  // 开始写某个模块（用于新增或待处理的模块）
  const handleStartWriting = async (featureId: string) => {
    const feature = session.features.find((f) => f.featureId === featureId)
    if (!feature) return

    setPendingRegenerate(conversationId, featureId)

    let message: string
    if (feature.outline) {
      message = `请根据以下要求，撰写「${feature.title}」模块：\n\n${feature.outline}`
    } else {
      message = `请帮我撰写「${feature.title}」这个模块的内容。`
    }

    await sendMessage(message, undefined, false, undefined, conversationId, true, true)
  }

  // 更新文档中已存在的章节
  const handleUpdateDocSection = async (featureId: string) => {
    const feature = session.features.find((f) => f.featureId === featureId)
    if (!feature) return

    resetFeatureStatus(conversationId, featureId)
    setPendingRegenerate(conversationId, featureId)

    let message: string
    if (feature.outline) {
      // 用户指定了更新要求
      message = `请更新文档中「${feature.title}」章节的内容，更新要求：\n\n${feature.outline}`
    } else {
      // 没有特别要求，让 AI 自行判断
      message = `请更新文档中「${feature.title}」章节的内容。${
        feature.originalContent
          ? `\n\n当前内容摘要：${feature.originalContent.slice(0, 300)}...`
          : ''
      }`
    }

    if (feature.docAnchor) {
      message += `\n\n（文档锚点：${feature.docAnchor}）`
    }

    await sendMessage(message, undefined, false, undefined, conversationId, true, true)
  }

  return (
    <div className="flex-1 flex flex-col overflow-hidden">
      {/* 进度条 */}
      {total > 0 && (
        <div className="px-4 py-3 border-b border-gray-100">
          <div className="flex items-center justify-between mb-2">
            <span className="text-xs font-medium text-gray-600">写作进度</span>
            <span className="text-xs text-gray-400">{lockedCount}/{total} 已完成</span>
          </div>
          <div className="h-1.5 bg-gray-100 rounded-full overflow-hidden">
            <div
              className="h-full bg-green-500 rounded-full transition-all duration-500"
              style={{ width: total > 0 ? `${(lockedCount / total) * 100}%` : '0%' }}
            />
          </div>
        </div>
      )}

      {/* 模块列表 */}
      <div className="flex-1 overflow-y-auto">
        {session.features.length === 0 ? (
          <div className="px-4 py-8 text-center text-gray-400 text-xs">
            <p>暂无模块</p>
            <p className="mt-1">AI 会根据对话生成写作规划</p>
          </div>
        ) : (
          session.features.map((feature, index) => {
            const isExpanded = expandedIds.has(feature.featureId)
            const isEditingThis = editingId === feature.featureId
            const config = getFeatureDisplayConfig(feature)
            const isFromDoc = feature.source === 'doc'

            return (
              <div
                key={feature.featureId}
                className={`border-b border-gray-50 last:border-b-0 ${
                  draggedIndex === index ? 'opacity-50' : ''
                }`}
                draggable
                onDragStart={() => handleDragStart(index)}
                onDragOver={(e) => handleDragOver(e, index)}
                onDragEnd={handleDragEnd}
              >
                {/* 标题行 */}
                <div
                  className={`flex items-center gap-1 px-2 py-2 cursor-pointer hover:bg-gray-50 transition-colors ${
                    feature.userEdited && feature.status !== 'locked' ? 'bg-amber-50/50' : ''
                  }`}
                >
                  {/* 拖拽手柄 */}
                  <div className="p-1 text-gray-300 hover:text-gray-500 cursor-grab">
                    <GripVertical size={12} />
                  </div>

                  {/* 展开按钮 */}
                  <button
                    className="p-0.5 text-gray-400 hover:text-gray-600"
                    onClick={() => toggleExpand(feature.featureId)}
                  >
                    {isExpanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                  </button>

                  {/* 状态图标 */}
                  {config.icon}

                  {/* 标题（可编辑） */}
                  {isEditingThis && editingField === 'title' ? (
                    <input
                      autoFocus
                      value={editValue}
                      onChange={(e) => setEditValue(e.target.value)}
                      onBlur={() => saveEdit(feature.featureId)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') saveEdit(feature.featureId)
                        if (e.key === 'Escape') cancelEdit()
                      }}
                      className="flex-1 text-sm border border-violet-300 rounded px-1.5 py-0.5 outline-none focus:ring-1 focus:ring-violet-300"
                      onClick={(e) => e.stopPropagation()}
                    />
                  ) : (
                    <span
                      className={`flex-1 text-sm truncate ${
                        feature.status === 'locked' ? 'line-through text-gray-400' : config.color
                      }`}
                      onClick={() => toggleExpand(feature.featureId)}
                      onDoubleClick={(e) => {
                        e.stopPropagation()
                        if (feature.status !== 'drilling') {
                          startEditTitle(feature.featureId, feature.title)
                        }
                      }}
                      title="双击编辑标题"
                    >
                      {feature.title}
                    </span>
                  )}

                  {/* 已修改标记：只在 AI 生成的模块被用户修改过、且尚未完成时显示 */}
                  {feature.userEdited && feature.status !== 'locked' && (
                    <span
                      className="text-[10px] text-amber-600 bg-amber-100 px-1 py-0.5 rounded"
                      title="摘要已被修改，可点击「开始写」按新摘要生成"
                    >
                      已改
                    </span>
                  )}

                  {/* 删除按钮 */}
                  {feature.status !== 'drilling' && (
                    <button
                      onClick={(e) => {
                        e.stopPropagation()
                        handleDelete(feature.featureId)
                      }}
                      className="p-1 text-gray-300 hover:text-red-500 transition-colors"
                      title="删除模块"
                    >
                      <Trash2 size={12} />
                    </button>
                  )}
                </div>

                {/* 展开内容 */}
                {isExpanded && (
                  <div className="px-4 pb-3 pl-9">
                    {isEditingThis && editingField === 'outline' ? (
                      <div className="space-y-2">
                        <textarea
                          autoFocus
                          value={editValue}
                          onChange={(e) => setEditValue(e.target.value)}
                          className="w-full text-xs border border-violet-300 rounded-md px-2.5 py-2 outline-none focus:ring-1 focus:ring-violet-300 resize-none"
                          rows={3}
                          placeholder="描述这个模块要写什么内容..."
                        />
                        <div className="flex items-center gap-2">
                          <button
                            onClick={() => saveEdit(feature.featureId)}
                            className="flex items-center gap-1 text-xs text-white bg-violet-500 hover:bg-violet-600 px-2 py-1 rounded transition-colors"
                          >
                            <Check size={11} />
                            保存
                          </button>
                          <button
                            onClick={cancelEdit}
                            className="flex items-center gap-1 text-xs text-gray-500 hover:text-gray-700 px-2 py-1 rounded hover:bg-gray-100 transition-colors"
                          >
                            <X size={11} />
                            取消
                          </button>
                        </div>
                      </div>
                    ) : (
                      <div className="space-y-2">
                        <p className="text-xs text-gray-600 leading-relaxed whitespace-pre-wrap">
                          {feature.outline || '（暂无摘要，点击编辑添加）'}
                        </p>

                        {/* 操作按钮 */}
                        <div className="flex items-center gap-3 pt-1">
                          {/* 编辑按钮：非进行中状态都可编辑 */}
                          {feature.status !== 'drilling' && (
                            <button
                              onClick={(e) => {
                                e.stopPropagation()
                                startEditOutline(feature.featureId, feature.outline || '')
                              }}
                              className="flex items-center gap-1 text-xs text-gray-400 hover:text-violet-500 transition-colors"
                            >
                              <Pencil size={11} />
                              编辑
                            </button>
                          )}

                          {/* 开始写：待处理状态的非文档模块 */}
                          {feature.status === 'pending' && !isFromDoc && (
                            <button
                              onClick={(e) => {
                                e.stopPropagation()
                                handleStartWriting(feature.featureId)
                              }}
                              className="flex items-center gap-1 text-xs text-violet-500 hover:text-violet-700 font-medium transition-colors"
                            >
                              <Pencil size={11} />
                              开始写
                            </button>
                          )}

                          {/* 更新内容：从文档导入的待更新模块 */}
                          {feature.status === 'pending' && isFromDoc && (
                            <button
                              onClick={(e) => {
                                e.stopPropagation()
                                handleUpdateDocSection(feature.featureId)
                              }}
                              className="flex items-center gap-1 text-xs text-amber-500 hover:text-amber-700 font-medium transition-colors"
                            >
                              <RefreshCw size={11} />
                              更新内容
                            </button>
                          )}

                          {/* 重新生成：已完成的非文档模块 */}
                          {feature.status === 'locked' && !isFromDoc && (
                            <button
                              onClick={(e) => {
                                e.stopPropagation()
                                handleRegenerate(feature.featureId)
                              }}
                              className="flex items-center gap-1 text-xs text-gray-400 hover:text-violet-500 transition-colors"
                            >
                              <RefreshCw size={11} />
                              重新生成
                            </button>
                          )}

                          {/* 更新内容：从文档导入的已存在模块 */}
                          {feature.status === 'locked' && isFromDoc && (
                            <button
                              onClick={(e) => {
                                e.stopPropagation()
                                handleUpdateDocSection(feature.featureId)
                              }}
                              className="flex items-center gap-1 text-xs text-blue-400 hover:text-blue-600 transition-colors"
                            >
                              <RefreshCw size={11} />
                              更新内容
                            </button>
                          )}
                        </div>

                        {/* 已生成的内容预览 */}
                        {feature.generatedContent && (
                          <div className="mt-2 pt-2 border-t border-gray-100">
                            <p className="text-xs text-gray-400 mb-1">已生成：</p>
                            <p className="text-xs text-gray-500 leading-relaxed line-clamp-2">
                              {feature.generatedContent.slice(0, 150)}
                              {feature.generatedContent.length > 150 ? '...' : ''}
                            </p>
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                )}
              </div>
            )
          })
        )}

        {/* 添加新模块 */}
        {isAddingNew ? (
          <div className="px-4 py-3 border-t border-gray-100 bg-gray-50/50">
            <input
              autoFocus
              value={newTitle}
              onChange={(e) => setNewTitle(e.target.value)}
              placeholder="模块标题"
              className="w-full text-sm border border-gray-200 rounded px-2.5 py-1.5 mb-2 outline-none focus:border-violet-300 focus:ring-1 focus:ring-violet-300"
            />
            <textarea
              value={newOutline}
              onChange={(e) => setNewOutline(e.target.value)}
              placeholder="模块摘要（可选）"
              className="w-full text-xs border border-gray-200 rounded px-2.5 py-2 mb-2 outline-none focus:border-violet-300 focus:ring-1 focus:ring-violet-300 resize-none"
              rows={2}
            />
            <div className="flex items-center gap-2">
              <button
                onClick={handleAddNew}
                disabled={!newTitle.trim()}
                className="flex items-center gap-1 text-xs text-white bg-violet-500 hover:bg-violet-600 disabled:bg-gray-300 px-2.5 py-1 rounded transition-colors"
              >
                <Check size={11} />
                添加
              </button>
              <button
                onClick={() => {
                  setIsAddingNew(false)
                  setNewTitle('')
                  setNewOutline('')
                }}
                className="flex items-center gap-1 text-xs text-gray-500 hover:text-gray-700 px-2 py-1 rounded hover:bg-gray-100 transition-colors"
              >
                <X size={11} />
                取消
              </button>
            </div>
          </div>
        ) : (
          <button
            onClick={() => setIsAddingNew(true)}
            className="w-full px-4 py-2.5 text-xs text-gray-400 hover:text-violet-500 hover:bg-gray-50 transition-colors flex items-center justify-center gap-1 border-t border-gray-100"
          >
            <Plus size={14} />
            添加模块
          </button>
        )}
      </div>
    </div>
  )
}

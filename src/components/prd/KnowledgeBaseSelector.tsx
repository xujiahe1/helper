/**
 * 知识库选择器组件
 * 必须选择一个具体的知识库进行管理
 */

import { memo, useState, useRef, useEffect, useMemo } from 'react'
import { Database, Plus, ChevronDown, Pencil, Trash2, Check, X, AlertCircle, ArrowRight } from 'lucide-react'
import { usePrdStore, type KnowledgeBase } from '../../stores/prdStore'
import { showToast } from '../Toast'

interface KnowledgeBaseSelectorProps {
  className?: string
}

export const KnowledgeBaseSelector = memo(function KnowledgeBaseSelector({
  className = '',
}: KnowledgeBaseSelectorProps) {
  const {
    knowledgeBases,
    activeKnowledgeBaseId,
    createKnowledgeBase,
    renameKnowledgeBase,
    deleteKnowledgeBase,
    setActiveKnowledgeBase,
    documents,
    getOrphanedDocuments,
    migrateOrphanedDocuments,
    deleteOrphanedDocuments,
  } = usePrdStore()

  const [isOpen, setIsOpen] = useState(false)
  const [isCreating, setIsCreating] = useState(false)
  const [newName, setNewName] = useState('')
  const [editingId, setEditingId] = useState<string | null>(null)
  const [editName, setEditName] = useState('')
  const dropdownRef = useRef<HTMLDivElement>(null)

  // 点击外部关闭下拉框
  useEffect(() => {
    const handleClickOutside = (e: MouseEvent) => {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target as Node)) {
        setIsOpen(false)
        setIsCreating(false)
        setEditingId(null)
      }
    }
    document.addEventListener('mousedown', handleClickOutside)
    return () => document.removeEventListener('mousedown', handleClickOutside)
  }, [])

  const activeKb = knowledgeBases.find(kb => kb.id === activeKnowledgeBaseId)

  // 获取知识库的文档数量
  const getDocCount = (kbId: string) => {
    return documents.filter(doc => doc.knowledgeBaseId === kbId).length
  }

  // 获取孤立文档数量
  const orphanedDocs = useMemo(() => getOrphanedDocuments(), [documents])
  const orphanedCount = orphanedDocs.length

  // 迁移孤立文档到当前知识库
  const handleMigrateOrphaned = () => {
    if (!activeKnowledgeBaseId) {
      showToast('请先选择一个知识库', 'error')
      return
    }
    const count = migrateOrphanedDocuments(activeKnowledgeBaseId)
    showToast(`已将 ${count} 个文档迁移到当前知识库`, 'success')
  }

  // 删除孤立文档
  const handleDeleteOrphaned = () => {
    if (window.confirm(`确定要删除 ${orphanedCount} 个未分类文档吗？此操作不可恢复。`)) {
      const count = deleteOrphanedDocuments()
      showToast(`已删除 ${count} 个未分类文档`, 'success')
    }
  }

  // 创建知识库
  const handleCreate = () => {
    const trimmed = newName.trim()
    if (!trimmed) {
      showToast('请输入知识库名称', 'error')
      return
    }
    if (knowledgeBases.some(kb => kb.name === trimmed)) {
      showToast('知识库名称已存在', 'error')
      return
    }
    const newId = createKnowledgeBase(trimmed)
    setNewName('')
    setIsCreating(false)
    setIsOpen(false)
    // 自动选中新建的知识库
    setActiveKnowledgeBase(newId)
    showToast('知识库创建成功', 'success')
  }

  // 重命名知识库
  const handleRename = (id: string) => {
    const trimmed = editName.trim()
    if (!trimmed) {
      showToast('请输入知识库名称', 'error')
      return
    }
    if (knowledgeBases.some(kb => kb.id !== id && kb.name === trimmed)) {
      showToast('知识库名称已存在', 'error')
      return
    }
    renameKnowledgeBase(id, trimmed)
    setEditingId(null)
    showToast('重命名成功', 'success')
  }

  // 删除知识库
  const handleDelete = (kb: KnowledgeBase) => {
    const docCount = getDocCount(kb.id)
    const message = docCount > 0
      ? `确定要删除知识库「${kb.name}」吗？其中的 ${docCount} 个文档也将被删除。`
      : `确定要删除知识库「${kb.name}」吗？`

    if (window.confirm(message)) {
      deleteKnowledgeBase(kb.id)
      showToast('知识库已删除', 'success')
    }
  }

  // 选择知识库
  const handleSelect = (id: string) => {
    setActiveKnowledgeBase(id)
    setIsOpen(false)
  }

  // 如果没有知识库，显示创建提示
  if (knowledgeBases.length === 0) {
    return (
      <div ref={dropdownRef} className={`relative ${className}`}>
        <button
          onClick={() => setIsOpen(!isOpen)}
          className="flex items-center gap-2 px-3 py-2 bg-amber-50 border border-amber-200 rounded-lg hover:border-amber-300 transition-colors text-sm"
        >
          <Database size={16} className="text-amber-500" />
          <span className="text-amber-700">请创建知识库</span>
          <ChevronDown size={14} className={`text-amber-400 transition-transform ${isOpen ? 'rotate-180' : ''}`} />
        </button>

        {isOpen && (
          <div className="absolute top-full left-0 mt-1 w-72 bg-white rounded-xl shadow-xl border border-gray-200 z-50 overflow-hidden">
            <div className="px-4 py-3 text-sm text-gray-500 text-center border-b border-gray-100">
              需要先创建一个知识库才能开始使用
            </div>

            {isCreating ? (
              <div className="p-3">
                <div className="flex items-center gap-2">
                  <input
                    value={newName}
                    onChange={e => setNewName(e.target.value)}
                    onKeyDown={e => {
                      if (e.key === 'Enter') handleCreate()
                      if (e.key === 'Escape') {
                        setIsCreating(false)
                        setNewName('')
                      }
                    }}
                    placeholder="输入知识库名称"
                    className="flex-1 px-3 py-2 text-sm border border-gray-200 rounded-lg outline-none focus:border-indigo-300 focus:ring-2 focus:ring-indigo-100"
                    autoFocus
                  />
                  <button
                    onClick={handleCreate}
                    className="p-2 text-white bg-indigo-500 hover:bg-indigo-600 rounded-lg transition-colors"
                  >
                    <Check size={16} />
                  </button>
                  <button
                    onClick={() => {
                      setIsCreating(false)
                      setNewName('')
                    }}
                    className="p-2 text-gray-400 hover:text-gray-600 hover:bg-gray-100 rounded-lg transition-colors"
                  >
                    <X size={16} />
                  </button>
                </div>
              </div>
            ) : (
              <button
                onClick={() => setIsCreating(true)}
                className="w-full flex items-center gap-2 px-4 py-3 text-sm text-indigo-600 hover:bg-indigo-50 transition-colors"
              >
                <Plus size={16} />
                <span>创建知识库</span>
              </button>
            )}
          </div>
        )}
      </div>
    )
  }

  return (
    <div ref={dropdownRef} className={`relative ${className}`}>
      {/* 触发按钮 */}
      <button
        onClick={() => setIsOpen(!isOpen)}
        className="flex items-center gap-2 px-3 py-2 bg-white border border-gray-200 rounded-lg hover:border-indigo-300 transition-colors text-sm"
      >
        <Database size={16} className="text-indigo-500" />
        <span className="text-gray-700 max-w-[150px] truncate">
          {activeKb ? activeKb.name : '选择知识库'}
        </span>
        <ChevronDown size={14} className={`text-gray-400 transition-transform ${isOpen ? 'rotate-180' : ''}`} />
      </button>

      {/* 下拉框 - 右对齐防止超出屏幕 */}
      {isOpen && (
        <div className="absolute top-full right-0 mt-1 w-72 bg-white rounded-xl shadow-xl border border-gray-200 z-50 overflow-hidden">
          {/* 知识库列表 */}
          <div className="max-h-60 overflow-y-auto">
            {knowledgeBases.map(kb => (
              <div
                key={kb.id}
                className={`flex items-center gap-2 px-4 py-2.5 hover:bg-gray-50 transition-colors ${
                  activeKnowledgeBaseId === kb.id ? 'bg-indigo-50' : ''
                }`}
              >
                {editingId === kb.id ? (
                  /* 编辑模式 */
                  <div className="flex items-center gap-1 flex-1">
                    <input
                      value={editName}
                      onChange={e => setEditName(e.target.value)}
                      onKeyDown={e => {
                        if (e.key === 'Enter') handleRename(kb.id)
                        if (e.key === 'Escape') setEditingId(null)
                      }}
                      className="flex-1 px-2 py-1 text-sm border border-indigo-300 rounded outline-none focus:ring-2 focus:ring-indigo-100"
                      autoFocus
                    />
                    <button
                      onClick={() => handleRename(kb.id)}
                      className="p-1 text-green-600 hover:bg-green-50 rounded"
                    >
                      <Check size={14} />
                    </button>
                    <button
                      onClick={() => setEditingId(null)}
                      className="p-1 text-gray-400 hover:bg-gray-100 rounded"
                    >
                      <X size={14} />
                    </button>
                  </div>
                ) : (
                  /* 正常显示 */
                  <>
                    <button
                      onClick={() => handleSelect(kb.id)}
                      className="flex-1 flex items-center gap-2 min-w-0"
                    >
                      <Database
                        size={16}
                        className={activeKnowledgeBaseId === kb.id ? 'text-indigo-500' : 'text-gray-400'}
                      />
                      <span className={`truncate text-sm ${
                        activeKnowledgeBaseId === kb.id ? 'text-indigo-700 font-medium' : 'text-gray-700'
                      }`}>
                        {kb.name}
                      </span>
                      <span className="text-xs text-gray-400 flex-shrink-0">
                        {getDocCount(kb.id)}
                      </span>
                    </button>
                    {/* 编辑和删除按钮始终显示 */}
                    <div className="flex items-center gap-0.5">
                      <button
                        onClick={(e) => {
                          e.stopPropagation()
                          setEditingId(kb.id)
                          setEditName(kb.name)
                        }}
                        className="p-1.5 text-gray-400 hover:text-indigo-600 hover:bg-indigo-50 rounded transition-colors"
                        title="重命名"
                      >
                        <Pencil size={14} />
                      </button>
                      <button
                        onClick={(e) => {
                          e.stopPropagation()
                          handleDelete(kb)
                        }}
                        className="p-1.5 text-gray-400 hover:text-red-600 hover:bg-red-50 rounded transition-colors"
                        title="删除"
                      >
                        <Trash2 size={14} />
                      </button>
                    </div>
                  </>
                )}
              </div>
            ))}
          </div>

          {/* 孤立文档提示 */}
          {orphanedCount > 0 && (
            <>
              <div className="border-t border-gray-100" />
              <div className="p-3 bg-amber-50">
                <div className="flex items-start gap-2">
                  <AlertCircle size={16} className="text-amber-500 flex-shrink-0 mt-0.5" />
                  <div className="flex-1 min-w-0">
                    <p className="text-xs text-amber-700 mb-2">
                      有 {orphanedCount} 个文档未分配知识库
                    </p>
                    <div className="flex items-center gap-2">
                      <button
                        onClick={handleMigrateOrphaned}
                        disabled={!activeKnowledgeBaseId}
                        className="flex items-center gap-1 px-2 py-1 text-xs text-indigo-600 bg-white border border-indigo-200 rounded hover:bg-indigo-50 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                      >
                        <ArrowRight size={12} />
                        迁移到当前
                      </button>
                      <button
                        onClick={handleDeleteOrphaned}
                        className="flex items-center gap-1 px-2 py-1 text-xs text-red-600 bg-white border border-red-200 rounded hover:bg-red-50 transition-colors"
                      >
                        <Trash2 size={12} />
                        全部删除
                      </button>
                    </div>
                  </div>
                </div>
              </div>
            </>
          )}

          <div className="border-t border-gray-100" />

          {/* 创建知识库 */}
          {isCreating ? (
            <div className="p-3">
              <div className="flex items-center gap-2">
                <input
                  value={newName}
                  onChange={e => setNewName(e.target.value)}
                  onKeyDown={e => {
                    if (e.key === 'Enter') handleCreate()
                    if (e.key === 'Escape') {
                      setIsCreating(false)
                      setNewName('')
                    }
                  }}
                  placeholder="输入知识库名称"
                  className="flex-1 px-3 py-2 text-sm border border-gray-200 rounded-lg outline-none focus:border-indigo-300 focus:ring-2 focus:ring-indigo-100"
                  autoFocus
                />
                <button
                  onClick={handleCreate}
                  className="p-2 text-white bg-indigo-500 hover:bg-indigo-600 rounded-lg transition-colors"
                >
                  <Check size={16} />
                </button>
                <button
                  onClick={() => {
                    setIsCreating(false)
                    setNewName('')
                  }}
                  className="p-2 text-gray-400 hover:text-gray-600 hover:bg-gray-100 rounded-lg transition-colors"
                >
                  <X size={16} />
                </button>
              </div>
            </div>
          ) : (
            <button
              onClick={() => setIsCreating(true)}
              className="w-full flex items-center gap-2 px-4 py-3 text-sm text-indigo-600 hover:bg-indigo-50 transition-colors"
            >
              <Plus size={16} />
              <span>创建知识库</span>
            </button>
          )}
        </div>
      )}
    </div>
  )
})

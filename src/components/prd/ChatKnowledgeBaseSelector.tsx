/**
 * 对话时的 PRD 知识库控制组件
 * 合并了开关和知识库多选功能
 */

import { memo, useState, useRef, useEffect, useMemo } from 'react'
import { BookOpen, ChevronDown, Check, CheckSquare, Square, Power } from 'lucide-react'
import { usePrdStore } from '../../stores/prdStore'

interface ChatKnowledgeBaseSelectorProps {
  className?: string
  disabled?: boolean
  enabled: boolean
  onToggle: (enabled: boolean) => void
}

export const ChatKnowledgeBaseSelector = memo(function ChatKnowledgeBaseSelector({
  className = '',
  disabled = false,
  enabled,
  onToggle,
}: ChatKnowledgeBaseSelectorProps) {
  const {
    knowledgeBases,
    chatKnowledgeBaseIds,
    toggleChatKnowledgeBase,
    selectAllChatKnowledgeBases,
    documents,
  } = usePrdStore()

  const [isOpen, setIsOpen] = useState(false)
  const dropdownRef = useRef<HTMLDivElement>(null)

  // 点击外部关闭下拉框
  useEffect(() => {
    const handleClickOutside = (e: MouseEvent) => {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target as Node)) {
        setIsOpen(false)
      }
    }
    document.addEventListener('mousedown', handleClickOutside)
    return () => document.removeEventListener('mousedown', handleClickOutside)
  }, [])

  // 计算有效选中的知识库（空数组表示全选）
  const effectiveSelectedIds = useMemo(() => {
    if (chatKnowledgeBaseIds.length === 0) {
      return knowledgeBases.map(kb => kb.id)
    }
    return chatKnowledgeBaseIds
  }, [chatKnowledgeBaseIds, knowledgeBases])

  // 是否全选
  const isAllSelected = effectiveSelectedIds.length === knowledgeBases.length

  // 获取知识库的文档数量
  const getDocCount = (kbId: string) => {
    return documents.filter(doc => doc.knowledgeBaseId === kbId).length
  }

  // 总文档数
  const totalDocCount = documents.filter(doc =>
    doc.knowledgeBaseId && effectiveSelectedIds.includes(doc.knowledgeBaseId)
  ).length

  // 显示文本
  const displayText = useMemo(() => {
    if (!enabled) return 'PRD 知识'
    if (knowledgeBases.length === 0) return 'PRD 知识'
    if (isAllSelected) return 'PRD 知识'
    if (effectiveSelectedIds.length === 1) {
      const kb = knowledgeBases.find(kb => kb.id === effectiveSelectedIds[0])
      return kb ? kb.name : 'PRD 知识'
    }
    return `${effectiveSelectedIds.length} 个知识库`
  }, [enabled, knowledgeBases, isAllSelected, effectiveSelectedIds])

  const hasKnowledgeBases = knowledgeBases.length > 0

  return (
    <div ref={dropdownRef} className={`relative ${className}`}>
      {/* 触发按钮 */}
      <button
        onClick={() => !disabled && setIsOpen(!isOpen)}
        disabled={disabled}
        className={`flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-xs font-medium transition-all ${
          disabled
            ? 'bg-gray-100 text-gray-400 cursor-not-allowed'
            : enabled
              ? 'bg-indigo-50 text-indigo-700 border border-indigo-200 hover:bg-indigo-100'
              : 'bg-gray-50 text-gray-400 border border-gray-200 hover:bg-gray-100 hover:text-gray-600'
        }`}
        title="PRD 知识：自动关联已添加的 PRD 文档中的相关内容"
      >
        <BookOpen size={14} />
        <span className="max-w-[100px] truncate">{displayText}</span>
        {enabled && <span className="w-1.5 h-1.5 rounded-full bg-indigo-500" />}
        <ChevronDown size={12} className={`transition-transform ${isOpen ? 'rotate-180' : ''}`} />
      </button>

      {/* 下拉框 */}
      {isOpen && (
        <div className="absolute bottom-full left-0 mb-1 w-60 bg-white rounded-xl shadow-xl border border-gray-200 z-50 overflow-hidden">
          {/* 开关 */}
          <button
            onClick={() => onToggle(!enabled)}
            className={`w-full flex items-center gap-2 px-3 py-2.5 text-xs transition-colors ${
              enabled
                ? 'bg-indigo-50 text-indigo-700 hover:bg-indigo-100'
                : 'text-gray-600 hover:bg-gray-50'
            }`}
          >
            <Power size={14} className={enabled ? 'text-indigo-500' : 'text-gray-400'} />
            <span className="flex-1 text-left font-medium">
              {enabled ? '已开启' : '已关闭'}
            </span>
            <div className={`w-8 h-4 rounded-full transition-colors ${enabled ? 'bg-indigo-500' : 'bg-gray-300'}`}>
              <div className={`w-3 h-3 rounded-full bg-white shadow-sm transform transition-transform mt-0.5 ${enabled ? 'translate-x-4 ml-0.5' : 'translate-x-0.5'}`} />
            </div>
          </button>

          {/* 知识库选择 - 只有开启且有知识库时显示 */}
          {enabled && hasKnowledgeBases && (
            <>
              <div className="border-t border-gray-100" />

              <div className="px-3 py-2 text-[10px] text-gray-400 uppercase tracking-wide">
                引用知识库
              </div>

              {/* 全选按钮 */}
              <button
                onClick={() => selectAllChatKnowledgeBases()}
                className={`w-full flex items-center gap-2 px-3 py-2 text-xs hover:bg-gray-50 transition-colors ${
                  isAllSelected ? 'text-indigo-700' : 'text-gray-700'
                }`}
              >
                {isAllSelected ? (
                  <CheckSquare size={14} className="text-indigo-500" />
                ) : (
                  <Square size={14} className="text-gray-400" />
                )}
                <span className="flex-1 text-left">全部</span>
                <span className="text-gray-400">{documents.length}</span>
              </button>

              {/* 知识库列表 */}
              <div className="max-h-40 overflow-y-auto border-t border-gray-50">
                {knowledgeBases.map(kb => {
                  const isSelected = effectiveSelectedIds.includes(kb.id)
                  const docCount = getDocCount(kb.id)
                  return (
                    <button
                      key={kb.id}
                      onClick={() => toggleChatKnowledgeBase(kb.id)}
                      className={`w-full flex items-center gap-2 px-3 py-2 text-xs hover:bg-gray-50 transition-colors ${
                        isSelected ? 'text-indigo-700' : 'text-gray-600'
                      }`}
                    >
                      {isSelected ? (
                        <Check size={14} className="text-indigo-500" />
                      ) : (
                        <div className="w-3.5 h-3.5" />
                      )}
                      <span className="flex-1 text-left truncate">{kb.name}</span>
                      <span className="text-gray-400">{docCount}</span>
                    </button>
                  )
                })}
              </div>
            </>
          )}

          {/* 无知识库提示 */}
          {enabled && !hasKnowledgeBases && (
            <>
              <div className="border-t border-gray-100" />
              <div className="px-3 py-3 text-xs text-gray-400 text-center">
                暂无知识库，请先在 PRD 知识库中创建
              </div>
            </>
          )}
        </div>
      )}
    </div>
  )
})

import { useState } from 'react'
import { X, FileText, Pencil, Check, FolderSync } from 'lucide-react'
import { PrdOutlinePanel } from './PrdOutlinePanel'
import { useGuidedPrdStore } from '../../stores/guidedPrdStore'
import { useChatStore } from '../../store'

interface RightPanelProps {
  conversationId: string
  onClose: () => void
}

export function RightPanel({ conversationId, onClose }: RightPanelProps) {
  const session = useGuidedPrdStore((s) => s.sessions[conversationId])
  const updateWriteTarget = useGuidedPrdStore((s) => s.updateWriteTargetDescription)
  const importFeaturesFromDoc = useGuidedPrdStore((s) => s.importFeaturesFromDoc)
  const sendMessage = useChatStore((s) => s.sendMessage)

  const [editingTarget, setEditingTarget] = useState(false)
  const [editValue, setEditValue] = useState('')
  const [isImporting, setIsImporting] = useState(false)

  const writeTarget = session?.writeTarget

  const startEdit = () => {
    setEditValue(writeTarget?.description ?? '')
    setEditingTarget(true)
  }

  const commitEdit = () => {
    if (editValue.trim()) {
      updateWriteTarget(conversationId, editValue.trim())
    }
    setEditingTarget(false)
  }

  // 从文档导入目录结构
  const handleImportFromDoc = async () => {
    if (!writeTarget?.docId || writeTarget.mode !== 'existing_doc') return

    setIsImporting(true)
    try {
      // 发送消息让 AI 读取文档结构并导入
      await sendMessage(
        `请读取文档 ${writeTarget.docId} 的目录结构，然后告诉我有哪些章节。我想基于现有结构进行更新。`,
        undefined,
        false,
        undefined,
        conversationId,
        true,
        true
      )
    } finally {
      setIsImporting(false)
    }
  }

  return (
    <div className="flex flex-col h-full border-l border-gray-200 bg-white">
      {/* 面板头部 */}
      <div className="h-12 flex items-center justify-between px-4 border-b border-gray-200 flex-shrink-0">
        <div className="flex items-center gap-2 text-sm font-medium text-gray-700">
          <FileText size={15} className="text-violet-500" />
          PRD 助手
        </div>
        <button
          onClick={onClose}
          className="p-1 text-gray-400 hover:text-gray-600 rounded hover:bg-gray-100 transition-colors"
        >
          <X size={15} />
        </button>
      </div>

      {/* 写入目标状态行 */}
      <div className={`px-4 py-2.5 border-b flex items-center gap-2 flex-shrink-0 ${
        writeTarget ? 'bg-violet-50 border-violet-100' : 'bg-gray-50 border-gray-100'
      }`}>
        <span className="text-xs flex-shrink-0" title="写入目标">
          {writeTarget ? '📄' : '⏳'}
        </span>

        {editingTarget ? (
          <div className="flex items-center gap-1.5 flex-1 min-w-0">
            <input
              autoFocus
              value={editValue}
              onChange={(e) => setEditValue(e.target.value)}
              onBlur={commitEdit}
              onKeyDown={(e) => e.key === 'Enter' && commitEdit()}
              className="flex-1 min-w-0 text-xs border border-violet-300 rounded px-2 py-0.5 outline-none focus:ring-1 focus:ring-violet-300 bg-white"
            />
            <button
              onClick={commitEdit}
              className="p-0.5 text-violet-500 hover:text-violet-700 flex-shrink-0"
            >
              <Check size={13} />
            </button>
          </div>
        ) : (
          <>
            <span className={`text-xs flex-1 min-w-0 truncate ${
              writeTarget ? 'text-violet-700' : 'text-gray-400 italic'
            }`}>
              {writeTarget ? writeTarget.description : '写入目标待确定…'}
            </span>
            <button
              onClick={startEdit}
              className="p-0.5 text-gray-300 hover:text-violet-500 flex-shrink-0 transition-colors"
              title="修改写入目标"
            >
              <Pencil size={11} />
            </button>
            {/* 同步文档结构按钮：仅在有 existing_doc 目标时显示 */}
            {writeTarget?.mode === 'existing_doc' && writeTarget.docId && (
              <button
                onClick={handleImportFromDoc}
                disabled={isImporting}
                className="p-0.5 text-gray-300 hover:text-blue-500 flex-shrink-0 transition-colors disabled:opacity-50"
                title="读取文档目录结构，同步到右侧面板"
              >
                <FolderSync size={11} />
              </button>
            )}
          </>
        )}
      </div>

      {/* 合并后的大纲面板（含进度 + 摘要编辑） */}
      <PrdOutlinePanel conversationId={conversationId} />
    </div>
  )
}

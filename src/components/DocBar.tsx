import { useState, memo } from 'react'
import { FileText, Plus, X, HelpCircle } from 'lucide-react'
import { useChatStore } from '../store'

interface Props {
  conversationId: string
  docIds: string[]
}

export const DocBar = memo(function DocBar({ conversationId, docIds }: Props) {
  const addDoc = useChatStore(s => s.addDocToConversation)
  const removeDoc = useChatStore(s => s.removeDocFromConversation)
  const [showInput, setShowInput] = useState(false)
  const [inputValue, setInputValue] = useState('')
  const [showTip, setShowTip] = useState(false)

  const handleAdd = () => {
    const trimmed = inputValue.trim()
    if (!trimmed) return
    addDoc(conversationId, trimmed)
    setInputValue('')
    setShowInput(false)
  }

  return (
    <div className="px-4 py-2 border-b border-gray-100 bg-gray-50/70 flex-shrink-0">
      <div className="max-w-3xl mx-auto flex items-center gap-2 flex-wrap">
        <div className="flex items-center gap-1.5 text-xs text-gray-500 mr-1 relative">
          <FileText size={13} className="text-indigo-500" />
          <span className="font-medium">参考文档</span>
          <button
            className="text-gray-400 hover:text-gray-600 transition-colors"
            onMouseEnter={() => setShowTip(true)}
            onMouseLeave={() => setShowTip(false)}
          >
            <HelpCircle size={12} />
          </button>
          {showTip && (
            <div className="absolute left-0 top-full mt-1 z-50 w-48 p-2 bg-gray-800 text-white text-[11px] rounded-lg shadow-lg leading-relaxed">
              添加 KM 文档后，发送消息时会自动检索相关内容作为参考
            </div>
          )}
        </div>

        {docIds.map(docId => (
          <span
            key={docId}
            className="inline-flex items-center gap-1 px-2.5 py-1 rounded-md bg-indigo-50 border border-indigo-200 text-xs text-indigo-700 font-mono"
          >
            {docId.length > 12 ? docId.slice(0, 6) + '...' + docId.slice(-4) : docId}
            <button
              onClick={() => removeDoc(conversationId, docId)}
              className="ml-0.5 p-0.5 rounded hover:bg-indigo-200 transition-colors"
            >
              <X size={11} />
            </button>
          </span>
        ))}

        {showInput ? (
          <div className="inline-flex items-center gap-1">
            <input
              value={inputValue}
              onChange={e => setInputValue(e.target.value)}
              onKeyDown={e => {
                if (e.key === 'Enter') handleAdd()
                if (e.key === 'Escape') { setShowInput(false); setInputValue('') }
              }}
              placeholder="粘贴 KM 文档链接或 ID"
              autoFocus
              className="w-48 px-2 py-1 rounded-md border border-gray-300 text-xs outline-none focus:border-indigo-400 bg-white"
            />
            <button
              onClick={handleAdd}
              disabled={!inputValue.trim()}
              className="px-2 py-1 rounded-md bg-indigo-600 text-white text-xs hover:bg-indigo-700 disabled:bg-gray-300 transition-colors"
            >
              添加
            </button>
            <button
              onClick={() => { setShowInput(false); setInputValue('') }}
              className="p-1 rounded-md hover:bg-gray-200 text-gray-400 transition-colors"
            >
              <X size={13} />
            </button>
          </div>
        ) : (
          <button
            onClick={() => setShowInput(true)}
            className="inline-flex items-center gap-1 px-2 py-1 rounded-md border border-dashed border-gray-300 text-xs text-gray-500 hover:border-indigo-400 hover:text-indigo-600 hover:bg-indigo-50/50 transition-all"
          >
            <Plus size={12} />
            添加文档
          </button>
        )}
      </div>
    </div>
  )
})

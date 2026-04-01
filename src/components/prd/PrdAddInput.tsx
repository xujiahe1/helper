import { useState, useCallback } from 'react'
import { Link2, Plus, X } from 'lucide-react'
import { parseDocIdFromUrl } from '../../services/prdService'
import { showToast } from '../Toast'

interface PrdAddInputProps {
  onAdd: (docUrl: string, docId: string) => void
  onBatchAdd?: (items: Array<{ docUrl: string; docId: string }>) => void
  disabled?: boolean
}

export function PrdAddInput({ onAdd, onBatchAdd, disabled }: PrdAddInputProps) {
  const [inputValue, setInputValue] = useState('')
  const [isFocused, setIsFocused] = useState(false)
  const [isExpanded, setIsExpanded] = useState(false)

  // 解析输入中的所有文档链接
  const parseLinks = useCallback((text: string) => {
    const lines = text.split(/[\n,;]/).map(l => l.trim()).filter(Boolean)
    const results: Array<{ url: string; docId: string }> = []

    for (const line of lines) {
      // 尝试从每行提取 URL
      const urlMatch = line.match(/https?:\/\/[^\s]+/)
      const url = urlMatch ? urlMatch[0] : line
      const docId = parseDocIdFromUrl(url)
      if (docId && !results.find(r => r.docId === docId)) {
        results.push({ url, docId })
      }
    }

    return results
  }, [])

  const parsedLinks = parseLinks(inputValue)

  const handleSubmit = useCallback(() => {
    if (parsedLinks.length === 0) {
      showToast('未识别到有效的文档链接', 'error')
      return
    }

    // 批量添加时优先使用 onBatchAdd（串行处理）
    if (parsedLinks.length > 1 && onBatchAdd) {
      onBatchAdd(parsedLinks.map(({ url, docId }) => ({ docUrl: url, docId })))
    } else {
      // 单个添加
      for (const { url, docId } of parsedLinks) {
        onAdd(url, docId)
      }
      if (parsedLinks.length > 1) {
        showToast(`已添加 ${parsedLinks.length} 个文档`, 'success')
      }
    }

    setInputValue('')
    setIsExpanded(false)
  }, [parsedLinks, onAdd, onBatchAdd])

  const handleKeyDown = useCallback((e: React.KeyboardEvent) => {
    // Cmd/Ctrl + Enter 提交
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
      e.preventDefault()
      handleSubmit()
    }
    // 单行模式下 Enter 提交
    if (e.key === 'Enter' && !e.shiftKey && !isExpanded) {
      e.preventDefault()
      handleSubmit()
    }
  }, [handleSubmit, isExpanded])

  const handlePaste = useCallback((e: React.ClipboardEvent) => {
    if (disabled) return

    const text = e.clipboardData.getData('text')
    if (!text) return

    // 检测是否包含多个链接
    const links = parseLinks(text)
    if (links.length > 1) {
      e.preventDefault()
      setInputValue(text)
      setIsExpanded(true)
    } else if (links.length === 1) {
      // 单个链接，直接添加
      e.preventDefault()
      onAdd(links[0].url, links[0].docId)
      setInputValue('')
    }
  }, [disabled, parseLinks, onAdd])

  return (
    <div
      className={`
        flex flex-col gap-2 px-4 py-3 rounded-xl border bg-white
        transition-all duration-200
        ${isFocused ? 'border-indigo-300 ring-2 ring-indigo-100' : 'border-gray-200 hover:border-gray-300'}
        ${disabled ? 'opacity-50 pointer-events-none' : ''}
      `}
    >
      <div className="flex items-start gap-2">
        <Link2 size={18} className="text-gray-400 flex-shrink-0 mt-0.5" />

        {isExpanded ? (
          <textarea
            value={inputValue}
            onChange={e => setInputValue(e.target.value)}
            onKeyDown={handleKeyDown}
            onFocus={() => setIsFocused(true)}
            onBlur={() => setIsFocused(false)}
            placeholder="粘贴多个 KM 文档链接，每行一个..."
            className="flex-1 bg-transparent outline-none text-sm text-gray-700 placeholder-gray-400 resize-none min-h-[80px]"
            disabled={disabled}
            autoFocus
          />
        ) : (
          <input
            type="text"
            value={inputValue}
            onChange={e => setInputValue(e.target.value)}
            onKeyDown={handleKeyDown}
            onPaste={handlePaste}
            onFocus={() => setIsFocused(true)}
            onBlur={() => setIsFocused(false)}
            placeholder="粘贴 KM 文档链接，支持批量粘贴多个..."
            className="flex-1 bg-transparent outline-none text-sm text-gray-700 placeholder-gray-400"
            disabled={disabled}
          />
        )}

        <div className="flex items-center gap-1 flex-shrink-0">
          {!isExpanded && (
            <button
              onClick={() => setIsExpanded(true)}
              className="p-1.5 rounded-lg text-gray-400 hover:text-gray-600 hover:bg-gray-100 transition-colors"
              title="展开批量添加"
            >
              <Plus size={16} />
            </button>
          )}

          {isExpanded && (
            <button
              onClick={() => { setIsExpanded(false); setInputValue('') }}
              className="p-1.5 rounded-lg text-gray-400 hover:text-gray-600 hover:bg-gray-100 transition-colors"
              title="收起"
            >
              <X size={16} />
            </button>
          )}
        </div>
      </div>

      {/* 识别到的链接预览 */}
      {isExpanded && parsedLinks.length > 0 && (
        <div className="flex items-center justify-between pt-2 border-t border-gray-100">
          <span className="text-xs text-gray-500">
            识别到 {parsedLinks.length} 个文档链接
          </span>
          <button
            onClick={handleSubmit}
            disabled={disabled || parsedLinks.length === 0}
            className={`
              flex items-center gap-1 px-3 py-1.5 rounded-lg text-sm font-medium
              transition-all duration-150
              bg-indigo-500 text-white hover:bg-indigo-600
            `}
          >
            <Plus size={16} />
            <span>全部添加</span>
          </button>
        </div>
      )}

      {/* 单行模式下的添加按钮 */}
      {!isExpanded && inputValue.trim() && (
        <div className="flex justify-end">
          <button
            onClick={handleSubmit}
            disabled={disabled || parsedLinks.length === 0}
            className={`
              flex items-center gap-1 px-3 py-1.5 rounded-lg text-sm font-medium
              transition-all duration-150
              ${parsedLinks.length > 0
                ? 'bg-indigo-500 text-white hover:bg-indigo-600'
                : 'bg-gray-100 text-gray-400 cursor-not-allowed'
              }
            `}
          >
            <Plus size={16} />
            <span>添加{parsedLinks.length > 1 ? ` ${parsedLinks.length} 个` : ''}</span>
          </button>
        </div>
      )}
    </div>
  )
}

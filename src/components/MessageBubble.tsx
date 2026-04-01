import { useState, useCallback, useEffect, useRef, memo, type ReactNode } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import {
  Copy, Check, User, Bot, FileText, ImageIcon,
  ChevronDown, ChevronRight, Brain, Download,
  RefreshCw, ClipboardCopy, X, Loader2,
  FileSpreadsheet, Pencil, RotateCcw, AlertTriangle,
} from 'lucide-react'
import type { Message, SheetData } from '../types'
import { ExcelPreview } from './ExcelPreview'
import { exportToXlsx, exportToCSVBlob } from '../services/excel'
import { useChatStore } from '../store'
import { MermaidBlock } from './MermaidBlock'
import { PrdCardRenderer } from './prd/PrdCardRenderer'

function CodeBlock({ className, children }: { className?: string; children?: ReactNode }) {
  const match = /language-(\w+)/.exec(className || '')
  const codeString = String(children).replace(/\n$/, '')
  const isBlock = match || codeString.includes('\n')

  const lang = (match?.[1] || '').toLowerCase()
  const looksLikeMermaid =
    /^\s*(flowchart|sequenceDiagram|classDiagram|stateDiagram|stateDiagram-v2|erDiagram|journey|gantt|pie|mindmap|timeline|quadrantChart|gitGraph|graph)\b/.test(codeString.trim())

  if (lang === 'mermaid' || (!lang && isBlock && looksLikeMermaid)) {
    return <MermaidBlock code={codeString} />
  }

  if (isBlock) {
    return (
      <div className="relative my-3 rounded-lg overflow-hidden">
        <div className="flex items-center justify-between bg-slate-800 text-slate-300 px-4 py-2 text-xs">
          <span>{match?.[1] || 'code'}</span>
          <button
            onClick={() => navigator.clipboard.writeText(codeString)}
            className="hover:text-white transition-colors"
          >
            复制
          </button>
        </div>
        <pre className="bg-slate-900 text-slate-100 p-4 overflow-x-auto text-[13px] leading-relaxed">
          <code>{children}</code>
        </pre>
      </div>
    )
  }

  return (
    <code className="px-1.5 py-0.5 rounded bg-gray-100 text-indigo-700 text-[13px] font-mono">
      {children}
    </code>
  )
}

function ThinkingBlock({ content, isStreaming }: { content: string; isStreaming?: boolean }) {
  const [userToggled, setUserToggled] = useState(false)
  const [userExpanded, setUserExpanded] = useState(false)

  const expanded = userToggled ? userExpanded : !!isStreaming

  const toggle = () => {
    setUserToggled(true)
    setUserExpanded(!expanded)
  }

  return (
    <div className="mb-3 rounded-lg border border-amber-200 bg-amber-50/50 overflow-hidden">
      <button
        onClick={toggle}
        className="w-full flex items-center gap-2 px-3 py-2 text-xs font-medium text-amber-700 hover:bg-amber-100/50 transition-colors"
      >
        <Brain size={13} />
        <span>{isStreaming ? '思考中…' : '思考过程'}</span>
        {isStreaming && <span className="typing-cursor" />}
        <span className="ml-auto">
          {expanded ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
        </span>
      </button>
      {expanded && (
        <div className="px-3 pb-3 text-xs text-amber-800/80 leading-relaxed whitespace-pre-wrap border-t border-amber-200/50 pt-2 max-h-80 overflow-y-auto">
          {content}
        </div>
      )}
    </div>
  )
}

function AttachmentPreview({ attachments }: { attachments: Message['attachments'] }) {
  if (!attachments?.length) return null

  const excelAtts = attachments.filter(a => a.parsedSheets?.length)
  const otherAtts = attachments.filter(a => !a.parsedSheets?.length)

  return (
    <div className="mb-2">
      {otherAtts.length > 0 && (
        <div className="flex gap-2 flex-wrap mb-1">
          {otherAtts.map(att => (
            <div key={att.id}>
              {att.type === 'image' && att.dataUrl ? (
                <img
                  src={att.dataUrl}
                  alt={att.name}
                  className="max-h-48 max-w-xs rounded-lg object-contain"
                />
              ) : att.type === 'image' && !att.dataUrl ? (
                <div className="flex items-center gap-1.5 px-2 py-1 rounded-md bg-white/20 text-xs">
                  <ImageIcon size={12} />
                  <span>{att.name}</span>
                </div>
              ) : (
                <div className="flex items-center gap-1.5 px-2 py-1 rounded-md bg-white/20 text-xs">
                  <FileText size={12} />
                  <span>{att.name}</span>
                </div>
              )}
            </div>
          ))}
        </div>
      )}
      {excelAtts.map(att => (
        <ExcelPreview key={att.id} sheets={att.parsedSheets!} fileName={att.name} />
      ))}
    </div>
  )
}

function ImageLightbox({ src, onClose }: { src: string; onClose: () => void }) {
  useEffect(() => {
    const handler = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', handler)
    return () => document.removeEventListener('keydown', handler)
  }, [onClose])

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 backdrop-blur-sm"
      onClick={onClose}
    >
      <button
        onClick={onClose}
        className="absolute top-4 right-4 p-2 rounded-full bg-white/10 hover:bg-white/20 text-white transition-colors"
      >
        <X size={20} />
      </button>
      <img
        src={src}
        alt="preview"
        className="max-w-[90vw] max-h-[90vh] object-contain rounded-lg shadow-2xl"
        onClick={e => e.stopPropagation()}
      />
    </div>
  )
}

function GeneratedImages({ attachments }: { attachments: Message['attachments'] }) {
  const [lightboxSrc, setLightboxSrc] = useState<string | null>(null)
  const [copiedId, setCopiedId] = useState<string | null>(null)

  const images = attachments?.filter(a => a.type === 'image')
  if (!images?.length) return null

  const handleCopyImage = async (dataUrl: string, id: string) => {
    try {
      const res = await fetch(dataUrl)
      const blob = await res.blob()
      await navigator.clipboard.write([new ClipboardItem({ [blob.type]: blob })])
      setCopiedId(id)
      setTimeout(() => setCopiedId(null), 2000)
    } catch {
      await navigator.clipboard.writeText(dataUrl)
      setCopiedId(id)
      setTimeout(() => setCopiedId(null), 2000)
    }
  }

  return (
    <>
      {lightboxSrc && <ImageLightbox src={lightboxSrc} onClose={() => setLightboxSrc(null)} />}
      <div className="flex flex-col gap-3 my-3">
        {images.map(img => (
          img.dataUrl ? (
            <div key={img.id} className="relative group inline-block">
              <img
                src={img.dataUrl}
                alt={img.name}
                className="max-w-full rounded-xl border border-gray-200 shadow-sm cursor-zoom-in hover:shadow-md transition-shadow"
                onClick={() => setLightboxSrc(img.dataUrl)}
              />
              <div className="absolute top-2 right-2 flex gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
                <button
                  onClick={() => handleCopyImage(img.dataUrl, img.id)}
                  className="bg-black/50 hover:bg-black/70 text-white p-1.5 rounded-lg transition-colors"
                  title="复制图片"
                >
                  {copiedId === img.id ? <Check size={14} /> : <ClipboardCopy size={14} />}
                </button>
                <a
                  href={img.dataUrl}
                  download={img.name}
                  className="bg-black/50 hover:bg-black/70 text-white p-1.5 rounded-lg transition-colors"
                  title="下载图片"
                >
                  <Download size={14} />
                </a>
              </div>
            </div>
          ) : (
            <div key={img.id} className="flex items-center gap-2 px-4 py-3 rounded-xl border border-gray-200 bg-gray-50 text-gray-400 text-sm">
              <ImageIcon size={18} />
              <span>图片已过期（刷新后无法保留）</span>
            </div>
          )
        ))}
      </div>
    </>
  )
}

function ProcessedDataBlock({ sheets, fileName, messageId, hasOpsRaw }: { sheets: SheetData[]; fileName?: string; messageId: string; hasOpsRaw?: boolean }) {
  const displayName = fileName || '处理结果'
  const reExecuteExcelOps = useChatStore(s => s.reExecuteExcelOps)
  const handleDownload = useCallback((format: 'xlsx' | 'csv') => {
    try {
      console.log('[Excel-Download] 开始导出', { format, sheetsCount: sheets.length, sheets })
      if (!sheets.length) {
        console.warn('[Excel-Download] 没有数据可导出')
        return
      }
      const blob = format === 'xlsx'
        ? exportToXlsx(sheets)
        : exportToCSVBlob(sheets[0])
      console.log('[Excel-Download] Blob 创建成功', { size: blob.size, type: blob.type })
      const ext = format === 'xlsx' ? '.xlsx' : '.csv'
      const a = document.createElement('a')
      a.href = URL.createObjectURL(blob)
      a.download = displayName + ext
      console.log('[Excel-Download] 触发下载', { fileName: a.download })
      a.click()
      URL.revokeObjectURL(a.href)
    } catch (error) {
      console.error('[Excel-Download] 导出失败:', error)
      alert(`导出失败: ${error instanceof Error ? error.message : '未知错误'}`)
    }
  }, [sheets, displayName])

  if (!sheets.length) return null

  return (
    <div className="my-3">
      <ExcelPreview sheets={sheets} fileName={displayName} />
      <div className="flex items-center gap-2 mt-2 flex-wrap">
        <button
          onClick={() => handleDownload('xlsx')}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium bg-emerald-50 text-emerald-700 hover:bg-emerald-100 transition-colors border border-emerald-200"
        >
          <FileSpreadsheet size={13} />
          下载 Excel
        </button>
        <button
          onClick={() => handleDownload('csv')}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium bg-blue-50 text-blue-700 hover:bg-blue-100 transition-colors border border-blue-200"
        >
          <Download size={13} />
          下载 CSV
        </button>
        {hasOpsRaw && (
          <button
            onClick={() => reExecuteExcelOps(messageId, true)}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium bg-amber-50 text-amber-700 hover:bg-amber-100 transition-colors border border-amber-200"
            title="忽略链式结果，从用户上传的原始数据重新执行此操作"
          >
            <RotateCcw size={13} />
            从原始数据重算
          </button>
        )}
        <span className="text-xs text-gray-400">
          {sheets[0].rows.length} 行 × {sheets[0].headers.length} 列
        </span>
      </div>
    </div>
  )
}

const markdownComponents = {
  code: CodeBlock as any,
  pre: ({ children }: { children?: ReactNode }) => <>{children}</>,
}

function HighlightedText({ text, keyword }: { text: string; keyword: string }) {
  if (!keyword) return <>{text}</>
  const parts: { text: string; match: boolean }[] = []
  const lower = text.toLowerCase()
  const kw = keyword.toLowerCase()
  let pos = 0
  while (pos < text.length) {
    const idx = lower.indexOf(kw, pos)
    if (idx < 0) {
      parts.push({ text: text.slice(pos), match: false })
      break
    }
    if (idx > pos) parts.push({ text: text.slice(pos, idx), match: false })
    parts.push({ text: text.slice(idx, idx + kw.length), match: true })
    pos = idx + kw.length
  }
  return (
    <>
      {parts.map((p, i) =>
        p.match ? <mark key={i} className="bg-yellow-200 text-inherit rounded-sm px-0.5">{p.text}</mark> : p.text
      )}
    </>
  )
}

interface Props {
  message: Message
  isStreaming?: boolean
  isImageGen?: boolean
  highlighted?: boolean
  highlightKeyword?: string | null
  onRetry?: (messageId: string) => void
  onEdit?: (messageId: string) => void
  conversationId?: string
}

export const MessageBubble = memo(function MessageBubble({ message, isStreaming, isImageGen, highlighted, highlightKeyword, onRetry, onEdit, conversationId }: Props) {
  const [copied, setCopied] = useState(false)
  const bubbleRef = useRef<HTMLDivElement>(null)
  const markdownRef = useRef<HTMLDivElement>(null)
  const isUser = message.role === 'user'

  useEffect(() => {
    if (highlighted && bubbleRef.current) {
      bubbleRef.current.scrollIntoView({ behavior: 'smooth', block: 'center' })
    }
  }, [highlighted])

  useEffect(() => {
    if (!markdownRef.current || !highlightKeyword || isUser) return
    const container = markdownRef.current
    const kw = highlightKeyword.toLowerCase()
    const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT)
    const ranges: Range[] = []
    let node: Text | null
    while ((node = walker.nextNode() as Text | null)) {
      const text = node.textContent?.toLowerCase() || ''
      let idx = text.indexOf(kw)
      while (idx >= 0) {
        const range = document.createRange()
        range.setStart(node, idx)
        range.setEnd(node, idx + kw.length)
        ranges.push(range)
        idx = text.indexOf(kw, idx + kw.length)
      }
    }
    if (ranges.length === 0) return
    for (const range of ranges) {
      const mark = document.createElement('mark')
      mark.className = 'bg-yellow-200 rounded-sm px-0.5'
      range.surroundContents(mark)
    }
    return () => {
      container.querySelectorAll('mark.bg-yellow-200').forEach(el => {
        const parent = el.parentNode
        if (parent) {
          parent.replaceChild(document.createTextNode(el.textContent || ''), el)
          parent.normalize()
        }
      })
    }
  }, [highlightKeyword, isUser, message.content])
  const hasThinking = !!message.thinking
  const isThinkingPhase = isStreaming && hasThinking && !message.content
  const isFailed = !isUser && message.content.startsWith('\u26a0\ufe0f')

  const handleCopy = () => {
    navigator.clipboard.writeText(message.content)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  return (
    <div ref={bubbleRef} className={`flex gap-3 mb-6 ${isUser ? 'justify-end' : ''} ${highlighted ? 'animate-highlight-fade rounded-xl ring-2 ring-indigo-400 ring-offset-2' : ''}`}>
      {!isUser && (
        <div className="w-8 h-8 rounded-lg bg-indigo-100 flex items-center justify-center flex-shrink-0 mt-1">
          <Bot size={16} className="text-indigo-600" />
        </div>
      )}
      <div className={`flex-1 max-w-[85%] ${isUser ? 'flex justify-end' : ''}`}>
        <div
          className={`relative group ${
            isUser
              ? 'bg-indigo-600 text-white rounded-2xl rounded-br-md px-4 py-2.5 inline-block max-w-full'
              : ''
          }`}
        >
          {isUser ? (
            <>
              <AttachmentPreview attachments={message.attachments} />
              <p className="text-sm whitespace-pre-wrap leading-relaxed">
                {highlightKeyword
                  ? <HighlightedText text={message.content} keyword={highlightKeyword} />
                  : message.content}
              </p>
              {onEdit && !isStreaming && (
                <div className="flex justify-end mt-1 opacity-0 group-hover:opacity-100 transition-opacity">
                  <button
                    onClick={() => onEdit(message.id)}
                    className="p-1 rounded hover:bg-white/20 transition-colors"
                    title="编辑消息"
                  >
                    <Pencil size={12} className="text-white/70" />
                  </button>
                </div>
              )}
            </>
          ) : (
            <div ref={markdownRef} className="markdown-body text-sm text-gray-800">
              {hasThinking && !isImageGen && (
                <ThinkingBlock content={message.thinking!} isStreaming={isThinkingPhase} />
              )}
              <GeneratedImages attachments={message.attachments} />
              {message.content ? (
                <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
                  {message.content}
                </ReactMarkdown>
              ) : isStreaming ? (
                isImageGen ? (
                  <div className="flex items-center gap-2 py-2 text-violet-500">
                    <Loader2 size={16} className="animate-spin" />
                    <span className="text-sm">{message.thinking || '正在生成图片…'}</span>
                  </div>
                ) : !hasThinking ? (
                  <span className="typing-cursor text-gray-400">思考中</span>
                ) : null
              ) : null}
              {message.prdCard && conversationId && (
                <div className="mt-2">
                  <PrdCardRenderer
                    card={message.prdCard}
                    isDone={message.prdCardDone ?? false}
                    msgId={message.id}
                    conversationId={conversationId}
                  />
                </div>
              )}
              {message.processedSheets?.length ? (
                <ProcessedDataBlock
                  sheets={message.processedSheets}
                  fileName={message.content.replace(/[\\/:*?"<>|\n]/g, '').slice(0, 30).trim() || undefined}
                  messageId={message.id}
                  hasOpsRaw={!!message.excelOpsRaw}
                />
              ) : null}
              {message.excelError && (
                <div className="my-3 flex items-start gap-2 px-3 py-2 rounded-lg bg-red-50 border border-red-200 text-red-700 text-sm">
                  <AlertTriangle size={15} className="mt-0.5 flex-shrink-0" />
                  <span>{message.excelError}</span>
                </div>
              )}
            </div>
          )}

          {/* Action bar */}
          {!isUser && !isStreaming && (message.content || message.attachments?.length || message.thinking) && (
            <div className="flex items-center gap-1 mt-2 opacity-0 group-hover:opacity-100 transition-opacity">
              {message.content && (
                <button
                  onClick={handleCopy}
                  className="p-1 rounded hover:bg-gray-100 transition-colors"
                  title="复制文本"
                >
                  {copied ? (
                    <Check size={14} className="text-green-500" />
                  ) : (
                    <Copy size={14} className="text-gray-400" />
                  )}
                </button>
              )}
              {onRetry && (
                <button
                  onClick={() => onRetry(message.id)}
                  className={`flex items-center gap-1 px-2 py-1 rounded-md text-xs font-medium transition-colors ${
                    isFailed
                      ? 'text-orange-600 hover:bg-orange-50'
                      : 'text-gray-400 hover:bg-gray-100 hover:text-gray-600'
                  }`}
                  title={isFailed ? '重试' : '重新生成'}
                >
                  <RefreshCw size={13} />
                  <span>{isFailed ? '重试' : '重新生成'}</span>
                </button>
              )}
            </div>
          )}
        </div>
      </div>
      {isUser && (
        <div className="w-8 h-8 rounded-lg bg-gray-200 flex items-center justify-center flex-shrink-0 mt-1">
          <User size={16} className="text-gray-600" />
        </div>
      )}
    </div>
  )
}, (prev, next) =>
  prev.message === next.message
  && prev.isStreaming === next.isStreaming
  && prev.isImageGen === next.isImageGen
  && prev.highlighted === next.highlighted
  && prev.highlightKeyword === next.highlightKeyword
  && prev.onRetry === next.onRetry
  && prev.onEdit === next.onEdit
)

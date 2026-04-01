import { useRef, useEffect, useState, useCallback, useMemo } from 'react'
import {
  Send,
  PanelLeftOpen,
  ChevronDown,
  Square,
  Sparkles,
  Paperclip,
  X as XIcon,
  FileText,
  Brain,
  Table2,
  Download,
  Pencil,
  Wrench,
} from 'lucide-react'
import { v4 as uuidv4 } from 'uuid'
import { useChatStore } from '../store'
import { MessageBubble } from './MessageBubble'
import { DocBar } from './DocBar'
import { Dropdown } from './Dropdown'
import { showToast } from './Toast'
import { ChatKnowledgeBaseSelector } from './prd/ChatKnowledgeBaseSelector'
import { RightPanel } from './prd/RightPanel'
import { useGuidedPrdStore } from '../stores/guidedPrdStore'
import { useGuidedPrd } from '../hooks/useGuidedPrd'
import type { Attachment } from '../types'
import { ASPECT_RATIOS, IMAGE_SIZES, isExcelFile, PRESET_ROLES } from '../types'
import { isImageModel } from '../services/llm'
import { parseExcelFromFile } from '../services/excel'
import { SourceIndicator } from './SourceIndicator'

const MAX_FILE_SIZE = 50 * 1024 * 1024
const ACCEPT_TYPES = 'image/jpeg,image/png,image/gif,image/webp,application/pdf,.xlsx,.xls,.csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,application/vnd.ms-excel,text/csv'
const SCROLL_THRESHOLD = 100

interface ChatAreaProps {
  paneConversationId?: string
  isSplitPane?: boolean
  onClose?: () => void
}

const MAX_TOKENS = 200_000

export function ChatArea({ paneConversationId, isSplitPane, onClose }: ChatAreaProps = {}) {
  const conversations = useChatStore(s => s.conversations)
  const activeConversationId = useChatStore(s => s.activeConversationId)
  const settings = useChatStore(s => s.settings)
  const streamingIds = useChatStore(s => s.streamingIds)
  const sidebarOpen = useChatStore(s => s.sidebarOpen)
  const sendMessage = useChatStore(s => s.sendMessage)
  const editMessage = useChatStore(s => s.editMessage)
  const retryMessage = useChatStore(s => s.retryMessage)
  const stopStreaming = useChatStore(s => s.stopStreaming)
  const setConversationModel = useChatStore(s => s.setConversationModel)
  const toggleSidebar = useChatStore(s => s.toggleSidebar)
  const createConversation = useChatStore(s => s.createConversation)
  const setConversationPrompt = useChatStore(s => s.setConversationPrompt)
  const highlightMessageId = useChatStore(s => s.highlightMessageId)
  const setHighlightMessage = useChatStore(s => s.setHighlightMessage)
  const searchHighlightKeyword = useChatStore(s => s.searchHighlightKeyword)
  const setSearchHighlightKeyword = useChatStore(s => s.setSearchHighlightKeyword)
  const effectiveConversationId = paneConversationId || activeConversationId

  const [input, setInput] = useState('')
  const [attachments, setAttachments] = useState<Attachment[]>([])
  const [thinkingEnabled, setThinkingEnabled] = useState(true)
  const [mcpEnabled, setMcpEnabled] = useState(true)
  const [prdEnabled, setPrdEnabled] = useState(true) // PRD 认知层开关

  // 知识库开关联动 MCP：开知识库自动开 MCP（知识库场景必然需要工具调用）
  const handlePrdToggle = (enabled: boolean) => {
    setPrdEnabled(enabled)
    if (enabled) setMcpEnabled(true)
  }
  const [isDragging, setIsDragging] = useState(false)
  const dragCounterRef = useRef(0)
  const [showModelDropdown, setShowModelDropdown] = useState(false)
  const [showPromptEditor, setShowPromptEditor] = useState(false)
  const [editingPrompt, setEditingPrompt] = useState('')
  const [aspectRatio, setAspectRatio] = useState('1:1')
  const [imageSize, setImageSize] = useState('2K')
  const [showAspectDropdown, setShowAspectDropdown] = useState(false)
  const [showSizeDropdown, setShowSizeDropdown] = useState(false)
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const scrollContainerRef = useRef<HTMLDivElement>(null)
  const exportContentRef = useRef<HTMLDivElement>(null)
  const isNearBottomRef = useRef(true)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)

  const activeConversation = conversations.find(c => c.id === effectiveConversationId)
  const selectedModel = activeConversation?.model || settings.defaultModel
  const isImageGenModel = isImageModel(selectedModel)
  const activePreset = activeConversation?.presetId
    ? PRESET_ROLES.find(r => r.id === activeConversation.presetId)
    : undefined

  const streamingMessageId = effectiveConversationId ? streamingIds[effectiveConversationId] || null : null
  const isStreaming = !!streamingMessageId

  const estimatedTokens = useMemo(() => {
    if (!activeConversation) return 0
    let chars = 0
    for (const msg of activeConversation.messages) {
      chars += msg.content.length
      if (msg.thinking) chars += msg.thinking.length
      if (msg.attachments?.length) {
        for (const att of msg.attachments) {
          if (att.parsedSheets?.length) chars += 3000 * att.parsedSheets.length
          else if (att.mimeType === 'application/pdf') chars += 5000
          else if (att.type === 'image') chars += 1000
        }
      }
    }
    return Math.round(chars * 1.3)
  }, [activeConversation?.messages])

  const tokenPercent = Math.min(100, Math.round((estimatedTokens / MAX_TOKENS) * 100))

  const handleScroll = useCallback(() => {
    const el = scrollContainerRef.current
    if (!el) return
    isNearBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < SCROLL_THRESHOLD
  }, [])

  useEffect(() => {
    if (isNearBottomRef.current) {
      messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
    }
  }, [activeConversation?.messages])

  useEffect(() => {
    if (highlightMessageId) {
      const timer = setTimeout(() => {
        setHighlightMessage(null)
        setSearchHighlightKeyword(null)
      }, 5000)
      return () => clearTimeout(timer)
    }
  }, [highlightMessageId, setHighlightMessage, setSearchHighlightKeyword])

  useEffect(() => {
    setShowPromptEditor(false)
  }, [effectiveConversationId])

  useEffect(() => {
    if (activeConversation?.imageGenConfig) {
      setAspectRatio(activeConversation.imageGenConfig.aspectRatio)
      setImageSize(activeConversation.imageGenConfig.imageSize)
    }
  }, [effectiveConversationId])

  const handleSend = async () => {
    const trimmed = input.trim()
    if ((!trimmed && attachments.length === 0) || isStreaming) return
    const pendingAttachments = [...attachments]
    const thinking = thinkingEnabled
    setInput('')
    setAttachments([])
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto'
    }
    isNearBottomRef.current = true

    // 引导模式保底：发送时如果 session 还没建，立即同步建好
    const currentCid = effectiveConversationId || activeConversation?.id || ''
    const currentConv = useChatStore.getState().conversations.find(c => c.id === currentCid)
    const currentPresetId = currentConv?.presetId
    const isCurrentlyGuided = currentPresetId === 'prd' || !!useGuidedPrdStore.getState().sessions[currentCid]
    if (currentPresetId === 'prd' && !useGuidedPrdStore.getState().sessions[currentCid]) {
      // prd preset 对话从空白启动，消息数传 0
      useGuidedPrdStore.getState().initSession(currentCid, activeConversation?.model ?? settings.defaultModel, 0)
    }

    await sendMessage(
      trimmed,
      pendingAttachments.length ? pendingAttachments : undefined,
      thinking || undefined,
      isImageGenModel ? { aspectRatio, imageSize } : undefined,
      effectiveConversationId || undefined,
      mcpEnabled || undefined,
      prdEnabled || undefined,
    )
  }

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault()
      handleSend()
    }
  }

  const handleTextareaChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    setInput(e.target.value)
    const el = e.target
    el.style.height = 'auto'
    el.style.height = Math.min(el.scrollHeight, 200) + 'px'
  }

  const validateFileSize = (file: File): boolean => {
    if (file.size > MAX_FILE_SIZE) {
      showToast('文件「' + file.name + '」超出 50MB 大小限制')
      return false
    }
    return true
  }

  const processFiles = useCallback(async (files: File[]) => {
    for (const file of files) {
      if (!validateFileSize(file)) continue
      if (isExcelFile(file.type, file.name)) {
        try {
          const sheets = await parseExcelFromFile(file)
          const reader = new FileReader()
          reader.onload = () => {
            setAttachments(prev => [...prev, {
              id: uuidv4(),
              type: 'file',
              name: file.name,
              mimeType: file.type || 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
              dataUrl: reader.result as string,
              parsedSheets: sheets,
            }])
          }
          reader.readAsDataURL(file)
        } catch {
          showToast('Excel 文件「' + file.name + '」解析失败')
        }
      } else {
        const reader = new FileReader()
        reader.onload = () => {
          setAttachments(prev => [...prev, {
            id: uuidv4(),
            type: file.type.startsWith('image/') ? 'image' : 'file',
            name: file.name,
            mimeType: file.type,
            dataUrl: reader.result as string,
          }])
        }
        reader.readAsDataURL(file)
      }
    }
  }, [])

  const handleFileSelect = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files
    if (!files) return
    await processFiles(Array.from(files))
    e.target.value = ''
  }

  const handleDragEnter = useCallback((e: React.DragEvent) => {
    e.preventDefault()
    e.stopPropagation()
    dragCounterRef.current++
    if (e.dataTransfer.types.includes('Files')) setIsDragging(true)
  }, [])

  const handleDragLeave = useCallback((e: React.DragEvent) => {
    e.preventDefault()
    e.stopPropagation()
    dragCounterRef.current--
    if (dragCounterRef.current === 0) setIsDragging(false)
  }, [])

  const handleDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault()
    e.stopPropagation()
  }, [])

  const handleDrop = useCallback(async (e: React.DragEvent) => {
    e.preventDefault()
    e.stopPropagation()
    dragCounterRef.current = 0
    setIsDragging(false)
    const files = Array.from(e.dataTransfer.files)
    if (files.length > 0) await processFiles(files)
  }, [processFiles])

  const handlePaste = (e: React.ClipboardEvent) => {
    const items = e.clipboardData.items
    let hasImage = false
    for (const item of items) {
      if (item.type.startsWith('image/')) {
        hasImage = true
        e.preventDefault()
        const file = item.getAsFile()
        if (!file) continue
        if (!validateFileSize(file)) continue
        const reader = new FileReader()
        reader.onload = () => {
          setAttachments(prev => [...prev, {
            id: uuidv4(),
            type: 'image',
            name: 'pasted-image.png',
            mimeType: file.type,
            dataUrl: reader.result as string,
          }])
        }
        reader.readAsDataURL(file)
      }
    }

    if (!hasImage) {
      const text = e.clipboardData.getData('text/plain')
      if (text && text.includes('\t') && text.includes('\n')) {
        const lines = text.trim().split('\n').map(l => l.split('\t'))
        if (lines.length >= 2 && lines[0].length >= 2) {
          e.preventDefault()
          const headers = lines[0]
          const rows = lines.slice(1).map(row =>
            headers.map((_, i) => {
              const v = row[i] ?? ''
              const n = Number(v)
              return v !== '' && !isNaN(n) ? n : (v || null)
            })
          )
          const sheet = { name: '粘贴数据', headers, rows }
          const csvLines = [headers.join(',')]
          for (const row of rows) csvLines.push(row.map(v => v == null ? '' : String(v)).join(','))
          const dataUrl = 'data:text/csv;base64,' + btoa(unescape(encodeURIComponent(csvLines.join('\n'))))
          setAttachments(prev => [...prev, {
            id: uuidv4(),
            type: 'file',
            name: '粘贴表格数据.csv',
            mimeType: 'text/csv',
            dataUrl,
            parsedSheets: [sheet],
          }])
          showToast('已识别粘贴的表格数据（' + rows.length + ' 行 × ' + headers.length + ' 列）', 'success')
        }
      }
    }
  }

  const removeAttachment = (id: string) => {
    setAttachments(prev => prev.filter(a => a.id !== id))
  }

  const handleModelSelect = (modelId: string) => {
    setShowModelDropdown(false)
    if (activeConversation) {
      setConversationModel(activeConversation.id, modelId)
    }
  }

  const handleEdit = useCallback((messageId: string) => {
    const result = editMessage(messageId)
    if (!result) return
    setInput(result.content)
    setAttachments(result.attachments || [])
    setTimeout(() => {
      const el = textareaRef.current
      if (el) {
        el.style.height = 'auto'
        el.style.height = Math.min(el.scrollHeight, 200) + 'px'
        el.focus()
      }
    }, 0)
  }, [editMessage])

  const [showExportDropdown, setShowExportDropdown] = useState(false)
  const [isExporting, setIsExporting] = useState(false)

  const handleExportPdf = useCallback(async () => {
    if (!activeConversation || isExporting) return
    if (!exportContentRef.current) {
      showToast('当前没有可导出的内容', 'error')
      return
    }
    setIsExporting(true)
    showToast('正在生成 PDF…', 'info')
    try {
      const { default: jsPDF } = await import('jspdf')
      const { default: html2canvas } = await import('html2canvas')
      // 直接将“页面上展示的聊天内容”截图，保证样式一致
      const canvas = await html2canvas(exportContentRef.current, {
        scale: 2,
        useCORS: true,
        logging: false,
        backgroundColor: '#ffffff',
      })

      const pdf = new jsPDF('p', 'mm', 'a4')
      const pageWidthMm = pdf.internal.pageSize.getWidth()
      const pageHeightMm = pdf.internal.pageSize.getHeight()

      // 以“铺满页面宽度”为基准进行分页切片（单位：px）
      const pageHeightPx = Math.floor(canvas.width * (pageHeightMm / pageWidthMm))
      const totalPages = Math.max(1, Math.ceil(canvas.height / pageHeightPx))

      const ctx = canvas.getContext('2d')
      if (!ctx) throw new Error('无法生成 PDF：Canvas context 不可用')

      for (let page = 0; page < totalPages; page++) {
        const sliceY = page * pageHeightPx
        const sliceHeight = Math.min(pageHeightPx, canvas.height - sliceY)

        const pageCanvas = document.createElement('canvas')
        pageCanvas.width = canvas.width
        pageCanvas.height = sliceHeight
        const pageCtx = pageCanvas.getContext('2d')
        if (!pageCtx) throw new Error('无法生成 PDF：分页 Canvas context 不可用')

        // 从大图中裁剪当前页
        pageCtx.drawImage(canvas, 0, sliceY, canvas.width, sliceHeight, 0, 0, canvas.width, sliceHeight)

        const imgData = pageCanvas.toDataURL('image/jpeg', 0.92)
        if (page > 0) pdf.addPage()
        pdf.addImage(imgData, 'JPEG', 0, 0, pageWidthMm, (sliceHeight * pageWidthMm) / canvas.width)
      }

      pdf.save(activeConversation.title + '.pdf')
      showToast('PDF 导出成功', 'success')
    } catch (e) {
      console.error('[PDF Export]', e)
      showToast('PDF 导出失败', 'error')
    } finally {
      setIsExporting(false)
    }
  }, [activeConversation, isExporting])

  const canSend = input.trim() || attachments.length > 0

  // ── 引导式 PRD 状态 ───────────────────────────────────────
  const conversationId = activeConversation?.id ?? ''
  const guidedSession = useGuidedPrdStore((s) => s.sessions[conversationId])
  const initSession = useGuidedPrdStore((s) => s.initSession)
  const isPrdPreset = activeConversation?.presetId === 'prd'
  const isGuidedMode = isPrdPreset || !!guidedSession
  const [rightPanelOpen, setRightPanelOpen] = useState(true)

  // 当进入引导模式时，自动打开右侧面板
  useEffect(() => {
    if (isGuidedMode && !rightPanelOpen) {
      setRightPanelOpen(true)
    }
  }, [isGuidedMode]) // eslint-disable-line react-hooks/exhaustive-deps

  // prd 预设对话：切换到该对话时自动初始化 guided session（从空白启动，消息数=0）
  useEffect(() => {
    if (isPrdPreset && conversationId && !guidedSession) {
      initSession(conversationId, activeConversation?.model ?? settings.defaultModel, 0)
    }
  }, [conversationId, isPrdPreset]) // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div
      className="flex-1 flex h-full min-w-0 relative"
      onDragEnter={handleDragEnter}
      onDragLeave={handleDragLeave}
      onDragOver={handleDragOver}
      onDrop={handleDrop}
    >
      {/* 主区域（聊天） */}
      <div className="flex flex-col flex-1 min-w-0 h-full overflow-hidden">
      {isDragging && (
        <div className="absolute inset-0 z-50 bg-indigo-50/90 border-2 border-dashed border-indigo-400 rounded-lg flex items-center justify-center pointer-events-none">
          <div className="text-center">
            <Paperclip size={32} className="text-indigo-500 mx-auto mb-2" />
            <p className="text-sm font-medium text-indigo-600">松开鼠标上传文件</p>
            <p className="text-xs text-indigo-400 mt-1">支持图片、PDF、Excel</p>
          </div>
        </div>
      )}
      {/* Top Bar */}
      <div className="h-12 border-b border-gray-200 flex items-center justify-between px-4 flex-shrink-0">
        <div className="flex items-center gap-2">
          {!sidebarOpen && !isSplitPane && (
            <button
              onClick={toggleSidebar}
              className="p-1.5 rounded-md hover:bg-gray-100 transition-colors"
            >
              <PanelLeftOpen size={18} className="text-gray-500" />
            </button>
          )}
          <div className="relative">
            <button
              onClick={() => setShowModelDropdown(!showModelDropdown)}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg hover:bg-gray-100 transition-colors text-sm font-medium text-gray-700"
            >
              <Sparkles size={14} className="text-indigo-500" />
              {settings.models.find(m => m.id === selectedModel)?.name || selectedModel}
              <ChevronDown size={14} className="text-gray-400" />
            </button>
            <Dropdown open={showModelDropdown} onClose={() => setShowModelDropdown(false)} className="w-56">
              {settings.models.map(model => (
                <button
                  key={model.id}
                  onClick={() => handleModelSelect(model.id)}
                  className={`w-full text-left px-3 py-2 text-sm hover:bg-gray-50 transition-colors ${
                    selectedModel === model.id ? 'text-indigo-600 bg-indigo-50' : 'text-gray-700'
                  }`}
                >
                  {model.name}
                </button>
              ))}
            </Dropdown>
          </div>

          {activeConversation && (
            <div className="relative">
              <button
                onClick={() => {
                  const current = activeConversation.customPrompt
                    || (activePreset ? activePreset.systemPrompt : '')
                  setEditingPrompt(current)
                  setShowPromptEditor(!showPromptEditor)
                }}
                className={`flex items-center gap-1 px-2 py-1 rounded-lg text-xs font-medium transition-colors ${
                  activePreset || activeConversation.customPrompt
                    ? 'bg-violet-50 text-violet-600 hover:bg-violet-100'
                    : 'text-gray-400 hover:bg-gray-100 hover:text-gray-600'
                }`}
                title="设置角色提示词"
              >
                {activePreset ? (
                  <>
                    <span>{activePreset.icon}</span>
                    {activePreset.name}
                  </>
                ) : activeConversation.customPrompt ? (
                  <span>自定义角色</span>
                ) : (
                  <span>角色设定</span>
                )}
                <Pencil size={10} className="ml-0.5 opacity-60" />
              </button>
              {showPromptEditor && (
                <>
                  <div className="fixed inset-0 z-40" onClick={() => setShowPromptEditor(false)} />
                  <div className="absolute left-0 top-full mt-1 z-50 w-96 bg-white rounded-xl shadow-xl border border-gray-200 p-3">
                    <div className="flex items-center justify-between mb-2">
                      <span className="text-xs font-medium text-gray-600">角色提示词</span>
                      {activePreset && (
                        <button
                          onClick={() => setEditingPrompt(activePreset.systemPrompt)}
                          className="text-[10px] text-gray-400 hover:text-violet-600 transition-colors"
                        >
                          恢复默认
                        </button>
                      )}
                    </div>
                    <textarea
                      value={editingPrompt}
                      onChange={e => setEditingPrompt(e.target.value)}
                      placeholder="输入角色提示词，例如：你是一名资深产品经理…"
                      className="w-full h-32 text-xs text-gray-700 border border-gray-200 rounded-lg p-2 resize-none outline-none focus:border-violet-300 focus:ring-1 focus:ring-violet-100"
                    />
                    <div className="flex justify-end gap-2 mt-2">
                      <button
                        onClick={() => setShowPromptEditor(false)}
                        className="px-3 py-1 text-xs text-gray-500 hover:text-gray-700 transition-colors"
                      >
                        取消
                      </button>
                      <button
                        onClick={() => {
                          setConversationPrompt(activeConversation.id, editingPrompt)
                          setShowPromptEditor(false)
                        }}
                        className="px-3 py-1 text-xs bg-violet-600 text-white rounded-md hover:bg-violet-700 transition-colors"
                      >
                        保存
                      </button>
                    </div>
                  </div>
                </>
              )}
            </div>
          )}
        </div>
        <div className="flex items-center gap-1">
          {activeConversation && activeConversation.messages.length > 0 && (
            <div className="relative">
              <button
                onClick={() => setShowExportDropdown(!showExportDropdown)}
                className="p-1.5 rounded-md hover:bg-gray-100 transition-colors text-gray-400 hover:text-gray-600"
                title="导出对话"
              >
                <Download size={16} />
              </button>
              <Dropdown open={showExportDropdown} onClose={() => setShowExportDropdown(false)} className="w-36 right-0 left-auto">
                <button
                  onClick={() => { handleExportPdf(); setShowExportDropdown(false) }}
                  disabled={isExporting}
                  className="w-full text-left px-3 py-2 text-xs hover:bg-gray-50 text-gray-700 transition-colors flex items-center gap-2 disabled:opacity-50"
                >
                  <Download size={13} />
                  导出 PDF
                </button>
              </Dropdown>
            </div>
          )}
          {isSplitPane && onClose && (
            <button
              onClick={onClose}
              className="p-1.5 rounded-md hover:bg-gray-100 transition-colors text-gray-400 hover:text-red-500"
              title="关闭窗格"
            >
              <XIcon size={16} />
            </button>
          )}
        </div>
      </div>

      {/* Document Bar */}
      {activeConversation && (
        <DocBar
          conversationId={activeConversation.id}
          docIds={activeConversation.docIds || []}
        />
      )}

      {/* Messages or Welcome Screen */}
      <div className="flex-1 overflow-y-auto" ref={scrollContainerRef} onScroll={handleScroll}>
        {!activeConversation || activeConversation.messages.length === 0 ? (
          <div className="h-full flex flex-col items-center justify-center px-4">
            <img src="/fish.jpg" alt="鳕鱼助理" className="w-20 h-20 rounded-2xl object-cover mb-6 shadow-lg shadow-indigo-200" />
            {activeConversation ? (
              activePreset ? (
                <>
                  <span className="text-3xl mb-3">{activePreset.icon}</span>
                  <h1 className="text-lg font-medium text-gray-600 mb-2">{activePreset.name}</h1>
                  <p className="text-sm text-gray-400">{activePreset.description}，请输入你的需求</p>
                </>
              ) : (
                <h1 className="text-xl font-medium text-gray-400">
                  有什么可以帮你的？
                </h1>
              )
            ) : (
              <>
                <h1 className="text-xl font-medium text-gray-400 mb-8">
                  有什么可以帮你的？
                </h1>
                <div className="grid grid-cols-3 sm:grid-cols-5 gap-3 max-w-xl">
                  {PRESET_ROLES.map(role => (
                    <button
                      key={role.id}
                      onClick={() => {
                        const cid = createConversation(role.id)
                        if (role.id === 'prd') {
                          // 引导式 PRD：先创建对话，再启动引导会话
                          // startGuidedSession 在用户首次发消息时由 handleSend 触发
                          // 这里只需切换到该对话，初始化由 hook 完成
                          useGuidedPrdStore.getState().initSession(cid, useChatStore.getState().settings.defaultModel)
                        }
                      }}
                      className="flex flex-col items-center gap-2 px-3 py-4 rounded-xl border border-gray-200 hover:border-indigo-300 hover:bg-indigo-50/50 transition-all group"
                    >
                      <span className="text-2xl">{role.icon}</span>
                      <span className="text-xs font-medium text-gray-600 group-hover:text-indigo-600">{role.name}</span>
                      <span className="text-[10px] text-gray-400 leading-tight text-center">{role.description}</span>
                    </button>
                  ))}
                </div>
              </>
            )}
          </div>
        ) : (
          <div ref={exportContentRef} className="max-w-3xl mx-auto py-6 px-4">
            {activeConversation.messages.map((msg, idx) => (
              <div key={msg.id}>
                <MessageBubble
                  message={msg}
                  isStreaming={msg.id === streamingMessageId}
                  isImageGen={isImageGenModel}
                  highlighted={msg.id === highlightMessageId}
                  highlightKeyword={searchHighlightKeyword}
                  onRetry={retryMessage}
                  onEdit={handleEdit}
                  conversationId={activeConversation.id}
                />
                {/* 参考来源提示：只在最新的 assistant 消息下方显示 */}
                {msg.role === 'assistant' &&
                 idx === activeConversation.messages.length - 1 && (
                  (() => {
                    // 分离 PRD 和手动关联的显示逻辑
                    const hasPrdMatches = (activeConversation.lastPrdMatches?.length ?? 0) > 0
                    const hasManualDocs = (activeConversation.docIds?.length ?? 0) > 0

                    // 只有当至少有一个来源时才显示
                    if (!hasPrdMatches && !hasManualDocs) return null

                    return (
                      <SourceIndicator
                        prdMatches={hasPrdMatches ? activeConversation.lastPrdMatches : undefined}
                        docSources={hasManualDocs ? activeConversation.docIds.map(id => ({ docId: id })) : undefined}
                      />
                    )
                  })()
                )}
              </div>
            ))}
            <div ref={messagesEndRef} />
          </div>
        )}
      </div>

      {/* Context Warning */}
      {tokenPercent >= 75 && (
        <div className={`px-4 py-1.5 text-center text-xs font-medium ${
          tokenPercent >= 90
            ? 'bg-red-50 text-red-600 border-t border-red-200'
            : 'bg-amber-50 text-amber-600 border-t border-amber-200'
        }`}>
          上下文已使用约 {tokenPercent}%（~{Math.round(estimatedTokens / 1000)}K tokens），
          {tokenPercent >= 90 ? '即将达到上限，请开启新对话' : '建议适时开启新对话'}
        </div>
      )}

      {/* Input Area */}
      <div className="border-t border-gray-200 px-4 py-3 flex-shrink-0">
        <div className="max-w-3xl mx-auto">
          {/* Attachment Previews */}
          {attachments.length > 0 && (
            <div className="flex gap-2 mb-2 flex-wrap">
              {attachments.map(att => (
                <div key={att.id} className="relative group">
                  {att.type === 'image' ? (
                    <img
                      src={att.dataUrl}
                      alt={att.name}
                      className="h-16 w-16 object-cover rounded-lg border border-gray-200"
                    />
                  ) : att.parsedSheets ? (
                    <div className="h-16 px-3 flex items-center gap-2 rounded-lg border border-emerald-200 bg-emerald-50">
                      <Table2 size={16} className="text-emerald-600" />
                      <div className="flex flex-col">
                        <span className="text-xs text-gray-700 max-w-[100px] truncate">{att.name}</span>
                        <span className="text-[10px] text-emerald-500">
                          {att.parsedSheets.length} 个工作表
                        </span>
                      </div>
                    </div>
                  ) : (
                    <div className="h-16 px-3 flex items-center gap-2 rounded-lg border border-gray-200 bg-gray-50">
                      <FileText size={16} className="text-red-500" />
                      <span className="text-xs text-gray-600 max-w-[100px] truncate">{att.name}</span>
                    </div>
                  )}
                  <button
                    onClick={() => removeAttachment(att.id)}
                    className="absolute -top-1.5 -right-1.5 w-5 h-5 rounded-full bg-gray-800 text-white flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity"
                  >
                    <XIcon size={10} />
                  </button>
                </div>
              ))}
            </div>
          )}

          {/* Enhancement Toolbar - 增强模式工具栏 */}
          {!isImageGenModel && (
            <div className="flex items-center gap-1 mb-2 pb-2 border-b border-gray-100">
              <div className="flex items-center gap-1 text-xs text-gray-400 mr-2">
                <Wrench size={12} />
                <span>增强</span>
              </div>

              {/* 深度思考 */}
              <button
                onClick={() => setThinkingEnabled(!thinkingEnabled)}
                className={`flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-xs font-medium transition-all ${
                  thinkingEnabled
                    ? 'bg-amber-50 text-amber-700 border border-amber-200 hover:bg-amber-100'
                    : 'bg-gray-50 text-gray-400 border border-gray-200 hover:bg-gray-100 hover:text-gray-600'
                }`}
                title="深度思考：AI 会在回答前进行更深入的推理分析"
              >
                <Brain size={14} />
                <span>深度思考</span>
                {thinkingEnabled && <span className="w-1.5 h-1.5 rounded-full bg-amber-500" />}
              </button>

              {/* PRD 知识库（开启时自动带上 MCP 工具调用） */}
              <ChatKnowledgeBaseSelector
                enabled={prdEnabled}
                onToggle={handlePrdToggle}
                disabled={isStreaming}
              />

              {/* PRD 助手：手动启动引导模式 */}
              {!isGuidedMode && (
                <button
                  onClick={() => {
                    if (conversationId) {
                      const msgCount = activeConversation?.messages.length ?? 0
                      useGuidedPrdStore.getState().initSession(
                        conversationId,
                        activeConversation?.model ?? settings.defaultModel,
                        msgCount  // 中途打开：记录已有消息数
                      )
                    }
                  }}
                  disabled={isStreaming}
                  className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-xs font-medium transition-all bg-gray-50 text-gray-400 border border-gray-200 hover:bg-indigo-50 hover:text-indigo-600 hover:border-indigo-200 disabled:opacity-40"
                  title="启动 PRD 助手：AI 将引导你梳理需求并写入文档"
                >
                  <FileText size={14} />
                  <span>PRD 助手</span>
                </button>
              )}

              {/* 已进入引导模式的标识 - 点击可打开/关闭右侧面板 */}
              {isGuidedMode && (
                <button
                  onClick={() => setRightPanelOpen(!rightPanelOpen)}
                  className={`flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-xs font-medium transition-all ${
                    rightPanelOpen
                      ? 'bg-indigo-50 text-indigo-600 border border-indigo-200'
                      : 'bg-gray-50 text-gray-500 border border-gray-200 hover:bg-indigo-50 hover:text-indigo-600'
                  }`}
                  title={rightPanelOpen ? '关闭 PRD 助手面板' : '打开 PRD 助手面板'}
                >
                  <FileText size={14} />
                  <span>PRD 助手</span>
                  <span className={`w-1.5 h-1.5 rounded-full ${rightPanelOpen ? 'bg-indigo-500' : 'bg-gray-400'}`} />
                </button>
              )}
            </div>
          )}

          <div className="relative flex items-end bg-gray-50 rounded-2xl border border-gray-200 focus-within:border-indigo-400 focus-within:ring-2 focus-within:ring-indigo-100 transition-all">
            <input
              ref={fileInputRef}
              type="file"
              accept={ACCEPT_TYPES}
              multiple
              onChange={handleFileSelect}
              className="hidden"
            />
            <button
              onClick={() => fileInputRef.current?.click()}
              className="m-2 p-2 rounded-lg hover:bg-gray-200 transition-colors text-gray-400 hover:text-gray-600"
              title="添加图片、PDF 或 Excel"
            >
              <Paperclip size={16} />
            </button>

            {/* Image Generation Controls */}
            {isImageGenModel && (
              <>
                <div className="relative my-2">
                  <button
                    onClick={() => { setShowAspectDropdown(!showAspectDropdown); setShowSizeDropdown(false) }}
                    className="flex items-center gap-1 px-2 py-1.5 rounded-lg text-xs font-medium bg-violet-50 text-violet-600 hover:bg-violet-100 transition-colors"
                  >
                    <span>比例</span>
                    <span className="font-semibold">{aspectRatio}</span>
                    <ChevronDown size={12} />
                  </button>
                  <Dropdown
                    open={showAspectDropdown}
                    onClose={() => setShowAspectDropdown(false)}
                    direction="up"
                    className="w-24 max-h-52 overflow-y-auto"
                  >
                    {ASPECT_RATIOS.map(r => (
                      <button
                        key={r}
                        onClick={() => { setAspectRatio(r); setShowAspectDropdown(false) }}
                        className={`w-full text-left px-3 py-1.5 text-xs hover:bg-gray-50 transition-colors ${
                          aspectRatio === r ? 'text-violet-600 bg-violet-50 font-medium' : 'text-gray-700'
                        }`}
                      >
                        {r}
                      </button>
                    ))}
                  </Dropdown>
                </div>
                <div className="relative my-2">
                  <button
                    onClick={() => { setShowSizeDropdown(!showSizeDropdown); setShowAspectDropdown(false) }}
                    className="flex items-center gap-1 px-2 py-1.5 rounded-lg text-xs font-medium bg-violet-50 text-violet-600 hover:bg-violet-100 transition-colors"
                  >
                    <span>尺寸</span>
                    <span className="font-semibold">{imageSize}</span>
                    <ChevronDown size={12} />
                  </button>
                  <Dropdown
                    open={showSizeDropdown}
                    onClose={() => setShowSizeDropdown(false)}
                    direction="up"
                    className="w-20"
                  >
                    {IMAGE_SIZES.map(s => (
                      <button
                        key={s}
                        onClick={() => { setImageSize(s); setShowSizeDropdown(false) }}
                        className={`w-full text-left px-3 py-1.5 text-xs hover:bg-gray-50 transition-colors ${
                          imageSize === s ? 'text-violet-600 bg-violet-50 font-medium' : 'text-gray-700'
                        }`}
                      >
                        {s}
                      </button>
                    ))}
                  </Dropdown>
                </div>
              </>
            )}

            <textarea
              ref={textareaRef}
              value={input}
              onChange={handleTextareaChange}
              onKeyDown={handleKeyDown}
              onPaste={handlePaste}
              placeholder="输入消息…"
              rows={1}
              className="flex-1 resize-none bg-transparent py-3 pl-1 outline-none text-gray-800 text-sm placeholder-gray-400 max-h-[200px]"
            />
            {isStreaming ? (
              <button
                onClick={() => stopStreaming(effectiveConversationId || undefined)}
                className="m-2 p-2 rounded-lg bg-gray-200 hover:bg-gray-300 transition-colors"
              >
                <Square size={16} className="text-gray-600" />
              </button>
            ) : (
              <button
                onClick={handleSend}
                disabled={!canSend}
                className="m-2 p-2 rounded-lg bg-indigo-600 hover:bg-indigo-700 disabled:bg-gray-300 disabled:cursor-not-allowed transition-colors"
              >
                <Send size={16} className="text-white" />
              </button>
            )}
          </div>
          <p className="text-xs text-gray-400 text-center mt-2">
            鳕鱼助理可能产生不准确的信息，请注意核实。
          </p>
        </div>
      </div>
      </div>{/* 主区域结束 */}

      {/* 右侧 PRD 草稿面板（仅引导模式且面板打开） */}
      {isGuidedMode && rightPanelOpen && conversationId && (
        <div className="w-[380px] flex-shrink-0 h-full">
          <RightPanel
            conversationId={conversationId}
            onClose={() => setRightPanelOpen(false)}
          />
        </div>
      )}

    </div>
  )
}

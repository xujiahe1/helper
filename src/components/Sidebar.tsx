import { useState, useMemo, useRef, useCallback } from 'react'
import {
  Plus,
  MessageSquare,
  Trash2,
  Pencil,
  Settings,
  Check,
  X,
  PanelLeftClose,
  Search,
  Columns2,
  Network,
} from 'lucide-react'
import { useChatStore } from '../store'
import { PRESET_ROLES } from '../types'

export function Sidebar() {
  const conversations = useChatStore(s => s.conversations)
  const activeConversationId = useChatStore(s => s.activeConversationId)
  const createConversation = useChatStore(s => s.createConversation)
  const deleteConversation = useChatStore(s => s.deleteConversation)
  const renameConversation = useChatStore(s => s.renameConversation)
  const setActiveConversation = useChatStore(s => s.setActiveConversation)
  const setHighlightMessage = useChatStore(s => s.setHighlightMessage)
  const setSearchHighlightKeyword = useChatStore(s => s.setSearchHighlightKeyword)
  const addSplitPane = useChatStore(s => s.addSplitPane)
  const toggleSettings = useChatStore(s => s.toggleSettings)
  const toggleSidebar = useChatStore(s => s.toggleSidebar)
  const activeView = useChatStore(s => s.activeView)
  const setActiveView = useChatStore(s => s.setActiveView)

  const [editingId, setEditingId] = useState<string | null>(null)
  const [editTitle, setEditTitle] = useState('')
  const [searchQuery, setSearchQuery] = useState('')
  const [showNewMenu, setShowNewMenu] = useState(false)
  const hoverTimerRef = useRef<ReturnType<typeof setTimeout>>()

  const openMenu = useCallback(() => {
    clearTimeout(hoverTimerRef.current)
    setShowNewMenu(true)
  }, [])

  const closeMenuDelayed = useCallback(() => {
    hoverTimerRef.current = setTimeout(() => setShowNewMenu(false), 150)
  }, [])

  const handleStartRename = (e: React.MouseEvent, id: string, currentTitle: string) => {
    e.stopPropagation()
    setEditingId(id)
    setEditTitle(currentTitle)
  }

  const handleConfirmRename = () => {
    if (editingId && editTitle.trim()) {
      renameConversation(editingId, editTitle.trim())
    }
    setEditingId(null)
  }

  const handleCancelRename = () => {
    setEditingId(null)
    setEditTitle('')
  }

  const handleDelete = (e: React.MouseEvent, id: string) => {
    e.stopPropagation()
    deleteConversation(id)
  }

  const filtered = useMemo(() => {
    if (!searchQuery.trim()) return conversations
    const q = searchQuery.toLowerCase()
    return conversations.filter(c => {
      if (c.title.toLowerCase().includes(q)) return true
      return c.messages.some(m => m.content.toLowerCase().includes(q))
    })
  }, [conversations, searchQuery])

  const today = new Date()
  today.setHours(0, 0, 0, 0)
  const yesterday = new Date(today)
  yesterday.setDate(yesterday.getDate() - 1)
  const sevenDaysAgo = new Date(today)
  sevenDaysAgo.setDate(sevenDaysAgo.getDate() - 7)

  const groups: { label: string; items: typeof conversations }[] = []
  const todayItems = filtered.filter(c => c.updatedAt >= today.getTime())
  const yesterdayItems = filtered.filter(
    c => c.updatedAt >= yesterday.getTime() && c.updatedAt < today.getTime(),
  )
  const weekItems = filtered.filter(
    c => c.updatedAt >= sevenDaysAgo.getTime() && c.updatedAt < yesterday.getTime(),
  )
  const olderItems = filtered.filter(c => c.updatedAt < sevenDaysAgo.getTime())

  if (todayItems.length) groups.push({ label: '今天', items: todayItems })
  if (yesterdayItems.length) groups.push({ label: '昨天', items: yesterdayItems })
  if (weekItems.length) groups.push({ label: '最近七天', items: weekItems })
  if (olderItems.length) groups.push({ label: '更早', items: olderItems })

  return (
    <div className="w-64 h-full bg-slate-900 text-slate-300 flex flex-col flex-shrink-0">
      <div className="p-3 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <img src="/fish.jpg" alt="鳕鱼助理" className="w-8 h-8 rounded-lg object-cover" />
          <span className="font-semibold text-white text-lg">鳕鱼助理</span>
        </div>
        <button
          onClick={toggleSidebar}
          className="p-1.5 rounded-md hover:bg-slate-800 transition-colors"
        >
          <PanelLeftClose size={18} />
        </button>
      </div>

      <div className="px-3 mb-2 space-y-2">
        <div
          className="relative"
          onMouseEnter={openMenu}
          onMouseLeave={closeMenuDelayed}
        >
          <button
            onClick={() => { createConversation(); setActiveView('chat') }}
            className="w-full flex items-center gap-2 px-3 py-2.5 rounded-lg border border-slate-700 hover:bg-slate-800 transition-colors text-sm"
          >
            <Plus size={16} />
            <span>新建对话</span>
          </button>
          {showNewMenu && (
            <div
              className="absolute left-full top-0 ml-1 z-50 bg-slate-800 rounded-lg border border-slate-700 py-1 shadow-xl w-48"
              onMouseEnter={openMenu}
              onMouseLeave={closeMenuDelayed}
            >
              <div className="px-3 py-1.5 text-[10px] text-slate-500 font-medium">选择预设角色</div>
              {PRESET_ROLES.map(role => (
                <button
                  key={role.id}
                  onClick={() => { createConversation(role.id); setActiveView('chat'); setShowNewMenu(false) }}
                  className="w-full flex items-center gap-2.5 px-3 py-2 text-xs hover:bg-slate-700 transition-colors"
                >
                  <span className="text-base">{role.icon}</span>
                  <div className="text-left">
                    <div className="text-slate-200">{role.name}</div>
                    <div className="text-slate-500 text-[10px] leading-tight">{role.description}</div>
                  </div>
                </button>
              ))}
            </div>
          )}
        </div>
        {conversations.length > 0 && (
          <div className="relative">
            <Search size={14} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-slate-500" />
            <input
              value={searchQuery}
              onChange={e => setSearchQuery(e.target.value)}
              placeholder="搜索对话…"
              className="w-full bg-slate-800 text-slate-300 placeholder-slate-500 text-xs rounded-lg pl-8 pr-7 py-2 outline-none focus:ring-1 focus:ring-slate-600"
            />
            {searchQuery && (
              <button
                onClick={() => setSearchQuery('')}
                className="absolute right-2 top-1/2 -translate-y-1/2 text-slate-500 hover:text-slate-300"
              >
                <X size={12} />
              </button>
            )}
          </div>
        )}
      </div>

      <div className="flex-1 overflow-y-auto px-2 sidebar-scrollbar">
        {groups.map(group => (
          <div key={group.label} className="mb-3">
            <div className="px-2 py-1.5 text-xs font-medium text-slate-500 uppercase tracking-wider">
              {group.label}
            </div>
            {group.items.map(conv => {
              const lastMsg = conv.messages.length > 0 ? conv.messages[conv.messages.length - 1] : null
              const preview = lastMsg
                ? (lastMsg.content || (lastMsg.attachments?.length ? '[附件]' : '')).slice(0, 40).replace(/\n/g, ' ')
                : ''
              return (
                <div
                  key={conv.id}
                  onClick={() => {
                    setActiveConversation(conv.id)
                    setActiveView('chat')
                    if (searchQuery.trim()) {
                      const q = searchQuery.toLowerCase()
                      const match = conv.messages.find(m => m.content.toLowerCase().includes(q))
                      if (match) {
                        setHighlightMessage(match.id)
                        setSearchHighlightKeyword(searchQuery.trim())
                      }
                    } else {
                      setSearchHighlightKeyword(null)
                    }
                  }}
                  className={`group flex items-start gap-2 px-2 py-2 rounded-lg cursor-pointer text-sm transition-colors mb-0.5 ${
                    activeConversationId === conv.id
                      ? 'bg-slate-800 text-white'
                      : 'hover:bg-slate-800/50'
                  }`}
                >
                  <MessageSquare size={14} className="flex-shrink-0 opacity-50 mt-0.5" />
                  {editingId === conv.id ? (
                    <div className="flex-1 flex items-center gap-1">
                      <input
                        value={editTitle}
                        onChange={e => setEditTitle(e.target.value)}
                        onKeyDown={e => {
                          if (e.key === 'Enter') handleConfirmRename()
                          if (e.key === 'Escape') handleCancelRename()
                        }}
                        className="flex-1 bg-slate-700 text-white px-1.5 py-0.5 rounded text-sm outline-none"
                        autoFocus
                        onClick={e => e.stopPropagation()}
                      />
                      <button onClick={handleConfirmRename} className="p-0.5 hover:text-green-400">
                        <Check size={14} />
                      </button>
                      <button onClick={handleCancelRename} className="p-0.5 hover:text-red-400">
                        <X size={14} />
                      </button>
                    </div>
                  ) : (
                    <>
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-1">
                          <span className="truncate flex-1">{conv.title}</span>
                          {conv.messages.length > 0 && (
                            <span className="text-[10px] text-slate-500 flex-shrink-0">{conv.messages.length}条</span>
                          )}
                        </div>
                        {preview && (
                          <p className="text-[11px] text-slate-500 truncate mt-0.5">{preview}</p>
                        )}
                      </div>
                      <div className="hidden group-hover:flex items-center gap-0.5 flex-shrink-0">
                        <button
                          onClick={e => { e.stopPropagation(); addSplitPane(conv.id) }}
                          className="p-1 rounded hover:bg-slate-700 transition-colors"
                          title="在新窗格中打开"
                        >
                          <Columns2 size={12} />
                        </button>
                        <button
                          onClick={e => handleStartRename(e, conv.id, conv.title)}
                          className="p-1 rounded hover:bg-slate-700 transition-colors"
                        >
                          <Pencil size={12} />
                        </button>
                        <button
                          onClick={e => handleDelete(e, conv.id)}
                          className="p-1 rounded hover:bg-slate-700 hover:text-red-400 transition-colors"
                        >
                          <Trash2 size={12} />
                        </button>
                      </div>
                    </>
                  )}
                </div>
              )
            })}
          </div>
        ))}
        {groups.length === 0 && (
          <div className="px-3 py-8 text-center text-slate-500 text-sm">
            {searchQuery ? '没有匹配的对话' : '暂无对话'}
          </div>
        )}
      </div>

      <div className="p-3 border-t border-slate-800">
        <button
          onClick={() => setActiveView('prd')}
          className={`w-full flex items-center gap-2 px-3 py-2 rounded-lg hover:bg-slate-800 transition-colors text-sm ${
            activeView === 'prd' ? 'bg-slate-800 text-white' : ''
          }`}
        >
          <Network size={16} />
          <span>PRD 知识库</span>
        </button>
        <button
          onClick={toggleSettings}
          className="w-full flex items-center gap-2 px-3 py-2 rounded-lg hover:bg-slate-800 transition-colors text-sm mt-1"
        >
          <Settings size={16} />
          <span>设置</span>
        </button>
      </div>
    </div>
  )
}

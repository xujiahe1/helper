import { useEffect } from 'react'
import { Sidebar } from './components/Sidebar'
import { ChatArea } from './components/ChatArea'
import { SettingsModal } from './components/SettingsModal'
import { ErrorBoundary } from './components/ErrorBoundary'
import { ToastContainer } from './components/Toast'
import { PrdManager } from './components/prd/PrdManager'
import { useChatStore } from './store'

export default function App() {
  const sidebarOpen = useChatStore(s => s.sidebarOpen)
  const settingsOpen = useChatStore(s => s.settingsOpen)
  const splitPaneIds = useChatStore(s => s.splitPaneIds)
  const removeSplitPane = useChatStore(s => s.removeSplitPane)
  const activeView = useChatStore(s => s.activeView)

  useEffect(() => {
    const state = useChatStore.getState()
    if (state.conversations.length === 0) {
      state.createConversation()
    } else if (!state.activeConversationId) {
      state.setActiveConversation(state.conversations[0].id)
    }

    // 从 IndexedDB 恢复附件
    state.restoreAttachments().catch(console.error)
  }, [])

  return (
    <ErrorBoundary>
      <div className="h-full flex bg-white">
        {sidebarOpen && <Sidebar />}
        {activeView === 'chat' ? (
          <>
            <ChatArea />
            {splitPaneIds.map(paneId => (
              <div key={paneId} className="flex-1 min-w-0 border-l border-gray-200">
                <ChatArea
                  paneConversationId={paneId}
                  isSplitPane
                  onClose={() => removeSplitPane(paneId)}
                />
              </div>
            ))}
          </>
        ) : (
          <div className="flex-1 min-w-0">
            <PrdManager />
          </div>
        )}
        {settingsOpen && <SettingsModal />}
        <ToastContainer />
      </div>
    </ErrorBoundary>
  )
}

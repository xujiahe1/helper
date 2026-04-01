import { useState, useEffect } from 'react'
import { X, Plus, Trash2 } from 'lucide-react'
import { useChatStore } from '../store'
import type { AppSettings, ModelOption } from '../types'

export function SettingsModal() {
  const settings = useChatStore(s => s.settings)
  const updateSettings = useChatStore(s => s.updateSettings)
  const toggleSettings = useChatStore(s => s.toggleSettings)

  const [formData, setFormData] = useState<AppSettings>({ ...settings })

  useEffect(() => {
    setFormData({ ...settings })
  }, [settings])

  useEffect(() => {
    const handler = (e: KeyboardEvent) => { if (e.key === 'Escape') toggleSettings() }
    document.addEventListener('keydown', handler)
    return () => document.removeEventListener('keydown', handler)
  }, [toggleSettings])

  const handleSave = () => {
    updateSettings(formData)
    toggleSettings()
  }

  const handleAddModel = () => {
    setFormData(prev => ({
      ...prev,
      models: [...prev.models, { id: '', name: '' }],
    }))
  }

  const handleRemoveModel = (index: number) => {
    setFormData(prev => ({
      ...prev,
      models: prev.models.filter((_, i) => i !== index),
    }))
  }

  const handleModelChange = (index: number, field: keyof ModelOption, value: string) => {
    setFormData(prev => ({
      ...prev,
      models: prev.models.map((m, i) => (i === index ? { ...m, [field]: value } : m)),
    }))
  }

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50" onClick={toggleSettings}>
      <div
        className="bg-white rounded-2xl w-full max-w-lg max-h-[80vh] overflow-hidden shadow-xl"
        onClick={e => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-200">
          <h2 className="text-lg font-semibold text-gray-800">设置</h2>
          <button onClick={toggleSettings} className="p-1 rounded-md hover:bg-gray-100 transition-colors">
            <X size={20} className="text-gray-500" />
          </button>
        </div>

        <div className="overflow-y-auto px-6 py-4 space-y-6 max-h-[calc(80vh-130px)]">
          {/* LLM API */}
          <section>
            <h3 className="text-sm font-semibold text-gray-800 mb-3">大模型 API</h3>
            <div className="space-y-3">
              <div>
                <label className="block text-xs text-gray-500 mb-1">API Base URL</label>
                <input
                  value={formData.apiBaseUrl}
                  onChange={e => setFormData(prev => ({ ...prev, apiBaseUrl: e.target.value }))}
                  placeholder="/llm-api/v1"
                  className="w-full px-3 py-2 rounded-lg border border-gray-300 text-sm outline-none focus:border-indigo-400 focus:ring-2 focus:ring-indigo-100"
                />
              </div>
              <div>
                <label className="block text-xs text-gray-500 mb-1">API Key</label>
                <input
                  type="password"
                  value={formData.apiKey}
                  onChange={e => setFormData(prev => ({ ...prev, apiKey: e.target.value }))}
                  placeholder="输入 API Key…"
                  className="w-full px-3 py-2 rounded-lg border border-gray-300 text-sm outline-none focus:border-indigo-400 focus:ring-2 focus:ring-indigo-100"
                />
              </div>
              <div>
                <label className="block text-xs text-gray-500 mb-1">默认模型</label>
                <select
                  value={formData.defaultModel}
                  onChange={e => setFormData(prev => ({ ...prev, defaultModel: e.target.value }))}
                  className="w-full px-3 py-2 rounded-lg border border-gray-300 text-sm outline-none focus:border-indigo-400 focus:ring-2 focus:ring-indigo-100 bg-white"
                >
                  {formData.models.map(m => (
                    <option key={m.id} value={m.id}>{m.name || m.id}</option>
                  ))}
                </select>
              </div>
              <div>
                <label className="block text-xs text-gray-500 mb-1">系统模型</label>
                <p className="text-xs text-gray-400 mb-1">用于实体抽取、归一化等后台任务</p>
                <select
                  value={formData.systemModel}
                  onChange={e => setFormData(prev => ({ ...prev, systemModel: e.target.value }))}
                  className="w-full px-3 py-2 rounded-lg border border-gray-300 text-sm outline-none focus:border-indigo-400 focus:ring-2 focus:ring-indigo-100 bg-white"
                >
                  {formData.models.map(m => (
                    <option key={m.id} value={m.id}>{m.name || m.id}</option>
                  ))}
                </select>
              </div>
            </div>
          </section>

          {/* Models */}
          <section>
            <div className="flex items-center justify-between mb-3">
              <h3 className="text-sm font-semibold text-gray-800">模型列表</h3>
              <button
                onClick={handleAddModel}
                className="flex items-center gap-1 text-xs text-indigo-600 hover:text-indigo-700"
              >
                <Plus size={14} />
                添加模型
              </button>
            </div>
            <div className="space-y-2">
              {formData.models.map((model, index) => (
                <div key={index} className="flex gap-2 items-center">
                  <input
                    value={model.id}
                    onChange={e => handleModelChange(index, 'id', e.target.value)}
                    placeholder="模型 ID"
                    className="flex-1 px-3 py-2 rounded-lg border border-gray-300 text-sm outline-none focus:border-indigo-400 focus:ring-2 focus:ring-indigo-100"
                  />
                  <input
                    value={model.name}
                    onChange={e => handleModelChange(index, 'name', e.target.value)}
                    placeholder="显示名称"
                    className="flex-1 px-3 py-2 rounded-lg border border-gray-300 text-sm outline-none focus:border-indigo-400 focus:ring-2 focus:ring-indigo-100"
                  />
                  <button
                    onClick={() => handleRemoveModel(index)}
                    className="p-2 rounded-lg hover:bg-red-50 text-gray-400 hover:text-red-500 transition-colors"
                  >
                    <Trash2 size={14} />
                  </button>
                </div>
              ))}
            </div>
          </section>

          {/* Document API */}
          <section>
            <h3 className="text-sm font-semibold text-gray-800 mb-3">文档 API</h3>
            <p className="text-xs text-gray-400 mb-3">
              Token 将通过 App ID 和 Secret 自动获取。
            </p>
            <div className="space-y-3">
              <div>
                <label className="block text-xs text-gray-500 mb-1">API Base URL</label>
                <input
                  value={formData.documentApiBaseUrl}
                  onChange={e => setFormData(prev => ({ ...prev, documentApiBaseUrl: e.target.value }))}
                  placeholder="/doc-api"
                  className="w-full px-3 py-2 rounded-lg border border-gray-300 text-sm outline-none focus:border-indigo-400 focus:ring-2 focus:ring-indigo-100"
                />
              </div>
              <div>
                <label className="block text-xs text-gray-500 mb-1">App ID</label>
                <input
                  value={formData.documentAppId}
                  onChange={e => setFormData(prev => ({ ...prev, documentAppId: e.target.value }))}
                  placeholder="cli_xxx"
                  className="w-full px-3 py-2 rounded-lg border border-gray-300 text-sm outline-none focus:border-indigo-400 focus:ring-2 focus:ring-indigo-100 font-mono text-xs"
                />
              </div>
              <div>
                <label className="block text-xs text-gray-500 mb-1">App Secret</label>
                <input
                  type="password"
                  value={formData.documentAppSecret}
                  onChange={e => setFormData(prev => ({ ...prev, documentAppSecret: e.target.value }))}
                  placeholder="输入 App Secret…"
                  className="w-full px-3 py-2 rounded-lg border border-gray-300 text-sm outline-none focus:border-indigo-400 focus:ring-2 focus:ring-indigo-100"
                />
              </div>
            </div>
          </section>

          {/* MCP */}
          <section>
            <h3 className="text-sm font-semibold text-gray-800 mb-3">MCP（openapi-mcp）</h3>
            <p className="text-xs text-gray-400 mb-3">
              用于在对话中执行 MCP 工具。推荐启动 openapi-mcp 时开启 <span className="font-mono">--json-response</span>。
            </p>
            <div className="space-y-3">
              <div>
                <label className="block text-xs text-gray-500 mb-1">MCP Base URL</label>
                <input
                  value={formData.mcpBaseUrl}
                  onChange={e => setFormData(prev => ({ ...prev, mcpBaseUrl: e.target.value }))}
                  placeholder="http://localhost:5524"
                  className="w-full px-3 py-2 rounded-lg border border-gray-300 text-sm outline-none focus:border-indigo-400 focus:ring-2 focus:ring-indigo-100"
                />
              </div>
              <div>
                <label className="block text-xs text-gray-500 mb-1">App ID</label>
                <input
                  value={formData.mcpAppId}
                  onChange={e => setFormData(prev => ({ ...prev, mcpAppId: e.target.value }))}
                  placeholder="cli_xxx"
                  className="w-full px-3 py-2 rounded-lg border border-gray-300 text-sm outline-none focus:border-indigo-400 focus:ring-2 focus:ring-indigo-100 font-mono text-xs"
                />
              </div>
              <div>
                <label className="block text-xs text-gray-500 mb-1">App Secret</label>
                <input
                  type="password"
                  value={formData.mcpAppSecret}
                  onChange={e => setFormData(prev => ({ ...prev, mcpAppSecret: e.target.value }))}
                  placeholder="输入 App Secret…"
                  className="w-full px-3 py-2 rounded-lg border border-gray-300 text-sm outline-none focus:border-indigo-400 focus:ring-2 focus:ring-indigo-100"
                />
              </div>
            </div>
          </section>
        </div>

        <div className="px-6 py-4 border-t border-gray-200 flex justify-end gap-3">
          <button
            onClick={toggleSettings}
            className="px-4 py-2 rounded-lg border border-gray-300 text-sm text-gray-700 hover:bg-gray-50 transition-colors"
          >
            取消
          </button>
          <button
            onClick={handleSave}
            className="px-4 py-2 rounded-lg bg-indigo-600 hover:bg-indigo-700 text-white text-sm transition-colors"
          >
            保存
          </button>
        </div>
      </div>
    </div>
  )
}

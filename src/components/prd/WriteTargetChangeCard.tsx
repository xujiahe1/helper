import { useState } from 'react'
import { AlertTriangle, ArrowRight, Loader2 } from 'lucide-react'
import type { WriteTargetChangeCardData } from '../../types/guided-prd'
import { useChatStore } from '../../store'
import { executeMigration } from '../../services/agentAction'

interface WriteTargetChangeCardProps {
  data: WriteTargetChangeCardData
  isDone: boolean
  msgId: string
  conversationId: string
}

export function WriteTargetChangeCard({
  data, isDone, msgId, conversationId,
}: WriteTargetChangeCardProps) {
  const settings = useChatStore((s) => s.settings)
  const markDone = useChatStore((s) => s.markPrdCardDone)
  const updateMsg = useChatStore((s) => s.updateMessage)

  const [migrateChoice, setMigrateChoice] = useState<'migrate' | 'keep_old' | null>(
    data.migrateChoice
  )
  const [conflictChoice, setConflictChoice] = useState<'overwrite' | 'append' | 'skip' | null>(
    data.conflictChoice ?? 'append'  // 默认追加
  )
  const [executing, setExecuting] = useState(false)

  const resolved = isDone || data.resolved
  const hasWritten = data.writtenSectionCount > 0
  const hasConflict = data.conflictingHeadings.length > 0

  // 是否需要选冲突处理方式：只有迁移 + 新文档有冲突时才需要
  const needConflictChoice = migrateChoice === 'migrate' && hasConflict

  const canConfirm =
    (!hasWritten || migrateChoice !== null) &&
    (!needConflictChoice || conflictChoice !== null)

  const handleConfirm = async () => {
    if (!canConfirm || executing) return
    setExecuting(true)

    // 更新卡片数据（持久化用户选择）
    const updatedCard: WriteTargetChangeCardData = {
      ...data,
      migrateChoice: migrateChoice ?? 'keep_old',
      conflictChoice: conflictChoice,
      resolved: true,
    }
    updateMsg(conversationId, msgId, { prdCard: updatedCard })
    markDone(conversationId, msgId)

    // 执行迁移
    if (migrateChoice === 'migrate') {
      try {
        await executeMigration(updatedCard, conversationId, settings)
      } catch (err) {
        console.error('[WriteTargetChange] 迁移失败:', err)
      }
    }

    setExecuting(false)
  }

  return (
    <div className="border border-orange-200 rounded-xl overflow-hidden bg-white shadow-sm max-w-xl">
      {/* 头部 */}
      <div className="bg-orange-500 text-white px-4 py-2.5 flex items-center gap-2">
        <AlertTriangle size={14} />
        <span className="font-medium text-sm">写入目标已更改</span>
        {resolved && (
          <span className="ml-auto text-xs bg-orange-700/50 px-2 py-0.5 rounded-full">已处理</span>
        )}
      </div>

      {/* 目标变更展示 */}
      <div className="px-4 py-3 flex items-center gap-2 text-sm border-b border-gray-100">
        <div className="flex-1 min-w-0">
          <p className="text-xs text-gray-400 mb-0.5">原目标</p>
          <p className="text-gray-700 truncate">{data.oldTarget.description}</p>
        </div>
        <ArrowRight size={16} className="text-gray-300 flex-shrink-0" />
        <div className="flex-1 min-w-0">
          <p className="text-xs text-gray-400 mb-0.5">新目标</p>
          <p className="text-gray-700 truncate font-medium">{data.newTarget.description}</p>
        </div>
      </div>

      {/* 迁移选项（有已写内容时显示） */}
      {hasWritten && !resolved && (
        <div className="px-4 py-3 border-b border-gray-100">
          <p className="text-xs font-medium text-gray-600 mb-2">
            旧目标已写入 {data.writtenSectionCount} 个章节，如何处理？
          </p>
          <div className="space-y-1.5">
            {[
              { value: 'migrate' as const, label: '搬到新目标文档', desc: '将已写内容迁移过去' },
              { value: 'keep_old' as const, label: '保留在原文档', desc: '新目标从空白开始写' },
            ].map((opt) => (
              <label key={opt.value} className="flex items-start gap-2 cursor-pointer group">
                <input
                  type="radio"
                  name={`migrate_${msgId}`}
                  value={opt.value}
                  checked={migrateChoice === opt.value}
                  onChange={() => setMigrateChoice(opt.value)}
                  className="mt-0.5 accent-orange-500"
                />
                <div>
                  <span className="text-sm text-gray-800">{opt.label}</span>
                  <span className="text-xs text-gray-400 ml-1.5">{opt.desc}</span>
                </div>
              </label>
            ))}
          </div>
        </div>
      )}

      {/* 冲突处理（新文档有同名章节 + 用户选择迁移时显示） */}
      {hasConflict && needConflictChoice && !resolved && (
        <div className="px-4 py-3 border-b border-gray-100 bg-amber-50/50">
          <p className="text-xs font-medium text-gray-600 mb-1">
            新文档已有以下章节，迁移时遇到重名如何处理？
          </p>
          <p className="text-xs text-gray-400 mb-2">
            {data.conflictingHeadings.map((h) => `「${h}」`).join('、')}
          </p>
          <div className="space-y-1.5">
            {[
              { value: 'append' as const, label: '追加到章节末尾', desc: '（推荐）保留原有内容' },
              { value: 'overwrite' as const, label: '覆盖已有内容', desc: '用迁移内容替换' },
              { value: 'skip' as const, label: '跳过重名章节', desc: '只迁移新章节' },
            ].map((opt) => (
              <label key={opt.value} className="flex items-start gap-2 cursor-pointer">
                <input
                  type="radio"
                  name={`conflict_${msgId}`}
                  value={opt.value}
                  checked={conflictChoice === opt.value}
                  onChange={() => setConflictChoice(opt.value)}
                  className="mt-0.5 accent-amber-500"
                />
                <div>
                  <span className="text-sm text-gray-800">{opt.label}</span>
                  <span className="text-xs text-gray-400 ml-1.5">{opt.desc}</span>
                </div>
              </label>
            ))}
          </div>
        </div>
      )}

      {/* 确认按钮 */}
      {!resolved && (
        <div className="px-4 py-3 flex justify-end">
          <button
            onClick={handleConfirm}
            disabled={!canConfirm || executing}
            className="flex items-center gap-1.5 px-4 py-1.5 bg-orange-500 text-white text-sm rounded-lg hover:bg-orange-600 disabled:opacity-40 font-medium transition-colors"
          >
            {executing && <Loader2 size={13} className="animate-spin" />}
            确认并继续
          </button>
        </div>
      )}

      {/* 已处理状态 */}
      {resolved && (
        <div className="px-4 py-2.5 text-xs text-gray-400">
          {data.migrateChoice === 'migrate' ? '已迁移到新目标' : '保留在原文档，继续写入新目标'}
        </div>
      )}
    </div>
  )
}

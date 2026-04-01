import { useState } from 'react'
import { ChevronDown, ChevronUp, Check, X } from 'lucide-react'
import type { FeatureConfirmCardData } from '../../types/guided-prd'

interface FeatureConfirmCardProps {
  data: FeatureConfirmCardData
  isDone: boolean
  msgId: string
  onConfirm: (cardMsgId: string, featureId: string, featureTitle: string, summary: string) => void
  onReject: (cardMsgId: string, featureTitle: string, feedback: string) => void
}

export function FeatureConfirmCard({ data, isDone, msgId, onConfirm, onReject }: FeatureConfirmCardProps) {
  const [expanded, setExpanded] = useState(true)
  const [rejecting, setRejecting] = useState(false)
  const [feedback, setFeedback] = useState('')
  const confirmed = isDone || data.confirmed

  return (
    <div className="border border-amber-200 rounded-xl overflow-hidden bg-white shadow-sm max-w-xl">
      {/* 头部 */}
      <div className="bg-amber-500 text-white px-4 py-2.5 flex items-center justify-between">
        <span className="font-medium text-sm">📋 确认：{data.featureTitle}</span>
        {confirmed && (
          <span className="text-xs bg-amber-700/60 px-2 py-0.5 rounded-full">已确认</span>
        )}
      </div>

      {/* 摘要内容 */}
      <div>
        <button
          className="w-full px-4 py-2 flex items-center justify-between text-xs text-gray-500 hover:bg-gray-50 transition-colors"
          onClick={() => setExpanded(!expanded)}
        >
          <span className="font-medium">功能规格摘要</span>
          {expanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
        </button>
        {expanded && (
          <div className="px-4 py-3 bg-gray-50 text-xs text-gray-700 whitespace-pre-wrap leading-relaxed border-t border-gray-100 max-h-48 overflow-y-auto">
            {data.summary}
          </div>
        )}
      </div>

      {/* 操作区 */}
      {!confirmed && (
        <div className="px-4 py-3 border-t border-gray-100">
          {!rejecting ? (
            <div className="flex items-center gap-2">
              <button
                onClick={() => onConfirm(msgId, data.featureId, data.featureTitle, data.summary)}
                className="flex items-center gap-1.5 px-3 py-1.5 bg-green-600 text-white text-xs rounded-lg hover:bg-green-700 font-medium transition-colors"
              >
                <Check size={13} /> 确认，继续
              </button>
              <button
                onClick={() => setRejecting(true)}
                className="flex items-center gap-1.5 px-3 py-1.5 text-gray-500 hover:text-gray-700 text-xs rounded-lg hover:bg-gray-100 transition-colors"
              >
                <X size={13} /> 需要调整
              </button>
            </div>
          ) : (
            <div className="space-y-2">
              <textarea
                autoFocus
                className="w-full border border-gray-200 rounded-lg p-2 text-xs resize-none focus:outline-none focus:ring-1 focus:ring-amber-300"
                rows={2}
                placeholder="说明需要调整的地方…"
                value={feedback}
                onChange={(e) => setFeedback(e.target.value)}
              />
              <div className="flex gap-2">
                <button
                  disabled={!feedback.trim()}
                  onClick={() => { onReject(msgId, data.featureTitle, feedback); setFeedback('') }}
                  className="px-3 py-1 bg-amber-500 text-white text-xs rounded-lg hover:bg-amber-600 disabled:opacity-40 transition-colors"
                >
                  提交反馈
                </button>
                <button
                  onClick={() => { setRejecting(false); setFeedback('') }}
                  className="px-3 py-1 text-gray-400 hover:text-gray-600 text-xs"
                >
                  取消
                </button>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

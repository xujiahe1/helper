import { useState } from 'react'
import { ChevronDown, ChevronUp, Lock } from 'lucide-react'
import type { FeatureDraftCardData } from '../../types/guided-prd'

interface FeatureDraftCardProps {
  data: FeatureDraftCardData
  isDone: boolean
  msgId: string
  onSubmit: (cardMsgId: string, answer: string) => void
}

export function FeatureDraftCard({ data, isDone, msgId, onSubmit }: FeatureDraftCardProps) {
  const [draftExpanded, setDraftExpanded] = useState(false)
  const [answer, setAnswer] = useState('')
  const submitted = isDone || data.submitted

  return (
    <div className="border border-blue-200 rounded-xl overflow-hidden bg-white shadow-sm max-w-xl">
      {/* 头部 */}
      <div className="bg-blue-600 text-white px-4 py-2.5 flex items-center justify-between">
        <span className="font-medium text-sm">🔍 {data.featureTitle}</span>
        {submitted && (
          <span className="text-xs bg-blue-800/60 px-2 py-0.5 rounded-full flex items-center gap-1">
            <Lock size={10} /> 已提交
          </span>
        )}
      </div>

      {/* 折叠草稿 */}
      {data.draft && (
        <div className="border-b border-gray-100">
          <button
            className="w-full px-4 py-2 flex items-center justify-between text-xs text-gray-500 hover:bg-gray-50 transition-colors"
            onClick={() => setDraftExpanded(!draftExpanded)}
          >
            <span className="font-medium">查看当前草稿假设</span>
            {draftExpanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
          </button>
          {draftExpanded && (
            <div className="px-4 py-3 bg-gray-50 text-xs text-gray-600 whitespace-pre-wrap max-h-48 overflow-y-auto leading-relaxed border-t border-gray-100">
              {data.draft}
            </div>
          )}
        </div>
      )}

      {/* 追问问题 */}
      <div className="px-4 py-3">
        <p className="text-xs font-medium text-gray-500 mb-2">请回答以下问题：</p>
        <ol className="space-y-1.5">
          {data.questions.map((q, i) => (
            <li key={i} className="text-sm text-gray-800 leading-relaxed">
              <span className="text-blue-500 font-medium mr-1">{i + 1}.</span>
              {q}
            </li>
          ))}
        </ol>
      </div>

      {/* 回答区域 */}
      {submitted ? (
        <div className="px-4 pb-3 text-xs text-gray-400 flex items-center gap-1.5 border-t border-gray-100 pt-2.5">
          <Lock size={11} />
          已提交，等待 AI 继续
        </div>
      ) : (
        <div className="px-4 pb-4 border-t border-gray-100 pt-3">
          <textarea
            className="w-full border border-gray-200 rounded-lg p-3 text-sm resize-none focus:outline-none focus:ring-2 focus:ring-blue-300 focus:border-transparent transition-all"
            rows={3}
            placeholder="输入你的回答… (⌘↵ 提交)"
            value={answer}
            onChange={(e) => setAnswer(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && (e.metaKey || e.ctrlKey) && answer.trim()) {
                onSubmit(msgId, answer)
                setAnswer('')
              }
            }}
          />
          <div className="flex items-center justify-between mt-2">
            <button
              className="text-xs text-gray-400 hover:text-gray-600 transition-colors"
              onClick={() => { onSubmit(msgId, '（跳过，保持草稿假设不变）'); setAnswer('') }}
            >
              跳过
            </button>
            <button
              className="px-3 py-1.5 bg-blue-600 text-white text-xs rounded-lg hover:bg-blue-700 disabled:opacity-40 font-medium transition-colors"
              disabled={!answer.trim()}
              onClick={() => { onSubmit(msgId, answer); setAnswer('') }}
            >
              提交 ⌘↵
            </button>
          </div>
        </div>
      )}
    </div>
  )
}

import { useState } from 'react'
import { ChevronDown, ChevronUp, CheckCircle2 } from 'lucide-react'
import type { FeatureLockedCardData } from '../../types/guided-prd'

interface FeatureLockedCardProps {
  data: FeatureLockedCardData
}

export function FeatureLockedCard({ data }: FeatureLockedCardProps) {
  const [expanded, setExpanded] = useState(false)

  return (
    <div className="border border-green-200 rounded-xl overflow-hidden bg-white shadow-sm max-w-xl">
      <div className="bg-green-600 text-white px-4 py-2.5 flex items-center gap-2">
        <CheckCircle2 size={15} />
        <span className="font-medium text-sm flex-1">{data.featureTitle}</span>
        <span className="text-xs bg-green-800/50 px-2 py-0.5 rounded-full">已锁定</span>
      </div>

      <div>
        <button
          className="w-full px-4 py-2.5 flex items-center justify-between text-xs text-gray-500 hover:bg-gray-50 transition-colors"
          onClick={() => setExpanded(!expanded)}
        >
          <span className="font-medium">查看功能摘要</span>
          {expanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
        </button>
        {expanded && (
          <div className="px-4 pb-3 pt-1 text-xs text-gray-600 leading-relaxed border-t border-gray-100 bg-gray-50 whitespace-pre-wrap">
            {data.summary}
          </div>
        )}
      </div>
    </div>
  )
}

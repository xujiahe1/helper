import { memo, useState } from 'react'
import { Paperclip, FileText, BookOpen, ChevronDown, ChevronUp } from 'lucide-react'

interface PrdMatch {
  docId?: string
  docTitle: string
  matchedEntities: string[]
}

interface DocSource {
  docId: string
  title?: string
}

interface Props {
  /** PRD 知识匹配结果 */
  prdMatches?: PrdMatch[]
  /** 关联文档来源 */
  docSources?: DocSource[]
}

export const SourceIndicator = memo(function SourceIndicator({ prdMatches, docSources }: Props) {
  const [expanded, setExpanded] = useState(false)

  const hasPrd = prdMatches && prdMatches.length > 0
  const hasDoc = docSources && docSources.length > 0

  if (!hasPrd && !hasDoc) return null

  const totalSources = (prdMatches?.length || 0) + (docSources?.length || 0)

  return (
    <div className="mt-3 mb-1">
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex items-center gap-1.5 text-xs text-gray-500 hover:text-gray-700 transition-colors"
      >
        <Paperclip size={12} />
        <span>参考了 {totalSources} 个来源</span>
        {expanded ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
      </button>

      {expanded && (
        <div className="mt-2 p-3 bg-gray-50 rounded-lg border border-gray-100 space-y-2">
          {/* 关联文档来源 */}
          {hasDoc && docSources!.map((doc, idx) => (
            <div key={`doc-${idx}`} className="flex items-start gap-2 text-xs">
              <FileText size={13} className="text-indigo-500 mt-0.5 flex-shrink-0" />
              <div>
                <span className="text-gray-600">关联文档：</span>
                <span className="text-gray-800 font-medium">
                  {doc.title || doc.docId}
                </span>
              </div>
            </div>
          ))}

          {/* PRD 知识匹配 */}
          {hasPrd && prdMatches!.map((match, idx) => (
            <div key={`prd-${idx}`} className="flex items-start gap-2 text-xs">
              <BookOpen size={13} className="text-indigo-500 mt-0.5 flex-shrink-0" />
              <div>
                <span className="text-gray-600">PRD 知识：</span>
                <span className="text-gray-800 font-medium">{match.docTitle}</span>
                {match.matchedEntities.length > 0 && (
                  <span className="text-gray-400 ml-1">
                    (匹配: {match.matchedEntities.slice(0, 3).join('、')}
                    {match.matchedEntities.length > 3 && ` 等${match.matchedEntities.length}项`})
                  </span>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
})

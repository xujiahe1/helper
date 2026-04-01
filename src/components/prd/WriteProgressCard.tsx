import { CheckCircle2, Loader2, AlertCircle, Clock } from 'lucide-react'
import type { WriteProgressCardData } from '../../types/guided-prd'

interface WriteProgressCardProps {
  data: WriteProgressCardData
}

const statusIcon = (status: WriteProgressCardData['sections'][0]['status']) => {
  switch (status) {
    case 'done':    return <CheckCircle2 size={13} className="text-green-500" />
    case 'writing': return <Loader2 size={13} className="text-blue-500 animate-spin" />
    case 'failed':  return <AlertCircle size={13} className="text-red-500" />
    default:        return <Clock size={13} className="text-gray-300" />
  }
}

export function WriteProgressCard({ data }: WriteProgressCardProps) {
  const allDone = data.sections.every((s) => s.status === 'done')
  const hasFailed = data.sections.some((s) => s.status === 'failed')
  const isWriting = data.sections.some((s) => s.status === 'writing')

  return (
    <div className="border border-violet-200 rounded-xl overflow-hidden bg-white shadow-sm max-w-xl">
      {/* 头部 */}
      <div className={`px-4 py-2.5 flex items-center gap-2 text-white text-sm font-medium ${
        allDone ? 'bg-green-600' : hasFailed ? 'bg-red-500' : 'bg-violet-600'
      }`}>
        {isWriting && <Loader2 size={14} className="animate-spin" />}
        {allDone && <CheckCircle2 size={14} />}
        <span>
          {allDone ? '写入完成' : hasFailed ? '部分写入失败' : '写入文档中…'}
        </span>
        {data.docTitle && (
          <span className="text-xs opacity-75 ml-auto">{data.docTitle}</span>
        )}
      </div>

      {/* 章节列表 */}
      <div className="divide-y divide-gray-50">
        {data.sections.map((s, i) => (
          <div key={i} className="px-4 py-2 flex items-center gap-2">
            {statusIcon(s.status)}
            <span className={`text-xs flex-1 ${
              s.status === 'done' ? 'text-gray-500' :
              s.status === 'failed' ? 'text-red-500' :
              s.status === 'writing' ? 'text-blue-600 font-medium' :
              'text-gray-400'
            }`}>
              {s.heading}
            </span>
          </div>
        ))}
      </div>

      {/* 文档链接（完成后显示） */}
      {allDone && data.docId && (
        <div className="px-4 py-2.5 bg-green-50 border-t border-green-100">
          <span className="text-xs text-green-600">
            已写入文档，可在 KM 中查看
          </span>
        </div>
      )}
    </div>
  )
}

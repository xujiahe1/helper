import { useState, memo } from 'react'
import { Table2, ChevronDown } from 'lucide-react'
import type { SheetData, MergeRange } from '../types'
import { Dropdown } from './Dropdown'

const MAX_PREVIEW_ROWS = 50
const MAX_PREVIEW_COLS = 12

interface Props {
  sheets: SheetData[]
  fileName: string
}

/**
 * 将 merges 列表转换为两个查询结构（O(1) 查询）：
 *  - anchorMap: "r,c" → { rowSpan, colSpan }  // 该格是合并区域的左上角锚点
 *  - hiddenSet: "r,c"                           // 该格被合并区域覆盖，不渲染
 *
 * 合并区域跨越视口边界时，rowSpan/colSpan 会裁剪到视口边界。
 */
function buildMergeMap(
  merges: MergeRange[] | undefined,
  maxRows: number,
  maxCols: number,
): {
  anchorMap: Map<string, { rowSpan: number; colSpan: number }>
  hiddenSet: Set<string>
} {
  const anchorMap = new Map<string, { rowSpan: number; colSpan: number }>()
  const hiddenSet = new Set<string>()
  if (!merges?.length) return { anchorMap, hiddenSet }

  for (const m of merges) {
    // 合并区域完全在视口之外，跳过
    if (m.sr >= maxRows || m.sc >= maxCols || m.er < 0 || m.ec < 0) continue

    // 裁剪到视口边界
    const visibleEr = Math.min(m.er, maxRows - 1)
    const visibleEc = Math.min(m.ec, maxCols - 1)
    const rowSpan = visibleEr - m.sr + 1
    const colSpan = visibleEc - m.sc + 1

    // 单格无需处理
    if (rowSpan <= 1 && colSpan <= 1) continue

    // 注册左上角锚点
    anchorMap.set(`${m.sr},${m.sc}`, { rowSpan, colSpan })

    // 标记被覆盖的格（除锚点自身外）
    for (let r = m.sr; r <= visibleEr; r++) {
      for (let c = m.sc; c <= visibleEc; c++) {
        if (r !== m.sr || c !== m.sc) hiddenSet.add(`${r},${c}`)
      }
    }
  }

  return { anchorMap, hiddenSet }
}

export const ExcelPreview = memo(function ExcelPreview({ sheets, fileName }: Props) {
  const [activeSheet, setActiveSheet] = useState(0)
  const [showSheetDropdown, setShowSheetDropdown] = useState(false)

  if (!sheets.length) return null
  const sheet = sheets[activeSheet]
  if (!sheet) return null

  const displayCols = Math.min(sheet.headers.length, MAX_PREVIEW_COLS)
  const displayRows = sheet.rows.slice(0, MAX_PREVIEW_ROWS)
  const hasMoreRows = sheet.rows.length > MAX_PREVIEW_ROWS
  const hasMoreCols = sheet.headers.length > MAX_PREVIEW_COLS

  // 构建合并单元格映射（无 merges 时开销极小）
  const { anchorMap, hiddenSet } = buildMergeMap(sheet.merges, displayRows.length, displayCols)
  // header 行合并映射（行坐标固定为 0，只取列方向）
  const { anchorMap: headerAnchorMap, hiddenSet: headerHiddenSet } = buildMergeMap(
    sheet.headerMerges,
    1,          // header 只有 1 行
    displayCols,
  )

  return (
    <div className="my-2 rounded-lg border border-emerald-200 bg-emerald-50/30 overflow-hidden min-w-0 max-w-full">
      {/* Header */}
      <div className="flex items-center justify-between px-3 py-2 bg-emerald-50 border-b border-emerald-200">
        <div className="flex items-center gap-2 text-xs font-medium text-emerald-700">
          <Table2 size={13} />
          <span className="truncate max-w-[200px]">{fileName}</span>
          <span className="text-emerald-500">
            {sheet.rows.length} 行 × {sheet.headers.length} 列
          </span>
        </div>
        {sheets.length > 1 && (
          <div className="relative">
            <button
              onClick={() => setShowSheetDropdown(!showSheetDropdown)}
              className="flex items-center gap-1 px-2 py-1 rounded text-xs text-emerald-600 hover:bg-emerald-100 transition-colors"
            >
              {sheet.name}
              <ChevronDown size={12} />
            </button>
            <Dropdown open={showSheetDropdown} onClose={() => setShowSheetDropdown(false)} className="w-36">
              {sheets.map((s, i) => (
                <button
                  key={i}
                  onClick={() => { setActiveSheet(i); setShowSheetDropdown(false) }}
                  className={`w-full text-left px-3 py-1.5 text-xs hover:bg-gray-50 transition-colors ${
                    i === activeSheet ? 'text-emerald-600 bg-emerald-50 font-medium' : 'text-gray-700'
                  }`}
                >
                  {s.name}
                </button>
              ))}
            </Dropdown>
          </div>
        )}
      </div>

      {/* Table */}
      <div className="overflow-x-auto max-h-[320px] overflow-y-auto">
        <table className="text-xs border-collapse">
          <thead className="sticky top-0">
            <tr className="bg-emerald-100/80">
              <th className="px-2 py-1.5 text-left text-emerald-600 font-medium border-b border-emerald-200 w-10">#</th>
              {Array.from({ length: displayCols }, (_, i) => {
                if (headerHiddenSet.has(`0,${i}`)) return null
                const anchor = headerAnchorMap.get(`0,${i}`)
                return (
                  <th
                    key={i}
                    colSpan={anchor?.colSpan}
                    className="px-2 py-1.5 text-left text-emerald-600 font-medium border border-emerald-200 whitespace-nowrap max-w-[160px] truncate"
                  >
                    {sheet.headers[i] || '(空)'}
                  </th>
                )
              })}
              {hasMoreCols && (
                <th className="px-2 py-1.5 text-center text-emerald-400 border-b border-emerald-200">…</th>
              )}
            </tr>
          </thead>
          <tbody>
            {displayRows.map((row, ri) => (
              <tr key={ri} className={ri % 2 === 0 ? 'bg-white' : 'bg-emerald-50/30'}>
                <td className="px-2 py-1 text-gray-400 border-b border-gray-100">{ri + 1}</td>
                {Array.from({ length: displayCols }, (_, ci) => {
                  // 被合并区域覆盖的格：不渲染
                  if (hiddenSet.has(`${ri},${ci}`)) return null
                  const anchor = anchorMap.get(`${ri},${ci}`)
                  return (
                    <td
                      key={ci}
                      rowSpan={anchor?.rowSpan}
                      colSpan={anchor?.colSpan}
                      className="px-2 py-1 text-gray-700 border border-gray-100 whitespace-nowrap max-w-[160px] truncate align-top"
                    >
                      {row[ci] == null ? '' : String(row[ci])}
                    </td>
                  )
                })}
                {hasMoreCols && (
                  <td className="px-2 py-1 text-center text-gray-400 border-b border-gray-100">…</td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Footer */}
      {(hasMoreRows || hasMoreCols) && (
        <div className="px-3 py-1.5 bg-emerald-50 border-t border-emerald-200 text-xs text-emerald-500">
          {hasMoreRows && '仅预览前 ' + MAX_PREVIEW_ROWS + ' 行'}
          {hasMoreRows && hasMoreCols && '，'}
          {hasMoreCols && '仅显示前 ' + MAX_PREVIEW_COLS + ' 列'}
        </div>
      )}
    </div>
  )
})

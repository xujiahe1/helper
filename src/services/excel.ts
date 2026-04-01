import * as XLSX from 'xlsx'
import type { SheetData, CellValue, MergeRange } from '../types'
import { dataUrlToBytes } from './utils'

function workbookToSheets(workbook: XLSX.WorkBook): SheetData[] {
  return workbook.SheetNames.map(name => {
    const sheet = workbook.Sheets[name]
    const json: unknown[][] = XLSX.utils.sheet_to_json(sheet, { header: 1, defval: null })
    if (json.length === 0) return { name, headers: [], rows: [] }
    const headers = (json[0] || []).map(h => (h == null ? '' : String(h)))
    const rows = json.slice(1).map(row =>
      (row as unknown[]).map(cell => {
        if (cell == null) return null
        if (typeof cell === 'number' || typeof cell === 'boolean') return cell
        return String(cell)
      }),
    )

    // 读取并转换合并单元格信息（sheet['!merges'] 行号包含 header 行，需整体 -1 偏移）
    const rawMerges: XLSX.Range[] | undefined = sheet['!merges']
    let merges: MergeRange[] | undefined
    let headerMerges: MergeRange[] | undefined
    if (rawMerges?.length) {
      const bodyConverted: MergeRange[] = []
      const headerConverted: MergeRange[] = []
      for (const m of rawMerges) {
        const sr = m.s.r - 1  // 偏移 header 行
        const er = m.e.r - 1
        if (er < 0) {
          // 合并区域完全在 header 行内（纯横向合并 header 列）
          headerConverted.push({ sr: 0, sc: m.s.c, er: 0, ec: m.e.c })
        } else {
          // body 区域合并（含跨越 header 行的情况，sr clamp 到 0）
          bodyConverted.push({ sr: Math.max(sr, 0), sc: m.s.c, er, ec: m.e.c })
        }
      }
      if (bodyConverted.length > 0) merges = bodyConverted
      if (headerConverted.length > 0) {
        headerMerges = headerConverted
        // header 行横向合并：被覆盖列的列名填充为锚点列名，保证列名完整
        for (const m of headerConverted) {
          const anchorName = headers[m.sc] ?? ''
          for (let c = m.sc + 1; c <= m.ec; c++) {
            if (!headers[c]) headers[c] = anchorName
          }
        }
      }

      // 将合并区域的左上角值填充到被覆盖的空格中
      // SheetJS 只在左上角保留值，其余格为 null，导致 AI 摘要统计出现大量"空值"
      if (bodyConverted.length > 0) {
        for (const m of bodyConverted) {
          const anchorValue = rows[m.sr]?.[m.sc] ?? null
          for (let r = m.sr; r <= m.er; r++) {
            for (let c = m.sc; c <= m.ec; c++) {
              if (r === m.sr && c === m.sc) continue  // 锚点自身跳过
              if (rows[r]) rows[r][c] = anchorValue
            }
          }
        }
      }
    }

    return { name, headers, rows, merges, headerMerges }
  })
}

export function parseExcelFromDataUrl(dataUrl: string): SheetData[] {
  const bytes = dataUrlToBytes(dataUrl)
  if (bytes.length === 0) return []
  return workbookToSheets(XLSX.read(bytes, { type: 'array' }))
}

export function parseExcelFromFile(file: File): Promise<SheetData[]> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => {
      try {
        const bytes = new Uint8Array(reader.result as ArrayBuffer)
        resolve(workbookToSheets(XLSX.read(bytes, { type: 'array' })))
      } catch (e) {
        reject(e)
      }
    }
    reader.onerror = () => reject(reader.error)
    reader.readAsArrayBuffer(file)
  })
}

function rowsToMarkdownTable(headers: string[], rows: CellValue[][]): string {
  const header = '| ' + headers.join(' | ') + ' |'
  const separator = '| ' + headers.map(() => '---').join(' | ') + ' |'
  const body = rows.map(row => {
    const cells = headers.map((_, i) => {
      const v = row[i]
      return v == null ? '' : String(v)
    })
    return '| ' + cells.join(' | ') + ' |'
  })
  return header + '\n' + separator + '\n' + body.join('\n')
}

// 只发送 Schema 摘要（列信息 + 枚举/统计 + 少量样本行），不发送全量数据
export function sheetToAISummary(sheet: SheetData): string {
  if (sheet.headers.length === 0) return '（空表）'
  const total = sheet.rows.length

  const lines: string[] = [
    '## 工作表: ' + sheet.name + '（' + total + ' 行 × ' + sheet.headers.length + ' 列）',
    '',
    '### 列信息',
  ]

  for (let c = 0; c < sheet.headers.length; c++) {
    const colName = sheet.headers[c] || '列' + (c + 1)
    const values = sheet.rows.map(r => r[c])
    const nonNull = values.filter(v => v != null)
    const nums = nonNull.filter(v => typeof v === 'number') as number[]

    let info = colName + ': ' + nonNull.length + '/' + total + ' 非空'

    if (nums.length > nonNull.length * 0.5 && nums.length > 0) {
      const min = Math.min(...nums)
      const max = Math.max(...nums)
      const avg = nums.reduce((a, b) => a + b, 0) / nums.length
      info += ' | 数值 | 最小=' + min + ' | 最大=' + max + ' | 平均=' + avg.toFixed(2)
    } else {
      const freq = new Map<string, number>()
      for (const v of nonNull) {
        const s = String(v)
        freq.set(s, (freq.get(s) || 0) + 1)
      }
      const uniqueCount = freq.size
      info += ' | 文本 | 唯一值=' + uniqueCount

      if (uniqueCount <= 50) {
        const all = [...freq.keys()].map(v => '"' + v + '"').join(', ')
        info += ' | 全部枚举: ' + all
      } else {
        const sorted = [...freq.entries()].sort((a, b) => b[1] - a[1])
        const top = sorted.slice(0, 30).map(([v, cnt]) => '"' + v + '"(' + cnt + ')').join(', ')
        info += ' | 前30高频: ' + top
      }
    }
    lines.push('- ' + info)
  }

  const sampleCount = Math.min(5, total)
  if (sampleCount > 0) {
    lines.push('', '### 数据示例（前' + sampleCount + '行）', '')
    lines.push(rowsToMarkdownTable(sheet.headers, sheet.rows.slice(0, sampleCount)))
  }

  return lines.join('\n')
}

function sheetToCSV(sheet: SheetData): string {
  const escape = (v: CellValue) => {
    if (v == null) return ''
    const s = String(v)
    if (s.includes(',') || s.includes('"') || s.includes('\n')) {
      return '"' + s.replace(/"/g, '""') + '"'
    }
    return s
  }
  const lines = [sheet.headers.map(h => escape(h)).join(',')]
  for (const row of sheet.rows) {
    lines.push(sheet.headers.map((_, i) => escape(row[i] ?? null)).join(','))
  }
  return lines.join('\n')
}


function deduplicateRows(sheet: SheetData, columns: number[]): SheetData {
  const seen = new Set<string>()
  const deduped = sheet.rows.filter(row => {
    const key = columns.map(c => String(row[c] ?? '')).join('\0')
    if (seen.has(key)) return false
    seen.add(key)
    return true
  })
  return { ...sheet, rows: deduped }
}

function deleteColumns(sheet: SheetData, columnIndices: number[]): SheetData {
  const keep = sheet.headers.map((_, i) => i).filter(i => !columnIndices.includes(i))
  return {
    ...sheet,
    headers: keep.map(i => sheet.headers[i]),
    rows: sheet.rows.map(row => keep.map(i => row[i] ?? null)),
  }
}


function sortByColumn(sheet: SheetData, column: number, ascending = true): SheetData {
  const sorted = [...sheet.rows].sort((a, b) => {
    const va = a[column]
    const vb = b[column]
    if (va == null && vb == null) return 0
    if (va == null) return 1
    if (vb == null) return -1
    if (typeof va === 'number' && typeof vb === 'number') {
      return ascending ? va - vb : vb - va
    }
    const sa = String(va)
    const sb = String(vb)
    return ascending ? sa.localeCompare(sb) : sb.localeCompare(sa)
  })
  return { ...sheet, rows: sorted }
}

export function exportToXlsx(sheets: SheetData[]): Blob {
  const wb = XLSX.utils.book_new()
  for (const sheet of sheets) {
    const data = [sheet.headers, ...sheet.rows]
    const ws = XLSX.utils.aoa_to_sheet(data)
    // 写回合并单元格信息
    const allMerges: XLSX.Range[] = []
    // header 行合并（原始行号为 0，无需偏移）
    if (sheet.headerMerges?.length) {
      for (const m of sheet.headerMerges) {
        allMerges.push({ s: { r: 0, c: m.sc }, e: { r: 0, c: m.ec } } as XLSX.Range)
      }
    }
    // body 区域合并（行号 +1 还原 header 行偏移）
    if (sheet.merges?.length) {
      for (const m of sheet.merges) {
        allMerges.push({ s: { r: m.sr + 1, c: m.sc }, e: { r: m.er + 1, c: m.ec } } as XLSX.Range)
      }
    }
    if (allMerges.length > 0) ws['!merges'] = allMerges
    XLSX.utils.book_append_sheet(wb, ws, sheet.name)
  }
  const buf = XLSX.write(wb, { bookType: 'xlsx', type: 'array' })
  return new Blob([buf], { type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' })
}

export function exportToCSVBlob(sheet: SheetData): Blob {
  return new Blob([sheetToCSV(sheet)], { type: 'text/csv;charset=utf-8' })
}


/* ────────────────────────────────────────────
   AI-driven Excel Operations (18 ops)
   ──────────────────────────────────────────── */

export interface ExcelOperation {
  op: string
  // 各操作的参数以可选字段形式存在，由 op 决定使用哪些
  column?: string
  columns?: (string | { column: string; ascending: boolean })[]
  operator?: string
  value?: string
  ascending?: boolean
  name?: string
  formula?: string
  rows?: CellValue[][]
  lookupColumn?: string
  fromSheet?: string
  fromKeyColumn?: string
  fromValueColumn?: string
  newColumnName?: string
  rename?: Record<string, string>
  find?: string
  replace?: string
  condition?: { column: string; operator: string; value?: string }
  targetColumn?: string
  newValue?: string
  separator?: string
  newColumns?: string[]
  newColumn?: string
  groupBy?: string[]
  aggregates?: { column: string; func: string }[]
  rowField?: string
  columnField?: string
  valueField?: string
  func?: string
  mode?: string
  joinKey?: string
  fillValue?: string
  sheets?: string[]  // 用于 keep_sheets / delete_sheets 操作
  direction?: 'vertical' | 'horizontal'  // merge_cells 用
  row?: number  // merge_cells 横向时指定行
}

function colIdx(headers: string[], name: string): number {
  const i = headers.indexOf(name)
  if (i >= 0) return i
  const lower = name.toLowerCase()
  return headers.findIndex(h => h.toLowerCase() === lower)
}

function matchCell(cell: CellValue, operator: string, value?: string): boolean {
  const s = cell == null ? '' : String(cell)
  const n = Number(cell)
  switch (operator) {
    case 'eq': return s === (value ?? '')
    case 'neq': return s !== (value ?? '')
    case 'contains': return s.includes(value ?? '')
    case 'startswith': return s.startsWith(value ?? '')
    case 'endswith': return s.endsWith(value ?? '')
    case 'gt': return !isNaN(n) && n > Number(value)
    case 'gte': return !isNaN(n) && n >= Number(value)
    case 'lt': return !isNaN(n) && n < Number(value)
    case 'lte': return !isNaN(n) && n <= Number(value)
    case 'empty': return cell == null || s === ''
    case 'notempty': return cell != null && s !== ''
    default: return true
  }
}

function evalFormula(formula: string, headers: string[], row: CellValue[]): CellValue {
  let expr = formula
  for (let i = 0; i < headers.length; i++) {
    const placeholder = '$' + headers[i] + '$'
    if (!expr.includes(placeholder)) continue
    const v = row[i]
    const replacement = v == null ? 'null'
      : typeof v === 'number' ? String(v)
      : typeof v === 'boolean' ? String(v)
      : JSON.stringify(v)
    expr = expr.split(placeholder).join(replacement)
  }
  try {
    return new Function('return (' + expr + ')')() as CellValue //nolint:forbidigo
  } catch {
    return null
  }
}

function applyOpInner(sheet: SheetData, op: ExcelOperation, allSheets: SheetData[]): SheetData {
  switch (op.op) {

    /* ── 查 ── */

    case 'filter': {
      const ci = colIdx(sheet.headers, op.column ?? '')
      if (ci < 0) return sheet
      return {
        ...sheet,
        rows: sheet.rows.filter(r => matchCell(r[ci], op.operator ?? 'eq', op.value)),
      }
    }

    case 'sort': {
      if (op.columns && Array.isArray(op.columns)) {
        let rows = [...sheet.rows]
        const specs = [...op.columns].reverse() as { column: string; ascending: boolean }[]
        for (const spec of specs) {
          const ci = colIdx(sheet.headers, spec.column)
          if (ci < 0) continue
          rows = sortByColumn({ ...sheet, rows }, ci, spec.ascending !== false).rows
        }
        return { ...sheet, rows }
      }
      const ci = colIdx(sheet.headers, op.column ?? '')
      if (ci < 0) return sheet
      return sortByColumn(sheet, ci, op.ascending !== false)
    }

    case 'dedup': {
      const cols = (op.columns as string[] | undefined) ?? []
      const indices = cols.map(c => colIdx(sheet.headers, c)).filter(i => i >= 0)
      if (indices.length === 0) return sheet
      return deduplicateRows(sheet, indices)
    }

    case 'select_columns': {
      const cols = (op.columns as string[] | undefined) ?? []
      const indices = cols.map(c => colIdx(sheet.headers, c)).filter(i => i >= 0)
      if (indices.length === 0) return sheet
      return {
        ...sheet,
        headers: indices.map(i => sheet.headers[i]),
        rows: sheet.rows.map(r => indices.map(i => r[i] ?? null)),
      }
    }

    /* ── 增 ── */

    case 'add_column': {
      if (!op.name) return sheet
      const headers = [...sheet.headers, op.name]
      const rows = sheet.rows.map(r => {
        const val = op.formula ? evalFormula(op.formula, sheet.headers, r) : null
        return [...r, val]
      })
      return { ...sheet, headers, rows }
    }

    case 'add_rows': {
      if (!op.rows?.length) return sheet
      return { ...sheet, rows: [...sheet.rows, ...op.rows] }
    }

    case 'vlookup': {
      const lookupCi = colIdx(sheet.headers, op.lookupColumn ?? '')
      if (lookupCi < 0) return sheet
      const source = allSheets.find(s => s.name === op.fromSheet)
      if (!source) return sheet
      const fromKeyCi = colIdx(source.headers, op.fromKeyColumn ?? '')
      const fromValCi = colIdx(source.headers, op.fromValueColumn ?? '')
      if (fromKeyCi < 0 || fromValCi < 0) return sheet
      const lookup = new Map<string, CellValue>()
      for (const r of source.rows) {
        const key = r[fromKeyCi] == null ? '' : String(r[fromKeyCi])
        if (!lookup.has(key)) lookup.set(key, r[fromValCi])
      }
      const newColName = op.newColumnName ?? op.fromValueColumn ?? 'lookup'
      return {
        ...sheet,
        headers: [...sheet.headers, newColName],
        rows: sheet.rows.map(r => {
          const key = r[lookupCi] == null ? '' : String(r[lookupCi])
          return [...r, lookup.get(key) ?? null]
        }),
      }
    }

    /* ── 删 ── */

    case 'delete_columns': {
      const cols = (op.columns as string[] | undefined) ?? []
      const indices = cols.map(c => colIdx(sheet.headers, c)).filter(i => i >= 0)
      if (indices.length === 0) return sheet
      return deleteColumns(sheet, indices)
    }

    case 'delete_rows': {
      const ci = colIdx(sheet.headers, op.column ?? '')
      if (ci < 0) return sheet
      return {
        ...sheet,
        rows: sheet.rows.filter(r => !matchCell(r[ci], op.operator ?? 'eq', op.value)),
      }
    }

    /* ── 改 ── */

    case 'rename_columns': {
      if (!op.rename) return sheet
      const headers = sheet.headers.map(h => op.rename![h] ?? h)
      return { ...sheet, headers }
    }

    case 'update_cells': {
      const ci = colIdx(sheet.headers, op.column ?? '')
      if (ci < 0) return sheet
      const find = op.find ?? ''
      const repl = op.replace ?? ''
      return {
        ...sheet,
        rows: sheet.rows.map(r => {
          const row = [...r]
          const v = row[ci]
          if (v != null && typeof v === 'string') {
            row[ci] = v.split(find).join(repl)
          } else if (v != null && String(v) === find) {
            row[ci] = repl
          }
          return row
        }),
      }
    }

    case 'conditional_update': {
      const cond = op.condition
      if (!cond) return sheet
      const condCi = colIdx(sheet.headers, cond.column)
      let targetCi = colIdx(sheet.headers, op.targetColumn ?? '')
      if (condCi < 0) return sheet
      let headers = sheet.headers
      if (targetCi < 0 && op.targetColumn) {
        headers = [...sheet.headers, op.targetColumn]
        targetCi = headers.length - 1
      }
      if (targetCi < 0) return sheet
      return {
        ...sheet,
        headers,
        rows: sheet.rows.map(r => {
          const row = [...r]
          while (row.length < headers.length) row.push(null)
          if (matchCell(row[condCi], cond.operator, cond.value)) {
            const nv = op.newValue ?? ''
            row[targetCi] = isNaN(Number(nv)) ? nv : Number(nv)
          }
          return row
        }),
      }
    }

    case 'fill_null': {
      const ci = colIdx(sheet.headers, op.column ?? '')
      if (ci < 0) return sheet
      const fv = op.fillValue ?? op.value ?? ''
      const numFv = Number(fv)
      const fillVal: CellValue = isNaN(numFv) ? fv : numFv
      return {
        ...sheet,
        rows: sheet.rows.map(r => {
          if (r[ci] != null && String(r[ci]) !== '') return r
          const row = [...r]
          row[ci] = fillVal
          return row
        }),
      }
    }

    case 'split_column': {
      const ci = colIdx(sheet.headers, op.column ?? '')
      if (ci < 0 || !op.separator) return sheet
      const newCols = op.newColumns ?? []
      const count = newCols.length || 2
      const headers = [...sheet.headers, ...newCols]
      if (newCols.length === 0) {
        for (let i = 0; i < count; i++) headers.push((op.column ?? '') + '_' + (i + 1))
      }
      return {
        ...sheet,
        headers,
        rows: sheet.rows.map(r => {
          const val = r[ci] == null ? '' : String(r[ci])
          const parts = val.split(op.separator!)
          const extra: CellValue[] = []
          for (let i = 0; i < count; i++) extra.push(parts[i] ?? null)
          return [...r, ...extra]
        }),
      }
    }

    case 'concat_columns': {
      const cols = (op.columns as string[] | undefined) ?? []
      const indices = cols.map(c => colIdx(sheet.headers, c)).filter(i => i >= 0)
      if (indices.length === 0) return sheet
      const sep = op.separator ?? ''
      const newCol = op.newColumn ?? cols.join(sep || '+')
      return {
        ...sheet,
        headers: [...sheet.headers, newCol],
        rows: sheet.rows.map(r => {
          const val = indices.map(i => r[i] == null ? '' : String(r[i])).join(sep)
          return [...r, val]
        }),
      }
    }

    /* ── 高级 ── */

    case 'group_aggregate': {
      const groupCols = op.groupBy ?? []
      const aggs = op.aggregates ?? []
      const groupIndices = groupCols.map(c => colIdx(sheet.headers, c)).filter(i => i >= 0)
      if (groupIndices.length === 0 || aggs.length === 0) return sheet

      const groups = new Map<string, CellValue[][]>()
      for (const row of sheet.rows) {
        const key = groupIndices.map(i => String(row[i] ?? '')).join('\0')
        if (!groups.has(key)) groups.set(key, [])
        groups.get(key)!.push(row)
      }

      const newHeaders = [...groupCols]
      for (const agg of aggs) newHeaders.push(agg.column + '_' + agg.func)
      const newRows: CellValue[][] = []

      for (const [, rows] of groups) {
        const newRow: CellValue[] = groupIndices.map(i => rows[0][i])
        for (const agg of aggs) {
          const aggCi = colIdx(sheet.headers, agg.column)
          const vals = aggCi >= 0
            ? rows.map(r => r[aggCi]).filter(v => v != null && !isNaN(Number(v))).map(Number)
            : []
          switch (agg.func) {
            case 'sum': newRow.push(vals.reduce((a, b) => a + b, 0)); break
            case 'avg': newRow.push(vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : null); break
            case 'count': newRow.push(rows.length); break
            case 'min': newRow.push(vals.length ? Math.min(...vals) : null); break
            case 'max': newRow.push(vals.length ? Math.max(...vals) : null); break
            default: newRow.push(null)
          }
        }
        newRows.push(newRow)
      }
      return { ...sheet, headers: newHeaders, rows: newRows }
    }

    case 'pivot': {
      const rowCi = colIdx(sheet.headers, op.rowField ?? '')
      const colCi = colIdx(sheet.headers, op.columnField ?? '')
      const valCi = colIdx(sheet.headers, op.valueField ?? '')
      if (rowCi < 0 || colCi < 0 || valCi < 0) return sheet

      const rowKeys = [...new Set(sheet.rows.map(r => String(r[rowCi] ?? '')))]
      const colKeys = [...new Set(sheet.rows.map(r => String(r[colCi] ?? '')))]
      const agg = new Map<string, number[]>()
      for (const r of sheet.rows) {
        const key = String(r[rowCi] ?? '') + '\0' + String(r[colCi] ?? '')
        if (!agg.has(key)) agg.set(key, [])
        const v = Number(r[valCi])
        if (!isNaN(v)) agg.get(key)!.push(v)
      }
      const fn = op.func ?? 'sum'
      const headers = [op.rowField ?? '', ...colKeys]
      const rows: CellValue[][] = rowKeys.map(rk => {
        const row: CellValue[] = [rk]
        for (const ck of colKeys) {
          const vals = agg.get(rk + '\0' + ck) ?? []
          if (vals.length === 0) { row.push(null); continue }
          switch (fn) {
            case 'sum': row.push(vals.reduce((a, b) => a + b, 0)); break
            case 'avg': row.push(vals.reduce((a, b) => a + b, 0) / vals.length); break
            case 'count': row.push(vals.length); break
            case 'min': row.push(Math.min(...vals)); break
            case 'max': row.push(Math.max(...vals)); break
            default: row.push(vals.reduce((a, b) => a + b, 0))
          }
        }
        return row
      })
      return { ...sheet, headers, rows }
    }

    case 'merge_sheets': {
      const source = allSheets.find(s => s.name === op.fromSheet)
      if (!source) return sheet
      if (op.mode === 'join' && op.joinKey) {
        const keyCi = colIdx(sheet.headers, op.joinKey)
        const srcKeyCi = colIdx(source.headers, op.joinKey)
        if (keyCi < 0 || srcKeyCi < 0) return sheet
        const srcOtherIndices = source.headers.map((_, i) => i).filter(i => i !== srcKeyCi)
        const newHeaders = [...sheet.headers, ...srcOtherIndices.map(i => source.headers[i])]
        const lookup = new Map<string, CellValue[]>()
        for (const r of source.rows) {
          const key = String(r[srcKeyCi] ?? '')
          if (!lookup.has(key)) lookup.set(key, r)
        }
        return {
          ...sheet,
          headers: newHeaders,
          rows: sheet.rows.map(r => {
            const key = String(r[keyCi] ?? '')
            const match = lookup.get(key)
            const extra = srcOtherIndices.map(i => match ? (match[i] ?? null) : null)
            return [...r, ...extra]
          }),
        }
      }
      // union
      const allHeaders = [...new Set([...sheet.headers, ...source.headers])]
      const mapRow = (r: CellValue[], headers: string[]) =>
        allHeaders.map(h => {
          const i = headers.indexOf(h)
          return i >= 0 ? (r[i] ?? null) : null
        })
      return {
        ...sheet,
        headers: allHeaders,
        rows: [
          ...sheet.rows.map(r => mapRow(r, sheet.headers)),
          ...source.rows.map(r => mapRow(r, source.headers)),
        ],
      }
    }

    case 'merge_cells': {
      const direction = op.direction || 'vertical'
      const newMerges: MergeRange[] = [...(sheet.merges || [])]

      if (direction === 'vertical' && op.column) {
        // 纵向合并：按列相同值
        const ci = colIdx(sheet.headers, op.column)
        if (ci < 0) return sheet

        // 先移除该列已有的合并
        const filteredMerges = newMerges.filter(m => !(m.sc === ci && m.ec === ci))

        let start = 0
        for (let r = 1; r <= sheet.rows.length; r++) {
          const prev = sheet.rows[r - 1]?.[ci]
          const curr = sheet.rows[r]?.[ci]
          if (r === sheet.rows.length || String(curr ?? '') !== String(prev ?? '')) {
            if (r - start > 1) {
              filteredMerges.push({ sr: start, sc: ci, er: r - 1, ec: ci })
            }
            start = r
          }
        }
        return { ...sheet, merges: filteredMerges.length > 0 ? filteredMerges : undefined }
      } else if (direction === 'horizontal') {
        // 横向合并：按行相同值
        const targetRows = op.row !== undefined ? [op.row] : sheet.rows.map((_, i) => i)

        // 先移除目标行已有的横向合并
        const filteredMerges = newMerges.filter(m => {
          if (m.sr !== m.er) return true // 保留跨行的合并
          return !targetRows.includes(m.sr)
        })

        for (const ri of targetRows) {
          const row = sheet.rows[ri]
          if (!row) continue
          let start = 0
          for (let c = 1; c <= row.length; c++) {
            const prev = row[c - 1]
            const curr = row[c]
            if (c === row.length || String(curr ?? '') !== String(prev ?? '')) {
              if (c - start > 1) {
                filteredMerges.push({ sr: ri, sc: start, er: ri, ec: c - 1 })
              }
              start = c
            }
          }
        }
        return { ...sheet, merges: filteredMerges.length > 0 ? filteredMerges : undefined }
      }
      return sheet
    }

    case 'unmerge_cells': {
      if (!op.column) {
        // 取消全表合并
        return { ...sheet, merges: undefined, headerMerges: undefined }
      }
      // 取消指定列的合并
      const ci = colIdx(sheet.headers, op.column)
      if (ci < 0) return sheet
      const newMerges = (sheet.merges || []).filter(m => !(m.sc <= ci && ci <= m.ec))
      return { ...sheet, merges: newMerges.length > 0 ? newMerges : undefined }
    }

    default:
      return sheet
  }
}

/** 执行操作后统一清除 merges（行列坐标已失效） */
function applyOp(sheet: SheetData, op: ExcelOperation, allSheets: SheetData[]): SheetData {
  const result = applyOpInner(sheet, op, allSheets)
  // result === sheet 仅在 default 分支（未知 op）时成立，此时数据未变，保留合并信息
  if (result === sheet) return result
  // merge_cells 和 unmerge_cells 专门管理 merges，不清除
  if (op.op === 'merge_cells' || op.op === 'unmerge_cells') {
    return result
  }
  return { ...result, merges: undefined, headerMerges: undefined }
}

export function executeOperations(
  allSheets: SheetData[],
  targetSheet: number | string,
  ops: ExcelOperation[],
): SheetData[] {
  if (allSheets.length === 0) return [{ name: 'Sheet1', headers: [], rows: [] }]

  // 先处理工作表级别的操作（keep_sheets / delete_sheets）
  let currentSheets = [...allSheets]
  const sheetLevelOps = ops.filter(op => op.op === 'keep_sheets' || op.op === 'delete_sheets')
  const rowLevelOps = ops.filter(op => op.op !== 'keep_sheets' && op.op !== 'delete_sheets')

  for (const op of sheetLevelOps) {
    if (op.op === 'keep_sheets' && op.sheets?.length) {
      // 只保留指定的工作表
      const keepNames = new Set(op.sheets.map(s => s.toLowerCase()))
      currentSheets = currentSheets.filter(s => keepNames.has(s.name.toLowerCase()))
    } else if (op.op === 'delete_sheets' && op.sheets?.length) {
      // 删除指定的工作表
      const deleteNames = new Set(op.sheets.map(s => s.toLowerCase()))
      currentSheets = currentSheets.filter(s => !deleteNames.has(s.name.toLowerCase()))
    }
  }

  // 如果没有行级别操作，直接返回工作表级别操作的结果
  if (rowLevelOps.length === 0) {
    return currentSheets.length > 0 ? currentSheets : [{ name: 'Sheet1', headers: [], rows: [] }]
  }

  // 处理行/列级别的操作
  let sheetIdx = typeof targetSheet === 'number'
    ? targetSheet
    : currentSheets.findIndex(s => s.name === targetSheet)
  if (sheetIdx < 0) sheetIdx = 0
  if (sheetIdx >= currentSheets.length) sheetIdx = 0

  let result = { ...currentSheets[sheetIdx] }
  for (const op of rowLevelOps) {
    result = applyOp(result, op, currentSheets)
  }
  return currentSheets.map((s, i) => i === sheetIdx ? result : s)
}

/** 执行多组操作（支持多个 excel-ops 代码块，每个针对不同工作表） */
export function executeMultipleOperations(
  allSheets: SheetData[],
  operations: ParsedExcelOps[],
): SheetData[] {
  if (allSheets.length === 0) return [{ name: 'Sheet1', headers: [], rows: [] }]

  let currentSheets = [...allSheets]

  for (const opGroup of operations) {
    if (opGroup.outputSheet) {
      // 有 outputSheet：将结果输出到新工作表，不修改原表
      const resultSheets = executeOperations(currentSheets, opGroup.targetSheet, opGroup.ops)

      // 找到被处理的工作表索引
      const sheetIdx = typeof opGroup.targetSheet === 'number'
        ? opGroup.targetSheet
        : currentSheets.findIndex(s => s.name === opGroup.targetSheet)

      if (sheetIdx >= 0 && sheetIdx < resultSheets.length) {
        // 创建新工作表，内容是处理后的结果
        const newSheet: SheetData = {
          ...resultSheets[sheetIdx],
          name: opGroup.outputSheet,
        }
        // 恢复原表，添加新表
        currentSheets = [...currentSheets, newSheet]
      }
    } else {
      // 无 outputSheet：直接修改原表（原有行为）
      currentSheets = executeOperations(currentSheets, opGroup.targetSheet, opGroup.ops)
    }
  }

  return currentSheets
}

export interface ParsedExcelOps {
  ops: ExcelOperation[]
  targetSheet: number | string
  outputSheet?: string  // 如果指定，结果输出到新工作表而不是覆盖原表
  source: 'original' | 'previous'
}

export function parseExcelOpsFromContent(content: string): {
  operations: ParsedExcelOps[]
  source: 'original' | 'previous'
  cleanContent: string
} | null {
  const regex = /```excel-ops\s*\n([\s\S]*?)\n```/g
  const matches: RegExpExecArray[] = []
  let match: RegExpExecArray | null
  while ((match = regex.exec(content)) !== null) {
    matches.push(match)
  }

  if (matches.length === 0) return null

  const operations: ParsedExcelOps[] = []
  let globalSource: 'original' | 'previous' = 'original'

  for (const m of matches) {
    try {
      const parsed = JSON.parse(m[1])
      let ops: ExcelOperation[]
      let targetSheet: number | string = 0
      let outputSheet: string | undefined
      let source: 'original' | 'previous' = 'original'

      if (Array.isArray(parsed)) {
        ops = parsed
      } else if (parsed && Array.isArray(parsed.ops)) {
        ops = parsed.ops
        if (parsed.targetSheet != null) targetSheet = parsed.targetSheet
        if (parsed.outputSheet) outputSheet = parsed.outputSheet
        if (parsed.source === 'previous') source = 'previous'
      } else {
        continue
      }

      operations.push({ ops, targetSheet, outputSheet, source })
      if (source === 'previous') globalSource = 'previous'
    } catch {
      // 跳过解析失败的代码块
      continue
    }
  }

  if (operations.length === 0) return null

  // 移除所有 excel-ops 代码块
  const cleanContent = content.replace(/```excel-ops\s*\n[\s\S]*?\n```/g, '').trim()

  return { operations, source: globalSource, cleanContent }
}

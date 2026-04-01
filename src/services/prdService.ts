import type { AppSettings } from '../types'
import { callMcpTool, ensureMcpInitialized } from './mcp'
import { getDocumentDetail } from './document'
import { callLLM, extractJsonFromText } from './llm'
import type { PrdEntity } from '../stores/prdStore'
import { v4 as uuidv4 } from 'uuid'

// 文档类型
type DocType = 'doc' | 'sheet'

// 解析结果
interface ParsedDocInfo {
  docId: string
  docType: DocType
  sheetId?: string  // 表格文档的 sheet ID
}

// 文档分块结果
interface DocumentChunk {
  content: string
  heading?: string  // 该块的标题（如果有）
  index: number
}

// ========== 文档分块逻辑 ==========

/**
 * 按章节/段落拆分长文档
 * 优先按标题拆分，没有标题则按段落拆分
 */
export function splitDocumentIntoChunks(
  content: string,
  maxChunkSize: number = 40000
): DocumentChunk[] {
  // 1. 识别所有可能的章节标题
  const headingPatterns = [
    /^#{1,3}\s+.+$/gm,                              // Markdown: # ## ###
    /^\d+(\.\d+)*[.、]\s*.+$/gm,                    // 数字序号: 1. 1.1 1.1.1
    /^[一二三四五六七八九十]+[、.]\s*.+$/gm,         // 中文序号: 一、二、
    /^[（(][一二三四五六七八九十\d]+[）)]\s*.+$/gm,  // 带括号: （一）(1)
    /^【[^】]+】$/gm,                                // 方括号标题: 【概述】
  ]

  // 找出所有标题及其位置
  interface HeadingMatch {
    text: string
    index: number
    level: number  // 标题层级，用于判断是否是主要分割点
  }

  const headings: HeadingMatch[] = []

  for (const pattern of headingPatterns) {
    let match
    const regex = new RegExp(pattern.source, pattern.flags)
    while ((match = regex.exec(content)) !== null) {
      // 判断标题层级
      let level = 1
      if (match[0].startsWith('##')) level = 2
      else if (match[0].startsWith('###')) level = 3
      else if (/^\d+\.\d+/.test(match[0])) level = 2
      else if (/^\d+\.\d+\.\d+/.test(match[0])) level = 3

      headings.push({
        text: match[0].trim(),
        index: match.index,
        level,
      })
    }
  }

  // 按位置排序并去重（同一位置可能被多个 pattern 匹配）
  headings.sort((a, b) => a.index - b.index)
  const uniqueHeadings = headings.filter((h, i) =>
    i === 0 || h.index !== headings[i - 1].index
  )

  // 2. 如果没有找到任何标题，按段落拆分
  if (uniqueHeadings.length === 0) {
    console.log('[PRD Split] 未找到章节标题，按段落拆分')
    return splitByParagraphs(content, maxChunkSize)
  }

  console.log(`[PRD Split] 找到 ${uniqueHeadings.length} 个章节标题`)

  // 3. 按标题拆分
  const rawChunks: DocumentChunk[] = []
  for (let i = 0; i < uniqueHeadings.length; i++) {
    const start = uniqueHeadings[i].index
    const end = i + 1 < uniqueHeadings.length
      ? uniqueHeadings[i + 1].index
      : content.length

    rawChunks.push({
      content: content.slice(start, end).trim(),
      heading: uniqueHeadings[i].text,
      index: i,
    })
  }

  // 处理第一个标题之前的内容（如果有）
  if (uniqueHeadings[0].index > 0) {
    const preamble = content.slice(0, uniqueHeadings[0].index).trim()
    if (preamble.length > 100) {  // 忽略太短的前言
      rawChunks.unshift({
        content: preamble,
        heading: '文档开头',
        index: -1,
      })
    }
  }

  // 4. 合并相邻的小块，拆分过大的块
  return optimizeChunks(rawChunks, maxChunkSize)
}

/**
 * 按段落拆分（兜底策略）
 */
function splitByParagraphs(content: string, maxSize: number): DocumentChunk[] {
  const paragraphs = content.split(/\n\n+/)
  const chunks: DocumentChunk[] = []
  let current = ''
  let chunkIndex = 0

  for (const para of paragraphs) {
    // 如果当前块加上新段落会超限，先保存当前块
    if (current.length + para.length > maxSize && current.length > 0) {
      chunks.push({
        content: current.trim(),
        index: chunkIndex++,
      })
      current = ''
    }
    current += (current ? '\n\n' : '') + para
  }

  // 保存最后一块
  if (current.trim()) {
    chunks.push({
      content: current.trim(),
      index: chunkIndex,
    })
  }

  return chunks
}

/**
 * 优化分块：合并小块、拆分大块
 */
function optimizeChunks(chunks: DocumentChunk[], maxSize: number): DocumentChunk[] {
  const result: DocumentChunk[] = []
  let pendingChunk: DocumentChunk | null = null

  for (const chunk of chunks) {
    // 如果块太大，需要拆分
    if (chunk.content.length > maxSize) {
      // 先保存之前累积的块
      if (pendingChunk) {
        result.push(pendingChunk)
        pendingChunk = null
      }
      // 拆分大块（按段落）
      const subChunks = splitByParagraphs(chunk.content, maxSize)
      for (let i = 0; i < subChunks.length; i++) {
        result.push({
          ...subChunks[i],
          heading: i === 0 ? chunk.heading : `${chunk.heading}（续${i}）`,
          index: result.length,
        })
      }
      continue
    }

    // 尝试合并小块
    if (pendingChunk) {
      if (pendingChunk.content.length + chunk.content.length <= maxSize) {
        // 可以合并
        pendingChunk = {
          content: pendingChunk.content + '\n\n' + chunk.content,
          heading: pendingChunk.heading,  // 保留第一个块的标题
          index: pendingChunk.index,
        }
      } else {
        // 不能合并，保存之前的块
        result.push(pendingChunk)
        pendingChunk = chunk
      }
    } else {
      pendingChunk = chunk
    }
  }

  // 保存最后累积的块
  if (pendingChunk) {
    result.push(pendingChunk)
  }

  // 重新编号
  return result.map((chunk, i) => ({ ...chunk, index: i }))
}

/**
 * 合并多个分块提取的实体，去重并合并描述
 */
function mergeExtractedEntities(entitiesArrays: PrdEntity[][]): PrdEntity[] {
  const entityMap = new Map<string, PrdEntity>()

  for (const entities of entitiesArrays) {
    for (const entity of entities) {
      const key = entity.name.toLowerCase()
      const existing = entityMap.get(key)

      if (existing) {
        // 合并描述（如果不同）
        if (entity.description && !existing.description.includes(entity.description)) {
          // 描述不同，追加
          const combined = existing.description + '；' + entity.description
          // 限制总长度
          existing.description = combined.length > 200
            ? combined.slice(0, 200) + '...'
            : combined
        }
      } else {
        entityMap.set(key, { ...entity })
      }
    }
  }

  return Array.from(entityMap.values())
}

// 从 URL 提取 doc_id 和相关信息
export function parseDocIdFromUrl(url: string): string | null {
  const info = parseDocInfoFromUrl(url)
  return info?.docId || null
}

// 从 URL 提取完整的文档信息（包括类型和 sheetId）
export function parseDocInfoFromUrl(url: string): ParsedDocInfo | null {
  // 支持多种KM文档 URL 格式
  // https://xxx.feishu.cn/docx/xxx
  // https://xxx.feishu.cn/wiki/xxx
  // https://xxx.feishu.cn/docs/xxx
  // https://km.mihoyo.com/doc/xxx?sheetId=xxx (表格文档)
  const patterns = [
    /\/docx\/([a-zA-Z0-9]+)/,
    /\/wiki\/([a-zA-Z0-9]+)/,
    /\/docs\/([a-zA-Z0-9]+)/,
    /\/doc\/([a-zA-Z0-9]+)/,
  ]

  let docId: string | null = null

  for (const pattern of patterns) {
    const match = url.match(pattern)
    if (match) {
      docId = match[1]
      break
    }
  }

  // 如果直接是 doc_id 格式
  if (!docId && /^[a-zA-Z0-9]{20,}$/.test(url.trim())) {
    docId = url.trim()
  }

  if (!docId) return null

  // 检查是否有 sheetId 参数（表格文档）
  const sheetIdMatch = url.match(/[?&]sheetId=([a-zA-Z0-9]+)/)
  if (sheetIdMatch) {
    return {
      docId,
      docType: 'sheet',
      sheetId: sheetIdMatch[1],
    }
  }

  return {
    docId,
    docType: 'doc',
  }
}

// 获取文档内容（通过 MCP）
async function fetchDocumentContentViaMcp(
  settings: AppSettings,
  docId: string,
  docType: DocType = 'doc',
  sheetId?: string
): Promise<string> {
  await ensureMcpInitialized(settings)

  // 根据文档类型调用不同的 MCP 服务
  if (docType === 'sheet' && sheetId) {
    // 表格文档：调用表格相关的 MCP 服务
    return await fetchSheetContentViaMcp(settings, docId, sheetId)
  }

  // 普通文档
  const result = await callMcpTool(settings, 'get_doc_detail', {
    doc_id: docId,
    format: 'plain_text',
  })

  console.log(`[PRD] MCP get_doc_detail 返回:`, typeof result, result ? Object.keys(result) : 'null')

  if (result?.content) {
    const textContent = (result.content as Array<{ type: string; text?: string }>)
      .filter((c: any) => c.type === 'text')
      .map((c: any) => c.text || '')
      .join('\n')

    // MCP 工具错误：isError=true 时 content 里是错误信息文本
    if (result.isError) {
      const errText = textContent.trim()
      console.warn(`[PRD] MCP 返回工具错误:`, errText)
      const isPermission = /403|401|forbidden|unauthorized|无权|permission|没有权限/i.test(errText)
      throw new Error(isPermission ? '无权限访问该文档' : `无法获取文档内容：${errText.slice(0, 80)}`)
    }

    console.log(`[PRD] MCP 解析后文本长度: ${textContent.length}`)
    if (textContent.length === 0) {
      console.warn(`[PRD] ⚠️ MCP 返回了 content 但解析出空文本, content 结构:`, JSON.stringify(result.content).slice(0, 500))
    }
    return textContent
  }

  throw new Error('无法获取文档内容')
}

// 获取表格文档内容（通过 MCP）
// 返回格式化的内容，包含表格名称、sheet名称、列名等关键信息以便于实体识别和检索
async function fetchSheetContentViaMcp(
  settings: AppSettings,
  docId: string,
  sheetId: string
): Promise<string> {
  try {
    // 获取工作表列表
    const sheetsResult = await callMcpTool(settings, 'get_spreadsheet_sheets', {
      doc_id: docId,
    })

    // 将表格数据转换为文本格式
    let content = ''
    let spreadsheetTitle = ''
    let sheetTitle = ''
    const allSheetNames: string[] = []

    // 解析工作表列表
    if (sheetsResult?.content) {
      const sheetsText = (sheetsResult.content as Array<{ type: string; text?: string }>)
        .filter((c: any) => c.type === 'text')
        .map((c: any) => c.text || '')
        .join('\n')

      try {
        const sheetsData = JSON.parse(sheetsText)
        // 获取所有 sheet 名称
        const sheets = sheetsData.sheets || sheetsData.data?.sheets || []
        for (const sheet of sheets) {
          const name = sheet.sheet_name || sheet.title || sheet.name
          if (name) {
            allSheetNames.push(name)
          }
        }
        // 获取当前 sheet 名称
        const currentSheet = sheets.find((s: any) =>
          s.sheet_id === sheetId || s.id === sheetId
        )
        if (currentSheet) {
          sheetTitle = currentSheet.sheet_name || currentSheet.title || currentSheet.name || ''
        }
        // 尝试获取表格标题
        spreadsheetTitle = sheetsData.title || sheetsData.data?.title || ''
      } catch {
        // 解析失败，继续处理
      }
    }

    // 添加表格标题
    if (spreadsheetTitle) {
      content += `# ${spreadsheetTitle}\n\n`
      content += `本文档是一个表格文档，表格名称为「${spreadsheetTitle}」。\n\n`
    }
    if (sheetTitle) {
      content += `## 当前 Sheet: ${sheetTitle}\n\n`
    }
    if (allSheetNames.length > 1) {
      content += `该表格包含以下工作表：${allSheetNames.join('、')}。\n\n`
    }

    // 获取指定 sheet 的数据
    const dataResult = await callMcpTool(settings, 'get_spreadsheet_resource', {
      doc_id: docId,
      sheet_id: sheetId,
      range_address: 'A1:Z100',  // 读取 A1 到 Z100 范围
    })

    // 处理表格数据
    let columnHeaders: string[] = []
    if (dataResult?.content) {
      const dataText = (dataResult.content as Array<{ type: string; text?: string }>)
        .filter((c: any) => c.type === 'text')
        .map((c: any) => c.text || '')
        .join('\n')

      // 尝试将表格数据转换为可读格式
      try {
        const tableData = JSON.parse(dataText)
        // 处理 resource 字段（API 返回的可能是嵌套结构）
        const resource = tableData.resource || tableData.data?.resource || tableData
        const { text, headers } = formatTableDataAsTextWithHeaders(
          typeof resource === 'string' ? JSON.parse(resource) : resource
        )
        content += text
        columnHeaders = headers
      } catch {
        // 如果解析失败，直接使用原始文本
        content += dataText
      }
    }

    // 添加元数据摘要，帮助实体识别和检索
    content += '\n\n---\n'
    content += '## 表格元数据摘要\n\n'
    if (spreadsheetTitle) {
      content += `- 表格名称：${spreadsheetTitle}\n`
    }
    if (sheetTitle) {
      content += `- 当前工作表：${sheetTitle}\n`
    }
    if (allSheetNames.length > 0) {
      content += `- 所有工作表：${allSheetNames.join('、')}\n`
    }
    if (columnHeaders.length > 0) {
      content += `- 数据列（字段）：${columnHeaders.join('、')}\n`
      content += `\n该表格包含以下数据字段：${columnHeaders.join('、')}。可通过这些字段名称查询相关信息。\n`
    }

    if (!content.trim()) {
      throw new Error('表格内容为空')
    }

    return content
  } catch (error) {
    console.error('[PRD] 获取表格内容失败:', error)

    // 降级：尝试用普通文档方式获取
    console.log('[PRD] 尝试用普通文档方式获取...')
    try {
      const result = await callMcpTool(settings, 'get_doc_detail', {
        doc_id: docId,
        format: 'plain_text',
      })

      if (result?.content) {
        const textContent = (result.content as Array<{ type: string; text?: string }>)
          .filter((c: any) => c.type === 'text')
          .map((c: any) => c.text || '')
          .join('\n')
        return textContent
      }
    } catch (fallbackError) {
      console.error('[PRD] 降级获取也失败:', fallbackError)
    }

    throw new Error('无法获取表格文档内容')
  }
}

// 将表格数据格式化为文本，同时返回列名
function formatTableDataAsTextWithHeaders(tableData: any): { text: string; headers: string[] } {
  if (!tableData) return { text: '', headers: [] }

  // 如果是 { values: [[...], [...]] } 格式
  const values = tableData.values || tableData.data || tableData

  if (!Array.isArray(values)) {
    return { text: JSON.stringify(tableData, null, 2), headers: [] }
  }

  if (values.length === 0) return { text: '(空表格)', headers: [] }

  // 检查是否是二维数组
  if (Array.isArray(values[0])) {
    // 二维数组格式
    const rows = values as any[][]

    // 假设第一行是表头
    const headers = (rows[0] || []).map(h => String(h ?? '').trim()).filter(Boolean)
    const dataRows = rows.slice(1)

    let result = ''

    // 输出为 markdown 表格
    if (headers.length > 0) {
      result += '| ' + headers.join(' | ') + ' |\n'
      result += '| ' + headers.map(() => '---').join(' | ') + ' |\n'
    }

    for (const row of dataRows) {
      if (row && row.length > 0) {
        result += '| ' + row.map(cell => String(cell ?? '')).join(' | ') + ' |\n'
      }
    }

    return { text: result, headers }
  }

  // 对象数组格式
  if (typeof values[0] === 'object') {
    const objects = values as Record<string, any>[]
    const headers = Object.keys(objects[0] || {})

    let result = '| ' + headers.join(' | ') + ' |\n'
    result += '| ' + headers.map(() => '---').join(' | ') + ' |\n'

    for (const obj of objects) {
      result += '| ' + headers.map(h => String(obj[h] ?? '')).join(' | ') + ' |\n'
    }

    return { text: result, headers }
  }

  // 其他格式，直接 JSON 化
  return { text: JSON.stringify(values, null, 2), headers: [] }
}

// 获取文档详情（标题 + 更新时间）
async function fetchDocumentInfo(
  settings: AppSettings,
  docId: string
): Promise<{ title: string; updateTime: number }> {
  try {
    const detail = await getDocumentDetail(docId, settings)
    return {
      title: detail.title || '未命名文档',
      updateTime: detail.update_time ? parseInt(detail.update_time) : Date.now(),
    }
  } catch (error) {
    console.warn('[PRD] 获取文档信息失败:', error)
    // 将错误信息向上传递（供 fetchDocumentContent 捕获并生成友好标题）
    const msg = error instanceof Error ? error.message : String(error)
    const isPermission = /403|401|forbidden|unauthorized|无权|permission/i.test(msg)
    throw new Error(isPermission ? '无权限访问该文档' : '无法获取文档信息')
  }
}

// 获取文档内容、标题和更新时间
export async function fetchDocumentContent(
  settings: AppSettings,
  docId: string,
  docUrl?: string  // 可选：传入 URL 以解析表格信息
): Promise<{ title: string; content: string; updateTime: number }> {
  // 解析文档类型
  let docType: DocType = 'doc'
  let sheetId: string | undefined

  if (docUrl) {
    const docInfo = parseDocInfoFromUrl(docUrl)
    if (docInfo) {
      docType = docInfo.docType
      sheetId = docInfo.sheetId
    }
  }

  // 并行获取信息和内容
  const [info, content] = await Promise.all([
    fetchDocumentInfo(settings, docId),
    fetchDocumentContentViaMcp(settings, docId, docType, sheetId),
  ])

  let title = info.title

  // 如果 API 没有返回标题，从内容第一行提取
  if (!title) {
    const lines = content.split('\n').filter(l => l.trim())
    title = lines[0]?.replace(/^#+\s*/, '').trim().slice(0, 50) || '未命名文档'
  }

  // 表格文档添加标识
  if (docType === 'sheet') {
    title = title.includes('[表格]') ? title : `[表格] ${title}`
  }

  return { title, content, updateTime: info.updateTime }
}

// 使用 LLM 提取实体（增强版，支持长文档分块）
export async function extractEntities(
  settings: AppSettings,
  content: string,
  docId: string
): Promise<PrdEntity[]> {
  const maxChunkSize = 40000  // 单块最大 40000 字符

  // 如果文档较短，直接处理
  if (content.length <= maxChunkSize) {
    console.log(`[PRD] 文档长度 ${content.length} 字符，直接处理`)
    return extractEntitiesFromChunk(settings, content, docId)
  }

  // 长文档：分块处理
  console.log(`[PRD] 长文档 ${content.length} 字符，启用分块处理`)
  const chunks = splitDocumentIntoChunks(content, maxChunkSize)
  console.log(`[PRD] 拆分为 ${chunks.length} 个分块:`, chunks.map(c => ({
    heading: c.heading,
    length: c.content.length,
  })))

  // 并行处理所有分块
  const results = await Promise.all(
    chunks.map(async (chunk, i) => {
      console.log(`[PRD] 处理分块 ${i + 1}/${chunks.length}: ${chunk.heading || '无标题'} (${chunk.content.length} 字符)`)
      try {
        return await extractEntitiesFromChunk(settings, chunk.content, docId)
      } catch (e) {
        console.error(`[PRD] 分块 ${i + 1} 处理失败:`, e)
        return []
      }
    })
  )

  // 合并去重
  const merged = mergeExtractedEntities(results)
  console.log(`[PRD] 分块提取完成，共 ${results.flat().length} 个实体，合并后 ${merged.length} 个`)
  return merged
}

// 从单个分块提取实体（内部函数）
async function extractEntitiesFromChunk(
  settings: AppSettings,
  content: string,
  docId: string
): Promise<PrdEntity[]> {
  console.log(`[PRD] 送入 LLM 的内容长度: ${content.length} 字符, 内容预览: "${content.slice(0, 100)}..."`)

  // 检测是否为表格文档
  const isSpreadsheet = content.includes('本文档是一个表格文档') || content.includes('表格元数据摘要')

  const prompt = `你是一个 PRD 文档分析专家。请从以下 PRD 文档内容中提取**业务实体**。

## 提取原则
1. **优先提取**：专有名词、产品/功能名称、业务概念、系统模块、状态/类型枚举
2. **适度提取**：文档中反复出现的术语、有明确定义的概念
3. **避免提取**：纯技术术语（如"接口"、"数据库"）、过于宽泛的词（如"用户"、"系统"）
${isSpreadsheet ? `
## 表格文档特别说明
这是一个表格文档，请特别注意提取：
- **表格名称**：作为实体，描述该表格的用途和包含的数据类型
- **工作表(Sheet)名称**：如果有多个工作表，每个工作表名称可作为实体
- **重要的列名/字段名**：表格中的关键数据字段，尤其是业务相关的字段
- **表格中的关键数据**：如状态值、类型枚举、配置项等
` : ''}
## 返回格式
请返回 JSON 数组，每个实体包含：
- name: 实体名称（使用 PRD 原文中的表述）
- description: **一句话**简要定义，不超过 30 个字

## 示例
\`\`\`json
[
  { "name": "订单状态机", "description": "管理订单状态流转的核心模块，含5种状态" },
  { "name": "商品SKU", "description": "商品最小库存单位，编码格式为品类+品牌+序号" }
]
\`\`\`

请仔细阅读文档，提取所有符合条件的实体。如果文档确实没有业务实体，返回空数组 []。

## 文档内容
${content}
`

  // 重试逻辑
  const maxRetries = 2
  let lastError: Error | null = null

  for (let attempt = 0; attempt <= maxRetries; attempt++) {
    try {
      const text = await callLLM(settings, prompt, {
        maxTokens: 16384,
        temperature: 0.3,
      })

      console.log(`[PRD] LLM 返回内容长度: ${text.length}`)
      if (!text.trim()) {
        console.warn(`[PRD] 第 ${attempt + 1} 次尝试: LLM 返回空内容`)
        continue
      }

      // 增强的 JSON 解析
      const parsed = extractJsonFromText<Array<Record<string, unknown>>>(text, 'array')
      const entities = parsed !== null
        ? buildEntitiesFromParsed(parsed, docId)
        : []

      if (entities.length > 0) {
        console.log(`[PRD] 成功提取 ${entities.length} 个实体`)
        return entities
      }

      // parsed !== null 说明 JSON 解析成功了，但过滤后没有有效实体（可能 LLM 确实返回了 []）
      if (parsed !== null) {
        console.warn(`[PRD] 第 ${attempt + 1} 次尝试: JSON 解析成功但无有效实体（数组长度=${parsed.length}），不重试`)
        return []
      }

      // parsed === null 说明 JSON 解析失败（格式不完整/截断），应该重试
      console.warn(`[PRD] 第 ${attempt + 1} 次尝试: JSON 解析失败，将重试`)

    } catch (e) {
      lastError = e as Error
      console.error(`[PRD] 第 ${attempt + 1} 次尝试失败:`, e)

      if (attempt < maxRetries) {
        // 等待后重试
        await new Promise(resolve => setTimeout(resolve, 1000 * (attempt + 1)))
      }
    }
  }

  console.error('[PRD] 所有重试均失败:', lastError)
  return []
}

/**
 * 从已解析的 JSON 数组构建实体列表（校验字段 + 生成 id）
 */
function buildEntitiesFromParsed(parsed: Array<Record<string, unknown>>, docId: string): PrdEntity[] {
  const now = Date.now()
  const MAX_DESC_LENGTH = 100  // 描述兜底截断（prompt 要求 30 字，但留余量）

  return parsed
    .filter(item =>
      item &&
      typeof item === 'object' &&
      typeof item.name === 'string' &&
      (item.name as string).trim().length > 0
    )
    .map(item => {
      let desc = typeof item.description === 'string' ? item.description.trim() : ''
      if (desc.length > MAX_DESC_LENGTH) {
        desc = desc.slice(0, MAX_DESC_LENGTH) + '…'
      }
      return {
        id: uuidv4(),
        name: (item.name as string).trim(),
        description: desc,
        source: {
          docId,
          method: 'llm' as const,
        },
        createdAt: now,
        updatedAt: now,
      }
    })
}

// 解析并提取文档实体的完整流程
export async function processDocument(
  settings: AppSettings,
  docId: string,
  onProgress?: (status: string) => void,
  docUrl?: string  // 可选：传入 URL 以支持表格文档
): Promise<{ title: string; entities: PrdEntity[]; content: string }> {
  onProgress?.('正在获取文档内容...')
  const { title, content } = await fetchDocumentContent(settings, docId, docUrl)

  console.log(`[PRD] 文档内容获取完成: title="${title}", 内容长度=${content.length} 字符, docUrl=${docUrl || '无'}`)
  if (content.length < 50) {
    console.warn(`[PRD] ⚠️ 文档内容异常短: "${content.slice(0, 200)}"`)
  }

  onProgress?.('正在分析文档实体...')
  const entities = await extractEntities(settings, content, docId)

  return { title, entities, content }
}

// ========== 跨文档分析 ==========

/** 从文本中提取候选词（2-8字的中文词或英文/数字组合） */
export function extractCandidateTerms(content: string): Map<string, number> {
  const termCounts = new Map<string, number>()

  // 通用停用词
  const stopWords = new Set([
    '的', '是', '在', '有', '和', '与', '或', '及', '等', '了', '着', '过',
    '这', '那', '个', '些', '种', '类', '方', '面', '上', '下', '中', '内',
    '可以', '需要', '进行', '通过', '使用', '支持', '包括', '以及', '如果',
    '用户', '系统', '功能', '模块', '接口', '数据', '信息', '操作', '处理',
    '页面', '按钮', '输入', '输出', '显示', '列表', '详情', '状态', '类型',
    '新增', '修改', '删除', '查询', '提交', '保存', '取消', '确认', '返回',
  ])

  // 提取中文词组（2-8字）
  const chinesePattern = /[\u4e00-\u9fa5]{2,8}/g
  let match
  while ((match = chinesePattern.exec(content)) !== null) {
    const term = match[0]
    if (!stopWords.has(term)) {
      termCounts.set(term, (termCounts.get(term) || 0) + 1)
    }
  }

  // 提取英文/数字组合（如 lml、SKU、V2 等，2-20字符）
  const englishPattern = /[a-zA-Z][a-zA-Z0-9_-]{1,19}/g
  while ((match = englishPattern.exec(content)) !== null) {
    const term = match[0].toLowerCase()
    // 排除常见技术词
    const techWords = new Set(['http', 'https', 'api', 'url', 'json', 'html', 'css', 'true', 'false', 'null', 'undefined'])
    if (!techWords.has(term) && term.length >= 2) {
      termCounts.set(term, (termCounts.get(term) || 0) + 1)
    }
  }

  return termCounts
}

/** 跨文档分析：找出在多篇文档中高频出现但未被识别的潜在实体 */
export function findCrossDocumentTerms(
  documents: Array<{ content: string; existingEntities: string[] }>,
  minDocCount: number = 2,  // 至少在几篇文档中出现
  minTermFreq: number = 2,  // 在单篇文档中至少出现几次
): string[] {
  // 统计每个词出现在几篇文档中
  const termDocCount = new Map<string, number>()
  // 收集所有已识别的实体名
  const allExistingEntities = new Set<string>()

  for (const doc of documents) {
    for (const entity of doc.existingEntities) {
      allExistingEntities.add(entity.toLowerCase())
    }

    const termCounts = extractCandidateTerms(doc.content)
    const termsInThisDoc = new Set<string>()

    for (const [term, count] of termCounts) {
      // 只统计在该文档中出现足够次数的词
      if (count >= minTermFreq) {
        termsInThisDoc.add(term)
      }
    }

    for (const term of termsInThisDoc) {
      termDocCount.set(term, (termDocCount.get(term) || 0) + 1)
    }
  }

  // 筛选：出现在足够多文档中，且不在已识别实体中
  const candidates: string[] = []
  for (const [term, docCount] of termDocCount) {
    if (docCount >= minDocCount && !allExistingEntities.has(term.toLowerCase())) {
      candidates.push(term)
    }
  }

  // 按文档出现次数排序
  candidates.sort((a, b) => (termDocCount.get(b) || 0) - (termDocCount.get(a) || 0))

  return candidates.slice(0, 20) // 最多返回20个候选
}

/** 使用 LLM 验证候选词是否为有意义的业务实体 */
async function validateCandidateEntities(
  settings: AppSettings,
  candidates: string[],
  sampleContents: string[], // 提供一些上下文示例
): Promise<Array<{ name: string; description: string }>> {
  if (candidates.length === 0) return []

  const contextSample = sampleContents
    .map(c => c.slice(0, 1000))
    .join('\n\n---\n\n')
    .slice(0, 4000)

  const prompt = `你是一个 PRD 文档分析专家。以下是一些在**多篇 PRD 文档中反复出现**的词汇，请判断哪些是有业务含义的**专有名词或核心概念**。

## 候选词汇
${candidates.join('、')}

## 文档片段示例（供参考上下文）
${contextSample}

## 判断原则
- 选出那些代表特定业务概念、产品名称、系统模块的词
- 排除通用技术术语和泛化描述
- 如果某个词是项目/产品的专有缩写（如 lml、SKU），也应该识别出来

## 返回格式
请以 JSON 数组格式返回你认为有意义的实体，每个包含：
- name: 实体名称
- description: 简短定义（根据上下文推断）

如果没有有意义的实体，返回空数组 []。只返回 JSON 数组，不要其他内容。
`

  try {
    const text = await callLLM(settings, prompt, { maxTokens: 2048 })
    const result = extractJsonFromText<Array<{ name: string; description: string }>>(text, 'array')
    return result ?? []
  } catch (e) {
    console.error('验证候选实体失败:', e)
  }

  return []
}

/** 跨文档增强分析的完整流程 */
export async function crossDocumentAnalysis(
  settings: AppSettings,
  documents: Array<{ docId: string; content: string; entities: PrdEntity[] }>,
  onProgress?: (status: string) => void,
): Promise<Map<string, PrdEntity[]>> {
  // 返回值：docId -> 新发现的实体列表
  const newEntitiesMap = new Map<string, PrdEntity[]>()

  if (documents.length < 2) {
    return newEntitiesMap // 少于2篇文档，无法做跨文档分析
  }

  onProgress?.('正在进行跨文档词频分析...')

  // 1. 找出跨文档高频词
  const docsForAnalysis = documents.map(d => ({
    content: d.content,
    existingEntities: d.entities.map(e => e.name),
  }))

  const minDocCount = Math.max(2, Math.floor(documents.length * 0.3)) // 至少30%的文档中出现
  const candidates = findCrossDocumentTerms(docsForAnalysis, minDocCount, 2)

  if (candidates.length === 0) {
    onProgress?.('未发现新的跨文档高频词')
    return newEntitiesMap
  }

  onProgress?.(`发现 ${candidates.length} 个候选词，正在验证...`)

  // 2. 用 LLM 验证这些候选词
  const sampleContents = documents.slice(0, 3).map(d => d.content)
  const validatedEntities = await validateCandidateEntities(settings, candidates, sampleContents)

  if (validatedEntities.length === 0) {
    onProgress?.('候选词验证完成，无新增实体')
    return newEntitiesMap
  }

  onProgress?.(`验证通过 ${validatedEntities.length} 个新实体，正在分配到文档...`)

  // 3. 将新实体分配到包含该词的文档中
  const now = Date.now()
  for (const doc of documents) {
    const contentLower = doc.content.toLowerCase()
    const existingNames = new Set(doc.entities.map(e => e.name.toLowerCase()))
    const newEntities: PrdEntity[] = []

    for (const entity of validatedEntities) {
      const nameLower = entity.name.toLowerCase()
      // 检查文档中是否包含该实体，且不在已有实体中
      if (contentLower.includes(nameLower) && !existingNames.has(nameLower)) {
        newEntities.push({
          id: uuidv4(),
          name: entity.name,
          description: entity.description + '（跨文档分析发现）',
          source: {
            docId: doc.docId,
            method: 'llm' as const,
          },
          createdAt: now,
          updatedAt: now,
        })
      }
    }

    if (newEntities.length > 0) {
      newEntitiesMap.set(doc.docId, newEntities)
    }
  }

  return newEntitiesMap
}

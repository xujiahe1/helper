import type { AppSettings, Attachment, ImageGenConfig } from '../types'
import { IMAGE_MODEL_ID, isExcelFile } from '../types'
import { extractPdfText, renderPdfToImages } from './pdf'
import { parseExcelFromDataUrl, sheetToAISummary } from './excel'

type ContentPart =
  | { type: 'text'; text: string }
  | { type: 'image_url'; image_url: { url: string } }

export interface ChatMessage {
  role: string
  content: string | ContentPart[] | null
  tool_calls?: Array<{ id: string; type: string; function: { name: string; arguments: string } }>
  tool_call_id?: string
}

export interface ToolCallInfo {
  id: string
  name: string
  arguments: string
}

export interface StreamChunk {
  type: 'thinking' | 'text' | 'tool_calls'
  text: string
  toolCalls?: ToolCallInfo[]
}

interface BuildMessageOptions {
  pdfMode: 'native' | 'images' | 'extract'
}

export async function buildApiMessage(
  msg: { role: string; content: string; attachments?: Attachment[] },
  options: BuildMessageOptions = { pdfMode: 'images' },
): Promise<ChatMessage> {
  const validAttachments = msg.attachments?.filter(a => a.dataUrl) || []
  if (validAttachments.length === 0) {
    return { role: msg.role, content: msg.content }
  }

  const parts: ContentPart[] = []
  if (msg.content) {
    parts.push({ type: 'text', text: msg.content })
  }
  for (const att of validAttachments) {
    if (att.mimeType === 'application/pdf') {
      if (options.pdfMode === 'native') {
        parts.push({ type: 'image_url', image_url: { url: att.dataUrl } })
      } else if (options.pdfMode === 'images') {
        let imagesDone = false
        try {
          const images = await renderPdfToImages(att.dataUrl)
          if (images.length > 0) {
            parts.push({ type: 'text', text: '【文档: ' + att.name + '（共 ' + images.length + ' 页，已渲染为图片）】' })
            for (const img of images) {
              parts.push({ type: 'image_url', image_url: { url: img } })
            }
            imagesDone = true
          }
        } catch (e) {
          console.warn('[PDF] 图片渲染失败，降级为文本提取:', e)
        }
        // 图片渲染失败则自动降级到文本提取
        if (!imagesDone) {
          try {
            const text = await extractPdfText(att.dataUrl)
            if (text) {
              parts.push({ type: 'text', text: '【文档: ' + att.name + '（图片渲染失败，已提取文本）】\n\n' + text })
            } else {
              parts.push({ type: 'text', text: '【文档: ' + att.name + '（PDF 无法渲染也无法提取文本，可能是扫描件）】' })
            }
          } catch (e2) {
            const errMsg = e2 instanceof Error ? e2.message : String(e2)
            parts.push({ type: 'text', text: '【文档: ' + att.name + '（PDF 处理完全失败: ' + errMsg + '）】' })
          }
        }
      } else {
        try {
          const text = await extractPdfText(att.dataUrl)
          if (text) {
            parts.push({ type: 'text', text: '【文档: ' + att.name + '】\n\n' + text })
          } else {
            parts.push({ type: 'text', text: '【文档: ' + att.name + '（无法提取文本，可能是扫描件）】' })
          }
        } catch (e) {
          const errMsg = e instanceof Error ? e.message : String(e)
          parts.push({ type: 'text', text: '【文档: ' + att.name + '（PDF 解析失败: ' + errMsg + '）】' })
        }
      }
    } else if (isExcelFile(att.mimeType, att.name)) {
      try {
        const sheets = att.parsedSheets?.length
          ? att.parsedSheets
          : parseExcelFromDataUrl(att.dataUrl)
        if (sheets.length > 0) {
          const summaries = sheets.map(s => sheetToAISummary(s)).join('\n\n')
          parts.push({
            type: 'text',
            text: '【Excel 文件: ' + att.name + '（共 ' + sheets.length + ' 个工作表）】\n\n' + summaries,
          })
        } else {
          parts.push({ type: 'text', text: '【Excel 文件: ' + att.name + '（文件为空）】' })
        }
      } catch (e) {
        const errMsg = e instanceof Error ? e.message : String(e)
        parts.push({ type: 'text', text: '【Excel 文件: ' + att.name + '（解析失败: ' + errMsg + '）】' })
      }
    } else {
      parts.push({ type: 'image_url', image_url: { url: att.dataUrl } })
    }
  }
  return { role: msg.role, content: parts }
}

export function messageHasPdf(messages: { attachments?: Attachment[] }[]): boolean {
  return messages.some(m => m.attachments?.some(a => a.mimeType === 'application/pdf' && a.dataUrl))
}

const MAX_RETRIES = 2
const RETRY_DELAY_MS = 3000
const IMAGE_REQUEST_TIMEOUT_MS = 60_000
const STREAM_IDLE_TIMEOUT_MS = 120_000
const STREAM_IDLE_TIMEOUT_MCP_MS = 1_800_000

function friendlyError(status: number, body: string): string {
  switch (status) {
    case 429: return '请求频率过高，请稍后重试'
    case 401: return 'API Key 无效，请在设置中检查'
    case 403: return '没有访问权限，请检查 API Key'
    case 502:
    case 503: return '服务暂时不可用，请稍后重试'
    default: return 'API 错误 (' + status + '): ' + body
  }
}

function shouldRetry(status: number): boolean {
  return status === 429 || status === 502 || status === 503
}

function sleep(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms))
}

// ========== 非流式 LLM 调用 + JSON 提取 ==========

export interface CallLLMOptions {
  maxTokens?: number       // 默认 2048
  temperature?: number     // 默认 0.2
  model?: string           // 默认 settings.systemModel
  retries?: number         // 默认 0（不重试）
  retryDelay?: number      // 首次重试延迟 ms，默认 1000，递增
  timeoutMs?: number       // 超时 ms，默认 60000（60s）
}

/**
 * 非流式 LLM 调用，返回文本内容
 *
 * 统一替代各 service 中手写的 fetch(settings.apiBaseUrl + '/chat/completions', ...)
 */
export async function callLLM(
  settings: AppSettings,
  prompt: string,
  options?: CallLLMOptions,
): Promise<string> {
  const {
    maxTokens = 2048,
    temperature = 0.2,
    model = settings.systemModel,  // 系统级调用默认使用 settings.systemModel
    retries = 0,
    retryDelay = 1000,
    timeoutMs = 60_000,
  } = options ?? {}

  let lastError: Error | null = null

  for (let attempt = 0; attempt <= retries; attempt++) {
    try {
      if (attempt > 0) {
        await sleep(retryDelay * attempt)
      }

      const controller = new AbortController()
      const timeoutId = setTimeout(() => controller.abort(), timeoutMs)

      let response: Response
      try {
        response = await fetch(settings.apiBaseUrl + '/chat/completions', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'Authorization': 'Bearer ' + settings.apiKey,
          },
          body: JSON.stringify({
            model,
            messages: [{ role: 'user', content: prompt }],
            max_tokens: maxTokens,
            temperature,
          }),
          signal: controller.signal,
        })
      } finally {
        clearTimeout(timeoutId)
      }

      if (!response.ok) {
        const errorText = await response.text().catch(() => response.statusText)
        throw new Error(`LLM 请求失败 (${response.status}): ${errorText}`)
      }

      const data = await response.json()
      const text = data.choices?.[0]?.message?.content || ''
      return text
    } catch (e) {
      lastError = e as Error
      if ((e as Error).name === 'AbortError') {
        lastError = new Error(`LLM 请求超时 (${Math.round(timeoutMs / 1000)}s)`)
      }
      if (attempt >= retries) break
    }
  }

  throw lastError ?? new Error('LLM 调用失败')
}

/**
 * 从 LLM 返回的自由文本中提取 JSON
 *
 * 合并了项目中所有已有的解析策略：
 * - 策略 1: 正则直接匹配 [...] 或 {...}
 * - 策略 2: 匹配 ```json ... ``` 代码块
 * - 策略 3: 清理 markdown 格式后定位首尾括号
 * - 策略 4: 修复常见 JSON 错误（注释、尾随逗号）
 *
 * @param text   LLM 返回的原始文本
 * @param type   期望提取的 JSON 类型：'array' (默认) 或 'object'
 * @returns 解析后的 JSON 对象，全部策略失败则返回 null
 */
export function extractJsonFromText<T = unknown>(
  text: string,
  type: 'array' | 'object' = 'array',
): T | null {
  const open = type === 'array' ? '[' : '{'
  const close = type === 'array' ? ']' : '}'
  const regex = type === 'array' ? /\[[\s\S]*\]/ : /\{[\s\S]*\}/

  const strategies: Array<() => unknown> = [
    // 策略 1: 正则直接匹配
    () => {
      const match = text.match(regex)
      if (match) return JSON.parse(match[0])
      return null
    },
    // 策略 2: markdown 代码块
    () => {
      const pattern = type === 'array'
        ? /```(?:json)?\s*(\[[\s\S]*?\])\s*```/
        : /```(?:json)?\s*(\{[\s\S]*?\})\s*```/
      const match = text.match(pattern)
      if (match) return JSON.parse(match[1])
      return null
    },
    // 策略 3: 清理格式后定位
    () => {
      let cleaned = text
        .replace(/```json\s*/g, '')
        .replace(/```\s*/g, '')
        .replace(/^\s*[\r\n]+/, '')
        .replace(/[\r\n]+\s*$/, '')
        .trim()

      const startIdx = cleaned.indexOf(open)
      const endIdx = cleaned.lastIndexOf(close)
      if (startIdx !== -1 && endIdx !== -1 && endIdx > startIdx) {
        cleaned = cleaned.slice(startIdx, endIdx + 1)
      }
      return JSON.parse(cleaned)
    },
    // 策略 4: 修复常见 JSON 错误
    () => {
      let cleaned = text
      // 移除单行注释
      cleaned = cleaned.replace(/\/\/[^\n]*/g, '')
      // 移除尾随逗号
      cleaned = cleaned.replace(/,(\s*[}\]])/g, '$1')
      // 修复字符串值内部的裸双引号：如 "description": "品牌标识，如"姬米花""
      // LLM 经常在值中使用中文引号""或直接用 ASCII 双引号，导致 JSON 解析失败
      // 替换 "\u201c \u201d（中文左右引号）为安全字符
      cleaned = cleaned.replace(/\u201c/g, '\u300c') // " → 「
      cleaned = cleaned.replace(/\u201d/g, '\u300d') // " → 」
      cleaned = cleaned.replace(/\u2018/g, '\u300c') // ' → 「
      cleaned = cleaned.replace(/\u2019/g, '\u300d') // ' → 」
      // 定位 JSON 部分
      const startIdx = cleaned.indexOf(open)
      const endIdx = cleaned.lastIndexOf(close)
      if (startIdx !== -1 && endIdx !== -1) {
        cleaned = cleaned.slice(startIdx, endIdx + 1)
      }
      return JSON.parse(cleaned)
    },
    // 策略 5: 修复值中的裸双引号（LLM 常在值中用未转义的 " 导致解析失败）
    // 如: "description": "品牌标识，如"姬米花"" → 值中的 " 未转义
    () => {
      let cleaned = text
      // 去 markdown 包裹
      cleaned = cleaned.replace(/```(?:json)?\s*/g, '').replace(/```\s*/g, '').trim()
      const startIdx = cleaned.indexOf(open)
      const endIdx = cleaned.lastIndexOf(close)
      if (startIdx === -1 || endIdx === -1) return null
      cleaned = cleaned.slice(startIdx, endIdx + 1)

      // 逐字符扫描，修复值中的裸双引号
      let fixed = ''
      let inString = false
      let escaped = false
      for (let i = 0; i < cleaned.length; i++) {
        const ch = cleaned[i]
        if (escaped) {
          fixed += ch
          escaped = false
          continue
        }
        if (ch === '\\') {
          fixed += ch
          escaped = true
          continue
        }
        if (ch === '"') {
          if (!inString) {
            // 进入字符串
            inString = true
            fixed += ch
          } else {
            // 在字符串中遇到 "，判断是否是字符串结束
            // 如果下一个非空白字符是 : , } ] 则认为是字符串结束
            let nextNonSpace = ''
            for (let j = i + 1; j < cleaned.length; j++) {
              if (cleaned[j] !== ' ' && cleaned[j] !== '\n' && cleaned[j] !== '\r' && cleaned[j] !== '\t') {
                nextNonSpace = cleaned[j]
                break
              }
            }
            if (nextNonSpace === ':' || nextNonSpace === ',' || nextNonSpace === '}' || nextNonSpace === ']' || nextNonSpace === '') {
              // 确实是字符串结束
              inString = false
              fixed += ch
            } else {
              // 值中间的裸双引号，转义它
              fixed += '\\"'
            }
          }
        } else {
          fixed += ch
        }
      }

      return JSON.parse(fixed)
    },
  ]

  // 额外策略（仅数组）: 修复被截断的 JSON 数组
  // 当 LLM 输出被截断时，JSON 数组不完整
  // 此策略从后往前尝试在每个 }（后跟,或换行）位置截断，补 ] 后尝试解析
  if (type === 'array') {
    strategies.push(() => {
      const startIdx = text.indexOf('[')
      if (startIdx === -1) return null

      let arr = text.slice(startIdx)
      // 去掉 markdown 尾部
      arr = arr.replace(/\s*```\s*$/, '')

      // 如果已经以 ] 结尾，前面的策略应该已经处理了
      if (arr.trimEnd().endsWith(']')) return null

      // 收集所有可能的截断位置（}, 后面跟的位置）
      const cutPositions: number[] = []
      for (let i = arr.length - 1; i >= 0; i--) {
        if (arr[i] === '}') {
          // } 后面是 , 或空白/换行或结尾
          const next = arr[i + 1]
          if (next === ',' || next === '\n' || next === '\r' || next === ' ' || next === undefined) {
            cutPositions.push(i)
          }
        }
      }

      // 从最靠后的位置开始尝试
      for (const pos of cutPositions) {
        try {
          const fixed = arr.slice(0, pos + 1) + ']'
          const result = JSON.parse(fixed)
          if (Array.isArray(result) && result.length > 0) {
            console.warn(`[JSON] 修复被截断的 JSON 数组，恢复了 ${result.length} 个完整元素`)
            return result
          }
        } catch {
          // 这个位置不行，试下一个
        }
      }

      return null
    })
  }

  for (let si = 0; si < strategies.length; si++) {
    try {
      const result = strategies[si]()
      if (result !== null && result !== undefined) {
        // 类型校验
        if (type === 'array' && !Array.isArray(result)) continue
        if (type === 'object' && (typeof result !== 'object' || Array.isArray(result))) continue
        return result as T
      }
    } catch (e) {
      console.debug(`[JSON] 策略 ${si + 1}/${strategies.length} 失败:`, (e as Error).message?.slice(0, 100))
    }
  }

  return null
}

export function isImageModel(model: string): boolean {
  return model === IMAGE_MODEL_ID
}

export async function generateImage(
  messages: ChatMessage[],
  model: string,
  settings: AppSettings,
  imageConfig: ImageGenConfig,
  signal?: AbortSignal,
  onProgress?: (status: string) => void,
): Promise<{ images: string[]; text: string }> {
  if (!settings.apiKey) {
    throw new Error('Please configure your API key in Settings first.')
  }

  const body = {
    model,
    messages,
    max_tokens: 65536,
    extra_body: {
      generationConfig: {
        responseModalities: ['IMAGE'],
        imageConfig: {
          aspectRatio: imageConfig.aspectRatio,
          imageSize: imageConfig.imageSize,
        },
      },
    },
  }

  let lastError: Error | null = null
  for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
    if (attempt > 0) {
      onProgress?.('重试中 (' + attempt + '/' + MAX_RETRIES + ')…')
      await sleep(RETRY_DELAY_MS * attempt)
    } else {
      onProgress?.('正在生成图片…')
    }

    // 每次请求独立超时，同时监听用户手动取消
    const timeoutCtrl = new AbortController()
    const timer = setTimeout(() => timeoutCtrl.abort(), IMAGE_REQUEST_TIMEOUT_MS)
    if (signal) {
      signal.addEventListener('abort', () => timeoutCtrl.abort(), { once: true })
    }

    let response: Response
    try {
      response = await fetch(settings.apiBaseUrl + '/chat/completions', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': 'Bearer ' + settings.apiKey,
        },
        body: JSON.stringify(body),
        signal: timeoutCtrl.signal,
      })
    } catch (err) {
      if (signal?.aborted) throw err
      lastError = new Error('请求超时，请稍后重试')
      if (attempt < MAX_RETRIES) continue
      throw lastError
    } finally {
      clearTimeout(timer)
    }

    if (!response.ok) {
      const errorText = await response.text()
      lastError = new Error(friendlyError(response.status, errorText))
      if (shouldRetry(response.status) && attempt < MAX_RETRIES) continue
      throw lastError
    }

    const data = await response.json()

    const msg = data.choices?.[0]?.message
    if (!msg) {
      const preview = JSON.stringify(data).slice(0, 500)
      throw new Error('API 未返回有效的 choices 结构。\n\nAPI 原始响应:\n```json\n' + preview + '\n```')
    }

    const images: string[] = []
    let text = ''

    if (Array.isArray(msg.content)) {
      for (const part of msg.content) {
        if (part.type === 'image_url' && part.image_url?.url) {
          images.push(part.image_url.url)
        } else if (part.type === 'text' && part.text) {
          text += part.text
        // 兼容 inline_data 格式（Gemini 原生格式）
        } else if (part.inline_data?.data) {
          const mime = part.inline_data.mimeType || 'image/png'
          images.push('data:' + mime + ';base64,' + part.inline_data.data)
        }
      }
    } else if (typeof msg.content === 'string') {
      const content = msg.content
      if (content.startsWith('data:image/')) {
        images.push(content)
      } else {
        text = content
      }
    }

    // 兼容 audio.data 格式（某些代理将图片放在 message.audio.data 中）
    if (images.length === 0 && msg.audio?.data) {
      const audioData = msg.audio.data
      if (typeof audioData === 'string') {
        if (audioData.startsWith('data:image/')) {
          images.push(audioData)
        } else {
          images.push('data:image/png;base64,' + audioData)
        }
      }
    }

    if (images.length === 0 && data.images) {
      for (const img of data.images) {
        if (typeof img === 'string') images.push(img)
        else if (img?.url) images.push(img.url)
        else if (img?.b64_json) images.push('data:image/png;base64,' + img.b64_json)
      }
    }

    if (images.length === 0 && !text) {
      const preview = JSON.stringify(data).slice(0, 500)
      throw new Error('生成图片失败，API 返回了无法解析的内容。\n\nAPI 原始响应:\n```json\n' + preview + '\n```')
    }

    return { images, text }
  }

  throw lastError || new Error('生成图片失败')
}

export async function* streamChat(
  messages: ChatMessage[],
  model: string,
  settings: AppSettings,
  signal?: AbortSignal,
  enableThinking?: boolean,
  tools?: Array<{ type: string; function: { name: string; description?: string; parameters: Record<string, unknown> } }>,
  mcpMode?: boolean,
): AsyncGenerator<StreamChunk> {
  if (!settings.apiKey) {
    throw new Error('Please configure your API key in Settings first.')
  }

  const body: Record<string, unknown> = {
    model,
    messages,
    stream: true,
  }

  // Claude 4.5 系列模型 max_tokens 限制为 64000
  const isClaude45 = model.includes('claude-4-5')
  body.max_tokens = isClaude45 ? 32000 : 65536

  if (enableThinking) {
    // Claude 4.5 的 thinking budget 也需要相应调小
    body.thinking = { type: 'enabled', budget_tokens: isClaude45 ? 32000 : 64000 }
    body.temperature = 1
  } else {
    body.temperature = 0.7
  }

  if (tools?.length) {
    body.tools = tools
    body.tool_choice = 'auto'
  }

  console.log('[LLM] 请求参数:', { model, max_tokens: body.max_tokens, hasThinking: !!body.thinking, toolsCount: tools?.length || 0 })

  const idleTimeout = mcpMode ? STREAM_IDLE_TIMEOUT_MCP_MS : STREAM_IDLE_TIMEOUT_MS
  const fetchTimeout = new AbortController()
  const fetchTimer = setTimeout(() => fetchTimeout.abort(), idleTimeout)
  if (signal) {
    signal.addEventListener('abort', () => fetchTimeout.abort(), { once: true })
  }

  let response: Response | null = null
  for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
    if (attempt > 0) {
      console.log(`[LLM] 重试第 ${attempt} 次...`)
      await sleep(RETRY_DELAY_MS * attempt)
    }

    try {
      console.log('[LLM] 发送请求到:', settings.apiBaseUrl + '/chat/completions')
      const startTime = Date.now()
      response = await fetch(settings.apiBaseUrl + '/chat/completions', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': 'Bearer ' + settings.apiKey,
        },
        body: JSON.stringify(body),
        signal: fetchTimeout.signal,
      })
      console.log(`[LLM] 收到响应: ${response.status} (耗时 ${Date.now() - startTime}ms)`)
    } catch (err) {
      clearTimeout(fetchTimer)
      console.error('[LLM] 请求失败:', err)
      if (signal?.aborted) throw err
      throw new Error('请求超时或网络错误，可能是文件过大导致上传失败。请尝试减小文件大小。')
    }

    clearTimeout(fetchTimer)

    if (response.ok) break

    const errorText = await response.text()
    if (shouldRetry(response.status) && attempt < MAX_RETRIES) continue
    throw new Error(friendlyError(response.status, errorText))
  }

  const reader = response!.body?.getReader()
  if (!reader) throw new Error('No response body')

  console.log('[LLM] 开始读取流...')
  const decoder = new TextDecoder()
  let buffer = ''
  let hasContent = false
  let hasText = false
  let lastStreamError = ''
  let streamDone = false
  let chunkCount = 0
  const accToolCalls = new Map<number, { id: string; name: string; arguments: string }>()

  while (!streamDone) {
    const readPromise = reader.read()
    const timeoutPromise = new Promise<never>((_, reject) =>
      setTimeout(() => reject(new Error('模型响应超时，请稍后重试')), idleTimeout)
    )

    const { done, value } = await Promise.race([readPromise, timeoutPromise])
    if (done) {
      console.log(`[LLM] 流读取完成，共 ${chunkCount} 个 chunk`)
      break
    }

    chunkCount++
    if (chunkCount === 1) {
      console.log('[LLM] 收到第一个 chunk')
    }
    if (done) break

    buffer += decoder.decode(value, { stream: true })
    const lines = buffer.split('\n')
    buffer = lines.pop() || ''

    for (const line of lines) {
      const trimmed = line.trim()
      if (!trimmed || !trimmed.startsWith('data: ')) continue

      const data = trimmed.slice(6)
      if (data === '[DONE]') { streamDone = true; break }

      try {
        const parsed = JSON.parse(data)

        if (parsed.error) {
          lastStreamError = parsed.error.message || JSON.stringify(parsed.error)
          continue
        }

        const delta = parsed.choices?.[0]?.delta
        if (!delta) continue

        const thinking = delta.reasoning_content ?? delta.thinking
        if (thinking) {
          hasContent = true
          yield { type: 'thinking', text: thinking }
        }

        const content = delta.content
        // 只处理真正的字符串内容，忽略 null、0、空字符串等
        if (typeof content === 'string' && content) {
          hasContent = true
          hasText = true
          yield { type: 'text', text: content }
        }

        if (delta.tool_calls) {
          hasContent = true
          for (const tc of delta.tool_calls as any[]) {
            const idx: number = tc.index ?? 0
            const existing = accToolCalls.get(idx)
            if (!existing) {
              accToolCalls.set(idx, {
                id: tc.id || '',
                name: tc.function?.name || '',
                arguments: tc.function?.arguments || '',
              })
            } else {
              if (tc.id) existing.id = tc.id
              if (tc.function?.name) existing.name += tc.function.name
              if (tc.function?.arguments) existing.arguments += tc.function.arguments
            }
          }
        }
      } catch {
        // skip malformed chunks
      }
    }
  }

  if (accToolCalls.size > 0) {
    yield { type: 'tool_calls', text: '', toolCalls: Array.from(accToolCalls.values()) }
    return
  }

  if (!hasContent) {
    if (lastStreamError) {
      throw new Error('API 返回错误: ' + lastStreamError)
    }
    throw new Error('模型未返回任何内容，可能不支持当前输入格式（如 PDF）。请尝试更换模型或减小文件大小。')
  }

  if (hasContent && !hasText) {
    throw new Error('模型在思考过程中意外中断，未生成回复内容，请点击重试。')
  }
}


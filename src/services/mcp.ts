import type { AppSettings } from '../types'

type JsonRpcId = number

interface JsonRpcRequest {
  jsonrpc: '2.0'
  id: JsonRpcId
  method: string
  params?: Record<string, unknown>
}

interface JsonRpcSuccess {
  jsonrpc: '2.0'
  id: JsonRpcId
  result: any
}

interface JsonRpcError {
  jsonrpc: '2.0'
  id: JsonRpcId
  error: { code: number; message: string; data?: any }
}

type JsonRpcResponse = JsonRpcSuccess | JsonRpcError

let nextId = 1
let cachedSessionId: string | null = null
let initialized = false

// ============ 稳定性配置 ============
const MCP_CALL_TIMEOUT_MS = 300_000        // 单次 MCP 调用超时：5 分钟
const MCP_LONG_CALL_TIMEOUT_MS = 3600_000  // 长时任务超时：60 分钟（撰写超长 PRD 等）
const MCP_MAX_RETRIES = 3                   // 最大重试次数
const MCP_RETRY_DELAY_MS = 2000             // 重试间隔基数
const MCP_SESSION_ERROR_CODES = [-32001, -32002, -32600]  // 需要重新初始化的错误码

// 长时任务工具名单（这些工具允许更长的超时时间）
const LONG_RUNNING_TOOLS = [
  // 文档写入类
  'create_document', 'update_document', 'append_document',
  'write_doc', 'create_doc', 'update_doc',
  'km_create', 'km_update', 'km_append',
  // 文档读取类（超长文档读取也需要更长时间）
  'get_doc_detail', 'get_document', 'read_doc',
  // 表格读取类
  'get_sheet_meta', 'read_sheet', 'get_sheet', 'read_spreadsheet',
  // 检索类（大知识库检索可能较慢）
  'retrieve', 'search',
]

function isLongRunningTool(toolName: string): boolean {
  return LONG_RUNNING_TOOLS.some(t => toolName.toLowerCase().includes(t.toLowerCase()))
}

function sleep(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms))
}

function getMcpUrl(settings: AppSettings): string {
  const base = (settings.mcpBaseUrl || '').trim()
  if (!base) throw new Error('MCP Base URL 未配置，请在设置中填写。')
  return base.replace(/\/+$/, '') + '/mcp'
}

function buildHeaders(settings: AppSettings): Record<string, string> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    // MCP Server 要求同时接受 application/json 和 text/event-stream
    'Accept': 'application/json, text/event-stream',
    // 2025-03-26 是 MCP 的默认协商版本（服务端在无 header 时也会默认）
    'mcp-protocol-version': '2025-03-26',
  }

  // Wave Open Platform MCP 使用 app_id/app_secret header 鉴权
  const appId = (settings.mcpAppId || '').trim()
  const appSecret = (settings.mcpAppSecret || '').trim()
  if (appId && appSecret) {
    headers['app_id'] = appId
    headers['app_secret'] = appSecret
  }

  if (cachedSessionId) headers['mcp-session-id'] = cachedSessionId
  return headers
}

// 判断错误是否可重试
function isRetryableError(error: any): boolean {
  if (error instanceof Error) {
    const msg = error.message.toLowerCase()
    // 网络错误、超时、服务端临时错误
    if (msg.includes('timeout') || msg.includes('network') || msg.includes('fetch')) return true
    if (msg.includes('502') || msg.includes('503') || msg.includes('504')) return true
    if (msg.includes('429')) return true  // 限流
  }
  return false
}

// 判断是否需要重新初始化会话
function needsReinitialization(error: any): boolean {
  if (error instanceof Error) {
    const msg = error.message
    if (msg.includes('session') || msg.includes('Session')) return true
    if (msg.includes('initialized') || msg.includes('Initialized')) return true
    // 检查错误码
    for (const code of MCP_SESSION_ERROR_CODES) {
      if (msg.includes(String(code))) return true
    }
  }
  return false
}

async function jsonRpcWithRetry(
  settings: AppSettings,
  req: JsonRpcRequest,
  timeoutMs: number = MCP_CALL_TIMEOUT_MS
): Promise<any> {
  let lastError: Error | null = null

  for (let attempt = 0; attempt <= MCP_MAX_RETRIES; attempt++) {
    if (attempt > 0) {
      // 指数退避重试
      const delay = MCP_RETRY_DELAY_MS * Math.pow(2, attempt - 1)
      console.log(`[MCP] 第 ${attempt} 次重试，等待 ${delay}ms...`)
      await sleep(delay)
    }

    try {
      const result = await jsonRpcCore(settings, req, timeoutMs)
      return result
    } catch (error) {
      lastError = error as Error
      console.warn(`[MCP] 请求失败 (attempt ${attempt + 1}/${MCP_MAX_RETRIES + 1}):`, error)

      // 如果是会话错误，尝试重新初始化
      if (needsReinitialization(error)) {
        console.log('[MCP] 检测到会话错误，重新初始化...')
        initialized = false
        cachedSessionId = null
        try {
          await ensureMcpInitialized(settings)
        } catch (initError) {
          console.error('[MCP] 重新初始化失败:', initError)
        }
        continue
      }

      // 如果不是可重试的错误，直接抛出
      if (!isRetryableError(error)) {
        throw error
      }
    }
  }

  throw lastError || new Error('MCP 请求失败，已达最大重试次数')
}

async function jsonRpcCore(
  settings: AppSettings,
  req: JsonRpcRequest,
  timeoutMs: number
): Promise<any> {
  const url = getMcpUrl(settings)

  // 创建超时控制
  const controller = new AbortController()
  const timeoutId = setTimeout(() => {
    controller.abort()
  }, timeoutMs)

  try {
    const res = await fetch(url, {
      method: 'POST',
      headers: buildHeaders(settings),
      body: JSON.stringify(req),
      signal: controller.signal,
    })

    if (!res.ok) {
      const text = await res.text().catch(() => '')
      throw new Error('MCP 请求失败: ' + res.status + ' ' + res.statusText + (text ? '\n' + text : ''))
    }

    const sid = res.headers.get('mcp-session-id')
    if (sid) cachedSessionId = sid

    // MCP Server 返回 SSE 格式（event: message\ndata: {...}）或纯 JSON
    const text = await res.text()
    let data: JsonRpcResponse

    // 检查是否是 SSE 格式
    if (text.startsWith('event:') || text.includes('\ndata:')) {
      // 解析 SSE 格式，提取 data 行
      const dataMatch = text.match(/^data:\s*(.+)$/m)
      if (!dataMatch) {
        throw new Error('MCP 响应格式错误: 无法解析 SSE 数据')
      }
      data = JSON.parse(dataMatch[1])
    } else {
      // 纯 JSON 格式
      data = JSON.parse(text)
    }

    if ('error' in data) {
      const details = data.error?.data ? '\n' + JSON.stringify(data.error.data, null, 2) : ''
      const err = new Error('MCP 错误: ' + data.error.message + details)
      ;(err as any).code = data.error.code
      throw err
    }
    return data.result
  } catch (error) {
    if ((error as Error).name === 'AbortError') {
      throw new Error(`MCP 请求超时 (${Math.round(timeoutMs / 1000)}s)`)
    }
    throw error
  } finally {
    clearTimeout(timeoutId)
  }
}

export async function ensureMcpInitialized(settings: AppSettings): Promise<void> {
  if (initialized) return

  // initialize - 初始化使用较短的超时
  await jsonRpcCore(settings, {
    jsonrpc: '2.0',
    id: nextId++,
    method: 'initialize',
    params: {
      protocolVersion: '2025-03-26',
      capabilities: {},
      clientInfo: { name: 'xueyu-assistant', version: '1.0.0' },
    },
  }, 30_000)

  // notifications/initialized（无需响应，但我们仍用 JSON-RPC notification 形式发）
  await fetch(getMcpUrl(settings), {
    method: 'POST',
    headers: buildHeaders(settings),
    body: JSON.stringify({ jsonrpc: '2.0', method: 'notifications/initialized', params: {} }),
  }).catch(() => {})

  initialized = true
}

export async function listMcpTools(settings: AppSettings): Promise<any> {
  await ensureMcpInitialized(settings)
  return await jsonRpcWithRetry(settings, {
    jsonrpc: '2.0',
    id: nextId++,
    method: 'tools/list',
    params: {},
  })
}

export async function callMcpTool(
  settings: AppSettings,
  name: string,
  args?: Record<string, unknown>
): Promise<any> {
  await ensureMcpInitialized(settings)

  // 根据工具类型选择超时时间
  const timeout = isLongRunningTool(name) ? MCP_LONG_CALL_TIMEOUT_MS : MCP_CALL_TIMEOUT_MS

  return await jsonRpcWithRetry(settings, {
    jsonrpc: '2.0',
    id: nextId++,
    method: 'tools/call',
    params: { name, arguments: args || {} },
  }, timeout)
}

export function resetMcpSession() {
  cachedSessionId = null
  initialized = false
  _cachedTools = null
  _cachedToolsKey = null
}

// ── 将 MCP tools 转为 OpenAI function calling 格式 ──────────────

export interface OpenAITool {
  type: 'function'
  function: {
    name: string
    description?: string
    parameters: Record<string, unknown>
  }
}

let _cachedTools: OpenAITool[] | null = null
let _cachedToolsKey: string | null = null

export async function getMcpToolsAsOpenAI(settings: AppSettings): Promise<OpenAITool[]> {
  const key = (settings.mcpBaseUrl || '') + '|' + (settings.mcpAppId || '')
  if (_cachedTools && _cachedToolsKey === key) return _cachedTools

  const result = await listMcpTools(settings)
  const mcpTools: any[] = result.tools || []
  _cachedTools = mcpTools.map(t => {
    const params = { ...t.inputSchema }
    if (params.properties) {
      // context 是 FastMCP 框架内部参数，不暴露给 LLM
      const { context: _ctx, ...rest } = params.properties
      params.properties = rest
      if (params.required) {
        params.required = params.required.filter((r: string) => r !== 'context')
      }
    }
    return {
      type: 'function' as const,
      function: {
        name: t.name,
        description: t.description,
        parameters: params,
      },
    }
  })
  _cachedToolsKey = key
  return _cachedTools!
}


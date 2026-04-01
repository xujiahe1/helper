import type { AppSettings } from '../types'
import { getAccessToken } from './auth'

export interface DocRetrieveResult {
  meta: {
    doc_url: string
    doc_id: string
    title: string
  }
  score: number
  content: string
}

interface DocRetrieveResponse {
  retcode: number
  message: string
  data: {
    invalid_scope: {
      knowledge_id_list: string[]
      doc_id_list: string[]
    }
    result: DocRetrieveResult[]
  }
}

// 文档详情接口返回结构
export interface DocDetailInfo {
  doc_id: string
  parent_doc_id?: string
  workspace_id?: string
  knowledge_id?: string
  title: string
  owner?: {
    id: string
    id_type: string
    tenant_id: string
  }
  doc_type?: string
  content?: string
  create_time?: string
  update_time?: string
  last_modifier?: {
    id: string
    id_type: string
    tenant_id: string
  }
  is_archived?: boolean
}

interface DocDetailResponse {
  retcode: number
  message: string
  data: {
    info: DocDetailInfo
  }
}

/**
 * 获取文档详情（包含标题）
 */
export async function getDocumentDetail(
  docId: string,
  settings: AppSettings,
): Promise<DocDetailInfo> {
  if (!settings.documentApiBaseUrl || !settings.documentAppId) {
    throw new Error('文档 API 未配置')
  }

  const token = await getAccessToken(settings)

  const response = await fetch(
    settings.documentApiBaseUrl + '/openapi/docs/v1/doc/detail/get',
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json; charset=utf-8',
        'Authorization': token,
      },
      body: JSON.stringify({
        doc_id: docId,
      }),
    },
  )

  if (!response.ok) {
    throw new Error('Document API Error: ' + response.statusText)
  }

  const data: DocDetailResponse = await response.json()
  if (data.retcode !== 0) {
    throw new Error('Document API Error: ' + data.message)
  }

  return data.data.info
}

export async function retrieveDocuments(
  query: string,
  docIds: string[],
  settings: AppSettings,
): Promise<DocRetrieveResult[]> {
  if (!settings.documentApiBaseUrl || !settings.documentAppId || docIds.length === 0) {
    return []
  }

  const token = await getAccessToken(settings)

  const response = await fetch(
    settings.documentApiBaseUrl + '/openapi/docs/v1/doc/retrieve',
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json; charset=utf-8',
        'Authorization': token,
      },
      body: JSON.stringify({
        scope: {
          doc_id_list: docIds,
        },
        query,
        setting: {
          top_k: 20,
          score_threshold: 0,
        },
      }),
    },
  )

  if (!response.ok) {
    throw new Error('Document API Error: ' + response.statusText)
  }

  const data: DocRetrieveResponse = await response.json()
  if (data.retcode !== 0) {
    throw new Error('Document API Error: ' + data.message)
  }

  return data.data.result || []
}

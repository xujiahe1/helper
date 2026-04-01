import type { AppSettings } from '../types'

let cachedToken: string | null = null
let tokenExpiry = 0

interface TokenResponse {
  retcode: number
  message: string
  data: {
    access_token: string
    expire: number
  }
}

export async function getAccessToken(settings: AppSettings): Promise<string> {
  const now = Date.now()
  // expire is unix timestamp in seconds, convert to ms for comparison
  if (cachedToken && tokenExpiry * 1000 - now > 30 * 60 * 1000) {
    return cachedToken
  }

  if (!settings.documentApiBaseUrl || !settings.documentAppId || !settings.documentAppSecret) {
    throw new Error('Document API credentials not configured. Check Settings.')
  }

  const response = await fetch(
    settings.documentApiBaseUrl + '/openapi/auth/v1/access_token/internal',
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json; charset=utf-8',
      },
      body: JSON.stringify({
        app_id: settings.documentAppId,
        app_secret: settings.documentAppSecret,
      }),
    },
  )

  if (!response.ok) {
    throw new Error('Auth Error: ' + response.statusText)
  }

  const data: TokenResponse = await response.json()
  if (data.retcode !== 0) {
    throw new Error('Auth Error: ' + data.message)
  }

  cachedToken = data.data.access_token
  tokenExpiry = data.data.expire

  return cachedToken
}

export function clearTokenCache() {
  cachedToken = null
  tokenExpiry = 0
}

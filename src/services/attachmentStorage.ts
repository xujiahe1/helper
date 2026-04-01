/**
 * 附件存储服务
 * 使用 IndexedDB 存储大文件（图片、PDF、Excel 等）
 * 解决 localStorage 5MB 限制问题
 */

const DB_NAME = 'wave-chat-attachments'
const DB_VERSION = 1
const STORE_NAME = 'attachments'

interface StoredAttachment {
  id: string  // attachment id
  conversationId: string
  messageId: string
  dataUrl: string
  createdAt: number
}

let dbPromise: Promise<IDBDatabase> | null = null

function openDB(): Promise<IDBDatabase> {
  if (dbPromise) return dbPromise

  dbPromise = new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION)

    request.onerror = () => {
      console.error('[AttachmentStorage] 打开数据库失败:', request.error)
      reject(request.error)
    }

    request.onsuccess = () => {
      resolve(request.result)
    }

    request.onupgradeneeded = (event) => {
      const db = (event.target as IDBOpenDBRequest).result

      if (!db.objectStoreNames.contains(STORE_NAME)) {
        const store = db.createObjectStore(STORE_NAME, { keyPath: 'id' })
        store.createIndex('conversationId', 'conversationId', { unique: false })
        store.createIndex('messageId', 'messageId', { unique: false })
      }
    }
  })

  return dbPromise
}

/**
 * 保存单个附件
 */
export async function saveAttachment(
  attachmentId: string,
  conversationId: string,
  messageId: string,
  dataUrl: string
): Promise<void> {
  if (!dataUrl) return

  try {
    const db = await openDB()
    const tx = db.transaction(STORE_NAME, 'readwrite')
    const store = tx.objectStore(STORE_NAME)

    const attachment: StoredAttachment = {
      id: attachmentId,
      conversationId,
      messageId,
      dataUrl,
      createdAt: Date.now(),
    }

    store.put(attachment)

    await new Promise<void>((resolve, reject) => {
      tx.oncomplete = () => resolve()
      tx.onerror = () => reject(tx.error)
    })
  } catch (error) {
    console.error('[AttachmentStorage] 保存附件失败:', error)
  }
}

/**
 * 批量保存附件（用于消息发送后）
 */
export async function saveAttachments(
  conversationId: string,
  messageId: string,
  attachments: Array<{ id: string; dataUrl?: string }>
): Promise<void> {
  const validAttachments = attachments.filter(a => a.dataUrl)
  if (validAttachments.length === 0) return

  try {
    const db = await openDB()
    const tx = db.transaction(STORE_NAME, 'readwrite')
    const store = tx.objectStore(STORE_NAME)

    for (const att of validAttachments) {
      const stored: StoredAttachment = {
        id: att.id,
        conversationId,
        messageId,
        dataUrl: att.dataUrl!,
        createdAt: Date.now(),
      }
      store.put(stored)
    }

    await new Promise<void>((resolve, reject) => {
      tx.oncomplete = () => resolve()
      tx.onerror = () => reject(tx.error)
    })
  } catch (error) {
    console.error('[AttachmentStorage] 批量保存附件失败:', error)
  }
}

/**
 * 获取单个附件
 */
export async function getAttachment(attachmentId: string): Promise<string | null> {
  try {
    const db = await openDB()
    const tx = db.transaction(STORE_NAME, 'readonly')
    const store = tx.objectStore(STORE_NAME)

    return new Promise((resolve, reject) => {
      const request = store.get(attachmentId)
      request.onsuccess = () => {
        const result = request.result as StoredAttachment | undefined
        resolve(result?.dataUrl || null)
      }
      request.onerror = () => reject(request.error)
    })
  } catch (error) {
    console.error('[AttachmentStorage] 获取附件失败:', error)
    return null
  }
}

/**
 * 批量获取附件（按 ID 列表）
 */
export async function getAttachments(attachmentIds: string[]): Promise<Map<string, string>> {
  const result = new Map<string, string>()
  if (attachmentIds.length === 0) return result

  try {
    const db = await openDB()
    const tx = db.transaction(STORE_NAME, 'readonly')
    const store = tx.objectStore(STORE_NAME)

    await Promise.all(
      attachmentIds.map(
        (id) =>
          new Promise<void>((resolve) => {
            const request = store.get(id)
            request.onsuccess = () => {
              const stored = request.result as StoredAttachment | undefined
              if (stored?.dataUrl) {
                result.set(id, stored.dataUrl)
              }
              resolve()
            }
            request.onerror = () => resolve()
          })
      )
    )
  } catch (error) {
    console.error('[AttachmentStorage] 批量获取附件失败:', error)
  }

  return result
}

/**
 * 删除对话的所有附件
 */
export async function deleteConversationAttachments(conversationId: string): Promise<void> {
  try {
    const db = await openDB()
    const tx = db.transaction(STORE_NAME, 'readwrite')
    const store = tx.objectStore(STORE_NAME)
    const index = store.index('conversationId')

    const request = index.getAllKeys(IDBKeyRange.only(conversationId))

    await new Promise<void>((resolve, reject) => {
      request.onsuccess = () => {
        const keys = request.result
        for (const key of keys) {
          store.delete(key)
        }
        resolve()
      }
      request.onerror = () => reject(request.error)
    })

    await new Promise<void>((resolve, reject) => {
      tx.oncomplete = () => resolve()
      tx.onerror = () => reject(tx.error)
    })
  } catch (error) {
    console.error('[AttachmentStorage] 删除对话附件失败:', error)
  }
}

/**
 * 删除单个消息的所有附件
 */
export async function deleteMessageAttachments(messageId: string): Promise<void> {
  try {
    const db = await openDB()
    const tx = db.transaction(STORE_NAME, 'readwrite')
    const store = tx.objectStore(STORE_NAME)
    const index = store.index('messageId')

    const request = index.getAllKeys(IDBKeyRange.only(messageId))

    await new Promise<void>((resolve, reject) => {
      request.onsuccess = () => {
        const keys = request.result
        for (const key of keys) {
          store.delete(key)
        }
        resolve()
      }
      request.onerror = () => reject(request.error)
    })

    await new Promise<void>((resolve, reject) => {
      tx.oncomplete = () => resolve()
      tx.onerror = () => reject(tx.error)
    })
  } catch (error) {
    console.error('[AttachmentStorage] 删除消息附件失败:', error)
  }
}

/**
 * 获取存储使用情况
 */
export async function getStorageUsage(): Promise<{ count: number; estimatedSize: number }> {
  try {
    const db = await openDB()
    const tx = db.transaction(STORE_NAME, 'readonly')
    const store = tx.objectStore(STORE_NAME)

    return new Promise((resolve, reject) => {
      const countRequest = store.count()
      countRequest.onsuccess = () => {
        const count = countRequest.result
        // 估算大小：遍历计算 dataUrl 长度
        const getAllRequest = store.getAll()
        getAllRequest.onsuccess = () => {
          const all = getAllRequest.result as StoredAttachment[]
          const estimatedSize = all.reduce((sum, a) => sum + (a.dataUrl?.length || 0), 0)
          resolve({ count, estimatedSize })
        }
        getAllRequest.onerror = () => resolve({ count, estimatedSize: 0 })
      }
      countRequest.onerror = () => reject(countRequest.error)
    })
  } catch (error) {
    console.error('[AttachmentStorage] 获取存储使用情况失败:', error)
    return { count: 0, estimatedSize: 0 }
  }
}

/**
 * 清空所有附件存储
 */
export async function clearAllAttachments(): Promise<void> {
  try {
    const db = await openDB()
    const tx = db.transaction(STORE_NAME, 'readwrite')
    const store = tx.objectStore(STORE_NAME)
    store.clear()

    await new Promise<void>((resolve, reject) => {
      tx.oncomplete = () => resolve()
      tx.onerror = () => reject(tx.error)
    })
  } catch (error) {
    console.error('[AttachmentStorage] 清空存储失败:', error)
  }
}

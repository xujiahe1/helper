/**
 * 实体归一化服务
 * 负责将原始实体归一化为统一实体，检测冲突
 *
 * 简化版：移除面向分类，直接用 LLM 判断冲突
 */

import { v4 as uuidv4 } from 'uuid'
import type { AppSettings } from '../types'
import type {
  PrdDocument,
  NormalizedEntity,
  EntityVersion,
} from '../stores/prdStore'
import { callLLM, extractJsonFromText } from './llm'

// ========== 归一化处理 ==========

/**
 * 从文档列表构建归一化实体
 */
export function buildNormalizedEntities(documents: PrdDocument[]): NormalizedEntity[] {
  const entityMap = new Map<string, {
    names: Set<string>
    versions: EntityVersion[]
  }>()

  // 第一步：按名称分组
  for (const doc of documents) {
    if (doc.status !== 'done') continue

    for (const entity of doc.entities) {
      const key = entity.name.toLowerCase()

      if (!entityMap.has(key)) {
        entityMap.set(key, { names: new Set(), versions: [] })
      }

      const group = entityMap.get(key)!
      group.names.add(entity.name)
      group.versions.push({
        rawEntityId: entity.id,
        docId: doc.id,
        docTitle: doc.title,
        docUpdateTime: doc.docUpdateTime || doc.updatedAt,
        description: entity.description,
        extractedAt: entity.createdAt || Date.now(),
      })
    }
  }

  // 第二步：构建归一化实体
  const now = Date.now()
  const normalized: NormalizedEntity[] = []

  for (const [key, group] of entityMap) {
    // 按文档更新时间降序排序
    const sortedVersions = group.versions.sort((a, b) => b.docUpdateTime - a.docUpdateTime)
    const latest = sortedVersions[0]
    const aliases = Array.from(group.names)

    normalized.push({
      id: uuidv4(),
      canonicalName: aliases[0], // 使用第一个出现的名称
      aliases,
      versions: sortedVersions,
      relations: [],
      knowledgeBaseId: '',  // 由 runNormalization 调用时统一赋值
      currentDescription: latest.description,
      currentDocId: latest.docId,
      currentDocTitle: latest.docTitle,
      currentDocUpdateTime: latest.docUpdateTime,
      hasConflict: false,
      normalizationMethod: 'auto',
      createdAt: now,
      updatedAt: now,
    })
  }

  return normalized.sort((a, b) => b.versions.length - a.versions.length)
}

/**
 * LLM 同义词归一化
 */
async function normalizeWithLLM(
  settings: AppSettings,
  entities: NormalizedEntity[]
): Promise<{ merges: Array<{ source: string; target: string; reason: string }> }> {
  if (entities.length < 2) return { merges: [] }

  const entityNames = entities.map(e => e.canonicalName)

  const prompt = `你是一个术语归一化专家。请分析以下业务实体名称，找出指代同一概念的同义词/近义词对。

## 实体列表
${entityNames.map((n, i) => `${i + 1}. ${n}`).join('\n')}

## 判断标准
- 同义词：完全相同含义（如"域账号"和"AD账号"）
- 近义词：在该业务场景下可互换使用
- 注意多义词：如"工单"可能指IT工单或业务工单，需结合上下文

## 返回格式
返回 JSON 数组，每项包含：
- source: 应被合并的实体名
- target: 合并到的目标实体名（保留这个）
- reason: 合并理由（一句话）

如果没有需要合并的，返回空数组 []。只返回 JSON。

示例：
[{"source": "AD账号", "target": "域账号", "reason": "同指企业域控账号体系"}]
`

  try {
    const text = await callLLM(settings, prompt, { maxTokens: 2048 })
    const merges = extractJsonFromText<Array<{ source: string; target: string; reason: string }>>(text, 'array')
    return { merges: merges ?? [] }
  } catch (e) {
    console.error('[Normalize] LLM归一化失败:', e)
  }

  return { merges: [] }
}

/**
 * 简化版冲突检测：直接用 LLM 判断多个描述是否有冲突
 */
async function detectConflicts(
  settings: AppSettings,
  entity: NormalizedEntity
): Promise<{ hasConflict: boolean; conflictSummary?: string }> {
  if (entity.versions.length < 2) {
    return { hasConflict: false }
  }

  // 构建描述列表，截断过长的描述
  const versionsInfo = entity.versions.map((v, i) => {
    const desc = v.description.length > 500
      ? v.description.slice(0, 500) + '...'
      : v.description
    return `${i + 1}. [${v.docTitle}] ${desc}`
  }).join('\n\n')

  const prompt = `分析以下同一业务概念「${entity.canonicalName}」在不同文档中的描述，判断是否存在定义冲突。

## 各文档的描述
${versionsInfo}

## 判断标准
- **无冲突**：
  - 描述互补（从不同角度描述同一事物）
  - 新定义是旧定义的更新/细化/补充
  - 描述的是同一事物的不同使用场景
- **有冲突**：
  - 对同一属性/特征给出不同值（如 A 文档说是 X，B 文档说是 Y）
  - 定义本质上矛盾（不可能同时为真）

## 返回 JSON
{
  "hasConflict": true 或 false,
  "conflictSummary": "如果有冲突，一句话描述冲突点（例如：'文档A定义为X，文档B定义为Y'）；无冲突则为空字符串"
}

只返回 JSON，不要其他内容。`

  try {
    const text = await callLLM(settings, prompt, { maxTokens: 512 })
    const result = extractJsonFromText<{
      hasConflict: boolean
      conflictSummary?: string
    }>(text, 'object')

    if (result) {
      return {
        hasConflict: result.hasConflict,
        conflictSummary: result.hasConflict ? result.conflictSummary : undefined,
      }
    }
  } catch (e) {
    console.error('[DetectConflicts] 冲突检测失败:', e)
  }

  return { hasConflict: false }
}

/**
 * 完整的归一化流程（简化版：移除面向分类）
 */
export async function runNormalization(
  settings: AppSettings,
  documents: PrdDocument[],
  existingNormalized: NormalizedEntity[],
  onProgress?: (status: string) => void,
  knowledgeBaseId?: string
): Promise<NormalizedEntity[]> {
  onProgress?.('构建实体索引...')

  // 保留手动归一化的实体（包括用户手动管理的别名）
  const manualEntities = existingNormalized.filter(e => e.normalizationMethod === 'manual')
  const manualAliases = new Set(manualEntities.flatMap(e => e.aliases.map(a => a.toLowerCase())))

  // 收集所有用户手动添加的别名映射（用于在新归一化结果中保留）
  // 使用所有别名作为 key，确保即使 canonicalName 变化也能匹配
  const manualAliasMap = new Map<string, string[]>()  // alias (lower) -> manualAliases
  for (const entity of existingNormalized) {
    if (entity.manualAliases && entity.manualAliases.length > 0) {
      // 用所有别名作为 key
      for (const alias of entity.aliases) {
        manualAliasMap.set(alias.toLowerCase(), entity.manualAliases)
      }
    }
  }

  // 收集已有的冲突解决状态（用于在新归一化结果中恢复）
  // 使用所有别名作为 key，确保即使 canonicalName 变化也能匹配
  const conflictResolutionMap = new Map<string, NormalizedEntity['conflictResolution']>()
  for (const entity of existingNormalized) {
    if (entity.conflictResolution) {
      // 用所有别名作为 key
      for (const alias of entity.aliases) {
        conflictResolutionMap.set(alias.toLowerCase(), entity.conflictResolution)
      }
    }
  }

  // 收集用户手动设置的 canonicalName（用于恢复）
  // 如果实体是 manual 方法或有手动别名，说明用户可能调整过 canonicalName
  const manualCanonicalNameMap = new Map<string, string>()  // alias (lower) -> canonicalName
  for (const entity of existingNormalized) {
    if (entity.normalizationMethod === 'manual' || entity.manualAliases?.length) {
      for (const alias of entity.aliases) {
        manualCanonicalNameMap.set(alias.toLowerCase(), entity.canonicalName)
      }
    }
  }

  // 构建自动归一化实体（排除已手动处理的）
  let autoEntities = buildNormalizedEntities(documents)
    .filter(e => !e.aliases.some(a => manualAliases.has(a.toLowerCase())))

  onProgress?.('LLM 同义词识别...')

  // LLM 同义词归一化
  if (autoEntities.length >= 2) {
    const { merges } = await normalizeWithLLM(settings, autoEntities)

    for (const merge of merges) {
      const sourceIdx = autoEntities.findIndex(e =>
        e.aliases.some(a => a.toLowerCase() === merge.source.toLowerCase())
      )
      const targetIdx = autoEntities.findIndex(e =>
        e.aliases.some(a => a.toLowerCase() === merge.target.toLowerCase())
      )

      if (sourceIdx !== -1 && targetIdx !== -1 && sourceIdx !== targetIdx) {
        const source = autoEntities[sourceIdx]
        const target = autoEntities[targetIdx]

        // 合并
        target.aliases = [...new Set([...target.aliases, ...source.aliases])]
        target.versions = [...target.versions, ...source.versions]
          .sort((a, b) => b.docUpdateTime - a.docUpdateTime)

        const latest = target.versions[0]
        target.currentDescription = latest.description
        target.currentDocId = latest.docId
        target.currentDocTitle = latest.docTitle
        target.currentDocUpdateTime = latest.docUpdateTime
        target.updatedAt = Date.now()

        // 移除被合并的
        autoEntities = autoEntities.filter((_, i) => i !== sourceIdx)
      }
    }
  }

  // 恢复用户手动添加的别名、冲突解决状态和 canonicalName
  for (const entity of autoEntities) {
    // 尝试用任意一个别名匹配
    let manualAliasesForEntity: string[] | undefined
    let existingResolution: NormalizedEntity['conflictResolution'] | undefined
    let existingCanonicalName: string | undefined

    for (const alias of entity.aliases) {
      const aliasLower = alias.toLowerCase()
      if (!manualAliasesForEntity) {
        manualAliasesForEntity = manualAliasMap.get(aliasLower)
      }
      if (!existingResolution) {
        existingResolution = conflictResolutionMap.get(aliasLower)
      }
      if (!existingCanonicalName) {
        existingCanonicalName = manualCanonicalNameMap.get(aliasLower)
      }
      if (manualAliasesForEntity && existingResolution && existingCanonicalName) break
    }

    // 恢复手动添加的别名
    if (manualAliasesForEntity) {
      entity.aliases = [...new Set([...entity.aliases, ...manualAliasesForEntity])]
      entity.manualAliases = manualAliasesForEntity
    }

    // 恢复用户设置的 canonicalName（如果它仍在别名列表中）
    if (existingCanonicalName && entity.aliases.includes(existingCanonicalName)) {
      entity.canonicalName = existingCanonicalName
    }

    // 恢复冲突解决状态
    if (existingResolution) {
      entity.conflictResolution = existingResolution
      // 如果之前已解决冲突，标记为手动处理过
      entity.normalizationMethod = 'manual'
    }
  }

  onProgress?.('检测版本冲突...')

  // 简化版冲突检测：直接用 LLM 判断
  for (const entity of autoEntities) {
    if (entity.versions.length >= 2) {
      const { hasConflict, conflictSummary } = await detectConflicts(settings, entity)
      entity.hasConflict = hasConflict
      entity.conflictSummary = conflictSummary
    }
  }

  // 合并手动和自动实体，并统一标记所属知识库
  const result = [...manualEntities, ...autoEntities]
    .sort((a, b) => b.versions.length - a.versions.length)

  if (knowledgeBaseId) {
    return result.map(e => ({ ...e, knowledgeBaseId }))
  }
  return result
}

/**
 * 关系提取服务
 * 从 PRD 文档中提取实体之间的关系，并检测关系冲突
 */

import type { AppSettings } from '../types'
import type { PrdDocument, NormalizedEntity, EntityRelation, RelationSource, RelationConflictInfo } from '../stores/prdStore'
import { callLLM, extractJsonFromText } from './llm'
import { v4 as uuidv4 } from 'uuid'

/** 提取结果：从单个文档中提取的关系 */
interface ExtractedRelation {
  sourceEntity: string      // 源实体名称
  relationType: string      // 关系类型
  targetEntity: string      // 目标实体名称
  description?: string      // 关系描述
  anchor?: string           // 文档锚点（章节位置）
  confidence: number        // 置信度
}

/**
 * 从单个文档中提取实体关系
 */
export async function extractRelationsFromDocument(
  settings: AppSettings,
  doc: PrdDocument,
  normalizedEntities: NormalizedEntity[],
  onProgress?: (status: string) => void
): Promise<ExtractedRelation[]> {
  if (!doc.rawContent || doc.rawContent.length < 50) {
    return []
  }

  // 构建实体名称列表（包含别名）
  const entityNames = new Set<string>()
  for (const ne of normalizedEntities) {
    entityNames.add(ne.canonicalName)
    for (const alias of ne.aliases) {
      entityNames.add(alias)
    }
  }

  if (entityNames.size < 2) {
    return []
  }

  const entityList = Array.from(entityNames).join('、')

  const systemPrompt = `你是一个专业的知识图谱构建专家，擅长从产品文档中提取实体之间的关系。

任务：分析给定的文档内容，提取其中涉及的实体关系。

## 已知实体列表
${entityList}

## 提取规则
1. 只提取上述已知实体之间的关系，不要创造新实体
2. 关系类型应该是描述性的动词或短语，如："依赖"、"包含"、"基于...生成"、"调用"、"继承"、"配置于"等
3. 关系应该有明确的方向性（从源实体指向目标实体）
4. 如果文档中明确提到了章节位置，记录在 anchor 字段
5. 根据文档中描述的确定性给出置信度（0.5-1.0）

## 输出格式
返回 JSON 数组：
[
  {
    "sourceEntity": "实体A",
    "relationType": "依赖",
    "targetEntity": "实体B",
    "description": "实体A 依赖实体B 进行数据校验",
    "anchor": "3.2",
    "confidence": 0.9
  }
]

如果没有发现有效的关系，返回空数组 []`

  // 与实体抽取保持一致的截断限制
  const maxContentLength = 50000
  const truncatedContent = doc.rawContent.slice(0, maxContentLength)
  const isTruncated = doc.rawContent.length > maxContentLength

  const userPrompt = `请分析以下文档内容，提取实体之间的关系：

【文档：${doc.title}】
${truncatedContent}

${isTruncated ? '（内容已截断）' : ''}

请提取其中涉及的实体关系，以 JSON 数组格式返回。`

  try {
    onProgress?.(`正在分析文档：${doc.title}`)

    const fullPrompt = `${systemPrompt}

${userPrompt}`

    const response = await callLLM(settings, fullPrompt)

    const relations = extractJsonFromText(response)
    if (!Array.isArray(relations)) {
      console.warn('[RelationExtraction] 返回格式不正确')
      return []
    }

    // 验证和清理
    return relations
      .filter(r =>
        r.sourceEntity &&
        r.relationType &&
        r.targetEntity &&
        entityNames.has(r.sourceEntity) &&
        entityNames.has(r.targetEntity) &&
        r.sourceEntity !== r.targetEntity
      )
      .map(r => ({
        sourceEntity: r.sourceEntity,
        relationType: r.relationType,
        targetEntity: r.targetEntity,
        description: r.description || undefined,
        anchor: r.anchor || undefined,
        confidence: typeof r.confidence === 'number' ? Math.min(Math.max(r.confidence, 0.5), 1) : 0.8,
      }))
  } catch (error) {
    console.error('[RelationExtraction] 提取失败:', error)
    return []
  }
}

/**
 * 将提取的关系转换为 EntityRelation 格式，并关联到实体
 */
export function convertToEntityRelations(
  extractedRelations: ExtractedRelation[],
  doc: PrdDocument,
  normalizedEntities: NormalizedEntity[]
): Map<string, EntityRelation[]> {
  // 构建名称到实体 ID 的映射
  const nameToEntityId = new Map<string, string>()
  for (const ne of normalizedEntities) {
    nameToEntityId.set(ne.canonicalName.toLowerCase(), ne.id)
    for (const alias of ne.aliases) {
      nameToEntityId.set(alias.toLowerCase(), ne.id)
    }
  }

  // 按源实体 ID 分组
  const result = new Map<string, EntityRelation[]>()

  for (const rel of extractedRelations) {
    const sourceEntityId = nameToEntityId.get(rel.sourceEntity.toLowerCase())
    const targetEntityId = nameToEntityId.get(rel.targetEntity.toLowerCase())

    if (!sourceEntityId || !targetEntityId) continue

    const sourceEntity = normalizedEntities.find(e => e.id === sourceEntityId)
    const targetEntity = normalizedEntities.find(e => e.id === targetEntityId)
    if (!sourceEntity || !targetEntity) continue

    const relationSource: RelationSource = {
      docId: doc.docId,
      docTitle: doc.title,
      anchor: rel.anchor,
    }

    // 为源实体添加 outgoing 关系
    const outgoingRelation: EntityRelation = {
      id: uuidv4(),
      targetEntityId: targetEntityId,
      targetEntityName: targetEntity.canonicalName,
      relationType: rel.relationType,
      direction: 'outgoing',
      description: rel.description,
      sources: [relationSource],
      confidence: rel.confidence,
      method: 'auto',
      createdAt: Date.now(),
      updatedAt: Date.now(),
    }

    if (!result.has(sourceEntityId)) {
      result.set(sourceEntityId, [])
    }
    result.get(sourceEntityId)!.push(outgoingRelation)

    // 为目标实体添加 incoming 关系（双向存储）
    const incomingRelation: EntityRelation = {
      id: uuidv4(),
      targetEntityId: sourceEntityId,
      targetEntityName: sourceEntity.canonicalName,
      relationType: rel.relationType,
      direction: 'incoming',
      description: rel.description,
      sources: [relationSource],
      confidence: rel.confidence,
      method: 'auto',
      createdAt: Date.now(),
      updatedAt: Date.now(),
    }

    if (!result.has(targetEntityId)) {
      result.set(targetEntityId, [])
    }
    result.get(targetEntityId)!.push(incomingRelation)
  }

  return result
}

/**
 * 合并新关系到已有关系中（去重）
 */
export function mergeRelations(
  existingRelations: EntityRelation[],
  newRelations: EntityRelation[]
): EntityRelation[] {
  const result = [...existingRelations]

  for (const newRel of newRelations) {
    // 检查是否已存在相同的关系（相同目标、相同类型、相同方向）
    const existing = result.find(r =>
      r.targetEntityId === newRel.targetEntityId &&
      r.relationType === newRel.relationType &&
      r.direction === newRel.direction
    )

    if (existing) {
      // 合并来源（去重）
      const existingSourceIds = new Set(existing.sources.map(s => `${s.docId}-${s.anchor || ''}`))
      for (const src of newRel.sources) {
        const srcId = `${src.docId}-${src.anchor || ''}`
        if (!existingSourceIds.has(srcId)) {
          existing.sources.push(src)
        }
      }
      // 更新置信度（取较高值）
      existing.confidence = Math.max(existing.confidence, newRel.confidence)
      existing.updatedAt = Date.now()
    } else {
      result.push(newRel)
    }
  }

  return result
}

/**
 * 批量提取关系（从多个文档）
 */
export async function extractRelationsFromDocuments(
  settings: AppSettings,
  docs: PrdDocument[],
  normalizedEntities: NormalizedEntity[],
  onProgress?: (status: string) => void
): Promise<Map<string, EntityRelation[]>> {
  const allRelationsByEntity = new Map<string, EntityRelation[]>()

  for (let i = 0; i < docs.length; i++) {
    const doc = docs[i]
    onProgress?.(`提取关系 (${i + 1}/${docs.length}): ${doc.title}`)

    try {
      const extracted = await extractRelationsFromDocument(
        settings,
        doc,
        normalizedEntities,
        onProgress
      )

      if (extracted.length > 0) {
        const converted = convertToEntityRelations(extracted, doc, normalizedEntities)

        // 合并到总结果
        for (const [entityId, relations] of converted) {
          const existing = allRelationsByEntity.get(entityId) || []
          allRelationsByEntity.set(entityId, mergeRelations(existing, relations))
        }
      }
    } catch (error) {
      console.error(`[RelationExtraction] 文档 ${doc.title} 处理失败:`, error)
    }
  }

  return allRelationsByEntity
}

// ========== 关系冲突检测 ==========

/**
 * 检测单个实体的关系冲突
 * 对同一目标实体有多个不同类型的关系时，用 LLM 判断是否矛盾
 */
async function detectConflictsForEntity(
  settings: AppSettings,
  entityName: string,
  relations: EntityRelation[]
): Promise<RelationConflictInfo[]> {
  // 按目标实体分组
  const byTarget = new Map<string, EntityRelation[]>()
  for (const rel of relations) {
    if (rel.direction !== 'outgoing') continue  // 只检查 outgoing 方向，避免重复检测
    const key = rel.targetEntityId
    if (!byTarget.has(key)) {
      byTarget.set(key, [])
    }
    byTarget.get(key)!.push(rel)
  }

  const conflicts: RelationConflictInfo[] = []

  // 对每个目标实体检查是否有冲突
  for (const [targetId, rels] of byTarget) {
    // 只有当有多个不同类型的关系时才需要检测
    const uniqueTypes = new Set(rels.map(r => r.relationType))
    if (uniqueTypes.size < 2) continue

    const targetName = rels[0].targetEntityName

    // 构建关系描述列表
    const relDescriptions = rels.map((r, i) => {
      const sources = r.sources.map(s => s.docTitle).join('、')
      return `${i + 1}. 关系类型「${r.relationType}」${r.description ? `：${r.description}` : ''}（来源：${sources}）`
    }).join('\n')

    const prompt = `分析以下实体「${entityName}」与实体「${targetName}」之间的多个关系描述，判断是否存在矛盾。

## 关系列表
${relDescriptions}

## 判断标准
- **无矛盾**：
  - 关系类型是互补的（如"依赖"和"调用"可以同时成立）
  - 关系类型描述的是不同方面（如"管理"和"包含"）
  - 新关系是旧关系的细化或补充
- **有矛盾**：
  - 关系类型是互斥的（如"依赖"和"不依赖"）
  - 关系类型是逆向的且不可能同时成立（如 A 包含 B，同时 B 包含 A）
  - 关系描述在本质上冲突

## 返回 JSON
{
  "hasConflict": true 或 false,
  "conflictSummary": "如果有矛盾，一句话描述矛盾点；无矛盾则为空字符串"
}

只返回 JSON，不要其他内容。`

    try {
      const text = await callLLM(settings, prompt, { maxTokens: 512 })
      const result = extractJsonFromText<{
        hasConflict: boolean
        conflictSummary?: string
      }>(text, 'object')

      if (result?.hasConflict && result.conflictSummary) {
        conflicts.push({
          relationIds: rels.map(r => r.id),
          conflictSummary: result.conflictSummary,
        })

        // 标记这些关系为冲突状态
        for (const rel of rels) {
          rel.hasConflict = true
          rel.conflictWith = rels.filter(r => r.id !== rel.id).map(r => r.id)
        }
      }
    } catch (e) {
      console.error(`[RelationConflict] 检测 ${entityName} -> ${targetName} 冲突失败:`, e)
    }
  }

  return conflicts
}

/**
 * 批量检测关系冲突
 * 在关系提取完成后调用，检测所有实体的关系冲突
 */
export async function detectRelationConflicts(
  settings: AppSettings,
  relationsByEntity: Map<string, EntityRelation[]>,
  normalizedEntities: NormalizedEntity[],
  onProgress?: (status: string) => void
): Promise<Map<string, RelationConflictInfo[]>> {
  const allConflicts = new Map<string, RelationConflictInfo[]>()

  // 构建实体 ID 到名称的映射
  const entityIdToName = new Map<string, string>()
  for (const ne of normalizedEntities) {
    entityIdToName.set(ne.id, ne.canonicalName)
  }

  const entityIds = Array.from(relationsByEntity.keys())
  let checkedCount = 0

  for (const entityId of entityIds) {
    const relations = relationsByEntity.get(entityId)!
    const entityName = entityIdToName.get(entityId) || entityId

    // 只检查有多个关系的实体
    if (relations.length < 2) continue

    checkedCount++
    onProgress?.(`检测关系冲突 (${checkedCount}/${entityIds.length}): ${entityName}`)

    const conflicts = await detectConflictsForEntity(settings, entityName, relations)
    if (conflicts.length > 0) {
      allConflicts.set(entityId, conflicts)
    }
  }

  return allConflicts
}

/**
 * 完整的关系提取流程（含冲突检测）
 */
export async function extractRelationsWithConflictDetection(
  settings: AppSettings,
  docs: PrdDocument[],
  normalizedEntities: NormalizedEntity[],
  onProgress?: (status: string) => void
): Promise<{
  relationsByEntity: Map<string, EntityRelation[]>
  conflictsByEntity: Map<string, RelationConflictInfo[]>
}> {
  // 1. 提取关系
  const relationsByEntity = await extractRelationsFromDocuments(
    settings,
    docs,
    normalizedEntities,
    onProgress
  )

  // 2. 检测冲突
  const conflictsByEntity = await detectRelationConflicts(
    settings,
    relationsByEntity,
    normalizedEntities,
    onProgress
  )

  return { relationsByEntity, conflictsByEntity }
}

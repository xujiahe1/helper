/**
 * 语义召回服务
 * 实现智能知识召回机制：LLM 意图理解 + 关系链路扩展 + 多文档聚合
 */

import type { AppSettings } from '../types'
import type { PrdDocument, NormalizedEntity, EntityRelation } from '../stores/prdStore'
import { callLLM, extractJsonFromText } from './llm'

/** 语义意图提取结果 */
export interface SemanticIntent {
  /** 用户问题的主要意图 */
  intent: string
  /** 涉及的核心概念（可能是实体名称或通用概念） */
  concepts: string[]
  /** 需要回答的问题类型 */
  questionType: 'what' | 'how' | 'why' | 'impact' | 'comparison' | 'config' | 'other'
  /** 是否是影响面分析类问题 */
  isImpactAnalysis: boolean
}

/** 召回的实体信息 */
export interface RecalledEntity {
  entityId: string
  entityName: string
  /** 召回层级：0=直接匹配，1=一层关联，2=二层关联 */
  level: number
  /** 匹配得分 */
  score: number
  /** 匹配原因 */
  matchReason: string
  /** 关联路径（用于说明为何召回） */
  relationPath?: string[]
}

/** 召回的文档信息 */
export interface RecalledDocument {
  docId: string
  docTitle: string
  /** 相关度层级 */
  relevanceLevel: 'direct' | 'related' | 'indirect'
  /** 相关的实体 */
  relatedEntities: string[]
  /** 得分 */
  score: number
}

/**
 * 使用 LLM 提取用户问题的语义意图（内部使用）
 */
async function extractSemanticIntent(
  settings: AppSettings,
  userInput: string,
  entityNames: string[]
): Promise<SemanticIntent> {
  const entityList = entityNames.slice(0, 50).join('、') // 限制数量

  const prompt = `你是一个语义理解专家，请分析用户的问题意图。

## 已知实体列表（可能相关）
${entityList}

## 用户问题
${userInput}

## 分析任务
1. 理解用户问题的主要意图
2. 提取问题中涉及的核心概念（优先匹配已知实体，但也可以识别新概念）
3. 判断问题类型
4. 判断是否是影响面分析类问题（如"改了X会影响什么"、"X的影响范围"等）

## 输出格式
返回 JSON：
{
  "intent": "用户想了解...",
  "concepts": ["概念1", "概念2"],
  "questionType": "what|how|why|impact|comparison|config|other",
  "isImpactAnalysis": true|false
}

只返回 JSON，不要其他内容。`

  try {
    const response = await callLLM(settings, prompt)
    const result = extractJsonFromText(response) as Record<string, unknown> | null

    if (!result || typeof result !== 'object') {
      return getDefaultIntent(userInput)
    }

    return {
      intent: (result.intent as string) || userInput,
      concepts: Array.isArray(result.concepts) ? result.concepts as string[] : [],
      questionType: (result.questionType as SemanticIntent['questionType']) || 'other',
      isImpactAnalysis: result.isImpactAnalysis === true,
    }
  } catch (error) {
    console.error('[SemanticRecall] 意图提取失败:', error)
    return getDefaultIntent(userInput)
  }
}

/**
 * 默认意图（降级）
 */
function getDefaultIntent(userInput: string): SemanticIntent {
  const impactKeywords = ['影响', '波及', '牵扯', '改动', '改了']
  const isImpact = impactKeywords.some(kw => userInput.includes(kw))

  return {
    intent: userInput,
    concepts: [],
    questionType: isImpact ? 'impact' : 'other',
    isImpactAnalysis: isImpact,
  }
}

/**
 * 基于语义匹配召回实体（内部使用）
 * 支持别名匹配和概念语义匹配
 */
function matchEntitiesBySemantic(
  concepts: string[],
  normalizedEntities: NormalizedEntity[]
): RecalledEntity[] {
  const results: RecalledEntity[] = []
  const conceptsLower = concepts.map(c => c.toLowerCase())

  for (const entity of normalizedEntities) {
    const nameLower = entity.canonicalName.toLowerCase()
    const aliasesLower = entity.aliases.map(a => a.toLowerCase())
    const allNames = [nameLower, ...aliasesLower]

    // 精确匹配
    for (const concept of conceptsLower) {
      if (allNames.includes(concept)) {
        results.push({
          entityId: entity.id,
          entityName: entity.canonicalName,
          level: 0,
          score: 1.0,
          matchReason: '精确匹配',
        })
        break
      }
    }

    // 模糊匹配（包含关系）
    if (!results.find(r => r.entityId === entity.id)) {
      for (const concept of conceptsLower) {
        for (const name of allNames) {
          if (name.includes(concept) || concept.includes(name)) {
            results.push({
              entityId: entity.id,
              entityName: entity.canonicalName,
              level: 0,
              score: 0.7,
              matchReason: '模糊匹配',
            })
            break
          }
        }
        if (results.find(r => r.entityId === entity.id)) break
      }
    }
  }

  return results
}

/**
 * 沿关系链路扩展关联实体（内部使用，BFS）
 * @param startEntities 起始实体 ID 列表
 * @param normalizedEntities 所有实体
 * @param maxDepth 最大扩展深度
 * @returns 扩展后的实体列表（包含原始实体）
 */
function expandByRelations(
  startEntities: RecalledEntity[],
  normalizedEntities: NormalizedEntity[],
  maxDepth: number = 2
): RecalledEntity[] {
  const results = new Map<string, RecalledEntity>()

  // 初始化起始实体
  for (const entity of startEntities) {
    results.set(entity.entityId, entity)
  }

  // BFS 扩展
  let currentLevel = startEntities
  for (let depth = 1; depth <= maxDepth; depth++) {
    const nextLevel: RecalledEntity[] = []

    for (const current of currentLevel) {
      const entity = normalizedEntities.find(e => e.id === current.entityId)
      if (!entity || !entity.relations) continue

      for (const relation of entity.relations) {
        // 只处理 outgoing 方向的关系（避免重复）
        if (relation.direction === 'incoming') continue

        if (!results.has(relation.targetEntityId)) {
          const relatedEntity: RecalledEntity = {
            entityId: relation.targetEntityId,
            entityName: relation.targetEntityName,
            level: depth,
            score: Math.max(0.3, current.score - 0.2), // 每层降低得分
            matchReason: `通过「${relation.relationType}」关联`,
            relationPath: [
              ...(current.relationPath || [current.entityName]),
              `--${relation.relationType}-->`,
              relation.targetEntityName,
            ],
          }
          results.set(relation.targetEntityId, relatedEntity)
          nextLevel.push(relatedEntity)
        }
      }
    }

    currentLevel = nextLevel
  }

  return Array.from(results.values())
}

/**
 * 根据召回的实体计算文档相关度（内部使用）
 */
function rankDocuments(
  recalledEntities: RecalledEntity[],
  normalizedEntities: NormalizedEntity[],
  documents: PrdDocument[]
): RecalledDocument[] {
  const docScores = new Map<string, { score: number; entities: string[]; minLevel: number }>()

  for (const recalled of recalledEntities) {
    const entity = normalizedEntities.find(e => e.id === recalled.entityId)
    if (!entity) continue

    // 找到包含该实体的文档
    const docIds = new Set(entity.versions.map(v => v.docId))

    for (const docId of docIds) {
      const doc = documents.find(d => d.id === docId)
      if (!doc) continue

      const existing = docScores.get(doc.docId) || { score: 0, entities: [], minLevel: Infinity }
      existing.score += recalled.score
      if (!existing.entities.includes(recalled.entityName)) {
        existing.entities.push(recalled.entityName)
      }
      existing.minLevel = Math.min(existing.minLevel, recalled.level)
      docScores.set(doc.docId, existing)
    }
  }

  // 转换为结果数组
  const results: RecalledDocument[] = []
  for (const [docId, info] of docScores) {
    const doc = documents.find(d => d.docId === docId)
    if (!doc) continue

    results.push({
      docId: doc.docId,
      docTitle: doc.title,
      relevanceLevel: info.minLevel === 0 ? 'direct' : info.minLevel === 1 ? 'related' : 'indirect',
      relatedEntities: info.entities,
      score: info.score,
    })
  }

  // 按得分排序
  results.sort((a, b) => b.score - a.score)

  return results
}

/**
 * 动态决定召回数量（内部使用）
 * 根据问题类型和匹配情况决定返回多少文档
 */
function determineRecallCount(
  intent: SemanticIntent,
  totalMatches: number,
  maxTokenBudget: number = 30000 // 假设的 token 预算
): number {
  // 影响面分析需要更多文档
  if (intent.isImpactAnalysis) {
    return Math.min(totalMatches, 10)
  }

  // 配置类问题可能需要多个配置场景
  if (intent.questionType === 'config') {
    return Math.min(totalMatches, 6)
  }

  // 一般问题
  return Math.min(totalMatches, 5)
}

/**
 * 完整的语义召回流程
 */
export async function semanticRecall(
  settings: AppSettings,
  userInput: string,
  documents: PrdDocument[],
  normalizedEntities: NormalizedEntity[],
  options?: {
    maxDepth?: number
    skipIntentExtraction?: boolean
  }
): Promise<{
  intent: SemanticIntent
  recalledEntities: RecalledEntity[]
  recalledDocuments: RecalledDocument[]
}> {
  // 1. 提取语义意图
  const entityNames = normalizedEntities.map(e => e.canonicalName)
  const intent = options?.skipIntentExtraction
    ? getDefaultIntent(userInput)
    : await extractSemanticIntent(settings, userInput, entityNames)

  console.log('[SemanticRecall] 意图:', intent)

  // 2. 基于语义匹配实体
  let recalledEntities = matchEntitiesBySemantic(intent.concepts, normalizedEntities)

  // 如果 LLM 没提取到有效概念，降级到关键词匹配
  if (recalledEntities.length === 0) {
    const keywords = userInput
      .split(/[\s,，。！？、；：]+/)
      .filter(k => k.length >= 2)
    recalledEntities = matchEntitiesBySemantic(keywords, normalizedEntities)
  }

  console.log('[SemanticRecall] 直接匹配实体:', recalledEntities.map(e => e.entityName))

  // 3. 沿关系链路扩展
  if (recalledEntities.length > 0) {
    const maxDepth = intent.isImpactAnalysis ? 2 : 1
    recalledEntities = expandByRelations(
      recalledEntities,
      normalizedEntities,
      options?.maxDepth ?? maxDepth
    )
    console.log('[SemanticRecall] 扩展后实体:', recalledEntities.map(e => `${e.entityName}(L${e.level})`))
  }

  // 4. 计算文档相关度
  const recalledDocuments = rankDocuments(recalledEntities, normalizedEntities, documents)

  // 5. 动态决定召回数量
  const recallCount = determineRecallCount(intent, recalledDocuments.length)
  const finalDocuments = recalledDocuments.slice(0, recallCount)

  console.log('[SemanticRecall] 召回文档:', finalDocuments.map(d => d.docTitle))

  return {
    intent,
    recalledEntities,
    recalledDocuments: finalDocuments,
  }
}

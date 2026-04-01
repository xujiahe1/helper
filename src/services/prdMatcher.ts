import type { PrdDocument, NormalizedEntity, EntityAspect, EntityVersion, EntityRelation } from '../stores/prdStore'
import { ASPECT_LABELS } from '../stores/prdStore'
import type { SemanticIntent, RecalledEntity, RecalledDocument } from './semanticRecall'
import { semanticRecall } from './semanticRecall'
import type { AppSettings } from '../types'

export interface MatchResult {
  docId: string
  docTitle: string
  matchedEntities: string[]  // 匹配到的实体名称
  score: number
}

export interface EnhancedMatchResult extends MatchResult {
  // 归一化实体相关
  normalizedEntityId?: string
  canonicalName?: string
  aliases?: string[]
  // 版本/变更信息
  hasConflict?: boolean
  conflictSummary?: string
  versionCount?: number
  latestVersion?: {
    description: string
    docTitle: string
    docUpdateTime: number
  }
  // 新增：面向信息
  aspectSummary?: {
    definition?: string
    usageCount?: number
    configCount?: number
    hasAspectConflict?: boolean
    aspects?: EntityAspect[]
  }
}

/** 从用户输入提取关键词 */
export function extractKeywords(input: string): string[] {
  // 中文分词：按标点/空格分割 + 提取 2-6 字片段
  const stopWords = new Set(['的', '是', '在', '有', '和', '帮我', '请', '写', '一个', '我', '你', '他', '她', '它', '这', '那', '什么', '怎么', '为什么', '如何', '可以', '能', '要', '会', '让', '给', '把', '用', '到', '从', '对', '与', '或', '及', '等', '了', '着', '过', '呢', '吗', '吧', '啊', '哦', '嗯', '好', '很', '太', '更', '最', '也', '都', '就', '还', '又', '再', '才', '只', '不', '没', '无', '非', '别', '被', '所', '而', '且', '但', '却', '然', '因', '为', '以', '于', '在', '当', '则', '虽', '若', '如', '即', '便', '已', '将', '把', '被', '让', '给', '同', '向', '比', '按', '沿', '随', '经', '通过'])

  const parts = input.split(/[，。！？、；：\s,.!?;:\n]+/).filter(Boolean)
  const keywords: string[] = []

  for (const part of parts) {
    if (part.length >= 2 && part.length <= 8 && !stopWords.has(part)) {
      keywords.push(part)
    }
    // N-gram 提取：从较长的词汇中提取 2-6 字片段
    if (part.length > 4) {
      for (let len = 2; len <= Math.min(6, part.length); len++) {
        for (let i = 0; i <= part.length - len; i++) {
          const gram = part.slice(i, i + len)
          if (!stopWords.has(gram)) keywords.push(gram)
        }
      }
    }
  }
  return [...new Set(keywords)]
}

/**
 * 匹配用户输入与 PRD 文档（支持别名匹配 + 返回归一化实体信息）
 */
export function matchWithNormalizedEntities(
  userInput: string,
  prdDocuments: PrdDocument[],
  normalizedEntities: NormalizedEntity[],
  maxResults = 3
): EnhancedMatchResult[] {
  const keywords = extractKeywords(userInput)
  if (keywords.length === 0) return []

  // 构建别名到归一化实体的映射
  const aliasToNormalized = new Map<string, NormalizedEntity>()
  for (const ne of normalizedEntities) {
    for (const alias of ne.aliases) {
      aliasToNormalized.set(alias.toLowerCase(), ne)
    }
  }

  const results = new Map<string, EnhancedMatchResult>()
  const matchedNormalizedIds = new Set<string>()

  for (const doc of prdDocuments) {
    if (doc.status !== 'done') continue

    const matchedEntities: string[] = []
    let score = 0
    let matchedNormalized: NormalizedEntity | undefined
    const titleLower = doc.title.toLowerCase()

    // 匹配文档标题（增强版：多关键词累加，权重提升）
    const titleMatchedKws: string[] = []
    for (const kw of keywords) {
      if (kw.length >= 2 && titleLower.includes(kw.toLowerCase())) {
        titleMatchedKws.push(kw)
      }
    }
    if (titleMatchedKws.length > 0) {
      matchedEntities.push(`[标题] ${doc.title}`)
      // 基础分 0.8 + 每个额外匹配关键词 0.4，最高 2.0
      score += Math.min(2.0, 0.8 + (titleMatchedKws.length - 1) * 0.4)
    }

    // 匹配实体（支持别名）
    for (const entity of doc.entities) {
      for (const kw of keywords) {
        const kwLower = kw.toLowerCase()
        const entityNameLower = entity.name.toLowerCase()

        // 精确匹配
        if (entityNameLower === kwLower) {
          matchedEntities.push(entity.name)
          score += 1.0
          matchedNormalized = aliasToNormalized.get(entityNameLower)
          break
        }

        // 检查是否匹配别名
        const normalized = aliasToNormalized.get(kwLower)
        if (normalized && normalized.aliases.some(a => a.toLowerCase() === entityNameLower)) {
          matchedEntities.push(`${entity.name} (别名: ${normalized.canonicalName})`)
          score += 1.0
          matchedNormalized = normalized
          break
        }

        // 模糊匹配
        if (entityNameLower.includes(kwLower) || kwLower.includes(entityNameLower)) {
          matchedEntities.push(entity.name)
          score += 0.6
          matchedNormalized = aliasToNormalized.get(entityNameLower)
          break
        }
      }
    }

    if (matchedEntities.length > 0) {
      const result: EnhancedMatchResult = {
        docId: doc.docId,
        docTitle: doc.title,
        matchedEntities: [...new Set(matchedEntities)].slice(0, 5),
        score
      }

      // 附加归一化实体信息
      if (matchedNormalized) {
        result.normalizedEntityId = matchedNormalized.id
        result.canonicalName = matchedNormalized.canonicalName
        result.aliases = matchedNormalized.aliases
        result.hasConflict = matchedNormalized.hasConflict
        result.conflictSummary = matchedNormalized.conflictSummary
        result.versionCount = matchedNormalized.versions.length
        result.latestVersion = {
          description: matchedNormalized.currentDescription,
          docTitle: matchedNormalized.currentDocTitle,
          docUpdateTime: matchedNormalized.currentDocUpdateTime,
        }

        // 新增：计算面向摘要
        const aspects = new Set<EntityAspect>()
        let usageCount = 0
        let configCount = 0
        let definitionDesc: string | undefined

        for (const version of matchedNormalized.versions) {
          if (version.aspect) {
            aspects.add(version.aspect)
            if (version.aspect === 'usage') usageCount++
            if (version.aspect === 'config') configCount++
            if (version.aspect === 'definition' && !definitionDesc) {
              definitionDesc = version.description
            }
          }
        }

        result.aspectSummary = {
          definition: definitionDesc,
          usageCount: usageCount > 0 ? usageCount : undefined,
          configCount: configCount > 0 ? configCount : undefined,
          hasAspectConflict: matchedNormalized.aspectConflicts && matchedNormalized.aspectConflicts.length > 0,
          aspects: aspects.size > 0 ? Array.from(aspects) : undefined,
        }

        // 如果有冲突，提升权重
        if (matchedNormalized.hasConflict) {
          result.score += 0.3
        }

        matchedNormalizedIds.add(matchedNormalized.id)
      }

      // 使用 docId 作为 key，防止重复
      const existing = results.get(doc.docId)
      if (!existing || result.score > existing.score) {
        results.set(doc.docId, result)
      }
    }
  }

  // 过滤低置信度匹配：
  // - 只有标题模糊匹配（score <= 0.8）不算有效匹配
  // - 至少需要一个实体精确匹配(1.0)或多个模糊匹配累加(>0.8)
  const MIN_SCORE_THRESHOLD = 0.9

  return Array.from(results.values())
    .filter(r => r.score >= MIN_SCORE_THRESHOLD)
    .sort((a, b) => b.score - a.score)
    .slice(0, maxResults)
}

/**
 * 按面向分组版本的辅助函数（内部使用）
 */
function groupVersionsByAspect(versions: EntityVersion[]): Map<EntityAspect, EntityVersion[]> {
  const grouped = new Map<EntityAspect, EntityVersion[]>()

  for (const version of versions) {
    const aspect = version.aspect || 'other'
    if (!grouped.has(aspect)) {
      grouped.set(aspect, [])
    }
    grouped.get(aspect)!.push(version)
  }

  return grouped
}

/**
 * 构建增强的 PRD 上下文
 * 按面向聚合展示，而非简单列出最新定义
 * 关系信息直接从 normalizedEntities 内部的 relations 字段读取
 */
export function buildEnhancedContext(
  matches: EnhancedMatchResult[],
  normalizedEntities: NormalizedEntity[],
): string {
  if (matches.length === 0) return ''

  const parts: string[] = []

  // 1. 匹配摘要
  const matchSummary = matches
    .map(m => `- ${m.docTitle}: ${m.matchedEntities.join(', ')}`)
    .join('\n')
  parts.push(`## PRD 实体匹配\n${matchSummary}`)

  // 2. 收集涉及的归一化实体
  const involvedNormalized = new Map<string, NormalizedEntity>()
  for (const match of matches) {
    if (match.normalizedEntityId) {
      const ne = normalizedEntities.find(n => n.id === match.normalizedEntityId)
      if (ne) {
        involvedNormalized.set(ne.id, ne)
      }
    }
  }

  // 3. 按实体输出（按面向组织）
  if (involvedNormalized.size > 0) {
    for (const ne of involvedNormalized.values()) {
      const entityParts: string[] = []

      // 实体标题
      const aliasInfo = ne.aliases.length > 1
        ? `\n别名：${ne.aliases.join('、')}`
        : ''
      entityParts.push(`## 实体：${ne.canonicalName}${aliasInfo}`)

      // 按面向分组版本
      const versionsByAspect = groupVersionsByAspect(ne.versions)

      // 定义面向
      const definitions = versionsByAspect.get('definition')
      if (definitions && definitions.length > 0) {
        const latestDef = definitions[0]
        entityParts.push(`### ${ASPECT_LABELS.definition}\n${latestDef.description}\n来源：${latestDef.docTitle} (${new Date(latestDef.docUpdateTime).toLocaleDateString('zh-CN')})`)
      }

      // 使用场景面向
      const usages = versionsByAspect.get('usage')
      if (usages && usages.length > 0) {
        const usageItems = usages.map(v =>
          `- ${v.description.slice(0, 100)}${v.description.length > 100 ? '...' : ''} - ${v.docTitle}`
        ).join('\n')
        entityParts.push(`### ${ASPECT_LABELS.usage}（${usages.length} 篇文档提及）\n${usageItems}`)
      }

      // 技术实现面向
      const impls = versionsByAspect.get('implementation')
      if (impls && impls.length > 0) {
        const implItems = impls.map(v =>
          `- ${v.description.slice(0, 100)}${v.description.length > 100 ? '...' : ''} - ${v.docTitle}`
        ).join('\n')
        entityParts.push(`### ${ASPECT_LABELS.implementation}（${impls.length} 篇文档提及）\n${implItems}`)
      }

      // 配置项面向
      const configs = versionsByAspect.get('config')
      if (configs && configs.length > 0) {
        const hasVariation = configs.length > 1
        const configTitle = hasVariation
          ? `### ${ASPECT_LABELS.config} ⚠️ 不同场景有不同配置`
          : `### ${ASPECT_LABELS.config}`
        const configItems = configs.map(v =>
          `- ${v.description.slice(0, 150)}${v.description.length > 150 ? '...' : ''} (${v.docTitle})`
        ).join('\n')
        entityParts.push(`${configTitle}\n${configItems}`)
      }

      // 权限面向
      const permissions = versionsByAspect.get('permission')
      if (permissions && permissions.length > 0) {
        const permItems = permissions.map(v =>
          `- ${v.description.slice(0, 100)}${v.description.length > 100 ? '...' : ''} - ${v.docTitle}`
        ).join('\n')
        entityParts.push(`### ${ASPECT_LABELS.permission}\n${permItems}`)
      }

      // 集成对接面向
      const integrations = versionsByAspect.get('integration')
      if (integrations && integrations.length > 0) {
        const intItems = integrations.map(v =>
          `- ${v.description.slice(0, 100)}${v.description.length > 100 ? '...' : ''} - ${v.docTitle}`
        ).join('\n')
        entityParts.push(`### ${ASPECT_LABELS.integration}\n${intItems}`)
      }

      // 冲突状态
      if (ne.conflictResolution) {
        // 已解决的冲突，告诉大模型解决方案
        const resolution = ne.conflictResolution
        let resolutionNote = ''
        if (resolution.resolution === 'authoritative') {
          resolutionNote = '用户已指定上述为权威定义'
        } else if (resolution.resolution === 'merged') {
          resolutionNote = '用户已手动合并定义，上述为合并后的版本'
        } else if (resolution.resolution === 'resolved') {
          resolutionNote = '用户已确认知晓此冲突'
        } else if (resolution.resolution === 'split') {
          resolutionNote = '用户已将此实体拆分为多个独立概念'
        }
        if (resolution.note) {
          resolutionNote += `（用户备注：${resolution.note}）`
        }
        entityParts.push(`### ✅ 冲突已解决\n${resolutionNote}`)
      } else if (ne.aspectConflicts && ne.aspectConflicts.length > 0 && !ne.conflictResolution) {
        // 未解决的冲突
        const conflictItems = ne.aspectConflicts.map(c =>
          `- **${ASPECT_LABELS[c.aspect]}**: ${c.summary}`
        ).join('\n')
        entityParts.push(`### ⚠️ 注意：存在未解决的冲突\n${conflictItems}`)
      } else if (ne.hasConflict && ne.conflictSummary && !ne.conflictResolution) {
        entityParts.push(`### ⚠️ 注意：存在未解决的冲突\n${ne.conflictSummary}`)
      }

      // 相关关系（从实体的 relations 字段读取）
      if (ne.relations && ne.relations.length > 0) {
        const relationItems = ne.relations.map(r => {
          const source = r.sources[0]
            ? (r.sources[0].anchor
              ? `（来源：${r.sources[0].docTitle} §${r.sources[0].anchor}）`
              : `（来源：${r.sources[0].docTitle}）`)
            : ''
          const direction = r.direction === 'outgoing' ? '→' : r.direction === 'incoming' ? '←' : '↔'
          return `- ${ne.canonicalName} ${direction} ${r.relationType} ${direction} ${r.targetEntityName} ${source}`
        })
        entityParts.push(`### 相关关系\n${relationItems.join('\n')}`)
      }

      parts.push(entityParts.join('\n\n'))
    }
  }

  return parts.join('\n\n---\n\n')
}

/**
 * 语义增强版匹配：使用 LLM 理解意图 + 关系链路扩展
 * 返回结果不限数量，按相关度分层
 */
export async function semanticMatchWithNormalizedEntities(
  settings: AppSettings,
  userInput: string,
  prdDocuments: PrdDocument[],
  normalizedEntities: NormalizedEntity[],
  options?: {
    maxDepth?: number
    skipIntentExtraction?: boolean
  }
): Promise<{
  matches: EnhancedMatchResult[]
  intent: SemanticIntent
  recalledEntities: RecalledEntity[]
}> {
  // 如果没有归一化实体，使用基础匹配
  if (normalizedEntities.length === 0) {
    const basicMatches = matchWithNormalizedEntities(userInput, prdDocuments, [])
    return {
      matches: basicMatches,
      intent: {
        intent: userInput,
        concepts: [],
        questionType: 'other',
        isImpactAnalysis: false,
      },
      recalledEntities: [],
    }
  }

  // 使用语义召回服务
  const { intent, recalledEntities, recalledDocuments } = await semanticRecall(
    settings,
    userInput,
    prdDocuments,
    normalizedEntities,
    options
  )

  // 转换为 EnhancedMatchResult 格式
  const matches: EnhancedMatchResult[] = []

  for (const recalledDoc of recalledDocuments) {
    const doc = prdDocuments.find(d => d.docId === recalledDoc.docId)
    if (!doc) continue

    // 找到与该文档相关的归一化实体
    const relatedNormalized = normalizedEntities.find(ne =>
      recalledDoc.relatedEntities.includes(ne.canonicalName)
    )

    const result: EnhancedMatchResult = {
      docId: recalledDoc.docId,
      docTitle: recalledDoc.docTitle,
      matchedEntities: recalledDoc.relatedEntities,
      score: recalledDoc.score,
    }

    // 附加归一化实体信息
    if (relatedNormalized) {
      result.normalizedEntityId = relatedNormalized.id
      result.canonicalName = relatedNormalized.canonicalName
      result.aliases = relatedNormalized.aliases
      result.hasConflict = relatedNormalized.hasConflict
      result.conflictSummary = relatedNormalized.conflictSummary
      result.versionCount = relatedNormalized.versions.length
      result.latestVersion = {
        description: relatedNormalized.currentDescription,
        docTitle: relatedNormalized.currentDocTitle,
        docUpdateTime: relatedNormalized.currentDocUpdateTime,
      }

      // 计算面向摘要
      const aspects = new Set<EntityAspect>()
      let usageCount = 0
      let configCount = 0
      let definitionDesc: string | undefined

      for (const version of relatedNormalized.versions) {
        if (version.aspect) {
          aspects.add(version.aspect)
          if (version.aspect === 'usage') usageCount++
          if (version.aspect === 'config') configCount++
          if (version.aspect === 'definition' && !definitionDesc) {
            definitionDesc = version.description
          }
        }
      }

      result.aspectSummary = {
        definition: definitionDesc,
        usageCount: usageCount > 0 ? usageCount : undefined,
        configCount: configCount > 0 ? configCount : undefined,
        hasAspectConflict: relatedNormalized.aspectConflicts && relatedNormalized.aspectConflicts.length > 0,
        aspects: aspects.size > 0 ? Array.from(aspects) : undefined,
      }
    }

    matches.push(result)
  }

  return {
    matches,
    intent,
    recalledEntities,
  }
}

/**
 * 构建语义增强上下文
 * 包含意图理解结果和关系路径
 */
export function buildSemanticEnhancedContext(
  matches: EnhancedMatchResult[],
  normalizedEntities: NormalizedEntity[],
  intent: SemanticIntent,
  recalledEntities: RecalledEntity[]
): string {
  if (matches.length === 0) return ''

  const parts: string[] = []

  // 1. 意图理解摘要
  parts.push(`## 问题理解\n- 意图: ${intent.intent}\n- 问题类型: ${intent.questionType}${intent.isImpactAnalysis ? '\n- 这是一个影响面分析问题' : ''}`)

  // 2. 召回实体和路径
  const directEntities = recalledEntities.filter(e => e.level === 0)
  const relatedEntities = recalledEntities.filter(e => e.level > 0)

  if (directEntities.length > 0) {
    parts.push(`## 直接相关实体\n${directEntities.map(e => `- ${e.entityName}`).join('\n')}`)
  }

  if (relatedEntities.length > 0) {
    const pathDescriptions = relatedEntities
      .filter(e => e.relationPath)
      .map(e => `- ${e.relationPath!.join(' ')}`)
      .slice(0, 10) // 限制数量

    if (pathDescriptions.length > 0) {
      parts.push(`## 关联实体（通过关系扩展）\n${pathDescriptions.join('\n')}`)
    }
  }

  // 3. 匹配文档摘要
  const matchSummary = matches
    .map(m => `- ${m.docTitle}: ${m.matchedEntities.join(', ')}`)
    .join('\n')
  parts.push(`## 召回文档\n${matchSummary}`)

  // 4. 调用原有的上下文构建（按面向组织实体信息）
  const entityContext = buildEnhancedContext(matches, normalizedEntities)
  if (entityContext) {
    parts.push(entityContext)
  }

  return parts.join('\n\n---\n\n')
}


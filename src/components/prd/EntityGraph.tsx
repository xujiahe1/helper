import { memo, useMemo, useState, useCallback, useEffect, useRef } from 'react'
import { Search, List, Network as NetworkIcon, ChevronDown, ChevronUp, AlertTriangle, Tag, Filter, ZoomIn, ZoomOut, Maximize2, CheckCircle } from 'lucide-react'
import type { PrdDocument, NormalizedEntity, EntityRelation } from '../../stores/prdStore'

interface EntityGraphProps {
  documents: PrdDocument[]
  normalizedEntities: NormalizedEntity[]
  selectedEntityName: string | null
  selectedNormalizedId: string | null
  onEntityClick: (entityName: string) => void
  onNormalizedEntityClick: (normalizedId: string) => void
}

interface EntityNode {
  name: string
  count: number
  docIds: string[]
  // 归一化相关字段
  normalizedId?: string
  hasConflict?: boolean
  conflictResolved?: boolean  // 冲突已解决
  aliasCount?: number
  versionCount?: number
}

/** 连线数据：可能来自真实关系或共现推断 */
interface ConnectionData {
  from: string
  to: string
  fromId?: string
  toId?: string
  strength: number
  relation?: EntityRelation  // 使用 EntityRelation 替代 KnowledgeTriple
  relationType?: string      // 关系类型文本
}

// 实体数量阈值：超过此值切换到列表模式（全部实体时）
const GRAPH_MAX_ENTITIES = 30
// 图谱视图的实体上限（过滤后）
const GRAPH_FILTERED_MAX = 60

// 频率过滤选项
type FrequencyFilter = 'all' | 'top50' | 'top20'

// 布局基础参数
const BASE_WIDTH = 600
const BASE_HEIGHT = 400

/** 绘制带箭头的有向线段 */
function drawArrowLine(
  ctx: CanvasRenderingContext2D,
  from: { x: number; y: number },
  to: { x: number; y: number },
  color: string,
  lineWidth: number,
) {
  const nodeRadius = 30
  const headLength = 8
  const dx = to.x - from.x
  const dy = to.y - from.y
  const dist = Math.sqrt(dx * dx + dy * dy)
  if (dist < nodeRadius * 2) return // 太近不画

  const ux = dx / dist
  const uy = dy / dist
  // 缩短线段两端，留出节点半径
  const startX = from.x + ux * nodeRadius
  const startY = from.y + uy * nodeRadius
  const endX = to.x - ux * nodeRadius
  const endY = to.y - uy * nodeRadius

  ctx.beginPath()
  ctx.moveTo(startX, startY)
  ctx.lineTo(endX, endY)
  ctx.strokeStyle = color
  ctx.lineWidth = lineWidth
  ctx.stroke()

  // 三角形箭头
  const angle = Math.atan2(endY - startY, endX - startX)
  ctx.beginPath()
  ctx.moveTo(endX, endY)
  ctx.lineTo(endX - headLength * Math.cos(angle - Math.PI / 6), endY - headLength * Math.sin(angle - Math.PI / 6))
  ctx.lineTo(endX - headLength * Math.cos(angle + Math.PI / 6), endY - headLength * Math.sin(angle + Math.PI / 6))
  ctx.closePath()
  ctx.fillStyle = color
  ctx.fill()
}

export const EntityGraph = memo(function EntityGraph({
  documents,
  normalizedEntities,
  selectedEntityName,
  selectedNormalizedId,
  onEntityClick,
  onNormalizedEntityClick,
}: EntityGraphProps) {
  const [searchQuery, setSearchQuery] = useState('')
  const [viewMode, setViewMode] = useState<'graph' | 'list'>('graph')
  const [frequencyFilter, setFrequencyFilter] = useState<FrequencyFilter>('top50')
  const [hoveredEntity, setHoveredEntity] = useState<string | null>(null)
  const [expandedGroups, setExpandedGroups] = useState<Set<string>>(new Set(['high', 'medium']))
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const containerRef = useRef<HTMLDivElement>(null)
  const graphContainerRef = useRef<HTMLDivElement>(null)

  // 缩放和平移状态
  const [scale, setScale] = useState(1)
  const [offset, setOffset] = useState({ x: 0, y: 0 })
  const [isPanning, setIsPanning] = useState(false)
  const [panStart, setPanStart] = useState({ x: 0, y: 0 })

  // 节点拖拽状态
  const [draggingNode, setDraggingNode] = useState<string | null>(null)
  const [dragOffset, setDragOffset] = useState({ x: 0, y: 0 })

  // 节点位置（可被拖拽修改）
  const [nodePositions, setNodePositions] = useState<Map<string, { x: number; y: number }>>(new Map())

  // 优先使用归一化实体，否则从文档聚合
  const entityNodes = useMemo(() => {
    // 如果有归一化实体，优先使用
    if (normalizedEntities.length > 0) {
      return normalizedEntities.map(ne => ({
        name: ne.canonicalName,
        count: ne.versions.length,
        docIds: [...new Set(ne.versions.map(v => v.docId))],
        normalizedId: ne.id,
        // 只有未解决的冲突才显示冲突图标
        hasConflict: ne.hasConflict && !ne.conflictResolution,
        // 冲突已解决
        conflictResolved: !!ne.conflictResolution,
        aliasCount: ne.aliases.length,
        versionCount: ne.versions.length,
      })).sort((a, b) => b.count - a.count)
    }

    // 否则从文档聚合
    const entityMap = new Map<string, EntityNode>()

    for (const doc of documents) {
      for (const entity of doc.entities) {
        const existing = entityMap.get(entity.name)
        if (existing) {
          if (!existing.docIds.includes(doc.id)) {
            existing.count++
            existing.docIds.push(doc.id)
          }
        } else {
          entityMap.set(entity.name, {
            name: entity.name,
            count: 1,
            docIds: [doc.id],
          })
        }
      }
    }

    return Array.from(entityMap.values()).sort((a, b) => b.count - a.count)
  }, [documents, normalizedEntities])

  // 根据频率过滤实体（按分布比例）
  const frequencyFilteredNodes = useMemo(() => {
    if (entityNodes.length === 0) return []

    // 按 count 降序排列（已排好序）
    const total = entityNodes.length

    switch (frequencyFilter) {
      case 'top20':
        // 取前 20% 的高频实体
        return entityNodes.slice(0, Math.max(1, Math.ceil(total * 0.2)))
      case 'top50':
        // 取前 50% 的实体
        return entityNodes.slice(0, Math.max(1, Math.ceil(total * 0.5)))
      case 'all':
      default:
        return entityNodes
    }
  }, [entityNodes, frequencyFilter])

  // 自动切换视图模式：当过滤后的实体仍然过多时，切换到列表
  useEffect(() => {
    if (frequencyFilteredNodes.length > GRAPH_FILTERED_MAX) {
      setViewMode('list')
    } else if (entityNodes.length > GRAPH_MAX_ENTITIES && frequencyFilter === 'all') {
      setViewMode('list')
    }
  }, [entityNodes.length, frequencyFilteredNodes.length, frequencyFilter])

  // 判断是否可以使用图谱视图
  const canUseGraphView = frequencyFilteredNodes.length <= GRAPH_FILTERED_MAX

  // 过滤搜索
  const filteredNodes = useMemo(() => {
    if (!searchQuery.trim()) return frequencyFilteredNodes
    const q = searchQuery.toLowerCase()
    return frequencyFilteredNodes.filter(n => n.name.toLowerCase().includes(q))
  }, [frequencyFilteredNodes, searchQuery])

  // 分组：按分布比例分为高频(前20%) / 中频(20%-50%) / 低频(50%以后)
  const groupedNodes = useMemo(() => {
    const total = filteredNodes.length
    const top20Index = Math.ceil(total * 0.2)
    const top50Index = Math.ceil(total * 0.5)

    const high = filteredNodes.slice(0, top20Index)
    const medium = filteredNodes.slice(top20Index, top50Index)
    const low = filteredNodes.slice(top50Index)

    return { high, medium, low }
  }, [filteredNodes])

  // 计算实体之间的关联
  const connections = useMemo((): ConnectionData[] => {
    const nodeNames = new Set(filteredNodes.map(n => n.name))
    const result: ConnectionData[] = []

    // 构建 normalizedId → node 的映射
    const idToNode = new Map<string, EntityNode>()
    for (const node of filteredNodes) {
      if (node.normalizedId) {
        idToNode.set(node.normalizedId, node)
      }
    }

    // 从 normalizedEntities 的 relations 中提取连线
    for (const entity of normalizedEntities) {
      const fromNode = idToNode.get(entity.id)
      if (!fromNode || !nodeNames.has(fromNode.name)) continue

      for (const relation of entity.relations) {
        // 只处理 outgoing 和 bidirectional 方向的关系（避免重复）
        if (relation.direction === 'incoming') continue

        const toNode = idToNode.get(relation.targetEntityId)
        if (!toNode || !nodeNames.has(toNode.name)) continue
        if (fromNode.name === toNode.name) continue

        // 合并相同方向的连线
        const existing = result.find(c => c.from === fromNode.name && c.to === toNode.name)
        if (existing) {
          existing.strength++
        } else {
          result.push({
            from: fromNode.name,
            to: toNode.name,
            fromId: entity.id,
            toId: relation.targetEntityId,
            strength: 1,
            relation: relation,
            relationType: relation.relationType,
          })
        }
      }
    }

    // 如果没有关系数据，回退到共现逻辑
    if (result.length === 0) {
      for (const doc of documents) {
        const docEntities = doc.entities.filter(e => nodeNames.has(e.name))
        for (let i = 0; i < docEntities.length; i++) {
          for (let j = i + 1; j < docEntities.length; j++) {
            const existing = result.find(
              c => (c.from === docEntities[i].name && c.to === docEntities[j].name) ||
                   (c.from === docEntities[j].name && c.to === docEntities[i].name)
            )
            if (existing) {
              existing.strength++
            } else {
              result.push({
                from: docEntities[i].name,
                to: docEntities[j].name,
                strength: 1,
              })
            }
          }
        }
      }
    }

    return result
  }, [documents, filteredNodes, normalizedEntities])

  // 计算节点布局（根据缩放级别调整间距）
  useEffect(() => {
    if (filteredNodes.length === 0 || viewMode !== 'graph') return
    if (filteredNodes.length > GRAPH_FILTERED_MAX) return

    const positions = new Map<string, { x: number; y: number }>()

    // 放大时增加节点间距，让叠加的节点分散开
    const spreadFactor = scale // scale 越大，间距越大
    const width = BASE_WIDTH
    const height = BASE_HEIGHT
    const centerX = width / 2
    const centerY = height / 2

    const maxCount = Math.max(...filteredNodes.map(n => n.count))
    const nodeCount = filteredNodes.length

    // 基础半径，根据节点数量和缩放调整
    const baseRadius = Math.min(100, 250 / Math.sqrt(nodeCount)) * spreadFactor

    filteredNodes.forEach((node) => {
      const importance = node.count / maxCount
      const layer = importance > 0.5 ? 0 : importance > 0.25 ? 1 : 2
      const layerRadius = (baseRadius * 0.5 + layer * baseRadius * 0.6)

      const nodesInLayer = filteredNodes.filter(n => {
        const imp = n.count / maxCount
        return (layer === 0 && imp > 0.5) ||
               (layer === 1 && imp > 0.25 && imp <= 0.5) ||
               (layer === 2 && imp <= 0.25)
      })
      const indexInLayer = nodesInLayer.findIndex(n => n.name === node.name)
      const angleStep = (2 * Math.PI) / Math.max(nodesInLayer.length, 1)
      const angle = indexInLayer * angleStep - Math.PI / 2

      positions.set(node.name, {
        x: centerX + Math.cos(angle) * layerRadius,
        y: centerY + Math.sin(angle) * layerRadius,
      })
    })

    setNodePositions(positions)
  }, [filteredNodes, viewMode, scale])

  // 绘制连线
  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas || nodePositions.size === 0 || viewMode !== 'graph') return

    const ctx = canvas.getContext('2d')
    if (!ctx) return

    const rect = canvas.getBoundingClientRect()
    canvas.width = rect.width * 2
    canvas.height = rect.height * 2
    ctx.scale(2, 2)
    ctx.clearRect(0, 0, rect.width, rect.height)

    // 应用偏移
    ctx.save()
    ctx.translate(offset.x, offset.y)

    for (const conn of connections) {
      const from = nodePositions.get(conn.from)
      const to = nodePositions.get(conn.to)
      if (!from || !to) continue

      const isSelectedEdge = selectedEntityName === conn.from || selectedEntityName === conn.to ||
                           hoveredEntity === conn.from || hoveredEntity === conn.to

      if (conn.relation) {
        // 知识三元组连线
        const color = isSelectedEdge ? '#8b5cf6' : '#8b5cf633'
        const lineWidth = isSelectedEdge
          ? Math.min(conn.strength, 5) + 1.5
          : Math.min(conn.strength, 5)

        drawArrowLine(ctx, from, to, color, lineWidth)

        // 选中时显示关系类型标签
        if (isSelectedEdge) {
          const midX = (from.x + to.x) / 2
          const midY = (from.y + to.y) / 2
          const label = (conn.relationType || '').slice(0, 10)
          if (label) {
            ctx.font = '10px sans-serif'
            const textWidth = ctx.measureText(label).width
            ctx.fillStyle = 'rgba(255,255,255,0.9)'
            ctx.fillRect(midX - textWidth / 2 - 3, midY - 7, textWidth + 6, 14)
            ctx.fillStyle = '#8b5cf6'
            ctx.textAlign = 'center'
            ctx.textBaseline = 'middle'
            ctx.fillText(label, midX, midY)
          }
        }
      } else {
        // 共现逻辑连线
        ctx.beginPath()
        ctx.moveTo(from.x, from.y)
        ctx.lineTo(to.x, to.y)
        ctx.strokeStyle = isSelectedEdge ? 'rgba(99, 102, 241, 0.5)' : 'rgba(156, 163, 175, 0.2)'
        ctx.lineWidth = Math.min(conn.strength, 3)
        ctx.stroke()
      }
    }

    ctx.restore()
  }, [nodePositions, connections, selectedEntityName, hoveredEntity, viewMode, scale, offset, filteredNodes])

  // 获取有未解决冲突的实体
  const conflictEntities = useMemo(() => {
    return filteredNodes.filter(n => n.hasConflict)
  }, [filteredNodes])

  // 快速跳转到下一个冲突实体
  const [showConflictList, setShowConflictList] = useState(false)

  const toggleGroup = (group: string) => {
    setExpandedGroups(prev => {
      const next = new Set(prev)
      if (next.has(group)) {
        next.delete(group)
      } else {
        next.add(group)
      }
      return next
    })
  }

  // 缩放控制
  const handleZoomIn = () => setScale(s => Math.min(s * 1.3, 3))
  const handleZoomOut = () => setScale(s => Math.max(s / 1.3, 0.5))
  const handleResetZoom = () => {
    setScale(1)
    setOffset({ x: 0, y: 0 })
  }

  // 鼠标滚轮缩放
  const handleWheel = useCallback((e: WheelEvent) => {
    e.preventDefault()
    const delta = e.deltaY > 0 ? 0.9 : 1.1
    setScale(s => Math.min(Math.max(s * delta, 0.5), 3))
  }, [])

  // 使用原生事件监听器以支持 { passive: false }
  useEffect(() => {
    const container = graphContainerRef.current
    if (!container || viewMode !== 'graph') return

    container.addEventListener('wheel', handleWheel, { passive: false })
    return () => {
      container.removeEventListener('wheel', handleWheel)
    }
  }, [handleWheel, viewMode])

  // 画布平移（按住空格或在空白处拖拽）
  const handleCanvasMouseDown = useCallback((e: React.MouseEvent) => {
    // 只在空白区域启动平移
    if ((e.target as HTMLElement).closest('button')) return
    setIsPanning(true)
    setPanStart({ x: e.clientX - offset.x, y: e.clientY - offset.y })
  }, [offset])

  const handleCanvasMouseMove = useCallback((e: React.MouseEvent) => {
    if (draggingNode) {
      // 节点拖拽
      const container = graphContainerRef.current
      if (!container) return
      const rect = container.getBoundingClientRect()
      const x = (e.clientX - rect.left - offset.x) - dragOffset.x
      const y = (e.clientY - rect.top - offset.y) - dragOffset.y

      setNodePositions(prev => {
        const next = new Map(prev)
        next.set(draggingNode, { x, y })
        return next
      })
    } else if (isPanning) {
      // 画布平移
      setOffset({
        x: e.clientX - panStart.x,
        y: e.clientY - panStart.y,
      })
    }
  }, [draggingNode, isPanning, panStart, offset, dragOffset])

  const handleCanvasMouseUp = useCallback(() => {
    setIsPanning(false)
    setDraggingNode(null)
  }, [])

  // 节点拖拽开始
  const handleNodeDragStart = useCallback((e: React.MouseEvent, nodeName: string) => {
    e.stopPropagation()
    const container = graphContainerRef.current
    if (!container) return

    const rect = container.getBoundingClientRect()
    const pos = nodePositions.get(nodeName)
    if (!pos) return

    // 计算鼠标点击位置相对于节点中心的偏移
    const mouseX = e.clientX - rect.left - offset.x
    const mouseY = e.clientY - rect.top - offset.y
    setDragOffset({ x: mouseX - pos.x, y: mouseY - pos.y })
    setDraggingNode(nodeName)
  }, [nodePositions, offset])

  if (documents.length === 0) {
    return (
      <div className="h-full flex items-center justify-center text-gray-400 text-sm">
        添加 PRD 文档后，这里会展示实体关系图
      </div>
    )
  }

  if (entityNodes.length === 0) {
    return (
      <div className="h-full flex items-center justify-center text-gray-400 text-sm">
        文档解析中...
      </div>
    )
  }

  return (
    <div ref={containerRef} className="h-full flex flex-col">
      {/* 搜索栏 + 视图切换 */}
      <div className="flex-shrink-0 p-3 border-b border-gray-100">
        <div className="flex items-center gap-2">
          <div className="relative flex-1">
            <Search size={14} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-gray-400" />
            <input
              value={searchQuery}
              onChange={e => setSearchQuery(e.target.value)}
              placeholder="搜索实体..."
              className="w-full bg-gray-50 text-gray-700 placeholder-gray-400 text-sm rounded-lg pl-8 pr-3 py-2 outline-none focus:ring-2 focus:ring-indigo-100 focus:bg-white border border-transparent focus:border-indigo-200"
            />
          </div>

          {/* 频率过滤器 */}
          <div className="relative">
            <select
              value={frequencyFilter}
              onChange={e => setFrequencyFilter(e.target.value as FrequencyFilter)}
              className="appearance-none bg-gray-50 text-gray-600 text-xs rounded-lg pl-7 pr-6 py-2 outline-none focus:ring-2 focus:ring-indigo-100 border border-gray-200 cursor-pointer"
            >
              <option value="all">全部实体</option>
              <option value="top50">前 50%</option>
              <option value="top20">前 20%</option>
            </select>
            <Filter size={12} className="absolute left-2 top-1/2 -translate-y-1/2 text-gray-400 pointer-events-none" />
            <ChevronDown size={12} className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-400 pointer-events-none" />
          </div>

          <div className="flex items-center bg-gray-100 rounded-lg p-0.5">
            <button
              onClick={() => setViewMode('graph')}
              disabled={!canUseGraphView}
              className={`p-1.5 rounded-md transition-colors ${
                viewMode === 'graph'
                  ? 'bg-white text-indigo-600 shadow-sm'
                  : 'text-gray-400 hover:text-gray-600'
              } ${!canUseGraphView ? 'opacity-50 cursor-not-allowed' : ''}`}
              title={!canUseGraphView ? `实体过多(${frequencyFilteredNodes.length})，请使用列表视图或调整过滤条件` : '图谱视图'}
            >
              <NetworkIcon size={16} />
            </button>
            <button
              onClick={() => setViewMode('list')}
              className={`p-1.5 rounded-md transition-colors ${
                viewMode === 'list'
                  ? 'bg-white text-indigo-600 shadow-sm'
                  : 'text-gray-400 hover:text-gray-600'
              }`}
              title="列表视图"
            >
              <List size={16} />
            </button>
          </div>
        </div>
        <div className="mt-2 text-xs text-gray-400 flex items-center flex-wrap gap-x-2">
          <span>共 {filteredNodes.length} 个实体{entityNodes.length !== frequencyFilteredNodes.length && ` (全部 ${entityNodes.length})`} · {connections.length} 个关联</span>
          {normalizedEntities.length > 0 && (
            <span className="text-green-600">已归一化</span>
          )}
          {/* 冲突快捷导航 */}
          {conflictEntities.length > 0 && (
            <div className="relative">
              <button
                onClick={() => setShowConflictList(!showConflictList)}
                className="flex items-center gap-1 px-2 py-0.5 bg-amber-100 text-amber-700 rounded hover:bg-amber-200 transition-colors"
              >
                <AlertTriangle size={12} />
                {conflictEntities.length} 个冲突
                <ChevronDown size={12} className={`transition-transform ${showConflictList ? 'rotate-180' : ''}`} />
              </button>
              {showConflictList && (
                <div className="absolute top-full left-0 mt-1 z-50 bg-white border border-gray-200 rounded-lg shadow-lg p-1 min-w-[200px] max-h-[300px] overflow-y-auto">
                  <div className="text-[10px] text-gray-400 px-2 py-1 border-b border-gray-100 mb-1">
                    点击跳转到冲突实体
                  </div>
                  {conflictEntities.map(node => (
                    <button
                      key={node.normalizedId}
                      onClick={() => {
                        if (node.normalizedId) {
                          onNormalizedEntityClick(node.normalizedId)
                        } else {
                          onEntityClick(node.name)
                        }
                        setShowConflictList(false)
                      }}
                      className="w-full flex items-center gap-2 px-2 py-1.5 text-xs text-left rounded hover:bg-amber-50 text-gray-700"
                    >
                      <AlertTriangle size={12} className="text-amber-500 flex-shrink-0" />
                      <span className="truncate">{node.name}</span>
                      <span className="text-gray-400 ml-auto flex-shrink-0">({node.count})</span>
                    </button>
                  ))}
                </div>
              )}
            </div>
          )}
          {!canUseGraphView && viewMode === 'list' && (
            <span className="text-amber-500">实体较多，请调整过滤条件或使用列表视图</span>
          )}
        </div>
      </div>

      {/* 图谱视图 */}
      {viewMode === 'graph' && (
        <div
          ref={graphContainerRef}
          className="flex-1 relative overflow-hidden bg-gray-50/50"
          onMouseDown={handleCanvasMouseDown}
          onMouseMove={handleCanvasMouseMove}
          onMouseUp={handleCanvasMouseUp}
          onMouseLeave={handleCanvasMouseUp}
          style={{ cursor: draggingNode ? 'grabbing' : isPanning ? 'grabbing' : 'default' }}
        >
          {/* 缩放控制按钮 */}
          <div className="absolute top-3 right-3 z-30 flex flex-col gap-1 bg-white rounded-lg shadow-md border border-gray-200 p-1">
            <button
              onClick={handleZoomIn}
              className="p-1.5 text-gray-500 hover:text-indigo-600 hover:bg-indigo-50 rounded transition-colors"
              title="放大（节点会分散开）"
            >
              <ZoomIn size={16} />
            </button>
            <button
              onClick={handleZoomOut}
              className="p-1.5 text-gray-500 hover:text-indigo-600 hover:bg-indigo-50 rounded transition-colors"
              title="缩小"
            >
              <ZoomOut size={16} />
            </button>
            <div className="border-t border-gray-200 my-0.5" />
            <button
              onClick={handleResetZoom}
              className="p-1.5 text-gray-500 hover:text-indigo-600 hover:bg-indigo-50 rounded transition-colors"
              title="重置视图"
            >
              <Maximize2 size={16} />
            </button>
          </div>

          {/* 操作提示 */}
          <div className="absolute bottom-3 left-3 z-30 px-2 py-1 bg-white/80 rounded text-[10px] text-gray-400 border border-gray-200">
            滚轮缩放 · 拖拽节点 · 空白处平移
          </div>

          {/* 缩放比例指示 */}
          {scale !== 1 && (
            <div className="absolute bottom-3 right-3 z-30 px-2 py-1 bg-white/80 rounded text-xs text-gray-500 border border-gray-200">
              {Math.round(scale * 100)}%
            </div>
          )}

          {/* 可缩放平移的内容区域 */}
          <div
            className="absolute inset-0"
            style={{
              transform: `translate(${offset.x}px, ${offset.y}px)`,
              transition: isPanning || draggingNode ? 'none' : 'transform 0.1s ease-out',
            }}
          >
            <canvas
              ref={canvasRef}
              className="absolute inset-0 w-full h-full"
              style={{ pointerEvents: 'none' }}
            />

            {filteredNodes.map(node => {
              const pos = nodePositions.get(node.name)
              if (!pos) return null

              const isSelected = selectedEntityName === node.name || selectedNormalizedId === node.normalizedId
              const isHovered = hoveredEntity === node.name
              const isDragging = draggingNode === node.name
              const isConnected = selectedEntityName && connections.some(
                c => (c.from === selectedEntityName && c.to === node.name) ||
                     (c.to === selectedEntityName && c.from === node.name)
              )

              const handleClick = (e: React.MouseEvent) => {
                // 如果刚拖拽完，不触发点击
                if (draggingNode) return
                e.stopPropagation()
                if (node.normalizedId) {
                  onNormalizedEntityClick(node.normalizedId)
                } else {
                  onEntityClick(node.name)
                }
              }

              return (
                <button
                  key={node.name}
                  onClick={handleClick}
                  onMouseDown={(e) => handleNodeDragStart(e, node.name)}
                  onMouseEnter={() => !draggingNode && setHoveredEntity(node.name)}
                  onMouseLeave={() => setHoveredEntity(null)}
                  className={`
                    absolute transform -translate-x-1/2 -translate-y-1/2
                    px-2.5 py-1.5 rounded-lg text-xs font-medium
                    transition-all duration-150 whitespace-nowrap select-none
                    ${isDragging ? 'cursor-grabbing z-50 shadow-xl' : 'cursor-grab'}
                    ${node.hasConflict ? 'ring-2 ring-amber-400 ring-offset-1' : ''}
                    ${node.conflictResolved ? 'ring-2 ring-green-400 ring-offset-1' : ''}
                    ${isSelected
                      ? 'bg-indigo-500 text-white shadow-lg scale-110 z-20'
                      : isHovered
                        ? 'bg-indigo-100 text-indigo-700 shadow-md scale-105 z-10'
                        : isConnected
                          ? 'bg-indigo-50 text-indigo-600 border border-indigo-200'
                          : 'bg-white text-gray-700 border border-gray-200 hover:border-indigo-200'
                    }
                  `}
                  style={{
                    left: pos.x,
                    top: pos.y,
                  }}
                  title={`${node.count} 个版本${node.aliasCount && node.aliasCount > 1 ? ` · ${node.aliasCount} 个别名` : ''}${node.hasConflict ? ' · 存在冲突' : ''}${node.conflictResolved ? ' · 冲突已解决' : ''}\n拖拽可调整位置`}
                >
                  {node.hasConflict && <AlertTriangle size={10} className="inline mr-1 text-amber-500" />}
                  {node.conflictResolved && <CheckCircle size={10} className="inline mr-1 text-green-500" />}
                  {node.name}
                  {node.count > 1 && (
                    <span className={`ml-1 text-[10px] ${isSelected ? 'text-indigo-200' : 'text-gray-400'}`}>
                      ({node.count})
                    </span>
                  )}
                  {node.aliasCount && node.aliasCount > 1 && (
                    <Tag size={10} className={`inline ml-1 ${isSelected ? 'text-indigo-200' : 'text-gray-400'}`} />
                  )}
                </button>
              )
            })}
          </div>
        </div>
      )}

      {/* 列表视图 */}
      {viewMode === 'list' && (
        <div className="flex-1 overflow-y-auto">
          {/* 高频实体 */}
          {groupedNodes.high.length > 0 && (
            <div className="border-b border-gray-100">
              <button
                onClick={() => toggleGroup('high')}
                className="w-full flex items-center justify-between px-4 py-2 text-xs font-medium text-gray-500 hover:bg-gray-50"
              >
                <span>高频实体 ({groupedNodes.high.length})</span>
                {expandedGroups.has('high') ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
              </button>
              {expandedGroups.has('high') && (
                <div className="px-3 pb-3 flex flex-wrap gap-1.5">
                  {groupedNodes.high.map(node => (
                    <EntityTag
                      key={node.name}
                      node={node}
                      isSelected={selectedEntityName === node.name || selectedNormalizedId === node.normalizedId}
                      onClick={() => node.normalizedId ? onNormalizedEntityClick(node.normalizedId) : onEntityClick(node.name)}
                    />
                  ))}
                </div>
              )}
            </div>
          )}

          {/* 中频实体 */}
          {groupedNodes.medium.length > 0 && (
            <div className="border-b border-gray-100">
              <button
                onClick={() => toggleGroup('medium')}
                className="w-full flex items-center justify-between px-4 py-2 text-xs font-medium text-gray-500 hover:bg-gray-50"
              >
                <span>中频实体 ({groupedNodes.medium.length})</span>
                {expandedGroups.has('medium') ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
              </button>
              {expandedGroups.has('medium') && (
                <div className="px-3 pb-3 flex flex-wrap gap-1.5">
                  {groupedNodes.medium.map(node => (
                    <EntityTag
                      key={node.name}
                      node={node}
                      isSelected={selectedEntityName === node.name || selectedNormalizedId === node.normalizedId}
                      onClick={() => node.normalizedId ? onNormalizedEntityClick(node.normalizedId) : onEntityClick(node.name)}
                    />
                  ))}
                </div>
              )}
            </div>
          )}

          {/* 低频实体 */}
          {groupedNodes.low.length > 0 && (
            <div className="border-b border-gray-100">
              <button
                onClick={() => toggleGroup('low')}
                className="w-full flex items-center justify-between px-4 py-2 text-xs font-medium text-gray-500 hover:bg-gray-50"
              >
                <span>低频实体 ({groupedNodes.low.length})</span>
                {expandedGroups.has('low') ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
              </button>
              {expandedGroups.has('low') && (
                <div className="px-3 pb-3 flex flex-wrap gap-1.5">
                  {groupedNodes.low.map(node => (
                    <EntityTag
                      key={node.name}
                      node={node}
                      isSelected={selectedEntityName === node.name || selectedNormalizedId === node.normalizedId}
                      onClick={() => node.normalizedId ? onNormalizedEntityClick(node.normalizedId) : onEntityClick(node.name)}
                    />
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  )
})

// 实体标签组件
function EntityTag({ node, isSelected, onClick }: {
  node: EntityNode
  isSelected: boolean
  onClick: () => void
}) {
  return (
    <button
      onClick={onClick}
      title={`${node.count} 个版本${node.aliasCount && node.aliasCount > 1 ? ` · ${node.aliasCount} 个别名` : ''}${node.hasConflict ? ' · 存在未解决的冲突' : ''}${node.conflictResolved ? ' · 冲突已解决' : ''}`}
      className={`
        px-2.5 py-1.5 rounded-lg text-xs font-medium transition-all
        ${node.hasConflict ? 'ring-2 ring-amber-400 ring-offset-1' : ''}
        ${node.conflictResolved ? 'ring-2 ring-green-400 ring-offset-1' : ''}
        ${isSelected
          ? 'bg-indigo-500 text-white shadow-sm'
          : 'bg-white text-gray-700 border border-gray-200 hover:border-indigo-300 hover:text-indigo-600'
        }
      `}
    >
      {node.hasConflict && (
        <AlertTriangle
          size={12}
          className={`inline mr-1 ${isSelected ? 'text-amber-300' : 'text-amber-500'}`}
        />
      )}
      {node.conflictResolved && (
        <CheckCircle
          size={12}
          className={`inline mr-1 ${isSelected ? 'text-green-300' : 'text-green-500'}`}
        />
      )}
      {node.name}
      {node.count > 1 && (
        <span className={`ml-1 ${isSelected ? 'text-indigo-200' : 'text-gray-400'}`}>
          ({node.count})
        </span>
      )}
      {node.aliasCount && node.aliasCount > 1 && (
        <Tag size={10} className={`inline ml-1 ${isSelected ? 'text-indigo-200' : 'text-gray-400'}`} />
      )}
    </button>
  )
}

import { useState, useEffect, useRef, useCallback, Component, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { Download, Code2, Eye, Maximize2, X, ZoomIn, ZoomOut, RotateCcw } from 'lucide-react'

// ─── Mermaid 初始化（Promise 单例）────────────────────────────────────────────

let initPromise: Promise<void> | null = null
function initMermaid(): Promise<void> {
  if (!initPromise) {
    initPromise = import('mermaid').then(m => {
      m.default.initialize({
        startOnLoad: false,
        theme: 'default',
        securityLevel: 'loose',
        fontFamily: 'system-ui, -apple-system, sans-serif',
      })
    }).catch(err => {
      initPromise = null
      throw err
    })
  }
  return initPromise
}

// ─── Mermaid 代码修复（两轮策略）────────────────────────────────────────────

/** 第一轮：修复节点括号内含特殊字符的标签（中文 + | - ; 等） */
function fixNodeLabels(code: string): string {
  return code.split('\n').map(line => {
    const t = line.trim()
    if (!t || t.startsWith('%%') ||
      /^(style|classDef|class|click|subgraph|end|direction|linkStyle)\s/i.test(t))
      return line
    return line.replace(
      /(\[{1,2}\(?|\(\[?|\({1,2}|\{+)([^\])"'`][^)\]}"'`]*)(\)?\]{1,2}|\)\]?|\){1,2}|\}+)/g,
      (match, open, label, close) => {
        if (/^["'`]/.test(label.trim())) return match
        if (/[|;,\-<>{}()\[\]]/.test(label) || /[\u4e00-\u9fff]/.test(label))
          return open + '"' + label.replace(/"/g, '&quot;') + '"' + close
        return match
      },
    )
  }).join('\n')
}

/** 第二轮：更激进——引号化所有边标签（-->|label|）且包含中文或特殊字符的部分 */
function fixEdgeLabels(code: string): string {
  // -->|some label| 或 --|some label|> 等形式
  return code.replace(/(\-+>?|={2,}>?|\.+>?)\|([^|]+)\|/g, (match, arrow, label) => {
    if (/[\u4e00-\u9fff]/.test(label) || /[|;\-<>{}()\[\]]/.test(label))
      return arrow + '|"' + label.replace(/"/g, '&quot;') + '"|'
    return match
  })
}

/** 第三轮：把 style 关键字开头但不是指令的行中的 style 替换为 styl（规避保留字）
 *  适用于节点 ID 恰好叫 "style正式" 这种情况 */
function fixReservedWords(code: string): string {
  return code.split('\n').map(line => {
    const t = line.trim()
    // 如果是合法的 style 指令行则保留
    if (/^style\s+\w/.test(t)) return line
    // 把非指令位置出现的 style 字符串替换掉
    return line.replace(/\bstyle(?=\s*[^\s])/g, 'styl_')
  }).join('\n')
}

function sanitizeMermaid(code: string): string {
  let s = code.trim()
  s = s.replace(/^```\s*mermaid\s*\n?/i, '').replace(/\n?```\s*$/, '')
  s = s.replace(/^\uFEFF/, '').replace(/[\u200B\u200C\u200D]/g, '')
  s = s.replace(/\r\n/g, '\n').replace(/\r/g, '\n')
  return s
}

/** 第四轮：sequenceDiagram 里 participant 的显示名自动加引号（含中文/特殊字符时） */
function fixSequenceParticipants(code: string): string {
  const lines = code.split('\n')
  const firstNonEmpty = lines.find(l => l.trim())
  if (!firstNonEmpty || !/^sequenceDiagram\b/i.test(firstNonEmpty.trim())) return code

  return lines.map(line => {
    const m = line.match(/^(\s*participant\s+[A-Za-z0-9_]+\s+as\s+)(.+?)(\s*)$/)
    if (!m) return line
    const [, prefix, rawLabel, suffix] = m
    const label = rawLabel.trim()
    if (!label || /^["'`].*["'`]$/.test(label)) return line
    // 仅在包含中文或特殊符号时加引号，避免影响简单英文 label
    if (/[\u4e00-\u9fff]|[\/()<>:;]/.test(label)) {
      return prefix + '"' + label.replace(/"/g, '\\"') + '"' + suffix
    }
    return line
  }).join('\n')
}

let renderQueue: Promise<unknown> = Promise.resolve()
let renderCounter = 0

async function doRender(
  mermaid: { render(id: string, code: string): Promise<{ svg: string }> },
  id: string,
  code: string,
): Promise<string> {
  try {
    const { svg } = await mermaid.render(id, code)
    return svg
  } finally {
    try {
      document.getElementById(id)?.remove()
      document.getElementById('d' + id)?.remove()
    } catch {}
  }
}

async function tryRender(code: string): Promise<string> {
  await initMermaid()
  const mermaid = (await import('mermaid')).default
  const id = `mmd-${++renderCounter}-${Date.now()}`
  const ticket: Promise<string> = renderQueue.then(
    () => doRender(mermaid, id, code),
    () => doRender(mermaid, id, code),
  )
  renderQueue = ticket.then(() => {}, () => {})
  return ticket
}

// ─── Lightbox（滚轮缩放 + 拖拽平移）─────────────────────────────────────────

function MermaidLightbox({
  svgContent, onClose, onDownloadPng,
}: {
  svgContent: string; onClose: () => void; onDownloadPng: () => void
}) {
  const [zoom, setZoom] = useState(1)
  const offsetRef = useRef({ x: 0, y: 0 })
  const [offsetState, setOffsetState] = useState({ x: 0, y: 0 })
  const [cursor, setCursor] = useState<'grab' | 'grabbing'>('grab')

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])

  const handleWheel = useCallback((e: React.WheelEvent) => {
    e.preventDefault()
    setZoom(z => Math.min(8, Math.max(0.2, z * (e.deltaY < 0 ? 1.15 : 0.87))))
  }, [])

  // ✅ 关键：在 mousedown 的同步闭包里直接绑 document 事件，零延迟
  const handleMouseDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault()
    setCursor('grabbing')

    let startX = e.clientX - offsetRef.current.x
    let startY = e.clientY - offsetRef.current.y

    const onMove = (ev: MouseEvent) => {
      const nx = ev.clientX - startX
      const ny = ev.clientY - startY
      offsetRef.current = { x: nx, y: ny }
      setOffsetState({ x: nx, y: ny })
    }
    const onUp = () => {
      setCursor('grab')
      document.removeEventListener('mousemove', onMove)
      document.removeEventListener('mouseup', onUp)
    }
    document.addEventListener('mousemove', onMove)
    document.addEventListener('mouseup', onUp)
  }, [])

  const resetView = () => {
    offsetRef.current = { x: 0, y: 0 }
    setOffsetState({ x: 0, y: 0 })
    setZoom(1)
  }

  const btn: React.CSSProperties = {
    display: 'flex', alignItems: 'center', gap: 4,
    padding: '6px 10px', borderRadius: 8, border: 'none', cursor: 'pointer',
    background: 'rgba(255,255,255,0.15)', color: '#fff', fontSize: 12,
  }

  return createPortal(
    <div style={{ position: 'fixed', inset: 0, zIndex: 9999, display: 'flex', flexDirection: 'column', background: 'rgba(0,0,0,0.88)', backdropFilter: 'blur(6px)' }}>
      {/* 顶栏 */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '10px 16px', flexShrink: 0 }}>
        <div style={{ display: 'flex', gap: 8 }}>
          <button style={btn} onClick={() => setZoom(z => Math.min(8, z * 1.25))}><ZoomIn size={14} />放大</button>
          <button style={btn} onClick={() => setZoom(z => Math.max(0.2, z * 0.8))}><ZoomOut size={14} />缩小</button>
          <button style={btn} onClick={resetView}><RotateCcw size={14} />重置</button>
          <span style={{ color: 'rgba(255,255,255,0.45)', fontSize: 12, alignSelf: 'center' }}>{Math.round(zoom * 100)}%</span>
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          <button style={btn} onClick={onDownloadPng}><Download size={14} />PNG</button>
          <button style={{ ...btn, background: 'rgba(255,255,255,0.08)' }} onClick={onClose}><X size={16} /></button>
        </div>
      </div>

      {/* 画布 */}
      <div
        style={{ flex: 1, overflow: 'hidden', cursor, userSelect: 'none' }}
        onWheel={handleWheel}
        onMouseDown={handleMouseDown}
      >
        <div style={{ width: '100%', height: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
          <div
            style={{
              transform: `translate(${offsetState.x}px, ${offsetState.y}px) scale(${zoom})`,
              transformOrigin: 'center',
              background: '#fff', borderRadius: 12, padding: 32,
              boxShadow: '0 25px 60px rgba(0,0,0,0.6)',
              // 拖拽时阻止 SVG 内部元素抢事件
              pointerEvents: cursor === 'grabbing' ? 'none' : 'auto',
            }}
            dangerouslySetInnerHTML={{ __html: svgContent }}
          />
        </div>
      </div>

      <div style={{ textAlign: 'center', padding: 8, color: 'rgba(255,255,255,0.3)', fontSize: 11 }}>
        滚轮缩放 · 拖拽平移 · ESC 关闭
      </div>
    </div>,
    document.body,
  )
}

// ─── 防崩溃边界 ─────────────────────────────────────────────────────────────

interface MEBProps { children: ReactNode; code: string }

class MermaidErrorBoundary extends Component<MEBProps, { hasError: boolean }> {
  state = { hasError: false }
  static getDerivedStateFromError() { return { hasError: true } }
  componentDidCatch() {}
  componentDidUpdate(prevProps: MEBProps) {
    if (prevProps.code !== this.props.code && this.state.hasError) {
      this.setState({ hasError: false })
    }
  }
  render() {
    if (this.state.hasError) {
      return (
        <div className="my-3 rounded-lg overflow-hidden border border-gray-200">
          <div className="bg-slate-800 text-slate-300 px-4 py-2 text-xs"><span>mermaid</span></div>
          <div className="p-4 bg-red-50 text-red-600 text-sm">
            <p className="font-medium mb-1">Mermaid 渲染出错</p>
            <pre className="bg-slate-900 text-slate-100 p-3 rounded text-xs overflow-x-auto mt-2"><code>{this.props.code}</code></pre>
          </div>
        </div>
      )
    }
    return this.props.children
  }
}

// ─── 主组件 ──────────────────────────────────────────────────────────────────

function MermaidBlockInner({ code }: { code: string }) {
  const containerRef = useRef<HTMLDivElement>(null)
  // svgContent 只增不清——渲染失败时保留上一次成功的图，不闪烁错误
  const [svgContent, setSvgContent] = useState('')
  const [error, setError] = useState('')
  const [showSource, setShowSource] = useState(false)
  const [lightboxOpen, setLightboxOpen] = useState(false)

  const sanitizedCode = sanitizeMermaid(code)

  useEffect(() => {
    let cancelled = false

    async function run() {
      try {
        const firstNonEmpty = sanitizedCode.split('\n').find(l => l.trim())
        const isSequenceDiagram = !!firstNonEmpty && /^sequenceDiagram\b/i.test(firstNonEmpty.trim())

        const variants = isSequenceDiagram
          ? [
              sanitizedCode,
              fixSequenceParticipants(sanitizedCode),
            ]
          : [
              sanitizedCode,
              fixNodeLabels(sanitizedCode),
              fixEdgeLabels(fixNodeLabels(sanitizedCode)),
              fixReservedWords(fixEdgeLabels(fixNodeLabels(sanitizedCode))),
            ]

        for (let i = 0; i < variants.length; i++) {
          if (cancelled) return
          try {
            const svg = await tryRender(variants[i])
            if (!cancelled) { setSvgContent(svg); setError('') }
            return
          } catch {
            // 静默，继续下一种修复
          }
        }

        if (!cancelled) {
          setError('渲染失败，代码含有 Mermaid 不支持的语法，可点「源码」查看或到 mermaid.live 调试')
        }
      } catch {
        if (!cancelled) {
          setError('渲染失败，代码含有 Mermaid 不支持的语法，可点「源码」查看或到 mermaid.live 调试')
        }
      }
    }

    // 1000ms 防抖：等流式输出停止后再渲染
    const t = setTimeout(run, 1000)
    return () => { cancelled = true; clearTimeout(t) }
  }, [sanitizedCode])

  const handleDownloadPng = useCallback(async () => {
    if (!svgContent) return

    // 从 svgContent 字符串解析 SVG
    const parser = new DOMParser()
    const doc = parser.parseFromString(svgContent, 'image/svg+xml')
    const svgEl = doc.querySelector('svg')
    if (!svgEl) return

    // 确保有 xmlns 属性
    svgEl.setAttribute('xmlns', 'http://www.w3.org/2000/svg')
    svgEl.setAttribute('xmlns:xlink', 'http://www.w3.org/1999/xlink')

    // 获取原始尺寸，优先从 viewBox 或 width/height 属性
    let w = 1200, h = 800
    const viewBox = svgEl.getAttribute('viewBox')
    if (viewBox) {
      const parts = viewBox.split(/\s+/)
      if (parts.length >= 4) {
        w = Math.round(parseFloat(parts[2]) * 2) || 1200
        h = Math.round(parseFloat(parts[3]) * 2) || 800
      }
    } else {
      const widthAttr = svgEl.getAttribute('width')
      const heightAttr = svgEl.getAttribute('height')
      if (widthAttr) w = Math.round(parseFloat(widthAttr) * 2) || 1200
      if (heightAttr) h = Math.round(parseFloat(heightAttr) * 2) || 800
    }

    svgEl.setAttribute('width', String(w))
    svgEl.setAttribute('height', String(h))

    // 内联所有外部样式，避免 canvas tainted
    const styleEl = doc.createElementNS('http://www.w3.org/2000/svg', 'style')
    styleEl.textContent = `
      * { font-family: system-ui, -apple-system, sans-serif; }
      text { fill: currentColor; }
    `
    svgEl.insertBefore(styleEl, svgEl.firstChild)

    const svgStr = new XMLSerializer().serializeToString(svgEl)
    // 使用 Data URL 而不是 Blob URL，避免跨域 tainted canvas 问题
    const dataUrl = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svgStr)

    try {
      const img = await new Promise<HTMLImageElement>((res, rej) => {
        const i = new Image()
        i.onload = () => res(i)
        i.onerror = rej
        i.src = dataUrl
      })
      const canvas = document.createElement('canvas')
      canvas.width = w; canvas.height = h
      const ctx = canvas.getContext('2d')!
      ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, w, h)
      ctx.drawImage(img, 0, 0, w, h)
      const a = document.createElement('a')
      a.href = canvas.toDataURL('image/png'); a.download = 'diagram.png'; a.click()
    } catch (err) {
      console.error('PNG 导出失败:', err)
      // 降级：直接下载 SVG
      const svgBlob = new Blob([svgStr], { type: 'image/svg+xml;charset=utf-8' })
      const svgUrl = URL.createObjectURL(svgBlob)
      const a = document.createElement('a')
      a.href = svgUrl; a.download = 'diagram.svg'; a.click()
      URL.revokeObjectURL(svgUrl)
    }
  }, [svgContent])

  return (
    <>
      {lightboxOpen && svgContent && (
        <MermaidLightbox
          svgContent={svgContent}
          onClose={() => setLightboxOpen(false)}
          onDownloadPng={handleDownloadPng}
        />
      )}
      <div className="my-3 rounded-lg overflow-hidden border border-gray-200">
        <div className="flex items-center justify-between bg-slate-800 text-slate-300 px-4 py-2 text-xs">
          <span>mermaid</span>
          <div className="flex items-center gap-2">
            {svgContent && (
              <button onClick={() => setLightboxOpen(true)} className="hover:text-white transition-colors flex items-center gap-1">
                <Maximize2 size={12} />放大
              </button>
            )}
            <button onClick={() => setShowSource(s => !s)} className="hover:text-white transition-colors flex items-center gap-1">
              {showSource ? <Eye size={12} /> : <Code2 size={12} />}
              {showSource ? '预览' : '源码'}
            </button>
            {svgContent && (
              <button onClick={handleDownloadPng} className="hover:text-white transition-colors flex items-center gap-1">
                <Download size={12} />PNG
              </button>
            )}
          </div>
        </div>

        {showSource ? (
          <pre className="bg-slate-900 text-slate-100 p-4 overflow-x-auto text-[13px] leading-relaxed">
            <code>{sanitizedCode}</code>
          </pre>
        ) : svgContent ? (
          // 有成功 SVG 就展示，即使同时有 error 也不覆盖
          <div
            ref={containerRef}
            className="p-4 bg-white flex justify-center overflow-x-auto cursor-zoom-in hover:bg-gray-50/50 transition-colors"
            onClick={() => setLightboxOpen(true)}
            title="点击放大"
            dangerouslySetInnerHTML={{ __html: svgContent }}
          />
        ) : error ? (
          <div className="p-4 bg-red-50 text-red-600 text-sm">
            <p className="font-medium mb-1">Mermaid 渲染失败</p>
            <p className="text-xs text-gray-500 mb-2">
              {error}，或到{' '}
              <a href="https://mermaid.live" target="_blank" rel="noopener noreferrer" className="underline">mermaid.live</a>{' '}
              调试
            </p>
            <pre className="bg-slate-900 text-slate-100 p-3 rounded text-xs overflow-x-auto">
              <code>{sanitizedCode}</code>
            </pre>
          </div>
        ) : (
          <div className="p-4 bg-white flex items-center justify-center text-gray-400 text-sm">
            正在渲染…
          </div>
        )}
      </div>
    </>
  )
}

export function MermaidBlock({ code }: { code: string }) {
  return (
    <MermaidErrorBoundary code={sanitizeMermaid(code)}>
      <MermaidBlockInner code={code} />
    </MermaidErrorBoundary>
  )
}

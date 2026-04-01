import { useState, useEffect } from 'react'
import { X, AlertCircle, CheckCircle2, Info, AlertTriangle } from 'lucide-react'

type ToastType = 'error' | 'success' | 'info' | 'warning'

interface Toast {
  id: number
  message: string
  type: ToastType
}

const listeners = new Set<(toast: Toast) => void>()
let nextId = 0

export function showToast(message: string, type: ToastType = 'error') {
  const toast: Toast = { id: nextId++, message, type }
  listeners.forEach(fn => fn(toast))
}

const ICON_MAP = { error: AlertCircle, success: CheckCircle2, info: Info, warning: AlertTriangle }
const STYLE_MAP = {
  error: 'bg-red-50 border-red-200 text-red-800',
  success: 'bg-green-50 border-green-200 text-green-800',
  info: 'bg-blue-50 border-blue-200 text-blue-800',
  warning: 'bg-amber-50 border-amber-200 text-amber-800',
}

export function ToastContainer() {
  const [toasts, setToasts] = useState<Toast[]>([])

  useEffect(() => {
    const handler = (toast: Toast) => {
      setToasts(prev => [...prev, toast])
      setTimeout(() => {
        setToasts(prev => prev.filter(t => t.id !== toast.id))
      }, 4000)
    }
    listeners.add(handler)
    return () => { listeners.delete(handler) }
  }, [])

  if (toasts.length === 0) return null

  return (
    <div className="fixed top-4 right-4 z-[100] flex flex-col gap-2 max-w-sm">
      {toasts.map(toast => {
        const Icon = ICON_MAP[toast.type]
        return (
          <div
            key={toast.id}
            className={`flex items-start gap-2 px-4 py-3 rounded-lg border shadow-lg text-sm toast-slide-in ${STYLE_MAP[toast.type]}`}
          >
            <Icon size={16} className="flex-shrink-0 mt-0.5" />
            <span className="flex-1">{toast.message}</span>
            <button
              onClick={() => setToasts(prev => prev.filter(t => t.id !== toast.id))}
              className="flex-shrink-0 p-0.5 rounded hover:bg-black/5 transition-colors"
            >
              <X size={14} />
            </button>
          </div>
        )
      })}
    </div>
  )
}

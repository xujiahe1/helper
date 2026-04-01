import { useEffect, type ReactNode } from 'react'

interface Props {
  open: boolean
  onClose: () => void
  className?: string
  direction?: 'up' | 'down'
  children: ReactNode
}

export function Dropdown({ open, onClose, className, direction = 'down', children }: Props) {
  useEffect(() => {
    if (!open) return
    const handler = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', handler)
    return () => document.removeEventListener('keydown', handler)
  }, [open, onClose])

  if (!open) return null
  return (
    <>
      <div className="fixed inset-0 z-10" onClick={onClose} />
      <div
        className={`absolute ${
          direction === 'down' ? 'top-full mt-1' : 'bottom-full mb-1'
        } left-0 bg-white rounded-lg shadow-lg border border-gray-200 py-1 z-20 ${className || ''}`}
      >
        {children}
      </div>
    </>
  )
}

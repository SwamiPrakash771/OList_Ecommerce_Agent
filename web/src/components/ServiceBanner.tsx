// "Temporarily unavailable" banner. Shown when the server answers 503 (database down or busy)
// or can't be reached at all; hidden again as soon as a request succeeds.

import { AlertTriangle, RotateCcw } from 'lucide-react'
import { useEffect, useState } from 'react'

export const SERVICE_DOWN = 'olist:service-unavailable'
export const SERVICE_OK = 'olist:service-ok'

export default function ServiceBanner({ className = '' }: { className?: string }) {
  const [message, setMessage] = useState<string | null>(null)
  useEffect(() => {
    const down = (e: Event) => setMessage((e as CustomEvent<string>).detail)
    const ok = () => setMessage(null)
    window.addEventListener(SERVICE_DOWN, down)
    window.addEventListener(SERVICE_OK, ok)
    return () => {
      window.removeEventListener(SERVICE_DOWN, down)
      window.removeEventListener(SERVICE_OK, ok)
    }
  }, [])
  if (!message) return null
  return (
    <div role="alert" className={`flex items-center gap-3 border-b border-amber-200 bg-amber-50 px-4 py-2.5 text-sm text-amber-900 ${className}`}>
      <AlertTriangle size={16} className="shrink-0" />
      <span className="flex-1">{message}</span>
      <button onClick={() => window.location.reload()} className="flex items-center gap-1 rounded-lg border border-amber-300 bg-white px-3 py-1 text-xs font-semibold">
        <RotateCcw size={12} /> Retry
      </button>
    </div>
  )
}

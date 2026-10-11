// Shared chat state for the AI assistant page and the floating chat bubble.
// Replies stream from POST /api/chat as Server-Sent Events (see api/main.py).

import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from 'react'
import { useStore } from './state'

export type Step = { id: string; label: string; status: 'running' | 'done' | 'error' }
export type ChatMessage = {
  id: string
  role: 'user' | 'assistant'
  content: string
  steps?: Step[]
  error?: string
}

type ChatState = {
  messages: ChatMessage[]
  busy: boolean
  send: (text: string) => Promise<void>
  stop: () => void
  reset: () => void
  widgetOpen: boolean
  setWidgetOpen: (open: boolean) => void
}

const ChatContext = createContext<ChatState | null>(null)
const storageKey = (who: string) => `olist:chat:${who}`
const newId = () => Math.random().toString(36).slice(2)

function load(who: string): ChatMessage[] {
  try {
    return JSON.parse(sessionStorage.getItem(storageKey(who)) ?? '[]')
  } catch {
    return []
  }
}

export function ChatProvider({ children }: { children: ReactNode }) {
  const { user, refreshCart } = useStore()
  const who = user?.username ?? 'guest'
  const [messages, setMessages] = useState<ChatMessage[]>(() => load(who))
  const [busy, setBusy] = useState(false)
  const [widgetOpen, setWidgetOpen] = useState(false)
  const abortRef = useRef<AbortController | null>(null)

  // Each account (and guests) keeps its own conversation for this browser session.
  useEffect(() => {
    abortRef.current?.abort()
    setMessages(load(who))
    setBusy(false)
  }, [who])

  useEffect(() => {
    try {
      sessionStorage.setItem(storageKey(who), JSON.stringify(messages.slice(-50)))
    } catch {
      /* storage unavailable: history lasts until reload */
    }
  }, [messages, who])

  const patchLast = (fn: (m: ChatMessage) => ChatMessage) =>
    setMessages((all) => (all.length ? [...all.slice(0, -1), fn(all[all.length - 1])] : all))

  const send = useCallback(
    async (text: string) => {
      const content = text.trim()
      if (!content || busy) return
      const history = [...messages, { id: newId(), role: 'user' as const, content }]
      setMessages([...history, { id: newId(), role: 'assistant', content: '', steps: [] }])
      setBusy(true)
      const controller = new AbortController()
      abortRef.current = controller

      try {
        const endpoint = user?.role === 'admin' ? '/api/admin/chat' : user?.role === 'seller' ? '/api/seller/chat' : '/api/chat'
        const res = await fetch(endpoint, {
          method: 'POST',
          credentials: 'include',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            messages: history.filter((m) => m.content).slice(-20).map(({ role, content }) => ({ role, content })),
          }),
          signal: controller.signal,
        })
        if (!res.ok || !res.body) {
          const detail = await res.json().catch(() => ({}))
          throw new Error(typeof detail.detail === 'string' ? detail.detail : `Request failed (${res.status})`)
        }

        const reader = res.body.getReader()
        const decoder = new TextDecoder()
        let buffer = ''
        for (;;) {
          const { value, done } = await reader.read()
          if (done) break
          buffer += decoder.decode(value, { stream: true })
          let cut
          while ((cut = buffer.indexOf('\n\n')) >= 0) {
            const raw = buffer.slice(0, cut).replace(/^data: /, '')
            buffer = buffer.slice(cut + 2)
            if (!raw.trim()) continue
            const ev = JSON.parse(raw)
            if (ev.type === 'text') patchLast((m) => ({ ...m, content: m.content + ev.delta }))
            else if (ev.type === 'step')
              patchLast((m) => {
                const steps = m.steps ?? []
                const i = steps.findIndex((s) => s.id === ev.id)
                const step = { id: ev.id, label: ev.label, status: ev.status }
                return { ...m, steps: i >= 0 ? steps.map((s, j) => (j === i ? step : s)) : [...steps, step] }
              })
            else if (ev.type === 'cart_changed') refreshCart()
            else if (ev.type === 'error') patchLast((m) => ({ ...m, error: ev.message }))
          }
        }
      } catch (e) {
        if ((e as Error).name !== 'AbortError') patchLast((m) => ({ ...m, error: (e as Error).message || 'Something went wrong' }))
      } finally {
        setBusy(false)
        abortRef.current = null
      }
    },
    [busy, messages, refreshCart, user?.role],
  )

  const stop = () => abortRef.current?.abort()
  const reset = () => {
    stop()
    setMessages([])
  }

  return (
    <ChatContext.Provider value={{ messages, busy, send, stop, reset, widgetOpen, setWidgetOpen }}>
      {children}
    </ChatContext.Provider>
  )
}

export function useChat() {
  const ctx = useContext(ChatContext)
  if (!ctx) throw new Error('useChat must be used inside ChatProvider')
  return ctx
}

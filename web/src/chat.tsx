// Shared chat state for the AI assistant page and the floating chat bubble.
// Replies stream from POST /api/chat as Server-Sent Events (see api/main.py).

import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from 'react'
import { useStore } from './state'

export type Step = { id: string; label: string; status: 'running' | 'done' | 'error' }
export type ConfirmCard = {
  actionId: string
  tool: string
  title: string
  lines: string[]
  status: 'pending' | 'working' | 'done' | 'failed' | 'cancelled' | 'expired'
  note?: string
}
/** What the admin assistant looked up for an answer (shown under it; admin chats only). */
export type Lookup = { tool: string; label: string; args: Record<string, unknown>; status: string; definition?: string; at: string }
/** A chart the admin assistant drew (data built by the server from the metric layer). */
export type ChartSpec = {
  kind: 'line' | 'area' | 'bar' | 'pie' | 'scatter'
  horizontal: boolean
  stacked?: boolean
  donut?: boolean
  title: string
  definition: string
  unit: string | null
  y_definition?: string | null
  y_unit?: string | null
  x_label: string
  plotted?: string
  show_values?: boolean
  filters?: string | null
  color_rule?: { type?: string; legend?: { label: string; color: string }[]; threshold?: number; above?: string;
    below?: string; above_label?: string; below_label?: string } | null
  series: { key: string; name: string; color?: string; dash?: string | null }[]
  data: ({ x: string; flag?: string | null; orders?: number; share?: number | null; color?: string; y?: number; compared_with?: string | null } & Record<string, unknown>)[]
  period: { start: string; end: string }
  figures_as_of: string | null
  notes: string[]
}
/** One of the admin's past chats, for the history sidebar. */
export type PastChat = { conversation_id: string; title: string | null; created_at: string; last_message_at: string | null; questions: number }
export type Undo = { tool: string; args: Record<string, unknown>; label?: string; status: 'ready' | 'working' | 'done' | 'failed' }
export type ChatMessage = {
  id: string
  role: 'user' | 'assistant'
  content: string
  steps?: Step[]
  confirms?: ConfirmCard[]
  undos?: Undo[]
  messageId?: number
  rating?: 'up' | 'down'
  error?: string
  lookups?: Lookup[]
  charts?: ChartSpec[]
}

type ChatState = {
  messages: ChatMessage[]
  busy: boolean
  send: (text: string) => Promise<void>
  stop: () => void
  reset: () => void
  decide: (msgId: string, actionId: string, approve: boolean) => Promise<void>
  undo: (msgId: string, index: number) => Promise<void>
  rate: (msgId: string, rating: 'up' | 'down') => Promise<void>
  widgetOpen: boolean
  setWidgetOpen: (open: boolean) => void
  /** Not signed in and the admin hasn't allowed guests: show a sign-in prompt instead of the chat. */
  signInRequired: boolean
  /** Admin chat history: the open chat, reopening a past one, and a counter that changes after each answer. */
  conversationId: string | null
  openPast: (conversationId: string) => Promise<void>
  historyVersion: number
}

type Saved = { conversationId: string | null; messages: ChatMessage[] }

const ChatContext = createContext<ChatState | null>(null)
const storageKey = (who: string) => `olist:chat:${who}`
const newId = () => Math.random().toString(36).slice(2)

function load(who: string): Saved {
  try {
    const raw = JSON.parse(sessionStorage.getItem(storageKey(who)) ?? 'null')
    if (Array.isArray(raw)) return { conversationId: null, messages: raw } // older format
    return raw ?? { conversationId: null, messages: [] }
  } catch {
    return { conversationId: null, messages: [] }
  }
}

async function postJson(url: string, body: unknown) {
  const res = await fetch(url, {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  const data = await res.json().catch(() => ({}))
  if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `Request failed (${res.status})`)
  return data
}

type PastMessage = { message_id: number; role: 'user' | 'assistant' | 'system'; content: string; lookups?: Lookup[]; charts?: ChartSpec[] }

/** One of the admin's own past chats, as chat messages (system notes are left out). */
async function fetchPast(id: string): Promise<{ title: string | null; messages: ChatMessage[] }> {
  const res = await fetch(`/api/admin/chat/conversations/${id}`, { credentials: 'include' })
  if (!res.ok) throw new Error('That chat could not be opened.')
  const data = await res.json()
  return {
    title: data.title,
    messages: (data.messages as PastMessage[])
      .filter((m) => m.role !== 'system')
      .map((m) => ({ id: newId(), role: m.role as 'user' | 'assistant', content: m.content, messageId: m.role === 'assistant' ? m.message_id : undefined, lookups: m.lookups, charts: m.charts })),
  }
}

/** The admin's own past chats, newest first; `search` matches titles and questions. */
export async function fetchPastChats(search: string, before?: string): Promise<PastChat[]> {
  const params = new URLSearchParams({ search, limit: '30' })
  if (before) params.set('before', before)
  const res = await fetch(`/api/admin/chat/conversations?${params}`, { credentials: 'include' })
  if (!res.ok) throw new Error('Chat history could not be loaded.')
  return res.json()
}

export function ChatProvider({ children }: { children: ReactNode }) {
  const { user, refreshCart } = useStore()
  const who = user?.username ?? 'guest'
  const [messages, setMessages] = useState<ChatMessage[]>(() => load(who).messages)
  const [conversationId, setConversationId] = useState<string | null>(() => load(who).conversationId)
  const [busy, setBusy] = useState(false)
  const [historyVersion, setHistoryVersion] = useState(0)
  const [widgetOpen, setWidgetOpen] = useState(false)
  const [guestsAllowed, setGuestsAllowed] = useState<boolean | null>(null)
  // The server enforces this too; asking first lets guests see a sign-in prompt instead of an error.
  useEffect(() => {
    if (user) return
    fetch('/api/chat/status', { credentials: 'include' })
      .then((r) => r.json())
      .then((s) => setGuestsAllowed(Boolean(s.guests_allowed)))
      .catch(() => setGuestsAllowed(false))
  }, [user])
  const signInRequired = !user && guestsAllowed !== true
  const abortRef = useRef<AbortController | null>(null)

  // Each account (and guests) keeps its own conversation for this browser session.
  useEffect(() => {
    abortRef.current?.abort()
    const saved = load(who)
    setMessages(saved.messages)
    setConversationId(saved.conversationId)
    setBusy(false)
  }, [who])

  useEffect(() => {
    try {
      sessionStorage.setItem(storageKey(who), JSON.stringify({ conversationId, messages: messages.slice(-50) }))
    } catch {
      /* storage unavailable: history lasts until reload */
    }
  }, [messages, conversationId, who])

  const patchLast = (fn: (m: ChatMessage) => ChatMessage) =>
    setMessages((all) => (all.length ? [...all.slice(0, -1), fn(all[all.length - 1])] : all))
  const patchById = (id: string, fn: (m: ChatMessage) => ChatMessage) =>
    setMessages((all) => all.map((m) => (m.id === id ? fn(m) : m)))

  const send = useCallback(
    async (text: string) => {
      const content = text.trim()
      if (!content || busy) return
      setMessages((all) => [...all, { id: newId(), role: 'user', content }, { id: newId(), role: 'assistant', content: '', steps: [] }])
      setBusy(true)
      const controller = new AbortController()
      abortRef.current = controller

      let conv = conversationId
      try {
        const endpoint = user?.role === 'admin' ? '/api/admin/chat' : user?.role === 'seller' ? '/api/seller/chat' : '/api/chat'
        const res = await fetch(endpoint, {
          method: 'POST',
          credentials: 'include',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ message: content, conversation_id: conversationId }),
          signal: controller.signal,
        })
        if (!res.ok || !res.body) {
          const detail = await res.json().catch(() => ({}))
          if (res.status === 404 && conversationId) setConversationId(null) // conversation expired: start fresh next time
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
            else if (ev.type === 'reset') patchLast((m) => ({ ...m, content: '' }))
            else if (ev.type === 'conversation') {
              conv = ev.conversation_id
              setConversationId(ev.conversation_id)
            }
            else if (ev.type === 'message') patchLast((m) => ({ ...m, messageId: ev.message_id }))
            else if (ev.type === 'step')
              patchLast((m) => {
                const steps = m.steps ?? []
                const i = steps.findIndex((s) => s.id === ev.id)
                const step = { id: ev.id, label: ev.label, status: ev.status }
                return { ...m, steps: i >= 0 ? steps.map((s, j) => (j === i ? step : s)) : [...steps, step] }
              })
            else if (ev.type === 'confirm')
              patchLast((m) => ({
                ...m,
                confirms: [
                  ...(m.confirms ?? []).filter((c) => c.actionId !== ev.action_id),
                  { actionId: ev.action_id, tool: ev.tool, title: ev.card?.title ?? 'Please confirm', lines: ev.card?.lines ?? [], status: 'pending' },
                ],
              }))
            else if (ev.type === 'undo')
              patchLast((m) => ({ ...m, undos: [...(m.undos ?? []), { tool: ev.tool, args: ev.args, label: ev.label, status: 'ready' }] }))
            else if (ev.type === 'chart') patchLast((m) => ({ ...m, charts: [...(m.charts ?? []), ev.chart] }))
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
      // Admin answers show what was looked up: read it back from the stored chat.
      if (user?.role === 'admin' && conv) {
        setHistoryVersion((v) => v + 1)
        try {
          const chat = await fetchPast(conv)
          const byId = new Map(chat.messages.map((m) => [m.messageId, m.lookups]))
          setMessages((all) => all.map((m) => (m.messageId && byId.get(m.messageId) ? { ...m, lookups: byId.get(m.messageId) } : m)))
        } catch {
          /* lookups stay hidden until the chat is reopened */
        }
      }
    },
    [busy, conversationId, refreshCart, user?.role],
  )

  const setCard = (msgId: string, actionId: string, patch: Partial<ConfirmCard>) =>
    patchById(msgId, (m) => ({ ...m, confirms: (m.confirms ?? []).map((c) => (c.actionId === actionId ? { ...c, ...patch } : c)) }))

  // The seller and admin assistants are separate agents with their own endpoints.
  const chatBase = user?.role === 'seller' ? '/api/seller/chat' : user?.role === 'admin' ? '/api/admin/chat' : '/api/chat'

  const openPast = async (id: string) => {
    if (busy) return
    const chat = await fetchPast(id)
    setConversationId(id)
    setMessages(chat.messages)
  }

  const decide = async (msgId: string, actionId: string, approve: boolean) => {
    setCard(msgId, actionId, { status: 'working' })
    try {
      const r = await postJson(`${chatBase}/actions/${actionId}`, { approve })
      if (r.status === 'rejected') setCard(msgId, actionId, { status: 'cancelled', note: 'Cancelled. Nothing was changed.' })
      else if (r.status === 'executed') setCard(msgId, actionId, { status: 'done', note: 'Done.' })
      else setCard(msgId, actionId, { status: 'failed', note: r.result?.error ?? 'That could not be completed.' })
    } catch (e) {
      setCard(msgId, actionId, { status: 'expired', note: (e as Error).message })
    }
  }

  const undo = async (msgId: string, index: number) => {
    const setUndo = (status: Undo['status']) =>
      patchById(msgId, (m) => ({ ...m, undos: (m.undos ?? []).map((u, i) => (i === index ? { ...u, status } : u)) }))
    const item = messages.find((m) => m.id === msgId)?.undos?.[index]
    if (!item) return
    setUndo('working')
    try {
      await postJson('/api/chat/undo', { tool: item.tool, args: item.args })
      setUndo('done')
      refreshCart()
    } catch {
      setUndo('failed')
    }
  }

  const rate = async (msgId: string, rating: 'up' | 'down') => {
    const m = messages.find((x) => x.id === msgId)
    if (!m?.messageId) return
    patchById(msgId, (x) => ({ ...x, rating }))
    try {
      await postJson(`${chatBase}/feedback`, { message_id: m.messageId, rating })
    } catch {
      patchById(msgId, (x) => ({ ...x, rating: undefined }))
    }
  }

  const stop = () => abortRef.current?.abort()
  const reset = () => {
    stop()
    setMessages([])
    setConversationId(null)
  }

  return (
    <ChatContext.Provider value={{
      messages, busy, send, stop, reset, decide, undo, rate, widgetOpen, setWidgetOpen, signInRequired,
      conversationId, openPast, historyVersion,
    }}>
      {children}
    </ChatContext.Provider>
  )
}

export function useChat() {
  const ctx = useContext(ChatContext)
  if (!ctx) throw new Error('useChat must be used inside ChatProvider')
  return ctx
}

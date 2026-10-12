// Chat UI shared by the full-page assistant and the floating bubble.

import {
  AlertCircle, ArrowUp, Bot, Check, Loader2, Maximize2, MessageCircle, RotateCcw, ShieldCheck, Sparkles, Square,
  ThumbsDown, ThumbsUp, Undo2, X,
} from 'lucide-react'
import { Fragment, useEffect, useRef, useState, type ReactNode } from 'react'
import { Link, useLocation } from 'react-router-dom'
import { useChat, type ChatMessage, type Step } from '../chat'
import { useStore } from '../state'

// What the assistant is called and offers, per account type.
type RoleChat = { title: string; short: string; tagline: string; intro: string; placeholder: string; suggestions: string[] }
const SHOPPER: Omit<RoleChat, 'suggestions'> = {
  title: 'AI Shopping Assistant',
  short: 'Shopping Assistant',
  tagline: 'Agentic: it can look things up and take actions for you',
  intro: 'I can find products, track your orders, add items to your cart and open support requests.',
  placeholder: 'Ask about products, orders, delivery…',
}
const ROLE_CHAT: Record<'guest' | 'customer' | 'admin' | 'seller', RoleChat> = {
  guest: {
    ...SHOPPER,
    intro: SHOPPER.intro + ' Sign in to get help with your orders.',
    suggestions: [
      'What are your best-selling products?',
      'Find top-rated watches under $200',
      'Which categories do you have?',
      'How does delivery work?',
    ],
  },
  customer: {
    ...SHOPPER,
    suggestions: [
      'Where is my latest order?',
      'Find top-rated watches under $200',
      'Add the best-selling garden tool to my cart',
      'One of my orders arrived late — what can I do?',
    ],
  },
  admin: {
    title: 'AI Admin Assistant',
    short: 'Admin Assistant',
    tagline: 'Agentic: it can analyse store data across all customers and sellers',
    intro: 'I can answer questions about sales, orders, customers and sellers.',
    placeholder: 'Ask about sales, orders, customers, sellers…',
    suggestions: [
      'How did revenue change over the last 3 months?',
      'Which orders are late and still not delivered?',
      'Which categories have the lowest ratings?',
      'Show the top 5 sellers by on-time delivery',
    ],
  },
  seller: {
    title: 'AI Seller Assistant',
    short: 'Seller Assistant',
    tagline: 'Agentic: it can analyse your sales and help manage your products',
    intro: 'I can summarise your sales, spot problems with orders or reviews, and help update your products.',
    placeholder: 'Ask about your sales, products, orders…',
    suggestions: [
      'How were my sales over the last 3 months?',
      'Which of my products have the lowest ratings?',
      'Which of my orders shipped late?',
      'Suggest a better price for my best-selling product',
    ],
  },
}

function useRoleChat(): RoleChat {
  const { user } = useStore()
  return ROLE_CHAT[user?.role ?? 'guest']
}

// Full-page assistant inside each area: the store, the admin panel, the seller center.
function assistantPathFor(pathname: string) {
  if (pathname.startsWith('/admin')) return '/admin/assistant'
  if (pathname.startsWith('/seller')) return '/seller/assistant'
  return '/assistant'
}
const ASSISTANT_PATHS = ['/assistant', '/admin/assistant', '/seller/assistant']

// ---- minimal markdown: paragraphs, bullet/numbered lists, **bold**, [links](/path) ----

function inline(text: string): ReactNode[] {
  const parts: ReactNode[] = []
  const re = /\*\*(.+?)\*\*|\[([^\]]+)\]\(([^)\s]+)\)/g
  let last = 0
  let m: RegExpExecArray | null
  while ((m = re.exec(text))) {
    if (m.index > last) parts.push(text.slice(last, m.index))
    if (m[1]) parts.push(<strong key={m.index} className="font-semibold">{m[1]}</strong>)
    else if (m[3].startsWith('/'))
      parts.push(<Link key={m.index} to={m[3]} className="font-medium text-brand-600 underline underline-offset-2">{m[2]}</Link>)
    else
      parts.push(<a key={m.index} href={m[3]} target="_blank" rel="noreferrer" className="font-medium text-brand-600 underline">{m[2]}</a>)
    last = m.index + m[0].length
  }
  if (last < text.length) parts.push(text.slice(last))
  return parts
}

function Markdown({ text }: { text: string }) {
  const blocks: ReactNode[] = []
  let list: { ordered: boolean; items: string[] } | null = null
  const flush = () => {
    if (!list) return
    const Tag = list.ordered ? 'ol' : 'ul'
    blocks.push(
      <Tag key={blocks.length} className={`my-1.5 space-y-0.5 pl-5 ${list.ordered ? 'list-decimal' : 'list-disc'}`}>
        {list.items.map((it, i) => <li key={i}>{inline(it)}</li>)}
      </Tag>,
    )
    list = null
  }
  for (const line of text.split('\n')) {
    const bullet = line.match(/^\s*[-*•]\s+(.*)/)
    const numbered = line.match(/^\s*\d+[.)]\s+(.*)/)
    if (bullet || numbered) {
      const ordered = Boolean(numbered)
      if (list && list.ordered !== ordered) flush()
      list ??= { ordered, items: [] }
      list.items.push((bullet ?? numbered)![1])
    } else {
      flush()
      if (line.trim()) blocks.push(<p key={blocks.length} className="my-1">{inline(line)}</p>)
    }
  }
  flush()
  return <>{blocks}</>
}

// ---- pieces ----

function Steps({ steps }: { steps: Step[] }) {
  if (!steps.length) return null
  return (
    <div className="mb-2 flex flex-col gap-1">
      {steps.map((s) => (
        <div key={s.id} className="flex items-center gap-2 text-xs text-slate-500">
          {s.status === 'running' ? <Loader2 size={12} className="animate-spin text-brand-500" />
            : s.status === 'done' ? <Check size={12} className="text-emerald-600" />
            : <AlertCircle size={12} className="text-rose-500" />}
          {s.label}
        </div>
      ))}
    </div>
  )
}

function ConfirmCards({ m }: { m: ChatMessage }) {
  const { decide } = useChat()
  if (!m.confirms?.length) return null
  return (
    <div className="mt-2 flex flex-col gap-2">
      {m.confirms.map((c) => (
        <div key={c.actionId} className="rounded-xl border border-amber-200 bg-amber-50 p-3">
          <div className="flex items-center gap-1.5 text-sm font-semibold text-slate-900">
            <ShieldCheck size={15} className="text-amber-600" /> {c.title}
          </div>
          <ul className="mt-1.5 space-y-0.5 text-xs text-slate-600">
            {c.lines.map((line, i) => <li key={i}>{line}</li>)}
          </ul>
          {c.status === 'pending' || c.status === 'working' ? (
            <div className="mt-2.5 flex gap-2">
              <button
                onClick={() => decide(m.id, c.actionId, true)}
                disabled={c.status === 'working'}
                className="flex items-center gap-1 rounded-lg bg-brand-500 px-3 py-1.5 text-xs font-semibold text-white disabled:opacity-50"
              >
                {c.status === 'working' ? <Loader2 size={12} className="animate-spin" /> : <Check size={12} />} Confirm
              </button>
              <button
                onClick={() => decide(m.id, c.actionId, false)}
                disabled={c.status === 'working'}
                className="rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-600 disabled:opacity-50"
              >
                Cancel
              </button>
            </div>
          ) : (
            <p className={`mt-2 flex items-center gap-1 text-xs font-medium ${c.status === 'done' ? 'text-emerald-700' : c.status === 'cancelled' ? 'text-slate-500' : 'text-rose-600'}`}>
              {c.status === 'done' ? <Check size={12} /> : c.status === 'cancelled' ? <X size={12} /> : <AlertCircle size={12} />} {c.note}
            </p>
          )}
        </div>
      ))}
    </div>
  )
}

function UndoButtons({ m }: { m: ChatMessage }) {
  const { undo } = useChat()
  if (!m.undos?.length) return null
  return (
    <div className="mt-2 flex flex-wrap gap-2">
      {m.undos.map((u, i) => (
        <button
          key={i}
          onClick={() => undo(m.id, i)}
          disabled={u.status !== 'ready'}
          className="flex items-center gap-1 rounded-lg border border-slate-200 px-2.5 py-1 text-xs text-slate-600 hover:bg-slate-50 disabled:opacity-60"
        >
          <Undo2 size={12} />
          {u.status === 'done' ? 'Undone' : u.status === 'failed' ? 'Undo failed' : u.status === 'working' ? 'Undoing…' : `Undo: ${u.label ?? 'change'}`}
        </button>
      ))}
    </div>
  )
}

function Feedback({ m }: { m: ChatMessage }) {
  const { rate } = useChat()
  if (!m.messageId) return null
  return (
    <div className="mt-1.5 flex gap-1 text-slate-400">
      <button onClick={() => rate(m.id, 'up')} aria-label="Helpful" className={`rounded p-1 hover:bg-slate-100 ${m.rating === 'up' ? 'text-emerald-600' : ''}`}><ThumbsUp size={12} /></button>
      <button onClick={() => rate(m.id, 'down')} aria-label="Not helpful" className={`rounded p-1 hover:bg-slate-100 ${m.rating === 'down' ? 'text-rose-600' : ''}`}><ThumbsDown size={12} /></button>
    </div>
  )
}

function Bubble({ m, streaming }: { m: ChatMessage; streaming: boolean }) {
  if (m.role === 'user') {
    return (
      <div className="flex justify-end">
        <div className="max-w-[85%] whitespace-pre-wrap rounded-2xl rounded-br-md bg-brand-500 px-4 py-2.5 text-sm text-white">{m.content}</div>
      </div>
    )
  }
  const thinking = streaming && !m.content && !(m.steps ?? []).some((s) => s.status === 'running')
  return (
    <div className="flex gap-2.5">
      <span className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-gradient-to-br from-brand-500 to-sky-400 text-white">
        <Sparkles size={14} />
      </span>
      <div className="min-w-0 max-w-[85%] rounded-2xl rounded-tl-md border border-slate-200 bg-white px-4 py-2.5 text-sm leading-relaxed text-slate-700">
        <Steps steps={m.steps ?? []} />
        {thinking && (
          <span className="flex items-center gap-1 py-1" aria-label="Assistant is typing">
            {[0, 150, 300].map((d) => (
              <span key={d} className="h-1.5 w-1.5 animate-bounce rounded-full bg-slate-400" style={{ animationDelay: `${d}ms` }} />
            ))}
          </span>
        )}
        <Markdown text={m.content} />
        <ConfirmCards m={m} />
        <UndoButtons m={m} />
        {!streaming && <Feedback m={m} />}
        {m.error && (
          <p className="mt-1 flex items-center gap-1.5 text-xs text-rose-600"><AlertCircle size={12} /> {m.error}</p>
        )}
      </div>
    </div>
  )
}

function Composer({ autoFocus = false }: { autoFocus?: boolean }) {
  const { send, busy, stop } = useChat()
  const roleChat = useRoleChat()
  const [text, setText] = useState('')
  const ref = useRef<HTMLTextAreaElement>(null)
  useEffect(() => {
    if (autoFocus) ref.current?.focus()
  }, [autoFocus])
  useEffect(() => {
    const el = ref.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 140)}px`
  }, [text])

  const submit = () => {
    if (!text.trim() || busy) return
    send(text)
    setText('')
  }
  return (
    <div className="flex items-end gap-2 rounded-2xl border border-slate-200 bg-white p-2 shadow-sm focus-within:border-brand-500 focus-within:ring-4 focus-within:ring-brand-100">
      <textarea
        ref={ref}
        rows={1}
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault()
            submit()
          }
        }}
        placeholder={roleChat.placeholder}
        className="max-h-36 flex-1 resize-none bg-transparent px-2 py-1.5 text-sm outline-none"
        aria-label="Message the assistant"
      />
      {busy ? (
        <button onClick={stop} className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-slate-900 text-white" aria-label="Stop">
          <Square size={14} fill="currentColor" />
        </button>
      ) : (
        <button onClick={submit} disabled={!text.trim()} className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-brand-500 text-white disabled:opacity-40" aria-label="Send">
          <ArrowUp size={18} />
        </button>
      )}
    </div>
  )
}

function Welcome({ compact }: { compact: boolean }) {
  const { send } = useChat()
  const { intro, suggestions } = useRoleChat()
  return (
    <div className={`flex flex-col items-center text-center ${compact ? 'px-2 py-6' : 'py-12'}`}>
      <span className={`flex items-center justify-center rounded-2xl bg-gradient-to-br from-brand-500 to-sky-400 text-white ${compact ? 'h-12 w-12' : 'h-16 w-16'}`}>
        <Sparkles size={compact ? 24 : 32} />
      </span>
      <h2 className={`mt-4 font-bold text-slate-900 ${compact ? 'text-lg' : 'text-2xl'}`}>How can I help you today?</h2>
      <p className="mt-1 max-w-md text-sm text-slate-500">{intro}</p>
      <div className={`mt-6 grid w-full gap-2 ${compact ? '' : 'max-w-2xl sm:grid-cols-2'}`}>
        {suggestions.map((s) => (
          <button key={s} onClick={() => send(s)} className="rounded-xl border border-slate-200 bg-white px-4 py-3 text-left text-sm text-slate-700 transition hover:border-brand-500 hover:bg-brand-50">
            {s}
          </button>
        ))}
      </div>
    </div>
  )
}

export function ChatThread({ compact = false }: { compact?: boolean }) {
  const { messages, busy } = useChat()
  const end = useRef<HTMLDivElement>(null)
  useEffect(() => {
    end.current?.scrollIntoView({ block: 'end' })
  }, [messages])
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className={`min-h-0 flex-1 overflow-y-auto ${compact ? 'px-4 py-3' : 'px-1 py-4'}`}>
        {messages.length === 0 ? <Welcome compact={compact} /> : (
          <div className="space-y-4">
            {messages.map((m, i) => (
              <Fragment key={m.id}><Bubble m={m} streaming={busy && i === messages.length - 1} /></Fragment>
            ))}
          </div>
        )}
        <div ref={end} />
      </div>
      <div className={compact ? 'border-t border-slate-100 p-3' : 'pt-2'}>
        <Composer autoFocus={compact} />
        <p className="mt-1.5 text-center text-[11px] text-slate-400">AI can make mistakes. Check order details before acting on them.</p>
      </div>
    </div>
  )
}

// ---- full page ----

export function AssistantPage() {
  const { messages, reset } = useChat()
  const roleChat = useRoleChat()
  const { pathname } = useLocation()
  // The admin panel and seller center have no store header above the page.
  const inPanel = pathname.startsWith('/admin') || pathname.startsWith('/seller')
  return (
    <div className={`mx-auto flex max-w-3xl flex-col ${inPanel ? 'h-[calc(100vh-4rem)]' : 'h-[calc(100vh-11rem)]'}`}>
      <div className="flex items-center justify-between border-b border-slate-200 pb-3">
        <div className="flex items-center gap-2">
          <Bot className="text-brand-500" />
          <div>
            <h1 className="text-lg font-bold text-slate-900">{roleChat.title}</h1>
            <p className="text-xs text-slate-500">{roleChat.tagline}</p>
          </div>
        </div>
        {messages.length > 0 && (
          <button onClick={reset} className="flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm text-slate-500 hover:bg-slate-100">
            <RotateCcw size={14} /> New chat
          </button>
        )}
      </div>
      <ChatThread />
    </div>
  )
}

// ---- floating bubble ----

export function ChatWidget() {
  const roleChat = useRoleChat()
  const { widgetOpen, setWidgetOpen, messages, reset } = useChat()
  const { pathname } = useLocation()
  const fullPage = assistantPathFor(pathname)
  if (ASSISTANT_PATHS.includes(pathname)) return null

  return (
    <div className="fixed bottom-4 right-4 z-40 flex flex-col items-end gap-3">
      {widgetOpen && (
        <div className="flex h-[min(600px,calc(100vh-7rem))] w-[min(400px,calc(100vw-2rem))] flex-col overflow-hidden rounded-2xl border border-slate-200 bg-slate-50 shadow-2xl">
          <div className="flex items-center gap-2 bg-gradient-to-r from-brand-600 to-brand-500 px-4 py-3 text-white">
            <Sparkles size={18} />
            <div className="flex-1">
              <div className="text-sm font-semibold">{roleChat.short}</div>
              <div className="text-[11px] text-white/80">Usually replies in a few seconds</div>
            </div>
            {messages.length > 0 && (
              <button onClick={reset} className="rounded-lg p-1.5 hover:bg-white/15" aria-label="New chat" title="New chat"><RotateCcw size={16} /></button>
            )}
            <Link to={fullPage} onClick={() => setWidgetOpen(false)} className="rounded-lg p-1.5 hover:bg-white/15" aria-label="Open full screen" title="Open full screen">
              <Maximize2 size={16} />
            </Link>
            <button onClick={() => setWidgetOpen(false)} className="rounded-lg p-1.5 hover:bg-white/15" aria-label="Close chat"><X size={18} /></button>
          </div>
          <ChatThread compact />
        </div>
      )}
      <button
        onClick={() => setWidgetOpen(!widgetOpen)}
        className="flex h-14 w-14 items-center justify-center rounded-full bg-gradient-to-br from-brand-600 to-sky-500 text-white shadow-xl transition hover:scale-105 active:scale-95"
        aria-label={widgetOpen ? 'Close assistant' : 'Open assistant'}
      >
        {widgetOpen ? <X size={24} /> : <MessageCircle size={26} />}
      </button>
    </div>
  )
}

// Chat UI shared by the full-page assistant and the floating bubble.

import {
  AlertCircle, ArrowUp, Bot, Check, ChevronDown, Database, History, Loader2, Maximize2, MessageCircle, Plus, RotateCcw,
  Search, ShieldCheck, Sparkles, Square, Lock, ThumbsDown, ThumbsUp, Undo2, X,
} from 'lucide-react'
import { Fragment, lazy, Suspense, useEffect, useRef, useState, type ReactNode } from 'react'
import { Link, useLocation } from 'react-router-dom'
import { fetchPastChats, useChat, type ChatMessage, type Lookup, type PastChat, type Step } from '../chat'
import { useStore } from '../state'

// Charts load only when the admin assistant draws one (keeps the chart library off other pages).
const ChartView = lazy(() => import('./ChartView'))

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
    tagline: 'Read-only: platform figures, sellers, refunds and support, with customers masked',
    intro: "I can answer questions about sales, sellers, delivery, refunds, support and the assistants. Customers are shown masked, and I can't change anything.",
    placeholder: 'Ask about sales, orders, customers, sellers…',
    suggestions: [
      'How did item revenue change month by month this year?',
      'Which refunds are stuck past their expected time?',
      'Which sellers with 50+ orders have the worst on-time rate?',
      'GMV by buyer state this year: top 5',
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
  const re = /\*\*(.+?)\*\*|\[([^\]]+)\]\(([^)\s]+)\)|(?<![\w*])[*_]([^*_\n]+?)[*_](?![\w*])/g
  let last = 0
  let m: RegExpExecArray | null
  while ((m = re.exec(text))) {
    if (m.index > last) parts.push(text.slice(last, m.index))
    if (m[1]) parts.push(<strong key={m.index} className="font-semibold">{m[1]}</strong>)
    else if (m[4]) parts.push(<em key={m.index} className="text-slate-500">{m[4]}</em>)
    else if (m[3].startsWith('/'))
      parts.push(<Link key={m.index} to={m[3]} className="font-medium text-brand-600 underline underline-offset-2">{m[2]}</Link>)
    else
      parts.push(<a key={m.index} href={m[3]} target="_blank" rel="noreferrer" className="font-medium text-brand-600 underline">{m[2]}</a>)
    last = m.index + m[0].length
  }
  if (last < text.length) parts.push(text.slice(last))
  return parts
}

const isTableRow = (line: string) => /^\s*\|.*\|\s*$/.test(line)
const isTableRule = (line: string) => /^\s*\|?(\s*:?-{2,}:?\s*\|)+\s*:?-*:?\s*\|?\s*$/.test(line)
const cells = (line: string) => line.trim().replace(/^\||\|$/g, '').split('|').map((c) => c.trim())
// Numbers, money and percentages are right-aligned so columns line up.
const numeric = (cell: string) => /^[+\-−]?\$?[\d,.]+%?$|^n\/a$/i.test(cell)

function Table({ rows }: { rows: string[] }) {
  const [head, ...body] = rows.filter((r) => !isTableRule(r)).map(cells)
  return (
    <div className="my-2 overflow-x-auto rounded-lg border border-slate-200">
      <table className="w-full border-collapse text-xs">
        <thead className="bg-slate-50 text-left text-slate-600">
          <tr>{head.map((h, i) => <th key={i} className="whitespace-nowrap px-3 py-1.5 font-semibold">{inline(h)}</th>)}</tr>
        </thead>
        <tbody>
          {body.map((r, i) => (
            <tr key={i} className="border-t border-slate-100">
              {head.map((_, j) => (
                <td key={j} className={`whitespace-nowrap px-3 py-1.5 ${numeric(r[j] ?? '') ? 'text-right tabular-nums' : ''}`}>{inline(r[j] ?? '')}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function Markdown({ text }: { text: string }) {
  const blocks: ReactNode[] = []
  let list: { ordered: boolean; items: string[] } | null = null
  let table: string[] | null = null
  const flushList = () => {
    if (!list) return
    const Tag = list.ordered ? 'ol' : 'ul'
    blocks.push(
      <Tag key={blocks.length} className={`my-1.5 space-y-0.5 pl-5 ${list.ordered ? 'list-decimal' : 'list-disc'}`}>
        {list.items.map((it, i) => <li key={i}>{inline(it)}</li>)}
      </Tag>,
    )
    list = null
  }
  const flushTable = () => {
    if (table) blocks.push(<Table key={blocks.length} rows={table} />)
    table = null
  }
  const lines = text.split('\n')
  lines.forEach((line, i) => {
    if (isTableRow(line) || (table && isTableRule(line))) {
      flushList()
      ;(table ??= []).push(line)
      return
    }
    // A blank line inside a table (some models add them between rows) doesn't end it.
    if (table && !line.trim() && isTableRow(lines.slice(i + 1).find((l) => l.trim()) ?? '')) return
    flushTable()
    const heading = line.match(/^\s*#{1,6}\s+(.*)/)
    const bullet = line.match(/^\s*[-*•]\s+(.*)/)
    const numbered = line.match(/^\s*\d+[.)]\s+(.*)/)
    if (bullet || numbered) {
      const ordered = Boolean(numbered)
      if (list && list.ordered !== ordered) flushList()
      list ??= { ordered, items: [] }
      list.items.push((bullet ?? numbered)![1])
    } else {
      flushList()
      if (heading) blocks.push(<p key={blocks.length} className="mt-2 mb-1 font-semibold text-slate-900">{inline(heading[1])}</p>)
      else if (line.trim()) blocks.push(<p key={blocks.length} className="my-1">{inline(line)}</p>)
    }
  })
  flushList()
  flushTable()
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

function describeArgs(args: Record<string, unknown>) {
  return Object.entries(args)
    .filter(([k, v]) => k !== 'sql' && v !== null && v !== undefined && !(Array.isArray(v) && v.length === 0))
    .map(([k, v]) => `${k.replace(/_/g, ' ')}: ${Array.isArray(v) ? v.join(', ') : String(v)}`)
    .join(' · ')
}

/** Admin answers: what was looked up, with the metric definition used (read from the stored chat). */
function Lookups({ lookups }: { lookups: Lookup[] }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="mt-2 border-t border-slate-100 pt-2">
      <button onClick={() => setOpen(!open)} className="flex items-center gap-1 text-xs font-medium text-slate-500 hover:text-slate-700" aria-expanded={open}>
        <Database size={12} /> Lookups ({lookups.length})
        <ChevronDown size={12} className={`transition ${open ? 'rotate-180' : ''}`} />
      </button>
      {open && (
        <ol className="mt-2 space-y-2">
          {lookups.map((l, i) => (
            <li key={i} className="rounded-lg bg-slate-50 px-3 py-2 text-xs text-slate-600">
              <div className="flex items-center justify-between gap-2">
                <span className="font-semibold text-slate-700">{l.label}</span>
                <span className={l.status === 'ok' ? 'text-emerald-600' : 'text-rose-600'}>{l.status.replace(/_/g, ' ')}</span>
              </div>
              {describeArgs(l.args) && <div className="mt-0.5">{describeArgs(l.args)}</div>}
              {l.definition && <div className="mt-0.5 italic text-slate-500">Definition: {l.definition}</div>}
              {typeof l.args.sql === 'string' && (
                <pre className="mt-1 overflow-x-auto whitespace-pre-wrap rounded bg-white p-2 font-mono text-[11px] text-slate-700">{l.args.sql}</pre>
              )}
            </li>
          ))}
        </ol>
      )}
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
        {m.charts?.map((c, i) => (
          <Suspense key={i} fallback={<div className="my-2 h-40 animate-pulse rounded-xl bg-slate-100" />}>
            <ChartView chart={c} />
          </Suspense>
        ))}
        <Markdown text={m.content} />
        <ConfirmCards m={m} />
        <UndoButtons m={m} />
        {!streaming && m.lookups && m.lookups.length > 0 && <Lookups lookups={m.lookups} />}
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

function SignInPrompt({ compact }: { compact: boolean }) {
  const { setWidgetOpen } = useChat()
  return (
    <div className={`flex flex-1 flex-col items-center justify-center text-center ${compact ? 'px-6 py-10' : 'py-16'}`}>
      <span className="flex h-14 w-14 items-center justify-center rounded-2xl bg-brand-50 text-brand-600"><Lock size={26} /></span>
      <h2 className="mt-4 text-lg font-bold text-slate-900">Sign in to use the assistant</h2>
      <p className="mt-1 max-w-sm text-sm text-slate-500">
        The assistant helps with your orders, deliveries, returns, refunds and cart, so it's available once you're signed in.
      </p>
      <Link to="/login" onClick={() => setWidgetOpen(false)} className="mt-5 rounded-xl bg-brand-500 px-5 py-2.5 text-sm font-semibold text-white hover:bg-brand-600">
        Sign in
      </Link>
    </div>
  )
}

export function ChatThread({ compact = false }: { compact?: boolean }) {
  const { messages, busy, signInRequired } = useChat()
  const end = useRef<HTMLDivElement>(null)
  useEffect(() => {
    end.current?.scrollIntoView({ block: 'end' })
  }, [messages])
  if (signInRequired) return <SignInPrompt compact={compact} />
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

// ---- admin chat history ----

function when(iso: string | null) {
  if (!iso) return ''
  const d = new Date(iso.replace(' ', 'T'))
  const today = new Date()
  return d.toDateString() === today.toDateString()
    ? d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
    : d.toLocaleDateString([], { day: 'numeric', month: 'short', year: d.getFullYear() === today.getFullYear() ? undefined : 'numeric' })
}

const PAGE = 30

/** The admin's own past chats: search, reopen, start a new one. Kept 1 year as an audit trail. */
function HistorySidebar() {
  const { conversationId, openPast, reset, busy, historyVersion } = useChat()
  const [search, setSearch] = useState('')
  const [chats, setChats] = useState<PastChat[]>([])
  const [more, setMore] = useState(false)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let live = true
    const timer = setTimeout(() => {
      setLoading(true)
      fetchPastChats(search.trim())
        .then((rows) => {
          if (!live) return
          setChats(rows)
          setMore(rows.length === PAGE)
          setError('')
        })
        .catch((e) => live && setError((e as Error).message))
        .finally(() => live && setLoading(false))
    }, search ? 250 : 0)
    return () => {
      live = false
      clearTimeout(timer)
    }
  }, [search, historyVersion])

  const loadOlder = async () => {
    const last = chats[chats.length - 1]
    try {
      const rows = await fetchPastChats(search.trim(), last?.last_message_at ?? last?.created_at)
      setChats([...chats, ...rows])
      setMore(rows.length === PAGE)
    } catch (e) {
      setError((e as Error).message)
    }
  }
  const open = async (id: string) => {
    try {
      await openPast(id)
    } catch (e) {
      setError((e as Error).message)
    }
  }

  return (
    <aside className="flex h-full min-h-0 w-full flex-col border-slate-200 md:w-72 md:border-r md:pr-4">
      <button onClick={reset} disabled={busy} className="flex items-center justify-center gap-1.5 rounded-xl bg-brand-500 px-3 py-2 text-sm font-semibold text-white hover:bg-brand-600 disabled:opacity-50">
        <Plus size={16} /> New chat
      </button>
      <label className="mt-3 flex items-center gap-2 rounded-xl border border-slate-200 bg-white px-3 py-2 focus-within:border-brand-500">
        <Search size={14} className="text-slate-400" />
        <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search your chats" maxLength={100}
          className="min-w-0 flex-1 bg-transparent text-sm outline-none" aria-label="Search your chats" />
      </label>
      <div className="mt-3 flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-slate-400">
        <History size={12} /> Your chats
      </div>
      <div className="mt-1 min-h-0 flex-1 overflow-y-auto">
        {error && <p className="py-2 text-xs text-rose-600">{error}</p>}
        {loading && chats.length === 0 ? <p className="py-2 text-xs text-slate-400">Loading…</p> : chats.length === 0 ? (
          <p className="py-2 text-xs text-slate-400">{search ? 'No chats match.' : 'No chats yet.'}</p>
        ) : (
          <ul className="space-y-0.5">
            {chats.map((c) => (
              <li key={c.conversation_id}>
                <button onClick={() => open(c.conversation_id)} disabled={busy}
                  className={`w-full rounded-lg px-2.5 py-2 text-left text-sm transition disabled:opacity-60 ${c.conversation_id === conversationId ? 'bg-brand-50 text-brand-700' : 'text-slate-700 hover:bg-slate-100'}`}
                  aria-current={c.conversation_id === conversationId}>
                  <span className="line-clamp-2">{c.title || 'Untitled chat'}</span>
                  <span className="mt-0.5 block text-[11px] text-slate-400">
                    {when(c.last_message_at ?? c.created_at)} · {c.questions} question{c.questions === 1 ? '' : 's'}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}
        {more && <button onClick={loadOlder} className="mt-2 w-full rounded-lg py-1.5 text-xs text-brand-600 hover:bg-brand-50">Load older chats</button>}
      </div>
      <p className="mt-2 flex items-start gap-1 text-[11px] text-slate-400">
        <ShieldCheck size={12} className="mt-0.5 shrink-0" /> Only you see your chats. They're kept for 1 year as an audit trail and can't be deleted.
      </p>
    </aside>
  )
}

// ---- full page ----

export function AssistantPage() {
  const { user } = useStore()
  return user?.role === 'admin' ? <AdminAssistantPage /> : <RoleAssistantPage />
}

function AdminAssistantPage() {
  const roleChat = useRoleChat()
  return (
    <div className="mx-auto flex h-[calc(100vh-4rem)] max-w-6xl flex-col gap-4 md:flex-row">
      <div className="h-56 shrink-0 md:h-full"><HistorySidebar /></div>
      <div className="flex min-h-0 min-w-0 flex-1 flex-col">
        <div className="flex items-center gap-2 border-b border-slate-200 pb-3">
          <Bot className="text-brand-500" />
          <div>
            <h1 className="text-lg font-bold text-slate-900">{roleChat.title}</h1>
            <p className="text-xs text-slate-500">{roleChat.tagline}</p>
          </div>
        </div>
        <ChatThread />
      </div>
    </div>
  )
}

function RoleAssistantPage() {
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

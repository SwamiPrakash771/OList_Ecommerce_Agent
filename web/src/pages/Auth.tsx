import { FlaskConical, Loader2, Lock, ShieldCheck, Store } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link, Navigate, useNavigate } from 'react-router-dom'
import { api, ApiError, type DemoAccount, type User } from '../api'
import { AddressEditor } from '../components/Address'
import { Card } from '../components/ui'
import { date, homeFor } from '../lib'
import { useStore } from '../state'

const input = 'w-full rounded-xl border border-slate-200 px-4 py-2.5 outline-none focus:border-brand-500 focus:ring-4 focus:ring-brand-100'

// Testing aid: one-click sign-in as a preset customer or the admin.
// Only shown when the server has DEMO_LOGIN enabled.
function QuickLogin({ onDone }: { onDone: (me: User) => void }) {
  const { demoLogin, notify } = useStore()
  const [accounts, setAccounts] = useState<DemoAccount[]>([])
  const [busy, setBusy] = useState('')
  useEffect(() => {
    api.demoAccounts().then(setAccounts).catch(() => setAccounts([]))
  }, [])
  if (accounts.length === 0) return null

  const go = async (username: string) => {
    setBusy(username)
    try {
      onDone(await demoLogin(username))
    } catch (err) {
      notify(err instanceof ApiError ? err.message : 'Quick login failed', 'error')
      setBusy('')
    }
  }
  const customers = accounts.filter((a) => a.role === 'customer')
  const sellers = accounts.filter((a) => a.role === 'seller')
  const admin = accounts.find((a) => a.role === 'admin')

  return (
    <Card className="mt-4 border-dashed">
      <div className="mb-3 flex items-center gap-2">
        <FlaskConical size={16} className="text-accent-500" />
        <h2 className="text-sm font-semibold text-slate-800">Quick test login</h2>
        <span className="ml-auto rounded-full bg-amber-100 px-2 py-0.5 text-[11px] font-medium text-amber-800">Demo only</span>
      </div>
      <div className="grid gap-2">
        {customers.map((a, i) => (
          <button
            key={a.username}
            onClick={() => go(a.username)}
            disabled={!!busy}
            className="flex items-center gap-3 rounded-xl border border-slate-200 px-3 py-2 text-left transition hover:border-brand-500 hover:bg-brand-50 disabled:opacity-50"
          >
            <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-brand-100 text-sm font-bold text-brand-700">
              {busy === a.username ? <Loader2 size={14} className="animate-spin" /> : i + 1}
            </span>
            <span className="min-w-0 flex-1">
              <span className="block text-sm font-semibold text-slate-800">Customer {i + 1} · {a.label}</span>
              <span className="block truncate font-mono text-[11px] text-slate-400">{a.username}</span>
            </span>
            <span className="shrink-0 text-xs text-slate-500">{a.orders} orders</span>
          </button>
        ))}
        {sellers.map((a, i) => (
          <button
            key={a.username}
            onClick={() => go(a.username)}
            disabled={!!busy}
            className="flex items-center gap-3 rounded-xl border border-emerald-200 bg-emerald-50/50 px-3 py-2 text-left transition hover:border-emerald-500 hover:bg-emerald-50 disabled:opacity-50"
          >
            <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-emerald-100 text-emerald-700">
              {busy === a.username ? <Loader2 size={14} className="animate-spin" /> : <Store size={14} />}
            </span>
            <span className="min-w-0 flex-1">
              <span className="block text-sm font-semibold text-slate-800">Seller {i + 1} · {a.label}</span>
              <span className="block truncate font-mono text-[11px] text-slate-400">{a.username}</span>
            </span>
            <span className="shrink-0 text-xs text-slate-500">{a.orders?.toLocaleString()} orders</span>
          </button>
        ))}
        {admin && (
          <button
            onClick={() => go(admin.username)}
            disabled={!!busy}
            className="mt-1 flex items-center justify-center gap-2 rounded-xl bg-slate-900 px-3 py-2.5 text-sm font-semibold text-white transition hover:bg-slate-800 disabled:opacity-50"
          >
            {busy === admin.username ? <Loader2 size={16} className="animate-spin" /> : <ShieldCheck size={16} />}
            Sign in as Admin
          </button>
        )}
      </div>
    </Card>
  )
}

export function Login() {
  const { user, login } = useStore()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const navigate = useNavigate()

  // Every sign-in lands on Home: the store home, or the seller's own shop home.
  if (user) return <Navigate to={homeFor(user.role)} replace />

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError('')
    try {
      const me = await login(username.trim(), password)
      navigate(homeFor(me.role), { replace: true })
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Sign in failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-gradient-to-br from-brand-50 to-slate-100 px-4">
      <div className="w-full max-w-md">
        <Link to="/" className="mb-6 flex items-center justify-center gap-2 text-2xl font-extrabold">
          <span className="flex h-10 w-10 items-center justify-center rounded-xl bg-brand-500 text-white">O</span>
          Olist<span className="-ml-2 text-brand-500">Store</span>
        </Link>
        <Card className="p-8 shadow-xl">
          <h1 className="text-2xl font-bold text-slate-900">Sign in</h1>
          <p className="mt-1 text-sm text-slate-500">Customers use their customer ID. Admins use their admin username.</p>
          <form onSubmit={submit} className="mt-6 space-y-4">
            <div>
              <label htmlFor="u" className="mb-1 block text-sm font-medium text-slate-700">Customer ID or username</label>
              <input id="u" className={input} value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" required />
            </div>
            <div>
              <label htmlFor="p" className="mb-1 block text-sm font-medium text-slate-700">Password</label>
              <input id="p" type="password" className={input} value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" required />
            </div>
            {error && <p className="rounded-lg bg-rose-50 px-3 py-2 text-sm text-rose-700" role="alert">{error}</p>}
            <button disabled={busy} className="flex w-full items-center justify-center gap-2 rounded-xl bg-brand-500 py-3 font-semibold text-white hover:bg-brand-600 disabled:opacity-60">
              {busy ? <Loader2 size={18} className="animate-spin" /> : <Lock size={16} />} Sign in
            </button>
          </form>
        </Card>
        <QuickLogin onDone={(me) => navigate(homeFor(me.role), { replace: true })} />
        <p className="mt-4 text-center text-sm text-slate-500">
          <Link to="/" className="hover:underline">← Continue browsing as guest</Link>
        </p>
      </div>
    </div>
  )
}

export function Account() {
  const { user, refreshUser, notify } = useStore()
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [confirm, setConfirm] = useState('')
  const [error, setError] = useState('')
  if (!user) return null
  const p = user.profile

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    setError('')
    if (next !== confirm) return setError("New passwords don't match.")
    try {
      await api.changePassword(current, next)
      setCurrent(''); setNext(''); setConfirm('')
      await refreshUser()
      notify('Password updated')
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not update password')
    }
  }

  return (
    <>
      <h1 className="mb-6 text-3xl font-bold text-slate-900">My account</h1>
      <div className="grid gap-6 lg:grid-cols-2">
        {user.role === 'customer' && (
          <Card className="lg:col-span-2">
            <h2 className="mb-4 text-lg font-semibold">Shipping address</h2>
            <AddressEditor />
          </Card>
        )}
        <Card>
          <h2 className="mb-4 text-lg font-semibold">Profile</h2>
          <dl className="space-y-3 text-sm">
            <div><dt className="text-slate-500">{user.role === 'admin' ? 'Username' : user.role === 'seller' ? 'Seller ID' : 'Customer ID'}</dt><dd className="font-mono">{user.username}</dd></div>
            {p && user.role === 'customer' && (
              <>
                <div><dt className="text-slate-500">Location</dt><dd>{p.city.replace(/\b\w/g, (c) => c.toUpperCase())}, {p.state} · ZIP prefix {p.zip_code}</dd></div>
                <div><dt className="text-slate-500">Customer since</dt><dd>{date(p.first_order)}</dd></div>
                <div><dt className="text-slate-500">Orders</dt><dd>{p.orders} · <Link to="/orders" className="text-brand-600 hover:underline">view orders</Link></dd></div>
              </>
            )}
          </dl>
        </Card>
        <Card>
          <h2 className="mb-1 text-lg font-semibold">Change password</h2>
          {user.must_change_password && <p className="mb-3 text-sm text-amber-700">You're using a temporary password — please set your own.</p>}
          <form onSubmit={submit} className="mt-3 space-y-3">
            <input type="password" placeholder="Current password" className={input} value={current} onChange={(e) => setCurrent(e.target.value)} required autoComplete="current-password" />
            <input type="password" placeholder="New password (min 8 characters)" className={input} value={next} onChange={(e) => setNext(e.target.value)} required minLength={8} autoComplete="new-password" />
            <input type="password" placeholder="Confirm new password" className={input} value={confirm} onChange={(e) => setConfirm(e.target.value)} required autoComplete="new-password" />
            {error && <p className="text-sm text-rose-600" role="alert">{error}</p>}
            <button className="rounded-xl bg-brand-500 px-5 py-2.5 font-semibold text-white hover:bg-brand-600">Update password</button>
          </form>
        </Card>
      </div>
    </>
  )
}

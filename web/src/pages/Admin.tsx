// Admin area: full access to every table, behind the admin role.

import { ArrowLeft, BarChart3, KeyRound, LifeBuoy, LogOut, Package, Search, ShoppingBag, Sparkles, Store, Tag, Users } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link, NavLink, Outlet, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { Bar, BarChart, CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { api, ApiError, type OrderDetail } from '../api'
import { AddressLines } from '../components/Address'
import { Toasts } from '../components/Layout'
import { ChatWidget } from '../components/Chat'
import OrderView from '../components/OrderView'
import TempPasswordBanner from '../components/TempPasswordBanner'
import { Card, EmptyState, Spinner, Stat } from '../components/ui'
import { categoryLabel, date, money } from '../lib'
import { useStore } from '../state'
import { OrderRow } from './Orders'

const SERIES = '#2a78d6' // categorical slot 1 of the reference palette
const field = 'rounded-xl border border-slate-200 bg-white px-3 py-2 text-sm outline-none focus:border-brand-500 focus:ring-4 focus:ring-brand-100'

export function AdminLayout() {
  const { user, logout } = useStore()
  const navigate = useNavigate()
  const links = [
    ['/admin', 'Dashboard', BarChart3],
    ['/admin/assistant', 'AI Assistant', Sparkles],
    ['/admin/orders', 'Orders', Package],
    ['/admin/products', 'Products', Tag],
    ['/admin/customers', 'Customers', Users],
    ['/admin/sellers', 'Sellers', Store],
    ['/admin/tickets', 'Support tickets', LifeBuoy],
    ['/admin/users', 'User accounts', KeyRound],
  ] as const
  return (
    <div className="flex min-h-screen">
      <aside className="sticky top-0 flex h-screen w-60 shrink-0 flex-col bg-slate-900 text-slate-300">
        <Link to="/admin" className="flex items-center gap-2 px-5 py-5 text-lg font-extrabold text-white">
          <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-brand-500">O</span> Olist Admin
        </Link>
        <nav className="flex-1 space-y-1 px-3">
          {links.map(([to, label, Icon]) => (
            <NavLink
              key={to}
              to={to}
              end={to === '/admin'}
              className={({ isActive }) => `flex items-center gap-3 rounded-lg px-3 py-2 text-sm ${isActive ? 'bg-white/10 font-semibold text-white' : 'hover:bg-white/5 hover:text-white'}`}
            >
              <Icon size={18} /> {label}
            </NavLink>
          ))}
        </nav>
        <div className="space-y-1 border-t border-white/10 p-3 text-sm">
          <Link to="/" className="flex items-center gap-3 rounded-lg px-3 py-2 hover:bg-white/5 hover:text-white"><ShoppingBag size={18} /> Store home</Link>
          <button onClick={async () => { await logout(); navigate('/') }} className="flex w-full items-center gap-3 rounded-lg px-3 py-2 hover:bg-white/5 hover:text-white">
            <LogOut size={18} /> Sign out ({user?.username})
          </button>
        </div>
      </aside>
      <main className="min-w-0 flex-1 bg-slate-50 p-8">
        <TempPasswordBanner accountPath="/admin/account" className="mb-6 rounded-xl" />
        <Outlet />
      </main>
      <Toasts />
      <ChatWidget />
    </div>
  )
}

function ChartTooltip({ active, payload, label, fmt }: { active?: boolean; payload?: { value: number }[]; label?: string; fmt: (v: number) => string }) {
  if (!active || !payload?.length) return null
  return (
    <div className="rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm shadow-lg">
      <div className="text-slate-500">{label}</div>
      <div className="font-semibold text-slate-900">{fmt(payload[0].value)}</div>
    </div>
  )
}

export function Dashboard() {
  const [d, setD] = useState<Awaited<ReturnType<typeof api.admin.dashboard>> | null>(null)
  useEffect(() => {
    api.admin.dashboard().then(setD)
  }, [])
  if (!d) return <Spinner />
  const k = d.kpis
  const monthly = d.monthly_revenue.map((m) => ({
    ...m,
    label: new Date(m.month).toLocaleDateString('en-GB', { month: 'short', year: '2-digit', timeZone: 'UTC' }),
  }))
  const cats = d.top_categories.map((c) => ({ ...c, label: categoryLabel(c.category) }))
  const usd = (v: number) => v.toLocaleString('en-US', { style: 'currency', currency: 'USD' })
  const compact = (v: number) => (v >= 1e6 ? `${(v / 1e6).toFixed(1)}M` : v >= 1e3 ? `${(v / 1e3).toFixed(0)}k` : String(v))

  return (
    <>
      <div className="mb-6 flex items-end justify-between">
        <h1 className="text-3xl font-bold text-slate-900">Dashboard</h1>
        <span className="text-xs text-slate-500">
          Updated {new Date(d.computed_at * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })} · refreshes every 5 min
        </span>
      </div>
      <div className="grid grid-cols-2 gap-4 md:grid-cols-3 xl:grid-cols-6">
        <Stat label="Revenue" value={`$${(k.revenue / 1e6).toFixed(2)}M`} />
        <Stat label="Orders" value={k.orders.toLocaleString()} />
        <Stat label="Customers" value={k.customers.toLocaleString()} />
        <Stat label="Sellers" value={k.sellers.toLocaleString()} />
        <Stat label="Avg review" value={`${k.avg_review.toFixed(2)} ★`} />
        <Stat label="On-time delivery" value={`${k.on_time_pct.toFixed(1)}%`} />
      </div>

      <Card className="mt-6">
        <h2 className="font-semibold text-slate-900">Monthly revenue</h2>
        <p className="mb-4 text-sm text-slate-500">Payments on non-canceled orders · months with 100+ orders</p>
        <div className="h-72">
          <ResponsiveContainer>
            <LineChart data={monthly} margin={{ left: 8, right: 16, top: 8 }}>
              <CartesianGrid stroke="#e2e8f0" vertical={false} />
              <XAxis dataKey="label" tick={{ fontSize: 12, fill: '#64748b' }} tickLine={false} axisLine={{ stroke: '#cbd5e1' }} />
              <YAxis tickFormatter={compact} tick={{ fontSize: 12, fill: '#64748b' }} tickLine={false} axisLine={false} width={48} />
              <Tooltip content={<ChartTooltip fmt={usd} />} cursor={{ stroke: '#94a3b8', strokeDasharray: '4 4' }} />
              <Line dataKey="revenue" stroke={SERIES} strokeWidth={2} dot={false} activeDot={{ r: 5, stroke: '#fff', strokeWidth: 2 }} />
            </LineChart>
          </ResponsiveContainer>
        </div>
      </Card>

      <div className="mt-6 grid gap-6 xl:grid-cols-2">
        <Card>
          <h2 className="mb-4 font-semibold text-slate-900">Top categories by item sales</h2>
          <div className="h-80">
            <ResponsiveContainer>
              <BarChart data={cats} layout="vertical" margin={{ left: 8, right: 16 }}>
                <CartesianGrid stroke="#e2e8f0" horizontal={false} />
                <XAxis type="number" tickFormatter={compact} tick={{ fontSize: 12, fill: '#64748b' }} tickLine={false} axisLine={false} />
                <YAxis type="category" dataKey="label" width={130} tick={{ fontSize: 12, fill: '#334155' }} tickLine={false} axisLine={false} />
                <Tooltip content={<ChartTooltip fmt={usd} />} cursor={{ fill: '#f1f5f9' }} />
                <Bar dataKey="revenue" fill={SERIES} radius={[0, 4, 4, 0]} barSize={18} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </Card>
        <Card>
          <h2 className="mb-4 font-semibold text-slate-900">Orders by status</h2>
          <div className="h-80">
            <ResponsiveContainer>
              <BarChart data={d.orders_by_status} layout="vertical" margin={{ left: 8, right: 16 }}>
                <CartesianGrid stroke="#e2e8f0" horizontal={false} />
                <XAxis type="number" scale="sqrt" tickFormatter={compact} tick={{ fontSize: 12, fill: '#64748b' }} tickLine={false} axisLine={false} />
                <YAxis type="category" dataKey="status" width={90} tick={{ fontSize: 12, fill: '#334155' }} tickLine={false} axisLine={false} />
                <Tooltip content={<ChartTooltip fmt={(v) => `${v.toLocaleString()} orders`} />} cursor={{ fill: '#f1f5f9' }} />
                <Bar dataKey="orders" fill={SERIES} radius={[0, 4, 4, 0]} barSize={18} />
              </BarChart>
            </ResponsiveContainer>
          </div>
          <p className="mt-2 text-xs text-slate-400">Square-root scale so small statuses stay visible next to 96k delivered.</p>
        </Card>
      </div>
    </>
  )
}

export function AdminOrders() {
  const [params, setParams] = useSearchParams()
  const [data, setData] = useState<Awaited<ReturnType<typeof api.admin.orders>> | null>(null)
  const [form, setForm] = useState({
    order_id: params.get('order_id') ?? '',
    customer: params.get('customer') ?? '',
    status: params.get('status') ?? '',
    date_from: params.get('date_from') ?? '',
    date_to: params.get('date_to') ?? '',
  })
  useEffect(() => {
    setData(null)
    api.admin
      .orders({
        order_id: params.get('order_id') ?? undefined,
        customer: params.get('customer') ?? undefined,
        status: params.get('status') ? [params.get('status')!] : undefined,
        date_from: params.get('date_from') ?? undefined,
        date_to: params.get('date_to') ?? undefined,
      })
      .then(setData)
  }, [params])

  return (
    <>
      <h1 className="mb-6 text-3xl font-bold text-slate-900">Orders</h1>
      <form
        className="mb-6 grid gap-3 rounded-2xl border border-slate-200 bg-white p-4 md:grid-cols-6"
        onSubmit={(e) => {
          e.preventDefault()
          setParams(Object.fromEntries(Object.entries(form).filter(([, v]) => v)))
        }}
      >
        <input className={`${field} md:col-span-2`} placeholder="Order ID" value={form.order_id} onChange={(e) => setForm({ ...form, order_id: e.target.value })} />
        <input className={`${field} md:col-span-2`} placeholder="Customer unique ID" value={form.customer} onChange={(e) => setForm({ ...form, customer: e.target.value })} />
        <select className={field} value={form.status} onChange={(e) => setForm({ ...form, status: e.target.value })}>
          <option value="">Any status</option>
          {data?.statuses.map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
        <button className="flex items-center justify-center gap-2 rounded-xl bg-brand-500 px-4 py-2 text-sm font-semibold text-white hover:bg-brand-600"><Search size={16} /> Search</button>
        <label className="flex items-center gap-2 text-sm text-slate-500 md:col-span-3">From <input type="date" className={`${field} flex-1`} value={form.date_from} onChange={(e) => setForm({ ...form, date_from: e.target.value })} /></label>
        <label className="flex items-center gap-2 text-sm text-slate-500 md:col-span-3">To <input type="date" className={`${field} flex-1`} value={form.date_to} onChange={(e) => setForm({ ...form, date_to: e.target.value })} /></label>
      </form>
      {!data ? <Spinner /> : (
        <>
          <p className="mb-3 text-sm text-slate-500">{data.orders.length} orders shown (newest first, max 200)</p>
          <div className="space-y-2">
            {data.orders.map((o) => <OrderRow key={o.order_id} o={o} to={`/admin/orders/${o.order_id}`} showCustomer />)}
          </div>
        </>
      )}
    </>
  )
}

export function AdminOrder() {
  const { id = '' } = useParams()
  const [order, setOrder] = useState<OrderDetail | null>(null)
  const [missing, setMissing] = useState(false)
  useEffect(() => {
    api.admin.order(id).then(setOrder).catch(() => setMissing(true))
  }, [id])
  if (missing) return <EmptyState icon={<Package size={40} />} title="Order not found" />
  if (!order) return <Spinner />
  return (
    <>
      <Link to="/admin/orders" className="mb-4 inline-flex items-center gap-1 text-sm text-slate-500 hover:text-slate-800"><ArrowLeft size={14} /> All orders</Link>
      <OrderView order={order} admin />
    </>
  )
}

export function AdminCustomers() {
  const [params, setParams] = useSearchParams()
  const id = params.get('id') ?? ''
  const [lookup, setLookup] = useState(id)
  const [top, setTop] = useState<Awaited<ReturnType<typeof api.admin.topCustomers>> | null>(null)
  const [cust, setCust] = useState<Awaited<ReturnType<typeof api.admin.customer>> | null | 'missing'>(null)

  useEffect(() => {
    api.admin.topCustomers().then(setTop)
  }, [])
  useEffect(() => {
    setLookup(id)
    setCust(null)
    if (id) api.admin.customer(id).then(setCust).catch(() => setCust('missing'))
  }, [id])

  return (
    <>
      <h1 className="mb-6 text-3xl font-bold text-slate-900">Customers</h1>
      <form className="mb-6 flex gap-2" onSubmit={(e) => { e.preventDefault(); setParams(lookup.trim() ? { id: lookup.trim() } : {}) }}>
        <input className={`${field} flex-1`} placeholder="Customer unique ID" value={lookup} onChange={(e) => setLookup(e.target.value)} />
        <button className="rounded-xl bg-brand-500 px-5 py-2 text-sm font-semibold text-white hover:bg-brand-600">Look up</button>
      </form>

      {id && cust === null && <Spinner />}
      {cust === 'missing' && <p className="mb-6 rounded-xl bg-rose-50 px-4 py-3 text-rose-700">No customer with that ID.</p>}
      {cust && cust !== 'missing' && (
        <div className="mb-10">
          <div className="mb-4 grid gap-4 sm:grid-cols-4">
            <Stat label="Location" value={<span className="text-lg">{cust.profile.city}, {cust.profile.state}</span>} />
            <Stat label="Orders" value={cust.summary.orders} />
            <Stat label="Total paid" value={money(cust.summary.total_paid)} />
            <Stat label="Customer since" value={<span className="text-lg">{date(cust.profile.first_order)}</span>} />
          </div>
          {cust.address && (
            <Card className="mb-4">
              <h3 className="mb-2 text-sm font-semibold text-slate-500">Saved shipping address</h3>
              <AddressLines a={cust.address} />
            </Card>
          )}
          <div className="space-y-2">
            {cust.orders.map((o) => <OrderRow key={o.order_id} o={o} to={`/admin/orders/${o.order_id}`} />)}
          </div>
        </div>
      )}

      <h2 className="mb-3 text-xl font-semibold">Most frequent customers</h2>
      {!top ? <Spinner /> : (
        <div className="overflow-x-auto rounded-2xl border border-slate-200 bg-white">
          <table className="w-full text-sm">
            <thead className="bg-slate-50 text-left text-slate-500">
              <tr><th className="px-4 py-3">Customer</th><th className="px-4 py-3">Location</th><th className="px-4 py-3 text-right">Orders</th><th className="px-4 py-3 text-right">Total paid</th></tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {top.map((c) => (
                <tr key={c.customer_unique_id} className="hover:bg-slate-50">
                  <td className="px-4 py-2.5"><button className="font-mono text-brand-600 hover:underline" onClick={() => { setParams({ id: c.customer_unique_id }); window.scrollTo({ top: 0 }) }}>{c.customer_unique_id}</button></td>
                  <td className="px-4 py-2.5">{c.city}, {c.state}</td>
                  <td className="px-4 py-2.5 text-right">{c.orders}</td>
                  <td className="px-4 py-2.5 text-right">{money(c.total_paid)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  )
}

export function AdminSellers() {
  const [state, setState] = useState('')
  const [minOrders, setMinOrders] = useState(0)
  const [data, setData] = useState<Awaited<ReturnType<typeof api.admin.sellers>> | null>(null)
  useEffect(() => {
    api.admin.sellers({ state: state || undefined, min_orders: minOrders }).then(setData)
  }, [state, minOrders])
  return (
    <>
      <h1 className="mb-6 text-3xl font-bold text-slate-900">Sellers</h1>
      <div className="mb-4 flex flex-wrap items-center gap-4">
        <select className={field} value={state} onChange={(e) => setState(e.target.value)}>
          <option value="">All states</option>
          {data?.states.map((s) => <option key={s}>{s}</option>)}
        </select>
        <label className="flex items-center gap-2 text-sm text-slate-600">
          Min orders
          <select className={field} value={minOrders} onChange={(e) => setMinOrders(Number(e.target.value))}>
            {[0, 10, 50, 100, 500, 1000].map((n) => <option key={n} value={n}>{n}+</option>)}
          </select>
        </label>
        {data && <span className="text-sm text-slate-500">{data.sellers.length.toLocaleString()} sellers</span>}
      </div>
      {!data ? <Spinner /> : (
        <div className="overflow-x-auto rounded-2xl border border-slate-200 bg-white">
          <table className="w-full text-sm">
            <thead className="bg-slate-50 text-left text-slate-500">
              <tr><th className="px-4 py-3">Seller</th><th className="px-4 py-3">Location</th><th className="px-4 py-3 text-right">Orders</th><th className="px-4 py-3 text-right">Rating</th><th className="px-4 py-3">On time</th></tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {data.sellers.slice(0, 300).map((s) => (
                <tr key={s.seller_id} className="hover:bg-slate-50">
                  <td className="px-4 py-2.5 font-mono text-xs">{s.seller_id}</td>
                  <td className="px-4 py-2.5">{s.seller_city}, {s.seller_state}</td>
                  <td className="px-4 py-2.5 text-right">{s.orders_fulfilled.toLocaleString()}</td>
                  <td className="px-4 py-2.5 text-right">{s.avg_rating?.toFixed(2) ?? '—'} ★</td>
                  <td className="px-4 py-2.5">
                    <div className="flex items-center gap-2">
                      <div className="h-2 w-24 overflow-hidden rounded-full bg-slate-100">
                        <div className="h-full rounded-full" style={{ width: `${s.on_time_pct ?? 0}%`, background: SERIES }} />
                      </div>
                      <span className="text-xs text-slate-600">{s.on_time_pct?.toFixed(1) ?? '—'}%</span>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {data.sellers.length > 300 && <p className="px-4 py-3 text-xs text-slate-400">Showing the first 300 — narrow with the filters.</p>}
        </div>
      )}
    </>
  )
}

export function AdminUsers() {
  const { notify } = useStore()
  const [role, setRole] = useState('')
  const [search, setSearch] = useState('')
  const [users, setUsers] = useState<Awaited<ReturnType<typeof api.admin.users>> | null>(null)
  const [resetName, setResetName] = useState('')
  const [tempPw, setTempPw] = useState('')
  const [newAdmin, setNewAdmin] = useState({ username: '', password: '' })

  const load = () => api.admin.users({ role: role || undefined, search: search || undefined }).then(setUsers)
  useEffect(() => {
    load()
  }, [role]) // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <>
      <h1 className="mb-6 text-3xl font-bold text-slate-900">User accounts</h1>
      <form className="mb-4 flex flex-wrap gap-2" onSubmit={(e) => { e.preventDefault(); load() }}>
        <select className={field} value={role} onChange={(e) => setRole(e.target.value)}>
          <option value="">All roles</option><option value="customer">Customers</option><option value="admin">Admins</option>
        </select>
        <input className={`${field} flex-1`} placeholder="Username starts with…" value={search} onChange={(e) => setSearch(e.target.value)} />
        <button className="rounded-xl bg-brand-500 px-5 py-2 text-sm font-semibold text-white">Search</button>
      </form>
      {!users ? <Spinner /> : (
        <div className="mb-8 overflow-x-auto rounded-2xl border border-slate-200 bg-white">
          <table className="w-full text-sm">
            <thead className="bg-slate-50 text-left text-slate-500">
              <tr><th className="px-4 py-3">Username</th><th className="px-4 py-3">Role</th><th className="px-4 py-3">Temp password</th><th className="px-4 py-3">Last login</th></tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {users.map((u) => (
                <tr key={u.user_id}>
                  <td className="px-4 py-2.5 font-mono text-xs">{u.username}</td>
                  <td className="px-4 py-2.5 capitalize">{u.role}</td>
                  <td className="px-4 py-2.5">{u.must_change_password ? 'Yes' : 'No'}</td>
                  <td className="px-4 py-2.5">{u.last_login ? new Date(u.last_login).toLocaleString() : 'Never'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="grid gap-6 lg:grid-cols-2">
        <Card>
          <h2 className="mb-3 font-semibold">Reset a password</h2>
          <form className="flex gap-2" onSubmit={async (e) => {
            e.preventDefault()
            try {
              setTempPw((await api.admin.resetPassword(resetName.trim())).temporary_password)
            } catch (err) {
              setTempPw('')
              notify(err instanceof ApiError ? err.message : 'Reset failed', 'error')
            }
          }}>
            <input className={`${field} flex-1`} placeholder="Username" value={resetName} onChange={(e) => setResetName(e.target.value)} required />
            <button className="rounded-xl bg-slate-900 px-4 py-2 text-sm font-semibold text-white">Reset</button>
          </form>
          {tempPw && <p className="mt-3 rounded-lg bg-emerald-50 px-3 py-2 text-sm text-emerald-800">Temporary password: <code className="font-bold">{tempPw}</code> — they must change it at next sign-in.</p>}
        </Card>
        <Card>
          <h2 className="mb-3 font-semibold">Create an admin</h2>
          <form className="space-y-2" onSubmit={async (e) => {
            e.preventDefault()
            try {
              await api.admin.createAdmin(newAdmin.username.trim(), newAdmin.password)
              notify(`Admin ${newAdmin.username} created`)
              setNewAdmin({ username: '', password: '' })
              load()
            } catch (err) {
              notify(err instanceof ApiError ? err.message : 'Could not create admin', 'error')
            }
          }}>
            <input className={`${field} w-full`} placeholder="Username" value={newAdmin.username} onChange={(e) => setNewAdmin({ ...newAdmin, username: e.target.value })} required minLength={3} />
            <input className={`${field} w-full`} type="password" placeholder="Password (min 8)" value={newAdmin.password} onChange={(e) => setNewAdmin({ ...newAdmin, password: e.target.value })} required minLength={8} />
            <button className="rounded-xl bg-slate-900 px-4 py-2 text-sm font-semibold text-white">Create admin</button>
          </form>
        </Card>
      </div>
    </>
  )
}

export function AdminTickets() {
  const [tickets, setTickets] = useState<Record<string, unknown>[] | null>(null)
  useEffect(() => {
    api.admin.tickets().then(setTickets)
  }, [])
  if (!tickets) return <Spinner />
  return (
    <>
      <h1 className="mb-6 text-3xl font-bold text-slate-900">Support tickets</h1>
      {tickets.length === 0 ? (
        <EmptyState icon={<LifeBuoy size={40} />} title="No tickets yet">The AI support agent will create tickets here when customers report problems.</EmptyState>
      ) : (
        <div className="overflow-x-auto rounded-2xl border border-slate-200 bg-white">
          <table className="w-full text-sm">
            <thead className="bg-slate-50 text-left text-slate-500">
              <tr>{Object.keys(tickets[0]).map((k) => <th key={k} className="px-4 py-3">{k}</th>)}</tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {tickets.map((t, i) => <tr key={i}>{Object.values(t).map((v, j) => <td key={j} className="px-4 py-2.5">{String(v ?? '')}</td>)}</tr>)}
            </tbody>
          </table>
        </div>
      )}
    </>
  )
}

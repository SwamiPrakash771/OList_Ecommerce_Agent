// Seller Center: a seller's own sales, products (editable) and orders.
// Every API call here is limited to the signed-in seller on the server.

import { ArrowLeft, BarChart3, Home, Loader2, LogOut, Package, Plus, Sparkles, Store, Tag, User } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link, NavLink, Outlet, useNavigate, useParams } from 'react-router-dom'
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { api, ApiError, type SellerOrder } from '../api'
import { ChatWidget } from '../components/Chat'
import { Toasts } from '../components/Layout'
import ProductImage from '../components/ProductImage'
import ServiceBanner from '../components/ServiceBanner'
import TempPasswordBanner from '../components/TempPasswordBanner'
import { Card, EmptyState, Spinner, Stat, StatusBadge } from '../components/ui'
import { categoryLabel, date, money } from '../lib'
import { useStore } from '../state'
import { ProductDetailView, ProductListView } from './AdminProducts'

const SERIES = '#2a78d6' // categorical slot 1 of the reference palette

export function SellerLayout() {
  const { user, logout } = useStore()
  const navigate = useNavigate()
  const links = [
    ['/', 'Home', Home],
    ['/seller', 'Dashboard', BarChart3],
    ['/seller/assistant', 'AI Assistant', Sparkles],
    ['/seller/products', 'My products', Tag],
    ['/seller/orders', 'Orders', Package],
    ['/seller/account', 'Account', User],
  ] as const
  const where = user?.profile?.city ? `${user.profile.city.replace(/\b\w/g, (c) => c.toUpperCase())}, ${user.profile.state}` : ''
  return (
    <div className="flex min-h-screen">
      <aside className="sticky top-0 flex h-screen w-60 shrink-0 flex-col bg-emerald-950 text-emerald-100/80">
        <Link to="/seller" className="px-5 pt-5 text-lg font-extrabold text-white">
          <span className="flex items-center gap-2">
            <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-emerald-500"><Store size={16} /></span>
            Seller Center
          </span>
        </Link>
        <div className="px-5 pb-4 pt-1 text-xs text-emerald-100/60">
          {where}
          <div className="truncate font-mono">{user?.username.slice(0, 12)}…</div>
        </div>
        <nav className="flex-1 space-y-1 px-3">
          {links.map(([to, label, Icon]) => (
            <NavLink
              key={to}
              to={to}
              end={to === '/seller' || to === '/'}
              className={({ isActive }) => `flex items-center gap-3 rounded-lg px-3 py-2 text-sm ${isActive ? 'bg-white/10 font-semibold text-white' : 'hover:bg-white/5 hover:text-white'}`}
            >
              <Icon size={18} /> {label}
            </NavLink>
          ))}
        </nav>
        <div className="space-y-1 border-t border-white/10 p-3 text-sm">
          <button onClick={async () => { await logout(); navigate('/') }} className="flex w-full items-center gap-3 rounded-lg px-3 py-2 hover:bg-white/5 hover:text-white">
            <LogOut size={18} /> Sign out
          </button>
        </div>
      </aside>
      <main className="min-w-0 flex-1 bg-slate-50 p-8">
        <ServiceBanner className="mb-6 rounded-xl border" />
        <TempPasswordBanner accountPath="/seller/account" className="mb-6 rounded-xl" />
        <Outlet />
      </main>
      <Toasts />
      <ChatWidget />
    </div>
  )
}

export function SellerDashboard() {
  const [d, setD] = useState<Awaited<ReturnType<typeof api.seller.dashboard>> | null>(null)
  useEffect(() => {
    api.seller.dashboard().then(setD)
  }, [])
  if (!d) return <Spinner />
  const monthly = d.monthly_sales.map((m) => ({
    ...m,
    label: new Date(m.month).toLocaleDateString('en-GB', { month: 'short', year: '2-digit', timeZone: 'UTC' }),
  }))
  const compact = (v: number) => (v >= 1e3 ? `${(v / 1e3).toFixed(0)}k` : String(v))

  return (
    <>
      <h1 className="mb-6 text-3xl font-bold text-slate-900">Dashboard</h1>
      <div className="grid grid-cols-2 gap-4 md:grid-cols-3 xl:grid-cols-6">
        <Stat label="Sales" value={money(d.kpis.revenue)} sub="item prices, all time" />
        <Stat label="Orders" value={d.kpis.orders.toLocaleString()} />
        <Stat label="Items sold" value={d.kpis.items_sold.toLocaleString()} />
        <Stat label="Products" value={d.kpis.products.toLocaleString()} sub="you are the main seller" />
        <Stat label="Avg rating" value={d.profile.avg_rating ? `${d.profile.avg_rating.toFixed(2)} ★` : '—'} />
        <Stat label="On-time delivery" value={d.profile.on_time_pct ? `${d.profile.on_time_pct.toFixed(1)}%` : '—'} />
      </div>

      <div className="mt-6 grid gap-6 xl:grid-cols-3">
        <Card className="xl:col-span-2">
          <h2 className="font-semibold text-slate-900">Monthly sales</h2>
          <p className="mb-4 text-sm text-slate-500">Your item sales on non-canceled orders</p>
          {monthly.length === 0 ? <p className="text-sm text-slate-500">No sales yet.</p> : (
            <div className="h-72">
              <ResponsiveContainer>
                <LineChart data={monthly} margin={{ left: 8, right: 16, top: 8 }}>
                  <CartesianGrid stroke="#e2e8f0" vertical={false} />
                  <XAxis dataKey="label" tick={{ fontSize: 12, fill: '#64748b' }} tickLine={false} axisLine={{ stroke: '#cbd5e1' }} />
                  <YAxis tickFormatter={compact} tick={{ fontSize: 12, fill: '#64748b' }} tickLine={false} axisLine={false} width={44} />
                  <Tooltip
                    formatter={(v: number) => [money(v), 'Sales']}
                    contentStyle={{ borderRadius: 8, border: '1px solid #e2e8f0', fontSize: 13 }}
                    cursor={{ stroke: '#94a3b8', strokeDasharray: '4 4' }}
                  />
                  <Line dataKey="revenue" stroke={SERIES} strokeWidth={2} dot={false} activeDot={{ r: 5, stroke: '#fff', strokeWidth: 2 }} />
                </LineChart>
              </ResponsiveContainer>
            </div>
          )}
        </Card>
        <Card>
          <h2 className="mb-3 font-semibold text-slate-900">Best-selling products</h2>
          <ul className="space-y-3">
            {d.top_products.map((p) => (
              <li key={p.product_id}>
                <Link to={`/seller/products/${p.product_id}`} className="flex items-center gap-3 rounded-lg p-1 hover:bg-slate-50">
                  <ProductImage productId={p.product_id} category={p.category} className="h-10 w-10 shrink-0 rounded-lg" iconSize={18} />
                  <div className="min-w-0 flex-1">
                    <div className="truncate text-sm font-medium text-slate-800">{p.name}</div>
                    <div className="text-xs text-slate-500">{categoryLabel(p.category)} · {p.rating?.toFixed(1) ?? '—'} ★</div>
                  </div>
                  <span className="text-sm font-semibold text-slate-700">{p.sold} sold</span>
                </Link>
              </li>
            ))}
          </ul>
        </Card>
      </div>
    </>
  )
}

export function SellerProducts() {
  return (
    <ProductListView
      title="My products"
      subtitle="Products where you are the main seller. Changes show in the store immediately."
      action="edit"
      showCategoryFilter={false}
      headerAction={
        <Link to="/seller/products/new" className="flex items-center gap-2 rounded-xl bg-emerald-600 px-4 py-2.5 text-sm font-semibold text-white hover:bg-emerald-700">
          <Plus size={16} /> Add product
        </Link>
      }
      detailPath={(id) => `/seller/products/${id}`}
      load={(q) => api.seller.products({ q: q.q, sort: q.sort, page: q.page, page_size: 25 })}
    />
  )
}

export function SellerProductEdit() {
  const { id = '' } = useParams()
  return (
    <ProductDetailView
      key={id}
      load={() => api.seller.product(id)}
      save={(changes) => api.seller.updateProduct(id, changes)}
      backTo="/seller/products"
      backLabel="My products"
    />
  )
}

export function SellerOrders() {
  const [status, setStatus] = useState('')
  const [orders, setOrders] = useState<SellerOrder[] | null>(null)
  useEffect(() => {
    setOrders(null)
    api.seller.orders(status || undefined).then((r) => setOrders(r.orders))
  }, [status])
  const statuses = ['', 'approved', 'invoiced', 'processing', 'shipped', 'delivered', 'canceled', 'unavailable']

  return (
    <>
      <h1 className="text-3xl font-bold text-slate-900">Orders</h1>
      <p className="mb-4 mt-1 text-sm text-slate-500">Orders that include your items. Totals cover only your items. Newest first, up to 200.</p>
      <div className="mb-4 flex flex-wrap gap-2">
        {statuses.map((s) => (
          <button
            key={s || 'all'}
            onClick={() => setStatus(s)}
            className={`rounded-full px-4 py-1.5 text-sm font-medium capitalize ${status === s ? 'bg-slate-900 text-white' : 'bg-white text-slate-600 ring-1 ring-slate-200 hover:bg-slate-50'}`}
          >
            {s || 'All'}
          </button>
        ))}
      </div>
      {!orders ? <Spinner /> : orders.length === 0 ? (
        <EmptyState icon={<Package size={40} />} title="No orders with this status" />
      ) : (
        <div className="overflow-x-auto rounded-2xl border border-slate-200 bg-white">
          <table className="w-full text-sm">
            <thead className="bg-slate-50 text-left text-slate-500">
              <tr>
                <th className="px-4 py-3">Order</th>
                <th className="px-4 py-3">Placed</th>
                <th className="px-4 py-3">Status</th>
                <th className="px-4 py-3">Ship to</th>
                <th className="px-4 py-3 text-right">Your items</th>
                <th className="px-4 py-3 text-right">Your total</th>
                <th className="px-4 py-3">Ship by</th>
                <th className="px-4 py-3">Delivered</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {orders.map((o) => (
                <tr key={o.order_id} className="hover:bg-slate-50">
                  <td className="px-4 py-2.5 font-mono text-xs">{o.order_id.slice(0, 12)}…</td>
                  <td className="px-4 py-2.5">{date(o.purchased_at)}</td>
                  <td className="px-4 py-2.5"><StatusBadge status={o.status} /></td>
                  <td className="px-4 py-2.5 capitalize">{o.customer_city}, {o.customer_state}</td>
                  <td className="px-4 py-2.5 text-right">{o.items}</td>
                  <td className="px-4 py-2.5 text-right font-medium">{money(o.order_total)}</td>
                  <td className="px-4 py-2.5">{date(o.ship_by)}</td>
                  <td className="px-4 py-2.5">{date(o.delivered_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  )
}

const NEW_FIELD = 'w-full rounded-xl border border-slate-200 bg-white px-3 py-2 text-sm outline-none focus:border-emerald-500 focus:ring-4 focus:ring-emerald-100'

export function SellerNewProduct() {
  const navigate = useNavigate()
  const { notify } = useStore()
  const [cats, setCats] = useState<string[]>([])
  const [form, setForm] = useState({
    name: '', category: '', price: '', shipping: '', weight_g: '', length_cm: '', width_cm: '', height_cm: '',
  })
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)
  useEffect(() => {
    api.categories().then((c) => setCats(c.map((x) => x.category).filter((x) => x !== 'uncategorized')))
  }, [])
  const set = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
    setForm({ ...form, [k]: e.target.value })

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    setSaving(true)
    setError('')
    const num = (v: string) => (v === '' ? undefined : Number(v))
    try {
      const created = await api.seller.createProduct({
        name: form.name.trim(),
        category: form.category,
        price: Number(form.price),
        shipping: Number(form.shipping),
        weight_g: num(form.weight_g),
        length_cm: num(form.length_cm),
        width_cm: num(form.width_cm),
        height_cm: num(form.height_cm),
      })
      notify('Product added to the store')
      navigate(`/seller/products/${created.product.product_id}`, { replace: true })
    } catch (err) {
      if (err instanceof ApiError && err.status === 422 && err.message.startsWith('[')) {
        const list = JSON.parse(err.message) as { loc: string[]; msg: string }[]
        setError(list.map((x) => `${x.loc[x.loc.length - 1]}: ${x.msg}`).join(' · '))
      } else {
        setError(err instanceof ApiError ? err.message : 'Could not add the product')
      }
      setSaving(false)
    }
  }

  const input = (k: keyof typeof form, label: string, props: React.InputHTMLAttributes<HTMLInputElement> = {}) => (
    <label className="block">
      <span className="mb-1 block text-xs font-medium text-slate-600">{label}</span>
      <input className={NEW_FIELD} value={form[k]} onChange={set(k)} {...props} />
    </label>
  )

  return (
    <>
      <Link to="/seller/products" className="mb-4 inline-flex items-center gap-1 text-sm text-slate-500 hover:text-slate-800">
        <ArrowLeft size={14} /> My products
      </Link>
      <h1 className="text-3xl font-bold text-slate-900">Add a product</h1>
      <p className="mb-6 mt-1 text-sm text-slate-500">It appears in the store as soon as you save it, listed under your seller account.</p>

      <div className="grid gap-6 xl:grid-cols-3">
        <Card className="xl:col-span-2">
          <form onSubmit={submit} className="space-y-5">
            <div className="grid gap-4 sm:grid-cols-2">
              <div className="sm:col-span-2">{input('name', 'Product name *', { required: true, minLength: 3, maxLength: 100, placeholder: 'e.g. Stainless Steel Watch 40mm' })}</div>
              <label className="block">
                <span className="mb-1 block text-xs font-medium text-slate-600">Category *</span>
                <select className={NEW_FIELD} value={form.category} onChange={set('category')} required>
                  <option value="">Choose a category</option>
                  {cats.map((c) => <option key={c} value={c}>{categoryLabel(c)}</option>)}
                </select>
              </label>
              <div className="grid grid-cols-2 gap-4">
                {input('price', 'Price ($) *', { type: 'number', step: '0.01', min: '0.01', required: true })}
                {input('shipping', 'Shipping ($) *', { type: 'number', step: '0.01', min: '0', required: true })}
              </div>
            </div>
            <div>
              <h3 className="mb-2 text-sm font-semibold text-slate-700">Package (optional)</h3>
              <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
                {input('weight_g', 'Weight (g)', { type: 'number', min: '0', step: '1' })}
                {input('length_cm', 'Length (cm)', { type: 'number', min: '0', step: '1' })}
                {input('width_cm', 'Width (cm)', { type: 'number', min: '0', step: '1' })}
                {input('height_cm', 'Height (cm)', { type: 'number', min: '0', step: '1' })}
              </div>
            </div>
            {error && <p className="rounded-lg bg-rose-50 px-3 py-2 text-sm text-rose-700" role="alert">{error}</p>}
            <div className="flex items-center gap-3 border-t border-slate-100 pt-4">
              <button disabled={saving} className="flex items-center gap-2 rounded-xl bg-emerald-600 px-5 py-2.5 text-sm font-semibold text-white hover:bg-emerald-700 disabled:opacity-50">
                {saving ? <Loader2 size={16} className="animate-spin" /> : <Plus size={16} />} Add product
              </button>
              <Link to="/seller/products" className="rounded-xl px-4 py-2.5 text-sm font-medium text-slate-600 hover:bg-slate-100">Cancel</Link>
            </div>
          </form>
        </Card>
        <Card>
          <h2 className="mb-3 font-semibold">Preview</h2>
          <div className="overflow-hidden rounded-2xl border border-slate-200">
            <ProductImage productId={form.name || 'new'} category={form.category || 'item'} className="aspect-square" iconSize={64} />
            <div className="p-4">
              <div className="text-xs font-medium uppercase tracking-wide text-slate-400">{form.category ? categoryLabel(form.category) : 'Category'}</div>
              <div className="mt-1 font-medium text-slate-800">{form.name || 'Product name'}</div>
              <div className="mt-2 text-xl font-bold text-slate-900">{form.price ? money(Number(form.price)) : '$0.00'}</div>
              <div className="text-xs text-slate-500">+ {form.shipping ? money(Number(form.shipping)) : '$0.00'} shipping · new listing</div>
            </div>
          </div>
        </Card>
      </div>
    </>
  )
}

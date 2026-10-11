// Product list + product detail, shared by the admin panel (read-only) and the
// seller center (sellers edit their own products).

import { ArrowLeft, Eye, History, Loader2, Lock, Package, Pencil, Save, Search } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { api, ApiError, type EditableProduct, type ProductEditResponse, type ProductPage } from '../api'
import ProductImage from '../components/ProductImage'
import { Card, EmptyState, Pagination, Spinner } from '../components/ui'
import { categoryLabel, money } from '../lib'
import { useStore } from '../state'

const field = 'rounded-xl border border-slate-200 bg-white px-3 py-2 text-sm outline-none focus:border-brand-500 focus:ring-4 focus:ring-brand-100 disabled:bg-slate-50 disabled:text-slate-600'
const PAGE_SIZE = 25

type ListQuery = { q: string; category: string; sort: string; page: number }

export function ProductListView({ title, subtitle, load, detailPath, action, showCategoryFilter = true, headerAction }: {
  title: string
  subtitle?: string
  load: (q: ListQuery) => Promise<ProductPage>
  detailPath: (id: string) => string
  action: 'view' | 'edit'
  showCategoryFilter?: boolean
  headerAction?: React.ReactNode
}) {
  const [params, setParams] = useSearchParams()
  const [cats, setCats] = useState<string[]>([])
  const [q, setQ] = useState(params.get('q') ?? '')
  const [data, setData] = useState<ProductPage | null>(null)
  const page = Number(params.get('page') ?? 1)
  const category = params.get('category') ?? ''
  const sort = params.get('sort') ?? 'popular'

  useEffect(() => {
    if (showCategoryFilter) api.categories().then((c) => setCats(c.map((x) => x.category)))
  }, [showCategoryFilter])
  useEffect(() => {
    setData(null)
    load({ q: params.get('q') ?? '', category, sort, page }).then(setData)
  }, [params]) // eslint-disable-line react-hooks/exhaustive-deps

  const update = (changes: Record<string, string>) => {
    const next = new URLSearchParams(params)
    Object.entries(changes).forEach(([k, v]) => (v ? next.set(k, v) : next.delete(k)))
    if (!('page' in changes)) next.delete('page')
    setParams(next)
  }

  return (
    <>
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-3xl font-bold text-slate-900">{title}</h1>
          {subtitle && <p className="mt-1 text-sm text-slate-500">{subtitle}</p>}
        </div>
        {headerAction}
      </div>
      <form className="mb-4 mt-6 flex flex-wrap gap-2" onSubmit={(e) => { e.preventDefault(); update({ q: q.trim() }) }}>
        <input
          className={`${field} min-w-60 flex-1`}
          placeholder="Search by name, category or product code (e.g. 53b36d)"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
        {showCategoryFilter && (
          <select className={field} value={category} onChange={(e) => update({ category: e.target.value })}>
            <option value="">All categories</option>
            {cats.map((c) => <option key={c} value={c}>{categoryLabel(c)}</option>)}
          </select>
        )}
        <select className={field} value={sort} onChange={(e) => update({ sort: e.target.value })}>
          <option value="popular">Most sold</option>
          <option value="rating">Top rated</option>
          <option value="price_low">Price: low → high</option>
          <option value="price_high">Price: high → low</option>
        </select>
        <button className="flex items-center gap-2 rounded-xl bg-brand-500 px-4 py-2 text-sm font-semibold text-white hover:bg-brand-600">
          <Search size={16} /> Search
        </button>
      </form>

      {!data ? <Spinner /> : data.items.length === 0 ? (
        <EmptyState icon={<Package size={40} />} title="No products found" />
      ) : (
        <>
          <p className="mb-2 text-sm text-slate-500">{data.total.toLocaleString()} products</p>
          <div className="overflow-x-auto rounded-2xl border border-slate-200 bg-white">
            <table className="w-full text-sm">
              <thead className="bg-slate-50 text-left text-slate-500">
                <tr>
                  <th className="px-4 py-3">Product</th>
                  <th className="px-4 py-3">Category</th>
                  <th className="px-4 py-3 text-right">Price</th>
                  <th className="px-4 py-3 text-right">Shipping</th>
                  <th className="px-4 py-3 text-right">Rating</th>
                  <th className="px-4 py-3 text-right">Sold</th>
                  <th className="px-4 py-3" />
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {data.items.map((p) => (
                  <tr key={p.product_id} className="hover:bg-slate-50">
                    <td className="px-4 py-2">
                      <div className="flex items-center gap-3">
                        <ProductImage productId={p.product_id} category={p.category} className="h-10 w-10 shrink-0 rounded-lg" iconSize={18} />
                        <div className="min-w-0">
                          <div className="truncate font-medium text-slate-800">{p.name}</div>
                          <div className="font-mono text-[11px] text-slate-400">{p.product_id}</div>
                        </div>
                      </div>
                    </td>
                    <td className="px-4 py-2 text-slate-600">{categoryLabel(p.category)}</td>
                    <td className="px-4 py-2 text-right font-medium">{money(p.price)}</td>
                    <td className="px-4 py-2 text-right text-slate-600">{money(p.shipping)}</td>
                    <td className="px-4 py-2 text-right">
                      {p.rating?.toFixed(1) ?? '—'} ★ <span className="text-slate-400">({p.reviews})</span>
                    </td>
                    <td className="px-4 py-2 text-right">{p.sold}</td>
                    <td className="px-4 py-2 text-right">
                      <Link to={detailPath(p.product_id)} className="inline-flex items-center gap-1 rounded-lg px-3 py-1.5 font-medium text-brand-600 hover:bg-brand-50">
                        {action === 'edit' ? <><Pencil size={14} /> Edit</> : <><Eye size={14} /> View</>}
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <Pagination page={page} total={data.total} pageSize={PAGE_SIZE} onPage={(p) => update({ page: String(p) })} />
        </>
      )}
    </>
  )
}

const FIELD_LABELS: Record<string, string> = {
  name: 'Name',
  category: 'Category',
  price: 'Price',
  shipping: 'Shipping',
  weight_g: 'Weight (g)',
  length_cm: 'Length (cm)',
  height_cm: 'Height (cm)',
  width_cm: 'Width (cm)',
}
const NUMERIC = ['price', 'shipping', 'weight_g', 'length_cm', 'height_cm', 'width_cm']

function showValue(f: string, v: unknown) {
  if (v === null || v === undefined || v === '') return '—'
  if (f === 'price' || f === 'shipping') return money(Number(v))
  if (f === 'category') return categoryLabel(String(v))
  return String(v)
}

/** Product details form. Pass `save` to make it editable; without it the form is read-only. */
export function ProductDetailView({ load, save, backTo, backLabel, readOnlyNote, showSeller = false }: {
  load: () => Promise<ProductEditResponse>
  save?: (changes: Partial<EditableProduct>) => Promise<ProductEditResponse>
  backTo: string
  backLabel: string
  readOnlyNote?: string
  showSeller?: boolean
}) {
  const { notify } = useStore()
  const [data, setData] = useState<ProductEditResponse | null>(null)
  const [missing, setMissing] = useState(false)
  const [form, setForm] = useState<Record<string, string>>({})
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const editable = Boolean(save)

  const apply = (d: ProductEditResponse) => {
    setData(d)
    const p = d.product
    setForm({
      name: p.name,
      category: p.category,
      price: String(p.price),
      shipping: String(p.shipping),
      weight_g: p.weight_g?.toString() ?? '',
      length_cm: p.length_cm?.toString() ?? '',
      height_cm: p.height_cm?.toString() ?? '',
      width_cm: p.width_cm?.toString() ?? '',
    })
  }
  useEffect(() => {
    load().then(apply).catch(() => setMissing(true))
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  if (missing) return <EmptyState icon={<Package size={40} />} title="Product not found" />
  if (!data) return <Spinner />
  const p = data.product

  // Only fields that differ from the saved product are sent.
  const changes: Record<string, string | number> = {}
  for (const [k, v] of Object.entries(form)) {
    const original = p[k as keyof EditableProduct]
    if (NUMERIC.includes(k)) {
      if (v !== '' && Number(v) !== original) changes[k] = Number(v)
    } else if (v.trim() !== original) {
      changes[k] = v.trim()
    }
  }
  const changeCount = Object.keys(changes).length

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!save) return
    setSaving(true)
    setError('')
    try {
      apply(await save(changes as Partial<EditableProduct>))
      notify('Product updated')
    } catch (err) {
      if (err instanceof ApiError && err.status === 422 && err.message.startsWith('[')) {
        const list = JSON.parse(err.message) as { loc: string[]; msg: string }[]
        setError(list.map((x) => `${FIELD_LABELS[x.loc[x.loc.length - 1]] ?? x.loc[x.loc.length - 1]}: ${x.msg}`).join(' · '))
      } else {
        setError(err instanceof ApiError ? err.message : 'Could not save')
      }
    } finally {
      setSaving(false)
    }
  }

  const input = (k: string, props: React.InputHTMLAttributes<HTMLInputElement> = {}) => (
    <label className="block">
      <span className="mb-1 block text-xs font-medium text-slate-600">{FIELD_LABELS[k]}</span>
      <input
        className={`${field} w-full`}
        value={form[k] ?? ''}
        onChange={(e) => setForm({ ...form, [k]: e.target.value })}
        disabled={!editable}
        {...props}
      />
    </label>
  )

  return (
    <>
      <Link to={backTo} className="mb-4 inline-flex items-center gap-1 text-sm text-slate-500 hover:text-slate-800">
        <ArrowLeft size={14} /> {backLabel}
      </Link>
      <div className="mb-6 flex flex-wrap items-center gap-4">
        <ProductImage productId={p.product_id} category={form.category || p.category} className="h-16 w-16 rounded-xl" iconSize={28} />
        <div className="min-w-0 flex-1">
          <h1 className="truncate text-2xl font-bold text-slate-900">{p.name}</h1>
          <div className="font-mono text-xs text-slate-400">{p.product_id}</div>
          <div className="text-sm text-slate-500">
            {p.times_sold} sold · {p.avg_rating?.toFixed(1) ?? '—'} ★ ({p.num_reviews} reviews)
            {showSeller && <> · seller <span className="font-mono">{p.seller_id}</span></>}
          </div>
        </div>
      </div>

      {!editable && readOnlyNote && (
        <div className="mb-4 flex items-center gap-2 rounded-xl border border-slate-200 bg-slate-100 px-4 py-3 text-sm text-slate-600">
          <Lock size={16} /> {readOnlyNote}
        </div>
      )}

      <div className="grid gap-6 xl:grid-cols-3">
        <Card className="xl:col-span-2">
          <form onSubmit={submit} className="space-y-5">
            <div className="grid gap-4 sm:grid-cols-2">
              <div className="sm:col-span-2">{input('name', { required: true, minLength: 3, maxLength: 100 })}</div>
              <label className="block">
                <span className="mb-1 block text-xs font-medium text-slate-600">Category</span>
                <select
                  className={`${field} w-full`}
                  value={form.category}
                  onChange={(e) => setForm({ ...form, category: e.target.value })}
                  disabled={!editable}
                >
                  {data.categories.map((c) => <option key={c} value={c}>{categoryLabel(c)}</option>)}
                </select>
              </label>
              <div className="grid grid-cols-2 gap-4">
                {input('price', { type: 'number', step: '0.01', min: '0.01', required: true })}
                {input('shipping', { type: 'number', step: '0.01', min: '0', required: true })}
              </div>
            </div>
            <div>
              <h3 className="mb-2 text-sm font-semibold text-slate-700">Package</h3>
              <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
                {input('weight_g', { type: 'number', min: '0', step: '1' })}
                {input('length_cm', { type: 'number', min: '0', step: '1' })}
                {input('width_cm', { type: 'number', min: '0', step: '1' })}
                {input('height_cm', { type: 'number', min: '0', step: '1' })}
              </div>
            </div>
            {editable && (
              <>
                {error && <p className="rounded-lg bg-rose-50 px-3 py-2 text-sm text-rose-700" role="alert">{error}</p>}
                <div className="flex flex-wrap items-center gap-3 border-t border-slate-100 pt-4">
                  <button
                    disabled={changeCount === 0 || saving}
                    className="flex items-center gap-2 rounded-xl bg-brand-500 px-5 py-2.5 text-sm font-semibold text-white hover:bg-brand-600 disabled:opacity-40"
                  >
                    {saving ? <Loader2 size={16} className="animate-spin" /> : <Save size={16} />} Save changes
                  </button>
                  {changeCount > 0 && (
                    <button type="button" onClick={() => apply(data)} className="rounded-xl px-4 py-2.5 text-sm font-medium text-slate-600 hover:bg-slate-100">
                      Discard
                    </button>
                  )}
                  <span className="text-xs text-slate-500">
                    {changeCount ? `${changeCount} unsaved change${changeCount > 1 ? 's' : ''}` : 'No changes'} · price changes apply to new orders only
                  </span>
                </div>
              </>
            )}
          </form>
        </Card>

        <div className="space-y-6">
        <Card>
          <h2 className="mb-3 font-semibold">How customers see it</h2>
          <div className="overflow-hidden rounded-2xl border border-slate-200">
            <ProductImage productId={p.product_id} category={form.category || p.category} className="aspect-[4/3]" iconSize={56} />
            <div className="p-4">
              <div className="text-xs font-medium uppercase tracking-wide text-slate-400">{categoryLabel(form.category || p.category)}</div>
              <div className="mt-1 font-medium text-slate-800">{form.name || p.name}</div>
              <div className="mt-1 text-sm text-slate-500">{p.avg_rating?.toFixed(1) ?? '—'} ★ ({p.num_reviews}) · {p.times_sold} sold</div>
              <div className="mt-2 text-xl font-bold text-slate-900">{money(Number(form.price || p.price))}</div>
              <div className="text-xs text-slate-500">+ {money(Number(form.shipping || p.shipping))} shipping</div>
            </div>
          </div>
        </Card>
        <Card>
          <h2 className="mb-3 flex items-center gap-2 font-semibold"><History size={18} /> Edit history</h2>
          {data.history.length === 0 ? (
            <p className="text-sm text-slate-500">No edits yet. This product still has its original Olist data.</p>
          ) : (
            <ol className="space-y-4">
              {data.history.map((h, i) => (
                <li key={i} className="text-sm">
                  <div className="text-xs text-slate-500">
                    {new Date(h.edited_at).toLocaleString()} · <span className="capitalize">{h.editor_role}</span> {h.edited_by.slice(0, 8)}
                  </div>
                  {'created' in h.changes && <div className="mt-1 font-medium text-emerald-700">Product created</div>}
                  <ul className="mt-1 space-y-0.5">
                    {Object.entries(h.changes).filter(([f]) => f !== 'created').map(([f, c]) => (
                      <li key={f}>
                        <span className="font-medium text-slate-700">{FIELD_LABELS[f] ?? f}:</span>{' '}
                        {c.from !== null && <><span className="text-slate-400 line-through">{showValue(f, c.from)}</span> →{' '}</>}
                        <span className="text-slate-800">{showValue(f, c.to)}</span>
                      </li>
                    ))}
                  </ul>
                </li>
              ))}
            </ol>
          )}
        </Card>
        </div>
      </div>
    </>
  )
}

// ---- admin wrappers (read-only) -------------------------------------------------

export function AdminProducts() {
  return (
    <ProductListView
      title="Products"
      subtitle="Read-only. Product details are managed by each product's seller."
      action="view"
      detailPath={(id) => `/admin/products/${id}`}
      // `_` skips the browser cache so recent seller edits show up.
      load={(q) => api.products({ q: q.q, category: q.category || undefined, sort: q.sort, page: q.page, page_size: PAGE_SIZE, _: Date.now() })}
    />
  )
}

export function AdminProductView() {
  const { id = '' } = useParams()
  return (
    <ProductDetailView
      key={id}
      load={() => api.admin.product(id)}
      backTo="/admin/products"
      backLabel="All products"
      readOnlyNote="View only. Product details can be changed by the product's seller in the Seller Center."
      showSeller
    />
  )
}


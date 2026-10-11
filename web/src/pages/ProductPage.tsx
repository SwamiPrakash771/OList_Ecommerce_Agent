import { BadgeCheck, ChevronRight, Eye, MapPin, Minus, Package, Pencil, Plus, Ruler, Truck } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api, ApiError, type ProductDetail } from '../api'
import ProductImage from '../components/ProductImage'
import { AddToCartButton, EmptyState, ProductGrid, Spinner, Stars } from '../components/ui'
import { categoryLabel, date, money } from '../lib'
import { useStore } from '../state'

export default function ProductPage() {
  const { id = '' } = useParams()
  const [p, setP] = useState<ProductDetail | null>(null)
  const [missing, setMissing] = useState(false)
  const [qty, setQty] = useState(1)
  const { user } = useStore()

  useEffect(() => {
    setP(null)
    setMissing(false)
    setQty(1)
    window.scrollTo({ top: 0 })
    api.product(id).then(setP).catch((e) => e instanceof ApiError && e.status === 404 && setMissing(true))
  }, [id])


  if (missing) return <EmptyState icon={<Package size={40} />} title="Product not found"><Link to="/" className="text-brand-600 underline">Back to the store</Link></EmptyState>
  if (!p) return <Spinner />

  const rating = p.avg_rating ?? 0
  return (
    <>
      <nav className="mb-4 flex items-center gap-1 text-sm text-slate-500">
        <Link to="/" className="hover:text-slate-800">Home</Link>
        <ChevronRight size={14} />
        <Link to={`/search?category=${p.category}`} className="hover:text-slate-800">{categoryLabel(p.category)}</Link>
        <ChevronRight size={14} />
        <span className="text-slate-800">{p.display_name}</span>
      </nav>

      <div className="grid gap-8 lg:grid-cols-2">
        <ProductImage productId={p.product_id} category={p.category} className="aspect-square w-full rounded-3xl border border-slate-200" iconSize={140} />

        <div>
          <Link to={`/search?category=${p.category}`} className="text-sm font-semibold uppercase tracking-wide text-brand-600">
            {categoryLabel(p.category)}
          </Link>
          <h1 className="mt-1 text-3xl font-bold text-slate-900">{p.display_name}</h1>
          <div className="mt-2 flex items-center gap-2 text-sm text-slate-600">
            <Stars value={rating} size={18} />
            <span className="font-semibold">{rating.toFixed(1)}</span>
            <span>· {p.num_reviews} reviews · {p.times_sold} sold</span>
          </div>

          <div className="mt-6 rounded-2xl border border-slate-200 bg-white p-6">
            <div className="text-4xl font-extrabold text-slate-900">{money(p.typical_price)}</div>
            <div className="mt-1 text-sm text-slate-500">
              or up to 10× of {money(p.typical_price / 10)} on credit card
            </div>
            <div className="mt-4 flex items-center gap-2 text-sm text-slate-700">
              <Truck size={18} className="text-brand-500" /> Shipping about {money(p.avg_freight)}
            </div>

            {/* Admins and sellers browse the store but don't buy. Sellers only ever see their own
                products here (the API scopes it), so they get the edit page. */}
            {user?.role === 'seller' ? (
              <Link
                to={`/seller/products/${p.product_id}`}
                className="mt-6 flex items-center justify-center gap-2 rounded-xl bg-emerald-600 px-6 py-3 font-medium text-white hover:bg-emerald-700"
              >
                <Pencil size={18} /> Edit product
              </Link>
            ) : user?.role === 'admin' ? (
              <Link
                to={`/admin/products/${p.product_id}`}
                className="mt-6 flex items-center justify-center gap-2 rounded-xl border border-slate-300 px-6 py-3 font-medium text-slate-700 hover:bg-slate-50"
              >
                <Eye size={18} /> View details in admin panel
              </Link>
            ) : (
              <div className="mt-6 flex flex-wrap items-center gap-3">
                <div className="flex items-center rounded-xl border border-slate-200">
                  <button className="p-3 hover:bg-slate-50 disabled:opacity-30" disabled={qty <= 1} onClick={() => setQty(qty - 1)} aria-label="Decrease quantity"><Minus size={16} /></button>
                  <span className="w-10 text-center font-semibold" aria-live="polite">{qty}</span>
                  <button className="p-3 hover:bg-slate-50 disabled:opacity-30" disabled={qty >= 20} onClick={() => setQty(qty + 1)} aria-label="Increase quantity"><Plus size={16} /></button>
                </div>
                <AddToCartButton productId={p.product_id} quantity={qty} large className="flex-1" />
              </div>
            )}
          </div>

          <div className="mt-4 grid gap-3 sm:grid-cols-2">
            <div className="rounded-2xl border border-slate-200 bg-white p-4">
              <div className="mb-1 flex items-center gap-2 font-semibold text-slate-800"><BadgeCheck size={18} className="text-emerald-600" /> Seller</div>
              <div className="flex items-center gap-1 text-sm text-slate-600"><MapPin size={14} /> {p.seller_city ? `${p.seller_city.replace(/\b\w/g, (c) => c.toUpperCase())}, ${p.seller_state}` : '—'}</div>
              <div className="mt-1 text-sm text-slate-600">
                {p.seller_rating?.toFixed(1)} ★ · {p.seller_on_time_pct}% on time · {p.seller_orders?.toLocaleString()} orders
              </div>
            </div>
            <div className="rounded-2xl border border-slate-200 bg-white p-4">
              <div className="mb-1 flex items-center gap-2 font-semibold text-slate-800"><Ruler size={18} className="text-brand-500" /> Package</div>
              <div className="text-sm text-slate-600">
                {p.length_cm ?? '—'} × {p.width_cm ?? '—'} × {p.height_cm ?? '—'} cm
              </div>
              <div className="text-sm text-slate-600">{p.weight_g ? `${(p.weight_g / 1000).toFixed(2)} kg` : '—'}</div>
            </div>
          </div>
        </div>
      </div>

      <section className="mt-12">
        <h2 className="mb-4 text-2xl font-bold text-slate-900">Customer reviews</h2>
        <div className="grid gap-6 lg:grid-cols-3">
          <div className="rounded-2xl border border-slate-200 bg-white p-6 text-center">
            <div className="text-5xl font-extrabold text-slate-900">{rating.toFixed(1)}</div>
            <Stars value={rating} size={22} />
            <div className="mt-2 text-sm text-slate-500">{p.num_reviews} ratings</div>
          </div>
          <div className="space-y-3 lg:col-span-2">
            {p.reviews.length === 0 && <p className="text-slate-500">No written reviews yet.</p>}
            {p.reviews.map((r, i) => (
              <div key={i} className="rounded-2xl border border-slate-200 bg-white p-4">
                <div className="flex items-center justify-between">
                  <Stars value={r.score} />
                  <span className="text-xs text-slate-400">{date(r.date)}</span>
                </div>
                {r.title && <div className="mt-2 font-semibold text-slate-800">{r.title}</div>}
                <p className="mt-1 text-slate-600">{r.comment}</p>
              </div>
            ))}
            {p.reviews.length > 0 && <p className="text-xs text-slate-400">Reviews are shown in the original Portuguese.</p>}
          </div>
        </div>
      </section>

      {p.related.length > 0 && (
        <section className="mt-12">
          <h2 className="mb-4 text-2xl font-bold text-slate-900">More in {categoryLabel(p.category)}</h2>
          <ProductGrid products={p.related.slice(0, 5)} />
        </section>
      )}
    </>
  )
}

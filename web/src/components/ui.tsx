// Small presentational building blocks.

import { ChevronLeft, ChevronRight, Loader2, ShoppingCart, Star, Truck } from 'lucide-react'
import { Link, useNavigate } from 'react-router-dom'
import type { Product } from '../api'
import { categoryLabel, money } from '../lib'
import { useStore } from '../state'
import ProductImage from './ProductImage'

export function Stars({ value, size = 14 }: { value: number | null; size?: number }) {
  const v = value ?? 0
  return (
    <span className="inline-flex items-center gap-0.5" aria-label={`${v.toFixed(1)} out of 5`}>
      {[1, 2, 3, 4, 5].map((i) => (
        <Star
          key={i}
          size={size}
          className={i <= Math.round(v) ? 'fill-amber-400 text-amber-400' : 'fill-slate-200 text-slate-200'}
        />
      ))}
    </span>
  )
}

export function Spinner({ label = 'Loading…' }: { label?: string }) {
  return (
    <div className="flex items-center justify-center gap-2 py-16 text-slate-500">
      <Loader2 className="animate-spin" size={20} /> {label}
    </div>
  )
}

export function EmptyState({ icon, title, children }: { icon: React.ReactNode; title: string; children?: React.ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center rounded-2xl border border-dashed border-slate-300 bg-white px-6 py-16 text-center">
      <div className="mb-4 text-slate-400">{icon}</div>
      <h3 className="text-lg font-semibold text-slate-800">{title}</h3>
      <div className="mt-2 text-slate-500">{children}</div>
    </div>
  )
}

export function AddToCartButton({ productId, className = '', quantity = 1, large = false }: {
  productId: string; className?: string; quantity?: number; large?: boolean
}) {
  const { user, addToCart } = useStore()
  const navigate = useNavigate()
  if (user && user.role !== 'customer') return null // admins and sellers don't buy
  if (!user) {
    // A button, not a link: this often sits inside a product card that is itself a link.
    return (
      <button
        onClick={(e) => {
          e.preventDefault()
          navigate('/login', { state: { from: window.location.pathname + window.location.search } })
        }}
        className={`inline-flex items-center justify-center gap-2 rounded-xl border border-brand-500 font-medium text-brand-600 hover:bg-brand-50 ${large ? 'px-6 py-3' : 'px-3 py-2 text-sm'} ${className}`}
      >
        Sign in to buy
      </button>
    )
  }
  return (
    <button
      onClick={(e) => {
        e.preventDefault()
        addToCart(productId, quantity)
      }}
      className={`inline-flex items-center justify-center gap-2 rounded-xl bg-brand-500 font-medium text-white shadow-sm transition hover:bg-brand-600 active:scale-[0.98] ${large ? 'px-6 py-3' : 'px-3 py-2 text-sm'} ${className}`}
    >
      <ShoppingCart size={large ? 20 : 16} /> Add to cart
    </button>
  )
}

export function ProductCard({ p }: { p: Product }) {
  return (
    <Link
      to={`/product/${p.product_id}`}
      className="group flex flex-col overflow-hidden rounded-2xl border border-slate-200 bg-white transition hover:-translate-y-0.5 hover:shadow-lg"
    >
      <ProductImage productId={p.product_id} category={p.category} className="aspect-square transition group-hover:scale-[1.02]" />
      <div className="flex flex-1 flex-col gap-1.5 p-4">
        <span className="text-xs font-medium uppercase tracking-wide text-slate-400">{categoryLabel(p.category)}</span>
        <h3 className="line-clamp-2 font-medium text-slate-800 group-hover:text-brand-600">{p.name}</h3>
        <div className="flex items-center gap-1.5 text-sm text-slate-500">
          <Stars value={p.rating} />
          <span>({p.reviews})</span>
        </div>
        <div className="mt-auto pt-2">
          <div className="text-xl font-bold text-slate-900">{money(p.price)}</div>
          <div className="flex items-center gap-1 text-xs text-slate-500">
            <Truck size={12} /> + {money(p.shipping)} shipping · {p.sold} sold
          </div>
        </div>
        <AddToCartButton productId={p.product_id} className="mt-3 w-full" />
      </div>
    </Link>
  )
}

export function ProductGrid({ products }: { products: Product[] }) {
  return (
    <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5">
      {products.map((p) => (
        <ProductCard key={p.product_id} p={p} />
      ))}
    </div>
  )
}

export function Pagination({ page, total, pageSize, onPage }: {
  page: number; total: number; pageSize: number; onPage: (p: number) => void
}) {
  const pages = Math.max(1, Math.ceil(total / pageSize))
  if (pages <= 1) return null
  const nums = Array.from(new Set([1, page - 1, page, page + 1, pages])).filter((n) => n >= 1 && n <= pages)
  const btn = 'flex h-10 min-w-10 items-center justify-center rounded-lg border px-3 text-sm'
  return (
    <nav className="flex items-center justify-center gap-1.5 py-8" aria-label="Pagination">
      <button className={`${btn} border-slate-200 bg-white disabled:opacity-40`} disabled={page === 1} onClick={() => onPage(page - 1)}>
        <ChevronLeft size={16} />
      </button>
      {nums.map((n, i) => (
        <span key={n} className="flex items-center gap-1.5">
          {i > 0 && n - nums[i - 1] > 1 && <span className="px-1 text-slate-400">…</span>}
          <button
            className={`${btn} ${n === page ? 'border-brand-500 bg-brand-500 text-white' : 'border-slate-200 bg-white hover:bg-slate-50'}`}
            onClick={() => onPage(n)}
          >
            {n}
          </button>
        </span>
      ))}
      <button className={`${btn} border-slate-200 bg-white disabled:opacity-40`} disabled={page === pages} onClick={() => onPage(page + 1)}>
        <ChevronRight size={16} />
      </button>
    </nav>
  )
}

const STATUS_STYLES: Record<string, string> = {
  delivered: 'bg-emerald-50 text-emerald-700 ring-emerald-200',
  shipped: 'bg-sky-50 text-sky-700 ring-sky-200',
  invoiced: 'bg-indigo-50 text-indigo-700 ring-indigo-200',
  processing: 'bg-amber-50 text-amber-700 ring-amber-200',
  approved: 'bg-blue-50 text-blue-700 ring-blue-200',
  created: 'bg-slate-100 text-slate-700 ring-slate-200',
  canceled: 'bg-rose-50 text-rose-700 ring-rose-200',
  unavailable: 'bg-rose-50 text-rose-700 ring-rose-200',
}

export function StatusBadge({ status }: { status: string }) {
  return (
    <span className={`inline-flex rounded-full px-2.5 py-0.5 text-xs font-medium capitalize ring-1 ring-inset ${STATUS_STYLES[status] ?? STATUS_STYLES.created}`}>
      {status}
    </span>
  )
}

export function Card({ children, className = '' }: { children: React.ReactNode; className?: string }) {
  return <div className={`rounded-2xl border border-slate-200 bg-white p-5 ${className}`}>{children}</div>
}

export function Stat({ label, value, sub }: { label: string; value: React.ReactNode; sub?: React.ReactNode }) {
  return (
    <Card>
      <div className="text-sm text-slate-500">{label}</div>
      <div className="mt-1 text-2xl font-bold text-slate-900">{value}</div>
      {sub && <div className="mt-1 text-xs text-slate-500">{sub}</div>}
    </Card>
  )
}

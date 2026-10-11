import { SearchX, SlidersHorizontal, X } from 'lucide-react'
import { useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { api, type Category, type Product } from '../api'
import { EmptyState, Pagination, ProductGrid, Spinner, Stars } from '../components/ui'
import { categoryLabel } from '../lib'

const PAGE_SIZE = 24
const SORTS = [
  ['popular', 'Most popular'],
  ['rating', 'Top rated'],
  ['price_low', 'Price: low to high'],
  ['price_high', 'Price: high to low'],
]
const PRICE_BANDS: [string, number | null, number | null][] = [
  ['Any price', null, null],
  ['Under $50', null, 50],
  ['$50 – $100', 50, 100],
  ['$100 – $250', 100, 250],
  ['$250 – $500', 250, 500],
  ['Over $500', 500, null],
]

export default function Search() {
  const [params, setParams] = useSearchParams()
  const [cats, setCats] = useState<Category[]>([])
  const [result, setResult] = useState<{ items: Product[]; total: number } | null>(null)
  const [showFilters, setShowFilters] = useState(false)

  const q = params.get('q') ?? ''
  const category = params.get('category') ?? ''
  const sort = params.get('sort') ?? 'popular'
  const minPrice = params.get('min_price')
  const maxPrice = params.get('max_price')
  const minRating = params.get('min_rating')
  const page = Number(params.get('page') ?? 1)

  useEffect(() => {
    api.categories().then(setCats)
  }, [])

  useEffect(() => {
    setResult(null)
    api
      .products({
        q, category: category || undefined, sort, page, page_size: PAGE_SIZE,
        min_price: minPrice ? Number(minPrice) : undefined,
        max_price: maxPrice ? Number(maxPrice) : undefined,
        min_rating: minRating ? Number(minRating) : undefined,
      })
      .then(setResult)
    window.scrollTo({ top: 0 })
  }, [q, category, sort, minPrice, maxPrice, minRating, page])

  const update = (changes: Record<string, string | null>) => {
    const next = new URLSearchParams(params)
    for (const [k, v] of Object.entries(changes)) {
      if (v === null || v === '') next.delete(k)
      else next.set(k, v)
    }
    if (!('page' in changes)) next.delete('page')
    setParams(next)
  }

  const activeBand = PRICE_BANDS.findIndex(([, lo, hi]) => String(lo ?? '') === (minPrice ?? '') && String(hi ?? '') === (maxPrice ?? ''))
  const title = q ? `Results for “${q}”` : category ? categoryLabel(category) : 'All products'
  const chips = [
    q && ['q', `“${q}”`],
    category && ['category', categoryLabel(category)],
    (minPrice || maxPrice) && ['price', PRICE_BANDS[activeBand]?.[0] ?? 'Custom price'],
    minRating && ['min_rating', `${minRating}★ & up`],
  ].filter(Boolean) as [string, string][]

  const filters = (
    <div className="space-y-6">
      <div>
        <h3 className="mb-2 text-sm font-semibold text-slate-900">Category</h3>
        <select
          value={category}
          onChange={(e) => update({ category: e.target.value })}
          className="w-full rounded-xl border border-slate-200 bg-white px-3 py-2 text-sm outline-none focus:border-brand-500"
        >
          <option value="">All categories</option>
          {cats.map((c) => (
            <option key={c.category} value={c.category}>
              {categoryLabel(c.category)} ({c.products})
            </option>
          ))}
        </select>
      </div>
      <div>
        <h3 className="mb-2 text-sm font-semibold text-slate-900">Price</h3>
        <div className="space-y-1">
          {PRICE_BANDS.map(([label, lo, hi], i) => (
            <label key={label} className="flex cursor-pointer items-center gap-2 rounded-lg px-2 py-1.5 text-sm hover:bg-slate-50">
              <input
                type="radio"
                name="price"
                checked={activeBand === i}
                onChange={() => update({ min_price: lo === null ? null : String(lo), max_price: hi === null ? null : String(hi) })}
                className="accent-brand-500"
              />
              {label}
            </label>
          ))}
        </div>
      </div>
      <div>
        <h3 className="mb-2 text-sm font-semibold text-slate-900">Customer rating</h3>
        <div className="space-y-1">
          {[null, '4.5', '4', '3'].map((r) => (
            <label key={r ?? 'any'} className="flex cursor-pointer items-center gap-2 rounded-lg px-2 py-1.5 text-sm hover:bg-slate-50">
              <input type="radio" name="rating" checked={(minRating ?? null) === r} onChange={() => update({ min_rating: r })} className="accent-brand-500" />
              {r ? (
                <span className="flex items-center gap-1.5"><Stars value={Number(r)} /> {r} & up</span>
              ) : (
                'Any rating'
              )}
            </label>
          ))}
        </div>
      </div>
    </div>
  )

  return (
    <div className="flex gap-8">
      <aside className="hidden w-60 shrink-0 lg:block">
        <div className="sticky top-36 rounded-2xl border border-slate-200 bg-white p-5">{filters}</div>
      </aside>

      <div className="min-w-0 flex-1">
        <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
          <div>
            <h1 className="text-2xl font-bold text-slate-900">{title}</h1>
            <p className="text-sm text-slate-500">{result ? `${result.total.toLocaleString()} products` : 'Searching…'}</p>
          </div>
          <div className="flex items-center gap-2">
            <button onClick={() => setShowFilters(true)} className="flex items-center gap-2 rounded-xl border border-slate-200 bg-white px-3 py-2 text-sm lg:hidden">
              <SlidersHorizontal size={16} /> Filters
            </button>
            <select
              value={sort}
              onChange={(e) => update({ sort: e.target.value })}
              className="rounded-xl border border-slate-200 bg-white px-3 py-2 text-sm outline-none focus:border-brand-500"
              aria-label="Sort by"
            >
              {SORTS.map(([v, l]) => (
                <option key={v} value={v}>{l}</option>
              ))}
            </select>
          </div>
        </div>

        {chips.length > 0 && (
          <div className="mb-4 flex flex-wrap gap-2">
            {chips.map(([key, label]) => (
              <button
                key={key}
                onClick={() => update(key === 'price' ? { min_price: null, max_price: null } : { [key]: null })}
                className="flex items-center gap-1 rounded-full bg-brand-50 px-3 py-1 text-sm text-brand-700 hover:bg-brand-100"
              >
                {label} <X size={14} />
              </button>
            ))}
            <button onClick={() => setParams(new URLSearchParams({ sort }))} className="px-2 text-sm text-slate-500 hover:underline">
              Clear all
            </button>
          </div>
        )}

        {!result ? (
          <Spinner />
        ) : result.items.length === 0 ? (
          <EmptyState icon={<SearchX size={40} />} title="No products found">
            Try a different word — searches match category names like “watches”, “garden” or “toys”.
          </EmptyState>
        ) : (
          <>
            <ProductGrid products={result.items} />
            <Pagination page={page} total={result.total} pageSize={PAGE_SIZE} onPage={(p) => update({ page: String(p) })} />
          </>
        )}
      </div>

      {showFilters && (
        <div className="fixed inset-0 z-40 flex lg:hidden">
          <div className="absolute inset-0 bg-black/30" onClick={() => setShowFilters(false)} />
          <div className="relative ml-auto h-full w-80 max-w-full overflow-y-auto bg-white p-5">
            <div className="mb-4 flex items-center justify-between">
              <h2 className="text-lg font-semibold">Filters</h2>
              <button onClick={() => setShowFilters(false)} aria-label="Close filters"><X /></button>
            </div>
            {filters}
          </div>
        </div>
      )}
    </div>
  )
}

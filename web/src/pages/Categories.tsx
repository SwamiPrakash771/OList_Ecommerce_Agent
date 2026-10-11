import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, type Category } from '../api'
import { iconFor } from '../components/ProductImage'
import { Spinner } from '../components/ui'
import { categoryLabel, hueFor, money } from '../lib'

export default function Categories() {
  const [cats, setCats] = useState<Category[] | null>(null)
  const [filter, setFilter] = useState('')
  useEffect(() => {
    api.categories().then(setCats)
  }, [])
  if (!cats) return <Spinner />
  const shown = cats.filter((c) => categoryLabel(c.category).toLowerCase().includes(filter.toLowerCase()))

  return (
    <>
      <div className="mb-6 flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-3xl font-bold text-slate-900">All categories</h1>
          <p className="text-slate-500">{cats.length} categories</p>
        </div>
        <input
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          placeholder="Filter categories"
          className="w-64 rounded-xl border border-slate-200 bg-white px-4 py-2 text-sm outline-none focus:border-brand-500 focus:ring-4 focus:ring-brand-100"
        />
      </div>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
        {shown.map((c) => {
          const Icon = iconFor(c.category)
          const hue = hueFor(c.category)
          return (
            <Link
              key={c.category}
              to={`/search?category=${c.category}`}
              className="group flex items-center gap-3 rounded-2xl border border-slate-200 bg-white p-4 transition hover:shadow-md"
            >
              <span className="flex h-12 w-12 shrink-0 items-center justify-center rounded-xl" style={{ background: `hsl(${hue} 70% 93%)`, color: `hsl(${hue} 45% 38%)` }}>
                <Icon size={24} />
              </span>
              <div className="min-w-0">
                <div className="truncate font-semibold text-slate-800 group-hover:text-brand-600">{categoryLabel(c.category)}</div>
                <div className="text-xs text-slate-500">
                  {c.products.toLocaleString()} items · from {money(c.from_price)}
                </div>
              </div>
            </Link>
          )
        })}
      </div>
    </>
  )
}

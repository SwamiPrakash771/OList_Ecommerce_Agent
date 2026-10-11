import { ArrowRight, RotateCcw, ShieldCheck, Truck } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, type Category, type Product } from '../api'
import { iconFor } from '../components/ProductImage'
import { ProductGrid, Spinner } from '../components/ui'
import { categoryLabel, hueFor, money } from '../lib'
import { useStore } from '../state'

function Section({ title, link, children }: { title: string; link: string; children: React.ReactNode }) {
  return (
    <section className="mt-12">
      <div className="mb-4 flex items-end justify-between">
        <h2 className="text-2xl font-bold text-slate-900">{title}</h2>
        <Link to={link} className="flex items-center gap-1 text-sm font-medium text-brand-600 hover:underline">
          See all <ArrowRight size={14} />
        </Link>
      </div>
      {children}
    </section>
  )
}

export default function Home() {
  const { user } = useStore()
  const seller = user?.role === 'seller'
  const [shopSize, setShopSize] = useState<number | null>(null)
  const [popular, setPopular] = useState<Product[] | null>(null)
  const [topRated, setTopRated] = useState<Product[] | null>(null)
  const [cats, setCats] = useState<Category[]>([])

  useEffect(() => {
    // For a signed-in seller the API returns only their products.
    api.products({ sort: 'popular', page_size: 10 }).then((r) => setPopular(r.items))
    api.products({ sort: 'rating', min_rating: 4.5, page_size: 10 }).then((r) => setTopRated(r.items))
    api.categories().then((c) => {
      setShopSize(c.reduce((n, x) => n + x.products, 0))
      setCats(c.filter((x) => x.category !== 'uncategorized').slice(0, 8))
    })
  }, [user?.username])

  return (
    <>
      {/* Hero */}
      <section className="relative overflow-hidden rounded-3xl bg-gradient-to-br from-brand-600 via-brand-500 to-sky-400 px-6 py-12 text-white md:px-12 md:py-16">
        <div className="relative z-10 max-w-xl">
          {seller ? (
            <>
              <p className="mb-2 text-sm font-semibold uppercase tracking-widest text-white/80">Your shop</p>
              <h1 className="text-4xl font-extrabold leading-tight md:text-5xl">
                {shopSize === null ? 'Your products' : `${shopSize.toLocaleString()} products you sell`}
              </h1>
              <p className="mt-4 text-lg text-white/85">
                This is how your shop looks to customers. Only your own products are shown here.
              </p>
              <div className="mt-8 flex flex-wrap gap-3">
                <Link to="/seller/products/new" className="rounded-xl bg-white px-6 py-3 font-semibold text-brand-700 shadow hover:bg-brand-50">
                  Add a product
                </Link>
                <Link to="/seller" className="rounded-xl border border-white/60 px-6 py-3 font-semibold text-white hover:bg-white/10">
                  Seller Center
                </Link>
              </div>
            </>
          ) : (
            <>
              <p className="mb-2 text-sm font-semibold uppercase tracking-widest text-white/80">
                {user?.role === 'customer' ? 'Welcome back' : 'Brazil’s marketplace'}
              </p>
              <h1 className="text-4xl font-extrabold leading-tight md:text-5xl">32,000+ products from 3,000 trusted sellers</h1>
              <p className="mt-4 text-lg text-white/85">Home, beauty, sports, electronics and more — rated by real customers.</p>
              <div className="mt-8 flex flex-wrap gap-3">
                <Link to="/search?sort=popular" className="rounded-xl bg-white px-6 py-3 font-semibold text-brand-700 shadow hover:bg-brand-50">
                  Shop best sellers
                </Link>
                <Link to="/categories" className="rounded-xl border border-white/60 px-6 py-3 font-semibold text-white hover:bg-white/10">
                  Browse categories
                </Link>
              </div>
            </>
          )}
        </div>
        <div className="absolute -right-16 -top-16 h-72 w-72 rounded-full bg-white/10" />
        <div className="absolute -bottom-24 right-24 h-64 w-64 rounded-full bg-white/10" />
      </section>

      {/* Trust strip (shopper benefits; not shown to sellers) */}
      {!seller && <div className="mt-6 grid gap-3 sm:grid-cols-3">
        {[
          [Truck, 'Delivery across Brazil', '92% of orders arrive on time'],
          [ShieldCheck, 'Secure checkout', 'Card, boleto, debit or voucher'],
          [RotateCcw, 'Easy returns', 'Report issues from your orders'],
        ].map(([Icon, title, sub]) => {
          const I = Icon as typeof Truck
          return (
            <div key={title as string} className="flex items-center gap-3 rounded-2xl border border-slate-200 bg-white p-4">
              <span className="flex h-10 w-10 items-center justify-center rounded-xl bg-brand-50 text-brand-600"><I size={20} /></span>
              <div>
                <div className="font-semibold text-slate-800">{title as string}</div>
                <div className="text-sm text-slate-500">{sub as string}</div>
              </div>
            </div>
          )
        })}
      </div>}

      <Section title={seller ? "Your categories" : "Shop by category"} link="/categories">
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          {cats.map((c) => {
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
                  <div className="text-xs text-slate-500">{c.products.toLocaleString()} items · from {money(c.from_price)}</div>
                </div>
              </Link>
            )
          })}
        </div>
      </Section>

      <Section title={seller ? "Your best sellers" : "Best sellers"} link="/search?sort=popular">
        {popular ? <ProductGrid products={popular} /> : <Spinner />}
      </Section>

      <Section title={seller ? "Your top-rated products" : "Top rated by customers"} link="/search?sort=rating&min_rating=4.5">
        {topRated ? <ProductGrid products={topRated} /> : <Spinner />}
      </Section>
    </>
  )
}

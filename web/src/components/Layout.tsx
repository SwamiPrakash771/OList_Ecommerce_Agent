// Store chrome: header with search + cart, category strip, footer, toasts.

import { CheckCircle2, ChevronDown, LogOut, Package, Plus, Search, ShieldCheck, ShoppingCart, Sparkles, Store, User as UserIcon, XCircle } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { Link, NavLink, Outlet, useNavigate, useSearchParams } from 'react-router-dom'
import { api, type Category } from '../api'
import { categoryLabel } from '../lib'
import { useStore } from '../state'
import { ChatWidget } from './Chat'
import ServiceBanner from './ServiceBanner'
import TempPasswordBanner from './TempPasswordBanner'

function SearchBar() {
  const [params] = useSearchParams()
  const [q, setQ] = useState(params.get('q') ?? '')
  const navigate = useNavigate()
  useEffect(() => setQ(params.get('q') ?? ''), [params])
  return (
    <form
      className="relative flex-1"
      onSubmit={(e) => {
        e.preventDefault()
        navigate(`/search?q=${encodeURIComponent(q.trim())}`)
      }}
      role="search"
    >
      <input
        value={q}
        onChange={(e) => setQ(e.target.value)}
        placeholder="Search products, categories…  e.g. watches, garden, toys"
        className="w-full rounded-xl border border-slate-200 bg-slate-50 py-2.5 pl-4 pr-12 text-sm outline-none transition focus:border-brand-500 focus:bg-white focus:ring-4 focus:ring-brand-100"
        aria-label="Search products"
      />
      <button type="submit" className="absolute right-1 top-1 flex h-8 w-10 items-center justify-center rounded-lg bg-brand-500 text-white hover:bg-brand-600" aria-label="Search">
        <Search size={16} />
      </button>
    </form>
  )
}

function AccountMenu() {
  const { user, logout } = useStore()
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  const navigate = useNavigate()
  useEffect(() => {
    const close = (e: MouseEvent) => ref.current && !ref.current.contains(e.target as Node) && setOpen(false)
    document.addEventListener('mousedown', close)
    return () => document.removeEventListener('mousedown', close)
  }, [])

  if (!user) {
    return (
      <Link to="/login" className="flex items-center gap-2 rounded-xl px-3 py-2 text-sm font-medium hover:bg-slate-100">
        <UserIcon size={18} /> Sign in
      </Link>
    )
  }
  const admin = user.role === 'admin'
  const seller = user.role === 'seller'
  const label = admin ? user.username : seller ? `Seller …${user.username.slice(-6)}` : `Customer …${user.username.slice(-6)}`
  return (
    <div className="relative" ref={ref}>
      <button onClick={() => setOpen(!open)} className="flex items-center gap-2 rounded-xl px-3 py-2 text-sm font-medium hover:bg-slate-100">
        <span className="flex h-7 w-7 items-center justify-center rounded-full bg-brand-100 text-brand-700">
          {admin ? <ShieldCheck size={15} /> : seller ? <Store size={15} /> : <UserIcon size={15} />}
        </span>
        <span className="hidden md:inline">{label}</span>
        <ChevronDown size={14} />
      </button>
      {open && (
        <div className="absolute right-0 z-30 mt-2 w-52 overflow-hidden rounded-xl border border-slate-200 bg-white py-1 shadow-xl" onClick={() => setOpen(false)}>
          {seller ? (
            <>
              <Link to="/seller" className="flex items-center gap-2 px-4 py-2 text-sm hover:bg-slate-50"><Store size={16} /> Seller Center</Link>
              <Link to="/seller/products/new" className="flex items-center gap-2 px-4 py-2 text-sm hover:bg-slate-50"><Plus size={16} /> Add product</Link>
              <Link to="/seller/account" className="flex items-center gap-2 px-4 py-2 text-sm hover:bg-slate-50"><UserIcon size={16} /> Account settings</Link>
            </>
          ) : admin ? (
            <>
              <Link to="/admin" className="flex items-center gap-2 px-4 py-2 text-sm hover:bg-slate-50"><ShieldCheck size={16} /> Admin panel</Link>
              <Link to="/admin/account" className="flex items-center gap-2 px-4 py-2 text-sm hover:bg-slate-50"><UserIcon size={16} /> Account settings</Link>
            </>
          ) : (
            <>
              <Link to="/orders" className="flex items-center gap-2 px-4 py-2 text-sm hover:bg-slate-50"><Package size={16} /> My orders</Link>
              <Link to="/account" className="flex items-center gap-2 px-4 py-2 text-sm hover:bg-slate-50"><UserIcon size={16} /> My account</Link>
            </>
          )}
          <button
            onClick={async () => {
              await logout()
              navigate('/')
            }}
            className="flex w-full items-center gap-2 border-t border-slate-100 px-4 py-2 text-left text-sm text-rose-600 hover:bg-rose-50"
          >
            <LogOut size={16} /> Sign out
          </button>
        </div>
      )}
    </div>
  )
}

function CategoryStrip() {
  const [cats, setCats] = useState<Category[]>([])
  useEffect(() => {
    api.categories().then((c) => setCats(c.filter((x) => x.category !== 'uncategorized').slice(0, 12)))
  }, [])
  return (
    <nav className="border-t border-slate-100 bg-white">
      <div className="mx-auto flex max-w-7xl gap-1 overflow-x-auto px-4 py-2 text-sm [scrollbar-width:none]">
        {cats.map((c) => (
          <NavLink
            key={c.category}
            to={`/search?category=${c.category}`}
            className="shrink-0 rounded-lg px-3 py-1.5 text-slate-600 hover:bg-slate-100 hover:text-slate-900"
          >
            {categoryLabel(c.category)}
          </NavLink>
        ))}
        <Link to="/categories" className="shrink-0 rounded-lg px-3 py-1.5 font-medium text-brand-600 hover:bg-brand-50">
          All categories →
        </Link>
      </div>
    </nav>
  )
}

function Toasts() {
  const { toasts } = useStore()
  return (
    <div className="fixed bottom-24 right-4 z-50 flex flex-col gap-2" aria-live="polite">
      {toasts.map((t) => (
        <div
          key={t.id}
          className={`flex items-center gap-2 rounded-xl px-4 py-3 text-sm font-medium text-white shadow-lg ${t.kind === 'success' ? 'bg-slate-900' : 'bg-rose-600'}`}
        >
          {t.kind === 'success' ? <CheckCircle2 size={18} className="text-emerald-400" /> : <XCircle size={18} />}
          {t.text}
        </div>
      ))}
    </div>
  )
}

export default function Layout() {
  const { user, cart } = useStore()
  return (
    <div className="flex min-h-screen flex-col">
      <header className="sticky top-0 z-20 bg-white/95 shadow-sm backdrop-blur">
        <div className="mx-auto flex max-w-7xl items-center gap-3 px-4 py-3 md:gap-6">
          <Link to="/" className="flex shrink-0 items-center gap-2 text-xl font-extrabold tracking-tight text-slate-900">
            <span className="flex h-9 w-9 items-center justify-center rounded-xl bg-brand-500 text-white">O</span>
            <span className="hidden sm:inline">Olist<span className="text-brand-500">Store</span></span>
          </Link>
          <SearchBar />
          <AccountMenu />
          {user?.role === 'customer' && (
            <NavLink
              to="/orders"
              className={({ isActive }) =>
                `flex items-center gap-2 rounded-xl px-3 py-2 text-sm font-medium ${isActive ? 'bg-brand-50 text-brand-700' : 'hover:bg-slate-100'}`
              }
            >
              <Package size={20} />
              <span className="hidden lg:inline">My orders</span>
            </NavLink>
          )}
          <NavLink
            to="/assistant"
            className={({ isActive }) =>
              `flex items-center gap-2 rounded-xl px-3 py-2 text-sm font-medium ${isActive ? 'bg-brand-50 text-brand-700' : 'hover:bg-slate-100'}`
            }
          >
            <Sparkles size={20} className="text-brand-500" />
            <span className="hidden lg:inline">AI Assistant</span>
          </NavLink>
          {user?.role === 'admin' ? (
            <Link to="/admin" className="flex items-center gap-2 rounded-xl px-3 py-2 text-sm font-medium hover:bg-slate-100">
              <ShieldCheck size={20} />
              <span className="hidden lg:inline">Admin panel</span>
            </Link>
          ) : user?.role === 'seller' ? (
            <Link to="/seller" className="flex items-center gap-2 rounded-xl px-3 py-2 text-sm font-medium hover:bg-slate-100">
              <Store size={20} />
              <span className="hidden lg:inline">Seller Center</span>
            </Link>
          ) : (
            <Link to="/cart" className="relative flex items-center gap-2 rounded-xl px-3 py-2 text-sm font-medium hover:bg-slate-100" aria-label={`Cart, ${cart.count} items`}>
              <ShoppingCart size={22} />
              {cart.count > 0 && (
                <span className="absolute -right-0.5 -top-0.5 flex h-5 min-w-5 items-center justify-center rounded-full bg-accent-500 px-1 text-xs font-bold text-white">
                  {cart.count}
                </span>
              )}
              <span className="hidden lg:inline">Cart</span>
            </Link>
          )}
        </div>
        <CategoryStrip />
      </header>

      <ServiceBanner />
      <TempPasswordBanner accountPath={user?.role === 'admin' ? '/admin/account' : user?.role === 'seller' ? '/seller/account' : '/account'} />

      <main className="mx-auto w-full max-w-7xl flex-1 px-4 py-6">
        <Outlet />
      </main>

      <footer className="mt-12 border-t border-slate-200 bg-white">
        <div className="mx-auto grid max-w-7xl gap-6 px-4 py-10 text-sm text-slate-500 sm:grid-cols-3">
          <div>
            <div className="mb-2 text-base font-bold text-slate-900">Olist<span className="text-brand-500">Store</span></div>
            Demo storefront built on the public Olist Brazilian e-commerce dataset (2016–2018, dates moved to 2024–2026).
          </div>
          <div>
            <div className="mb-2 font-semibold text-slate-700">Shop</div>
            <Link to="/search?sort=popular" className="block hover:text-slate-900">Best sellers</Link>
            <Link to="/search?sort=rating" className="block hover:text-slate-900">Top rated</Link>
            <Link to="/categories" className="block hover:text-slate-900">All categories</Link>
          </div>
          <div>
            <div className="mb-2 font-semibold text-slate-700">Account</div>
            {user?.role === 'seller' ? (
              <>
                <Link to="/seller" className="block hover:text-slate-900">Seller Center</Link>
                <Link to="/seller/products/new" className="block hover:text-slate-900">Add product</Link>
                <Link to="/seller/account" className="block hover:text-slate-900">Account settings</Link>
              </>
            ) : user?.role === 'admin' ? (
              <>
                <Link to="/admin" className="block hover:text-slate-900">Admin panel</Link>
                <Link to="/admin/account" className="block hover:text-slate-900">Account settings</Link>
              </>
            ) : (
              <>
                <Link to="/orders" className="block hover:text-slate-900">My orders</Link>
                <Link to="/cart" className="block hover:text-slate-900">Cart</Link>
                <Link to="/account" className="block hover:text-slate-900">Account settings</Link>
              </>
            )}
          </div>
        </div>
      </footer>
      <Toasts />
      <ChatWidget />
    </div>
  )
}

export { Toasts }

import { ArrowLeft, CheckCircle2, ChevronRight, Package } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { api, ApiError, type OrderDetail, type OrderSummary } from '../api'
import OrderView from '../components/OrderView'
import { EmptyState, Spinner, Stat, StatusBadge } from '../components/ui'
import { date, money } from '../lib'

export function OrdersList() {
  const [data, setData] = useState<Awaited<ReturnType<typeof api.orders>> | null>(null)
  const [filter, setFilter] = useState('all')
  useEffect(() => {
    api.orders().then(setData)
  }, [])
  if (!data) return <Spinner />

  const statuses = ['all', ...Array.from(new Set(data.orders.map((o) => o.status)))]
  const shown = data.orders.filter((o) => filter === 'all' || o.status === filter)

  return (
    <>
      <h1 className="mb-6 text-3xl font-bold text-slate-900">My orders</h1>
      <div className="mb-6 grid gap-4 sm:grid-cols-3">
        <Stat label="Orders placed" value={data.summary.orders} />
        <Stat label="Total spent" value={money(data.summary.total_paid)} />
        <Stat label="Average rating you gave" value={data.summary.avg_review_given ? `${data.summary.avg_review_given.toFixed(1)} ★` : '—'} />
      </div>

      <div className="mb-4 flex flex-wrap gap-2">
        {statuses.map((s) => (
          <button
            key={s}
            onClick={() => setFilter(s)}
            className={`rounded-full px-4 py-1.5 text-sm font-medium capitalize ${filter === s ? 'bg-slate-900 text-white' : 'bg-white text-slate-600 ring-1 ring-slate-200 hover:bg-slate-50'}`}
          >
            {s}
          </button>
        ))}
      </div>

      {shown.length === 0 ? (
        <EmptyState icon={<Package size={40} />} title="No orders yet"><Link to="/" className="text-brand-600 underline">Start shopping</Link></EmptyState>
      ) : (
        <div className="space-y-3">
          {shown.map((o) => <OrderRow key={o.order_id} o={o} to={`/orders/${o.order_id}`} />)}
        </div>
      )}
    </>
  )
}

export function OrderRow({ o, to, showCustomer = false }: { o: OrderSummary; to: string; showCustomer?: boolean }) {
  return (
    <Link to={to} className="flex flex-wrap items-center gap-4 rounded-2xl border border-slate-200 bg-white p-4 transition hover:shadow-md">
      <span className="flex h-12 w-12 items-center justify-center rounded-xl bg-brand-50 text-brand-600"><Package size={22} /></span>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-semibold text-slate-800">{date(o.purchased_at)}</span>
          <StatusBadge status={o.status} />
        </div>
        <div className="truncate font-mono text-xs text-slate-500">#{o.order_id}{showCustomer && o.customer_unique_id ? ` · customer ${o.customer_unique_id}` : ''}</div>
      </div>
      <div className="text-right text-sm">
        <div className="font-bold text-slate-900">{money(o.order_total)}</div>
        <div className="text-slate-500">{o.items} item{o.items === 1 ? '' : 's'}</div>
      </div>
      <div className="hidden text-right text-sm sm:block sm:w-40">
        <div className="text-slate-500">{o.delivered_at ? 'Delivered' : 'Estimated'}</div>
        <div className="font-medium text-slate-700">{date(o.delivered_at ?? o.estimated_delivery)}</div>
      </div>
      <ChevronRight className="text-slate-400" size={18} />
    </Link>
  )
}

export function OrderPage() {
  const { id = '' } = useParams()
  const [params] = useSearchParams()
  const [order, setOrder] = useState<OrderDetail | null>(null)
  const [missing, setMissing] = useState(false)
  useEffect(() => {
    api.order(id).then(setOrder).catch((e) => e instanceof ApiError && e.status === 404 && setMissing(true))
  }, [id])

  if (missing) return <EmptyState icon={<Package size={40} />} title="Order not found"><Link to="/orders" className="text-brand-600 underline">Back to my orders</Link></EmptyState>
  if (!order) return <Spinner />

  return (
    <>
      {params.get('placed') && (
        <div className="mb-6 flex items-center gap-3 rounded-2xl border border-emerald-200 bg-emerald-50 p-5 text-emerald-800">
          <CheckCircle2 size={28} />
          <div>
            <div className="text-lg font-semibold">Thank you! Your order has been placed.</div>
            <div className="text-sm">We'll keep you updated as it moves along.</div>
          </div>
        </div>
      )}
      <Link to="/orders" className="mb-4 inline-flex items-center gap-1 text-sm text-slate-500 hover:text-slate-800"><ArrowLeft size={14} /> All orders</Link>
      <OrderView order={order} />
    </>
  )
}

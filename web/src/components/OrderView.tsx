// Full order detail: progress tracker, items, payment, review. Used by customers and admins.

import { Check, CircleDot, MapPin, Star } from 'lucide-react'
import { Link } from 'react-router-dom'
import type { OrderDetail } from '../api'
import { categoryLabel, date, deliveryVerdict, money, PAYMENT_LABELS } from '../lib'
import { AddressLines } from './Address'
import ProductImage from './ProductImage'
import { Card, Stars, StatusBadge } from './ui'

const STEPS: [string, keyof OrderDetail][] = [
  ['Ordered', 'order_purchase_timestamp'],
  ['Payment approved', 'order_approved_at'],
  ['Shipped', 'order_delivered_carrier_date'],
  ['Delivered', 'order_delivered_customer_date'],
]

function Tracker({ order }: { order: OrderDetail }) {
  const canceled = order.order_status === 'canceled' || order.order_status === 'unavailable'
  const doneCount = STEPS.filter(([, k]) => order[k]).length
  return (
    <ol className="grid grid-cols-4 gap-2">
      {STEPS.map(([label, key], i) => {
        const done = Boolean(order[key])
        const current = !done && i === doneCount && !canceled
        return (
          <li key={label} className="relative flex flex-col items-center text-center">
            {i > 0 && (
              <span className={`absolute right-1/2 top-4 h-0.5 w-full -translate-y-1/2 ${done ? 'bg-brand-500' : 'bg-slate-200'}`} aria-hidden />
            )}
            <span
              className={`relative z-10 flex h-8 w-8 items-center justify-center rounded-full ${done ? 'bg-brand-500 text-white' : current ? 'border-2 border-brand-500 bg-white text-brand-500' : 'border-2 border-slate-200 bg-white text-slate-300'}`}
            >
              {done ? <Check size={16} /> : <CircleDot size={14} />}
            </span>
            <span className={`mt-2 text-xs font-medium sm:text-sm ${done ? 'text-slate-900' : 'text-slate-400'}`}>{label}</span>
            <span className="text-xs text-slate-500">{date(order[key] as string | null)}</span>
          </li>
        )
      })}
    </ol>
  )
}

export default function OrderView({ order, admin = false }: { order: OrderDetail; admin?: boolean }) {
  const itemsSubtotal = order.items.reduce((s, i) => s + i.price, 0)
  const shippingTotal = order.items.reduce((s, i) => s + i.freight, 0)
  const itemsTotal = itemsSubtotal + shippingTotal
  const paid = order.payments.reduce((s, p) => s + p.amount, 0)
  const verdict = deliveryVerdict(order.order_delivered_customer_date, order.order_estimated_delivery_date)
  // Olist stores quantity as repeated rows; group them for display.
  const grouped = Object.values(
    order.items.reduce<Record<string, (typeof order.items)[number] & { qty: number }>>((acc, i) => {
      acc[i.product_id] = acc[i.product_id] ? { ...acc[i.product_id], qty: acc[i.product_id].qty + 1 } : { ...i, qty: 1 }
      return acc
    }, {}),
  )

  return (
    <div className="space-y-6">
      <Card>
        <div className="mb-6 flex flex-wrap items-start justify-between gap-3">
          <div>
            <div className="text-sm text-slate-500">Order placed {date(order.order_purchase_timestamp)}</div>
            <div className="font-mono text-sm text-slate-700">#{order.order_id}</div>
            {admin && (
              <div className="mt-1 text-sm text-slate-500">
                Customer <Link className="font-mono text-brand-600 hover:underline" to={`/admin/customers?id=${order.customer_unique_id}`}>{order.customer_unique_id}</Link> · {order.customer_city}, {order.customer_state}
              </div>
            )}
          </div>
          <div className="text-right">
            <StatusBadge status={order.order_status} />
            <div className={`mt-1 text-sm font-medium ${verdict.tone === 'good' ? 'text-emerald-600' : verdict.tone === 'bad' ? 'text-rose-600' : 'text-slate-500'}`}>
              {verdict.text}
            </div>
            <div className="text-xs text-slate-500">Estimated {date(order.order_estimated_delivery_date)}</div>
          </div>
        </div>
        <Tracker order={order} />
      </Card>

      <div className="grid gap-6 lg:grid-cols-3">
        <Card className="lg:col-span-2">
          <h2 className="mb-4 text-lg font-semibold">Items</h2>
          {grouped.length === 0 && <p className="text-slate-500">No items recorded for this order.</p>}
          <ul className="divide-y divide-slate-100">
            {grouped.map((i) => (
              <li key={i.product_id} className="flex gap-4 py-3">
                <Link to={admin ? `/admin/products/${i.product_id}` : `/product/${i.product_id}`} className="shrink-0">
                  <ProductImage productId={i.product_id} category={i.category} className="h-16 w-16 rounded-lg" iconSize={28} />
                </Link>
                <div className="min-w-0 flex-1">
                  <Link to={admin ? `/admin/products/${i.product_id}` : `/product/${i.product_id}`} className="font-medium text-slate-800 hover:text-brand-600">{i.product}</Link>
                  <div className="text-sm text-slate-500">{categoryLabel(i.category)} · sold by a seller in {i.seller_city}, {i.seller_state}</div>
                  <div className="text-sm text-slate-500">
                    Qty {i.qty}{i.qty > 1 && <> · {money(i.price)} each</>}
                  </div>
                </div>
                <div className="text-right text-sm">
                  <div className="font-semibold text-slate-900">{money(i.price * i.qty)}</div>
                  <div className="text-slate-500">
                    + {money(i.freight * i.qty)} shipping
                    {i.qty > 1 && <span className="block text-xs">({i.qty} × {money(i.freight)})</span>}
                  </div>
                </div>
              </li>
            ))}
          </ul>
        </Card>

        <div className="space-y-6">
          <Card>
            <h2 className="mb-3 text-lg font-semibold">Payment</h2>
            <dl className="space-y-1.5 text-sm">
              <div className="flex justify-between"><dt className="text-slate-500">Items subtotal</dt><dd>{money(itemsSubtotal)}</dd></div>
              <div className="flex justify-between"><dt className="text-slate-500">Shipping</dt><dd>{money(shippingTotal)}</dd></div>
              <div className="flex justify-between border-t border-slate-100 pt-2 font-semibold"><dt>Order total</dt><dd>{money(itemsTotal)}</dd></div>
            </dl>
            <h3 className="mb-1.5 mt-4 text-xs font-semibold uppercase tracking-wide text-slate-400">Paid with</h3>
            <dl className="space-y-1.5 text-sm">
              {order.payments.map((p) => (
                <div key={p.seq} className="flex justify-between">
                  <dt className="text-slate-500">
                    {PAYMENT_LABELS[p.type] ?? p.type}
                    {p.installments > 1 ? ` · ${p.installments}× of ${money(p.amount / p.installments)}` : ''}
                  </dt>
                  <dd>{money(p.amount)}</dd>
                </div>
              ))}
              {Math.abs(paid - itemsTotal) >= 0.01 && (
                <div className="flex justify-between border-t border-slate-100 pt-2 text-xs text-slate-500">
                  <dt>Total paid ({paid > itemsTotal ? 'incl. card interest/fees' : 'after vouchers/discounts'})</dt>
                  <dd>{money(paid)}</dd>
                </div>
              )}
            </dl>
          </Card>

          <Card>
            <h2 className="mb-3 flex items-center gap-2 text-lg font-semibold"><MapPin size={18} /> Ship to</h2>
            {order.shipping_address ? (
              <AddressLines a={order.shipping_address} />
            ) : (
              <p className="text-sm capitalize text-slate-600">{order.customer_city}, {order.customer_state.toUpperCase()}</p>
            )}
          </Card>

          <Card>
            <h2 className="mb-3 flex items-center gap-2 text-lg font-semibold"><Star size={18} /> Review</h2>
            {order.review ? (
              <>
                <div className="flex items-center gap-2"><Stars value={order.review.review_score} /> <span className="text-sm text-slate-500">{date(order.review.review_creation_date)}</span></div>
                {order.review.review_comment_title && <div className="mt-2 font-semibold">{order.review.review_comment_title}</div>}
                {order.review.review_comment_message && <p className="mt-1 text-sm text-slate-600">{order.review.review_comment_message}</p>}
              </>
            ) : (
              <p className="text-sm text-slate-500">No review for this order.</p>
            )}
          </Card>
        </div>
      </div>
    </div>
  )
}

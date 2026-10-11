import { Minus, Plus, ShoppingBag, Trash2 } from 'lucide-react'
import { Link, useNavigate } from 'react-router-dom'
import ProductImage from '../components/ProductImage'
import { EmptyState } from '../components/ui'
import { categoryLabel, money } from '../lib'
import { useStore } from '../state'

export function OrderSummaryBox({ children }: { children?: React.ReactNode }) {
  const { cart } = useStore()
  return (
    <div className="rounded-2xl border border-slate-200 bg-white p-6">
      <h2 className="mb-4 text-lg font-semibold text-slate-900">Order summary</h2>
      <dl className="space-y-2 text-sm">
        <div className="flex justify-between"><dt className="text-slate-500">Items ({cart.count})</dt><dd>{money(cart.subtotal)}</dd></div>
        <div className="flex justify-between"><dt className="text-slate-500">Shipping</dt><dd>{money(cart.shipping)}</dd></div>
        <div className="flex justify-between border-t border-slate-100 pt-3 text-base font-bold text-slate-900"><dt>Total</dt><dd>{money(cart.total)}</dd></div>
      </dl>
      {children}
    </div>
  )
}

export default function CartPage() {
  const { cart, setQuantity, removeFromCart } = useStore()
  const navigate = useNavigate()

  if (cart.items.length === 0) {
    return (
      <EmptyState icon={<ShoppingBag size={48} />} title="Your cart is empty">
        <p>Find something you love.</p>
        <Link to="/" className="mt-4 inline-block rounded-xl bg-brand-500 px-6 py-3 font-semibold text-white hover:bg-brand-600">Start shopping</Link>
      </EmptyState>
    )
  }

  return (
    <>
      <h1 className="mb-6 text-3xl font-bold text-slate-900">Shopping cart</h1>
      <div className="grid gap-6 lg:grid-cols-3">
        <div className="space-y-3 lg:col-span-2">
          {cart.items.map((item) => (
            <div key={item.product_id} className="flex gap-4 rounded-2xl border border-slate-200 bg-white p-4">
              <Link to={`/product/${item.product_id}`} className="shrink-0">
                <ProductImage productId={item.product_id} category={item.category} className="h-24 w-24 rounded-xl" iconSize={36} />
              </Link>
              <div className="flex min-w-0 flex-1 flex-col">
                <div className="flex justify-between gap-4">
                  <div className="min-w-0">
                    <Link to={`/product/${item.product_id}`} className="font-semibold text-slate-800 hover:text-brand-600">{item.name}</Link>
                    <div className="text-sm text-slate-500">{categoryLabel(item.category)}</div>
                  </div>
                  <div className="text-right">
                    <div className="font-bold text-slate-900">{money(item.price * item.quantity)}</div>
                    {item.quantity > 1 && <div className="text-xs text-slate-500">{money(item.price)} each</div>}
                  </div>
                </div>
                <div className="mt-auto flex items-center justify-between pt-3">
                  <div className="flex items-center rounded-lg border border-slate-200">
                    <button className="p-2 hover:bg-slate-50" onClick={() => setQuantity(item.product_id, item.quantity - 1)} aria-label="Decrease quantity"><Minus size={14} /></button>
                    <span className="w-8 text-center text-sm font-semibold">{item.quantity}</span>
                    <button className="p-2 hover:bg-slate-50 disabled:opacity-30" disabled={item.quantity >= 20} onClick={() => setQuantity(item.product_id, item.quantity + 1)} aria-label="Increase quantity"><Plus size={14} /></button>
                  </div>
                  <button onClick={() => removeFromCart(item.product_id)} className="flex items-center gap-1 text-sm text-slate-500 hover:text-rose-600">
                    <Trash2 size={14} /> Remove
                  </button>
                </div>
              </div>
            </div>
          ))}
        </div>
        <div>
          <div className="sticky top-36">
            <OrderSummaryBox>
              <button onClick={() => navigate('/checkout')} className="mt-6 w-full rounded-xl bg-brand-500 py-3 font-semibold text-white hover:bg-brand-600">
                Proceed to checkout
              </button>
              <Link to="/" className="mt-3 block text-center text-sm text-brand-600 hover:underline">Continue shopping</Link>
            </OrderSummaryBox>
          </div>
        </div>
      </div>
    </>
  )
}

import { CreditCard, Gift, Landmark, Loader2, MapPin, Wallet } from 'lucide-react'
import { useState } from 'react'
import { Navigate, useNavigate } from 'react-router-dom'
import { api, ApiError } from '../api'
import { AddressEditor } from '../components/Address'
import { money } from '../lib'
import { useStore } from '../state'
import { OrderSummaryBox } from './CartPage'

const METHODS = [
  { id: 'credit_card', label: 'Credit card', sub: 'Pay in up to 12 installments', icon: CreditCard },
  { id: 'debit_card', label: 'Debit card', sub: 'Paid immediately', icon: Wallet },
  { id: 'boleto', label: 'Boleto', sub: 'Bank slip, approved in 1–2 days', icon: Landmark },
  { id: 'voucher', label: 'Voucher', sub: 'Use a gift voucher', icon: Gift },
]

export default function Checkout() {
  const { cart, refreshCart, notify } = useStore()
  const [hasAddress, setHasAddress] = useState(false)
  const [method, setMethod] = useState('credit_card')
  const [installments, setInstallments] = useState(1)
  const [placing, setPlacing] = useState(false)
  const navigate = useNavigate()

  if (cart.items.length === 0 && !placing) return <Navigate to="/cart" replace />

  const place = async () => {
    setPlacing(true)
    try {
      const { order_id } = await api.checkout(method, method === 'credit_card' ? installments : 1)
      await refreshCart()
      navigate(`/orders/${order_id}?placed=1`, { replace: true })
    } catch (e) {
      notify(e instanceof ApiError ? e.message : 'Could not place order', 'error')
      setPlacing(false)
    }
  }

  return (
    <>
      <h1 className="mb-6 text-3xl font-bold text-slate-900">Checkout</h1>
      <div className="grid gap-6 lg:grid-cols-3">
        <div className="space-y-6 lg:col-span-2">
          <section className="rounded-2xl border border-slate-200 bg-white p-6">
            <h2 className="mb-3 flex items-center gap-2 text-lg font-semibold"><MapPin size={20} className="text-brand-500" /> Delivery address</h2>
            <AddressEditor onChange={(a) => setHasAddress(a.is_saved)} />
            <p className="mt-3 text-sm text-slate-500">Estimated delivery in about 12 days.</p>
          </section>

          <section className="rounded-2xl border border-slate-200 bg-white p-6">
            <h2 className="mb-4 text-lg font-semibold">Payment method</h2>
            <div className="grid gap-3 sm:grid-cols-2">
              {METHODS.map((m) => (
                <label
                  key={m.id}
                  className={`flex cursor-pointer items-start gap-3 rounded-xl border p-4 transition ${method === m.id ? 'border-brand-500 bg-brand-50 ring-2 ring-brand-100' : 'border-slate-200 hover:border-slate-300'}`}
                >
                  <input type="radio" name="method" value={m.id} checked={method === m.id} onChange={() => setMethod(m.id)} className="mt-1 accent-brand-500" />
                  <m.icon size={22} className="text-brand-600" />
                  <span>
                    <span className="block font-semibold text-slate-800">{m.label}</span>
                    <span className="text-sm text-slate-500">{m.sub}</span>
                  </span>
                </label>
              ))}
            </div>
            {method === 'credit_card' && (
              <div className="mt-4">
                <label className="mb-1 block text-sm font-medium text-slate-700" htmlFor="inst">Installments</label>
                <select
                  id="inst"
                  value={installments}
                  onChange={(e) => setInstallments(Number(e.target.value))}
                  className="w-full rounded-xl border border-slate-200 px-3 py-2 sm:w-72"
                >
                  {Array.from({ length: 12 }, (_, i) => i + 1).map((n) => (
                    <option key={n} value={n}>{n}× of {money(cart.total / n)}{n === 1 ? ' (full payment)' : ' interest-free'}</option>
                  ))}
                </select>
              </div>
            )}
            <p className="mt-4 text-xs text-slate-400">Demo store: no real payment is taken. Placing the order writes it to the database.</p>
          </section>
        </div>

        <div>
          <div className="sticky top-36">
            <OrderSummaryBox>
              <button
                onClick={place}
                disabled={placing || !hasAddress}
                className="mt-6 flex w-full items-center justify-center gap-2 rounded-xl bg-accent-500 py-3 font-semibold text-white hover:brightness-95 disabled:opacity-60"
              >
                {placing && <Loader2 size={18} className="animate-spin" />} Place order · {money(cart.total)}
              </button>
              {!hasAddress && <p className="mt-2 text-center text-xs text-amber-700">Save a delivery address to place your order.</p>}
            </OrderSummaryBox>
          </div>
        </div>
      </div>
    </>
  )
}

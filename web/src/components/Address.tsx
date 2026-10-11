// Shipping address: read-only card, edit form, and a self-loading editor used
// on the Account and Checkout pages.

import { Loader2, MapPin, Pencil } from 'lucide-react'
import { useEffect, useState } from 'react'
import { api, ApiError, type Address, type SavedAddress } from '../api'
import { useStore } from '../state'
import { Spinner } from './ui'

const STATES = ['AC', 'AL', 'AP', 'AM', 'BA', 'CE', 'DF', 'ES', 'GO', 'MA', 'MT', 'MS', 'MG', 'PA',
  'PB', 'PR', 'PE', 'PI', 'RJ', 'RN', 'RS', 'RO', 'RR', 'SC', 'SP', 'SE', 'TO']

const field = 'w-full rounded-xl border border-slate-200 px-3 py-2 text-sm outline-none focus:border-brand-500 focus:ring-4 focus:ring-brand-100'

export function AddressLines({ a }: { a: Address }) {
  return (
    <address className="not-italic text-sm leading-relaxed text-slate-700">
      <span className="font-semibold text-slate-900">{a.recipient_name}</span>
      {a.phone && <span className="text-slate-500"> · {a.phone}</span>}
      <br />
      {a.street}, {a.number}{a.complement ? ` – ${a.complement}` : ''}
      <br />
      {a.neighborhood ? `${a.neighborhood}, ` : ''}{a.city} – {a.state}, {a.zip_code}
    </address>
  )
}

function AddressForm({ initial, onSaved, onCancel }: {
  initial: SavedAddress
  onSaved: (a: SavedAddress) => void
  onCancel?: () => void
}) {
  const { notify } = useStore()
  const [form, setForm] = useState({
    recipient_name: initial.recipient_name ?? '',
    phone: initial.phone ?? '',
    street: initial.street ?? '',
    number: initial.number ?? '',
    complement: initial.complement ?? '',
    neighborhood: initial.neighborhood ?? '',
    city: initial.city ?? '',
    state: initial.state ?? '',
    // Olist only knows the first 5 digits of the CEP; the customer completes it.
    zip_code: initial.is_saved ? initial.zip_code ?? '' : initial.zip_code ? `${initial.zip_code}-` : '',
  })
  const [errors, setErrors] = useState<Record<string, string>>({})
  const [saving, setSaving] = useState(false)
  const set = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
    setForm({ ...form, [k]: e.target.value })

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    setSaving(true)
    setErrors({})
    try {
      const saved = await api.saveAddress({
        ...form,
        phone: form.phone || null,
        complement: form.complement || null,
        neighborhood: form.neighborhood || null,
      })
      notify('Address saved')
      onSaved(saved)
    } catch (err) {
      if (err instanceof ApiError && err.status === 422) {
        // FastAPI validation errors: [{loc: [..., field], msg}]
        try {
          const list = JSON.parse(err.message) as { loc: string[]; msg: string }[]
          setErrors(Object.fromEntries(list.map((x) => [x.loc[x.loc.length - 1], x.msg.replace(/^Value error, /, '')])))
        } catch {
          notify('Please check the address fields', 'error')
        }
      } else {
        notify(err instanceof ApiError ? err.message : 'Could not save address', 'error')
      }
    } finally {
      setSaving(false)
    }
  }

  const input = (k: keyof typeof form, label: string, props: React.InputHTMLAttributes<HTMLInputElement> = {}, span = '') => (
    <label className={`block ${span}`}>
      <span className="mb-1 block text-xs font-medium text-slate-600">{label}</span>
      <input className={`${field} ${errors[k] ? 'border-rose-400' : ''}`} value={form[k]} onChange={set(k)} {...props} />
      {errors[k] && <span className="mt-0.5 block text-xs text-rose-600">{errors[k]}</span>}
    </label>
  )

  return (
    <form onSubmit={submit} className="grid grid-cols-6 gap-3">
      {input('recipient_name', 'Full name *', { required: true, autoComplete: 'name' }, 'col-span-6 sm:col-span-4')}
      {input('phone', 'Phone', { autoComplete: 'tel', type: 'tel' }, 'col-span-6 sm:col-span-2')}
      {input('zip_code', 'CEP (zip code) *', { required: true, placeholder: '00000-000', inputMode: 'numeric', autoComplete: 'postal-code' }, 'col-span-6 sm:col-span-2')}
      {input('street', 'Street *', { required: true, autoComplete: 'address-line1' }, 'col-span-6 sm:col-span-4')}
      {input('number', 'Number *', { required: true }, 'col-span-2')}
      {input('complement', 'Apt, suite (optional)', { autoComplete: 'address-line2' }, 'col-span-4')}
      {input('neighborhood', 'Neighborhood', {}, 'col-span-6 sm:col-span-2')}
      {input('city', 'City *', { required: true, autoComplete: 'address-level2' }, 'col-span-4 sm:col-span-3')}
      <label className="col-span-2 block sm:col-span-1">
        <span className="mb-1 block text-xs font-medium text-slate-600">State *</span>
        <select className={`${field} ${errors.state ? 'border-rose-400' : ''}`} value={form.state} onChange={set('state')} required>
          <option value="">–</option>
          {STATES.map((s) => <option key={s}>{s}</option>)}
        </select>
        {errors.state && <span className="mt-0.5 block text-xs text-rose-600">{errors.state}</span>}
      </label>
      <div className="col-span-6 flex gap-2 pt-1">
        <button disabled={saving} className="flex items-center gap-2 rounded-xl bg-brand-500 px-5 py-2.5 text-sm font-semibold text-white hover:bg-brand-600 disabled:opacity-60">
          {saving && <Loader2 size={16} className="animate-spin" />} Save address
        </button>
        {onCancel && (
          <button type="button" onClick={onCancel} className="rounded-xl px-4 py-2.5 text-sm font-medium text-slate-600 hover:bg-slate-100">Cancel</button>
        )}
      </div>
    </form>
  )
}

/** Loads the customer's address and lets them view or edit it. */
export function AddressEditor({ onChange }: { onChange?: (a: SavedAddress) => void }) {
  const [address, setAddress] = useState<SavedAddress | null>(null)
  const [editing, setEditing] = useState(false)

  useEffect(() => {
    api.address().then((a) => {
      setAddress(a)
      setEditing(!a.is_saved)
      onChange?.(a)
    })
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  if (!address) return <Spinner label="Loading address…" />

  if (editing) {
    return (
      <>
        {!address.is_saved && (
          <p className="mb-3 text-sm text-slate-500">Add your full delivery address. We've filled in what we already know.</p>
        )}
        <AddressForm
          initial={address}
          onSaved={(a) => {
            setAddress(a)
            setEditing(false)
            onChange?.(a)
          }}
          onCancel={address.is_saved ? () => setEditing(false) : undefined}
        />
      </>
    )
  }

  return (
    <div className="flex items-start justify-between gap-4">
      <div className="flex gap-3">
        <MapPin size={18} className="mt-0.5 shrink-0 text-brand-500" />
        <AddressLines a={address as Address} />
      </div>
      <button onClick={() => setEditing(true)} className="flex shrink-0 items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm font-medium text-brand-600 hover:bg-brand-50">
        <Pencil size={14} /> Edit
      </button>
    </div>
  )
}

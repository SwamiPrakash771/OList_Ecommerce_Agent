// Formatting helpers shared across pages.

// Amounts are shown with a $ sign as stored (no exchange-rate conversion).
export const money = (v: number | null | undefined) =>
  v === null || v === undefined ? '—' : v.toLocaleString('en-US', { style: 'currency', currency: 'USD' })

export const date = (v: string | null | undefined) =>
  v ? new Date(v).toLocaleDateString('en-GB', { day: '2-digit', month: 'short', year: 'numeric' }) : '—'

/** Every sign-in lands on the store home. For sellers the store shows only
 *  their own products (the API scopes it), so it doubles as their shop home. */
export const homeFor = (_role?: string) => '/'

export const categoryLabel = (c: string) => {
  const s = c.replace(/_/g, ' ')
  return s.charAt(0).toUpperCase() + s.slice(1)
}

// Deterministic hue per product so its generated image stays the same everywhere.
export function hueFor(id: string) {
  let h = 0
  for (let i = 0; i < id.length; i++) h = (h * 31 + id.charCodeAt(i)) % 360
  return h
}

export function deliveryVerdict(delivered: string | null, estimated: string | null) {
  if (!delivered) return { text: 'Not delivered yet', tone: 'neutral' as const }
  if (!estimated) return { text: 'Delivered', tone: 'good' as const }
  const day = 86_400_000
  const diff = Math.floor(new Date(estimated).setHours(0, 0, 0, 0) / day) -
    Math.floor(new Date(delivered).setHours(0, 0, 0, 0) / day)
  if (diff > 0) return { text: `On time · ${diff} day${diff === 1 ? '' : 's'} early`, tone: 'good' as const }
  if (diff === 0) return { text: 'On time', tone: 'good' as const }
  return { text: `Late by ${-diff} day${diff === -1 ? '' : 's'}`, tone: 'bad' as const }
}

export const PAYMENT_LABELS: Record<string, string> = {
  credit_card: 'Credit card',
  debit_card: 'Debit card',
  boleto: 'Boleto',
  voucher: 'Voucher',
}

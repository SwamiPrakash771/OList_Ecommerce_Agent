// Typed wrapper around the FastAPI backend. Cookies carry the session.

export type Product = {
  product_id: string
  name: string
  category: string
  price: number
  shipping: number
  rating: number | null
  reviews: number
  sold: number
}

export type ProductDetail = {
  product_id: string
  display_name: string
  category: string
  times_sold: number
  typical_price: number
  avg_freight: number
  avg_rating: number | null
  num_reviews: number
  photos: number | null
  weight_g: number | null
  length_cm: number | null
  height_cm: number | null
  width_cm: number | null
  seller_city: string | null
  seller_state: string | null
  seller_rating: number | null
  seller_on_time_pct: number | null
  seller_orders: number | null
  reviews: { score: number; title: string | null; comment: string; date: string }[]
  related: Product[]
}

export type Category = { category: string; products: number; from_price: number; sold: number }

export type CartItem = {
  product_id: string
  name: string
  category: string
  price: number
  shipping: number
  rating: number | null
  quantity: number
}
export type Cart = { items: CartItem[]; count: number; subtotal: number; shipping: number; total: number }

export type Profile = {
  customer_unique_id: string
  city: string
  state: string
  zip_code: string
  orders: number
  first_order: string
  last_order: string
}
export type SellerProfile = {
  seller_id: string
  city: string
  state: string
  zip_code: string
  orders_fulfilled: number | null
  avg_rating: number | null
  on_time_pct: number | null
}
export type Role = 'customer' | 'admin' | 'seller'

// AI settings (admin): how each role's assistant answers
export type AnswerMode = 'llm' | 'hybrid'
export type AIRoleConfig = {
  role: 'customer' | 'seller'
  answer_mode: AnswerMode
  model: string | null
  effective_model: string | null
  max_questions_per_chat: number | null // null = unlimited
  history_full_turns: number | null // earlier turns sent in full; null = unlimited
  history_qa_turns: number | null // earlier turns sent as question + answer; null = unlimited
  assistant_live: boolean
  hybrid_supported: boolean
  note: string
}
export type SystemHealth = {
  database: boolean
  agent_database: boolean
  pool_limits: { app: number; agent: number }
  llm: { status: string; paused_for_s: number; recent_failures: number; last_error: { category: string; seconds_ago: number; detail: string | null } | null }
  fallback_model: string | null
  local_model_loaded: boolean
}
export type AISettings = {
  roles: AIRoleConfig[]
  options: {
    answer_modes: { value: AnswerMode; label: string; description: string }[]
    models: { value: string | null; label: string }[]
  }
  local_model_loaded: boolean
  guests_allowed: boolean
  usage_7_days: { role: string; replies: number; local_replies: number; input_tokens: number; output_tokens: number; cost_usd: number }[]
}
export type User = {
  username: string
  role: Role
  must_change_password: boolean
  /** Customer profile for customers, seller profile for sellers, null for admins. */
  profile: (Profile & Partial<SellerProfile>) | null
}

export type DemoAccount = { username: string; role: Role; label: string; orders: number | null }

export type Address = {
  recipient_name: string
  phone: string | null
  street: string
  number: string
  complement: string | null
  neighborhood: string | null
  city: string
  state: string
  zip_code: string
}
export type SavedAddress = { [K in keyof Address]: Address[K] | null } & { is_saved: boolean; updated_at?: string }

export type EditableProduct = {
  product_id: string
  name: string
  category: string
  price: number
  shipping: number
  weight_g: number | null
  length_cm: number | null
  height_cm: number | null
  width_cm: number | null
  times_sold: number
  avg_rating: number | null
  num_reviews: number
  seller_id: string
}
export type ProductEditResponse = {
  product: EditableProduct
  categories: string[]
  history: {
    edited_by: string
    editor_role: string
    changes: Record<string, { from: unknown; to: unknown }>
    edited_at: string
  }[]
}
export type ProductPage = { items: Product[]; total: number; page: number; page_size: number }
export type SellerOrder = OrderSummary & { ship_by: string | null; customer_city: string; customer_state: string }

export type OrderSummary = {
  order_id: string
  purchased_at: string
  status: string
  items: number
  order_total: number
  estimated_delivery: string | null
  delivered_at: string | null
  customer_unique_id?: string
}

export type OrderDetail = {
  order_id: string
  order_status: string
  order_purchase_timestamp: string
  order_approved_at: string | null
  order_delivered_carrier_date: string | null
  order_delivered_customer_date: string | null
  order_estimated_delivery_date: string | null
  customer_unique_id: string
  customer_city: string
  customer_state: string
  items: {
    item: number
    product: string
    category: string
    price: number
    freight: number
    seller_city: string
    seller_state: string
    product_id: string
  }[]
  payments: { seq: number; type: string; installments: number; amount: number }[]
  review: {
    review_score: number
    review_comment_title: string | null
    review_comment_message: string | null
    review_creation_date: string
  } | null
  shipping_address: Address | null
}

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

// Tell the "temporarily unavailable" banner (components/ServiceBanner.tsx) what happened.
const signal = (name: string, detail?: string) => window.dispatchEvent(new CustomEvent(name, { detail }))

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  let res: Response
  try {
    res = await fetch(path, {
      credentials: 'include',
      headers: init.body ? { 'Content-Type': 'application/json' } : undefined,
      ...init,
    })
  } catch {
    const message = "Can't reach the store right now. Check your connection and try again."
    signal('olist:service-unavailable', message)
    throw new ApiError(0, message)
  }
  if (res.status === 503) {
    const body = await res.json().catch(() => ({}))
    const message = typeof body.detail === 'string' ? body.detail : 'The store is temporarily unavailable.'
    signal('olist:service-unavailable', message)
    throw new ApiError(503, message)
  }
  signal('olist:service-ok')
  if (!res.ok) {
    let message = res.statusText
    try {
      const body = await res.json()
      message = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)
    } catch {
      /* keep statusText */
    }
    throw new ApiError(res.status, message)
  }
  return res.json() as Promise<T>
}

const json = (method: string, body?: unknown): RequestInit => ({
  method,
  body: body === undefined ? undefined : JSON.stringify(body),
})

function qs(params: Record<string, string | number | string[] | null | undefined>): string {
  const sp = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === '') continue
    if (Array.isArray(v)) v.forEach((x) => sp.append(k, x))
    else sp.set(k, String(v))
  }
  const s = sp.toString()
  return s ? `?${s}` : ''
}

let categoriesRequest: Promise<Category[]> | null = null

/** Call when the signed-in account changes: a seller's catalog differs from everyone else's. */
export function resetCatalogCache() {
  categoriesRequest = null
}

export type ProductQuery = {
  q?: string
  category?: string
  min_price?: number
  max_price?: number
  min_rating?: number
  sort?: string
  page?: number
  page_size?: number
  /** bypass the browser's 5-minute cache (admin views right after an edit) */
  _?: number
}

export const api = {
  // auth
  me: () => request<User>('/api/auth/me'),
  login: (username: string, password: string) => request<User>('/api/auth/login', json('POST', { username, password })),
  demoAccounts: () => request<DemoAccount[]>('/api/auth/demo-accounts'),
  demoLogin: (username: string) => request<User>('/api/auth/demo-login', json('POST', { username })),
  logout: () => request<{ ok: boolean }>('/api/auth/logout', json('POST')),
  changePassword: (current: string, next: string) =>
    request<{ ok: boolean }>('/api/auth/change-password', json('POST', { current, new: next })),

  // catalog — categories never change, so every page shares one request
  categories: () => (categoriesRequest ??= request<Category[]>('/api/categories').catch((e) => {
    categoriesRequest = null
    throw e
  })),
  products: (q: ProductQuery) =>
    request<{ items: Product[]; total: number; page: number; page_size: number }>(`/api/products${qs(q)}`),
  product: (id: string) => request<ProductDetail>(`/api/products/${id}`),

  // shipping address
  address: () => request<SavedAddress>('/api/address'),
  saveAddress: (a: Address) => request<SavedAddress>('/api/address', json('PUT', a)),

  // cart & orders
  cart: () => request<Cart>('/api/cart'),
  addToCart: (product_id: string, quantity = 1) => request<Cart>('/api/cart/items', json('POST', { product_id, quantity })),
  setQuantity: (product_id: string, quantity: number) =>
    request<Cart>(`/api/cart/items/${product_id}`, json('PATCH', { quantity })),
  removeFromCart: (product_id: string) => request<Cart>(`/api/cart/items/${product_id}`, json('DELETE')),
  checkout: (payment_type: string, installments: number) =>
    request<{ order_id: string }>('/api/checkout', json('POST', { payment_type, installments })),
  orders: () =>
    request<{ summary: { orders: number; total_paid: number; avg_review_given: number | null }; orders: OrderSummary[] }>(
      '/api/orders',
    ),
  order: (id: string) => request<OrderDetail>(`/api/orders/${id}`),

  // admin
  admin: {
    dashboard: () =>
      request<{
        kpis: { orders: number; customers: number; sellers: number; revenue: number; avg_review: number; on_time_pct: number }
        monthly_revenue: { month: string; orders: number; revenue: number }[]
        orders_by_status: { status: string; orders: number }[]
        top_categories: { category: string; revenue: number; items_sold: number }[]
        computed_at: number
      }>('/api/admin/dashboard'),
    orders: (p: { order_id?: string; customer?: string; status?: string[]; date_from?: string; date_to?: string }) =>
      request<{ statuses: string[]; orders: OrderSummary[] }>(`/api/admin/orders${qs(p)}`),
    order: (id: string) => request<OrderDetail>(`/api/admin/orders/${id}`),
    topCustomers: () =>
      request<{ customer_unique_id: string; city: string; state: string; orders: number; total_paid: number }[]>(
        '/api/admin/customers',
      ),
    customer: (id: string) =>
      request<{
        profile: Profile
        summary: { orders: number; total_paid: number; avg_review_given: number | null }
        orders: OrderSummary[]
        address: Address | null
      }>(`/api/admin/customers/${id}`),
    sellers: (p: { state?: string; min_orders?: number }) =>
      request<{
        states: string[]
        sellers: {
          seller_id: string
          seller_city: string
          seller_state: string
          orders_fulfilled: number
          avg_rating: number | null
          on_time_pct: number | null
        }[]
      }>(`/api/admin/sellers${qs(p)}`),
    users: (p: { role?: string; search?: string }) =>
      request<
        {
          user_id: number
          username: string
          role: string
          customer_unique_id: string | null
          must_change_password: number
          is_active: number
          created_at: string
          last_login: string | null
        }[]
      >(`/api/admin/users${qs(p)}`),
    resetPassword: (username: string) =>
      request<{ temporary_password: string }>('/api/admin/users/reset-password', json('POST', { username })),
    createAdmin: (username: string, password: string) =>
      request<{ ok: boolean }>('/api/admin/users', json('POST', { username, password })),
    tickets: () => request<Record<string, unknown>[]>('/api/admin/tickets'),
    aiSettings: () => request<AISettings>('/api/admin/ai-settings'),
    systemHealth: () => request<SystemHealth>('/api/admin/system-health'),
    resetLLM: () => request<SystemHealth['llm']>('/api/admin/system-health/reset-llm', json('POST')),
    setGuestAccess: (enabled: boolean) =>
      request<{ guests_allowed: boolean }>('/api/admin/ai-settings/guest-access', json('PUT', { enabled })),
    updateAISettings: (
      role: string,
      settings: {
        answer_mode: AnswerMode
        model: string | null
        max_questions_per_chat: number | null
        history_full_turns: number | null
        history_qa_turns: number | null
      },
    ) => request<AIRoleConfig>(`/api/admin/ai-settings/${role}`, json('PUT', settings)),
    /** Read-only: products are edited by their seller. */
    product: (id: string) => request<ProductEditResponse>(`/api/admin/products/${id}`),
  },

  // seller center (everything is limited to the signed-in seller)
  seller: {
    dashboard: () =>
      request<{
        profile: SellerProfile
        kpis: { orders: number; items_sold: number; revenue: number; products: number }
        monthly_sales: { month: string; orders: number; revenue: number }[]
        top_products: { product_id: string; name: string; category: string; sold: number; rating: number | null }[]
      }>('/api/seller/dashboard'),
    products: (p: { q?: string; sort?: string; page?: number; page_size?: number; category?: string }) =>
      request<ProductPage>(`/api/seller/products${qs(p)}`),
    categories: () => request<{ category: string; products: number }[]>('/api/seller/categories'),
    product: (id: string) => request<ProductEditResponse>(`/api/seller/products/${id}`),
    updateProduct: (id: string, changes: Partial<EditableProduct>) =>
      request<ProductEditResponse>(`/api/seller/products/${id}`, json('PATCH', changes)),
    createProduct: (product: Partial<EditableProduct>) =>
      request<ProductEditResponse>('/api/seller/products', json('POST', product)),
    orders: (status?: string) => request<{ orders: SellerOrder[] }>(`/api/seller/orders${qs({ status })}`),
  },
}

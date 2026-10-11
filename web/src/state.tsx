// App-wide state: the signed-in user, their cart, and toast messages.

import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from 'react'
import { api, ApiError, resetCatalogCache, type Cart, type User } from './api'

const EMPTY_CART: Cart = { items: [], count: 0, subtotal: 0, shipping: 0, total: 0 }

type Toast = { id: number; text: string; kind: 'success' | 'error' }

type StoreState = {
  user: User | null
  loadingUser: boolean
  login: (username: string, password: string) => Promise<User>
  demoLogin: (username: string) => Promise<User>
  logout: () => Promise<void>
  refreshUser: () => Promise<void>
  cart: Cart
  addToCart: (productId: string, quantity?: number) => Promise<boolean>
  setQuantity: (productId: string, quantity: number) => Promise<void>
  removeFromCart: (productId: string) => Promise<void>
  refreshCart: () => Promise<void>
  toasts: Toast[]
  notify: (text: string, kind?: Toast['kind']) => void
}

const StoreContext = createContext<StoreState | null>(null)

export function StoreProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null)
  const [loadingUser, setLoadingUser] = useState(true)
  const [cart, setCart] = useState<Cart>(EMPTY_CART)
  const [toasts, setToasts] = useState<Toast[]>([])

  const notify = useCallback((text: string, kind: Toast['kind'] = 'success') => {
    const id = Date.now() + Math.random()
    setToasts((t) => [...t, { id, text, kind }])
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 3000)
  }, [])

  const refreshCart = useCallback(async () => {
    try {
      setCart(await api.cart())
    } catch {
      setCart(EMPTY_CART)
    }
  }, [])

  const refreshUser = useCallback(async () => {
    try {
      const me = await api.me()
      setUser(me)
      if (me.role === 'customer') await refreshCart()
    } catch {
      setUser(null)
      setCart(EMPTY_CART)
    } finally {
      setLoadingUser(false)
    }
  }, [refreshCart])

  useEffect(() => {
    refreshUser()
  }, [refreshUser])

  const login = async (username: string, password: string) => {
    const me = await api.login(username, password)
    resetCatalogCache()
    setUser(me)
    if (me.role === 'customer') await refreshCart()
    return me
  }

  const demoLogin = async (username: string) => {
    const me = await api.demoLogin(username)
    resetCatalogCache()
    setUser(me)
    if (me.role === 'customer') await refreshCart()
    else setCart(EMPTY_CART)
    return me
  }

  const logout = async () => {
    await api.logout()
    resetCatalogCache()
    setUser(null)
    setCart(EMPTY_CART)
  }

  const addToCart = async (productId: string, quantity = 1) => {
    try {
      setCart(await api.addToCart(productId, quantity))
      notify('Added to cart')
      return true
    } catch (e) {
      notify(e instanceof ApiError ? e.message : 'Could not add to cart', 'error')
      return false
    }
  }

  const setQuantity = async (productId: string, quantity: number) => {
    setCart(await api.setQuantity(productId, quantity))
  }

  const removeFromCart = async (productId: string) => {
    setCart(await api.removeFromCart(productId))
    notify('Removed from cart')
  }

  return (
    <StoreContext.Provider
      value={{
        user, loadingUser, login, demoLogin, logout, refreshUser,
        cart, addToCart, setQuantity, removeFromCart, refreshCart,
        toasts, notify,
      }}
    >
      {children}
    </StoreContext.Provider>
  )
}

export function useStore() {
  const ctx = useContext(StoreContext)
  if (!ctx) throw new Error('useStore must be used inside StoreProvider')
  return ctx
}

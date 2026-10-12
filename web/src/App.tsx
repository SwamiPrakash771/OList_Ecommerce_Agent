import { lazy, Suspense } from 'react'
import { Navigate, Route, Routes, useLocation } from 'react-router-dom'
import { AssistantPage } from './components/Chat'
import Layout from './components/Layout'
import { Spinner } from './components/ui'
import { Account, Login } from './pages/Auth'
import CartPage from './pages/CartPage'
import Categories from './pages/Categories'
import Checkout from './pages/Checkout'
import Home from './pages/Home'
import { OrderPage, OrdersList } from './pages/Orders'
import ProductPage from './pages/ProductPage'
import Search from './pages/Search'
import { homeFor } from './lib'
import { useStore } from './state'

// The admin area (and its charting library) loads only when an admin opens it.
const admin = () => import('./pages/Admin')
const AdminLayout = lazy(() => admin().then((m) => ({ default: m.AdminLayout })))
const Dashboard = lazy(() => admin().then((m) => ({ default: m.Dashboard })))
const AdminOrders = lazy(() => admin().then((m) => ({ default: m.AdminOrders })))
const AdminOrder = lazy(() => admin().then((m) => ({ default: m.AdminOrder })))
const AdminCustomers = lazy(() => admin().then((m) => ({ default: m.AdminCustomers })))
const AdminSellers = lazy(() => admin().then((m) => ({ default: m.AdminSellers })))
const AdminTickets = lazy(() => admin().then((m) => ({ default: m.AdminTickets })))
const AdminUsers = lazy(() => admin().then((m) => ({ default: m.AdminUsers })))
const AdminAISettings = lazy(() => admin().then((m) => ({ default: m.AdminAISettings })))
const adminProducts = () => import('./pages/AdminProducts')
const AdminProducts = lazy(() => adminProducts().then((m) => ({ default: m.AdminProducts })))
const AdminProductView = lazy(() => adminProducts().then((m) => ({ default: m.AdminProductView })))
// Seller Center, also loaded only when a seller opens it.
const seller = () => import('./pages/Seller')
const SellerLayout = lazy(() => seller().then((m) => ({ default: m.SellerLayout })))
const SellerDashboard = lazy(() => seller().then((m) => ({ default: m.SellerDashboard })))
const SellerProducts = lazy(() => seller().then((m) => ({ default: m.SellerProducts })))
const SellerProductEdit = lazy(() => seller().then((m) => ({ default: m.SellerProductEdit })))
const SellerOrders = lazy(() => seller().then((m) => ({ default: m.SellerOrders })))
const SellerNewProduct = lazy(() => seller().then((m) => ({ default: m.SellerNewProduct })))

// Client-side guard for navigation. The API enforces the same rules on every request.
function RequireRole({ role, children }: { role: 'customer' | 'admin' | 'seller' | 'any'; children: React.ReactNode }) {
  const { user, loadingUser } = useStore()
  const location = useLocation()
  if (loadingUser) return <Spinner />
  if (!user) return <Navigate to="/login" replace state={{ from: location.pathname + location.search }} />
  if (role !== 'any' && user.role !== role) return <Navigate to={user.role === 'admin' ? '/admin' : user.role === 'seller' ? '/seller' : homeFor(user.role)} replace />
  return <>{children}</>
}

export default function App() {
  return (
    <Suspense fallback={<Spinner />}>
    <Routes>
      <Route path="/login" element={<Login />} />

      {/* The store: customers and guests shop; admins and sellers browse without a cart.
          For a signed-in seller the API returns only that seller's products. */}
      <Route element={<Layout />}>
        <Route index element={<Home />} />
        <Route path="categories" element={<Categories />} />
        <Route path="search" element={<Search />} />
        <Route path="product/:id" element={<ProductPage />} />
        <Route path="assistant" element={<AssistantPage />} />
        <Route path="cart" element={<RequireRole role="customer"><CartPage /></RequireRole>} />
        <Route path="checkout" element={<RequireRole role="customer"><Checkout /></RequireRole>} />
        <Route path="orders" element={<RequireRole role="customer"><OrdersList /></RequireRole>} />
        <Route path="orders/:id" element={<RequireRole role="customer"><OrderPage /></RequireRole>} />
        <Route path="account" element={<RequireRole role="customer"><Account /></RequireRole>} />
      </Route>

      <Route path="admin" element={<RequireRole role="admin"><AdminLayout /></RequireRole>}>
        <Route index element={<Dashboard />} />
        <Route path="orders" element={<AdminOrders />} />
        <Route path="orders/:id" element={<AdminOrder />} />
        <Route path="customers" element={<AdminCustomers />} />
        <Route path="sellers" element={<AdminSellers />} />
        <Route path="tickets" element={<AdminTickets />} />
        <Route path="users" element={<AdminUsers />} />
        <Route path="ai-settings" element={<AdminAISettings />} />
        <Route path="products" element={<AdminProducts />} />
        <Route path="products/:id" element={<AdminProductView />} />
        <Route path="assistant" element={<AssistantPage />} />
        <Route path="account" element={<Account />} />
      </Route>

      <Route path="seller" element={<RequireRole role="seller"><SellerLayout /></RequireRole>}>
        <Route index element={<SellerDashboard />} />
        <Route path="assistant" element={<AssistantPage />} />
        <Route path="products" element={<SellerProducts />} />
        <Route path="products/new" element={<SellerNewProduct />} />
        <Route path="products/:id" element={<SellerProductEdit />} />
        <Route path="orders" element={<SellerOrders />} />
        <Route path="account" element={<Account />} />
      </Route>

      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
    </Suspense>
  )
}

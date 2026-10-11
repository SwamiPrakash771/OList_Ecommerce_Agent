"""REST API for the storefront and admin UI.

Run (dev):   uvicorn api.main:app --reload --port 8000
The React app in web/ calls /api/*. After `npm run build` in web/, this app also
serves the built frontend, so one process runs everything.
"""

import functools
import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Literal

import jwt
import pandas as pd
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

import auth
from repository import AdminRepository, CatalogRepository, CustomerRepository, SellerRepository

ROOT = Path(__file__).resolve().parent.parent
WEB_DIST = ROOT / "web" / "dist"

COOKIE_NAME = "olist_session"
SESSION_HOURS = 8
JWT_SECRET = os.environ.get("JWT_SECRET") or secrets.token_hex(32)  # random = sessions reset on restart

app = FastAPI(title="Olist Store API")
app.add_middleware(GZipMiddleware, minimum_size=1000)
catalog = CatalogRepository()


# ---- helpers --------------------------------------------------------------


def records(df: pd.DataFrame) -> list[dict]:
    """DataFrame -> JSON-friendly rows (NaN/NaT become null)."""
    return df.astype(object).where(df.notna(), None).to_dict("records")


def order_json(order: dict) -> dict:
    return {
        **{k: v for k, v in order.items() if k not in ("items", "payments")},
        "items": records(order["items"]),
        "payments": records(order["payments"]),
    }


# ---- auth -----------------------------------------------------------------


def current_user(request: Request) -> dict | None:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None


def require_user(user: dict | None = Depends(current_user)) -> dict:
    if not user:
        raise HTTPException(401, "Please sign in")
    return user


def require_customer(user: dict = Depends(require_user)) -> CustomerRepository:
    if user["role"] != "customer":
        raise HTTPException(403, "Customer account required")
    return CustomerRepository(user["cid"])


def require_admin(user: dict = Depends(require_user)) -> AdminRepository:
    if user["role"] != "admin":
        raise HTTPException(403, "Admin account required")
    return AdminRepository()


def require_seller(user: dict = Depends(require_user)) -> SellerRepository:
    if user["role"] != "seller":
        raise HTTPException(403, "Seller account required")
    return SellerRepository(user["sid"])


class LoginBody(BaseModel):
    username: str
    password: str


_failed_logins: dict[str, list[float]] = {}


@app.post("/api/auth/login")
def login(body: LoginBody, request: Request, response: Response):
    key = f"{request.client.host if request.client else '?'}:{body.username}"
    recent = [t for t in _failed_logins.get(key, []) if time.time() - t < 60]
    if len(recent) >= 5:
        raise HTTPException(429, "Too many failed attempts. Try again in a minute.")

    user = auth.authenticate(body.username, body.password)
    if not user:
        _failed_logins[key] = recent + [time.time()]
        raise HTTPException(401, "Wrong username or password")
    _failed_logins.pop(key, None)
    return start_session(response, user)


def start_session(response: Response, user: dict) -> dict:
    claims = {
        "uid": user["user_id"],
        "sub": user["username"],
        "role": user["role"],
        "cid": user["customer_unique_id"],
        "sid": user.get("seller_id"),
    }
    token = jwt.encode({**claims, "exp": int(time.time()) + SESSION_HOURS * 3600}, JWT_SECRET, algorithm="HS256")
    response.set_cookie(COOKIE_NAME, token, httponly=True, samesite="lax", max_age=SESSION_HOURS * 3600)
    return me(user=claims)


# ---- demo quick login (testing only) ----------------------------------------
# Enabled by DEMO_LOGIN=true in .env. The server picks the accounts itself: the
# five customers with the most orders, the two biggest sellers, and the admin.
# The browser only names which one; anything outside this list is refused.

DEMO_LOGIN = os.environ.get("DEMO_LOGIN", "").lower() in ("1", "true", "yes")
_demo_accounts: list[dict] | None = None
_demo_lock = threading.Lock()


def demo_accounts() -> list[dict]:
    """Built once (at startup warm-up); the lock stops concurrent requests computing it twice."""
    global _demo_accounts
    with _demo_lock:
        if _demo_accounts is None:
            customers = AdminRepository().top_customers(5)
            _demo_accounts = [
                {"username": r.customer_unique_id, "role": "customer",
                 "label": f"{r.city.title()}, {r.state}", "orders": int(r.orders)}
                for r in customers.itertuples()
            ]
            sellers = AdminRepository().sellers().head(2)
            _demo_accounts += [
                {"username": r.seller_id, "role": "seller",
                 "label": f"{r.seller_city.title()}, {r.seller_state}", "orders": int(r.orders_fulfilled)}
                for r in sellers.itertuples()
            ]
            _demo_accounts.append(
                {"username": os.environ.get("ADMIN_USERNAME", "admin"), "role": "admin", "label": "Admin", "orders": None}
            )
        return _demo_accounts


@app.get("/api/auth/demo-accounts")
def get_demo_accounts():
    return demo_accounts() if DEMO_LOGIN else []


class DemoLoginBody(BaseModel):
    username: str


@app.post("/api/auth/demo-login")
def demo_login(body: DemoLoginBody, response: Response):
    if not DEMO_LOGIN:
        raise HTTPException(404, "Demo login is disabled")
    if body.username not in {a["username"] for a in demo_accounts()}:
        raise HTTPException(403, "Not a demo account")
    user = auth.get_user(body.username)
    if not user:
        raise HTTPException(404, "Account not found")
    return start_session(response, user)


@app.post("/api/auth/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True}


@app.get("/api/auth/me")
def me(user: dict = Depends(require_user)):
    row = auth.db.query_one("SELECT must_change_password FROM app_users WHERE user_id = %s", (user["uid"],))
    if user["role"] == "customer":
        profile = CustomerRepository(user["cid"]).profile()
    elif user["role"] == "seller":
        profile = SellerRepository(user["sid"]).profile()
    else:
        profile = None
    return {
        "username": user["sub"],
        "role": user["role"],
        "must_change_password": bool(row and row["must_change_password"]),
        "profile": profile,
    }


class PasswordBody(BaseModel):
    current: str
    new: str


@app.post("/api/auth/change-password")
def change_password(body: PasswordBody, user: dict = Depends(require_user)):
    error = auth.change_password(user["uid"], body.current, body.new)
    if error:
        raise HTTPException(400, error)
    return {"ok": True}


# ---- catalog (public) ------------------------------------------------------


# Catalog data (product_stats, categories, reviews) is the same for every visitor
# and doesn't change while the app runs, so responses are cached in memory and
# browsers may reuse them for a few minutes.

CATALOG_TTL_SECONDS = 600
BROWSER_CACHE = "public, max-age=300"


_catalog_caches: list[dict] = []


def cached(fn):
    store: dict = {}
    _catalog_caches.append(store)

    @functools.wraps(fn)
    def wrapper(*args):
        hit = store.get(args)
        if hit and time.time() - hit[0] < CATALOG_TTL_SECONDS:
            return hit[1]
        value = fn(*args)
        if len(store) > 2000:  # keep memory bounded
            store.clear()
        store[args] = (time.time(), value)
        return value

    return wrapper


def clear_catalog_caches() -> None:
    """Called after an admin edits a product, so the store shows the change."""
    for store in _catalog_caches:
        store.clear()
    CatalogRepository._categories_cache = None


# A signed-in seller browsing the store sees only their own products. The
# seller_id is part of every cache key, and responses vary by the session cookie
# so a browser never reuses one account's catalog for another.


def store_scope(user: dict | None = Depends(current_user)) -> str | None:
    return user["sid"] if user and user["role"] == "seller" else None


def catalog_headers(response: Response, seller_id: str | None) -> None:
    response.headers["Cache-Control"] = "private, no-cache" if seller_id else BROWSER_CACHE
    response.headers["Vary"] = "Cookie"


@cached
def _categories(seller_id=None):
    return records(catalog.category_summary(seller_id))


@cached
def _products(q, category, min_price, max_price, min_rating, sort, page, page_size, seller_id=None):
    items, total = catalog.browse(q, category, min_price, max_price, min_rating, sort, page, page_size, seller_id)
    return {"items": records(items), "total": total, "page": page, "page_size": page_size}


@cached
def _product(product_id, seller_id=None):
    p = catalog.product(product_id)
    if not p or (seller_id and p["main_seller_id"] != seller_id):
        return None
    reviews = p.pop("recent_reviews")
    p.pop("main_seller_id", None)  # internal id, not shown to shoppers
    return {**p, "reviews": records(reviews), "related": records(catalog.related(product_id, seller_id=seller_id))}


@app.get("/api/categories")
def categories(response: Response, seller_id: str | None = Depends(store_scope)):
    catalog_headers(response, seller_id)
    return _categories(seller_id)


@app.get("/api/products")
def products(
    response: Response,
    q: str = "",
    category: str | None = None,
    min_price: float | None = None,
    max_price: float | None = None,
    min_rating: float | None = None,
    sort: Literal["popular", "rating", "price_low", "price_high"] = "popular",
    page: int = Query(1, ge=1),
    page_size: int = Query(24, ge=1, le=60),
    seller_id: str | None = Depends(store_scope),
):
    catalog_headers(response, seller_id)
    return _products(q.strip().lower(), category, min_price, max_price, min_rating, sort, page, page_size, seller_id)


@app.get("/api/products/{product_id}")
def product(product_id: str, response: Response, seller_id: str | None = Depends(store_scope)):
    p = _product(product_id, seller_id)
    if not p:  # also: another seller's product, for a signed-in seller
        raise HTTPException(404, "Product not found")
    catalog_headers(response, seller_id)
    return p


@app.on_event("startup")
def warm_up():
    """Open pooled connections and fill the cache for the home page before the first visitor."""
    def run():
        try:
            if DEMO_LOGIN:
                demo_accounts()
            _categories(None)
            _products("", None, None, None, None, "popular", 1, 10, None)
            _products("", None, None, None, 4.5, "rating", 1, 10, None)
            build_dashboard()
        except Exception as exc:  # warm-up is best effort
            print(f"cache warm-up skipped: {exc}")

    threading.Thread(target=run, daemon=True).start()


# ---- cart, checkout, orders (customer only) ---------------------------------


class CartAdd(BaseModel):
    product_id: str
    quantity: int = Field(1, ge=1, le=20)


class CartQty(BaseModel):
    quantity: int = Field(..., ge=0, le=20)


def cart_json(repo: CustomerRepository) -> dict:
    items = records(repo.cart())
    subtotal = sum(float(i["price"]) * i["quantity"] for i in items)
    shipping = sum(float(i["shipping"]) * i["quantity"] for i in items)
    return {
        "items": items,
        "count": sum(i["quantity"] for i in items),
        "subtotal": round(subtotal, 2),
        "shipping": round(shipping, 2),
        "total": round(subtotal + shipping, 2),
    }


@app.get("/api/cart")
def get_cart(repo: CustomerRepository = Depends(require_customer)):
    return cart_json(repo)


@app.post("/api/cart/items")
def add_cart_item(body: CartAdd, repo: CustomerRepository = Depends(require_customer)):
    try:
        repo.add_to_cart(body.product_id, body.quantity)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    return cart_json(repo)


@app.patch("/api/cart/items/{product_id}")
def update_cart_item(product_id: str, body: CartQty, repo: CustomerRepository = Depends(require_customer)):
    repo.set_cart_quantity(product_id, body.quantity)
    return cart_json(repo)


@app.delete("/api/cart/items/{product_id}")
def delete_cart_item(product_id: str, repo: CustomerRepository = Depends(require_customer)):
    repo.remove_from_cart(product_id)
    return cart_json(repo)


BR_STATES = {"AC", "AL", "AP", "AM", "BA", "CE", "DF", "ES", "GO", "MA", "MT", "MS", "MG", "PA",
             "PB", "PR", "PE", "PI", "RJ", "RN", "RS", "RO", "RR", "SC", "SP", "SE", "TO"}


class AddressBody(BaseModel):
    recipient_name: str = Field(..., min_length=2, max_length=100)
    phone: str | None = Field(None, max_length=30)
    street: str = Field(..., min_length=2, max_length=150)
    number: str = Field(..., min_length=1, max_length=20)
    complement: str | None = Field(None, max_length=100)
    neighborhood: str | None = Field(None, max_length=100)
    city: str = Field(..., min_length=2, max_length=64)
    state: str
    zip_code: str

    @field_validator("*", mode="before")
    @classmethod
    def strip(cls, v):
        return v.strip() or None if isinstance(v, str) else v

    @field_validator("state")
    @classmethod
    def valid_state(cls, v: str) -> str:
        if v.upper() not in BR_STATES:
            raise ValueError("Choose a Brazilian state")
        return v.upper()

    @field_validator("zip_code")
    @classmethod
    def format_zip(cls, v: str) -> str:
        digits = v.replace("-", "").replace(" ", "")
        if not (len(digits) == 8 and digits.isdigit()):
            raise ValueError("Enter an 8-digit CEP, like 01310-100")
        return f"{digits[:5]}-{digits[5:]}"


@app.get("/api/address")
def get_address(repo: CustomerRepository = Depends(require_customer)):
    return repo.address()


@app.put("/api/address")
def put_address(body: AddressBody, repo: CustomerRepository = Depends(require_customer)):
    return repo.save_address(body.model_dump())


class CheckoutBody(BaseModel):
    payment_type: Literal["credit_card", "boleto", "voucher", "debit_card"]
    installments: int = Field(1, ge=1, le=12)


@app.post("/api/checkout")
def checkout(body: CheckoutBody, repo: CustomerRepository = Depends(require_customer)):
    try:
        order_id = repo.checkout(body.payment_type, body.installments)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"order_id": order_id}


@app.get("/api/orders")
def my_orders(repo: CustomerRepository = Depends(require_customer)):
    return {"summary": repo.spending_summary(), "orders": records(repo.orders())}


@app.get("/api/orders/{order_id}")
def my_order(order_id: str, repo: CustomerRepository = Depends(require_customer)):
    order = repo.order_detail(order_id)
    if not order:  # also covers other customers' orders
        raise HTTPException(404, "Order not found")
    return order_json(order)


# ---- AI assistant chat -------------------------------------------------------
# The UI talks to this endpoint through a small streaming protocol (Server-Sent
# Events, one JSON object per event):
#   {"type": "text", "delta": "..."}              piece of the assistant's reply
#   {"type": "step", "id": "...", "label": "...", "status": "running" | "done" | "error"}
#                                                  an action the agent is taking
#   {"type": "cart_changed"}                       UI should refresh the cart badge
#   {"type": "error", "message": "..."}
#   {"type": "done"}
# The AI model is not connected yet, so for now the reply says so.


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(..., max_length=4000)


class ChatBody(BaseModel):
    messages: list[ChatMessage] = Field(..., min_length=1, max_length=50)


def sse(event: dict) -> str:
    return f"data: {json.dumps(event)}\n\n"


def placeholder_reply(text: str) -> StreamingResponse:
    """Streams a fixed message word by word until the AI model is connected."""
    def events():
        for word in text.split(" "):
            yield sse({"type": "text", "delta": word + " "})
            time.sleep(0.015)
        yield sse({"type": "done"})

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@app.post("/api/chat")
def chat(body: ChatBody, user: dict | None = Depends(current_user)):
    if user and user["role"] != "customer":
        raise HTTPException(403, "The shopping assistant is for customer accounts")
    return placeholder_reply(
        "Hi! I'm the Olist shopping assistant. My AI brain isn't connected yet, "
        "so I can't answer questions or take actions right now.\n\n"
        "Once it's switched on I'll be able to:\n"
        "- **Find products** by category, price and rating\n"
        "- **Track your orders** and explain delivery times\n"
        "- **Add items to your cart** for you\n"
        "- **Open a support ticket** for returns or problems"
    )


@app.post("/api/seller/chat")
def seller_chat(body: ChatBody, _: SellerRepository = Depends(require_seller)):
    return placeholder_reply(
        "Hi! I'm your Olist seller assistant. My AI brain isn't connected yet, "
        "so I can't answer questions or take actions right now.\n\n"
        "Once it's switched on I'll be able to:\n"
        "- **Summarise your sales** by month and product\n"
        "- **Spot problems** like late shipments or low-rated products\n"
        "- **Suggest price or listing changes** based on your data\n"
        "- **Update your products** for you, with your confirmation"
    )


@app.post("/api/admin/chat")
def admin_chat(body: ChatBody, _: AdminRepository = Depends(require_admin)):
    # Separate endpoint so the admin agent can get admin-only tools
    # (sales, all orders, all customers) without exposing them to customers or sellers.
    return placeholder_reply(
        "Hi! I'm the Olist admin assistant. My AI brain isn't connected yet, "
        "so I can't answer questions or take actions right now.\n\n"
        "Once it's switched on I'll be able to:\n"
        "- **Answer business questions** like revenue by month or top categories\n"
        "- **Find problem orders** such as late or undelivered ones\n"
        "- **Look up any customer** and their order history\n"
        "- **Compare sellers** on sales, ratings and on-time delivery"
    )


# ---- admin -------------------------------------------------------------------


# The dashboard aggregates the whole order history (~3 s warm, ~7 s right after
# a restart), so it is computed in the background at startup and reused for a
# few minutes, like a precomputed report.
DASHBOARD_TTL_SECONDS = 300
_dashboard: dict | None = None
_dashboard_lock = threading.Lock()


def build_dashboard() -> dict:
    global _dashboard
    with _dashboard_lock:
        if _dashboard and time.time() - _dashboard["computed_at"] < DASHBOARD_TTL_SECONDS:
            return _dashboard
        repo = AdminRepository()
        monthly = repo.monthly_revenue()
        _dashboard = {
            "kpis": repo.kpis(),
            "monthly_revenue": records(monthly[monthly["orders"] >= 100]),
            "orders_by_status": records(repo.orders_by_status()),
            "top_categories": records(repo.top_categories(10)),
            "computed_at": time.time(),
        }
        return _dashboard


@app.get("/api/admin/dashboard")
def admin_dashboard(_: AdminRepository = Depends(require_admin)):
    return build_dashboard()


@app.get("/api/admin/orders")
def admin_orders(
    order_id: str = "",
    customer: str = "",
    status: list[str] = Query(default=[]),
    date_from: str | None = None,
    date_to: str | None = None,
    repo: AdminRepository = Depends(require_admin),
):
    return {
        "statuses": repo.statuses(),
        "orders": records(repo.search_orders(order_id, customer, status, date_from, date_to, limit=200)),
    }


@app.get("/api/admin/orders/{order_id}")
def admin_order(order_id: str, repo: AdminRepository = Depends(require_admin)):
    order = repo.order_detail(order_id)
    if not order:
        raise HTTPException(404, "Order not found")
    return order_json(order)


@app.get("/api/admin/customers")
def admin_top_customers(repo: AdminRepository = Depends(require_admin)):
    global _top_customers_cache
    # Ranking takes ~0.8 s and barely changes minute to minute; reuse it briefly.
    if not _top_customers_cache or time.time() - _top_customers_cache[0] > 120:
        _top_customers_cache = (time.time(), records(repo.top_customers(100)))
    return _top_customers_cache[1]


_top_customers_cache: tuple[float, list[dict]] | None = None


@app.get("/api/admin/customers/{customer_unique_id}")
def admin_customer(customer_unique_id: str, _: AdminRepository = Depends(require_admin)):
    view = CustomerRepository(customer_unique_id)
    profile = view.profile()
    if not profile:
        raise HTTPException(404, "Customer not found")
    address = view.address()
    return {"profile": profile, "summary": view.spending_summary(), "orders": records(view.orders()),
            "address": address if address["is_saved"] else None}


class ProductUpdate(BaseModel):
    """Only the fields sent are changed."""
    name: str | None = Field(None, min_length=3, max_length=100)
    category: str | None = None
    price: float | None = Field(None, gt=0, le=100_000)
    shipping: float | None = Field(None, ge=0, le=10_000)
    weight_g: int | None = Field(None, ge=0, le=100_000)
    length_cm: int | None = Field(None, ge=0, le=300)
    height_cm: int | None = Field(None, ge=0, le=300)
    width_cm: int | None = Field(None, ge=0, le=300)

    @field_validator("name")
    @classmethod
    def strip_name(cls, v):
        return v.strip() if isinstance(v, str) else v

    @field_validator("price", "shipping")
    @classmethod
    def cents(cls, v):
        return round(v, 2) if v is not None else v


def product_edit_json(repo: AdminRepository | SellerRepository, product_id: str) -> dict:
    product = repo.editable_product(product_id)
    if not product:
        raise HTTPException(404, "Product not found")
    history = repo.product_edits(product_id)
    history["changes"] = history["changes"].map(lambda c: json.loads(c) if isinstance(c, str) else c)
    return {"product": product, "categories": catalog.categories(), "history": records(history)}


@app.get("/api/admin/products/{product_id}")
def admin_product(product_id: str, repo: AdminRepository = Depends(require_admin)):
    # Read-only for admins: product details are edited by the product's seller.
    return product_edit_json(repo, product_id)


# ---- seller center ---------------------------------------------------------------


@app.get("/api/seller/dashboard")
def seller_dashboard(repo: SellerRepository = Depends(require_seller)):
    monthly = repo.monthly_sales()
    return {
        "profile": repo.profile(),
        "kpis": repo.kpis(),
        "monthly_sales": records(monthly[monthly["orders"] >= 1]),
        "top_products": records(repo.top_products(5)),
    }


@app.get("/api/seller/products")
def seller_products(
    q: str = "",
    sort: Literal["popular", "rating", "price_low", "price_high"] = "popular",
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=60),
    category: str | None = None,
    repo: SellerRepository = Depends(require_seller),
):
    items, total = repo.products(q, sort, page, page_size, category)
    return {"items": records(items), "total": total, "page": page, "page_size": page_size}


@app.get("/api/seller/categories")
def seller_categories(repo: SellerRepository = Depends(require_seller)):
    return records(repo.categories())


class ProductCreate(ProductUpdate):
    """A new product: name, category, price and shipping are required."""
    name: str = Field(..., min_length=3, max_length=100)
    category: str
    price: float = Field(..., gt=0, le=100_000)
    shipping: float = Field(..., ge=0, le=10_000)


@app.post("/api/seller/products", status_code=201)
def seller_create_product(
    body: ProductCreate,
    repo: SellerRepository = Depends(require_seller),
    user: dict = Depends(require_user),
):
    if body.category not in catalog.categories():
        raise HTTPException(422, "Choose an existing category")
    product = repo.create_product(body.model_dump(exclude_none=True), created_by=user["sub"])
    clear_catalog_caches()
    return product_edit_json(repo, product["product_id"])


@app.get("/api/seller/products/{product_id}")
def seller_product(product_id: str, repo: SellerRepository = Depends(require_seller)):
    return product_edit_json(repo, product_id)  # 404 for another seller's product


@app.patch("/api/seller/products/{product_id}")
def seller_update_product(
    product_id: str,
    body: ProductUpdate,
    repo: SellerRepository = Depends(require_seller),
    user: dict = Depends(require_user),
):
    values = body.model_dump(exclude_none=True)
    if "category" in values and values["category"] not in catalog.categories():
        raise HTTPException(422, "Choose an existing category")
    try:
        repo.update_product(product_id, values, edited_by=user["sub"])
    except ValueError as exc:  # missing, or owned by another seller
        raise HTTPException(404, str(exc))
    clear_catalog_caches()
    return product_edit_json(repo, product_id)


@app.get("/api/seller/orders")
def seller_orders(status: str | None = None, repo: SellerRepository = Depends(require_seller)):
    return {"orders": records(repo.orders(status))}


@app.get("/api/admin/sellers")
def admin_sellers(state: str | None = None, min_orders: int = 0, repo: AdminRepository = Depends(require_admin)):
    return {"states": repo.seller_states(), "sellers": records(repo.sellers(state, min_orders))}


@app.get("/api/admin/users")
def admin_users(role: str | None = None, search: str = "", repo: AdminRepository = Depends(require_admin)):
    return records(repo.users(role, search))


class ResetBody(BaseModel):
    username: str


@app.post("/api/admin/users/reset-password")
def admin_reset_password(body: ResetBody, _: AdminRepository = Depends(require_admin)):
    temp = secrets.token_urlsafe(9)
    if not auth.reset_password(body.username.strip(), temp):
        raise HTTPException(404, "No user with that username")
    return {"temporary_password": temp}


class NewAdminBody(BaseModel):
    username: str = Field(..., min_length=3, max_length=64)
    password: str = Field(..., min_length=8)


@app.post("/api/admin/users")
def admin_create(body: NewAdminBody, _: AdminRepository = Depends(require_admin)):
    try:
        auth.create_admin(body.username.strip(), body.password)
    except Exception:
        raise HTTPException(409, "Username already exists")
    return {"ok": True}


@app.get("/api/admin/tickets")
def admin_tickets(repo: AdminRepository = Depends(require_admin)):
    return records(repo.support_tickets())


# ---- built frontend ----------------------------------------------------------

if WEB_DIST.exists():
    app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        file = WEB_DIST / path
        if path and file.is_file() and WEB_DIST in file.resolve().parents:
            return FileResponse(file)
        return FileResponse(WEB_DIST / "index.html")

"""Data access, split by who is allowed to see what.

- CatalogRepository: public product and seller information. Any logged-in user.
- CustomerRepository: one customer's own orders. Every query is filtered by the
  customer_unique_id the repository was created with, so a customer session
  can never read another customer's data.
- AdminRepository: unrestricted access to all tables.

UI pages get data only through these classes, never through raw SQL.
"""

import json
import re
import uuid
from datetime import datetime, timedelta
from decimal import Decimal

import pandas as pd

import db

ESTIMATED_DELIVERY_DAYS = 12
HEX_CODE = re.compile(r"[0-9a-f]{4,32}")
# Product fields an admin may edit -> where they are read from.
EDITABLE_PRODUCT_FIELDS = {
    "name": "ps.display_name",
    "category": "ps.category",
    "price": "ps.typical_price",
    "shipping": "ps.avg_freight",
    "weight_g": "p.weight_g",
    "length_cm": "p.length_cm",
    "height_cm": "p.height_cm",
    "width_cm": "p.width_cm",
}


def _plain(v):
    """DB value -> plain JSON-comparable value (Decimal -> float)."""
    return float(v) if isinstance(v, Decimal) else v


ADDRESS_FIELDS = ["recipient_name", "phone", "street", "number", "complement",
                  "neighborhood", "city", "state", "zip_code"]

ORDER_SUMMARY_SQL = """
    SELECT o.order_id,
           o.order_purchase_timestamp AS purchased_at,
           o.order_status AS status,
           COUNT(i.order_item_id) AS items,
           ROUND(COALESCE(SUM(i.price + i.freight_value), 0), 2) AS order_total,
           o.order_estimated_delivery_date AS estimated_delivery,
           o.order_delivered_customer_date AS delivered_at,
           c.customer_unique_id
    FROM orders o
    JOIN customers c USING (customer_id)
    LEFT JOIN order_items i USING (order_id)
"""


def _order_detail(order_id: str, customer_unique_id: str | None) -> dict | None:
    """Full detail for one order. If customer_unique_id is given, the order must belong to it."""
    sql = """
        SELECT o.*, c.customer_unique_id, c.customer_city, c.customer_state
        FROM orders o JOIN customers c USING (customer_id)
        WHERE o.order_id = %s
    """
    params: tuple = (order_id,)
    if customer_unique_id is not None:
        sql += " AND c.customer_unique_id = %s"
        params += (customer_unique_id,)
    order = db.query_one(sql, params)
    if not order:
        return None

    order["items"] = db.query_df(
        """
        SELECT i.order_item_id AS item, p.display_name AS product, p.category,
               i.price, i.freight_value AS freight,
               s.seller_city, s.seller_state, i.product_id, i.seller_id
        FROM order_items i
        JOIN products p USING (product_id)
        JOIN sellers s USING (seller_id)
        WHERE i.order_id = %s
        ORDER BY i.order_item_id
        """,
        (order_id,),
    )
    order["payments"] = db.query_df(
        """
        SELECT payment_sequential AS seq, payment_type AS type,
               payment_installments AS installments, payment_value AS amount
        FROM payments WHERE order_id = %s ORDER BY payment_sequential
        """,
        (order_id,),
    )
    order["review"] = db.query_one(
        """
        SELECT review_score, review_comment_title, review_comment_message, review_creation_date
        FROM reviews WHERE order_id = %s
        ORDER BY review_answer_timestamp DESC LIMIT 1
        """,
        (order_id,),
    )
    # Only orders placed in this app have a full address; Olist orders have city/state only.
    order["shipping_address"] = db.query_one(
        f"SELECT {', '.join(ADDRESS_FIELDS)} FROM order_addresses WHERE order_id = %s", (order_id,)
    )
    return order


class CatalogRepository:
    def categories(self) -> list[str]:
        df = db.query_df("SELECT DISTINCT category FROM product_stats ORDER BY category")
        return df["category"].tolist()

    def search_products(
        self,
        categories: list[str] | None = None,
        min_price: float = 0,
        max_price: float = 1e9,
        min_rating: float = 0,
        sort_by: str = "popular",
        limit: int = 100,
    ) -> pd.DataFrame:
        order_by = {
            "popular": "times_sold DESC",
            "rating": "avg_rating DESC, num_reviews DESC",
            "price_low": "typical_price ASC",
            "price_high": "typical_price DESC",
        }.get(sort_by, "times_sold DESC")
        sql = """
            SELECT product_id, display_name AS product, category, typical_price AS price,
                   avg_freight AS shipping, avg_rating AS rating, num_reviews AS reviews,
                   times_sold AS sold
            FROM product_stats
            WHERE typical_price BETWEEN %s AND %s
              AND COALESCE(avg_rating, 0) >= %s
        """
        params: list = [min_price, max_price, min_rating]
        if categories:
            sql += f" AND category IN ({', '.join(['%s'] * len(categories))})"
            params += categories
        sql += f" ORDER BY {order_by} LIMIT %s"
        params.append(int(limit))
        return db.query_df(sql, tuple(params))

    _categories_cache: list[str] | None = None

    def _category_names(self) -> list[str]:
        if CatalogRepository._categories_cache is None:
            CatalogRepository._categories_cache = self.categories()
        return CatalogRepository._categories_cache

    # seller_id: when set, every catalog query is limited to that seller's
    # products (a signed-in seller browsing the store sees only their own shop).

    def category_summary(self, seller_id: str | None = None) -> pd.DataFrame:
        where, params = ("WHERE main_seller_id = %s", (seller_id,)) if seller_id else ("", None)
        return db.query_df(
            f"""
            SELECT category, COUNT(*) AS products, ROUND(MIN(typical_price), 2) AS from_price,
                   SUM(times_sold) AS sold
            FROM product_stats {where} GROUP BY category ORDER BY sold DESC
            """,
            params,
        )

    def browse(
        self,
        q: str = "",
        category: str | None = None,
        min_price: float | None = None,
        max_price: float | None = None,
        min_rating: float | None = None,
        sort_by: str = "popular",
        page: int = 1,
        page_size: int = 24,
        seller_id: str | None = None,
    ) -> tuple[pd.DataFrame, int]:
        """Storefront search. Returns one page of products and the total match count."""
        # Each ORDER BY matches one index exactly (see load_mysql.py), so MySQL
        # reads rows in order instead of sorting the whole table.
        order_by = {
            "popular": "times_sold DESC, product_id DESC",
            "rating": "rating_rank DESC, num_reviews DESC, product_id DESC",
            "price_low": "typical_price ASC, product_id ASC",
            "price_high": "typical_price DESC, product_id DESC",
        }.get(sort_by, "times_sold DESC, product_id DESC")
        where, params = ["1=1"], []
        for word in q.lower().replace("#", " ").split():
            # No product titles exist, so a word matches category names (resolved
            # here against the ~74 categories, so the query can use the category
            # index) or the start of a product code like "53b36d".
            cats = [c for c in self._category_names() if word in c.replace("_", " ")]
            code = word if HEX_CODE.fullmatch(word) else None
            if not cats and not code:
                where.append("0=1")
                continue
            parts = []
            if cats:
                parts.append(f"category IN ({', '.join(['%s'] * len(cats))})")
                params += cats
            if code:
                parts.append("product_id LIKE %s")
                params.append(f"{code}%")
            where.append(f"({' OR '.join(parts)})")
        if category:
            where.append("category = %s")
            params.append(category)
        if min_price is not None:
            where.append("typical_price >= %s")
            params.append(min_price)
        if max_price is not None:
            where.append("typical_price <= %s")
            params.append(max_price)
        if min_rating:
            where.append("avg_rating >= %s")
            params.append(min_rating)
        if seller_id:
            where.append("main_seller_id = %s")
            params.append(seller_id)
        where_sql = " AND ".join(where)

        total = db.query_one(f"SELECT COUNT(*) AS n FROM product_stats WHERE {where_sql}", tuple(params))["n"]
        page_size = max(1, min(page_size, 60))
        offset = (max(page, 1) - 1) * page_size
        items = db.query_df(
            f"""
            SELECT product_id, display_name AS name, category, typical_price AS price,
                   avg_freight AS shipping, avg_rating AS rating, num_reviews AS reviews,
                   times_sold AS sold
            FROM product_stats WHERE {where_sql}
            ORDER BY {order_by} LIMIT %s OFFSET %s
            """,
            tuple(params) + (page_size, offset),
        )
        return items, int(total)

    def related(self, product_id: str, limit: int = 8, seller_id: str | None = None) -> pd.DataFrame:
        seller_sql, seller_params = ("AND main_seller_id = %s", (seller_id,)) if seller_id else ("", ())
        return db.query_df(
            f"""
            SELECT product_id, display_name AS name, category, typical_price AS price,
                   avg_freight AS shipping, avg_rating AS rating, num_reviews AS reviews,
                   times_sold AS sold
            FROM product_stats
            WHERE category = (SELECT category FROM product_stats WHERE product_id = %s)
              AND product_id <> %s {seller_sql}
            ORDER BY times_sold DESC LIMIT %s
            """,
            (product_id, product_id, *seller_params, limit),
        )

    def product(self, product_id: str) -> dict | None:
        product = db.query_one(
            """
            SELECT ps.*, p.photos, p.weight_g, p.length_cm, p.height_cm, p.width_cm,
                   ss.seller_city, ss.seller_state, ss.avg_rating AS seller_rating,
                   ss.on_time_pct AS seller_on_time_pct, ss.orders_fulfilled AS seller_orders
            FROM product_stats ps
            JOIN products p USING (product_id)
            LEFT JOIN seller_stats ss ON ss.seller_id = ps.main_seller_id
            WHERE ps.product_id = %s
            """,
            (product_id,),
        )
        if product:
            # Review text only: no order or customer identifiers leave this query.
            product["recent_reviews"] = db.query_df(
                """
                SELECT DISTINCT r.review_score AS score, r.review_comment_title AS title,
                       r.review_comment_message AS comment, DATE(r.review_creation_date) AS date
                FROM reviews r JOIN order_items i USING (order_id)
                WHERE i.product_id = %s AND r.review_comment_message IS NOT NULL
                ORDER BY date DESC LIMIT 10
                """,
                (product_id,),
            )
        return product


class CustomerRepository:
    def __init__(self, customer_unique_id: str):
        if not customer_unique_id:
            raise ValueError("customer_unique_id is required")
        self.customer_unique_id = customer_unique_id

    def profile(self) -> dict | None:
        return db.query_one(
            """
            SELECT c.customer_unique_id, c.customer_city AS city, c.customer_state AS state,
                   c.customer_zip_code_prefix AS zip_code,
                   COUNT(DISTINCT o.order_id) AS orders,
                   MIN(o.order_purchase_timestamp) AS first_order,
                   MAX(o.order_purchase_timestamp) AS last_order
            FROM customers c JOIN orders o USING (customer_id)
            WHERE c.customer_unique_id = %s
            GROUP BY c.customer_unique_id, c.customer_city, c.customer_state, c.customer_zip_code_prefix
            ORDER BY last_order DESC LIMIT 1
            """,
            (self.customer_unique_id,),
        )

    def orders(self) -> pd.DataFrame:
        return db.query_df(
            ORDER_SUMMARY_SQL
            + " WHERE c.customer_unique_id = %s GROUP BY o.order_id ORDER BY purchased_at DESC",
            (self.customer_unique_id,),
        ).drop(columns="customer_unique_id")

    def order_detail(self, order_id: str) -> dict | None:
        """Returns None if the order does not exist or belongs to someone else."""
        return _order_detail(order_id, self.customer_unique_id)

    def spending_summary(self) -> dict:
        return db.query_one(
            """
            SELECT COUNT(DISTINCT o.order_id) AS orders,
                   COALESCE(SUM(p.payment_value), 0) AS total_paid,
                   (SELECT ROUND(AVG(r.review_score), 2)
                      FROM reviews r JOIN orders o2 USING (order_id)
                      JOIN customers c2 USING (customer_id)
                     WHERE c2.customer_unique_id = %s) AS avg_review_given
            FROM orders o
            JOIN customers c USING (customer_id)
            LEFT JOIN payments p USING (order_id)
            WHERE c.customer_unique_id = %s
            """,
            (self.customer_unique_id, self.customer_unique_id),
        )


    # ---- shipping address -------------------------------------------------

    def address(self) -> dict:
        """The saved address, or a partial one prefilled from Olist's customer record."""
        saved = db.query_one(
            f"SELECT {', '.join(ADDRESS_FIELDS)}, updated_at FROM customer_addresses WHERE customer_unique_id = %s",
            (self.customer_unique_id,),
        )
        if saved:
            return {**saved, "is_saved": True}
        profile = self.profile() or {}
        return {
            **{f: None for f in ADDRESS_FIELDS},
            "city": (profile.get("city") or "").title() or None,
            "state": profile.get("state"),
            "zip_code": profile.get("zip_code"),
            "is_saved": False,
        }

    def save_address(self, address: dict) -> dict:
        values = [address.get(f) for f in ADDRESS_FIELDS]
        db.execute(
            f"""
            INSERT INTO customer_addresses (customer_unique_id, {', '.join(ADDRESS_FIELDS)})
            VALUES (%s, {', '.join(['%s'] * len(ADDRESS_FIELDS))})
            ON DUPLICATE KEY UPDATE {', '.join(f'{f} = VALUES({f})' for f in ADDRESS_FIELDS)}
            """,
            (self.customer_unique_id, *values),
        )
        return self.address()

    # ---- cart -------------------------------------------------------------

    def cart(self) -> pd.DataFrame:
        return db.query_df(
            """
            SELECT ci.product_id, ps.display_name AS name, ps.category,
                   ps.typical_price AS price, ps.avg_freight AS shipping,
                   ps.avg_rating AS rating, ci.quantity
            FROM cart_items ci JOIN product_stats ps USING (product_id)
            WHERE ci.customer_unique_id = %s
            ORDER BY ci.added_at
            """,
            (self.customer_unique_id,),
        )

    def add_to_cart(self, product_id: str, quantity: int = 1) -> None:
        if not db.query_one("SELECT 1 AS ok FROM product_stats WHERE product_id = %s", (product_id,)):
            raise ValueError("Product not found")
        db.execute(
            """
            INSERT INTO cart_items (customer_unique_id, product_id, quantity) VALUES (%s, %s, %s)
            ON DUPLICATE KEY UPDATE quantity = LEAST(quantity + VALUES(quantity), 20)
            """,
            (self.customer_unique_id, product_id, max(1, min(quantity, 20))),
        )

    def set_cart_quantity(self, product_id: str, quantity: int) -> None:
        if quantity <= 0:
            self.remove_from_cart(product_id)
            return
        db.execute(
            "UPDATE cart_items SET quantity = %s WHERE customer_unique_id = %s AND product_id = %s",
            (min(quantity, 20), self.customer_unique_id, product_id),
        )

    def remove_from_cart(self, product_id: str) -> None:
        db.execute(
            "DELETE FROM cart_items WHERE customer_unique_id = %s AND product_id = %s",
            (self.customer_unique_id, product_id),
        )

    def checkout(self, payment_type: str, installments: int = 1) -> str:
        """Turn the cart into an order. Returns the new order_id."""
        if payment_type not in {"credit_card", "boleto", "voucher", "debit_card"}:
            raise ValueError("Unsupported payment type")
        installments = installments if payment_type == "credit_card" else 1
        installments = max(1, min(int(installments), 12))

        with db.transaction() as cur:
            cur.execute(
                """
                SELECT ci.product_id, ci.quantity, ps.typical_price AS price,
                       ps.avg_freight AS freight, ps.main_seller_id AS seller_id
                FROM cart_items ci JOIN product_stats ps USING (product_id)
                WHERE ci.customer_unique_id = %s FOR UPDATE
                """,
                (self.customer_unique_id,),
            )
            lines = cur.fetchall()
            if not lines:
                raise ValueError("Your cart is empty")

            cur.execute(
                f"SELECT {', '.join(ADDRESS_FIELDS)} FROM customer_addresses WHERE customer_unique_id = %s",
                (self.customer_unique_id,),
            )
            address = cur.fetchone()
            if not address:
                raise ValueError("Please add a shipping address before placing the order")

            # A customer can have several customer_id rows (one per past order);
            # attach the new order to the most recent one.
            cur.execute(
                """
                SELECT c.customer_id FROM customers c
                LEFT JOIN orders o USING (customer_id)
                WHERE c.customer_unique_id = %s
                ORDER BY o.order_purchase_timestamp DESC LIMIT 1
                """,
                (self.customer_unique_id,),
            )
            customer_id = cur.fetchone()["customer_id"]

            order_id = uuid.uuid4().hex
            now = datetime.now().replace(microsecond=0)
            cur.execute(
                """
                INSERT INTO orders (order_id, customer_id, order_status, order_purchase_timestamp,
                                    order_approved_at, order_estimated_delivery_date)
                VALUES (%s, %s, 'approved', %s, %s, %s)
                """,
                (order_id, customer_id, now, now, (now + timedelta(days=ESTIMATED_DELIVERY_DAYS)).date()),
            )
            # Olist stores quantity as repeated item rows, so do the same.
            item_no, total = 0, 0.0
            for line in lines:
                for _ in range(line["quantity"]):
                    item_no += 1
                    total += float(line["price"]) + float(line["freight"])
                    cur.execute(
                        """
                        INSERT INTO order_items (order_id, order_item_id, product_id, seller_id,
                                                 shipping_limit_date, price, freight_value)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """,
                        (order_id, item_no, line["product_id"], line["seller_id"],
                         now + timedelta(days=3), line["price"], line["freight"]),
                    )
            cur.execute(
                """
                INSERT INTO payments (order_id, payment_sequential, payment_type,
                                      payment_installments, payment_value)
                VALUES (%s, 1, %s, %s, %s)
                """,
                (order_id, payment_type, installments, round(total, 2)),
            )
            cur.execute(
                f"""
                INSERT INTO order_addresses (order_id, {', '.join(ADDRESS_FIELDS)})
                VALUES (%s, {', '.join(['%s'] * len(ADDRESS_FIELDS))})
                """,
                (order_id, *[address[f] for f in ADDRESS_FIELDS]),
            )
            cur.execute("DELETE FROM cart_items WHERE customer_unique_id = %s", (self.customer_unique_id,))
        return order_id


class AdminRepository:
    def kpis(self) -> dict:
        return db.query_one(
            """
            SELECT (SELECT COUNT(*) FROM orders) AS orders,
                   (SELECT COUNT(DISTINCT customer_unique_id) FROM customers) AS customers,
                   (SELECT COUNT(*) FROM sellers) AS sellers,
                   (SELECT ROUND(SUM(payment_value), 2) FROM payments) AS revenue,
                   (SELECT ROUND(AVG(review_score), 2) FROM reviews) AS avg_review,
                   (SELECT ROUND(100 * AVG(order_delivered_customer_date <= order_estimated_delivery_date), 1)
                      FROM orders WHERE order_delivered_customer_date IS NOT NULL) AS on_time_pct
            """
        )

    def monthly_revenue(self) -> pd.DataFrame:
        return db.query_df(
            """
            SELECT DATE_FORMAT(o.order_purchase_timestamp, '%Y-%m-01') AS month,
                   COUNT(DISTINCT o.order_id) AS orders,
                   ROUND(SUM(p.payment_value), 2) AS revenue
            FROM orders o JOIN payments p USING (order_id)
            WHERE o.order_status NOT IN ('canceled', 'unavailable')
            GROUP BY month ORDER BY month
            """
        )

    def orders_by_status(self) -> pd.DataFrame:
        return db.query_df(
            "SELECT order_status AS status, COUNT(*) AS orders FROM orders GROUP BY status ORDER BY orders DESC"
        )

    def top_categories(self, limit: int = 10) -> pd.DataFrame:
        return db.query_df(
            """
            SELECT p.category, ROUND(SUM(i.price), 2) AS revenue, COUNT(*) AS items_sold
            FROM order_items i JOIN products p USING (product_id)
            GROUP BY p.category ORDER BY revenue DESC LIMIT %s
            """,
            (limit,),
        )

    def search_orders(
        self,
        order_id: str = "",
        customer_unique_id: str = "",
        statuses: list[str] | None = None,
        date_from=None,
        date_to=None,
        limit: int = 500,
    ) -> pd.DataFrame:
        where, params = [], []
        if order_id:
            where.append("o.order_id = %s")
            params.append(order_id.strip())
        if customer_unique_id:
            where.append("c.customer_unique_id = %s")
            params.append(customer_unique_id.strip())
        if statuses:
            where.append(f"o.order_status IN ({', '.join(['%s'] * len(statuses))})")
            params += statuses
        if date_from:
            where.append("o.order_purchase_timestamp >= %s")
            params.append(date_from)
        if date_to:
            where.append("o.order_purchase_timestamp < DATE_ADD(%s, INTERVAL 1 DAY)")
            params.append(date_to)
        # Pick the newest matching orders first (index on purchase time), then
        # total items for just those, instead of totalling all ~99k orders.
        params.append(int(limit))
        return db.query_df(
            f"""
            SELECT o.order_id,
                   o.order_purchase_timestamp AS purchased_at,
                   o.order_status AS status,
                   COUNT(i.order_item_id) AS items,
                   ROUND(COALESCE(SUM(i.price + i.freight_value), 0), 2) AS order_total,
                   o.order_estimated_delivery_date AS estimated_delivery,
                   o.order_delivered_customer_date AS delivered_at,
                   c.customer_unique_id
            FROM (
                SELECT o.order_id FROM orders o JOIN customers c USING (customer_id)
                {"WHERE " + " AND ".join(where) if where else ""}
                ORDER BY o.order_purchase_timestamp DESC LIMIT %s
            ) newest
            JOIN orders o USING (order_id)
            JOIN customers c USING (customer_id)
            LEFT JOIN order_items i USING (order_id)
            GROUP BY o.order_id
            ORDER BY purchased_at DESC
            """,
            tuple(params),
        )

    def order_detail(self, order_id: str) -> dict | None:
        return _order_detail(order_id, None)

    def statuses(self) -> list[str]:
        return db.query_df("SELECT DISTINCT order_status FROM orders ORDER BY 1")["order_status"].tolist()

    def top_customers(self, limit: int = 100) -> pd.DataFrame:
        # Ranking all 96k customers through orders and payments takes ~5 s.
        # In Olist every order has its own customer_id row, so rows per
        # customer_unique_id approximate the order count straight from an index.
        # Take everyone at or above the limit-th count (keeping ties), then
        # compute exact orders and totals for just those candidates.
        counts = db.query_df(
            """
            SELECT COUNT(*) AS n FROM customers
            GROUP BY customer_unique_id ORDER BY n DESC LIMIT %s
            """,
            (limit,),
        )
        if counts.empty:
            return counts
        threshold = int(counts["n"].min())
        return db.query_df(
            """
            SELECT c.customer_unique_id, ANY_VALUE(c.customer_city) AS city,
                   ANY_VALUE(c.customer_state) AS state,
                   COUNT(DISTINCT o.order_id) AS orders,
                   ROUND(SUM(p.payment_value), 2) AS total_paid
            FROM customers c
            JOIN orders o USING (customer_id)
            LEFT JOIN payments p USING (order_id)
            WHERE c.customer_unique_id IN (
                SELECT customer_unique_id FROM (
                    SELECT customer_unique_id FROM customers
                    GROUP BY customer_unique_id HAVING COUNT(*) >= %s
                ) AS candidates
            )
            GROUP BY c.customer_unique_id
            ORDER BY orders DESC, total_paid DESC LIMIT %s
            """,
            (threshold, limit),
        )

    def sellers(self, state: str | None = None, min_orders: int = 0) -> pd.DataFrame:
        sql = "SELECT * FROM seller_stats WHERE orders_fulfilled >= %s"
        params: list = [min_orders]
        if state:
            sql += " AND seller_state = %s"
            params.append(state)
        return db.query_df(sql + " ORDER BY orders_fulfilled DESC", tuple(params))

    def seller_states(self) -> list[str]:
        return db.query_df("SELECT DISTINCT seller_state FROM sellers ORDER BY 1")["seller_state"].tolist()

    def users(self, role: str | None = None, search: str = "", limit: int = 200) -> pd.DataFrame:
        sql = """SELECT user_id, username, role, customer_unique_id, must_change_password,
                        is_active, created_at, last_login FROM app_users WHERE 1=1"""
        params: list = []
        if role:
            sql += " AND role = %s"
            params.append(role)
        if search:
            sql += " AND username LIKE %s"
            params.append(f"{search.strip()}%")
        sql += " ORDER BY (last_login IS NULL), last_login DESC, user_id LIMIT %s"
        params.append(int(limit))
        return db.query_df(sql, tuple(params))

    def support_tickets(self) -> pd.DataFrame:
        return db.query_df("SELECT * FROM support_tickets ORDER BY created_at DESC")

    # ---- products (read-only for admins; sellers edit their own) ----------

    def editable_product(self, product_id: str) -> dict | None:
        return _product_for_edit(product_id)

    def product_edits(self, product_id: str, limit: int = 20) -> pd.DataFrame:
        return _product_edit_history(product_id, limit)


# ---- product editing (shared) ---------------------------------------------------
# Name, category, price and shipping live in product_stats (what the store reads);
# name and category are also copied in products. Both are updated together, and
# every edit is logged in product_edits.


def _product_for_edit(product_id: str, seller_id: str | None = None) -> dict | None:
    sql = f"""
        SELECT ps.product_id, {', '.join(f'{src} AS {name}' for name, src in EDITABLE_PRODUCT_FIELDS.items())},
               ps.times_sold, ps.avg_rating, ps.num_reviews, ps.main_seller_id AS seller_id
        FROM product_stats ps JOIN products p USING (product_id)
        WHERE ps.product_id = %s
    """
    params: tuple = (product_id,)
    if seller_id is not None:
        sql += " AND ps.main_seller_id = %s"
        params += (seller_id,)
    return db.query_one(sql, params)


def _product_edit_history(product_id: str, limit: int = 20) -> pd.DataFrame:
    return db.query_df(
        """
        SELECT edited_by, editor_role, changes, edited_at FROM product_edits
        WHERE product_id = %s ORDER BY edited_at DESC, edit_id DESC LIMIT %s
        """,
        (product_id, limit),
    )


def _apply_product_edit(product_id: str, before: dict, values: dict, edited_by: str, editor_role: str) -> None:
    changes = {
        k: {"from": _plain(before[k]), "to": v}
        for k, v in values.items()
        if k in EDITABLE_PRODUCT_FIELDS and _plain(before[k]) != v
    }
    if not changes:
        return
    stats_cols = {"name": "display_name", "category": "category", "price": "typical_price", "shipping": "avg_freight"}
    product_cols = {"name": "display_name", "category": "category", "weight_g": "weight_g",
                    "length_cm": "length_cm", "height_cm": "height_cm", "width_cm": "width_cm"}
    with db.transaction() as cur:
        for table, cols in (("product_stats", stats_cols), ("products", product_cols)):
            sets = [(col, changes[k]["to"]) for k, col in cols.items() if k in changes]
            if sets:
                cur.execute(
                    f"UPDATE {table} SET {', '.join(f'{c} = %s' for c, _ in sets)} WHERE product_id = %s",
                    (*[v for _, v in sets], product_id),
                )
        cur.execute(
            "INSERT INTO product_edits (product_id, edited_by, editor_role, changes) VALUES (%s, %s, %s, %s)",
            (product_id, edited_by, editor_role, json.dumps(changes)),
        )


class SellerRepository:
    """One seller's view: every query is limited to this seller_id.

    A product belongs to its main seller (product_stats.main_seller_id), the
    seller who sold it most; that is also who new orders are routed to.
    """

    def __init__(self, seller_id: str):
        if not seller_id:
            raise ValueError("seller_id is required")
        self.seller_id = seller_id

    def profile(self) -> dict | None:
        return db.query_one(
            """
            SELECT s.seller_id, s.seller_city AS city, s.seller_state AS state,
                   s.seller_zip_code_prefix AS zip_code,
                   ss.orders_fulfilled, ss.avg_rating, ss.on_time_pct
            FROM sellers s LEFT JOIN seller_stats ss USING (seller_id)
            WHERE s.seller_id = %s
            """,
            (self.seller_id,),
        )

    def kpis(self) -> dict:
        row = db.query_one(
            """
            SELECT COUNT(DISTINCT order_id) AS orders,
                   COUNT(*) AS items_sold,
                   ROUND(COALESCE(SUM(price), 0), 2) AS revenue
            FROM order_items WHERE seller_id = %s
            """,
            (self.seller_id,),
        )
        row["products"] = db.query_one(
            "SELECT COUNT(*) AS n FROM product_stats WHERE main_seller_id = %s", (self.seller_id,)
        )["n"]
        return row

    def monthly_sales(self) -> pd.DataFrame:
        return db.query_df(
            """
            SELECT DATE_FORMAT(o.order_purchase_timestamp, '%%Y-%%m-01') AS month,
                   COUNT(DISTINCT i.order_id) AS orders, ROUND(SUM(i.price), 2) AS revenue
            FROM order_items i JOIN orders o USING (order_id)
            WHERE i.seller_id = %s AND o.order_status NOT IN ('canceled', 'unavailable')
            GROUP BY month ORDER BY month
            """,
            (self.seller_id,),
        )

    def top_products(self, limit: int = 5) -> pd.DataFrame:
        return db.query_df(
            """
            SELECT product_id, display_name AS name, category, times_sold AS sold, avg_rating AS rating
            FROM product_stats WHERE main_seller_id = %s ORDER BY times_sold DESC LIMIT %s
            """,
            (self.seller_id, limit),
        )

    def products(self, q: str = "", sort_by: str = "popular", page: int = 1, page_size: int = 25,
                 category: str | None = None) -> tuple[pd.DataFrame, int]:
        order_by = {
            "popular": "times_sold DESC, product_id DESC",
            "rating": "rating_rank DESC, num_reviews DESC, product_id DESC",
            "price_low": "typical_price ASC, product_id ASC",
            "price_high": "typical_price DESC, product_id DESC",
        }.get(sort_by, "times_sold DESC, product_id DESC")
        where, params = ["main_seller_id = %s"], [self.seller_id]
        if category:
            where.append("category = %s")
            params.append(category)
        term = q.strip().lower()
        if term:
            where.append("(display_name LIKE %s OR product_id LIKE %s OR category LIKE %s)")
            params += [f"%{term}%", f"{term}%", f"%{term.replace(' ', '_')}%"]
        where_sql = " AND ".join(where)
        total = db.query_one(f"SELECT COUNT(*) AS n FROM product_stats WHERE {where_sql}", tuple(params))["n"]
        items = db.query_df(
            f"""
            SELECT product_id, display_name AS name, category, typical_price AS price,
                   avg_freight AS shipping, avg_rating AS rating, num_reviews AS reviews, times_sold AS sold
            FROM product_stats WHERE {where_sql}
            ORDER BY {order_by} LIMIT %s OFFSET %s
            """,
            tuple(params) + (page_size, (max(page, 1) - 1) * page_size),
        )
        return items, int(total)

    def categories(self) -> pd.DataFrame:
        """The categories this seller lists products in, largest first."""
        return db.query_df(
            """
            SELECT category, COUNT(*) AS products FROM product_stats
            WHERE main_seller_id = %s GROUP BY category ORDER BY products DESC
            """,
            (self.seller_id,),
        )

    def editable_product(self, product_id: str) -> dict | None:
        """None if the product doesn't exist or belongs to another seller."""
        return _product_for_edit(product_id, self.seller_id)

    def product_edits(self, product_id: str, limit: int = 20) -> pd.DataFrame:
        return _product_edit_history(product_id, limit)

    def create_product(self, values: dict, created_by: str) -> dict:
        """Add a new product owned by this seller. It starts with no sales or reviews."""
        product_id = uuid.uuid4().hex
        with db.transaction() as cur:
            cur.execute(
                """
                INSERT INTO products (product_id, category, display_name, photos,
                                      weight_g, length_cm, height_cm, width_cm)
                VALUES (%s, %s, %s, 0, %s, %s, %s, %s)
                """,
                (product_id, values["category"], values["name"], values.get("weight_g"),
                 values.get("length_cm"), values.get("height_cm"), values.get("width_cm")),
            )
            cur.execute(
                """
                INSERT INTO product_stats (product_id, display_name, category, times_sold,
                                           typical_price, avg_freight, avg_rating, num_reviews, main_seller_id)
                VALUES (%s, %s, %s, 0, %s, %s, NULL, 0, %s)
                """,
                (product_id, values["name"], values["category"], values["price"], values["shipping"], self.seller_id),
            )
            created = {k: {"from": None, "to": v} for k, v in values.items() if k in EDITABLE_PRODUCT_FIELDS}
            cur.execute(
                "INSERT INTO product_edits (product_id, edited_by, editor_role, changes) VALUES (%s, %s, 'seller', %s)",
                (product_id, created_by, json.dumps({"created": True, **created})),
            )
        return self.editable_product(product_id)

    def update_product(self, product_id: str, values: dict, edited_by: str) -> dict:
        before = self.editable_product(product_id)
        if not before:
            raise ValueError("Product not found")
        _apply_product_edit(product_id, before, values, edited_by, "seller")
        return self.editable_product(product_id)

    def orders(self, status: str | None = None, limit: int = 200) -> pd.DataFrame:
        """Orders containing this seller's items; totals cover only their items."""
        sql = """
            SELECT o.order_id, o.order_purchase_timestamp AS purchased_at, o.order_status AS status,
                   COUNT(*) AS items, ROUND(SUM(i.price + i.freight_value), 2) AS order_total,
                   o.order_estimated_delivery_date AS estimated_delivery,
                   o.order_delivered_customer_date AS delivered_at,
                   MAX(i.shipping_limit_date) AS ship_by,
                   ANY_VALUE(c.customer_city) AS customer_city, ANY_VALUE(c.customer_state) AS customer_state
            FROM order_items i
            JOIN orders o USING (order_id)
            JOIN customers c USING (customer_id)
            WHERE i.seller_id = %s
        """
        params: list = [self.seller_id]
        if status:
            sql += " AND o.order_status = %s"
            params.append(status)
        sql += " GROUP BY o.order_id ORDER BY purchased_at DESC LIMIT %s"
        params.append(int(limit))
        return db.query_df(sql, tuple(params))

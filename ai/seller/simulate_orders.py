"""Recent orders for the seller-agent scenarios (docs/seller/TEST_SCENARIOS.md).

    python -m ai.seller.simulate_orders           create (or re-create) the 3 orders
    python -m ai.seller.simulate_orders --undo    remove them

No seller has an order newer than 2026-08-13, so "what do I need to ship?" can't be tested on
the historical data. This adds three recent orders for Seller A (demo seller 1), relative to
today:

  1. due    - paid yesterday, shipping deadline tomorrow            (status approved)
  2. late   - paid 5 days ago, shipping deadline passed 2 days ago  (status invoiced)
  3. sent   - paid 4 days ago, handed to the carrier yesterday      (status shipped)

The buyer is a non-demo customer, so the demo customers' data is unchanged. Order IDs are
derived from fixed labels, so re-running replaces the same rows and --undo removes exactly
them. Rows are written like the store's checkout (orders, order_items, payments).
"""

from __future__ import annotations

import hashlib
import sys
from datetime import datetime, timedelta

import db

SELLER_A = "6560211a19b47992c3666cc44a7e94c0"
LABEL = "seller-agent-scenario"
ESTIMATED_DELIVERY_DAYS = 12


def order_id(n: int) -> str:
    return hashlib.md5(f"{LABEL}-{n}".encode()).hexdigest()


ORDER_IDS = [order_id(n) for n in (1, 2, 3)]


def _buyer(cur) -> str:
    """A customer_id of a non-demo customer: one order, in SP, not one of the sign-in demo accounts
    (the 5 customers with the most orders) and not used by the tests."""
    cur.execute(
        """
        SELECT c.customer_id FROM customers c JOIN orders o USING (customer_id)
        WHERE c.customer_state = 'SP'
          AND c.customer_unique_id IN (SELECT customer_unique_id FROM customers GROUP BY customer_unique_id
                                        HAVING COUNT(*) = 1)
        ORDER BY c.customer_id LIMIT 1
        """
    )
    return cur.fetchone()["customer_id"]


def _products(cur) -> list[dict]:
    cur.execute(
        """
        SELECT product_id, display_name, typical_price AS price, avg_freight AS freight
        FROM product_stats WHERE main_seller_id = %s ORDER BY times_sold DESC LIMIT 3
        """,
        (SELLER_A,),
    )
    return cur.fetchall()


def undo(cur) -> int:
    marks = ", ".join(["%s"] * len(ORDER_IDS))
    cur.execute(f"DELETE FROM payments WHERE order_id IN ({marks})", ORDER_IDS)
    cur.execute(f"DELETE FROM order_items WHERE order_id IN ({marks})", ORDER_IDS)
    return cur.execute(f"DELETE FROM orders WHERE order_id IN ({marks})", ORDER_IDS)


def create() -> list[dict]:
    now = datetime.now().replace(microsecond=0)
    day = timedelta(days=1)
    with db.transaction() as cur:
        undo(cur)   # re-running replaces the same three orders
        buyer, products = _buyer(cur), _products(cur)
        plan = [
            # (label, status, purchased, carrier_date, shipping_limit)
            ("due tomorrow", "approved", now - day, None, now + day),
            ("past its shipping deadline", "invoiced", now - 5 * day, None, now - 2 * day),
            ("shipped yesterday", "shipped", now - 4 * day, now - day, now),
        ]
        created = []
        for oid, product, (label, status, bought, carrier, limit) in zip(ORDER_IDS, products, plan):
            cur.execute(
                """
                INSERT INTO orders (order_id, customer_id, order_status, order_purchase_timestamp, order_approved_at,
                                    order_delivered_carrier_date, order_estimated_delivery_date)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (oid, buyer, status, bought, bought + timedelta(hours=1), carrier,
                 (bought + ESTIMATED_DELIVERY_DAYS * day).date()),
            )
            cur.execute(
                """
                INSERT INTO order_items (order_id, order_item_id, product_id, seller_id, shipping_limit_date,
                                         price, freight_value)
                VALUES (%s, 1, %s, %s, %s, %s, %s)
                """,
                (oid, product["product_id"], SELLER_A, limit, product["price"], product["freight"]),
            )
            cur.execute(
                "INSERT INTO payments (order_id, payment_sequential, payment_type, payment_installments, payment_value) "
                "VALUES (%s, 1, 'credit_card', 1, %s)",
                (oid, round(float(product["price"]) + float(product["freight"]), 2)),
            )
            created.append({"order_id": oid, "scenario": label, "status": status, "product": product["display_name"],
                            "price": float(product["price"]), "shipping_limit": limit})
    return created


if __name__ == "__main__":
    if sys.argv[1:] == ["--undo"]:
        with db.transaction() as cur:
            print(f"removed {undo(cur)} scenario orders")
    elif not sys.argv[1:]:
        for o in create():
            print(f"{o['order_id']}  {o['scenario']:28} {o['status']:9} {o['product']} ${o['price']:.2f} "
                  f"(ship by {o['shipping_limit']:%Y-%m-%d %H:%M})")
    else:
        raise SystemExit("usage: python -m ai.seller.simulate_orders [--undo]")

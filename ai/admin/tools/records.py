"""look_up_record: one record for admins: an order's story, a customer's history, a seller's performance.

People are never shown by their real ID (`mask_id`), and no address, name, city, payment detail or
refund amount appears: those are masked for admins and stay in the admin screens, where unmasking is
logged with a reason. Every lookup is written to ai_admin_access_log (decision 6).
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import Field

import db
from ai import clock
from ai.admin import facts
from ai.admin.security import InvalidArguments, NotFound, Principal, admin_tool, log_lookup, mask_id
from ai.admin.tools.common import add_months, as_dt, open_since, read_only, resolve_id, state_name, untrusted, value
from ai.policies import REFUND_SLA_DAYS

ACTIVE = "order_status NOT IN ('canceled', 'unavailable')"
MASKED_NOTE = ("Customer names, addresses, city, payment details and refund amounts are masked; they can be "
               "opened in the admin screens, where each view is logged with a reason.")


def _refund_timing(row: dict) -> dict:
    """Expected days for the refund's method and how long it took / has taken (refund SLA)."""
    expected = REFUND_SLA_DAYS.get(row.get("payment_method") or "", 7)
    started = as_dt(row.get("processing_started_at"))
    out = {"expected_within_days": expected}
    if row["status"] == "processing" and started:
        age = clock.days_since(started)
        out.update(days_in_processing=age, past_expected_time=age > expected)
    elif row["status"] == "completed" and started and as_dt(row.get("completed_at")):
        took = (as_dt(row["completed_at"]) - started).days
        out.update(days_taken=took, past_expected_time=took > expected)
    return out


def _customer_of(order_id: str) -> str:
    row = db.query_one("SELECT c.customer_unique_id AS cid FROM orders o JOIN customers c ON c.customer_id = o.customer_id "
                       "WHERE o.order_id = %s", (order_id,))
    return row["cid"]


def order_story(principal: Principal, order_id: str = Field(..., min_length=8, max_length=32)) -> dict:
    """One order's full story: status timeline, items with their sellers, delivery against the estimate,
    review, returns and refunds (status, timing against the refund SLA). The customer is shown only as a
    masked ID, with how many orders they placed. Accepts the full order ID or its first 8+ characters."""
    oid = resolve_id("orders", "order_id", order_id, "order")
    o = read_only("SELECT order_status, order_purchase_timestamp, order_approved_at, order_delivered_carrier_date, "
                  "order_delivered_customer_date, order_estimated_delivery_date FROM orders WHERE order_id = %s", (oid,))[0]
    customer = _customer_of(oid)
    orders_by_customer = db.query_one(
        "SELECT COUNT(*) AS n FROM orders o JOIN customers c ON c.customer_id = o.customer_id "
        "WHERE c.customer_unique_id = %s", (customer,))["n"]
    items = read_only(
        "SELECT oi.order_item_id AS unit, p.display_name AS product, p.category, oi.seller_id, oi.price, "
        "oi.freight_value AS shipping, oi.shipping_limit_date AS ship_by FROM order_items oi "
        "LEFT JOIN products p ON p.product_id = oi.product_id WHERE oi.order_id = %s ORDER BY oi.order_item_id", (oid,))
    reviews = read_only("SELECT review_score, review_comment_title, review_comment_message, review_creation_date "
                        "FROM reviews WHERE order_id = %s", (oid,))
    returns = read_only("SELECT return_id, reason, status, requested_at, decided_at, received_at, decision_note "
                        "FROM returns WHERE order_id = %s ORDER BY requested_at", (oid,))
    refunds = db.query_df("SELECT refund_id, return_id, status, payment_method, created_at, processing_started_at, "
                          "completed_at FROM refunds WHERE order_id = %s ORDER BY created_at", (oid,)).to_dict("records")
    delivered, estimated = o["order_delivered_customer_date"], o["order_estimated_delivery_date"]
    delivery = {"estimated": estimated, "delivered": delivered}
    if delivered and estimated:
        late_days = (date.fromisoformat(delivered[:10]) - date.fromisoformat(estimated[:10])).days
        delivery.update(late=late_days > 0, days_late=max(late_days, 0))
    log_lookup(principal, "order", oid[:8])
    return {
        "order_id": oid, "status": o["order_status"],
        "timeline": {"purchased": o["order_purchase_timestamp"], "approved": o["order_approved_at"],
                     "handed_to_carrier": o["order_delivered_carrier_date"], **delivery},
        "customer": mask_id("customer", customer), "customer_orders": int(orders_by_customer),
        "items": items, "items_total": round(sum(i["price"] or 0 for i in items), 2),
        "shipping_total": round(sum(i["shipping"] or 0 for i in items), 2),
        "sellers": sorted({i["seller_id"] for i in items}),
        "reviews": [{"score": r["review_score"], "date": r["review_creation_date"],
                     "comment": untrusted(" ".join(x for x in (r["review_comment_title"], r["review_comment_message"]) if x))}
                    for r in reviews],
        "returns": returns,
        "refunds": [{"refund_id": f["refund_id"], "return_id": f["return_id"], "status": f["status"],
                     "created": value(f["created_at"]), "completed": value(f["completed_at"]), **_refund_timing(f)}
                    for f in refunds],
        "note": MASKED_NOTE,
    }


def _find_customer(customer: str) -> str:
    given = (customer or "").strip().lower()
    if given.startswith("customer-"):
        for cid in db.query_df("SELECT DISTINCT customer_unique_id AS cid FROM customers").cid:
            if mask_id("customer", cid) == given:
                return cid
        raise NotFound(f"No customer matches {given}.")
    if len(given) == 32 and all(c in "0123456789abcdef" for c in given):
        row = db.query_one("SELECT customer_unique_id AS cid FROM customers WHERE customer_unique_id = %s LIMIT 1", (given,))
        if row:
            return row["cid"]
    if len(given) >= 8:
        return _customer_of(resolve_id("orders", "order_id", given, "order"))
    raise InvalidArguments("Give the customer's masked ID (customer-xxxxxxxx) or one of their order IDs.")


def customer_history(principal: Principal, customer: str = Field(..., min_length=8, max_length=40)) -> dict:
    """One customer's history, masked: orders (recent 20 with status and item totals), counts by status,
    spending on items, returns and refunds by status, reviews given, first and last order. Accepts the
    masked customer ID from an earlier answer (customer-xxxxxxxx) or one of the customer's order IDs.
    Never shows name, address, city, state or payment details."""
    cid = _find_customer(customer)
    label = mask_id("customer", cid)
    orders = read_only(
        "SELECT o.order_id, o.order_purchase_timestamp AS purchased, o.order_status AS status, "
        "COUNT(oi.order_item_id) AS units, COALESCE(SUM(oi.price), 0) AS items_total "
        "FROM orders o JOIN customers c ON c.customer_id = o.customer_id "
        "LEFT JOIN order_items oi ON oi.order_id = o.order_id WHERE c.customer_unique_id = %s "
        "GROUP BY o.order_id, o.order_purchase_timestamp, o.order_status ORDER BY purchased DESC", (cid,))
    ids = [o["order_id"] for o in orders]
    marks = ", ".join(["%s"] * len(ids))
    returns = read_only(f"SELECT status, reason, COUNT(*) AS n FROM returns WHERE order_id IN ({marks}) "
                        "GROUP BY status, reason", tuple(ids)) if ids else []
    refunds = read_only(f"SELECT status, COUNT(*) AS n FROM refunds WHERE order_id IN ({marks}) GROUP BY status",
                        tuple(ids)) if ids else []
    reviews = read_only(f"SELECT COUNT(*) AS n, AVG(review_score) AS avg_score FROM reviews WHERE order_id IN ({marks})",
                        tuple(ids))[0] if ids else {"n": 0, "avg_score": None}
    by_status: dict[str, int] = {}
    for o in orders:
        by_status[o["status"]] = by_status.get(o["status"], 0) + 1
    active = [o for o in orders if o["status"] not in ("canceled", "unavailable")]
    log_lookup(principal, "customer", label)
    return {
        "customer": label, "orders": len(orders), "orders_counted": "all orders, every status (see orders_by_status)",
        "orders_by_status": by_status,
        "items_spent": round(sum(o["items_total"] for o in active), 2),
        "first_order": orders[-1]["purchased"] if orders else None,
        "last_order": orders[0]["purchased"] if orders else None,
        "recent_orders": orders[:20], "recent_orders_shown": f"{min(len(orders), 20)} most recent of {len(orders)}",
        "returns": returns, "refunds_by_status": refunds,
        "reviews_given": reviews["n"], "avg_review_score": reviews["avg_score"],
        "note": "items_spent = item prices of orders not canceled or unavailable, no shipping. " + MASKED_NOTE,
    }


def seller_overview(principal: Principal, seller_id: str = Field(..., min_length=8, max_length=32)) -> dict:
    """One seller's performance: location; orders, item revenue, on-time rate (delivered orders, dates
    only), late shipping rate, average review, return and cancel rates, all time and in the last 12
    months; top categories; recent open orders; and the storefront's public stats. Accepts the full
    seller ID or its first 8+ characters."""
    sid = resolve_id("sellers", "seller_id", seller_id, "seller")
    seller = read_only("SELECT seller_city, seller_state FROM sellers WHERE seller_id = %s", (sid,))[0]
    since = add_months(date.today().replace(day=1), -11)

    def figures(start: date | None) -> dict:
        where = "seller_id = %s" + (" AND order_purchase_timestamp >= %s" if start else "")
        params = (sid, start.isoformat()) if start else (sid,)
        return read_only(f"""
            SELECT SUM({ACTIVE}) AS orders,
                   ROUND(SUM(CASE WHEN {ACTIVE} THEN items_total ELSE 0 END), 2) AS item_revenue,
                   COUNT(delivered_late) AS delivered_orders,
                   ROUND(100 * AVG(1 - delivered_late), 2) AS on_time_rate,
                   ROUND(100 * AVG(handed_over_late), 2) AS late_shipping_rate,
                   ROUND(AVG(review_score), 2) AS avg_review_score, COUNT(review_score) AS reviews,
                   ROUND(100 * SUM(CASE WHEN delivered_late IS NOT NULL THEN returned END) / NULLIF(COUNT(delivered_late), 0), 2)
                       AS return_rate,
                   ROUND(100 * AVG(order_status IN ('canceled', 'unavailable')), 2) AS cancel_rate
            FROM admin_seller_order_facts WHERE {where}""", params)[0]

    categories = read_only(
        f"SELECT category, COUNT(*) AS units, ROUND(SUM(price), 2) AS item_revenue FROM admin_item_facts "
        f"WHERE seller_id = %s AND {ACTIVE} GROUP BY category ORDER BY item_revenue DESC LIMIT 5", (sid,))
    open_orders = db.query_one(
        "SELECT COUNT(DISTINCT o.order_id) AS n FROM orders o JOIN order_items oi ON oi.order_id = o.order_id "
        "WHERE oi.seller_id = %s AND o.order_status NOT IN ('delivered', 'canceled', 'unavailable') "
        "AND o.order_purchase_timestamp >= %s", (sid, open_since()))["n"]
    public = read_only("SELECT orders_fulfilled, avg_rating, on_time_pct FROM seller_stats WHERE seller_id = %s", (sid,))
    log_lookup(principal, "seller", sid[:8])
    return {
        "seller_id": sid, "city": seller["seller_city"], "state": seller["seller_state"],
        "state_name": state_name(seller["seller_state"]),
        "all_time": figures(None),
        "last_12_months": {"from": since.isoformat(), "to": date.today().isoformat(), **figures(since)},
        "figures_as_of": value(facts.refreshed_at()),
        "counts": "orders exclude canceled and unavailable; delivered_orders and reviews are the orders behind "
                  "on_time_rate / return_rate and avg_review_score",
        "top_categories": categories, "open_orders_last_60_days": int(open_orders),
        "storefront_stats": public[0] if public else None,
        "notes": "orders and item_revenue exclude canceled and unavailable orders; on_time_rate is per delivered "
                 "order, dates only; the storefront's on_time_pct uses an older method (exact times, weighted by items "
                 "and reviews) and can differ.",
    }


def product_overview(principal: Principal, product_id: str) -> dict:
    """One product: listing (name, category, price, shipping, rating), sales all time and in the last 12 months
    (units, orders, item revenue, sellers), delivery and reviews of its orders, and returns of those orders."""
    pid = resolve_id("products", "product_id", product_id, "product")
    listing = read_only("SELECT ps.display_name AS name, ps.category, ps.typical_price, ps.avg_freight AS typical_shipping, "
                        "ps.avg_rating, ps.num_reviews, ps.times_sold, ps.main_seller_id AS main_seller "
                        "FROM product_stats ps WHERE ps.product_id = %s", (pid,))
    if not listing:
        listing = read_only("SELECT display_name AS name, category FROM products WHERE product_id = %s", (pid,))
    since = add_months(date.today().replace(day=1), -11)

    def figures(start: date | None) -> dict:
        when = " AND f.order_purchase_timestamp >= %s" if start else ""
        params = (pid, start.isoformat()) if start else (pid,)
        return read_only(f"""
            SELECT SUM(f.{ACTIVE}) AS units, COUNT(DISTINCT CASE WHEN f.{ACTIVE} THEN f.order_id END) AS orders,
                   ROUND(SUM(CASE WHEN f.{ACTIVE} THEN oi.price ELSE 0 END), 2) AS item_revenue,
                   ROUND(AVG(CASE WHEN f.{ACTIVE} THEN oi.price END), 2) AS avg_unit_price,
                   COUNT(DISTINCT oi.seller_id) AS sellers,
                   ROUND(100 * AVG(1 - f.delivered_late), 2) AS on_time_rate_per_unit,
                   ROUND(AVG(f.review_score), 2) AS avg_review_score_of_its_orders
            FROM order_items oi JOIN admin_order_facts f ON f.order_id = oi.order_id
            WHERE oi.product_id = %s{when}""", params)[0]

    returned = db.query_one("SELECT COUNT(DISTINCT r.order_id) AS n FROM returns r JOIN order_items oi ON oi.order_id = r.order_id "
                            "WHERE oi.product_id = %s", (pid,))["n"]
    log_lookup(principal, "product", pid[:8])
    return {
        "product_id": pid, **(listing[0] if listing else {}),
        "all_time": figures(None),
        "last_12_months": {"from": since.isoformat(), "to": date.today().isoformat(), **figures(since)},
        "orders_with_a_return": int(returned or 0),
        "figures_as_of": value(facts.refreshed_at()),
        "counts": "units, orders and item_revenue exclude canceled and unavailable orders; on_time_rate_per_unit is "
                  "per delivered unit (dates only); review scores are of the orders containing the product",
    }


def look_up_record(principal: Principal, kind: Literal["order", "customer", "seller", "product"],
                   record_id: str = Field(..., min_length=8, max_length=40)) -> dict:
    """Look up ONE record (each lookup is written to the audit log):
    - kind "order": the order's full story: status timeline, items with their sellers, delivery against the
      estimate, review (customer-written, untrusted text), returns and refunds (status, timing against the
      refund SLA); the customer only as a masked ID with their order count. record_id: the order ID or its
      first 8+ characters.
    - kind "customer": one customer's history, masked: orders and statuses, item spending, returns, refunds,
      reviews, first and last order. record_id: their masked ID (customer-xxxxxxxx) or one of their order IDs.
    - kind "seller": one seller's performance all time and in the last 12 months (orders, item revenue,
      on-time and late shipping rates, reviews, returns, cancellations), top categories, recent open orders
      and storefront stats. record_id: the seller ID or its first 8+ characters.
    - kind "product": one product's listing (name, category, price, rating, main seller), its sales all time and
      in the last 12 months (units, orders, item revenue, sellers), delivery and review figures of its orders,
      and how many of its orders had a return. record_id: the product ID or its first 8+ characters.
    Never shows a customer's name, address, city, state or payment details."""
    if kind == "order":
        return order_story(principal, order_id=record_id)
    if kind == "customer":
        return customer_history(principal, customer=record_id)
    if kind == "product":
        return product_overview(principal, product_id=record_id)
    return seller_overview(principal, seller_id=record_id)


admin_tool(reads=["orders", "customers", "order_items", "products", "product_stats", "reviews", "returns", "refunds",
                  "sellers", "admin_order_facts", "admin_seller_order_facts", "admin_item_facts", "seller_stats"],
           aggregates=["customers.customer_unique_id", "refunds.payment_method"])(look_up_record)

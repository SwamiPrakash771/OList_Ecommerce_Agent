"""Seller tools: the shop, orders to ship, one order, products, reviews, returns, listing edits.

Every query is limited to the signed-in seller (principal.seller_id from the session) and runs on
the seller agent's own MySQL user (column-level grants). In orders shared with other sellers only
this seller's items are ever returned. Another seller's order or product gives the same
"not found" as a missing one.
"""

from __future__ import annotations

import json

from pydantic import Field

import db
from ai.security import ToolKind
from ai.seller.security import InvalidArguments, NotFound, Principal, seller_tool
from ai.seller.tools.common import as_dt, iso, now, num, open_since, untrusted

ACTIVE = "o.order_status NOT IN ('canceled', 'unavailable')"
NOT_DONE = ('created', 'approved', 'invoiced', 'processing', 'shipped')
EDITOR_ROLE = "seller_assistant"   # product_edits.editor_role for changes made through the assistant
OrderId = Field(..., pattern=r"^[0-9a-f]{32}$", description="32-character order ID")
ProductId = Field(..., pattern=r"^[0-9a-f]{32}$", description="32-character product ID")


def _own_product(principal: Principal, product_id: str) -> dict:
    row = db.query_one(
        """
        SELECT ps.product_id, ps.display_name, ps.category, ps.typical_price, ps.avg_freight, ps.times_sold,
               ps.avg_rating, ps.num_reviews, p.weight_g, p.length_cm, p.height_cm, p.width_cm
        FROM product_stats ps JOIN products p ON p.product_id = ps.product_id
        WHERE ps.product_id = %s AND ps.main_seller_id = %s
        """,
        (product_id, principal.seller_id),
    )
    if not row:
        raise NotFound("product")
    return row


# ---- the shop -----------------------------------------------------------------------------------


@seller_tool(reads=["sellers", "seller_stats", "product_stats", "order_items", "orders"])
def my_shop(principal: Principal) -> dict:
    """The seller's shop at a glance: location, public rating and on-time rate, number of products,
    and all-time sales (revenue = own item prices, excluding canceled orders; shipping not included)."""
    sid = principal.seller_id
    profile = db.query_one(
        """
        SELECT s.seller_city, s.seller_state, st.orders_fulfilled, st.avg_rating, st.on_time_pct
        FROM sellers s JOIN seller_stats st ON st.seller_id = s.seller_id WHERE s.seller_id = %s
        """,
        (sid,),
    ) or {}
    sales = db.query_one(
        f"""
        SELECT COUNT(*) AS units, COUNT(DISTINCT i.order_id) AS orders, COALESCE(SUM(i.price), 0) AS revenue,
               MIN(o.order_purchase_timestamp) AS first_order, MAX(o.order_purchase_timestamp) AS last_order
        FROM order_items i JOIN orders o ON o.order_id = i.order_id WHERE i.seller_id = %s AND {ACTIVE}
        """,
        (sid,),
    )
    products = db.query_one("SELECT COUNT(*) AS n FROM product_stats WHERE main_seller_id = %s", (sid,))
    return {
        "city": profile.get("seller_city"), "state": profile.get("seller_state"),
        "public_rating": num(profile.get("avg_rating")), "on_time_pct": num(profile.get("on_time_pct")),
        "products_listed": int(products["n"]),
        "all_time": {"revenue": num(sales["revenue"]), "orders": int(sales["orders"]), "units": int(sales["units"]),
                     "first_order": iso(sales["first_order"]), "last_order": iso(sales["last_order"])},
        "today": now().date().isoformat(),
    }


# ---- orders -------------------------------------------------------------------------------------


@seller_tool(reads=["orders", "order_items", "products", "customers"])
def orders_to_ship(principal: Principal) -> dict:
    """Open orders of the last 60 days: to ship (with the shipping deadline; overdue ones first) and
    recently handed to the carrier. Older orders still marked shipped/invoiced never completed in
    the records; they are counted separately and are not work to do."""
    rows = db.query_df(
        f"""
        SELECT o.order_id, o.order_status, o.order_purchase_timestamp, o.order_delivered_carrier_date,
               MIN(i.shipping_limit_date) AS deadline, COUNT(*) AS units, SUM(i.price) AS value,
               GROUP_CONCAT(DISTINCT p.display_name ORDER BY p.display_name SEPARATOR ', ') AS products,
               c.customer_city, c.customer_state
        FROM orders o
        JOIN order_items i ON i.order_id = o.order_id AND i.seller_id = %s
        JOIN products p ON p.product_id = i.product_id
        JOIN customers c ON c.customer_id = o.customer_id
        WHERE o.order_status IN ({', '.join(['%s'] * len(NOT_DONE))})
        GROUP BY o.order_id, o.order_status, o.order_purchase_timestamp, o.order_delivered_carrier_date,
                 c.customer_city, c.customer_state
        ORDER BY deadline
        """,
        (principal.seller_id, *NOT_DONE),
    ).to_dict("records")
    t, since = now(), open_since()
    to_ship, in_transit, stale = [], [], []
    for r in rows:
        bought, deadline = as_dt(r["order_purchase_timestamp"]), as_dt(r["deadline"])
        if bought < since:
            stale.append(bought)
            continue
        entry = {"order_id": r["order_id"], "products": r["products"], "status": r["order_status"],
                 "bought": iso(bought), "units": int(r["units"]),
                 "value": num(r["value"]), "buyer_city": r["customer_city"], "buyer_state": r["customer_state"],
                 "shipping_deadline": iso(deadline)}
        carrier = as_dt(r["order_delivered_carrier_date"])   # empty dates arrive as NaT, not None
        if carrier or r["order_status"] == "shipped":
            entry["handed_to_carrier"] = iso(carrier)
            in_transit.append(entry)
        else:
            hours = (deadline - t).total_seconds() / 3600 if deadline else None
            entry["overdue"] = hours is not None and hours < 0
            entry["deadline_in_days" if hours is not None and hours >= 0 else "overdue_by_days"] = (
                round(abs(hours) / 24, 1) if hours is not None else None)
            to_ship.append(entry)
    to_ship.sort(key=lambda e: (not e["overdue"], e["shipping_deadline"] or ""))
    return {
        "to_ship": to_ship,
        "in_transit": in_transit,
        "old_unfinished": {"count": len(stale), "oldest": iso(min(stale)) if stale else None,
                           "note": "Older than 60 days and never completed in the records; not work to do."},
        "today": t.date().isoformat(),
    }


@seller_tool(reads=["orders", "order_items", "products", "customers", "returns", "refunds", "reviews"])
def order_details(principal: Principal, order_id: str = OrderId) -> dict:
    """One of the seller's orders: status and dates, only this seller's items (with shipping deadline
    and whether they were handed to the carrier late), buyer city/state, review, returns and refunds."""
    items = db.query_df(
        """
        SELECT i.order_item_id, i.product_id, p.display_name, i.price, i.freight_value, i.shipping_limit_date
        FROM order_items i JOIN products p ON p.product_id = i.product_id
        WHERE i.order_id = %s AND i.seller_id = %s ORDER BY i.order_item_id
        """,
        (order_id, principal.seller_id),
    ).to_dict("records")
    if not items:
        raise NotFound("order")
    o = db.query_one(
        """
        SELECT o.order_status, o.order_purchase_timestamp, o.order_approved_at, o.order_delivered_carrier_date,
               o.order_delivered_customer_date, o.order_estimated_delivery_date, c.customer_city, c.customer_state
        FROM orders o JOIN customers c ON c.customer_id = o.customer_id WHERE o.order_id = %s
        """,
        (order_id,),
    )
    carrier = as_dt(o["order_delivered_carrier_date"])
    delivered, estimated = as_dt(o["order_delivered_customer_date"]), as_dt(o["order_estimated_delivery_date"])
    review = db.query_one("SELECT review_score, review_comment_message FROM reviews WHERE order_id = %s LIMIT 1",
                          (order_id,))
    returns = db.query_df(
        """
        SELECT r.return_id, r.reason, r.status, r.requested_at, f.amount AS refund_amount, f.status AS refund_status
        FROM returns r LEFT JOIN refunds f ON f.return_id = r.return_id WHERE r.order_id = %s
        """,
        (order_id,),
    ).to_dict("records")
    return {
        "order_id": order_id, "status": o["order_status"], "bought": iso(o["order_purchase_timestamp"]),
        "approved": iso(o["order_approved_at"]), "handed_to_carrier": iso(carrier),
        "delivered": iso(delivered), "estimated_delivery": iso(estimated),
        "delivered_late": bool(delivered and estimated and delivered.date() > estimated.date()),
        "buyer_city": o["customer_city"], "buyer_state": o["customer_state"],
        "your_items": [{"product_id": i["product_id"], "product": i["display_name"], "price": num(i["price"]),
                        "shipping": num(i["freight_value"]), "shipping_deadline": iso(i["shipping_limit_date"]),
                        "handed_over_late": bool(carrier and as_dt(i["shipping_limit_date"])
                                                 and carrier > as_dt(i["shipping_limit_date"]))} for i in items],
        "review": {"score": int(review["review_score"]), "comment": untrusted(review["review_comment_message"])}
                  if review else None,
        "returns": [{"return_id": r["return_id"], "reason": r["reason"], "status": r["status"],
                     "requested": iso(r["requested_at"]), "refund_amount": num(r["refund_amount"]),
                     "refund_status": r["refund_status"]} for r in returns],
        "note": "Only your items are shown; other sellers' items in the same order are never visible.",
    }


# ---- products and reviews -----------------------------------------------------------------------


@seller_tool(reads=["product_stats", "products", "order_items", "orders", "product_edits"])
def my_product(principal: Principal, product_id: str = ProductId) -> dict:
    """One of the seller's products: listing (name, category, price, shipping, weight and size),
    sales, rating, and its last edits."""
    p = _own_product(principal, product_id)
    sales = db.query_one(
        f"""
        SELECT COUNT(*) AS units, COALESCE(SUM(i.price), 0) AS revenue, MAX(o.order_purchase_timestamp) AS last_sold
        FROM order_items i JOIN orders o ON o.order_id = i.order_id
        WHERE i.product_id = %s AND i.seller_id = %s AND {ACTIVE}
        """,
        (product_id, principal.seller_id),
    )
    edits = db.query_df(
        "SELECT editor_role, changes, edited_at FROM product_edits WHERE product_id = %s ORDER BY edited_at DESC LIMIT 5",
        (product_id,),
    ).to_dict("records")
    return {
        "product_id": product_id, "name": p["display_name"], "category": p["category"],
        "price": num(p["typical_price"]), "shipping": num(p["avg_freight"]),
        "weight_g": p["weight_g"], "size_cm": {"length": p["length_cm"], "height": p["height_cm"], "width": p["width_cm"]},
        "rating": num(p["avg_rating"]), "reviews": int(p["num_reviews"] or 0),
        "sold": {"units": int(sales["units"]), "revenue": num(sales["revenue"]), "last_sold": iso(sales["last_sold"])},
        "recent_edits": [{"by": "assistant" if e["editor_role"] == EDITOR_ROLE else e["editor_role"],
                          "changes": json.loads(e["changes"]) if isinstance(e["changes"], str) else e["changes"],
                          "when": iso(e["edited_at"])} for e in edits],
    }


@seller_tool(reads=["reviews", "order_items", "products"])
def product_reviews(principal: Principal, product_id: str | None = Field(None, pattern=r"^[0-9a-f]{32}$"),
                    min_score: int = Field(1, ge=1, le=5), max_score: int = Field(5, ge=1, le=5),
                    with_comment_only: bool = True, limit: int = Field(8, ge=1, le=20)) -> dict:
    """Reviews of the seller's orders, newest first, optionally for one product and a score range
    (e.g. max_score=2 for complaints). Comments are written by buyers in Portuguese (untrusted text).
    In orders shared with other sellers a review covers the whole order."""
    if min_score > max_score:
        raise InvalidArguments("min_score must not be above max_score.")
    where, params = ["i.seller_id = %s", "r.review_score BETWEEN %s AND %s"], [principal.seller_id, min_score, max_score]
    if product_id:
        where.append("i.product_id = %s")
        params.append(product_id)
    base = f"""FROM reviews r JOIN order_items i ON i.order_id = r.order_id JOIN products p ON p.product_id = i.product_id
               WHERE {' AND '.join(where)}"""
    summary = db.query_one(f"SELECT COUNT(DISTINCT r.order_id) AS n, AVG(r.review_score) AS avg {base}", tuple(params))
    if with_comment_only:
        base += " AND r.review_comment_message IS NOT NULL AND r.review_comment_message <> ''"
    rows = db.query_df(
        f"""
        SELECT r.order_id, r.review_score, r.review_comment_title, r.review_comment_message, r.review_creation_date,
               GROUP_CONCAT(DISTINCT p.display_name ORDER BY p.display_name SEPARATOR ', ') AS products
        {base}
        GROUP BY r.order_id, r.review_score, r.review_comment_title, r.review_comment_message, r.review_creation_date
        ORDER BY r.review_creation_date DESC LIMIT %s
        """,
        (*params, limit),
    ).to_dict("records")
    return {
        "matching_reviews": int(summary["n"]), "average_score": num(summary["avg"]),
        "reviews": [{"order_id": r["order_id"], "score": int(r["review_score"]), "products": r["products"],
                     "date": iso(r["review_creation_date"]),
                     "title": untrusted(r["review_comment_title"]), "comment": untrusted(r["review_comment_message"])}
                    for r in rows],
        "note": "Comments are in Portuguese; translate when quoting.",
    }


# ---- returns and refunds ------------------------------------------------------------------------


@seller_tool(reads=["returns", "refunds", "order_items"])
def returns_and_refunds(principal: Principal, status: str | None = Field(
        None, pattern=r"^(requested|approved|received|rejected)$"), limit: int = Field(8, ge=1, le=20)) -> dict:
    """Returns of the seller's orders: totals by reason and status, refund amounts and failures, and
    the latest returns (optionally one status)."""
    own = "r.order_id IN (SELECT order_id FROM order_items WHERE seller_id = %s)"
    sid = principal.seller_id
    by_reason = db.query_df(f"SELECT r.reason, COUNT(*) AS n FROM returns r WHERE {own} GROUP BY r.reason ORDER BY n DESC",
                            (sid,))
    by_status = db.query_df(f"SELECT r.status, COUNT(*) AS n FROM returns r WHERE {own} GROUP BY r.status", (sid,))
    refunds = db.query_df(
        f"""SELECT f.status, COUNT(*) AS n, COALESCE(SUM(f.amount), 0) AS amount
            FROM refunds f JOIN returns r ON r.return_id = f.return_id WHERE {own} GROUP BY f.status""", (sid,))
    latest = db.query_df(
        f"""
        SELECT r.return_id, r.order_id, r.reason, r.status, r.requested_at, r.decision_note,
               f.amount AS refund_amount, f.status AS refund_status
        FROM returns r LEFT JOIN refunds f ON f.return_id = r.return_id
        WHERE {own} {"AND r.status = %s" if status else ""} ORDER BY r.requested_at DESC LIMIT %s
        """,
        (sid, *([status] if status else []), limit),
    ).to_dict("records")
    return {
        "total_returns": int(by_reason.n.sum()) if len(by_reason) else 0,
        "by_reason": dict(zip(by_reason.reason, map(int, by_reason.n))),
        "by_status": dict(zip(by_status.status, map(int, by_status.n))),
        "refunds_by_status": {r["status"]: {"count": int(r["n"]), "amount": num(r["amount"])}
                              for r in refunds.to_dict("records")},
        "latest": [{"return_id": r["return_id"], "order_id": r["order_id"], "reason": r["reason"], "status": r["status"],
                    "requested": iso(r["requested_at"]), "decision_note": r["decision_note"],
                    "refund_amount": num(r["refund_amount"]), "refund_status": r["refund_status"]} for r in latest],
    }


# ---- listing edits (with the seller's confirmation) ---------------------------------------------

LISTING_FIELDS = {   # tool argument -> (table, column, label)
    "name": ("both", "display_name", "Name"),
    "category": ("both", "category", "Category"),
    "price": ("product_stats", "typical_price", "Price"),
    "shipping": ("product_stats", "avg_freight", "Shipping"),
    "weight_g": ("products", "weight_g", "Weight (g)"),
    "length_cm": ("products", "length_cm", "Length (cm)"),
    "height_cm": ("products", "height_cm", "Height (cm)"),
    "width_cm": ("products", "width_cm", "Width (cm)"),
}


def _changes(principal, product_id, name, category, price, shipping, weight_g, length_cm, height_cm, width_cm) -> tuple[dict, dict]:
    """The requested changes as {field: (before, after)}, checked like the seller center's form."""
    p = _own_product(principal, product_id)
    asked = {"name": name.strip() if name else name, "category": category, "price": price, "shipping": shipping,
             "weight_g": weight_g, "length_cm": length_cm, "height_cm": height_cm, "width_cm": width_cm}
    current = {"name": p["display_name"], "category": p["category"], "price": num(p["typical_price"]),
               "shipping": num(p["avg_freight"]), "weight_g": p["weight_g"], "length_cm": p["length_cm"],
               "height_cm": p["height_cm"], "width_cm": p["width_cm"]}
    if category is not None and not db.query_one(
            "SELECT 1 AS ok FROM product_stats WHERE category = %s LIMIT 1", (category,)):
        raise InvalidArguments(f"'{category}' is not a store category.")
    changes = {k: (current[k], v) for k, v in asked.items() if v is not None and v != current[k]}
    if not changes:
        raise InvalidArguments("Nothing to change: give at least one new value that differs from the listing.")
    return p, changes


def preview_update_listing(principal: Principal, product_id: str = ProductId,
                           name: str | None = Field(None, min_length=3, max_length=100), category: str | None = None,
                           price: float | None = Field(None, gt=0, le=100_000),
                           shipping: float | None = Field(None, ge=0, le=10_000),
                           weight_g: int | None = Field(None, ge=0, le=100_000),
                           length_cm: int | None = Field(None, ge=0, le=300),
                           height_cm: int | None = Field(None, ge=0, le=300),
                           width_cm: int | None = Field(None, ge=0, le=300)) -> dict:
    p, changes = _changes(principal, product_id, name, category, price, shipping, weight_g, length_cm, height_cm, width_cm)
    return {"title": f"Update {p['display_name']}",
            "lines": [f"{LISTING_FIELDS[k][2]}: {before} → {after}" for k, (before, after) in changes.items()]}


@seller_tool(kind=ToolKind.WRITE_CONFIRM, reads=["product_stats", "products"],
             writes=["products", "product_stats", "product_edits"], preview=preview_update_listing)
def update_listing(principal: Principal, product_id: str = ProductId,
                   name: str | None = Field(None, min_length=3, max_length=100), category: str | None = None,
                   price: float | None = Field(None, gt=0, le=100_000),
                   shipping: float | None = Field(None, ge=0, le=10_000),
                   weight_g: int | None = Field(None, ge=0, le=100_000),
                   length_cm: int | None = Field(None, ge=0, le=300),
                   height_cm: int | None = Field(None, ge=0, le=300),
                   width_cm: int | None = Field(None, ge=0, le=300)) -> dict:
    """Change the listing of one of the seller's products: name, category, price, shipping, weight
    or size. Give only the fields to change. The seller sees a confirmation card with old and new
    values; nothing changes until they click Confirm. The change is recorded in the edit history."""
    _, changes = _changes(principal, product_id, name, category, price, shipping, weight_g, length_cm, height_cm, width_cm)
    sets = {"products": [], "product_stats": []}
    for field, (_, after) in changes.items():
        table, column, _ = LISTING_FIELDS[field]
        for t in (["products", "product_stats"] if table == "both" else [table]):
            sets[t].append((column, after))
    with db.transaction() as cur:
        for table, cols in sets.items():
            if cols:
                cur.execute(f"UPDATE {table} SET {', '.join(f'{c} = %s' for c, _ in cols)} WHERE product_id = %s",
                            (*[v for _, v in cols], product_id))
        cur.execute(
            "INSERT INTO product_edits (product_id, edited_by, editor_role, changes) VALUES (%s, %s, %s, %s)",
            (product_id, (principal.username or principal.seller_id)[:64], EDITOR_ROLE,
             json.dumps({k: {"from": b, "to": a} for k, (b, a) in changes.items()})),
        )
    return {"updated": product_id, "changes": {k: {"from": b, "to": a} for k, (b, a) in changes.items()}}

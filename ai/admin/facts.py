"""Reporting tables behind admin_metric (the admin agent's semantic layer).

    python -m ai.admin.facts          build or refresh them now

Building the facts from the store tables takes several seconds (100k orders, 110k items, payments,
reviews and every customer's first order; 1-2 minutes on a laptop), too slow for each question, so
they are materialized and refreshed every hour by the scheduler (job `admin_facts_refresh`). A refresh builds new copies
and swaps them in atomically, so a question never sees a half-built table. Answers state the time of
the last refresh.

  admin_order_facts         one row per order (all its items and payments)
  admin_seller_order_facts  one row per order and seller (that seller's items only)
  admin_item_facts          one row per unit sold

The customer appears only as `customer_key`, a one-way hash used to count distinct people; it and
the buyer's state and payments are personal columns (masked for admins, aggregate-only).
Built by the app user; the admin agent may only SELECT them (ai/admin/db_user.py).
"""

from __future__ import annotations

from datetime import datetime

import db

TABLES = ("admin_order_facts", "admin_seller_order_facts", "admin_item_facts")

# Built in stages on one connection: each part goes into an indexed temporary table first, so
# the final join is fast (one big statement with derived tables took minutes on a small machine).
STAGES = [
    """CREATE TEMPORARY TABLE stage_items (PRIMARY KEY (order_id)) AS
       SELECT order_id, COUNT(*) AS item_count, SUM(price) AS items_total, SUM(freight_value) AS shipping_total
       FROM order_items GROUP BY order_id""",
    """CREATE TEMPORARY TABLE stage_pays (PRIMARY KEY (order_id)) AS
       SELECT order_id, SUM(payment_value) AS payments_total,
              SUBSTRING_INDEX(GROUP_CONCAT(payment_type ORDER BY payment_value DESC), ',', 1) AS payment_type
       FROM payments GROUP BY order_id""",
    """CREATE TEMPORARY TABLE stage_revs (PRIMARY KEY (order_id)) AS
       SELECT order_id, AVG(review_score) AS review_score FROM reviews GROUP BY order_id""",
    """CREATE TEMPORARY TABLE stage_firsts (PRIMARY KEY (customer_unique_id)) AS
       SELECT c.customer_unique_id, MIN(o.order_purchase_timestamp) AS first_at
       FROM orders o JOIN customers c ON c.customer_id = o.customer_id GROUP BY c.customer_unique_id""",
    """CREATE TEMPORARY TABLE stage_rets (PRIMARY KEY (order_id)) AS SELECT DISTINCT order_id FROM returns""",
]

ORDER_FACTS = """
CREATE TABLE {name} (PRIMARY KEY (order_id), INDEX idx_purchase (order_purchase_timestamp)) AS
SELECT o.order_id, o.order_status, o.order_purchase_timestamp, c.customer_state AS buyer_state,
       SHA2(c.customer_unique_id, 256) AS customer_key,
       COALESCE(i.item_count, 0) AS item_count, COALESCE(i.items_total, 0) AS items_total,
       COALESCE(i.shipping_total, 0) AS shipping_total, p.payments_total, p.payment_type,
       CASE WHEN o.order_status = 'delivered' AND o.order_delivered_customer_date IS NOT NULL
            THEN DATE(o.order_delivered_customer_date) > DATE(o.order_estimated_delivery_date) END AS delivered_late,
       CASE WHEN o.order_status = 'delivered' AND o.order_delivered_customer_date IS NOT NULL
            THEN TIMESTAMPDIFF(SECOND, o.order_purchase_timestamp, o.order_delivered_customer_date) / 86400 END
            AS delivery_days,
       r.review_score, rt.order_id IS NOT NULL AS returned,
       o.order_purchase_timestamp = f.first_at AS first_order, o.order_delivered_carrier_date
FROM orders o JOIN customers c ON c.customer_id = o.customer_id
     JOIN stage_firsts f ON f.customer_unique_id = c.customer_unique_id
     LEFT JOIN stage_items i ON i.order_id = o.order_id LEFT JOIN stage_pays p ON p.order_id = o.order_id
     LEFT JOIN stage_revs r ON r.order_id = o.order_id LEFT JOIN stage_rets rt ON rt.order_id = o.order_id
"""

SELLER_ORDER_FACTS = """
CREATE TABLE {name} (PRIMARY KEY (order_id, seller_id), INDEX idx_purchase (order_purchase_timestamp),
                     INDEX idx_seller (seller_id)) AS
SELECT f.order_id, si.seller_id, s.seller_state, si.item_count, si.items_total, si.shipping_total,
       f.order_status, f.order_purchase_timestamp, f.buyer_state, f.delivered_late, f.delivery_days,
       f.review_score, f.returned,
       CASE WHEN f.order_delivered_carrier_date IS NOT NULL THEN f.order_delivered_carrier_date > si.first_limit END
            AS handed_over_late
FROM (SELECT order_id, seller_id, COUNT(*) AS item_count, SUM(price) AS items_total,
             SUM(freight_value) AS shipping_total, MIN(shipping_limit_date) AS first_limit
      FROM order_items GROUP BY order_id, seller_id) si
     JOIN {orders} f ON f.order_id = si.order_id JOIN sellers s ON s.seller_id = si.seller_id
"""

ITEM_FACTS = """
CREATE TABLE {name} (PRIMARY KEY (order_id, order_item_id), INDEX idx_purchase (order_purchase_timestamp),
                     -- covering indexes: rankings by category or seller read only these columns
                     INDEX idx_category (category, order_status, order_purchase_timestamp, price),
                     INDEX idx_seller (seller_id, order_status, order_purchase_timestamp, price)) AS
SELECT f.order_id, oi.order_item_id, oi.seller_id, s.seller_state, oi.price, oi.freight_value, pr.category,
       pr.display_name AS product, f.order_status, f.order_purchase_timestamp, f.buyer_state,
       f.delivered_late, f.review_score
FROM order_items oi JOIN {orders} f ON f.order_id = oi.order_id
     JOIN sellers s ON s.seller_id = oi.seller_id LEFT JOIN products pr ON pr.product_id = oi.product_id
"""


def refresh() -> dict[str, int]:
    """Build new copies of the three tables and swap them in at once."""
    con = db.connect()   # no read timeout: a full build can take a minute
    try:
        with con.cursor() as cur:
            for t in TABLES:
                cur.execute(f"DROP TABLE IF EXISTS {t}_new")
            for stage in STAGES:
                cur.execute(stage)
            cur.execute(ORDER_FACTS.format(name="admin_order_facts_new"))
            cur.execute(SELLER_ORDER_FACTS.format(name="admin_seller_order_facts_new", orders="admin_order_facts_new"))
            cur.execute(ITEM_FACTS.format(name="admin_item_facts_new", orders="admin_order_facts_new"))
            cur.execute("SELECT table_name AS t FROM information_schema.tables WHERE table_schema = DATABASE() "
                        "AND table_name IN (%s, %s, %s)", TABLES)
            existing = {r["t"] for r in cur.fetchall()}
            renames = [f"{t} TO {t}_old" for t in TABLES if t in existing] + [f"{t}_new TO {t}" for t in TABLES]
            cur.execute("RENAME TABLE " + ", ".join(renames))   # atomic: readers see old or new, never neither
            for t in TABLES:
                cur.execute(f"DROP TABLE IF EXISTS {t}_old")
            counts = {}
            for t in TABLES:
                cur.execute(f"SELECT COUNT(*) AS n FROM {t}")
                counts[t] = int(cur.fetchone()["n"])
            return counts
    finally:
        con.close()   # temporary stage tables go with the connection


def refreshed_at() -> datetime | None:
    """When the facts were last rebuilt (the tables' creation time)."""
    row = db.query_one("SELECT MIN(create_time) AS at FROM information_schema.tables "
                       "WHERE table_schema = DATABASE() AND table_name = 'admin_order_facts'")
    return row["at"] if row else None


if __name__ == "__main__":
    import time

    started = time.time()
    counts = refresh()
    print(f"admin facts refreshed in {time.time() - started:.1f}s: {counts}")

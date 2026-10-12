"""Move the historical Olist dates forward so the data looks recent (2024-09 to 2026-10).

    python shift_dates.py          # apply ai.clock.DATE_SHIFT_DAYS (416 weeks)
    python shift_dates.py --undo   # move everything back to the original 2016-2018 dates

Moves, by the same number of days (so every gap, delay and age stays the same):
- orders: purchase, approval, carrier, delivery and estimated delivery dates
- order_items: shipping limit date
- reviews: creation and answer dates
- simulated returns, refunds and refund events (ai/simulate_returns.py)

Leaves alone everything created in the app (orders placed through checkout, returns requested
through the assistant, carts, tickets, accounts, AI records): those already carry real dates.
App orders are told apart by date: anything after the original data's end (plus any shift already
applied) was placed in the app.

The shift applied is stored in table dataset_shift, so running this twice doesn't move dates twice.
It runs as one transaction: either every table moves, or none does.
"""

from __future__ import annotations

import sys
from datetime import timedelta

import db
from ai.clock import DATE_SHIFT_DAYS, ORIGINAL_DATASET_END

DATE_COLUMNS = {
    "orders": ["order_purchase_timestamp", "order_approved_at", "order_delivered_carrier_date",
               "order_delivered_customer_date", "order_estimated_delivery_date"],
    "order_items": ["shipping_limit_date"],
    "reviews": ["review_creation_date", "review_answer_timestamp"],
}
SIMULATED_COLUMNS = {
    "returns": ["requested_at", "decided_at", "received_at"],
    "refunds": ["created_at", "processing_started_at", "completed_at"],
}


def applied_shift() -> int:
    db.execute("""CREATE TABLE IF NOT EXISTS dataset_shift (
                    id TINYINT PRIMARY KEY, days_applied INT NOT NULL, note VARCHAR(255), updated_at DATETIME NOT NULL)""")
    row = db.query_one("SELECT days_applied FROM dataset_shift WHERE id = 1")
    return int(row["days_applied"]) if row else 0


def shift(target: int) -> None:
    current = applied_shift()
    delta = target - current
    if delta == 0:
        print(f"Nothing to do: dates are already shifted by {current} days.")
        return
    cutoff = ORIGINAL_DATASET_END + timedelta(days=current)   # later orders were placed in the app
    add = lambda col: f"{col} = {col} + INTERVAL {delta} DAY"   # NULL stays NULL

    with db.transaction() as cur:
        cur.execute("DROP TEMPORARY TABLE IF EXISTS hist_orders")
        cur.execute("CREATE TEMPORARY TABLE hist_orders (order_id CHAR(32) PRIMARY KEY) "
                    "SELECT order_id FROM orders WHERE order_purchase_timestamp <= %s", (cutoff,))
        cur.execute("SELECT COUNT(*) AS n FROM hist_orders")
        n_orders = cur.fetchone()["n"]
        counts = {}
        for table, cols in DATE_COLUMNS.items():
            sets = ", ".join(f"x.{c} = x.{c} + INTERVAL {delta} DAY" for c in cols)
            counts[table] = cur.execute(f"UPDATE {table} x JOIN hist_orders h ON h.order_id = x.order_id SET {sets}")
        for table, cols in SIMULATED_COLUMNS.items():
            counts[table] = cur.execute(f"UPDATE {table} SET {', '.join(add(c) for c in cols)} WHERE source = 'simulated'")
        counts["refund_events"] = cur.execute(
            f"UPDATE refund_events e JOIN refunds f ON f.refund_id = e.refund_id "
            f"SET e.created_at = e.created_at + INTERVAL {delta} DAY WHERE f.source = 'simulated'")
        cur.execute("REPLACE INTO dataset_shift (id, days_applied, note, updated_at) VALUES (1, %s, %s, NOW())",
                    (target, f"historical Olist dates moved by {target} days (shift_dates.py)"))
        cur.execute("DROP TEMPORARY TABLE hist_orders")

    print(f"Moved historical dates by {delta:+} days (now {target} days from the original): "
          f"{n_orders:,} orders; rows updated: " + ", ".join(f"{t} {n:,}" for t, n in counts.items()))
    r = db.query_one("SELECT MIN(order_purchase_timestamp) AS first, MAX(order_purchase_timestamp) AS last "
                     "FROM orders WHERE order_purchase_timestamp <= %s", (ORIGINAL_DATASET_END + timedelta(days=target),))
    print(f"Historical orders now run from {r['first']} to {r['last']}.")


if __name__ == "__main__":
    shift(0 if "--undo" in sys.argv else DATE_SHIFT_DAYS)

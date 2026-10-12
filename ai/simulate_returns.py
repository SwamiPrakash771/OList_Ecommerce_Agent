"""Generate simulated returns and refunds for the customer-support demo.

    python -m ai.simulate_returns

Olist has no returns or refunds, so this creates them for about 3% of delivered orders,
labelled source = 'simulated'. Re-running replaces only simulated rows (fixed seed, so
the result is the same every time). Rows created in the app (source = 'app') are kept.

Timelines are relative to each order's delivery date and the dataset's last day
(ai.clock.DATASET_NOW), so statuses are consistent with the historical data:
  request (1-25 days after delivery) -> approved/rejected (1-3 days)
  -> item received (3-10 days) -> refund pending -> processing (0-2 days) -> completed
Refund durations depend on the payment method; ~2% of refunds fail and ~7% stall. Stalls
in the dataset's last 40 days are still stuck in processing (cases for the refund SLA
monitor and the support agent); older ones were eventually paid 15-30 days late.

The five demo customers on the sign-in page each get one fixed scenario (DEMO_CASES).

This works on the ORIGINAL 2016-2018 timeline, so run it before shift_dates.py (which then
moves these rows forward with the rest of the data). It refuses to run on shifted data.
"""

import json
import random
import sys
import uuid
from datetime import datetime, timedelta

import db
from ai.clock import ORIGINAL_DATASET_END as DATASET_NOW   # the original timeline's last day

SEED = 20181017
RETURN_RATE = 0.03
STALL_RATE = 0.07
FAIL_RATE = 0.02
STALL_WINDOW_DAYS = 40  # only refunds started this close to the dataset's end are still stuck

# Days from processing start to money back, by method (min, max).
REFUND_DAYS = {"voucher": (0, 1), "debit_card": (2, 5), "boleto": (3, 7), "credit_card": (5, 12)}

REASONS_BY_SCORE = {
    1: (["damaged", "not_as_described", "wrong_item", "late_delivery"], [35, 35, 20, 10]),
    2: (["not_as_described", "damaged", "wrong_item", "late_delivery"], [40, 25, 20, 15]),
    3: (["not_as_described", "changed_mind", "wrong_item"], [40, 40, 20]),
    4: (["changed_mind", "not_as_described"], [80, 20]),
    5: (["changed_mind"], [100]),
}

# One fixed, explainable scenario per demo customer (see the sign-in page).
DEMO_CASES = {
    # The headline case: item returned and received, refund stuck in processing > 7 days.
    "8d50f5eadf50201ccdcedfb9e2ac8455": {"order_id": "a1a6eadae352582010f449d8527b55f3", "reason": "damaged",
                                         "outcome": "stalled", "requested": "2018-09-02", "received": "2018-09-12"},
    "3e43e6105506432c953e165fb2acf44c": {"order_id": "ed92e8596b1e39671065510a4f5b0920", "reason": "not_as_described",
                                         "outcome": "completed", "requested": "2018-03-20", "received": "2018-03-28"},
    "ca77025e7201e3b30c44b472ff346268": {"order_id": "8dacd1da7c4dc40d5c59e7ddd93b3ac8", "reason": "changed_mind",
                                         "outcome": "rejected", "requested": "2018-07-20", "received": None},
    "1b6c7548a2a1f9037c1fd3ddfed95f33": {"order_id": "43f08913407cac4e74a898d968e58c1a", "reason": "wrong_item",
                                         "outcome": "completed", "requested": "2018-02-23", "received": "2018-03-02"},
    "6469f99c1f9dfae7733b25662e7f1782": {"order_id": "ccb1046449f1a494de800567e7cb5979", "reason": "damaged",
                                         "outcome": "failed", "requested": "2018-07-03", "received": "2018-07-11"},
}


def new_id() -> str:
    return uuid.uuid4().hex


def at(day: datetime, rng: random.Random) -> datetime:
    """A plausible business-hours time on `day`."""
    return day.replace(hour=rng.randint(8, 19), minute=rng.randint(0, 59), second=rng.randint(0, 59), microsecond=0)


def load_candidates() -> list[dict]:
    """Delivered Olist orders with their items, main payment method and review score."""
    orders = db.query_df(
        """
        SELECT o.order_id, c.customer_unique_id, o.order_delivered_customer_date AS delivered_at,
               (SELECT r.review_score FROM reviews r WHERE r.order_id = o.order_id
                ORDER BY r.review_answer_timestamp DESC LIMIT 1) AS score,
               (SELECT p.payment_type FROM payments p WHERE p.order_id = o.order_id
                ORDER BY p.payment_value DESC LIMIT 1) AS payment_type
        FROM orders o JOIN customers c USING (customer_id)
        WHERE o.order_status = 'delivered'
          AND o.order_delivered_customer_date IS NOT NULL
          AND o.order_delivered_customer_date <= %s
        """,
        (DATASET_NOW,),
    )
    items = db.query_df("SELECT order_id, order_item_id, product_id, price, freight_value FROM order_items")
    by_order: dict[str, list[dict]] = {}
    for r in items.itertuples(index=False):
        by_order.setdefault(r.order_id, []).append(
            {"order_item_id": int(r.order_item_id), "product_id": r.product_id,
             "price": float(r.price), "freight": float(r.freight_value)}
        )
    out = []
    for r in orders.itertuples(index=False):
        if r.order_id in by_order:
            out.append({
                "order_id": r.order_id, "customer": r.customer_unique_id, "delivered_at": r.delivered_at.to_pydatetime(),
                "score": int(r.score) if r.score == r.score and r.score is not None else 3,
                "method": r.payment_type if r.payment_type in REFUND_DAYS else "credit_card",
                "items": by_order[r.order_id],
            })
    return out


def pick_orders(cands: list[dict], rng: random.Random) -> list[dict]:
    """~3% of orders, weighted so low-rated orders are far more likely to be returned."""
    weight = {1: 8.0, 2: 5.0, 3: 2.0, 4: 0.6, 5: 0.3}
    mean_w = sum(weight[c["score"]] for c in cands) / len(cands)
    demo_orders = {case["order_id"] for case in DEMO_CASES.values()}
    return [c for c in cands
            if c["order_id"] not in demo_orders and rng.random() < RETURN_RATE * weight[c["score"]] / mean_w]


def build_case(c: dict, rng: random.Random, forced: dict | None = None) -> tuple[dict, dict | None, list[dict]]:
    """Return (return_row, refund_row or None, refund_events) for one order."""
    reasons, w = REASONS_BY_SCORE[c["score"]]
    reason = forced["reason"] if forced else rng.choices(reasons, w)[0]

    # Return one item usually, sometimes the whole order.
    lines = c["items"] if (len(c["items"]) == 1 or rng.random() < 0.3) else [rng.choice(c["items"])]
    amount = round(sum(l["price"] + l["freight"] for l in lines), 2)

    if forced:
        requested = at(datetime.fromisoformat(forced["requested"]), rng)
    else:
        requested = at(c["delivered_at"] + timedelta(days=rng.randint(1, 25)), rng)
    ret = {
        "return_id": new_id(), "order_id": c["order_id"], "customer_unique_id": c["customer"],
        "items": json.dumps([{"order_item_id": l["order_item_id"], "product_id": l["product_id"]} for l in lines]),
        "reason": reason, "status": "requested", "requested_at": requested,
        "decided_at": None, "received_at": None, "decision_note": None, "source": "simulated",
    }
    if requested > DATASET_NOW:
        return None, None, []  # would be in the dataset's future

    # Decision.
    decided = requested + timedelta(days=rng.randint(1, 3), hours=rng.randint(0, 6))
    rejected = (forced["outcome"] == "rejected") if forced else (
        rng.random() < (0.25 if reason == "changed_mind" else 0.05))
    if decided > DATASET_NOW:
        return ret, None, []
    ret["decided_at"] = decided
    if rejected:
        ret["status"] = "rejected"
        ret["decision_note"] = ("Return window for change of mind is 7 days after delivery"
                                if reason == "changed_mind" else "Item did not match the reported issue on inspection")
        return ret, None, []
    ret["status"] = "approved"

    # Item arrives back.
    received = (at(datetime.fromisoformat(forced["received"]), rng) if forced and forced.get("received")
                else decided + timedelta(days=rng.randint(3, 10), hours=rng.randint(0, 8)))
    if received > DATASET_NOW:
        return ret, None, []
    ret["status"] = "received"
    ret["received_at"] = received

    # Refund.
    refund = {
        "refund_id": new_id(), "return_id": ret["return_id"], "order_id": c["order_id"], "amount": amount,
        "payment_method": c["method"], "status": "pending", "created_at": received + timedelta(hours=rng.randint(1, 6)),
        "processing_started_at": None, "completed_at": None, "failure_reason": None, "source": "simulated",
    }
    events = [{"status": "pending", "note": "Return received; refund created", "created_at": refund["created_at"]}]
    started = refund["created_at"] + timedelta(days=rng.randint(0, 2), hours=rng.randint(1, 8))
    if started > DATASET_NOW:
        return ret, refund, events
    refund["status"] = "processing"
    refund["processing_started_at"] = started
    events.append({"status": "processing", "note": f"Refund sent to {c['method'].replace('_', ' ')} provider",
                   "created_at": started})

    outcome = forced["outcome"] if forced else (
        "failed" if rng.random() < FAIL_RATE else "stalled" if rng.random() < STALL_RATE else "completed")
    if outcome == "stalled" and not forced and started < DATASET_NOW - timedelta(days=STALL_WINDOW_DAYS):
        # A refund stuck for months isn't realistic; older stalls were eventually paid, late.
        outcome = "late"
    if outcome == "late":
        done = started + timedelta(days=rng.randint(15, 30), hours=rng.randint(1, 10))
        refund["status"] = "completed"
        refund["completed_at"] = done
        events.append({"status": "processing", "note": "Delayed at the payment provider; escalated",
                       "created_at": started + timedelta(days=8)})
        events.append({"status": "completed", "note": "Refund confirmed by the payment provider", "created_at": done})
        return ret, refund, events
    if outcome == "failed":
        failed_at = started + timedelta(days=rng.randint(1, 4))
        if failed_at <= DATASET_NOW:
            refund["status"] = "failed"
            refund["failure_reason"] = rng.choice([
                "Card account closed; refund rejected by the issuing bank",
                "Bank details for the refund transfer were invalid",
            ]) if not forced else "Card account closed; refund rejected by the issuing bank"
            events.append({"status": "failed", "note": refund["failure_reason"], "created_at": failed_at})
    elif outcome == "completed":
        lo, hi = REFUND_DAYS[c["method"]]
        done = started + timedelta(days=rng.randint(lo, hi), hours=rng.randint(1, 10))
        if done <= DATASET_NOW:
            refund["status"] = "completed"
            refund["completed_at"] = done
            events.append({"status": "completed", "note": "Refund confirmed by the payment provider", "created_at": done})
    # "stalled": stays processing with no further events.
    return ret, refund, events


def main() -> None:
    shifted = db.query_one("SELECT COUNT(*) AS n FROM information_schema.tables WHERE table_schema = DATABASE() "
                           "AND table_name = 'dataset_shift'")["n"] and \
        (db.query_one("SELECT days_applied FROM dataset_shift WHERE id = 1") or {}).get("days_applied")
    if shifted:
        sys.exit(f"Dates are shifted by {shifted} days. Run `python shift_dates.py --undo` first, then this, "
                 "then `python shift_dates.py` again.")
    rng = random.Random(SEED)
    cands = load_candidates()
    by_order = {c["order_id"]: c for c in cands}

    cases = []
    for customer, case in DEMO_CASES.items():
        c = by_order.get(case["order_id"])
        if not c or c["customer"] != customer:
            raise SystemExit(f"demo order {case['order_id']} not found for customer {customer}")
        cases.append(build_case(c, rng, forced=case))
    for c in pick_orders(cands, rng):
        cases.append(build_case(c, rng))
    cases = [x for x in cases if x[0] is not None]

    ret_cols = ["return_id", "order_id", "customer_unique_id", "items", "reason", "status", "requested_at",
                "decided_at", "received_at", "decision_note", "source"]
    ref_cols = ["refund_id", "return_id", "order_id", "amount", "payment_method", "status", "created_at",
                "processing_started_at", "completed_at", "failure_reason", "source"]
    with db.transaction() as cur:
        cur.execute("DELETE e FROM refund_events e JOIN refunds r USING (refund_id) WHERE r.source = 'simulated'")
        cur.execute("DELETE FROM refunds WHERE source = 'simulated'")
        cur.execute("DELETE FROM returns WHERE source = 'simulated'")
        cur.executemany(
            f"INSERT INTO returns ({', '.join(ret_cols)}) VALUES ({', '.join(['%s'] * len(ret_cols))})",
            [tuple(r[k] for k in ret_cols) for r, _, _ in cases],
        )
        refunds = [(ref, ev) for _, ref, ev in cases if ref]
        cur.executemany(
            f"INSERT INTO refunds ({', '.join(ref_cols)}) VALUES ({', '.join(['%s'] * len(ref_cols))})",
            [tuple(ref[k] for k in ref_cols) for ref, _ in refunds],
        )
        cur.executemany(
            "INSERT INTO refund_events (refund_id, status, note, created_at) VALUES (%s, %s, %s, %s)",
            [(ref["refund_id"], e["status"], e["note"], e["created_at"]) for ref, ev in refunds for e in ev],
        )

    summary = db.query_df(
        """
        SELECT 'returns' AS t, status, COUNT(*) AS n FROM returns WHERE source = 'simulated' GROUP BY status
        UNION ALL
        SELECT 'refunds', status, COUNT(*) FROM refunds WHERE source = 'simulated' GROUP BY status
        """
    )
    stuck = db.query_one(
        "SELECT COUNT(*) AS n FROM refunds WHERE status = 'processing' AND processing_started_at < %s",
        (DATASET_NOW - timedelta(days=7),),
    )["n"]
    print(f"simulated {len(cases):,} returns from {len(cands):,} delivered orders")
    print(summary.to_string(index=False))
    print(f"refunds stuck in processing > 7 days (as of {DATASET_NOW.date()}): {stuck}")


if __name__ == "__main__":
    main()

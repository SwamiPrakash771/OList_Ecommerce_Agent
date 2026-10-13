"""operations_report: refunds needing attention, late open orders and the AI assistants' usage. Lists are short and ranked (top_n); totals are given separately.
No chat content, ticket text or refund failure reasons are read (decision 2)."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Literal

from pydantic import Field

import db
from ai import clock
from ai.admin.security import Principal, admin_tool
from ai.admin.tools.common import PERIODS, as_dt, open_since, period_bounds, read_only, value
from ai.policies import REFUND_SLA_DAYS


def refund_issues(principal: Principal, kind: Literal["stuck", "failed", "processing"] = "stuck",
                  top_n: int = Field(20, ge=1, le=100)) -> dict:
    """Refunds needing attention. stuck = processing longer than its payment method's expected time
    (refund SLA: voucher 2 days, debit card 5, boleto 7, credit card 12, counted from being sent to the
    payment provider); failed = the payment provider refused it; processing = all in processing.
    Returns the count, total amount and count by payment method, and the refunds (oldest first) with
    days in processing against the expected time. Failure reasons are not available."""
    status = "failed" if kind == "failed" else "processing"
    rows = db.query_df("SELECT refund_id, order_id, payment_method, amount, created_at, processing_started_at "
                       "FROM refunds WHERE status = %s", (status,)).to_dict("records")
    items = []
    for r in rows:
        expected = REFUND_SLA_DAYS.get(r["payment_method"], 7)
        started = as_dt(r["processing_started_at"])
        age = clock.days_since(started) if started else None
        if kind == "stuck" and (age is None or age <= expected):
            continue
        items.append({"refund_id": r["refund_id"], "order_id": r["order_id"], "created": value(r["created_at"]),
                      **({"days_in_processing": age, "expected_within_days": expected,
                          "days_over": age - expected if age is not None else None} if status == "processing" else {}),
                      "_method": r["payment_method"], "_amount": float(r["amount"] or 0)})
    items.sort(key=lambda i: -(i["days_in_processing"] or 0) if status == "processing" else i["created"] or "")
    by_method: dict[str, int] = {}
    for i in items:
        by_method[i["_method"]] = by_method.get(i["_method"], 0) + 1
    total = round(sum(i.pop("_amount") for i in items), 2)
    for i in items:
        i.pop("_method")
    out = {"kind": kind, "count": len(items), "total_amount": total, "count_by_payment_method": by_method,
           "refunds": items[:top_n], "refunds_shown": f"{min(len(items), top_n)} of {len(items)}",
           "total_amount_of": f"all {len(items)} {kind} refunds", "ages_measured_against": "the data's last day (2026-10-07) for historical "
           "records, today for records created in the app"}
    if kind == "failed":
        out["note"] = "Failure reasons are not available to the assistant; open the refund in the admin screens."
    return out


def late_open_orders(principal: Principal, top_n: int = Field(20, ge=1, le=100)) -> dict:
    """Open orders (not delivered, canceled or unavailable) bought in the last 60 days, with those past
    their estimated delivery date listed first (most days late first) and their sellers. Older orders
    still marked shipped/invoiced/processing never completed in the records: they are counted
    separately as old leftovers and are not work to do."""
    since = open_since()
    open_rows = read_only(
        "SELECT o.order_id, o.order_status AS status, o.order_purchase_timestamp AS purchased, "
        "o.order_estimated_delivery_date AS estimated, GROUP_CONCAT(DISTINCT oi.seller_id) AS sellers "
        "FROM orders o LEFT JOIN order_items oi ON oi.order_id = o.order_id "
        "WHERE o.order_status NOT IN ('delivered', 'canceled', 'unavailable') AND o.order_purchase_timestamp >= %s "
        "GROUP BY o.order_id, o.order_status, o.order_purchase_timestamp, o.order_estimated_delivery_date",
        (since,), max_rows=5000)
    today = date.today()
    for r in open_rows:
        est = date.fromisoformat(r["estimated"][:10]) if r["estimated"] else None
        r["days_past_estimate"] = (today - est).days if est and est < today else 0
        r["sellers"] = (r["sellers"] or "").split(",") if r["sellers"] else []
    open_rows.sort(key=lambda r: (-r["days_past_estimate"], r["purchased"]))
    leftovers = db.query_one(
        "SELECT COUNT(*) AS n FROM orders WHERE order_status NOT IN ('delivered', 'canceled', 'unavailable') "
        "AND order_purchase_timestamp < %s", (since,))["n"]
    return {"open_since": value(since), "open_orders": len(open_rows),
            "late_open_orders": sum(1 for r in open_rows if r["days_past_estimate"] > 0),
            "orders": open_rows[:top_n], "orders_shown": f"{min(len(open_rows), top_n)} of {len(open_rows)}",
            "days_past_estimate_measured_against": date.today().isoformat(), "old_leftovers": int(leftovers),
            "note": "old_leftovers were bought before open_since and never completed in the records; not work to do."}


def ai_assistant_usage(principal: Principal, period: str = "last_30_days",
                       group_by: Literal["role", "model", "day"] = "role") -> dict:
    """The AI assistants' own usage and quality: replies (and how many were answered locally without an
    LLM), tokens, cost, thumbs up/down, and tool errors, by role, model or day. Never chat content."""
    start, end = period_bounds(period, date.today())
    params = (start or date(2000, 1, 1), end + timedelta(days=1))
    key = {"role": "c.owner_role", "model": "m.model", "day": "DATE(m.created_at)"}[group_by]
    ukey = {"role": "role", "model": "model", "day": "DATE(created_at)"}[group_by]
    # grouped under the alias grp: an alias named "role" would silently group by ai_messages.role
    replies = {str(r["grp"]): r for r in read_only(f"""
        SELECT {key} AS grp, COUNT(*) AS replies, SUM(m.model = 'local:fast-path') AS local_replies,
               COALESCE(SUM(m.input_tokens), 0) AS input_tokens, COALESCE(SUM(m.output_tokens), 0) AS output_tokens
        FROM ai_messages m JOIN ai_conversations c ON c.conversation_id = m.conversation_id
        WHERE m.role = 'assistant' AND m.created_at >= %s AND m.created_at < %s
        GROUP BY grp""", params, max_rows=1000)}
    cost = {str(r["grp"]): round(float(r["cost"]), 4) for r in db.query_df(   # AI costs need 4 decimals
        f"SELECT {ukey} AS grp, SUM(cost) AS cost FROM ai_usage "
        f"WHERE created_at >= %s AND created_at < %s GROUP BY grp", params).to_dict("records")}
    feedback = {str(r["grp"]): r for r in read_only(f"""
        SELECT {key} AS grp, SUM(f.rating = 'up') AS thumbs_up, SUM(f.rating = 'down') AS thumbs_down
        FROM ai_feedback f JOIN ai_messages m ON m.message_id = f.message_id
             JOIN ai_conversations c ON c.conversation_id = m.conversation_id
        WHERE f.created_at >= %s AND f.created_at < %s GROUP BY grp""", params, max_rows=1000)}
    rows = []
    for k in sorted(set(replies) | set(cost)):   # usage can exist without a stored reply (e.g. jobs)
        r = replies.get(k, {})
        rows.append({group_by: k, "replies": int(r.get("replies") or 0), "local_replies": int(r.get("local_replies") or 0),
                     "input_tokens": int(r.get("input_tokens") or 0), "output_tokens": int(r.get("output_tokens") or 0),
                     "cost_usd": cost.get(k, 0.0), "thumbs_up": int(feedback.get(k, {}).get("thumbs_up") or 0),
                     "thumbs_down": int(feedback.get(k, {}).get("thumbs_down") or 0)})
    errors = read_only("SELECT tool_name, status, COUNT(*) AS calls FROM ai_tool_calls WHERE status <> 'ok' "
                       "AND created_at >= %s AND created_at < %s GROUP BY tool_name, status ORDER BY calls DESC LIMIT 10",
                       params)
    return {"period": {"start": params[0].isoformat() if start else "first record", "end": end.isoformat()},
            "group_by": group_by, "rows": rows, "total_cost_usd": round(sum(cost.values()), 4),
            "tool_problems": errors, "note": "Counts, costs and ratings only; chat content is not available."}


ai_assistant_usage.__doc__ += f" Periods: {', '.join(PERIODS)}, a year or a month (2026-07)."

Report = Literal["stuck_refunds", "failed_refunds", "processing_refunds", "late_open_orders", "ai_usage"]


def operations_report(principal: Principal, report: Report, top_n: int = Field(20, ge=1, le=100),
                      period: str = "last_30_days", group_by: Literal["role", "model", "day"] = "role") -> dict:
    """Operations reports, each with its own rules applied:
    - "stuck_refunds": refunds processing longer than their payment method's expected time (refund SLA: voucher
      2 days, debit card 5, boleto 7, credit card 12, from being sent to the payment provider), oldest first;
      "failed_refunds": refused by the payment provider; "processing_refunds": all in processing. Each gives
      the count, total amount and count by payment method (failure reasons aren't available).
    - "late_open_orders": open orders (not delivered, canceled or unavailable) bought in the last 60 days, the
      most days past their estimated delivery first, with their sellers; older ones are counted apart as old
      leftovers that never completed in the records (not work to do).
    - "ai_usage": the AI assistants' replies, local answers, tokens, cost, thumbs up/down and tool problems
      by role, model or day (group_by) for a period. Never chat content.
    top_n: how many rows to list. For other questions (sellers at risk, tickets, ...) use the metrics or a
    custom analysis."""
    if report.endswith("_refunds"):
        return refund_issues(principal, kind=report.removesuffix("_refunds"), top_n=top_n)
    if report == "late_open_orders":
        return late_open_orders(principal, top_n=top_n)
    return ai_assistant_usage(principal, period=period, group_by=group_by)


admin_tool(reads=["refunds", "orders", "order_items", "ai_messages", "ai_conversations", "ai_usage", "ai_feedback",
                  "ai_tool_calls"], aggregates=["refunds.amount", "refunds.payment_method"])(operations_report)

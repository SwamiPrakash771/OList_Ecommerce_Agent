"""seller_metric: answers from defined metrics, with SQL compiled by the server (the semantic layer).

The model only chooses names and options (metric, group_by, period, top_n, ...). The SQL is built
here from the definitions in ai/metadata.yaml (`seller_semantic`), over two fact tables with a
known grain, so the usual mistakes can't happen: joins that repeat rows, date-time vs date
comparisons, per-unit averages for per-order questions, forgotten exclusions. Every number comes
back with the definition used, the period bounds, the overall total, and (on request) the change
against the previous period and each group's share of the total.

Model-supplied values never reach the SQL text: names are checked against the definitions, dates
are parsed, numbers are typed. It runs on the seller agent's MySQL user, read-only, time-limited.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Literal

import pymysql
from pydantic import Field

import db
from ai import metadata
from ai.seller.security import InvalidArguments, Principal, ToolError, seller_tool
from ai.seller.tools.common import state_name
from ai.seller.tools.query import WITH_SQL

# Readable values for yes/no groupings, so answers never show internal 0/1 codes.
FLAG_LABELS = {"delivered_late": {1: "delivered late", 0: "delivered on time"},
               "handed_over_late": {1: "handed to the carrier late", 0: "handed to the carrier on time"}}

TIME_LIMIT_MS = 5000
MAX_ROWS = 500

FACTS = """,
order_facts AS (
  SELECT o.order_id, o.order_status, o.order_purchase_timestamp, o.buyer_state, o.buyer_city,
         o.delivered_late, o.handed_over_late, o.days_to_carrier,
         t.item_count, t.items_total, t.shipping_total, t.order_total,
         (SELECT AVG(r.review_score) FROM my_reviews r WHERE r.order_id = o.order_id) AS review_score
  FROM my_orders o JOIN my_order_totals t ON t.order_id = o.order_id),
item_facts AS (
  SELECT s.order_id, s.product_id, s.price, s.freight_value, p.display_name AS product, p.category,
         f.order_status, f.order_purchase_timestamp, f.buyer_state, f.delivered_late, f.handed_over_late,
         f.review_score
  FROM my_sales s JOIN order_facts f ON f.order_id = s.order_id JOIN products p ON p.product_id = s.product_id)
"""

PERIODS = ("all", "this_year", "last_year", "this_month", "last_month", "last_30_days", "last_90_days",
           "last_12_months")


def semantic() -> dict:
    return metadata.load()["seller_semantic"]


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _add_months(d: date, n: int) -> date:
    y, m = divmod(d.year * 12 + d.month - 1 + n, 12)
    return date(y, m + 1, 1)


def period_bounds(period: str, today: date) -> tuple[date | None, date]:
    """(start, end inclusive) for a named period, a year ('2025') or a month ('2026-07')."""
    if period == "all":
        return None, today
    if period == "this_year":
        return date(today.year, 1, 1), today
    if period == "last_year":
        return date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)
    if period == "this_month":
        return _month_start(today), today
    if period == "last_month":
        start = _add_months(_month_start(today), -1)
        return start, _month_start(today) - timedelta(days=1)
    if period == "last_30_days":
        return today - timedelta(days=29), today
    if period == "last_90_days":
        return today - timedelta(days=89), today
    if period == "last_12_months":
        return _add_months(_month_start(today), -11), today
    if len(period) == 4 and period.isdigit():
        return date(int(period), 1, 1), min(date(int(period), 12, 31), today)
    if len(period) == 7 and period[4] == "-" and period.replace("-", "").isdigit():
        start = date(int(period[:4]), int(period[5:]), 1)
        return start, min(_add_months(start, 1) - timedelta(days=1), today)
    raise InvalidArguments(f"Unknown period '{period}'. Use one of {', '.join(PERIODS)}, a year (2025) or a month (2026-07).")


def _value(v):
    if isinstance(v, Decimal):
        return round(float(v), 2)
    if isinstance(v, float):
        return round(v, 2)
    return v


def compile_query(metric: str, group_by: list[str], start: date | None, end: date,
                  min_orders: int | None, top_n: int | None, sort: str) -> tuple[str, dict, str]:
    """The SQL for a metric (server-built from the definitions), its parameters and its grain."""
    sem = semantic()
    m = sem["metrics"][metric]
    dims = [sem["dimensions"][d] for d in group_by]
    grain = "item" if any(d.get("grain") == "item" for d in dims) else "order"
    if grain not in m:
        alt = "product or category" if grain == "item" else "order"
        raise InvalidArguments(f"'{metric}' can't be broken down by {alt}; choose another metric or grouping.")
    table = "item_facts" if grain == "item" else "order_facts"
    where = [sem["filters"].get(m["where"], m["where"])] if m.get("where") else []
    params: dict = {}
    if start:
        where.append("order_purchase_timestamp >= %(start)s")
        params["start"] = start.isoformat()
    where.append("order_purchase_timestamp < %(end)s")
    params["end"] = (end + timedelta(days=1)).isoformat()
    for name, d in zip(group_by, dims):
        if name in ("delivered_late", "handed_over_late"):
            where.append(f"{d['expr']} IS NOT NULL")
    select = [f"{d['expr']} AS `{name}`" for name, d in zip(group_by, dims)]
    order_count = "COUNT(*)" if grain == "order" else "COUNT(DISTINCT order_id)"
    sql = (f"SELECT {', '.join(select + [m[grain] + ' AS value', order_count + ' AS orders'])} FROM {table}"
           f" WHERE {' AND '.join(where)}")
    if group_by:
        sql += " GROUP BY " + ", ".join(f"`{g}`" for g in group_by)
        if min_orders:
            sql += f" HAVING {order_count} >= %(min_orders)s"
            params["min_orders"] = int(min_orders)
        if top_n:
            sql += f" ORDER BY value {'DESC' if sort == 'desc' else 'ASC'}, `{group_by[0]}` LIMIT %(top_n)s"
            params["top_n"] = int(top_n)
        else:
            sql += " ORDER BY " + ", ".join(f"`{g}`" for g in group_by)
    return WITH_SQL + FACTS.lstrip("\n") + sql, params, grain


def _run(sql: str, params: dict, seller_id: str) -> list[dict]:
    try:
        with db.cursor() as cur:
            cur.execute(f"SET SESSION max_execution_time = {TIME_LIMIT_MS}")
            cur.execute("START TRANSACTION READ ONLY")
            try:
                cur.execute(sql.replace("%Y-%m", "%%Y-%%m"), {"sid": seller_id, **params})
                rows = cur.fetchmany(MAX_ROWS + 1)
            finally:
                cur.execute("ROLLBACK")
                cur.execute("SET SESSION max_execution_time = 0")
    except pymysql.err.MySQLError as exc:
        if exc.args and exc.args[0] == 3024:
            raise ToolError("That breakdown took too long; narrow the period or grouping.") from None
        raise
    if len(rows) > MAX_ROWS:
        raise ToolError(f"More than {MAX_ROWS} groups; use top_n or a coarser grouping.")
    return [{k: _value(v) for k, v in r.items()} for r in rows]


def seller_metric(principal: Principal,
                  metric: str,
                  group_by: list[str] = Field(default_factory=list, max_length=2),
                  period: str = "all",
                  top_n: int | None = Field(None, ge=1, le=50),
                  sort: Literal["desc", "asc"] = "desc",
                  min_orders: int | None = Field(None, ge=1, le=100_000),
                  compare_previous: bool = False,
                  share_of_total: bool = False) -> dict:
    sem = semantic()
    if metric not in sem["metrics"]:
        raise InvalidArguments(f"Unknown metric '{metric}'. Available: {', '.join(sem['metrics'])}.")
    unknown = [g for g in group_by if g not in sem["dimensions"]]
    if unknown or len(set(group_by)) != len(group_by):
        raise InvalidArguments(f"Unknown or repeated grouping {unknown}. Available: {', '.join(sem['dimensions'])}.")
    if compare_previous and group_by not in (["month"], ["year"]):
        raise InvalidArguments("compare_previous needs group_by ['month'] or ['year'].")
    today = date.today()
    start, end = period_bounds(period, today)
    # One extra month/year before the period, so its first group also has a "previous" to compare with.
    query_start = start
    if compare_previous and start:
        query_start = _add_months(start, -1) if group_by == ["month"] else date(start.year - 1, 1, 1)
    sql, params, grain = compile_query(metric, group_by, query_start, end, min_orders,
                                       None if compare_previous else top_n, sort)
    rows = _run(sql, params, principal.seller_id)
    total_sql, total_params, _ = compile_query(metric, [], start, end, None, None, sort)
    total = _run(total_sql, total_params, principal.seller_id)[0]
    if compare_previous:
        for prev, row in zip([None] + rows[:-1], rows):
            if prev is not None and row["value"] is not None and prev["value"] is not None:
                row["change"] = round(row["value"] - prev["value"], 2)
                row["change_pct"] = round(100 * row["change"] / prev["value"], 2) if prev["value"] else None
        key = group_by[0]
        first = (start.strftime("%Y-%m") if group_by == ["month"] else start.year) if start else None
        rows = [r for r in rows if first is None or str(r[key]) >= str(first)]
        if top_n:
            rows = sorted([r for r in rows if "change" in r], key=lambda r: r["change"],
                          reverse=sort == "desc")[:top_n]
    out = {"metric": metric, "definition": sem["metrics"][metric]["label"], "unit": sem["metrics"][metric].get("unit"),
           "period": {"start": start.isoformat() if start else "first order", "end": end.isoformat()},
           "group_by": group_by, "rows": rows, "total": total["value"], "total_orders": total["orders"],
           "today": today.isoformat()}
    for r in rows:
        for g in group_by:
            if g in FLAG_LABELS and r.get(g) in FLAG_LABELS[g]:
                r[g] = FLAG_LABELS[g][r[g]]
        if "buyer_state" in group_by:
            r["buyer_state_name"] = state_name(r.get("buyer_state"))
    if share_of_total and group_by and total["value"]:
        for r in rows:
            r["share_pct"] = round(100 * r["value"] / total["value"], 2) if r["value"] is not None else None
        out["shown_groups_share_pct"] = round(100 * sum(r["value"] or 0 for r in rows) / total["value"], 2)
    if not rows:
        out["note"] = "No orders matched in this period."
    return out


def _doc() -> str:
    sem = semantic()
    metrics = "\n".join(f"- {k}: {v['label']}" for k, v in sem["metrics"].items())
    dims = "\n".join(f"- {k}: {v['label']}" + (" (item level)" if v.get("grain") == "item" else "")
                     for k, v in sem["dimensions"].items())
    return f"""The seller's figures from defined metrics: the server builds the query, so definitions are
always applied correctly. Use this first for any total, count, average, rate or comparison it
covers; use the SQL tool only for questions these metrics can't express.
Metrics:
{metrics}
Groupings (group_by, at most 2):
{dims}
Options: period = {', '.join(PERIODS)}, a year ("2025") or a month ("2026-07");
top_n + sort (desc/asc) for the highest/lowest groups; min_orders to ignore small groups (e.g. states
with at least 20 orders); compare_previous with group_by ["month"] or ["year"] adds change and
change_pct vs the previous month/year (top_n then ranks by change); share_of_total adds each group's
share_pct and shown_groups_share_pct (e.g. the share of the top 10 products).
The result includes the definition, period, total and orders behind each value: quote the definition
and period in the answer. Examples: revenue by month in 2026 with compare_previous; avg_review_score by
delivered_late; late_shipping_rate by buyer_state with min_orders 20, top_n 1; revenue by product top_n 10
with share_of_total."""


seller_metric.__doc__ = _doc()
seller_tool(reads=["order_items", "orders", "customers", "product_stats", "products", "reviews"])(seller_metric)

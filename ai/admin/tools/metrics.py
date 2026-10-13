"""admin_metric: platform figures from defined metrics, with SQL compiled by the server (semantic layer).

The model only chooses names and options (metric, group_by, filters, period, top_n, ...). The SQL is built
here from the definitions in ai/metadata.yaml (`admin_semantic`), over three reporting tables with a known
grain (order, order x seller, unit sold; rebuilt hourly, ai/admin/facts.py), so joins can't repeat rows,
item revenue and GMV can't be confused, and the exclusions are always applied. Each result carries the
definition, period, overall total and the orders behind each value.

General options instead of special cases: filters on any grouping; any period (named, a year, a quarter,
a month, a day or a date range); time series by day, week, month, quarter or year with no gaps; comparisons
of any row or the whole period with the previous period, the same period a year earlier or any period;
running totals.

Masked columns (payment values, the customer's identity, buyer state) are used only inside these
compiled aggregates, groupings and filters; "sensitive" metrics hide groups (and filtered totals) with
fewer than `min_group` orders, so a figure never describes one person. Model-supplied values never reach
the SQL text: names are checked against the definitions and values are bound parameters.
"""

from __future__ import annotations

import functools
from datetime import date, timedelta
from typing import Literal

from pydantic import Field

from ai import metadata
from ai.admin import facts
from ai.admin.security import InvalidArguments, Principal, admin_tool
from ai.admin.tools.common import PERIODS, add_months, period_bounds, read_only, resolve_id, state_name, value

GRAIN_ORDER = ("order", "seller_order", "item")
TIME_UNITS = ("day", "week", "month", "quarter", "year")
CYCLIC = ("weekday", "hour")   # groupings with a natural order: charts keep it unless asked to rank
MAX_SERIES_POINTS = 400   # more periods than this in one series: choose a shorter period or a coarser unit
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
FLAG_LABELS = {"delivered_late": {1: "delivered late", 0: "delivered on time"},
               "weekday": dict(enumerate(WEEKDAYS))}

TABLE = {"order": "admin_order_facts", "seller_order": "admin_seller_order_facts", "item": "admin_item_facts"}


def semantic() -> dict:
    return metadata.load()["admin_semantic"]


def choose_grain(metric: str, dims: list[str]) -> str:
    """The finest-grained-enough table for the metric and every grouping or filter used."""
    sem = semantic()
    m = sem["metrics"][metric]
    allowed = [g for g in GRAIN_ORDER if g in m]
    for d in dims:
        allowed = [g for g in allowed if g in sem["dimensions"][d]["grains"]]
    if not allowed:
        raise InvalidArguments(f"'{metric}' can't be broken down or filtered by {', '.join(dims)}; "
                               "choose another metric or grouping.")
    return allowed[0]


def _esc(expr: str) -> str:
    """Dimension expressions are fixed SQL text; their % (DATE_FORMAT) must not be read as parameters."""
    return expr.replace("%", "%%")


def compile_query(metric: str, group_by: list[str], start: date | None, end: date,
                  min_orders: int | None, top_n: int | None, sort: str,
                  only: dict[str, list] | None = None) -> tuple[str, dict]:
    """The SQL for a metric (server-built from the definitions) and its parameters. `only` keeps groupings
    to given values (filters, chart series), always as bound parameters."""
    sem = semantic()
    m = sem["metrics"][metric]
    only = only or {}
    grain = choose_grain(metric, group_by + [k for k in only if k not in group_by])
    dims = [sem["dimensions"][d] for d in group_by]
    where = [sem["filters"].get(m["where"], m["where"])] if m.get("where") else []
    params: dict = {"end": (end + timedelta(days=1)).isoformat()}
    where.append("order_purchase_timestamp < %(end)s")
    if start:
        where.append("order_purchase_timestamp >= %(start)s")
        params["start"] = start.isoformat()
    for name in group_by:
        if name == "delivered_late":
            where.append("delivered_late IS NOT NULL")
    for n, (name, values) in enumerate(only.items()):
        keys = [f"only_{n}_{i}" for i in range(len(values))]
        where.append(f"{_esc(sem['dimensions'][name]['expr'])} IN ({', '.join(f'%({k})s' for k in keys)})")
        params.update(zip(keys, values))
    select = [f"{_esc(d['expr'])} AS `{name}`" for name, d in zip(group_by, dims)]
    order_count = "COUNT(*)" if grain == "order" else "COUNT(DISTINCT order_id)"
    sql = (f"SELECT {', '.join(select + [m[grain] + ' AS value', order_count + ' AS orders'])} FROM {TABLE[grain]}"
           f" WHERE {' AND '.join(where)}")
    if group_by:
        sql += " GROUP BY " + ", ".join(f"`{g}`" for g in group_by)
        if min_orders:
            sql += f" HAVING {order_count} >= %(min_orders)s"
            params["min_orders"] = int(min_orders)
        if top_n:
            sql += f" ORDER BY value IS NULL, value {'DESC' if sort == 'desc' else 'ASC'}, `{group_by[0]}` LIMIT %(top_n)s"
            params["top_n"] = int(top_n)
        else:
            sql += " ORDER BY " + ", ".join(f"`{g}`" for g in group_by)
    return sql, params


# ---- filters ----------------------------------------------------------------------------------------

def normalize_filters(filters: dict[str, list] | None) -> dict[str, list]:
    """Check and normalise filters ({grouping: [values]}): known groupings only; state codes upper-case;
    sellers by full ID or a unique 8+ character prefix; weekdays by name; delivered_late by words."""
    sem = semantic()
    out: dict[str, list] = {}
    for name, values in (filters or {}).items():
        if name not in sem["dimensions"]:
            raise InvalidArguments(f"Unknown filter '{name}'. Filters use the groupings: {', '.join(sem['dimensions'])}.")
        values = values if isinstance(values, list) else [values]
        if not 1 <= len(values) <= 50:
            raise InvalidArguments(f"Filter '{name}' needs 1 to 50 values.")
        clean = []
        for v in values:
            if name in ("buyer_state", "seller_state"):
                v = str(v).strip().upper()
            elif name == "seller":
                v = resolve_id("admin_seller_order_facts", "seller_id", str(v).removeprefix("seller ").strip(), "seller")
            elif name == "weekday":
                v = WEEKDAYS.index(str(v).strip().capitalize()) if str(v).strip().capitalize() in WEEKDAYS else int(v)
            elif name == "delivered_late":
                v = 1 if str(v).strip().lower() in ("1", "true", "late", "delivered late") else 0
            elif name in ("year", "hour"):
                v = int(v)
            else:
                v = str(v).strip()
            clean.append(v)
        out[name] = clean
    return out


def describe_filters(filters: dict[str, list]) -> str | None:
    if not filters:
        return None
    sem = semantic()
    parts = []
    for name, values in filters.items():
        shown = [FLAG_LABELS[name].get(v, v) if name in FLAG_LABELS else v for v in values]
        parts.append(f"{sem['dimensions'][name]['label']} = {', '.join(map(str, shown))}")
    return "; ".join(parts)


# ---- time units: day, week, month, quarter, year ---------------------------------------------------

def unit_label(key: str, d: date):
    if key == "year":
        return d.year
    if key == "quarter":
        return f"{d.year}-Q{(d.month - 1) // 3 + 1}"
    if key == "month":
        return d.strftime("%Y-%m")
    if key == "week":
        iso_year, iso_week, _ = d.isocalendar()
        return f"{iso_year}-W{iso_week:02d}"
    return d.isoformat()


def unit_start(key: str, label) -> date:
    label = str(label)
    if key == "year":
        return date(int(label), 1, 1)
    if key == "quarter":
        y, q = label.split("-Q")
        return date(int(y), 3 * (int(q) - 1) + 1, 1)
    if key == "month":
        return date.fromisoformat(f"{label}-01")
    if key == "week":
        y, w = label.split("-W")
        return date.fromisocalendar(int(y), int(w), 1)
    return date.fromisoformat(label[:10])


def unit_add(key: str, d: date, n: int) -> date:
    if key in ("year", "quarter", "month"):
        return add_months(d.replace(day=1), n * {"year": 12, "quarter": 3, "month": 1}[key])
    return d + timedelta(days=n * (7 if key == "week" else 1))


def unit_period(key: str, label) -> tuple[date, date]:
    start = unit_start(key, label)
    return start, unit_add(key, start, 1) - timedelta(days=1)


def unit_floor(key: str, d: date) -> date:
    return unit_start(key, unit_label(key, d))


def baseline_label(key: str, label, compare_to: str):
    """The previous unit, or the same unit a year earlier (ISO week 53 falls back to week 52)."""
    start = unit_start(key, label)
    if compare_to != "same_period_last_year" or key == "year":
        return unit_label(key, unit_add(key, start, -1))
    if key == "week":
        y, w = str(label).split("-W")
        try:
            return unit_label(key, date.fromisocalendar(int(y) - 1, int(w), 1))
        except ValueError:
            return unit_label(key, date.fromisocalendar(int(y) - 1, 52, 1))
    return unit_label(key, shift_years(start, -1))


Comparison = Literal["none", "previous_period", "same_period_last_year"]
RankBy = Literal["value", "change", "change_pct"]


def shift_years(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year + years)
    except ValueError:   # 29 February
        return d.replace(year=d.year + years, day=28)


def baseline_window(start: date | None, end: date, compare_to: str, compare_period: str | None,
                    today: date) -> tuple[date, date, str]:
    """The window a whole period is compared with, and a readable label for it."""
    if compare_period:
        b_start, b_end = period_bounds(compare_period, today)
        if b_start is None:
            raise InvalidArguments("compare_period must be a bounded period (a year, a quarter, a month, a day or a range).")
        return b_start, b_end, compare_period
    if start is None:
        raise InvalidArguments("The period 'all' has nothing before it to compare with; choose a period.")
    if compare_to == "same_period_last_year":
        b_start, b_end = shift_years(start, -1), shift_years(end, -1)
    elif start.day == 1 and ((end + timedelta(days=1)).day == 1 or end == today):
        # calendar months/quarters/years: the same number of months just before, to the same day if not over yet
        months = (end.year - start.year) * 12 + end.month - start.month + 1
        b_start = add_months(start, -months)
        full_end = add_months(b_start, months) - timedelta(days=1)
        b_end = full_end if (end + timedelta(days=1)).day == 1 else \
            min(add_months(b_start, months - 1) + timedelta(days=end.day - 1), full_end)
    else:
        b_end = start - timedelta(days=1)
        b_start = b_end - (end - start)
    return b_start, b_end, f"{b_start.isoformat()} to {b_end.isoformat()}"


def _attach(row: dict, prev: dict | None, label: str, sensitive: bool, min_group: int) -> None:
    """The baseline's value, its label and the change, on the row itself (so it can't be mislabelled)."""
    prev_value = None if prev is None else prev.get("value")
    if sensitive and prev is not None and (prev.get("orders") or 0) < min_group:
        prev_value = None
    row["compared_with"] = label
    row["previous_value"] = prev_value
    if row.get("value") is not None and prev_value is not None:
        row["change"] = round(row["value"] - prev_value, 2)
        row["change_pct"] = round(100 * row["change"] / prev_value, 2) if prev_value else None
    else:
        row["change"] = row["change_pct"] = None


def _running(rows: list[dict], field: str, out_field: str) -> None:
    """A running total of `field`; after a hidden or missing value it stops (it would reveal that value)."""
    total, broken = 0.0, False
    for r in rows:
        v = r.get(field)
        if v is None or r.get("hidden"):
            broken = True
        if broken:
            r[out_field] = None
        else:
            total += v
            r[out_field] = round(total, 2)


def compute_metric(principal: Principal, metric: str, group_by: list[str] | None = None, period: str = "all",
                   top_n: int | None = None, sort: str = "desc", min_orders: int | None = None,
                   compare_to: str = "none", compare_period: str | None = None, rank_by: str = "value",
                   share_of_total: bool = False, only: dict[str, list] | None = None,
                   filters: dict[str, list] | None = None, cumulative: bool = False) -> dict:
    """admin_metric's work, callable from other tools with plain defaults. `filters` are the admin's (checked
    and normalised here); `only` is internal (e.g. one chart series). Both apply to every query."""
    group_by = list(group_by or [])
    sem = semantic()
    if metric not in sem["metrics"]:
        raise InvalidArguments(f"Unknown metric '{metric}'. Available: {', '.join(sem['metrics'])}.")
    unknown = [g for g in group_by if g not in sem["dimensions"]]
    if unknown or len(set(group_by)) != len(group_by):
        raise InvalidArguments(f"Unknown or repeated grouping {unknown}. Available: {', '.join(sem['dimensions'])}.")
    if sum(g in TIME_UNITS for g in group_by) > 1:
        raise InvalidArguments("Group by one time unit (day, week, month, quarter or year), not several.")
    filters = normalize_filters(filters)
    only = {**filters, **(only or {})}
    dims = group_by + [k for k in only if k not in group_by]
    if compare_period and compare_to == "none":
        compare_to = "period"
    comparing = compare_to != "none"
    m, today = sem["metrics"][metric], date.today()
    grain = choose_grain(metric, dims)
    additive = _additive(m, grain)
    if share_of_total and group_by and not splits_into_parts(m, grain):
        raise InvalidArguments("Shares need parts that add up to a whole; this metric is a rate, an average or a "
                               "distinct count (an order can fall in several groups). Leave share_of_total off.")
    if rank_by != "value" and not comparing:
        raise InvalidArguments("rank_by change needs a comparison (compare_to or compare_period).")
    time_key = group_by[0] if len(group_by) == 1 and group_by[0] in TIME_UNITS else None
    if cumulative and not (time_key and additive):
        raise InvalidArguments("cumulative needs one time grouping (day, week, month, quarter or year) and a sum or "
                               "count metric (running totals of rates or averages mean nothing).")
    if cumulative and (top_n or rank_by != "value"):
        raise InvalidArguments("cumulative keeps the series in time order: leave top_n and rank_by off.")
    sensitive, min_group = bool(m.get("sensitive")), sem["min_group"]
    start, end = period_bounds(period, today)
    rank_later = comparing and rank_by != "value"
    sql, params = compile_query(metric, group_by, start, end, min_orders,
                                None if (rank_later or time_key) else top_n, sort, only=only)
    rows = read_only(sql, params, max_rows=5000 if (rank_later or time_key) else 500)
    total = read_only(*compile_query(metric, [], start, end, None, None, sort, only=only))[0]
    if additive and total["value"] is None:   # no orders in the period: a sum or count of 0
        total["value"] = 0
    if time_key:
        rows = complete_series(rows, time_key, start, end, additive)
        mark_partial_and_thin(rows, time_key, end, today)
        mark_cut(rows, time_key, start, end)
    comparison = None

    if comparing:
        # Each time unit can always be compared with its own neighbour; the whole period only when it has a start.
        per_row_series = bool(time_key) and compare_to != "period"
        whole_window = start is not None or bool(compare_period)
        if not (per_row_series or whole_window):
            raise InvalidArguments("The period 'all' has nothing before it to compare with; choose a period.")
        b_total = None
        if whole_window:
            b_start, b_end, b_label = baseline_window(start, end, compare_to, compare_period, today)
            b_total = read_only(*compile_query(metric, [], b_start, b_end, None, None, sort, only=only))[0]
            if additive and b_total["value"] is None:   # no orders in the baseline: a sum or count of 0
                b_total["value"] = 0
        if per_row_series:
            if rows:
                lo = unit_period(time_key, baseline_label(time_key, rows[0][time_key], compare_to))[0]
                hi = unit_period(time_key, baseline_label(time_key, rows[-1][time_key], compare_to))[1]
                base = {str(r[time_key]): r for r in read_only(*compile_query(metric, group_by, lo, hi, None, None, sort,
                                                                              only=only), max_rows=5000)}
                for r in rows:
                    key = baseline_label(time_key, r[time_key], compare_to)
                    if r.get("partial") or r.get("cut"):   # only part of the unit: the same days of its baseline
                        u_start, u_end = unit_period(time_key, r[time_key])
                        a, b = max(u_start, start or u_start), min(u_end, end)
                        b_unit_start, b_unit_end = unit_period(time_key, key)
                        s0 = b_unit_start + (a - u_start)
                        e0 = min(b_unit_start + (b - u_start), b_unit_end)
                        prev = read_only(*compile_query(metric, [], s0, e0, None, None, sort, only=only))[0]
                        _attach(r, prev, f"{s0.isoformat()} to {e0.isoformat()} (same days)", sensitive, min_group)
                    else:
                        prev = base.get(str(key)) or {"value": 0 if additive else None, "orders": 0}
                        _attach(r, prev, str(key), sensitive, min_group)
            same = (f"the same {time_key} a year earlier" if compare_to == "same_period_last_year" and time_key != "year"
                    else f"the previous {time_key}")
            comparison = {"type": compare_to, "each_row": f"vs {same} (see compared_with)"}
        else:
            # every group (or the whole period) against the same group in the baseline window
            keep = {g: sorted({r[g] for r in rows if r[g] is not None}, key=str) for g in group_by}
            keep = {**only, **{g: v for g, v in keep.items() if v}}
            base_rows = read_only(*compile_query(metric, group_by, b_start, b_end, None, None, sort, only=keep),
                                  max_rows=5000) if group_by and rows else []
            base = {tuple(str(r[g]) for g in group_by): r for r in base_rows}
            for r in rows:
                prev = base.get(tuple(str(r[g]) for g in group_by)) if group_by else b_total
                _attach(r, prev or {"value": 0 if additive else None, "orders": 0}, b_label, sensitive, min_group)
            comparison = {"type": compare_to, "each_row": f"vs {b_label}"}
        if b_total is not None:
            whole: dict = {"value": total["value"]}
            _attach(whole, b_total, b_label, sensitive and bool(filters), min_group)
            comparison["baseline"] = {"start": b_start.isoformat(), "end": b_end.isoformat()}
            comparison["total"] = {"previous_total": whole["previous_value"], "compared_with": b_label,
                                   "change": whole["change"], "change_pct": whole["change_pct"]}
        else:
            comparison["baseline"] = comparison["total"] = None   # 'all' time: only the per-row comparisons
        if rank_later:
            ranked = [r for r in rows if r.get(rank_by) is not None]
            rows = sorted(ranked, key=lambda r: r[rank_by], reverse=sort == "desc")[:top_n or len(ranked)]
    if time_key and top_n and not rank_later:
        # "best month" / "worst 3 weeks": the series was built in full (gaps filled, flags set), now ranked by value
        ranked = [r for r in rows if r.get("value") is not None]
        rows = sorted(ranked, key=lambda r: r["value"], reverse=sort == "desc")[:top_n]

    hidden = 0
    if sensitive and group_by:
        for r in rows:
            if (r.get("orders") or 0) < min_group:
                r.update(value=None, hidden=f"fewer than {min_group} orders")
                if comparing:
                    r.update(change=None, change_pct=None)
                hidden += 1
    total_hidden = sensitive and bool(filters) and (total["orders"] or 0) < min_group
    if total_hidden:   # a filter narrow enough to describe one person: its total is hidden too
        total["value"] = None
    if cumulative:
        _running(rows, "value", "cumulative_value")
        if comparing:
            _running(rows, "previous_value", "previous_cumulative")
            for r in rows:
                if r.get("cumulative_value") is not None and r.get("previous_cumulative") is not None:
                    r["cumulative_change"] = round(r["cumulative_value"] - r["previous_cumulative"], 2)
                    r["cumulative_change_pct"] = (round(100 * r["cumulative_change"] / r["previous_cumulative"], 2)
                                                  if r["previous_cumulative"] else None)
    for r in rows:
        for g in group_by:
            if g in FLAG_LABELS and r.get(g) in FLAG_LABELS[g]:
                r[g] = FLAG_LABELS[g][r[g]]
        for g in ("buyer_state", "seller_state"):
            if g in group_by:
                r[f"{g}_name"] = state_name(r.get(g))
    out = {"metric": metric, "definition": m["label"], "unit": m.get("unit"),
           "period": {"start": start.isoformat() if start else "first order", "end": end.isoformat()},
           "group_by": group_by, "filters": describe_filters(filters), "rows": rows, "total": total["value"],
           "total_meaning": total_meaning(m, grain) + (" within the filters" if filters else ""),
           "total_orders": total["orders"], "orders_counted": orders_counted(m),
           "today": today.isoformat(), "figures_as_of": value(facts.refreshed_at())}
    if top_n and len(rows) >= top_n:
        out["rows_shown"] = f"the top {len(rows)} by {rank_by if comparing and rank_by != 'value' else 'value'} ({sort})"
    if comparison:
        out["comparison"] = comparison
    if share_of_total and group_by and total["value"]:
        for r in rows:
            r["share_pct"] = round(100 * r["value"] / total["value"], 2) if r["value"] is not None else None
        out["shown_groups_share_pct"] = round(100 * sum(r["value"] or 0 for r in rows) / total["value"], 2)
        out["share_of"] = "the whole period's total" + (" within the filters" if filters else "")
    notes = []
    thin = coverage_note(start, end)
    if thin:
        notes.append(thin)
    if comparing and comparison and comparison.get("baseline"):
        thin_base = coverage_note(date.fromisoformat(comparison["baseline"]["start"]),
                                  date.fromisoformat(comparison["baseline"]["end"]), "The comparison period")
        if thin_base:
            notes.append(thin_base)
    if comparing:
        notes.append("Each row's change is against its own compared_with value (previous_value); describe it by "
                     "that baseline.")
    if cumulative:
        notes.append("cumulative_value is the running total from the period's start" +
                     (", previous_cumulative the same for the comparison" if comparing else "") + ".")
    if hidden or total_hidden:
        notes.append(f"{hidden + total_hidden} figure(s) hidden: fewer than {min_group} orders, so they could "
                     "describe one person.")
    if any(r.get("partial") for r in rows):
        notes.append("The last group is not over yet (partial)" + (
            ", so it is compared with the same days of its baseline." if comparing else
            ": don't compare it with full periods as a trend."))
    if any(r.get("cut") for r in rows):
        notes.append("Groups marked cut cover only part of their dates (the period starts or ends inside them)" + (
            ", so they are compared with the same days of their baseline." if comparing else
            ": don't compare them with full units."))
    if any(r.get("few_orders") for r in rows):
        notes.append("Groups marked few_orders have under 10% of the typical number of orders (the records thin out "
                     "there): their values and changes are not a business trend; say so instead of reporting them as one.")
    if not any(r.get("orders") for r in rows) and not (not group_by and total["orders"]):
        notes.append("No orders matched in this period" + (" with these filters." if filters else "."))
    if notes:
        out["note"] = " ".join(notes)
    return out


def admin_metric(principal: Principal,
                 metric: str,
                 group_by: list[str] = Field(default_factory=list, max_length=2),
                 filters: dict[str, list[str | int]] | None = None,
                 period: str = "all",
                 top_n: int | None = Field(None, ge=1, le=50),
                 sort: Literal["desc", "asc"] = "desc",
                 min_orders: int | None = Field(None, ge=1, le=100_000),
                 compare_to: Comparison = "none",
                 compare_period: str | None = None,
                 rank_by: RankBy = "value",
                 share_of_total: bool = False,
                 cumulative: bool = False,
                 compare_previous: bool = False) -> dict:
    return compute_metric(principal, metric, group_by, period, top_n, sort, min_orders,
                          "previous_period" if compare_previous and compare_to == "none" else compare_to,
                          compare_period, rank_by, share_of_total, filters=filters, cumulative=cumulative)


# What each metric's per-row "orders" counts, from its filter (so a count is never mislabelled).
ORDERS_COUNTED = {"active": "orders excluding canceled and unavailable",
                  "active_with_items": "orders excluding canceled, unavailable and orders without items",
                  "delivered": "delivered orders", "reviewed": "reviewed orders"}


def orders_counted(m: dict) -> str:
    where = m.get("where")
    return ORDERS_COUNTED.get(where, "all orders" if not where else f"orders where {where}")


def total_meaning(m: dict, grain: str) -> str:
    expr = m[grain]
    if expr.startswith(("SUM(", "COUNT(")):
        return "the whole period's total"
    return "the whole period's overall value (an overall rate or average, not a sum of the rows)"


def splits_into_parts(m: dict, grain: str) -> bool:
    """Do the groups' values add up to the total? Sums and row counts do; rates, averages and distinct
    counts (an order with items in two categories counts in both) don't."""
    return m[grain].startswith("SUM(") or m[grain] == "COUNT(*)"


@functools.lru_cache(maxsize=4)
def _typical_daily_orders(as_of) -> float:
    row = read_only("SELECT COUNT(*) AS n, DATEDIFF(MAX(order_purchase_timestamp), MIN(order_purchase_timestamp)) + 1 "
                    "AS days FROM admin_order_facts WHERE order_status NOT IN ('canceled', 'unavailable')")[0]
    return (row["n"] or 0) / max(row["days"] or 1, 1)


def coverage_note(start: date | None, end: date, which: str = "This period") -> str | None:
    """A period with far fewer orders per day than the data's typical day is not a full picture."""
    if start is None:
        return None
    sql = ("SELECT COUNT(*) AS n FROM admin_order_facts WHERE order_status NOT IN ('canceled', 'unavailable') "
           "AND order_purchase_timestamp >= %s AND order_purchase_timestamp < %s")
    n = read_only(sql, (start.isoformat(), (end + timedelta(days=1)).isoformat()))[0]["n"] or 0
    days = (end - start).days + 1
    typical = _typical_daily_orders(facts.refreshed_at())
    if typical and n / days < 0.1 * typical:
        return (f"{which} has very few orders ({n} in {days} days; a typical day has about {typical:.0f}): "
                "the records thin out here, so its figures are not a full picture of the business. Say so.")
    return None


def _additive(m: dict, grain: str) -> bool:
    """Sums and counts are 0 for a period with no orders; averages and rates have no value."""
    return m[grain].startswith(("SUM(", "COUNT("))


def complete_series(rows: list[dict], key: str, start: date | None, end: date, additive: bool) -> list[dict]:
    """Every time unit from start to end, in order: a unit without orders gets 0 (sums and counts) or no
    value (averages), so 'change vs previous' always compares neighbouring units."""
    if not rows and start is None:
        return rows
    first = unit_floor(key, start) if start else unit_start(key, rows[0][key])
    found = {str(r[key]): r for r in rows}
    out, d = [], first
    while d <= end:
        if len(out) >= MAX_SERIES_POINTS:
            raise InvalidArguments(f"More than {MAX_SERIES_POINTS} {key}s in this period: choose a shorter period or a "
                                   "coarser time unit (week, month, quarter or year).")
        label = unit_label(key, d)
        out.append(found.get(str(label)) or {key: label, "value": 0 if additive else None, "orders": 0})
        d = unit_add(key, d, 1)
    return out


def mark_cut(rows: list[dict], key: str, start: date | None, end: date) -> None:
    """Flag units the period cuts (it starts or ends inside them); the unit still in progress is 'partial'."""
    for r in rows:
        if r.get("partial"):
            continue
        u_start, u_end = unit_period(key, r[key])
        a, b = max(u_start, start or u_start), min(u_end, end)
        if (a, b) != (u_start, u_end):
            r["cut"] = f"covers {a.isoformat()} to {b.isoformat()} only"


def mark_partial_and_thin(rows: list[dict], key: str, end: date, today: date) -> None:
    """Flag the unit still in progress, and units with far fewer orders than usual."""
    if rows and end == today and str(rows[-1][key]) == str(unit_label(key, today)):
        rows[-1]["partial"] = f"to {today.isoformat()}"
    counts = sorted(r["orders"] for r in rows if r.get("orders"))
    if len(counts) >= 3:
        typical = counts[len(counts) // 2]
        for r in rows:
            if typical >= 50 and (r.get("orders") or 0) < 0.1 * typical:
                r["few_orders"] = True


def _doc() -> str:
    sem = semantic()
    metrics = "\n".join(f"- {k}: {v['label']}" for k, v in sem["metrics"].items())
    dims = "\n".join(f"- {k}: {v['label']}" for k, v in sem["dimensions"].items())
    return f"""Platform figures from defined metrics: the server builds the query, so definitions are always
applied correctly. Use this first for any total, count, average, rate, ranking or comparison it covers.
Item revenue (item prices, no shipping) and GMV (payments incl. shipping) are different figures: use the
one asked for, and name it in the answer.
Metrics:
{metrics}
Groupings (group_by, at most 2; at most one time unit):
{dims}
Filters (filters = {{grouping: [values]}}, any of the groupings above, combined with AND): e.g.
{{"category": ["health_beauty"]}}, {{"buyer_state": ["SP", "RJ"]}}, {{"seller": ["54965bbe"]}} (an ID or its first 8+
characters), {{"payment_type": ["boleto"]}}, {{"weekday": ["Saturday", "Sunday"]}}. Use filters for any "for X" or
"only X" question instead of grouping by X.
Periods: {', '.join(PERIODS)}, a year ("2025"), a quarter ("2025-Q2"), a month ("2026-07"), a day
("2026-03-15") or a range ("2025-03-15..2025-06-30").
Time series: group_by one of day, week (ISO), month, quarter, year: every unit appears in order with no gaps;
the unit still in progress is marked partial, units with far fewer orders than usual few_orders.
cumulative = true adds a running total (cumulative_value) to a time series of a sum or count, e.g. year-to-date.
Options: top_n + sort (desc/asc) for the highest/lowest groups (or the best/worst time units); min_orders to
ignore small groups (e.g. sellers with at least 50 delivered orders); share_of_total adds each group's share_pct.
Comparisons (any metric, any grouping or none): compare_to = "previous_period" (each unit vs the one before it;
a whole period vs the same length just before) or "same_period_last_year" (each unit vs the same unit a year
earlier; a whole period vs the same dates a year earlier); or compare_period = any period to compare with.
Each row then has compared_with (the baseline's label), previous_value, change and change_pct, and
"comparison.total" compares the whole period; a unit not over yet is compared with the same days of its
baseline. With cumulative, also previous_cumulative and cumulative_change. rank_by = "change" or "change_pct"
ranks the groups by their change (e.g. the fastest-growing categories).
The result includes the definition, period, filters, total, the orders behind each value and figures_as_of
(the figures are refreshed hourly): quote the definition, period and filters in the answer. Examples:
item_revenue by month in 2026 with compare_to same_period_last_year; item_revenue by month with filters
category health_beauty; gmv by quarter in 2025; orders by weekday; item_revenue by month this_year with
cumulative and compare_to same_period_last_year (year-to-date vs last year); on_time_rate by seller with
min_orders 50, sort asc, top_n 10; item_revenue by category with compare_to same_period_last_year, rank_by
change, top_n 5."""


admin_metric.__doc__ = _doc()
admin_tool(reads=["admin_order_facts", "admin_seller_order_facts", "admin_item_facts"],
           aggregates=["admin_order_facts.buyer_state", "admin_order_facts.customer_key",
                       "admin_order_facts.payments_total", "admin_order_facts.payment_type",
                       "admin_seller_order_facts.buyer_state", "admin_item_facts.buyer_state"])(admin_metric)

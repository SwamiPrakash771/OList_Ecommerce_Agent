"""Helpers for admin tool outputs: periods, JSON-safe values, read-only queries, untrusted text."""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from decimal import Decimal

import pandas as pd
import pymysql

import db
from ai import metadata
from ai.admin.security import InvalidArguments, ToolError

OPEN_WINDOW_DAYS = 60   # an order not delivered after this long never completed in the records
TIME_LIMIT_MS = 8000          # free SQL written by the model (query_platform_data)
METRIC_TIME_LIMIT_MS = 15000  # queries the server builds itself (metrics, charts, lookups): bounded, but
                              # a cold cache on a small machine can make a correct one slow
MAX_ROWS = 500
PERIODS = ("all", "this_year", "last_year", "this_quarter", "last_quarter", "this_month", "last_month",
           "this_week", "last_week", "last_7_days", "last_30_days", "last_90_days", "last_12_months")


def now() -> datetime:
    return datetime.now().replace(microsecond=0)


def open_since() -> datetime:
    """Orders bought before this are old leftovers, not open work (catalog: seller_metrics 'open order')."""
    return now() - timedelta(days=OPEN_WINDOW_DAYS)


def _month_start(d: date) -> date:
    return d.replace(day=1)


def add_months(d: date, n: int) -> date:
    y, m = divmod(d.year * 12 + d.month - 1 + n, 12)
    return date(y, m + 1, 1)


def period_bounds(period: str, today: date) -> tuple[date | None, date]:
    """(start, end inclusive) for a named period, a year, a quarter ('2025-Q2'), a month, a day or a range."""
    if period == "all":
        return None, today
    if period == "this_year":
        return date(today.year, 1, 1), today
    if period == "last_year":
        return date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)
    if period == "this_month":
        return _month_start(today), today
    if period == "last_month":
        start = add_months(_month_start(today), -1)
        return start, _month_start(today) - timedelta(days=1)
    if period == "last_30_days":
        return today - timedelta(days=29), today
    if period == "last_90_days":
        return today - timedelta(days=89), today
    if period == "last_12_months":
        return add_months(_month_start(today), -11), today
    if period == "last_7_days":
        return today - timedelta(days=6), today
    if period in ("this_week", "last_week"):
        monday = today - timedelta(days=today.weekday()) - timedelta(days=7 if period == "last_week" else 0)
        return monday, today if period == "this_week" else monday + timedelta(days=6)
    if period in ("this_quarter", "last_quarter"):
        start = date(today.year, 3 * ((today.month - 1) // 3) + 1, 1)
        if period == "this_quarter":
            return start, today
        start = add_months(start, -3)
        return start, add_months(start, 3) - timedelta(days=1)
    if ".." in period:   # a date range: 2025-03-15..2025-06-30
        first, _, last = period.partition("..")
        start, end = _day(first.strip(), period), _day(last.strip(), period)
        if start > end:
            raise InvalidArguments(f"The range '{period}' ends before it starts.")
        return start, min(end, today)
    if len(period) == 4 and period.isdigit():
        return date(int(period), 1, 1), min(date(int(period), 12, 31), today)
    if len(period) == 7 and period[4:6] == "-Q" and period[:4].isdigit() and period[6] in "1234":
        start = date(int(period[:4]), 3 * (int(period[6]) - 1) + 1, 1)
        return start, min(add_months(start, 3) - timedelta(days=1), today)
    if len(period) == 7 and period[4] == "-" and period.replace("-", "").isdigit() and 1 <= int(period[5:]) <= 12:
        start = date(int(period[:4]), int(period[5:]), 1)
        return start, min(add_months(start, 1) - timedelta(days=1), today)
    if len(period) == 10 and period[4] == "-" and period[7] == "-":
        day = _day(period, period)
        return day, day
    raise InvalidArguments(f"Unknown period '{period}'. Use one of {', '.join(PERIODS)}, a year (2025), a quarter "
                           "(2025-Q2), a month (2026-07), a day (2026-03-15) or a range (2025-03-15..2025-06-30).")


def _day(text: str, period: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise InvalidArguments(f"'{text}' in period '{period}' isn't a date (use YYYY-MM-DD).") from None


def value(v):
    """JSON-safe value: money and rates rounded to 2 decimals, dates as text, NaN as None."""
    if v is None or v is pd.NaT or (isinstance(v, float) and math.isnan(v)):
        return None
    if isinstance(v, Decimal):   # whole numbers (counts from SUM) stay integers
        return int(v) if v == v.to_integral_value() else round(float(v), 2)
    if isinstance(v, float):
        return round(v, 2)
    if isinstance(v, pd.Timestamp):
        v = v.to_pydatetime()
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M")
    if isinstance(v, date):
        return v.isoformat()
    return v


def as_dt(v) -> datetime | None:
    if v is None or v is pd.NaT or (isinstance(v, float) and math.isnan(v)):
        return None
    return v.to_pydatetime() if isinstance(v, pd.Timestamp) else v


def read_only(sql: str, params: dict | tuple | None = None, *, max_rows: int = MAX_ROWS,
              time_limit_ms: int = METRIC_TIME_LIMIT_MS) -> list[dict]:
    """Run one query in a read-only, time-limited transaction; more than max_rows is refused."""
    try:
        with db.cursor() as cur:
            cur.execute(f"SET SESSION max_execution_time = {int(time_limit_ms)}")
            cur.execute("START TRANSACTION READ ONLY")
            try:
                cur.execute(sql, params)
                rows = cur.fetchmany(max_rows + 1)
            finally:
                cur.execute("ROLLBACK")
                cur.execute("SET SESSION max_execution_time = 0")
    except pymysql.err.MySQLError as exc:
        if exc.args and exc.args[0] == 3024:
            raise ToolError("That took too long and was stopped; narrow the period or grouping.") from None
        raise
    if len(rows) > max_rows:
        raise ToolError(f"More than {max_rows} rows; use top_n, a filter or a coarser grouping.")
    return [{k: value(v) for k, v in r.items()} for r in rows]


def state_name(code: str | None) -> str | None:
    """Brazilian state code -> name (catalog: brazil_states), e.g. PA -> Pará."""
    return (metadata.load().get("brazil_states") or {}).get((code or "").upper()) if code else None


def untrusted(text: str | None) -> dict | None:
    """Text written by customers (reviews): data for the model, never instructions."""
    return None if not text else {"untrusted_text": text}


def resolve_id(table: str, column: str, given: str, what: str) -> str:
    """A full 32-character ID from the full ID or a unique prefix of at least 8 characters."""
    given = (given or "").strip().lower()
    if not (8 <= len(given) <= 32) or not all(c in "0123456789abcdef" for c in given):
        raise InvalidArguments(f"Give the {what} ID (at least its first 8 characters).")
    rows = db.query_df(f"SELECT DISTINCT {column} AS id FROM {table} WHERE {column} LIKE %s LIMIT 2", (given + "%",))
    if rows.empty:
        from ai.admin.security import NotFound
        raise NotFound(f"No {what} found with ID {given}.")
    if len(rows) > 1:
        raise InvalidArguments(f"More than one {what} starts with {given}; give more characters.")
    return rows.id[0]

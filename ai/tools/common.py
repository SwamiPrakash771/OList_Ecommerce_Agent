"""Small helpers for building tool outputs: JSON-safe values and delivery status."""

from __future__ import annotations

import math
from datetime import date, datetime

import pandas as pd

from ai.clock import reference_now


def iso(v) -> str | None:
    """Datetime/date -> 'YYYY-MM-DD HH:MM' / 'YYYY-MM-DD'; None/NaT -> None."""
    if v is None or (isinstance(v, float) and math.isnan(v)) or v is pd.NaT:
        return None
    if isinstance(v, pd.Timestamp):
        v = v.to_pydatetime()
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M")
    if isinstance(v, date):
        return v.isoformat()
    return str(v)


def num(v) -> float | None:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    return round(float(v), 2)


def as_dt(v) -> datetime | None:
    if v is None or v is pd.NaT or (isinstance(v, float) and math.isnan(v)):
        return None
    return v.to_pydatetime() if isinstance(v, pd.Timestamp) else v


def delivery_status(status: str, purchased, estimated, delivered) -> dict:
    """Plain-language delivery state, measured against the right 'now' (ai.clock)."""
    purchased, estimated, delivered = as_dt(purchased), as_dt(estimated), as_dt(delivered)
    if status in ("canceled", "unavailable"):
        return {"state": status}
    if delivered:
        if estimated is None:
            return {"state": "delivered"}
        late = (delivered.date() - estimated.date()).days
        return {"state": "delivered_late", "days_late": late} if late > 0 else {"state": "delivered_on_time"}
    now = reference_now(purchased)
    if estimated and now.date() > estimated.date():
        return {"state": "not_delivered_past_estimate", "days_late": (now.date() - estimated.date()).days}
    return {"state": "in_progress", "estimated_delivery": iso(estimated)}

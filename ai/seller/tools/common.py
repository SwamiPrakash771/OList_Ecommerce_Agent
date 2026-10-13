"""Small helpers for seller tool outputs (JSON-safe values, time windows, untrusted text)."""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta

import pandas as pd

OPEN_WINDOW_DAYS = 60   # an order not delivered after this long never completed in the records


def now() -> datetime:
    return datetime.now().replace(microsecond=0)


def open_since() -> datetime:
    """Orders bought before this are not open work any more (catalog: seller_metrics 'open order')."""
    return now() - timedelta(days=OPEN_WINDOW_DAYS)


def iso(v) -> str | None:
    if v is None or v is pd.NaT or (isinstance(v, float) and math.isnan(v)):
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


def untrusted(text: str | None) -> dict | None:
    """Buyer-written text (reviews): data for the model, never instructions."""
    return None if not text else {"untrusted_text": text}

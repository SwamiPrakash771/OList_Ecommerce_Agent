"""Which "now" to measure ages against, and how far the historical data was moved in time.

Olist's data originally ran from 2016-09 to 2018-10-17. To make questions like "last 12 months"
meaningful, shift_dates.py moves every historical date forward by DATE_SHIFT_DAYS (416 weeks, so
weekdays are preserved), which puts the data's last day at 2026-10-07, just before the app's own
records. Gaps between dates (delivery times, delays, refund ages) don't change.

Records from the dataset period are aged against the dataset's last day (DATASET_NOW), so a refund
that was stuck when the data ends stays "34 days" stuck instead of growing every day; records
created in the app (after it) are aged against the real current time.
"""

from datetime import datetime, timedelta

ORIGINAL_DATASET_START = datetime(2016, 9, 4)            # first order in the Kaggle data
ORIGINAL_DATASET_END = datetime(2018, 10, 17, 23, 59, 59)  # last order in the Kaggle data
DATE_SHIFT_DAYS = 416 * 7                                  # 2,912 days; applied by shift_dates.py

DATASET_START = ORIGINAL_DATASET_START + timedelta(days=DATE_SHIFT_DAYS)   # 2024-08-25
DATASET_NOW = ORIGINAL_DATASET_END + timedelta(days=DATE_SHIFT_DAYS)       # 2026-10-07 23:59:59


def shifted(original: datetime) -> datetime:
    """Where a date from the original Kaggle timeline lands after the shift."""
    return original + timedelta(days=DATE_SHIFT_DAYS)


def reference_now(ts: datetime | None = None) -> datetime:
    """The 'now' to compare `ts` with: the dataset's end for historical records, else real now."""
    if ts is not None and ts <= DATASET_NOW:
        return DATASET_NOW
    return datetime.now()


def days_since(ts: datetime | None) -> int | None:
    if ts is None:
        return None
    return (reference_now(ts) - ts).days

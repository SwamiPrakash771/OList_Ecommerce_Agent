"""Which "now" to measure ages against.

Olist's data is historical and ends on 2018-10-17. Measuring a 2018 refund against
today's date would make every unfinished one look years overdue, so records from the
dataset period are aged against the dataset's last day, and records created in the app
(after it) against the real current time.
"""

from datetime import datetime

DATASET_NOW = datetime(2018, 10, 17, 23, 59, 59)


def reference_now(ts: datetime | None = None) -> datetime:
    """The 'now' to compare `ts` with: the dataset's end for historical records, else real now."""
    if ts is not None and ts <= DATASET_NOW:
        return DATASET_NOW
    return datetime.now()


def days_since(ts: datetime | None) -> int | None:
    if ts is None:
        return None
    return (reference_now(ts) - ts).days

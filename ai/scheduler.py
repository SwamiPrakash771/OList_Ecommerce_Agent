"""Background jobs: a small scheduler whose state lives in MySQL.

    python -m ai.scheduler run              run the scheduler loop (one dedicated process)
    python -m ai.scheduler run-now <job>    run one job immediately (triggered_by = manual)
    python -m ai.scheduler status           jobs, next run, last result, overdue warnings
    python -m ai.scheduler history [job]    recent runs

Design (docs/AI_PLAN.md section 10):
- Job definitions are code (JOBS below); scheduler_jobs holds their schedule, enabled flag
  and next run time, so an admin can pause or reschedule a job without a deploy.
- Every attempt is a row in scheduler_runs (retries get a new row linked by retry_of_run_id).
- A MySQL named lock (GET_LOCK) per job means a job never runs twice at once, even if two
  scheduler processes are started by mistake.
- Jobs are idempotent and incremental where it matters; checkpoints live in scheduler_state.
- Run this as ONE process next to the API, never inside the API workers.

Schedules: "every:5m", "every:1h", "daily@02:00" (server local time).
Timeouts are soft: a run that takes longer than timeout_s is recorded as 'timeout'.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

import db

WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"
POLL_SECONDS = 30
RETRY_BACKOFF_S = (10, 60)  # wait before retry 1, retry 2


@dataclass
class JobResult:
    rows_processed: int = 0
    summary: str = ""
    tokens_used: int = 0
    cost: float = 0.0


@dataclass
class Job:
    name: str
    description: str
    schedule: str
    func: Callable[["JobContext"], JobResult]
    timeout_s: int = 600
    max_retries: int = 2


@dataclass
class JobContext:
    job_name: str
    run_id: int
    triggered_by: str
    started_at: datetime = field(default_factory=lambda: datetime.now().replace(microsecond=0))

    def get_state(self, default=None):
        row = db.query_one("SELECT watermark FROM scheduler_state WHERE job_name = %s", (self.job_name,))
        if not row:
            return default
        return json.loads(row["watermark"]) if isinstance(row["watermark"], str) else row["watermark"]

    def set_state(self, value) -> None:
        """Advance the checkpoint. Call only after the work it describes is committed."""
        db.execute(
            "INSERT INTO scheduler_state (job_name, watermark, updated_at) VALUES (%s, %s, %s) "
            "ON DUPLICATE KEY UPDATE watermark = VALUES(watermark), updated_at = VALUES(updated_at)",
            (self.job_name, json.dumps(value, default=str), datetime.now().replace(microsecond=0)),
        )


JOBS: dict[str, Job] = {}


def job(name: str, description: str, schedule: str, timeout_s: int = 600, max_retries: int = 2):
    def register(func: Callable[[JobContext], JobResult]):
        next_run(schedule, datetime.now())  # validate the schedule string at import time
        JOBS[name] = Job(name, description, schedule, func, timeout_s, max_retries)
        return func

    return register


# ---- schedules ---------------------------------------------------------------------------


def next_run(schedule: str, after: datetime) -> datetime:
    kind, _, spec = schedule.partition(":") if schedule.startswith("every:") else schedule.partition("@")
    if kind == "every":
        unit = spec[-1]
        amount = int(spec[:-1])
        step = {"m": timedelta(minutes=amount), "h": timedelta(hours=amount)}.get(unit)
        if not step or amount <= 0:
            raise ValueError(f"bad schedule {schedule!r}")
        return (after + step).replace(microsecond=0)
    if kind == "daily":
        hour, minute = (int(x) for x in spec.split(":"))
        candidate = after.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return candidate if candidate > after else candidate + timedelta(days=1)
    raise ValueError(f"bad schedule {schedule!r}")


def interval(schedule: str) -> timedelta:
    now = datetime(2000, 1, 1)
    first = next_run(schedule, now)
    return next_run(schedule, first) - first


# ---- job registry -> database ------------------------------------------------------------


def sync_jobs() -> None:
    """Insert jobs defined in code; keep schedule/enabled edits made in the database."""
    now = datetime.now().replace(microsecond=0)
    for j in JOBS.values():
        db.execute(
            """
            INSERT INTO scheduler_jobs (job_name, description, schedule, enabled, timeout_s, max_retries,
                                        next_run_at, updated_by, updated_at)
            VALUES (%s, %s, %s, 1, %s, %s, %s, 'code', %s)
            ON DUPLICATE KEY UPDATE description = VALUES(description)
            """,
            (j.name, j.description, j.schedule, j.timeout_s, j.max_retries, next_run(j.schedule, now), now),
        )


# ---- running -------------------------------------------------------------------------------


def _start_run(name: str, triggered_by: str, scheduled_for, attempt: int, retry_of: int | None) -> int:
    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO scheduler_runs (job_name, triggered_by, scheduled_for, started_at, status,
                                        attempt_no, retry_of_run_id, worker_id)
            VALUES (%s, %s, %s, %s, 'running', %s, %s, %s)
            """,
            (name, triggered_by, scheduled_for, datetime.now().replace(microsecond=0), attempt, retry_of, WORKER_ID),
        )
        return cur.lastrowid


def _finish_run(run_id: int, status: str, duration_ms: int, result: JobResult | None,
                error: Exception | None) -> None:
    db.execute(
        """
        UPDATE scheduler_runs SET finished_at = %s, status = %s, duration_ms = %s, rows_processed = %s,
            tokens_used = %s, cost = %s, output_summary = %s, error_code = %s, error_summary = %s
        WHERE run_id = %s
        """,
        (datetime.now().replace(microsecond=0), status, duration_ms,
         result.rows_processed if result else None, result.tokens_used if result else None,
         result.cost if result else None, (result.summary[:1000] if result else None),
         type(error).__name__ if error else None, (str(error)[:1000] if error else None), run_id),
    )


def run_job(name: str, triggered_by: str = "manual") -> str:
    """Run a job (with retries) under its lock. Returns the final status."""
    j = JOBS.get(name)
    if j is None:
        raise SystemExit(f"unknown job {name!r}; known: {', '.join(JOBS)}")
    cfg = db.query_one("SELECT * FROM scheduler_jobs WHERE job_name = %s", (name,))
    if cfg is None:
        sync_jobs()
        cfg = db.query_one("SELECT * FROM scheduler_jobs WHERE job_name = %s", (name,))
    scheduled_for = cfg["next_run_at"] if triggered_by == "schedule" else None

    # Named locks belong to a connection, so hold one connection for the whole run.
    lock_con = db.connect()
    try:
        with lock_con.cursor() as cur:
            cur.execute("SELECT GET_LOCK(%s, 0) AS got", (f"olist_job:{name}",))
            got = cur.fetchone()["got"] == 1
        if not got:
            run_id = _start_run(name, triggered_by, scheduled_for, 1, None)
            _finish_run(run_id, "skipped", 0, JobResult(summary="another run holds the lock"), None)
            return "skipped"

        status, retry_of = "failed", None
        for attempt in range(1, int(cfg["max_retries"]) + 2):
            run_id = _start_run(name, triggered_by if attempt == 1 else "retry", scheduled_for, attempt, retry_of)
            started = time.monotonic()
            result, error = None, None
            try:
                result = j.func(JobContext(name, run_id, triggered_by))
                elapsed = time.monotonic() - started
                status = "timeout" if elapsed > int(cfg["timeout_s"]) else "success"
            except Exception as exc:  # recorded, then retried
                error, status = exc, "failed"
                traceback.print_exc()
            duration_ms = int((time.monotonic() - started) * 1000)
            _finish_run(run_id, status, duration_ms, result, error)
            db.execute("UPDATE scheduler_jobs SET last_run_id = %s WHERE job_name = %s", (run_id, name))
            if status == "success":
                db.execute("UPDATE scheduler_jobs SET last_success_at = %s WHERE job_name = %s",
                           (datetime.now().replace(microsecond=0), name))
                break
            if status == "timeout":
                break
            retry_of = run_id
            if attempt <= int(cfg["max_retries"]):
                time.sleep(RETRY_BACKOFF_S[min(attempt - 1, len(RETRY_BACKOFF_S) - 1)])
        return status
    finally:
        try:
            if triggered_by == "schedule":
                db.execute("UPDATE scheduler_jobs SET next_run_at = %s WHERE job_name = %s",
                           (next_run(cfg["schedule"], datetime.now()), name))
            with lock_con.cursor() as cur:
                cur.execute("SELECT RELEASE_LOCK(%s)", (f"olist_job:{name}",))
        finally:
            lock_con.close()


def tick() -> list[tuple[str, str]]:
    due = db.query_df(
        "SELECT job_name FROM scheduler_jobs WHERE enabled = 1 AND next_run_at <= %s ORDER BY next_run_at",
        (datetime.now(),),
    )
    return [(name, run_job(name, "schedule")) for name in due.job_name if name in JOBS]


def run_forever() -> None:
    sync_jobs()
    print(f"scheduler {WORKER_ID} started; jobs: {', '.join(JOBS)}")
    while True:
        for name, status in tick():
            print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {name}: {status}")
        time.sleep(POLL_SECONDS)


def status_report() -> list[dict]:
    rows = db.query_df(
        """
        SELECT j.job_name, j.schedule, j.enabled, j.next_run_at, j.last_success_at,
               r.status AS last_status, r.started_at AS last_started, r.output_summary, r.error_summary
        FROM scheduler_jobs j LEFT JOIN scheduler_runs r ON r.run_id = j.last_run_id
        ORDER BY j.job_name
        """
    ).to_dict("records")
    now = datetime.now()
    for r in rows:
        expected = interval(r["schedule"]) * 2 + timedelta(minutes=10)
        last_ok = r["last_success_at"]
        has_success = last_ok is not None and last_ok == last_ok  # NaT check
        r["never_run"] = r["last_status"] is None or r["last_status"] != r["last_status"]
        # Overdue: enabled, and the last success is older than two intervals (or it has run but never succeeded).
        r["overdue"] = bool(r["enabled"]) and not r["never_run"] and (not has_success or now - last_ok > expected)
    return rows


# ---- jobs ------------------------------------------------------------------------------------------


@job("catalog_profile", "Refresh live column stats and record catalog drift (ai/catalog.py)", "daily@02:00",
     timeout_s=900)
def catalog_profile(ctx: JobContext) -> JobResult:
    from ai import catalog

    r = catalog.profile()
    return JobResult(rows_processed=r["columns_profiled"], summary=f"open drift alerts: {r['open_drift']}")


@job("pending_action_expiry", "Expire confirm-card actions the user never decided on", "every:5m", timeout_s=60)
def pending_action_expiry(ctx: JobContext) -> JobResult:
    n = db.execute(
        "UPDATE ai_pending_actions SET status = 'expired', decided_at = %s WHERE status = 'pending' AND expires_at < %s",
        (ctx.started_at, ctx.started_at),
    )
    return JobResult(rows_processed=n, summary=f"expired {n} pending action(s)")


@job("usage_rollup", "Rebuild daily AI usage totals and check the monthly budget", "every:1h", timeout_s=300)
def usage_rollup(ctx: JobContext) -> JobResult:
    days = [ctx.started_at.date() - timedelta(days=1), ctx.started_at.date()]
    rebuilt = 0
    with db.transaction() as cur:
        for d in days:  # recompute from the ledger, so the totals are always exact and re-runnable
            cur.execute("DELETE FROM ai_usage_daily WHERE usage_date = %s", (d,))
            rebuilt += cur.execute(
                """
                INSERT INTO ai_usage_daily (usage_date, subject, role, requests, input_tokens, output_tokens,
                                            cached_tokens, cost)
                SELECT DATE(created_at), subject, ANY_VALUE(role), COUNT(*), SUM(input_tokens), SUM(output_tokens),
                       SUM(cached_tokens), SUM(cost)
                FROM ai_usage WHERE created_at >= %s AND created_at < %s
                GROUP BY DATE(created_at), subject
                """,
                (d, d + timedelta(days=1)),
            )
    month_start = ctx.started_at.date().replace(day=1)
    spent = float(db.query_one("SELECT COALESCE(SUM(cost), 0) AS c FROM ai_usage WHERE created_at >= %s",
                               (month_start,))["c"])
    budget_row = db.query_one("SELECT value FROM ai_settings WHERE setting_key = 'monthly_budget_usd'")
    budget = float(json.loads(budget_row["value"])) if budget_row else 0.0
    pct = 100 * spent / budget if budget else 0.0
    level = 100 if pct >= 100 else 80 if pct >= 80 else 0
    state = ctx.get_state({}) or {}
    alert = ""
    if level and (state.get("month") != str(month_start) or state.get("level", 0) < level):
        alert = f" ALERT: {level}% of the monthly budget reached"
        ctx.set_state({"month": str(month_start), "level": level})
    return JobResult(rows_processed=rebuilt,
                     summary=f"month spend ${spent:.4f} of ${budget:.2f} ({pct:.1f}%).{alert}")


@job("retention_purge", "Delete expired and user-deleted conversations; trim old run history", "daily@03:00",
     timeout_s=1800)
def retention_purge(ctx: JobContext) -> JobResult:
    settings = {r["setting_key"]: json.loads(r["value"]) for r in db.query_df(
        "SELECT setting_key, value FROM ai_settings WHERE setting_key IN "
        "('conversation_retention_days', 'deleted_conversation_grace_days')").to_dict("records")}
    keep_days = int(settings.get("conversation_retention_days", 90))
    grace_days = int(settings.get("deleted_conversation_grace_days", 30))
    now = ctx.started_at
    total = 0

    def purge(where: str, params: tuple) -> int:
        removed = 0
        while True:  # small batches keep locks short; messages/tool calls cascade
            ids = db.query_df(f"SELECT conversation_id FROM ai_conversations WHERE {where} LIMIT 500", params)
            if ids.empty:
                return removed
            removed += db.execute(
                f"DELETE FROM ai_conversations WHERE conversation_id IN ({', '.join(['%s'] * len(ids))})",
                tuple(ids.conversation_id),
            )

    # 1. Conversations the user deleted, after the grace period (legal holds excepted).
    total += purge("deleted_at IS NOT NULL AND deleted_at < %s AND retention_class <> 'legal_hold'",
                   (now - timedelta(days=grace_days),))
    # 2. Standard conversations inactive for longer than the retention period.
    #    Ticket-linked and legal-hold conversations follow their own class rules.
    total += purge("retention_class = 'standard' AND COALESCE(last_message_at, created_at) < %s",
                   (now - timedelta(days=keep_days),))
    # 3. Run history older than 90 days.
    runs = db.execute("DELETE FROM scheduler_runs WHERE started_at < %s AND run_id NOT IN "
                      "(SELECT last_run_id FROM (SELECT last_run_id FROM scheduler_jobs WHERE last_run_id IS NOT NULL) j)",
                      (now - timedelta(days=90),))
    return JobResult(rows_processed=total + runs,
                     summary=f"purged {total} conversation(s) and {runs} old run record(s)")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "run":
        run_forever()
    elif cmd == "run-now" and len(sys.argv) > 2:
        sync_jobs()
        print(f"{sys.argv[2]}: {run_job(sys.argv[2], 'manual')}")
    elif cmd == "status":
        sync_jobs()
        def show(v) -> str:
            return "-" if v is None or v != v else str(v)  # None / NaN from pandas -> "-"

        for r in status_report():
            flag = "  OVERDUE" if r["overdue"] else "  (never run)" if r["never_run"] else ""
            summary = "" if r["never_run"] else show(r["output_summary"])
            print(f"{r['job_name']:<22} {r['schedule']:<12} {'on ' if r['enabled'] else 'off'} "
                  f"next {r['next_run_at']}  last {show(r['last_status'])} {summary}{flag}")
    elif cmd == "history":
        where = "WHERE job_name = %s" if len(sys.argv) > 2 else ""
        rows = db.query_df(f"SELECT run_id, job_name, triggered_by, status, attempt_no, started_at, duration_ms, "
                           f"rows_processed, output_summary, error_summary FROM scheduler_runs {where} "
                           f"ORDER BY run_id DESC LIMIT 20", (sys.argv[2],) if len(sys.argv) > 2 else None)
        print(rows.to_string(index=False))
    else:
        print(__doc__)

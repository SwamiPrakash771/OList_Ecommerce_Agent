"""MySQL connection helpers. Settings come from .env.

Opening a MySQL connection costs ~400 ms here (authentication handshake), while
most queries take a few ms. So connections are kept in a small pool and reused
instead of being opened per query.

There are two pools:
  - the app pool (MYSQL_USER), used by the web app;
  - the agent pool (AGENT_MYSQL_USER), a least-privilege MySQL user for AI tools
    (see ai/db_user.py). Code inside `with db.as_agent():` runs every query on the
    agent pool, so the database itself refuses anything the agent isn't granted.

Failure handling (docs/ERROR_HANDLING.md):
  - every pooled connection has a connect timeout and read/write timeouts, so a hung
    database can't block requests forever;
  - each pool has a ceiling on open connections; when all are busy a request waits up to
    MYSQL_POOL_WAIT seconds, then gets DatabaseBusy instead of piling more connections on;
  - "MySQL is down / unreachable / out of connections" is raised as DatabaseUnavailable,
    which the API turns into a friendly 503;
  - reads (query_df, query_one) retry once after a lost connection, e.g. right after a
    MySQL restart. Writes are never retried automatically.
"""

import contextvars
import os
import queue
import threading
import time
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pymysql
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

POOL_SIZE = int(os.environ.get("MYSQL_POOL_SIZE", 10))            # idle connections kept
POOL_WAIT_S = float(os.environ.get("MYSQL_POOL_WAIT", 5))        # wait for a free connection
CONNECT_TIMEOUT_S = int(os.environ.get("MYSQL_CONNECT_TIMEOUT", 5))
READ_TIMEOUT_S = int(os.environ.get("MYSQL_READ_TIMEOUT", 60))
WRITE_TIMEOUT_S = int(os.environ.get("MYSQL_WRITE_TIMEOUT", 60))

# MySQL client errors that mean "the server isn't reachable / the connection is gone".
CONNECTION_ERRORS = {1040, 2003, 2005, 2006, 2013, 2055}


class DatabaseUnavailable(Exception):
    """MySQL is down, unreachable or out of connections. Shown to users as a friendly 503."""


class DatabaseBusy(DatabaseUnavailable):
    """Every connection in the pool is in use and none came free in time."""


def _connection_error(exc: BaseException) -> bool:
    return isinstance(exc, pymysql.OperationalError) and bool(exc.args) and exc.args[0] in CONNECTION_ERRORS


class Pool:
    def __init__(self, user_env: str, password_env: str, max_open: int = 10):
        self.user_env, self.password_env = user_env, password_env
        self.max_open = max_open
        self._idle: queue.LifoQueue[pymysql.Connection] = queue.LifoQueue(maxsize=POOL_SIZE)
        self._slots = threading.BoundedSemaphore(max_open)   # one slot per open connection

    def _open(self, *, timeouts: bool = True) -> pymysql.Connection:
        if not os.environ.get(self.user_env):
            raise RuntimeError(f"{self.user_env} is not set in .env")
        try:
            return pymysql.connect(
                host=os.environ["MYSQL_HOST"],
                port=int(os.environ.get("MYSQL_PORT", 3306)),
                user=os.environ[self.user_env],
                password=os.environ.get(self.password_env, ""),
                database=os.environ["MYSQL_DATABASE"],
                charset="utf8mb4",
                cursorclass=pymysql.cursors.DictCursor,
                autocommit=True,
                connect_timeout=CONNECT_TIMEOUT_S,
                read_timeout=READ_TIMEOUT_S if timeouts else None,
                write_timeout=WRITE_TIMEOUT_S if timeouts else None,
            )
        except pymysql.OperationalError as exc:
            if _connection_error(exc):
                raise DatabaseUnavailable(f"MySQL is not reachable ({exc.args[0]})") from exc
            raise

    def connect(self) -> pymysql.Connection:
        """A connection outside the pool, with no read timeout (setup scripts, long loads)."""
        return self._open(timeouts=False)

    def acquire(self) -> pymysql.Connection:
        try:
            con = self._idle.get_nowait()
        except queue.Empty:
            if not self._slots.acquire(timeout=POOL_WAIT_S):
                raise DatabaseBusy(f"all {self.max_open} database connections are busy")
            try:
                return self._open()
            except BaseException:
                self._slots.release()
                raise
        try:
            con.ping()  # a connection MySQL closed while idle is replaced below
        except pymysql.Error:
            self._close(con)
            if not self._slots.acquire(timeout=POOL_WAIT_S):
                raise DatabaseBusy(f"all {self.max_open} database connections are busy")
            try:
                con = self._open()
            except BaseException:
                self._slots.release()
                raise
        return con

    def prefill(self, n: int) -> None:
        """Open connections ahead of time: each new MySQL connection costs ~400 ms."""
        opened = [self.acquire() for _ in range(n)]
        for con in opened:
            self.release(con, healthy=True)

    def release(self, con: pymysql.Connection, healthy: bool) -> None:
        if healthy and con.open:
            try:
                self._idle.put_nowait(con)
                return
            except queue.Full:
                pass
        self._close(con)

    def _close(self, con: pymysql.Connection) -> None:
        try:
            con.close()
        except pymysql.Error:
            pass
        try:
            self._slots.release()
        except ValueError:   # already released (never more than max_open)
            pass

    def ping(self) -> bool:
        """Health check: can this pool reach MySQL right now?"""
        try:
            with _cursor_on(self) as cur:
                cur.execute("SELECT 1")
                return True
        except Exception:
            return False


APP_POOL = Pool("MYSQL_USER", "MYSQL_PASSWORD", int(os.environ.get("MYSQL_POOL_MAX", 15)))
# The agent's MySQL user is capped at 20 connections (ai/db_user.py); stay below it.
AGENT_POOL = Pool("AGENT_MYSQL_USER", "AGENT_MYSQL_PASSWORD", int(os.environ.get("AGENT_MYSQL_POOL_MAX", 18)))
# The seller agent's own user (ai/seller/db_user.py; column-level grants), capped below its limit.
SELLER_AGENT_POOL = Pool("SELLER_AGENT_MYSQL_USER", "SELLER_AGENT_MYSQL_PASSWORD",
                         int(os.environ.get("SELLER_AGENT_MYSQL_POOL_MAX", 10)))
_current_pool: contextvars.ContextVar[Pool] = contextvars.ContextVar("db_pool", default=APP_POOL)


def connect() -> pymysql.Connection:
    """A new app-user connection outside the pool (used by setup scripts)."""
    return APP_POOL.connect()


@contextmanager
def as_agent():
    """Run every query in this block as the least-privilege agent MySQL user."""
    token = _current_pool.set(AGENT_POOL)
    try:
        yield
    finally:
        _current_pool.reset(token)


def current_pool() -> Pool:
    return _current_pool.get()


@contextmanager
def as_seller_agent():
    """Run every query in this block as the seller agent's MySQL user."""
    token = _current_pool.set(SELLER_AGENT_POOL)
    try:
        yield
    finally:
        _current_pool.reset(token)


@contextmanager
def _cursor_on(pool: Pool):
    con = pool.acquire()
    healthy = True
    try:
        with con.cursor() as cur:
            yield cur
    except pymysql.OperationalError as exc:
        healthy = False
        if _connection_error(exc):
            raise DatabaseUnavailable(f"lost the database connection ({exc.args[0]})") from exc
        raise
    finally:
        pool.release(con, healthy)


@contextmanager
def cursor():
    with _cursor_on(_current_pool.get()) as cur:  # the connection goes back to the pool it came from
        yield cur


@contextmanager
def transaction():
    """Cursor whose statements commit together, or roll back if anything raises."""
    pool = _current_pool.get()
    con = pool.acquire()
    healthy = True
    con.autocommit(False)
    try:
        with con.cursor() as cur:
            yield cur
        con.commit()
    except Exception as exc:
        try:
            con.rollback()
        except pymysql.Error:
            healthy = False
        if _connection_error(exc):
            healthy = False
            raise DatabaseUnavailable(f"lost the database connection ({exc.args[0]})") from exc
        raise
    finally:
        try:
            con.autocommit(True)
        except pymysql.Error:
            healthy = False
        pool.release(con, healthy)


def _read(run):
    """Reads retry once after a lost connection (e.g. MySQL just restarted); a second
    failure is reported as DatabaseUnavailable."""
    try:
        return run()
    except DatabaseUnavailable as exc:
        if isinstance(exc, DatabaseBusy):
            raise
        time.sleep(0.3)
        return run()


def query_df(sql: str, params: tuple | dict | None = None) -> pd.DataFrame:
    """Run a SELECT and return the rows as a DataFrame. Always pass values via params."""
    def run():
        with cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall(), [d[0] for d in cur.description] if cur.description else []

    rows, columns = _read(run)
    df = pd.DataFrame(rows, columns=columns)
    # MySQL DECIMAL arrives as Python Decimal objects; charts and tables want floats.
    for col in df.columns:
        if df[col].map(lambda v: isinstance(v, Decimal)).any():
            df[col] = pd.to_numeric(df[col], errors="coerce").astype(float)
    return df


def query_one(sql: str, params: tuple | dict | None = None) -> dict | None:
    def run():
        with cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()

    return _read(run)


def execute(sql: str, params: tuple | dict | None = None) -> int:
    """Run an INSERT/UPDATE/DELETE and return the affected row count (never retried)."""
    with cursor() as cur:
        return cur.execute(sql, params)

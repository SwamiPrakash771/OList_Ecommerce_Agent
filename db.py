"""MySQL connection helpers. Settings come from .env.

Opening a MySQL connection costs ~400 ms here (authentication handshake), while
most queries take a few ms. So connections are kept in a small pool and reused
instead of being opened per query.

There are two pools:
  - the app pool (MYSQL_USER), used by the web app;
  - the agent pool (AGENT_MYSQL_USER), a least-privilege MySQL user for AI tools
    (see ai/db_user.py). Code inside `with db.as_agent():` runs every query on the
    agent pool, so the database itself refuses anything the agent isn't granted.
"""

import contextvars
import os
import queue
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pymysql
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

POOL_SIZE = int(os.environ.get("MYSQL_POOL_SIZE", 10))


class Pool:
    def __init__(self, user_env: str, password_env: str):
        self.user_env, self.password_env = user_env, password_env
        self._idle: queue.LifoQueue[pymysql.Connection] = queue.LifoQueue(maxsize=POOL_SIZE)

    def connect(self) -> pymysql.Connection:
        if not os.environ.get(self.user_env):
            raise RuntimeError(f"{self.user_env} is not set in .env")
        return pymysql.connect(
            host=os.environ["MYSQL_HOST"],
            port=int(os.environ.get("MYSQL_PORT", 3306)),
            user=os.environ[self.user_env],
            password=os.environ.get(self.password_env, ""),
            database=os.environ["MYSQL_DATABASE"],
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=True,
        )

    def acquire(self) -> pymysql.Connection:
        try:
            con = self._idle.get_nowait()
        except queue.Empty:
            return self.connect()
        try:
            con.ping()  # a connection MySQL closed while idle is replaced below
        except pymysql.Error:
            con.close()
            con = self.connect()
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
        con.close()


APP_POOL = Pool("MYSQL_USER", "MYSQL_PASSWORD")
AGENT_POOL = Pool("AGENT_MYSQL_USER", "AGENT_MYSQL_PASSWORD")
_current_pool: contextvars.ContextVar[Pool] = contextvars.ContextVar("db_pool", default=APP_POOL)


def connect() -> pymysql.Connection:
    """A new app-pool connection (used by setup scripts)."""
    return APP_POOL.connect()


@contextmanager
def as_agent():
    """Run every query in this block as the least-privilege agent MySQL user."""
    token = _current_pool.set(AGENT_POOL)
    try:
        yield
    finally:
        _current_pool.reset(token)


@contextmanager
def cursor():
    pool = _current_pool.get()  # the connection goes back to the pool it came from
    con = pool.acquire()
    healthy = True
    try:
        with con.cursor() as cur:
            yield cur
    except pymysql.OperationalError:
        healthy = False
        raise
    finally:
        pool.release(con, healthy)


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
    except Exception:
        try:
            con.rollback()
        except pymysql.Error:
            healthy = False
        raise
    finally:
        try:
            con.autocommit(True)
        except pymysql.Error:
            healthy = False
        pool.release(con, healthy)


def query_df(sql: str, params: tuple | dict | None = None) -> pd.DataFrame:
    """Run a SELECT and return the rows as a DataFrame. Always pass values via params."""
    with cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
        columns = [d[0] for d in cur.description] if cur.description else []
    df = pd.DataFrame(rows, columns=columns)
    # MySQL DECIMAL arrives as Python Decimal objects; charts and tables want floats.
    for col in df.columns:
        if df[col].map(lambda v: isinstance(v, Decimal)).any():
            df[col] = pd.to_numeric(df[col], errors="coerce").astype(float)
    return df


def query_one(sql: str, params: tuple | dict | None = None) -> dict | None:
    with cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def execute(sql: str, params: tuple | dict | None = None) -> int:
    """Run an INSERT/UPDATE/DELETE and return the affected row count."""
    with cursor() as cur:
        return cur.execute(sql, params)

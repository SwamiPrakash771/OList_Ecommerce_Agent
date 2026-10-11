"""MySQL connection helpers. Settings come from .env.

Opening a MySQL connection costs ~400 ms here (authentication handshake), while
most queries take a few ms. So connections are kept in a small pool and reused
instead of being opened per query.
"""

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
_pool: queue.LifoQueue[pymysql.Connection] = queue.LifoQueue(maxsize=POOL_SIZE)


def connect() -> pymysql.Connection:
    return pymysql.connect(
        host=os.environ["MYSQL_HOST"],
        port=int(os.environ.get("MYSQL_PORT", 3306)),
        user=os.environ["MYSQL_USER"],
        password=os.environ["MYSQL_PASSWORD"],
        database=os.environ["MYSQL_DATABASE"],
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
    )


def _acquire() -> pymysql.Connection:
    try:
        con = _pool.get_nowait()
    except queue.Empty:
        return connect()
    try:
        con.ping(reconnect=True)  # recovers connections MySQL closed while idle
    except pymysql.Error:
        con = connect()
    return con


def _release(con: pymysql.Connection, healthy: bool) -> None:
    if healthy and con.open:
        try:
            _pool.put_nowait(con)
            return
        except queue.Full:
            pass
    con.close()


@contextmanager
def cursor():
    con = _acquire()
    healthy = True
    try:
        with con.cursor() as cur:
            yield cur
    except pymysql.OperationalError:
        healthy = False
        raise
    finally:
        _release(con, healthy)


@contextmanager
def transaction():
    """Cursor whose statements commit together, or roll back if anything raises."""
    con = _acquire()
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
        _release(con, healthy)


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

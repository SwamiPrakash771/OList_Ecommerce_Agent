"""query_my_data: read-only SQL written by the agent, over the signed-in customer's own data.

For open questions (totals, counts, averages, grouping, date ranges) the agent writes a SELECT
instead of us adding a tool per question. The safety is in code, not in the prompt:

1. The model never sees real tables. It queries "my_*" tables that the server defines as filters
   on the signed-in customer (from the session), prepended as a WITH clause.
2. The SQL is parsed (sqlglot) before it runs: one SELECT only, only the my_* tables and the
   catalog, no WITH of its own, no variables, no locking, no INTO, no dangerous functions.
   The query that runs is the one regenerated from the parsed tree, so MySQL-only tricks such
   as executable comments (/*! ... */) are dropped rather than executed.
3. It runs on the agent database user inside a READ ONLY transaction with a time limit.

Limits never produce a wrong answer: there is no row LIMIT added to the query; if the result is
too big to send, or the query is too slow, the tool returns an error and no partial data.

The schema shown to the model is built from the data catalog (ai/metadata.yaml), and each
exposed column is checked against the catalog's customer visibility at import time.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pymysql
import sqlglot
from pydantic import Field
from sqlglot import exp

import db
from ai import metadata
from ai.clock import DATASET_NOW, DATASET_START
from ai.security import InvalidArguments, Principal, ToolError, tool

MAX_ROWS = 500
TIME_LIMIT_MS = 5000

# name -> (catalog table, exposed columns, body; the body may use %(cid)s and earlier names)
VIRTUAL_TABLES: dict[str, tuple[str, list[str], str]] = {
    "my_orders": ("orders", [
        "order_id", "order_status", "order_purchase_timestamp", "order_approved_at",
        "order_delivered_carrier_date", "order_delivered_customer_date", "order_estimated_delivery_date",
    ], "FROM orders o JOIN customers c ON c.customer_id = o.customer_id WHERE c.customer_unique_id = %(cid)s"),
    "my_order_items": ("order_items", ["order_id", "order_item_id", "product_id", "price", "freight_value"],
                       "FROM order_items o WHERE o.order_id IN (SELECT order_id FROM my_orders)"),
    "my_payments": ("payments", ["order_id", "payment_sequential", "payment_type", "payment_installments",
                                 "payment_value"],
                    "FROM payments o WHERE o.order_id IN (SELECT order_id FROM my_orders)"),
    "my_returns": ("returns", ["return_id", "order_id", "reason", "status", "requested_at", "decided_at",
                               "received_at", "decision_note"],
                   "FROM returns o WHERE o.customer_unique_id = %(cid)s"),
    "my_refunds": ("refunds", ["refund_id", "return_id", "order_id", "amount", "payment_method", "status",
                               "created_at", "processing_started_at", "completed_at", "failure_reason"],
                   "FROM refunds o WHERE o.return_id IN (SELECT return_id FROM my_returns)"),
    "catalog": ("products", ["product_id", "display_name", "category"], "FROM products o"),
}

FORBIDDEN_FUNCTIONS = {
    "sleep", "benchmark", "load_file", "get_lock", "release_lock", "release_all_locks", "is_free_lock",
    "is_used_lock", "master_pos_wait", "source_pos_wait", "user", "current_user", "currentuser",
    "session_user", "system_user", "database", "schema", "version", "connection_id", "last_insert_id",
    "row_count", "found_rows", "sys_eval", "sys_exec", "extractvalue", "updatexml",
}


def _check_catalog() -> None:
    """Every exposed column must exist in the catalog and be visible to customers."""
    for name, (table, cols, _) in VIRTUAL_TABLES.items():
        allowed = set(metadata.visible_columns(table, "customer"))
        hidden = [c for c in cols if c not in allowed]
        if hidden:
            raise RuntimeError(f"{name} exposes columns customers may not see: {hidden}")


def _with_clause() -> str:
    parts = [f"{name} AS (SELECT {', '.join('o.' + c for c in cols)} {body})"
             for name, (_, cols, body) in VIRTUAL_TABLES.items()]
    return "WITH " + ",\n".join(parts) + "\n"


def _schema_doc() -> str:
    catalog = metadata.load()["tables"]
    lines = []
    for name, (table, cols, _) in VIRTUAL_TABLES.items():
        described = []
        for c in cols:
            desc = (catalog[table]["columns"].get(c, {}).get("description") or "").split(".")[0]
            described.append(f"{c}" + (f" ({desc[:60]})" if desc and c not in ("order_id", "product_id") else ""))
        lines.append(f"- {name}: " + "; ".join(described))
    return "\n".join(lines)


_check_catalog()
WITH_SQL = _with_clause()


# ---- validation ------------------------------------------------------------------------------


def validate(sql: str) -> str:
    """Parse and check the model's SQL; return the regenerated query that will run."""
    try:
        statements = [s for s in sqlglot.parse(sql, read="mysql") if s is not None]
    except sqlglot.errors.ParseError as exc:
        raise InvalidArguments(f"The SQL could not be parsed: {str(exc).splitlines()[0]}") from None
    if len(statements) != 1:
        raise InvalidArguments("Send exactly one SELECT statement.")
    tree = statements[0]
    if not isinstance(tree, (exp.Select, exp.SetOperation)):
        raise InvalidArguments("Only SELECT queries are allowed.")
    for node in tree.walk():
        if isinstance(node, exp.With):
            raise InvalidArguments("WITH is not allowed; use subqueries instead.")
        if isinstance(node, (exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Alter,
                             exp.Command, exp.Into, exp.Lock)):
            raise InvalidArguments("Only reading is allowed.")
        if isinstance(node, (exp.Parameter, exp.SessionParameter, exp.Placeholder)):
            raise InvalidArguments("Variables and parameters are not allowed.")
        if isinstance(node, exp.Table):
            if node.args.get("db") or node.args.get("catalog") or node.name.lower() not in VIRTUAL_TABLES:
                raise InvalidArguments(f"Unknown table '{node.sql()}'. Available: {', '.join(VIRTUAL_TABLES)}.")
        if isinstance(node, exp.Func):
            fname = (node.name if isinstance(node, exp.Anonymous) else node.sql_name()).lower()
            if fname in FORBIDDEN_FUNCTIONS or isinstance(node, exp.CurrentUser):
                raise InvalidArguments(f"Function {fname.upper()} is not allowed.")
        if node.args.get("locks") or node.args.get("into"):
            raise InvalidArguments("Only reading is allowed.")
    # Comments are dropped: MySQL executes /*! ... */ "comments", so none may reach the database.
    query = tree.sql(dialect="mysql", comments=False)
    if "/*" in query:   # belt and braces: nothing comment-like may survive regeneration
        raise InvalidArguments("Comments are not allowed in the query.")
    return query


def _jsonable(v):
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M")
    if isinstance(v, date):
        return v.isoformat()
    return v


# ---- the tool --------------------------------------------------------------------------------


def query_my_data(principal: Principal, sql: str = Field(..., min_length=10, max_length=4000)) -> dict:
    query = validate(sql)
    statement = WITH_SQL + query.replace("%", "%%")   # only %(cid)s is a parameter
    try:
        with db.cursor() as cur:
            cur.execute(f"SET SESSION max_execution_time = {TIME_LIMIT_MS}")
            cur.execute("START TRANSACTION READ ONLY")
            try:
                cur.execute(statement, {"cid": principal.customer_unique_id})
                rows = cur.fetchmany(MAX_ROWS + 1)
                columns = [d[0] for d in cur.description or []]
            finally:
                cur.execute("ROLLBACK")
                cur.execute("SET SESSION max_execution_time = 0")
    except pymysql.err.MySQLError as exc:
        code, message = (exc.args + ("",))[:2]
        if code == 3024:
            raise ToolError("The query took too long and was stopped; simplify it. No result was returned.") from None
        raise ToolError(f"SQL error {code}: {message}") from None
    if len(rows) > MAX_ROWS:
        raise ToolError(f"The result has more than {MAX_ROWS} rows. Aggregate (GROUP BY, COUNT, SUM) or filter "
                        "in SQL. No partial result was returned.")
    out = {"columns": columns, "rows": [[_jsonable(r[c]) for c in columns] for r in rows],
           "row_count": len(rows), "today": date.today().isoformat()}
    if not rows:
        out["note"] = "No rows matched: the customer has no such records."
    elif any(v is None for row in out["rows"] for v in row):
        # SUM/AVG/MIN over no rows return NULL; without this the model tends to re-query.
        out["note"] = "null means there was nothing to count: a total or count over no rows is 0."
    return out


query_my_data.__doc__ = f"""Run ONE read-only MySQL 8 SELECT over the signed-in customer's own data. Use it for any
question that needs totals, counts, averages, grouping, sorting or date filtering, and do all the
arithmetic in SQL. Only these tables exist (already limited to this customer):
{_schema_doc()}
Notes: write totals as COALESCE(SUM(...), 0). Count orders with COUNT(DISTINCT o.order_id) whenever my_order_items is joined (one row per item). One query is usually enough; don't re-run a query
that already answered. An order's total = SUM(price + freight_value) from my_order_items; date an order by
order_purchase_timestamp; exclude order_status 'canceled' and 'unavailable' from spending.
Month: DATE_FORMAT(col, '%Y-%m'). Relative periods are always counted back from today, e.g. last 12
months = order_purchase_timestamp >= DATE_FORMAT(CURDATE() - INTERVAL 11 MONTH, '%Y-%m-01').
Never move or widen the period the customer asked for. For a relative period, return its bounds
as columns period_start and period_end and quote them; never compute dates yourself. Example,
spend per month in the last 12 months:
  SELECT DATE_FORMAT(o.order_purchase_timestamp, '%Y-%m') AS month,
         ROUND(SUM(i.price + i.freight_value), 2) AS spent,
         DATE_FORMAT(CURDATE() - INTERVAL 11 MONTH, '%Y-%m-01') AS period_start, CURDATE() AS period_end
  FROM my_orders o JOIN my_order_items i USING (order_id)
  WHERE o.order_status NOT IN ('canceled', 'unavailable')
    AND o.order_purchase_timestamp >= DATE_FORMAT(CURDATE() - INTERVAL 11 MONTH, '%Y-%m-01')
  GROUP BY month ORDER BY month
Periods always end today (CURDATE()), never earlier: orders after {DATASET_NOW:%Y-%m-%d} are real
orders placed in the app and must be counted ("this year" = from January 1 to today). Most older
history runs from {DATASET_START:%Y-%m} to {DATASET_NOW:%Y-%m-%d}; if a period has no orders, report
exactly that. Don't mention these dates to the customer unless they asked about a period."""

tool(roles=("customer",), reads=["orders", "customers", "order_items", "payments", "returns", "refunds",
                                  "products"])(query_my_data)

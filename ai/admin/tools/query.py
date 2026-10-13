"""query_platform_data: the admin agent's SQL fallback, for questions admin_metric can't express.

Same safety design as the other agents' SQL tools, implemented separately:
1. The model never sees real tables: it queries platform_* tables the server defines (a WITH clause).
   They hold **no masked column at all**: no customer identity, payments, buyer location, refund
   amounts or methods, and no free text (review comments, ticket descriptions, chat content).
   Figures that need those (GMV, customers, buyer states) come only from admin_metric, which
   aggregates them and hides small groups.
2. The SQL is parsed (sqlglot): one SELECT, only platform_* tables, no WITH of its own, no
   variables, locking, INTO or dangerous functions; the query that runs is regenerated from the tree.
3. It runs on the admin agent's MySQL user in a READ ONLY transaction with a time limit; results over
   MAX_ROWS are refused, never cut.
"""

from __future__ import annotations

from datetime import date

import pymysql
import sqlglot
from pydantic import Field
from sqlglot import exp

import db
from ai import metadata
from ai.admin import db_user
from ai.admin.security import InvalidArguments, Principal, ToolError, admin_tool
from ai.admin.tools.common import OPEN_WINDOW_DAYS, TIME_LIMIT_MS, value

MAX_ROWS = 200

# name -> (catalog table, columns). Every column must be admin-visible and not masked (checked below).
TABLES: dict[str, tuple[str, tuple[str, ...]]] = {
    "platform_orders": ("orders", ("order_id", "order_status", "order_purchase_timestamp", "order_approved_at",
                                   "order_delivered_carrier_date", "order_delivered_customer_date",
                                   "order_estimated_delivery_date")),
    "platform_items": ("order_items", ("order_id", "order_item_id", "product_id", "seller_id", "shipping_limit_date",
                                       "price", "freight_value")),
    "platform_products": ("products", ("product_id", "display_name", "category", "weight_g")),
    "platform_sellers": ("seller_stats", ("seller_id", "seller_city", "seller_state", "orders_fulfilled", "avg_rating",
                                          "on_time_pct")),
    "platform_reviews": ("reviews", ("review_id", "order_id", "review_score", "review_creation_date",
                                     "review_answer_timestamp")),
    "platform_returns": ("returns", ("return_id", "order_id", "reason", "status", "requested_at", "decided_at",
                                     "received_at")),
    "platform_refunds": ("refunds", ("refund_id", "return_id", "order_id", "status", "created_at",
                                     "processing_started_at", "completed_at")),
    "platform_tickets": ("support_tickets", ("ticket_id", "order_id", "issue_type", "priority", "status", "created_at",
                                             "updated_at")),
    "order_summary": ("admin_order_facts", ("order_id", "order_status", "order_purchase_timestamp", "item_count",
                                            "items_total", "shipping_total", "delivered_late", "delivery_days",
                                            "review_score", "returned", "first_order")),
    "seller_order_summary": ("admin_seller_order_facts", ("order_id", "seller_id", "seller_state", "item_count",
                                                          "items_total", "shipping_total", "order_status",
                                                          "order_purchase_timestamp", "delivered_late", "delivery_days",
                                                          "review_score", "returned", "handed_over_late")),
}
GRAINS = {"order_summary": "one row per order: its items, shipping and delivery, review and return flags",
          "seller_order_summary": "one row per order and seller: that seller's items only"}
FORBIDDEN_FUNCTIONS = {
    "sleep", "benchmark", "load_file", "get_lock", "release_lock", "release_all_locks", "is_free_lock",
    "is_used_lock", "master_pos_wait", "source_pos_wait", "user", "current_user", "currentuser",
    "session_user", "system_user", "database", "schema", "version", "connection_id", "last_insert_id",
    "row_count", "found_rows", "sys_eval", "sys_exec", "extractvalue", "updatexml",
}


def _check_catalog() -> None:
    """Every column exposed here must be one admin tools may show (admin-visible, not masked)."""
    shown = db_user.tool_columns()
    for name, (table, cols) in TABLES.items():
        bad = [c for c in cols if c not in shown.get(table, [])]
        if bad:
            raise RuntimeError(f"{name} exposes columns admin tools may not show: {bad}")


_check_catalog()
WITH_SQL = "WITH " + ",\n".join(f"{name} AS (SELECT {', '.join(cols)} FROM {table})"
                                for name, (table, cols) in TABLES.items()) + "\n"


def _schema_doc() -> str:
    cat = metadata.load()["tables"]
    lines = []
    for name, (table, cols) in TABLES.items():
        t = cat[table]
        lines.append(f"- {name} ({GRAINS.get(name) or ' '.join(str(t['grain']).split())})")
        for c in cols:
            col = t["columns"][c]
            desc = " ".join(str(col["description"]).split())
            values = col.get("allowed_values") or {}
            if values:
                desc += " Values: " + "; ".join(f"{k} = {' '.join(str(v).split())}" for k, v in values.items())
            lines.append(f"    {c}: {desc}")
    return "\n".join(lines)


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
            if node.args.get("db") or node.args.get("catalog") or node.name.lower() not in TABLES:
                raise InvalidArguments(f"Unknown table '{node.sql()}'. Available: {', '.join(TABLES)}.")
        if isinstance(node, exp.Func):
            fname = (node.name if isinstance(node, exp.Anonymous) else node.sql_name()).lower()
            if fname in FORBIDDEN_FUNCTIONS or isinstance(node, exp.CurrentUser):
                raise InvalidArguments(f"Function {fname.upper()} is not allowed.")
        if node.args.get("locks") or node.args.get("into"):
            raise InvalidArguments("Only reading is allowed.")
    query = tree.sql(dialect="mysql", comments=False)
    if "/*" in query:
        raise InvalidArguments("Comments are not allowed in the query.")
    return query


def query_platform_data(principal: Principal, sql: str = Field(..., min_length=10, max_length=4000)) -> dict:
    query = validate(sql)
    try:
        with db.cursor() as cur:
            cur.execute(f"SET SESSION max_execution_time = {TIME_LIMIT_MS}")
            cur.execute("START TRANSACTION READ ONLY")
            try:
                cur.execute(WITH_SQL + query.replace("%", "%%"))
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
    out = {"columns": columns, "rows": [[value(r[c]) for c in columns] for r in rows], "row_count": len(rows),
           "today": date.today().isoformat()}
    if not rows:
        out["note"] = "No rows matched."
    return out


query_platform_data.__doc__ = f"""Fallback: run ONE read-only MySQL 8 SELECT over platform data, only for questions
admin_metric and the other tools can't answer (unusual filters or combinations). Do all arithmetic in SQL and
aggregate: at most {MAX_ROWS} rows come back. Only these tables exist:
{_schema_doc()}
Not available here (by design): customer identity, payments/GMV, buyer location, refund amounts and methods,
review and ticket text, chat content. For GMV, customers or buyer states use admin_metric.
Definitions: item revenue = SUM(price) / SUM(items_total) of orders whose order_status is not canceled or
unavailable (no shipping); late delivery = delivered and DATE(delivered) > DATE(estimated) (delivered_late);
open order = not delivered, canceled or unavailable and bought in the last {OPEN_WINDOW_DAYS} days. Orders are
dated by order_purchase_timestamp. Month: DATE_FORMAT(col, '%Y-%m'). Return relative periods' bounds as
columns period_start and period_end and quote them. One query is usually enough."""

admin_tool(reads=[t for t, _ in TABLES.values()])(query_platform_data)

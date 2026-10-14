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

import re
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

# Order-level tables built from the ones above, so the simple query is the right one: averages
# "per order" come from one row per order, not from item rows (AVG over items = per item).
# name -> (columns with their description, body)
DERIVED_TABLES: dict[str, tuple[dict[str, str], str]] = {
    "my_order_totals": ({
        "order_id": "",
        "order_status": "",
        "order_purchase_timestamp": "",
        "item_count": "number of items in the order",
        "items_total": "item prices, without shipping",
        "shipping_total": "shipping (freight)",
        "order_total": "items + shipping: what the order cost",
    }, """SELECT o.order_id, o.order_status, o.order_purchase_timestamp, COUNT(i.order_id) AS item_count,
       COALESCE(SUM(i.price), 0) AS items_total, COALESCE(SUM(i.freight_value), 0) AS shipping_total,
       COALESCE(SUM(i.price + i.freight_value), 0) AS order_total
FROM my_orders o LEFT JOIN my_order_items i ON i.order_id = o.order_id
GROUP BY o.order_id, o.order_status, o.order_purchase_timestamp"""),
}
ALL_TABLES = [*VIRTUAL_TABLES, *DERIVED_TABLES]

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
    parts += [f"{name} AS ({body})" for name, (_, body) in DERIVED_TABLES.items()]
    return "WITH " + ",\n".join(parts) + "\n"


# Catalog notes meant for developers, never for the agent: how demo data was made, code files,
# columns and tables the agent can't query.
_INTERNAL_NOTE = re.compile(r"simulat|\bai/\w+\.py|\bsource = |category_translation|order_addresses|tools must", re.I)


def _text(value) -> str:
    """A catalog text as the agent sees it: one line, without developer-only sentences or
    parenthesised code references."""
    text = " ".join(str(value or "").split())
    text = re.sub(r"\s*\((?:[^()]*\bai/\w+\.py[^()]*)\)", "", text)        # "(ai/policies.py)"
    text = re.sub(r";?\s*see category_translation", "", text)
    text = re.sub(r"\s*\([^()]*moved forward[^()]*\)", "", text)                # how the dates were prepared
    sentences = re.split(r"(?<=[.;])\s+", text)
    return " ".join(s for s in sentences if not _INTERNAL_NOTE.search(s)).strip().rstrip(".;")


def _schema_doc() -> str:
    """The tables as the agent sees them, generated from the data catalog (ai/metadata.yaml): for
    each table its grain, description and links to the other tables; for each column its full
    description and the meaning of every allowed value."""
    catalog = metadata.load()["tables"]
    exposed = {table: name for name, (table, _, _) in VIRTUAL_TABLES.items()}   # orders -> my_orders
    lines = []
    for name, (table, cols, _) in VIRTUAL_TABLES.items():
        t = catalog[table]
        links = []
        for rel in t.get("relationships") or []:   # "order_id -> orders.order_id"
            col, target = [x.strip() for x in str(rel).split("->")]
            ttable, tcol = target.split(".")
            if col in cols and ttable in exposed:
                links.append(f"{col} -> {exposed[ttable]}.{tcol}")
        lines.append(f"- {name} ({_text(t.get('grain'))}): {_text(t.get('description'))}."
                     + (f" Links: {', '.join(links)}." if links else ""))
        for c in cols:
            col = t["columns"].get(c, {})
            desc = _text(col.get("description"))
            values = col.get("allowed_values") or {}
            if values:
                desc += ". Values: " + "; ".join(f"{k} = {_text(v)}" for k, v in values.items())
            lines.append(f"    {c}: {desc}")
    for name, (cols, _) in DERIVED_TABLES.items():
        lines.append(f"- {name} (one row per order): built from my_orders and my_order_items. Links: order_id -> my_orders.order_id.")
        lines += [f"    {c}: {d}" for c, d in cols.items() if d]
    return "\n".join(lines)


def _context_doc() -> str:
    """Glossary terms and data quirks that matter for the customer's own data (metadata.yaml
    `customer_context`)."""
    cat = metadata.load()
    chosen = cat.get("customer_context") or {}
    terms = [f"- {k}: {_text(cat['glossary'][k])}" for k in chosen.get("glossary", []) if k in cat["glossary"]]
    quirks = [f"- {_text(cat['quirks'][i])}" for i in chosen.get("quirks", []) if i < len(cat["quirks"])]
    return "\n".join(["Terms:", *terms, "Data notes:", *quirks])


def _metrics_doc() -> str:
    """Business definitions from the data catalog (metadata.yaml `metrics`)."""
    return "\n".join(f"- {k}: {' '.join(str(v).split())}" for k, v in (metadata.load().get("metrics") or {}).items())


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
            if node.args.get("db") or node.args.get("catalog") or node.name.lower() not in ALL_TABLES:
                raise InvalidArguments(f"Unknown table '{node.sql()}'. Available: {', '.join(ALL_TABLES)}.")
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


def data_section() -> str:
    """The customer's slice of the data catalog, for the system prompt ("Your data"): only the tables and
    columns a customer may query, already limited to the signed-in customer."""
    return f"""The data query reads only these tables, already limited to the signed-in customer:
{_schema_doc()}
Definitions:
{_metrics_doc()}
{_context_doc()}
"""


query_my_data.__doc__ = f"""Run ONE read-only MySQL 8 SELECT over the signed-in customer's own data. Use it for any
question that needs totals, counts, averages, grouping, sorting or date filtering, and do all the
arithmetic in SQL. Only the tables listed under "Your data" in your instructions exist (already limited
to this customer); use the definitions given there.
Notes: write totals as COALESCE(SUM(...), 0). One query is usually enough; don't re-run a query that
already answered.
Month: DATE_FORMAT(col, '%Y-%m'). Relative periods are always counted back from today, e.g. last 12
months = order_purchase_timestamp >= DATE_FORMAT(CURDATE() - INTERVAL 11 MONTH, '%Y-%m-01').
Never move or widen the period the customer asked for. For a relative period, return its bounds
as columns period_start and period_end and quote them; never compute dates yourself. Example,
spend per month in the last 12 months:
  SELECT DATE_FORMAT(t.order_purchase_timestamp, '%Y-%m') AS month,
         ROUND(SUM(t.order_total), 2) AS spent,
         DATE_FORMAT(CURDATE() - INTERVAL 11 MONTH, '%Y-%m-01') AS period_start, CURDATE() AS period_end
  FROM my_order_totals t
  WHERE t.order_status NOT IN ('canceled', 'unavailable')
    AND t.order_purchase_timestamp >= DATE_FORMAT(CURDATE() - INTERVAL 11 MONTH, '%Y-%m-01')
  GROUP BY month ORDER BY month
Periods always end today (CURDATE()), never earlier: orders after {DATASET_NOW:%Y-%m-%d} are real
orders placed in the app and must be counted ("this year" = from January 1 to today). Most older
history runs from {DATASET_START:%Y-%m} to {DATASET_NOW:%Y-%m-%d}; if a period has no orders, report
exactly that. Don't mention these dates to the customer unless they asked about a period."""

tool(roles=("customer",), reads=["orders", "customers", "order_items", "payments", "returns", "refunds",
                                  "products"])(query_my_data)

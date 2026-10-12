"""Systematic permission tests for every registered agent tool (docs/AI_PLAN.md section 13).

1. Coverage gate     every tool must be classified in TOOL_PLAN, so a new tool can't ship untested
2. Role matrix       every tool x every role: only the declared roles get through the dispatcher
3. Cross-customer    sampled real customers try each other's orders, returns, refunds and lists
4. Honest tables     each tool runs as a MySQL user granted ONLY its declared tables
5. Injection         hostile strings in ids and search text never cause database errors

These must pass 100% before any agent is connected to a model.
"""

import os
import random
import secrets

import pymysql
import pytest

import ai.tools  # noqa: F401  (registers the tools)
import db
from ai import metadata
from ai.security import (
    REGISTRY, InvalidArguments, NotFound, Principal, ToolError, ToolKind, call_tool, preview_tool,
)

ROLES = ("guest", "customer", "seller", "admin")
RNG = random.Random(20181017)

# How each tool is permission-tested. Every registered tool must appear here.
#   order      takes order_id; must be NotFound for other customers' orders
#   own_list   no order id; returns only the caller's rows
#   public     catalog data, same for every caller
#   own_write  changes only the caller's own rows (cart / address)
TOOL_PLAN = {
    "search_products": "public",
    "product_details": "public",
    "list_categories": "public",
    "my_orders": "own_list",
    "query_my_data": "own_list",
    "track_order": "order",
    "get_return_status": "order",
    "get_refund_status": "order",
    "request_return": "order",
    "my_cart": "own_list",
    "cart_add": "own_write",
    "cart_set_quantity": "own_write",
    "get_address": "own_list",
    "update_address": "own_write",
    "open_ticket": "order",
    "transfer_to_human": "order",
    "my_tickets": "own_list",
}

PRODUCT = "422879e10f46682990de24d770e7f83d"
FAKE = Principal(role="customer", user_id=9_200_001, username="probe", customer_unique_id="e" * 32)


def principal_for(role: str, customer: str | None = None) -> Principal:
    if role == "customer":
        return Principal(role="customer", user_id=9_200_000, username="c", customer_unique_id=customer or FAKE.customer_unique_id)
    if role == "seller":
        return Principal(role="seller", user_id=9_200_002, username="s", seller_id="6560211a19b47992c3666cc44a7e94c0")
    if role == "admin":
        return Principal(role="admin", user_id=9_200_003, username="a")
    return Principal.guest()


def minimal_args(name: str, order_id: str = "0" * 32) -> dict:
    """Valid-looking arguments for each tool (used for role and table checks)."""
    return {
        "search_products": {"query": "watch"},
        "product_details": {"product_id": PRODUCT},
        "list_categories": {},
        "my_orders": {},
        "query_my_data": {"sql": "SELECT o.order_id, i.price, p.amount, r.status, c.category, y.payment_value "
                                 "FROM my_orders o LEFT JOIN my_order_items i USING (order_id) "
                                 "LEFT JOIN catalog c USING (product_id) LEFT JOIN my_refunds p USING (order_id) "
                                 "LEFT JOIN my_returns r USING (order_id) LEFT JOIN my_payments y USING (order_id)"},
        "track_order": {"order_id": order_id},
        "get_return_status": {"order_id": order_id},
        "get_refund_status": {"order_id": order_id},
        "request_return": {"order_id": order_id, "reason": "damaged"},
        "my_cart": {},
        "cart_add": {"product_id": PRODUCT, "quantity": 1},
        "cart_set_quantity": {"product_id": PRODUCT, "quantity": 0},
        "get_address": {},
        "update_address": {"recipient_name": "Probe User", "street": "Rua A", "number": "1", "city": "Recife",
                           "state": "PE", "zip_code": "50000-000"},
        "open_ticket": {"category": "other", "description": "permission probe ticket", "order_id": order_id},
        "transfer_to_human": {"reason": "probe", "summary": "Permission probe summary text.", "order_id": order_id},
        "my_tickets": {},
    }[name]


@pytest.fixture(scope="module")
def customers() -> list[dict]:
    """30 sampled customers with orders, including some with returns and refunds."""
    with_refunds = db.query_df(
        "SELECT DISTINCT r.customer_unique_id AS cid FROM returns r JOIN refunds f USING (return_id) LIMIT 400"
    ).cid.tolist()
    with_orders = db.query_df(
        "SELECT customer_unique_id AS cid FROM customers GROUP BY customer_unique_id LIMIT 3000"
    ).cid.tolist()
    sample = RNG.sample(with_refunds, 15) + RNG.sample(with_orders, 15)
    out = []
    for cid in sample:
        orders = db.query_df(
            "SELECT o.order_id FROM orders o JOIN customers c USING (customer_id) WHERE c.customer_unique_id = %s",
            (cid,),
        ).order_id.tolist()
        if orders:
            out.append({"cid": cid, "orders": orders})
    return out


@pytest.fixture(autouse=True)
def cleanup():
    started = db.query_one("SELECT NOW() AS t")["t"]
    yield
    db.execute("DELETE FROM cart_items WHERE customer_unique_id = %s", (FAKE.customer_unique_id,))
    db.execute("DELETE FROM customer_addresses WHERE customer_unique_id = %s", (FAKE.customer_unique_id,))
    db.execute("DELETE FROM support_tickets WHERE source = 'ai_agent' AND created_at >= %s", (started,))
    db.execute("DELETE FROM returns WHERE source = 'app' AND requested_at >= %s", (started,))


# ---- 1. coverage gate --------------------------------------------------------------------------


def test_every_tool_has_a_permission_plan():
    missing = set(REGISTRY) - set(TOOL_PLAN)
    stale = set(TOOL_PLAN) - set(REGISTRY)
    assert not missing, f"new tools without a permission-test plan: {sorted(missing)}"
    assert not stale, f"plan lists tools that no longer exist: {sorted(stale)}"


def test_customer_tools_never_take_identity_and_order_tools_take_order_id():
    import inspect
    for name, plan in TOOL_PLAN.items():
        params = set(inspect.signature(REGISTRY[name].func).parameters) - {"principal"}
        if plan == "order":
            assert "order_id" in params, name
        assert not params & {"customer_unique_id", "seller_id", "user_id", "customer_id"}, name


# ---- 2. role matrix ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(TOOL_PLAN))
@pytest.mark.parametrize("role", ROLES)
def test_role_matrix(name, role):
    spec = REGISTRY[name]
    p = principal_for(role)
    args = minimal_args(name)
    if role not in spec.roles:
        with pytest.raises(ToolError, match="not available"):
            call_tool(p, name, args, confirmed=True)
        if spec.kind is ToolKind.WRITE_CONFIRM:
            with pytest.raises(ToolError, match="not available"):
                preview_tool(p, name, args)
    else:
        # Allowed roles reach the tool (it may still say "not found" for the dummy order id).
        try:
            call_tool(p, name, args, confirmed=True)
        except NotFound:
            assert TOOL_PLAN[name] == "order"


# ---- 3. cross-customer sweep -----------------------------------------------------------------------


def test_sample_is_meaningful(customers):
    assert len(customers) >= 25
    assert sum(1 for c in customers if len(c["orders"]) > 1) >= 1


@pytest.mark.parametrize("name", [n for n, p in TOOL_PLAN.items() if p == "order"])
def test_customers_cannot_reach_each_others_orders(customers, name):
    spec = REGISTRY[name]
    checked = 0
    for i, victim in enumerate(customers):
        attacker = customers[(i + 7) % len(customers)]
        if attacker["cid"] == victim["cid"]:
            continue
        p = principal_for("customer", attacker["cid"])
        for order_id in victim["orders"][:2]:
            if order_id in attacker["orders"]:
                continue
            args = minimal_args(name, order_id)
            with pytest.raises(NotFound):
                if spec.kind is ToolKind.WRITE_CONFIRM:
                    preview_tool(p, name, args)
                else:
                    call_tool(p, name, args)
            if spec.kind is ToolKind.WRITE_CONFIRM:
                with pytest.raises(NotFound):
                    call_tool(p, name, args, confirmed=True)
            checked += 1
    assert checked >= 25


@pytest.mark.parametrize("name", ["track_order", "get_return_status", "get_refund_status"])
def test_owners_can_read_their_own_orders(customers, name):
    for c in customers[:10]:
        out = call_tool(principal_for("customer", c["cid"]), name, {"order_id": c["orders"][0]})
        assert out["order_id"] == c["orders"][0]


def _values(value) -> set[str]:
    if isinstance(value, dict):
        return set().union(*(_values(v) for v in value.values())) if value else set()
    if isinstance(value, list):
        return set().union(*(_values(v) for v in value)) if value else set()
    return {value} if isinstance(value, str) else set()


def test_list_tools_only_show_the_callers_rows(customers):
    for i, me in enumerate(customers):
        other = customers[(i + 3) % len(customers)]
        mine = call_tool(principal_for("customer", me["cid"]), "my_orders", {"limit": 20})
        ids = {o["order_id"] for o in mine["orders"]}
        assert ids <= set(me["orders"])
        assert not ids & (set(other["orders"]) - set(me["orders"]))
        everything = _values(mine) | _values(call_tool(principal_for("customer", me["cid"]), "my_tickets"))
        if other["cid"] != me["cid"]:
            assert other["cid"] not in everything


# ---- 4. tools only touch the tables they declare ---------------------------------------------------


@pytest.fixture(scope="module")
def probe_user():
    """A throwaway MySQL user whose grants are reset per tool."""
    user, password = "olist_probe", secrets.token_urlsafe(18)
    for host in ("localhost", "127.0.0.1"):
        db.execute(f"CREATE USER IF NOT EXISTS '{user}'@'{host}' IDENTIFIED BY %s", (password,))
        db.execute(f"ALTER USER '{user}'@'{host}' IDENTIFIED BY %s", (password,))
    os.environ["PROBE_MYSQL_USER"], os.environ["PROBE_MYSQL_PASSWORD"] = user, password
    yield user
    for host in ("localhost", "127.0.0.1"):
        db.execute(f"DROP USER IF EXISTS '{user}'@'{host}'")


def _grant_exactly(user: str, spec) -> None:
    schema = os.environ["MYSQL_DATABASE"]
    from ai.db_user import AI_TABLE_GRANTS, WRITE_GRANTS
    for host in ("localhost", "127.0.0.1"):
        account = f"'{user}'@'{host}'"
        db.execute(f"REVOKE ALL PRIVILEGES, GRANT OPTION FROM {account}")
        joins = getattr(spec, "joins", ())
        for table in set(spec.reads) | set(joins):
            db.execute(f"GRANT SELECT ON `{schema}`.`{table}` TO {account}")
        for table in spec.writes:
            privs = set(WRITE_GRANTS.get(table, ())) | (set(AI_TABLE_GRANTS.get(table, ())) - {"SELECT"})
            db.execute(f"GRANT SELECT, {', '.join(sorted(privs))} ON `{schema}`.`{table}` TO {account}")


@pytest.mark.parametrize("name", sorted(TOOL_PLAN))
def test_tool_touches_only_declared_tables(probe_user, name, monkeypatch):
    spec = REGISTRY[name]
    _grant_exactly(probe_user, spec)
    probe_pool = db.Pool("PROBE_MYSQL_USER", "PROBE_MYSQL_PASSWORD")
    monkeypatch.setattr(db, "AGENT_POOL", probe_pool)  # call_tool -> db.as_agent() -> probe user

    owner = db.query_one(
        "SELECT c.customer_unique_id AS cid, r.order_id FROM refunds f JOIN returns r USING (return_id) "
        "JOIN orders o ON o.order_id = r.order_id JOIN customers c USING (customer_id) LIMIT 1"
    )
    role = sorted(spec.roles)[-1] if "customer" not in spec.roles else "customer"
    cid = FAKE.customer_unique_id if TOOL_PLAN[name] == "own_write" else owner["cid"]
    p = principal_for(role, cid)
    try:
        call_tool(p, name, minimal_args(name, owner["order_id"]), confirmed=True)
    except pymysql.MySQLError as exc:
        pytest.fail(f"{name} touched a table it did not declare: {exc}")
    except ToolError:
        pass  # business-rule refusals (e.g. return window) are fine; no undeclared access happened
    finally:
        for c in list(probe_pool._idle.queue):
            c.close()


# ---- 5. injection probes ---------------------------------------------------------------------------

HOSTILE_IDS = [
    "' OR '1'='1' -- " + "x" * 16,
    "x' UNION SELECT password_hash FR",
    "0" * 31 + "'",
    "%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%",
    "\\' ; DROP TABLE orders; --  xxxx",
]
HOSTILE_TEXT = ["' OR 1=1 --", "watch'; DROP TABLE products; --", "%", "_", "\\", "\" OR \"\"=\"", "ñandú 🐢"]


@pytest.mark.parametrize("bad_id", HOSTILE_IDS)
@pytest.mark.parametrize("name", ["track_order", "get_return_status", "get_refund_status"])
def test_hostile_order_ids_are_harmless(name, bad_id):
    assert len(bad_id) == 32
    with pytest.raises((NotFound, InvalidArguments)):
        call_tool(principal_for("customer", "8d50f5eadf50201ccdcedfb9e2ac8455"), name, {"order_id": bad_id})


@pytest.mark.parametrize("text", HOSTILE_TEXT)
def test_hostile_search_text_is_harmless(text):
    out = call_tool(Principal.guest(), "search_products", {"query": text})
    assert isinstance(out["products"], list)
    out = call_tool(Principal.guest(), "search_products", {"category": text})
    assert out["total_matches"] == 0
    assert db.query_one("SELECT COUNT(*) AS n FROM orders")["n"] > 0  # nothing was dropped


def test_catalog_still_matches_after_probes():
    assert metadata.validate(metadata.live_schema()) == []


# ---- query_my_data: model-written SQL --------------------------------------------------------

HOSTILE_SQL = [
    "SELECT * FROM orders",                                              # real table
    "SELECT username, password_hash FROM app_users",                     # secret table
    "SELECT * FROM information_schema.tables",                           # schema discovery
    "SELECT * FROM olist.customers",                                     # qualified name
    "SELECT (SELECT COUNT(*) FROM customers) AS n",                      # real table in a subquery
    "SELECT order_id FROM my_orders UNION SELECT customer_unique_id FROM customers",
    "WITH my_orders AS (SELECT * FROM orders) SELECT * FROM my_orders",  # shadowing the safe table
    "SELECT * FROM my_orders; DROP TABLE orders",                        # second statement
    "UPDATE cart_items SET quantity = 0",
    "DELETE FROM my_orders",
    "SELECT * FROM my_orders FOR UPDATE",
    "SELECT * FROM my_orders INTO OUTFILE '/tmp/x'",
    "SELECT SLEEP(10)",
    "SELECT BENCHMARK(100000000, MD5('x'))",
    "SELECT LOAD_FILE('/etc/passwd')",
    "SELECT @@version",
    "SELECT CURRENT_USER()",
]


@pytest.mark.parametrize("sql", HOSTILE_SQL)
def test_hostile_sql_is_refused_before_reaching_the_database(sql):
    with pytest.raises(InvalidArguments):
        call_tool(principal_for("customer", "8d50f5eadf50201ccdcedfb9e2ac8455"), "query_my_data", {"sql": sql})
    assert db.query_one("SELECT COUNT(*) AS n FROM orders")["n"] > 0


def test_mysql_executable_comment_is_stripped_not_run():
    """MySQL runs /*! ... */ 'comments'; the query that reaches the database must not contain them."""
    p = principal_for("customer", "8d50f5eadf50201ccdcedfb9e2ac8455")
    sql = "SELECT order_id FROM my_orders WHERE 1=1 /*!50000 UNION SELECT customer_unique_id FROM customers */"
    out = call_tool(p, "query_my_data", {"sql": sql})
    own = set(db.query_df("SELECT o.order_id FROM orders o JOIN customers c USING (customer_id) "
                          "WHERE c.customer_unique_id = %s", (p.customer_unique_id,)).order_id)
    assert {r[0] for r in out["rows"]} == own


def test_query_my_data_only_ever_sees_the_callers_rows(customers):
    """Cross-customer sweep: every my_* table returns exactly the caller's own records."""
    for c in customers:
        p = principal_for("customer", c["cid"])
        out = call_tool(p, "query_my_data", {"sql": "SELECT DISTINCT order_id FROM my_orders "
                                                     "UNION SELECT order_id FROM my_order_items "
                                                     "UNION SELECT order_id FROM my_payments "
                                                     "UNION SELECT order_id FROM my_returns "
                                                     "UNION SELECT order_id FROM my_refunds"})
        assert {r[0] for r in out["rows"]} <= set(c["orders"]), c["cid"]
        assert {r[0] for r in call_tool(p, "query_my_data", {"sql": "SELECT order_id FROM my_orders"})["rows"]} \
            == set(c["orders"])


def test_big_results_fail_loudly_instead_of_being_cut():
    """No silent truncation: a result over the row cap is an error, never a partial (wrong) answer."""
    p = principal_for("customer", "8d50f5eadf50201ccdcedfb9e2ac8455")
    with pytest.raises(ToolError, match="No partial result"):
        call_tool(p, "query_my_data", {"sql": "SELECT product_id FROM catalog"})
    total = call_tool(p, "query_my_data", {"sql": "SELECT COUNT(*) FROM catalog"})["rows"][0][0]
    assert total == db.query_one("SELECT COUNT(*) AS n FROM products")["n"]   # aggregates see everything


def test_sql_errors_come_back_to_the_model():
    p = principal_for("customer", "8d50f5eadf50201ccdcedfb9e2ac8455")
    with pytest.raises(ToolError, match="SQL error"):
        call_tool(p, "query_my_data", {"sql": "SELECT no_such_column FROM my_orders"})


def test_monthly_spending_query_matches_known_totals():
    p = principal_for("customer", "8d50f5eadf50201ccdcedfb9e2ac8455")
    sql = ("SELECT DATE_FORMAT(o.order_purchase_timestamp, '%Y-%m') AS month, "
           "ROUND(SUM(i.price + i.freight_value), 2) AS spent FROM my_orders o JOIN my_order_items i USING (order_id) "
           "WHERE o.order_status NOT IN ('canceled', 'unavailable') AND YEAR(o.order_purchase_timestamp) = 2026 "
           "GROUP BY month ORDER BY month")
    rows = call_tool(p, "query_my_data", {"sql": sql})["rows"]
    expected = [[r["month"], float(r["spent"])] for r in db.query_df(
        "SELECT DATE_FORMAT(o.order_purchase_timestamp, '%%Y-%%m') AS month, ROUND(SUM(i.price + i.freight_value), 2) AS spent "
        "FROM orders o JOIN customers c USING (customer_id) JOIN order_items i USING (order_id) "
        "WHERE c.customer_unique_id = %s AND o.order_status NOT IN ('canceled', 'unavailable') "
        "AND YEAR(o.order_purchase_timestamp) = 2026 GROUP BY month ORDER BY month", (p.customer_unique_id,)).to_dict("records")]
    assert rows == expected and rows

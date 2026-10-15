"""Seller agent, step 6: permission tests. A seller can never reach another seller's data, or any
buyer's identity, through any seller tool or argument (docs/seller/SELLER_AGENT.md).

Mirrors tests/test_permissions.py for the customer agent:
- a 30-seller cross-check: every ID a tool returns belongs to that seller;
- cross-access with other sellers' order and product IDs;
- the role matrix (customers, guests, admins get nothing);
- hostile arguments;
- each tool touches only the tables it declares (run on a throwaway MySQL user granted exactly
  those tables' seller columns).
"""

import json
import os
import re
import secrets

import pymysql
import pytest

import ai.seller.tools  # noqa: F401  (registers the seller tools)
import db
from ai.security import Principal, ToolError
from ai.seller import db_user, simulate_orders
from ai.seller.security import SELLER_REGISTRY, call_seller_tool, preview_seller_tool

ID = re.compile(r"\b[0-9a-f]{32}\b")
SHOP_VIEWS = ["overview", "orders_to_ship", "reviews", "returns"]   # my_shop's lists
HOSTILE_IDS = ["' OR '1'='1", "x" * 32, "../../etc/passwd", "6560211a19b47992c3666cc44a7e94c0; DROP TABLE orders",
               "%", "6560211A19B47992C3666CC44A7E94C0"]


def seller(seller_id: str, n: int = 0) -> Principal:
    return Principal(role="seller", user_id=9_100_100 + n, username=f"probe-{n}", seller_id=seller_id)


@pytest.fixture(scope="module", autouse=True)
def scenario_orders():
    simulate_orders.create()
    yield


@pytest.fixture(scope="module")
def sellers() -> list[dict]:
    """30 sellers of every size (deterministic sample) plus the two demo sellers, with what they own."""
    ids = db.query_df(
        "SELECT seller_id FROM (SELECT DISTINCT seller_id FROM order_items) s ORDER BY MD5(seller_id) LIMIT 30"
    ).seller_id.tolist()
    ids += [simulate_orders.SELLER_A, "4a3ca9315b744ce9f8e9374361493884"]
    out = []
    for sid in dict.fromkeys(ids):
        orders = set(db.query_df("SELECT DISTINCT order_id FROM order_items WHERE seller_id = %s", (sid,)).order_id)
        products = set(db.query_df("SELECT DISTINCT product_id FROM order_items WHERE seller_id = %s", (sid,)).product_id)
        products |= set(db.query_df("SELECT product_id FROM product_stats WHERE main_seller_id = %s", (sid,)).product_id)
        marks = ", ".join(["%s"] * len(orders)) or "''"
        returns = set(db.query_df(f"SELECT return_id FROM returns WHERE order_id IN ({marks})", tuple(orders)).return_id)
        refunds = set(db.query_df(f"SELECT refund_id FROM refunds WHERE order_id IN ({marks})", tuple(orders)).refund_id)
        own_product = db.query_one("SELECT product_id FROM product_stats WHERE main_seller_id = %s LIMIT 1", (sid,))
        out.append({"id": sid, "orders": orders, "products": products, "owned": orders | products | returns | refunds,
                    "own_product": own_product["product_id"] if own_product else None})
    return out


def _ids_in(result) -> set[str]:
    return set(ID.findall(json.dumps(result, default=str)))


def test_sample_is_meaningful(sellers):
    sizes = sorted(len(s["orders"]) for s in sellers)
    assert len(sellers) >= 30 and sizes[0] <= 3 and sizes[-1] > 1000   # tiny sellers and the largest


# ---- 1. every ID a tool returns belongs to the caller -------------------------------------------


@pytest.mark.parametrize("view", SHOP_VIEWS)
def test_shop_views_only_return_the_callers_records(sellers, view):
    for n, s in enumerate(sellers):
        found = _ids_in(call_seller_tool(seller(s["id"], n), "my_shop", {"view": view}))
        assert found <= s["owned"], f"{view} for {s['id']} returned foreign IDs: {sorted(found - s['owned'])[:3]}"


def test_order_details_of_own_orders_show_only_own_records(sellers):
    for n, s in enumerate(sellers):
        order_id = sorted(s["orders"])[0]
        found = _ids_in(call_seller_tool(seller(s["id"], n), "look_up", {"kind": "order", "record_id": order_id}))
        assert found <= s["owned"], s["id"]


# ---- 2. other sellers' orders and products are "not found" --------------------------------------


@pytest.mark.parametrize("kind", ["order", "product"])
def test_sellers_cannot_reach_each_others_records(sellers, kind):
    for n, s in enumerate(sellers):
        other = sellers[(n + 1) % len(sellers)]
        pool = sorted(other["orders"] - s["orders"]) if kind == "order" else sorted(
            {other["own_product"]} - s["products"] - {None})
        if not pool:
            continue
        with pytest.raises(ToolError, match="No (order|product) found"):
            call_seller_tool(seller(s["id"], n), "look_up", {"kind": kind, "record_id": pool[0]})


def test_sellers_cannot_edit_or_preview_each_others_products(sellers):
    for n, s in enumerate(sellers):
        other = sellers[(n + 1) % len(sellers)]["own_product"]
        if other and other not in s["products"]:
            with pytest.raises(ToolError, match="No product found"):
                preview_seller_tool(seller(s["id"], n), "update_listing", {"product_id": other, "price": 1.0})


def test_reviews_of_another_sellers_product_are_empty(sellers):
    a, b = sellers[-2], sellers[-1]   # the two demo sellers
    out = call_seller_tool(seller(a["id"]), "my_shop", {"view": "reviews", "product_id": b["own_product"], "with_comment_only": False})
    assert out["matching_reviews"] == 0 and out["reviews"] == []


# ---- 3. role matrix ------------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(SELLER_REGISTRY))
@pytest.mark.parametrize("who", [
    Principal(role="customer", user_id=1, username="c", customer_unique_id="8d50f5eadf50201ccdcedfb9e2ac8455"),
    Principal.guest(),
    Principal(role="admin", user_id=2, username="admin"),
])
def test_only_sellers_can_use_seller_tools(name, who):
    with pytest.raises(ToolError, match="not available"):
        call_seller_tool(who, name, {})


# ---- 4. hostile arguments ------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(SELLER_REGISTRY))
@pytest.mark.parametrize("smuggled", ["seller_id", "customer_id", "customer_unique_id", "user_id"])
def test_identity_can_never_be_passed_to_a_seller_tool(name, smuggled):
    # confirmed=True so a change tool reaches the identity check (unconfirmed, it is refused even earlier)
    with pytest.raises(ToolError, match="Identity"):
        call_seller_tool(seller(simulate_orders.SELLER_A), name, {smuggled: "4a3ca9315b744ce9f8e9374361493884"},
                         confirmed=True)


@pytest.mark.parametrize("bad_id", HOSTILE_IDS)
@pytest.mark.parametrize("kind", ["order", "product"])
def test_hostile_ids_are_refused_before_any_query(kind, bad_id):
    with pytest.raises(ToolError):
        call_seller_tool(seller(simulate_orders.SELLER_A), "look_up", {"kind": kind, "record_id": bad_id})


# ---- 5. each tool touches only the tables it declares -------------------------------------------


@pytest.fixture(scope="module")
def probe_user():
    user, password = "olist_seller_probe", secrets.token_urlsafe(18)   # secret-check: ignore (the literal is the user name)
    for host in ("localhost", "127.0.0.1"):
        db.execute(f"CREATE USER IF NOT EXISTS '{user}'@'{host}' IDENTIFIED BY %s", (password,))
        db.execute(f"ALTER USER '{user}'@'{host}' IDENTIFIED BY %s", (password,))
    os.environ["SELLER_PROBE_MYSQL_USER"], os.environ["SELLER_PROBE_MYSQL_PASSWORD"] = user, password
    yield user
    for host in ("localhost", "127.0.0.1"):
        db.execute(f"DROP USER IF EXISTS '{user}'@'{host}'")


def _grant_exactly(user: str, spec) -> None:
    """The seller columns of the declared tables only (plus join keys), and the declared writes."""
    schema, readable = os.environ["MYSQL_DATABASE"], db_user.readable_columns()
    for host in ("localhost", "127.0.0.1"):
        account = f"'{user}'@'{host}'"
        db.execute(f"REVOKE ALL PRIVILEGES, GRANT OPTION FROM {account}")
        for table in spec.reads:
            cols = ", ".join(f"`{c}`" for c in readable[table])
            db.execute(f"GRANT SELECT ({cols}) ON `{schema}`.`{table}` TO {account}")
        for table in spec.writes:
            db.execute(f"GRANT {', '.join(db_user.WRITE_GRANTS[table])} ON `{schema}`.`{table}` TO {account}")


# Every path of a tool is probed: each my_shop view and each look_up kind needs only the declared tables.
PROBE_ARGS = {
    "my_shop": lambda: [{}, {"view": "orders_to_ship"}, {"view": "reviews", "product_id": _top_product(), "max_score": 3},
                        {"view": "returns", "status": "received"}],
    "look_up": lambda: [{"kind": "order", "record_id": simulate_orders.ORDER_IDS[1]},
                        {"kind": "product", "record_id": _top_product()}],
    "seller_metric": lambda: {"metric": "avg_review_score", "group_by": ["product"], "period": "2026", "top_n": 3},
    # one query that reads every table the SQL tool exposes
    "query_seller_data": lambda: {"sql": "SELECT (SELECT COUNT(*) FROM my_sales) a, (SELECT COUNT(*) FROM my_orders) b, "
                                         "(SELECT COUNT(*) FROM my_products) c, (SELECT COUNT(*) FROM my_reviews) d, "
                                         "(SELECT COUNT(*) FROM my_returns) e, (SELECT COUNT(*) FROM my_refunds) f, "
                                         "(SELECT COUNT(*) FROM my_order_totals) g"},
}


def _top_product() -> str:
    return db.query_one("SELECT product_id FROM product_stats WHERE main_seller_id = %s ORDER BY times_sold DESC LIMIT 1",
                        (simulate_orders.SELLER_A,))["product_id"]


@pytest.mark.parametrize("name", sorted(SELLER_REGISTRY))
def test_tool_touches_only_declared_tables(probe_user, name, monkeypatch):
    spec = SELLER_REGISTRY[name]
    _grant_exactly(probe_user, spec)
    monkeypatch.setattr(db, "SELLER_AGENT_POOL", db.Pool("SELLER_PROBE_MYSQL_USER", "SELLER_PROBE_MYSQL_PASSWORD"))
    me = seller(simulate_orders.SELLER_A)
    arg_sets = PROBE_ARGS.get(name, dict)()
    arg_sets = arg_sets if isinstance(arg_sets, list) else [arg_sets]
    try:
        if name == "update_listing":
            pid = _top_product()
            before = db.query_one("SELECT typical_price FROM product_stats WHERE product_id = %s", (pid,))
            try:
                preview_seller_tool(me, name, {"product_id": pid, "price": 77.7})
                call_seller_tool(me, name, {"product_id": pid, "price": 77.7}, confirmed=True)
            finally:
                monkeypatch.undo()   # restore with the app user
                db.execute("UPDATE product_stats SET typical_price = %s WHERE product_id = %s",
                           (before["typical_price"], pid))
                db.execute("DELETE FROM product_edits WHERE product_id = %s AND editor_role = 'seller_assistant'", (pid,))
        else:
            for args in arg_sets:
                call_seller_tool(me, name, args)
    except ToolError as exc:
        if "command denied" in str(exc):
            pytest.fail(f"{name} touched a table it doesn't declare: {exc}")
        raise
    except pymysql.err.OperationalError as exc:
        pytest.fail(f"{name} touched a table it doesn't declare: {exc}")


# ---- 6. the SQL tool sees exactly the caller's rows ---------------------------------------------


def test_sql_tool_sees_exactly_each_sellers_rows(sellers):
    for n, s in enumerate(sellers):
        out = call_seller_tool(seller(s["id"], n), "query_seller_data", {"sql":
            "SELECT (SELECT COUNT(DISTINCT order_id) FROM my_sales) AS orders, "
            "(SELECT COUNT(*) FROM my_products) AS products"})
        products = db.query_one("SELECT COUNT(*) n FROM product_stats WHERE main_seller_id = %s", (s["id"],))["n"]
        assert out["rows"] == [[len(s["orders"]), products]], s["id"]


def test_sql_tool_never_exposes_buyer_or_seller_ids(sellers):
    a = sellers[-2]
    out = call_seller_tool(seller(a["id"]), "query_seller_data", {"sql":
        "SELECT o.order_id, o.buyer_city, s.product_id FROM my_orders o JOIN my_sales s ON s.order_id = o.order_id "
        "ORDER BY o.order_purchase_timestamp DESC LIMIT 50"})
    assert _ids_in(out) <= a["owned"]
    with pytest.raises(ToolError):   # columns that don't exist in the seller's tables
        call_seller_tool(seller(a["id"]), "query_seller_data", {"sql": "SELECT customer_id FROM my_orders"})
    with pytest.raises(ToolError):
        call_seller_tool(seller(a["id"]), "query_seller_data", {"sql": "SELECT seller_id FROM my_sales"})

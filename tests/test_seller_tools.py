"""Seller agent, step 5: the seller tools (docs/seller/TEST_SCENARIOS.md). Database values are the
ground truth; Seller A's three recent scenario orders are (re)created by the fixture."""

import json

import pytest

import ai.seller.tools  # noqa: F401  (registers the seller tools)
import db
from ai.security import ConfirmationRequired, Principal, ToolError
from ai.seller import simulate_orders
from ai.seller.security import call_seller_tool, preview_seller_tool, seller_tools

A_ID, B_ID = "6560211a19b47992c3666cc44a7e94c0", "4a3ca9315b744ce9f8e9374361493884"
A = Principal(role="seller", user_id=9_100_071, username="seller-a", seller_id=A_ID)
B = Principal(role="seller", user_id=9_100_072, username="seller-b", seller_id=B_ID)
ACTIVE = "o.order_status NOT IN ('canceled','unavailable')"


@pytest.fixture(scope="module", autouse=True)
def scenario_orders():
    simulate_orders.create()   # idempotent: same three orders, relative to now
    yield


def _one(sql, params):
    return db.query_one(sql, params)


def _own_product(seller_id):
    return _one("SELECT product_id FROM product_stats WHERE main_seller_id = %s ORDER BY times_sold DESC LIMIT 1",
                (seller_id,))["product_id"]


def _order_of(seller_id):
    return _one("SELECT order_id FROM order_items WHERE seller_id = %s ORDER BY order_id LIMIT 1", (seller_id,))["order_id"]


def test_the_seller_agent_has_its_own_nine_tools():
    assert {t.name for t in seller_tools()} == {
        "my_shop", "orders_to_ship", "order_details", "my_product", "product_reviews",
        "returns_and_refunds", "query_seller_data", "seller_metric", "update_listing"}


def test_my_shop_matches_the_database():
    shop = call_seller_tool(A, "my_shop")
    truth = _one(f"SELECT ROUND(SUM(i.price),2) v FROM order_items i JOIN orders o USING (order_id) "
                 f"WHERE i.seller_id = %s AND {ACTIVE}", (A_ID,))["v"]
    assert shop["all_time"]["revenue"] == pytest.approx(float(truth))
    assert shop["products_listed"] == 256 and shop["public_rating"] == pytest.approx(3.91, abs=0.01)


def test_orders_to_ship_puts_the_overdue_order_first_and_skips_old_leftovers():
    out = call_seller_tool(A, "orders_to_ship")
    late, due, sent = simulate_orders.ORDER_IDS[1], simulate_orders.ORDER_IDS[0], simulate_orders.ORDER_IDS[2]
    assert [o["order_id"] for o in out["to_ship"]] == [late, due]
    assert out["to_ship"][0]["overdue"] and not out["to_ship"][1]["overdue"]
    assert [o["order_id"] for o in out["in_transit"]] == [sent]
    assert out["old_unfinished"]["count"] == 28


def test_another_sellers_order_or_product_is_not_found():
    with pytest.raises(ToolError, match="No order found"):
        call_seller_tool(A, "order_details", {"order_id": _order_of(B_ID)})
    with pytest.raises(ToolError, match="No product found"):
        call_seller_tool(A, "my_product", {"product_id": _own_product(B_ID)})


def test_a_shared_order_shows_only_the_sellers_own_items():
    shared = _one("""SELECT order_id FROM order_items WHERE order_id IN (SELECT order_id FROM order_items
                     GROUP BY order_id HAVING COUNT(DISTINCT seller_id) > 1) AND seller_id = %s LIMIT 1""", (B_ID,))
    out = call_seller_tool(B, "order_details", {"order_id": shared["order_id"]})
    own = _one("SELECT COUNT(*) n FROM order_items WHERE order_id = %s AND seller_id = %s", (shared["order_id"], B_ID))
    assert len(out["your_items"]) == own["n"]
    text = json.dumps(out)
    assert "customer_unique_id" not in text and "seller_id" not in text


def test_reviews_are_filtered_linked_to_products_and_marked_untrusted():
    out = call_seller_tool(A, "product_reviews", {"max_score": 2, "limit": 3})
    assert out["matching_reviews"] == 319
    assert all(r["score"] <= 2 and r["products"] for r in out["reviews"])
    assert all("untrusted_text" in r["comment"] for r in out["reviews"])


def test_returns_and_refunds_match_the_scenarios_and_hide_the_demo_marker():
    out = call_seller_tool(A, "returns_and_refunds", {"limit": 3})
    assert out["by_reason"]["not_as_described"] == 24 and out["by_reason"]["wrong_item"] == 11
    assert out["refunds_by_status"]["failed"] == {"count": 2, "amount": 219.05}
    assert "simulated" not in json.dumps(out)


# ---- listing edits ------------------------------------------------------------------------------


@pytest.fixture
def product_a():
    pid = _own_product(A_ID)
    before = _one("SELECT display_name, typical_price, avg_freight FROM product_stats WHERE product_id = %s", (pid,))
    yield pid
    db.execute("UPDATE product_stats SET typical_price = %s, avg_freight = %s WHERE product_id = %s",
               (before["typical_price"], before["avg_freight"], pid))
    db.execute("DELETE FROM product_edits WHERE product_id = %s AND editor_role = 'seller_assistant'", (pid,))


def test_listing_change_needs_confirmation_and_is_audited(product_a):
    card = preview_seller_tool(A, "update_listing", {"product_id": product_a, "price": 79.9})
    assert card["lines"] == ["Price: 78.0 → 79.9"]
    with pytest.raises(ConfirmationRequired):
        call_seller_tool(A, "update_listing", {"product_id": product_a, "price": 79.9})
    assert float(_one("SELECT typical_price p FROM product_stats WHERE product_id = %s", (product_a,))["p"]) == 78.0
    call_seller_tool(A, "update_listing", {"product_id": product_a, "price": 79.9}, confirmed=True)
    assert float(_one("SELECT typical_price p FROM product_stats WHERE product_id = %s", (product_a,))["p"]) == 79.9
    edit = _one("SELECT editor_role, changes FROM product_edits WHERE product_id = %s ORDER BY edit_id DESC LIMIT 1",
                (product_a,))
    assert edit["editor_role"] == "seller_assistant" and json.loads(edit["changes"])["price"]["to"] == 79.9


@pytest.mark.parametrize("args,error", [
    ({"price": 0}, "Invalid arguments"),
    ({"price": 200_000}, "Invalid arguments"),
    ({"name": "ab"}, "Invalid arguments"),
    ({"category": "rocket_ships"}, "not a store category"),
    ({}, "Nothing to change"),
])
def test_listing_values_are_checked_like_the_seller_center(product_a, args, error):
    with pytest.raises(ToolError, match=error):
        preview_seller_tool(A, "update_listing", {"product_id": product_a, **args})


def test_a_seller_cannot_edit_another_sellers_product():
    with pytest.raises(ToolError, match="No product found"):
        preview_seller_tool(A, "update_listing", {"product_id": _own_product(B_ID), "price": 1.0})


# ---- the seller SQL tool ------------------------------------------------------------------------


def test_sql_tool_sees_only_the_sellers_own_items():
    out = call_seller_tool(A, "query_seller_data", {"sql": "SELECT COUNT(*) AS n FROM my_sales"})
    truth = _one("SELECT COUNT(*) n FROM order_items WHERE seller_id = %s", (A_ID,))["n"]
    assert out["rows"] == [[truth]]


def test_sql_tool_average_order_value_is_per_order():
    out = call_seller_tool(A, "query_seller_data", {"sql":
        "SELECT ROUND(AVG(items_total), 2) FROM my_order_totals WHERE order_status NOT IN ('canceled','unavailable')"})
    truth = _one(f"""SELECT ROUND(AVG(t), 2) v FROM (SELECT SUM(i.price) t FROM order_items i JOIN orders o USING (order_id)
                    WHERE i.seller_id = %s AND {ACTIVE} GROUP BY i.order_id) x""", (A_ID,))["v"]
    assert out["rows"] == [[float(truth)]]


@pytest.mark.parametrize("sql", [
    "SELECT * FROM order_items",                            # real tables are never reachable
    "SELECT payment_value FROM payments",
    "SELECT * FROM my_sales; DELETE FROM products",
    "WITH x AS (SELECT 1) SELECT * FROM x",
    "SELECT SLEEP(10) FROM my_sales",
    "UPDATE my_products SET typical_price = 1",
])
def test_sql_tool_refuses_anything_but_one_select_over_the_sellers_tables(sql):
    with pytest.raises(ToolError):
        call_seller_tool(A, "query_seller_data", {"sql": sql})


def test_sql_tool_description_comes_from_the_catalog():
    from ai.seller.tools.query import query_seller_data
    doc = query_seller_data.__doc__
    assert "my_sales (one row per unit the seller sold)" in doc
    assert "late shipping:" in doc and "open order:" in doc          # seller_metrics
    assert "simulat" not in doc.lower() and "customer_unique_id" not in doc

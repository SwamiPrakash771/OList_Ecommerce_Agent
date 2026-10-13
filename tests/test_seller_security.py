"""Seller agent, step 3: its own tool registry and its own MySQL user (docs/seller/)."""

import pymysql
import pytest

import db
from ai.security import Principal, ToolError, ToolKind
from ai.seller import db_user, security
from ai.seller.security import SellerToolDefinitionError, call_seller_tool, seller_tool

SELLER_A = Principal(role="seller", user_id=9_100_061, username="a", seller_id="6560211a19b47992c3666cc44a7e94c0")
CUSTOMER = Principal(role="customer", user_id=9_100_062, username="c", customer_unique_id="8d50f5eadf50201ccdcedfb9e2ac8455")


@pytest.fixture
def registered():
    """Remove tools registered by a test."""
    before = set(security.SELLER_REGISTRY)
    yield
    for name in set(security.SELLER_REGISTRY) - before:
        del security.SELLER_REGISTRY[name]


# ---- registry rules -----------------------------------------------------------------------------


def test_identity_can_never_be_a_tool_argument(registered):
    with pytest.raises(SellerToolDefinitionError, match="identity"):
        @seller_tool(reads=["order_items"])
        def sales_of(principal, seller_id: str):
            """Sales of a seller."""


def test_tools_take_the_session_identity_first(registered):
    with pytest.raises(SellerToolDefinitionError, match="principal"):
        @seller_tool(reads=["order_items"])
        def sales(product_id: str):
            """Sales."""


@pytest.mark.parametrize("table", ["payments", "app_users", "customer_addresses"])
def test_tools_may_not_read_what_sellers_may_not_see(registered, table):
    def peek(principal):
        """Look at a table."""

    with pytest.raises(SellerToolDefinitionError, match="may not read"):
        seller_tool(reads=[table])(peek)


def test_tools_may_not_write_without_a_grant(registered):
    with pytest.raises(SellerToolDefinitionError, match="may not write"):
        @seller_tool(kind=ToolKind.WRITE_UNDO, reads=["products"], writes=["orders"])
        def cancel(principal, order_id: str):
            """Cancel an order."""


def test_only_sellers_can_call_seller_tools(registered):
    @seller_tool(reads=["seller_stats"])
    def my_rating(principal):
        """The seller's own rating."""
        row = db.query_one("SELECT avg_rating FROM seller_stats WHERE seller_id = %s", (principal.seller_id,))
        return float(row["avg_rating"])

    assert call_seller_tool(SELLER_A, "my_rating") == pytest.approx(3.91, abs=0.01)
    with pytest.raises(ToolError, match="not available"):
        call_seller_tool(CUSTOMER, "my_rating")
    with pytest.raises(ToolError):
        call_seller_tool(SELLER_A, "my_rating", {"seller_id": "4a3ca9315b744ce9f8e9374361493884"})


def test_seller_and_customer_registries_are_separate():
    from ai.security import REGISTRY
    assert not set(security.SELLER_REGISTRY) & set(REGISTRY)
    import ai.tools  # noqa: F401  (the customer tools)
    assert all("seller" not in spec.roles for spec in REGISTRY.values())


# ---- the seller database user: MySQL itself refuses -----------------------------------------


def _as_seller(sql: str):
    with db.as_seller_agent():
        return db.query_df(sql)


@pytest.mark.parametrize("sql", [
    "SELECT payment_value FROM payments LIMIT 1",                      # payments: no grant at all
    "SELECT customer_unique_id FROM customers LIMIT 1",                # buyer identity
    "SELECT customer_zip_code_prefix FROM customers LIMIT 1",          # buyer location detail
    "SELECT payment_method FROM refunds LIMIT 1",                      # how the buyer was refunded
    "SELECT failure_reason FROM refunds LIMIT 1",
    "SELECT customer_unique_id FROM returns LIMIT 1",
    "SELECT source FROM returns LIMIT 1",                              # demo-data marker
    "SELECT username FROM app_users LIMIT 1",
    "SELECT * FROM customers LIMIT 1",                                 # * includes hidden columns
])
def test_mysql_refuses_what_sellers_may_not_see(sql):
    with pytest.raises(pymysql.err.OperationalError) as err:
        _as_seller(sql)
    assert err.value.args[0] in (1142, 1143)   # table / column command denied


def test_mysql_allows_what_sellers_may_see():
    assert len(_as_seller("SELECT customer_city, customer_state FROM customers LIMIT 1")) == 1
    assert len(_as_seller("SELECT amount, status FROM refunds LIMIT 1")) == 1
    assert len(_as_seller("SELECT order_id, review_score FROM reviews LIMIT 1")) == 1


@pytest.mark.parametrize("sql", [
    "UPDATE product_stats SET main_seller_id = main_seller_id WHERE product_id = 'x'",   # who owns a product
    "UPDATE products SET photos = photos WHERE product_id = 'x'",
    "UPDATE orders SET order_status = order_status WHERE order_id = 'x'",
    "UPDATE refunds SET amount = amount WHERE refund_id = 'x'",
    "DELETE FROM product_edits WHERE edit_id = 0",                                       # the audit trail
    "UPDATE product_edits SET changes = changes WHERE edit_id = 0",
])
def test_seller_user_cannot_change_anything_but_listing_fields(sql):
    with pytest.raises(pymysql.err.OperationalError) as err:
        with db.as_seller_agent():
            db.execute(sql)
    assert err.value.args[0] in (1142, 1143)


def test_seller_user_may_change_listing_fields_only():
    with db.as_seller_agent():   # no row matches; MySQL only checks the privilege
        assert db.execute("UPDATE product_stats SET typical_price = typical_price WHERE product_id = 'x'") == 0
        assert db.execute("UPDATE products SET weight_g = weight_g WHERE product_id = 'x'") == 0


def test_grants_come_from_the_catalogs_seller_rules():
    cols = db_user.readable_columns()
    assert "payments" not in cols and "app_users" not in cols
    assert set(cols["customers"]) == {"customer_id", "customer_city", "customer_state"}
    assert "amount" in cols["refunds"] and "payment_method" not in cols["refunds"]

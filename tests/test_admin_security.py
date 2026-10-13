"""Admin agent, step 3: its own tool registry, its own MySQL user, masking and lookup audit (docs/admin/)."""

import pymysql
import pytest

import db
from ai import store
from ai.admin import db_user, security
from ai.admin.security import (
    AdminToolDefinitionError, admin_tool, call_admin_tool, in_admin_conversation, log_lookup, mask_id,
)
from ai.security import Principal, ToolError, ToolKind

ADMIN = Principal(role="admin", user_id=9_100_071, username="admin-test")
SELLER_A = Principal(role="seller", user_id=9_100_061, username="a", seller_id="6560211a19b47992c3666cc44a7e94c0")
CUSTOMER = Principal(role="customer", user_id=9_100_062, username="c", customer_unique_id="8d50f5eadf50201ccdcedfb9e2ac8455")
WEAK_SELLER = "54965bbe3e4f07ae045b90b0b8541f52"


@pytest.fixture
def registered():
    """Remove tools registered by a test."""
    before = set(security.ADMIN_REGISTRY)
    yield
    for name in set(security.ADMIN_REGISTRY) - before:
        del security.ADMIN_REGISTRY[name]


@pytest.fixture
def test_rows():
    """Delete the rows this test user wrote (access log, conversations)."""
    yield
    db.execute("DELETE FROM ai_admin_access_log WHERE admin_user_id = %s", (ADMIN.user_id,))
    db.execute("DELETE FROM ai_conversations WHERE owner_user_id = %s AND owner_role = 'admin'", (ADMIN.user_id,))


# ---- registry rules -----------------------------------------------------------------------------


def test_the_admins_identity_can_never_be_a_tool_argument(registered):
    with pytest.raises(AdminToolDefinitionError, match="identity"):
        @admin_tool(reads=["orders"])
        def orders_of(principal, user_id: int):
            """Orders seen by an admin."""


def test_a_seller_id_is_a_normal_admin_lookup_argument(registered):
    @admin_tool(reads=["seller_stats"])
    def seller_rating(principal, seller_id: str):
        """A seller's rating."""
        row = db.query_one("SELECT avg_rating FROM seller_stats WHERE seller_id = %s", (seller_id,))
        return float(row["avg_rating"])

    assert call_admin_tool(ADMIN, "seller_rating", {"seller_id": WEAK_SELLER}) == pytest.approx(2.94, abs=0.01)


def test_tools_take_the_session_identity_first(registered):
    with pytest.raises(AdminToolDefinitionError, match="principal"):
        @admin_tool(reads=["orders"])
        def orders(order_id: str):
            """Orders."""


@pytest.mark.parametrize("table", ["app_users", "customer_addresses", "order_addresses", "cart_items"])
def test_tools_may_not_read_protected_tables(registered, table):
    def peek(principal):
        """Look at a table."""

    with pytest.raises(AdminToolDefinitionError, match="may not read"):
        admin_tool(reads=[table])(peek)


@pytest.mark.parametrize("kind, writes", [(ToolKind.WRITE_UNDO, ()), (ToolKind.READ, ("refunds",))])
def test_the_admin_agent_is_read_only(registered, kind, writes):
    with pytest.raises(AdminToolDefinitionError, match="read-only"):
        @admin_tool(kind=kind, reads=["refunds"], writes=writes)
        def approve_refund(principal, refund_id: str):
            """Approve a refund."""


@pytest.mark.parametrize("ref", ["refunds.failure_reason", "support_tickets.description", "customers.customer_zip_code_prefix"])
def test_only_listed_masked_columns_may_be_aggregated(registered, ref):
    def total(principal):
        """A total."""

    with pytest.raises(AdminToolDefinitionError, match="aggregate-only"):
        admin_tool(reads=["orders"], aggregates=[ref])(total)


def test_only_admins_can_call_admin_tools(registered):
    @admin_tool(reads=["orders"])
    def order_count(principal):
        """Orders on the platform."""
        return int(db.query_one("SELECT COUNT(*) AS n FROM orders")["n"])

    assert call_admin_tool(ADMIN, "order_count") > 90_000
    for other in (SELLER_A, CUSTOMER, Principal.guest(), Principal(role="admin")):   # no user id: not signed in
        with pytest.raises(ToolError, match="not available"):
            call_admin_tool(other, "order_count")
    with pytest.raises(ToolError):
        call_admin_tool(ADMIN, "order_count", {"user_id": 1})


def test_tools_run_on_the_admin_agents_database_user(registered):
    @admin_tool(reads=["orders"])
    def whoami(principal):
        """The database user."""
        return db.query_one("SELECT CURRENT_USER() AS u")["u"]

    assert call_admin_tool(ADMIN, "whoami").startswith("olist_admin_agent@")


def test_admin_registry_is_separate_from_the_other_agents():
    import ai.seller.tools  # noqa: F401
    import ai.tools  # noqa: F401
    from ai.security import REGISTRY
    from ai.seller.security import SELLER_REGISTRY
    assert not set(security.ADMIN_REGISTRY) & (set(REGISTRY) | set(SELLER_REGISTRY))


# ---- the admin database user: MySQL itself refuses -----------------------------------------------


def _as_admin(sql: str):
    with db.as_admin_agent():
        return db.query_df(sql)


@pytest.mark.parametrize("sql", [
    "SELECT username FROM app_users LIMIT 1",                          # logins
    "SELECT recipient_name FROM order_addresses LIMIT 1",              # addresses
    "SELECT * FROM customer_addresses LIMIT 1",
    "SELECT product_id FROM cart_items LIMIT 1",
    "SELECT customer_zip_code_prefix FROM customers LIMIT 1",          # personal, not aggregate-only
    "SELECT description FROM support_tickets LIMIT 1",                 # ticket text
    "SELECT ai_summary FROM support_tickets LIMIT 1",
    "SELECT customer_unique_id FROM support_tickets LIMIT 1",
    "SELECT failure_reason FROM refunds LIMIT 1",
    "SELECT customer_unique_id FROM returns LIMIT 1",
    "SELECT * FROM customers LIMIT 1",                                 # * includes ungranted columns
])
def test_mysql_refuses_what_the_admin_agent_may_not_see(sql):
    with pytest.raises(pymysql.err.OperationalError) as err:
        _as_admin(sql)
    assert err.value.args[0] in (1142, 1143)   # table / column command denied


def test_mysql_allows_platform_figures():
    gmv = _as_admin("SELECT SUM(payment_value) AS v FROM payments")
    assert float(gmv.v[0]) > 15_000_000
    by_state = _as_admin("SELECT customer_state, COUNT(*) AS n FROM customers GROUP BY customer_state")
    assert len(by_state) == 27
    assert len(_as_admin("SELECT status, SUM(amount) AS a FROM refunds GROUP BY status")) >= 3
    assert len(_as_admin("SELECT seller_id, on_time_pct FROM seller_stats LIMIT 1")) == 1


@pytest.mark.parametrize("sql", [
    "UPDATE orders SET order_status = order_status WHERE order_id = 'x'",
    "UPDATE refunds SET status = status WHERE refund_id = 'x'",
    "UPDATE products SET weight_g = weight_g WHERE product_id = 'x'",
    "UPDATE ai_settings SET value = value WHERE setting_key = 'x'",       # can't change AI settings
    "UPDATE ai_limits SET blocked = blocked WHERE scope = 'x'",
    "DELETE FROM ai_conversations WHERE conversation_id = 'x'",            # can't erase the audit trail
    "DELETE FROM ai_admin_access_log WHERE log_id = 0",
    "UPDATE ai_admin_access_log SET reason = reason WHERE log_id = 0",
    "UPDATE ai_messages SET content = content WHERE message_id = 0",
])
def test_the_admin_user_cannot_change_store_data_or_the_audit_trail(sql):
    with pytest.raises(pymysql.err.OperationalError) as err:
        with db.as_admin_agent():
            db.execute(sql)
    assert err.value.args[0] in (1142, 1143)


def test_grants_follow_the_catalogs_admin_rules():
    shown = db_user.tool_columns()
    assert "app_users" not in shown and "customer_addresses" not in shown and "order_addresses" not in shown
    assert "payment_value" not in shown["payments"]              # masked: aggregates only
    assert "customer_state" not in shown["customers"]
    assert "description" not in shown["support_tickets"]
    readable = db_user.readable_columns()
    assert {"payment_value", "payment_type"} <= set(readable["payments"])
    assert "customer_zip_code_prefix" not in readable["customers"]
    assert "failure_reason" not in readable["refunds"]


# ---- masking and lookup audit --------------------------------------------------------------------


def test_masked_ids_are_stable_and_do_not_reveal_the_real_id():
    real = CUSTOMER.customer_unique_id
    label = mask_id("customer", real)
    assert label == mask_id("customer", real)
    assert label.startswith("customer-") and real[:8] not in label
    assert mask_id("customer", real) != mask_id("customer", "another")
    assert mask_id("customer", None) is None


def test_record_lookups_are_logged_for_the_conversation(test_rows):
    log_lookup(ADMIN, "order", "a1a6eada")     # outside a conversation: nothing to link to
    conversation = "f" * 32
    with in_admin_conversation(conversation):
        with db.as_admin_agent():
            log_lookup(ADMIN, "order", "a1a6eada")
    rows = db.query_df("SELECT conversation_id, action, reason FROM ai_admin_access_log WHERE admin_user_id = %s",
                       (ADMIN.user_id,))
    assert len(rows) == 1
    assert rows.conversation_id[0] == conversation and rows.action[0] == "view"
    assert "order a1a6eada" in rows.reason[0]


# ---- retention: admin chats are an audit trail ---------------------------------------------------


def test_admin_chats_are_kept_as_an_audit_trail(test_rows):
    with db.as_admin_agent():
        conv = store.create_conversation(ADMIN, title="Revenue by month")
    row = db.query_one("SELECT retention_class FROM ai_conversations WHERE conversation_id = %s", (conv,))
    assert row["retention_class"] == "admin_audit"
    assert store.settings(refresh=True).get("admin_retention_days") == 365


def test_customer_chats_keep_standard_retention():
    conv = store.create_conversation(CUSTOMER)
    try:
        row = db.query_one("SELECT retention_class FROM ai_conversations WHERE conversation_id = %s", (conv,))
        assert row["retention_class"] == "standard"
    finally:
        db.execute("DELETE FROM ai_conversations WHERE conversation_id = %s", (conv,))

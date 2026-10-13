"""Admin agent, step 6: permission tests. Through any admin tool or argument, no one but a signed-in admin
gets anything; admins never get a person's identity, contact or payment details, chat content, ticket
text or refund failure reasons; and no tool reads more than it declares (docs/admin/ADMIN_AGENT.md).

1. the role matrix (customers, sellers, guests, admins without an account get nothing);
2. identity arguments are refused on every tool;
3. hostile IDs are refused before any query;
4. each tool touches only the tables and columns it declares (run on a throwaway MySQL user granted
   exactly those);
5. every tool's output: no personal field, no real customer ID;
6. sensitive metrics never show a group small enough to describe one person;
7. customer-written text comes back marked untrusted;
8. SQL fallback attacks.
"""

import json
import os
import re
import secrets

import pymysql
import pytest

import ai.admin.tools  # noqa: F401  (registers the admin tools)
import db
from ai.admin import db_user, facts
from ai.admin.security import ADMIN_REGISTRY, call_admin_tool, in_admin_conversation
from ai.admin.tools import metrics
from ai.security import Principal, ToolError

ADMIN = Principal(role="admin", user_id=9_100_071, username="admin-test")
DEMO_ORDER = "a1a6eadae352582010f449d8527b55f3"
REVIEWED_ORDER = "e28abf2eb2f1fbcbdc2dd0cd9a561671"   # has a written review comment
WEAK_SELLER = "54965bbe3e4f07ae045b90b0b8541f52"
HEX32 = re.compile(r"\b[0-9a-f]{32}\b")

# Typical arguments that make each tool read everything it can.
ARGS = {
    "admin_metric": [{"metric": "gmv", "group_by": ["buyer_state"]}, {"metric": "customers", "group_by": ["month"]},
                     {"metric": "on_time_rate", "group_by": ["seller"], "min_orders": 20, "top_n": 5},
                     {"metric": "item_revenue", "group_by": ["category", "seller_state"], "top_n": 5},
                     {"metric": "gmv", "group_by": ["month"], "period": "2025", "filters": {"buyer_state": ["SP"]}},
                     {"metric": "item_revenue", "group_by": ["week"], "period": "2025-Q2", "cumulative": True,
                      "compare_to": "same_period_last_year"}],
    "look_up_record": [{"kind": "order", "record_id": DEMO_ORDER}, {"kind": "order", "record_id": REVIEWED_ORDER},
                       {"kind": "customer", "record_id": DEMO_ORDER}, {"kind": "seller", "record_id": WEAK_SELLER},
                       {"kind": "product", "record_id": "99a4788c"}],
    "operations_report": [{"report": "stuck_refunds"}, {"report": "failed_refunds"}, {"report": "processing_refunds"},
                          {"report": "late_open_orders"}, {"report": "ai_usage", "period": "all"},
                          {"report": "ai_usage", "period": "all", "group_by": "model"}],
    "show_chart": [{"metric": "gmv", "group_by": ["month", "buyer_state"], "period": "2025"},
                   {"metric": "on_time_rate", "group_by": ["seller"], "min_orders": 50}],
    "query_platform_data": [{"sql": "SELECT (SELECT COUNT(*) FROM platform_orders) a, (SELECT COUNT(*) FROM platform_items) b, "
                                    "(SELECT COUNT(*) FROM platform_products) c, (SELECT COUNT(*) FROM platform_sellers) d, "
                                    "(SELECT COUNT(*) FROM platform_reviews) e, (SELECT COUNT(*) FROM platform_returns) f, "
                                    "(SELECT COUNT(*) FROM platform_refunds) g, (SELECT COUNT(*) FROM platform_tickets) h, "
                                    "(SELECT COUNT(*) FROM order_summary) i, (SELECT COUNT(*) FROM seller_order_summary) j"}],
}
PERSONAL_FIELDS = {"customer_unique_id", "customer_id", "customer_city", "customer_state", "customer_zip_code_prefix",
                   "zip_code", "recipient_name", "phone", "street", "number", "complement", "neighborhood",
                   "payment_value", "payments_total", "payment_installments", "payment_method", "amount",
                   "failure_reason", "description", "ai_summary", "content", "title", "args", "reason_text",
                   "customer_key", "password_hash", "username", "embedding", "session_id", "owner_user_id"}


@pytest.fixture(scope="module", autouse=True)
def reporting_tables():
    if facts.refreshed_at() is None:
        facts.refresh()


@pytest.fixture
def conversation():
    conv = "d" * 32
    with in_admin_conversation(conv):
        yield conv
    db.execute("DELETE FROM ai_admin_access_log WHERE admin_user_id = %s", (ADMIN.user_id,))


def test_every_tool_has_probe_arguments():
    assert set(ARGS) == set(ADMIN_REGISTRY)


# ---- 1. the role matrix ------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(ADMIN_REGISTRY))
@pytest.mark.parametrize("who", [
    Principal(role="customer", user_id=9_100_062, username="c", customer_unique_id="8d50f5eadf50201ccdcedfb9e2ac8455"),
    Principal(role="seller", user_id=9_100_061, username="s", seller_id="6560211a19b47992c3666cc44a7e94c0"),
    Principal.guest(),
    Principal(role="admin"),                       # no account: not signed in
], ids=["customer", "seller", "guest", "admin-without-account"])
def test_only_signed_in_admins_can_use_admin_tools(name, who):
    with pytest.raises(ToolError, match="not available"):
        call_admin_tool(who, name, ARGS[name][0])


# ---- 2. identity arguments ---------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(ADMIN_REGISTRY))
@pytest.mark.parametrize("smuggled", ["user_id", "admin_user_id", "username", "role"])
def test_the_admins_identity_can_never_be_passed_to_a_tool(name, smuggled):
    with pytest.raises(ToolError, match="Identity"):
        call_admin_tool(ADMIN, name, {**ARGS[name][0], smuggled: 1})


# ---- 3. hostile IDs ----------------------------------------------------------------------------

HOSTILE = ["' OR '1'='1", "%%%%%%%%", "________", "a1a6eada%", "a1a6eada' --", "../../etc/passwd",
           "a1a6eada; DROP TABLE orders", "SELECT * FROM customers"]


@pytest.mark.parametrize("bad", HOSTILE)
@pytest.mark.parametrize("kind", ["order", "customer", "seller"])
def test_hostile_ids_are_refused(kind, bad, conversation):
    with pytest.raises(ToolError):
        call_admin_tool(ADMIN, "look_up_record", {"kind": kind, "record_id": bad})
    assert db.query_one("SELECT COUNT(*) AS n FROM ai_admin_access_log WHERE admin_user_id = %s",
                        (ADMIN.user_id,))["n"] == 0          # nothing was looked up, so nothing logged


def test_masked_labels_cannot_be_guessed_by_prefix():
    with pytest.raises(ToolError):
        call_admin_tool(ADMIN, "look_up_record", {"kind": "customer", "record_id": "customer-"})
    with pytest.raises(ToolError):
        call_admin_tool(ADMIN, "look_up_record", {"kind": "customer", "record_id": "customer-8"})


# ---- 4. each tool touches only what it declares -------------------------------------------------


@pytest.fixture(scope="module")
def probe_user():
    user, password = "olist_admin_probe", secrets.token_urlsafe(18)
    for host in ("localhost", "127.0.0.1"):
        db.execute(f"CREATE USER IF NOT EXISTS '{user}'@'{host}' IDENTIFIED BY %s", (password,))
        db.execute(f"ALTER USER '{user}'@'{host}' IDENTIFIED BY %s", (password,))
    os.environ["ADMIN_PROBE_MYSQL_USER"], os.environ["ADMIN_PROBE_MYSQL_PASSWORD"] = user, password
    yield user
    for host in ("localhost", "127.0.0.1"):
        db.execute(f"DROP USER IF EXISTS '{user}'@'{host}'")


def _grant_exactly(user: str, spec) -> None:
    """The declared tables' showable columns, the declared aggregate-only columns, and the lookup log."""
    schema, shown = os.environ["MYSQL_DATABASE"], db_user.tool_columns()
    columns: dict[str, set[str]] = {t: set(shown[t]) for t in spec.reads}
    for ref in spec.aggregates:
        table, _, column = ref.partition(".")
        columns.setdefault(table, set()).add(column)
    for host in ("localhost", "127.0.0.1"):
        account = f"'{user}'@'{host}'"
        db.execute(f"REVOKE ALL PRIVILEGES, GRANT OPTION FROM {account}")
        for table, cols in columns.items():
            db.execute(f"GRANT SELECT ({', '.join(f'`{c}`' for c in sorted(cols))}) ON `{schema}`.`{table}` TO {account}")
        db.execute(f"GRANT INSERT ON `{schema}`.`ai_admin_access_log` TO {account}")


@pytest.mark.parametrize("name", sorted(ADMIN_REGISTRY))
def test_tool_touches_only_declared_columns(probe_user, name, monkeypatch, conversation):
    spec = ADMIN_REGISTRY[name]
    _grant_exactly(probe_user, spec)
    monkeypatch.setattr(db, "ADMIN_AGENT_POOL", db.Pool("ADMIN_PROBE_MYSQL_USER", "ADMIN_PROBE_MYSQL_PASSWORD"))
    for args in ARGS[name]:
        try:
            call_admin_tool(ADMIN, name, args)
        except ToolError as exc:
            if "command denied" in str(exc):
                pytest.fail(f"{name} touched something it doesn't declare: {exc}")
            raise
        except pymysql.err.OperationalError as exc:
            pytest.fail(f"{name}{args} touched something it doesn't declare: {exc}")


def test_no_tool_declares_content_or_secrets():
    never = {"ai_messages.content", "ai_tool_calls.args", "ai_conversations.title", "ai_feedback.reason",
             "support_tickets.description", "support_tickets.ai_summary", "refunds.failure_reason"}
    for spec in ADMIN_REGISTRY.values():
        assert not set(spec.aggregates) & never, spec.name
        for table in spec.reads:
            for col in db_user.tool_columns()[table]:
                assert f"{table}.{col}" not in never, (spec.name, table, col)


# ---- 5. outputs ---------------------------------------------------------------------------------


def _fields(obj) -> set[str]:
    if isinstance(obj, dict):
        return set(obj) | {k for v in obj.values() for k in _fields(v)}
    if isinstance(obj, list):
        return {k for v in obj for k in _fields(v)}
    return set()


@pytest.fixture(scope="module")
def outputs():
    out = []
    conv = "c" * 32
    with in_admin_conversation(conv):
        for name, arg_list in ARGS.items():
            for args in arg_list:
                out.append((name, args, call_admin_tool(ADMIN, name, args)))
    db.execute("DELETE FROM ai_admin_access_log WHERE admin_user_id = %s", (ADMIN.user_id,))
    return out


def test_no_output_has_a_personal_field(outputs):
    for name, args, result in outputs:
        assert not _fields(result) & PERSONAL_FIELDS, (name, args, _fields(result) & PERSONAL_FIELDS)


def test_no_output_contains_a_real_customer_id(outputs):
    found = {i for _, _, r in outputs for i in HEX32.findall(json.dumps(r, default=str))}
    assert found   # order, seller and refund IDs are expected
    marks = ", ".join(["%s"] * len(found))
    leaked = db.query_df(f"SELECT customer_unique_id AS id FROM customers WHERE customer_unique_id IN ({marks}) "
                         f"UNION SELECT customer_id FROM customers WHERE customer_id IN ({marks})", (*found, *found))
    assert leaked.empty, list(leaked.id)


def test_lookups_are_logged_under_the_calling_admin():
    conv = "b" * 32
    with in_admin_conversation(conv):
        call_admin_tool(ADMIN, "look_up_record", {"kind": "order", "record_id": DEMO_ORDER})
    try:
        rows = db.query_df("SELECT admin_user_id, action FROM ai_admin_access_log WHERE conversation_id = %s", (conv,))
        assert list(rows.admin_user_id) == [ADMIN.user_id] and list(rows.action) == ["view"]
    finally:
        db.execute("DELETE FROM ai_admin_access_log WHERE conversation_id = %s", (conv,))


# ---- 6. small groups ----------------------------------------------------------------------------

SENSITIVE = sorted(k for k, m in metrics.semantic()["metrics"].items() if m.get("sensitive"))
ORDER_DIMS = sorted(k for k, d in metrics.semantic()["dimensions"].items() if "order" in d["grains"])


@pytest.mark.parametrize("metric", SENSITIVE)
@pytest.mark.parametrize("group_by", [[d] for d in ORDER_DIMS] + [["buyer_state", "payment_type"]],
                         ids=lambda g: "+".join(g))
def test_sensitive_metrics_never_describe_a_tiny_group(metric, group_by):
    small = metrics.semantic()["min_group"]
    for period in ("2025", "2026-10"):   # a short period makes small groups likely (daily points need a bounded period)
        r = call_admin_tool(ADMIN, "admin_metric", {"metric": metric, "group_by": group_by, "period": period})
        for row in r["rows"]:
            if row["orders"] < small:
                assert row["value"] is None and "change" not in row, (metric, group_by, period, row)


# ---- 7. untrusted text ---------------------------------------------------------------------------


def test_review_text_comes_back_marked_untrusted():
    r = call_admin_tool(ADMIN, "look_up_record", {"kind": "order", "record_id": REVIEWED_ORDER})
    comments = [rv["comment"] for rv in r["reviews"] if rv["comment"]]
    assert comments and all(set(c) == {"untrusted_text"} for c in comments)


# ---- 8. SQL fallback attacks ---------------------------------------------------------------------


@pytest.mark.parametrize("sql", [
    "SELECT order_id FROM platform_orders UNION SELECT customer_unique_id FROM customers",
    "SELECT * FROM information_schema.tables",
    "SELECT * FROM platform_orders, mysql.user",
    "SELECT @@version FROM platform_orders",
    "SELECT * FROM platform_orders INTO OUTFILE '/tmp/x'",
    "SELECT LOAD_FILE('/etc/passwd') FROM platform_orders",
    "SELECT * FROM orders",                                     # the real table, not the safe one
    "SELECT (SELECT username FROM app_users LIMIT 1) FROM platform_orders",
    "SELECT * FROM platform_orders WHERE order_id IN (SELECT order_id FROM payments)",
    "SELECT * FROM platform_orders FOR UPDATE",
    "SELECT * FROM platform_orders; DROP TABLE orders",
    "SELECT order_id /* hidden */ FROM platform_orders",
])
def test_sql_fallback_attacks_are_refused(sql):
    with pytest.raises(ToolError):
        call_admin_tool(ADMIN, "query_platform_data", {"sql": sql})


def test_sql_fallback_refuses_row_dumps():
    with pytest.raises(ToolError, match="more than 200 rows"):
        call_admin_tool(ADMIN, "query_platform_data", {"sql": "SELECT order_id FROM platform_orders"})

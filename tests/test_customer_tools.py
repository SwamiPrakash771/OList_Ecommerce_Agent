"""Customer support tools: correctness on the demo scenarios, and permission guarantees.

The permission tests are the important ones: no tool may return another customer's data,
no output may contain fields the catalog hides from customers, and write tools follow
the undo / confirm rules.
"""

import pytest

from datetime import datetime

import ai.tools  # noqa: F401  (registers the tools)
import db
from ai.clock import shifted
from ai import store
from ai.security import (
    ConfirmationRequired, InvalidArguments, NotFound, Principal, ToolError, ToolKind, call_tool,
    in_conversation, preview_tool, tools_for,
)

C1 = Principal(role="customer", user_id=9_100_001, username="c1", customer_unique_id="8d50f5eadf50201ccdcedfb9e2ac8455")
C3 = Principal(role="customer", user_id=9_100_003, username="c3", customer_unique_id="ca77025e7201e3b30c44b472ff346268")
C5 = Principal(role="customer", user_id=9_100_005, username="c5", customer_unique_id="6469f99c1f9dfae7733b25662e7f1782")
RECENT = Principal(role="customer", user_id=9_100_007, username="r", customer_unique_id="fc0bcca3d8d90a812b707bb8e45069d6")
FAKE = Principal(role="customer", user_id=9_100_009, username="f", customer_unique_id="f" * 32)
GUEST = Principal.guest()
SELLER = Principal(role="seller", user_id=9_100_010, username="s", seller_id="6560211a19b47992c3666cc44a7e94c0")
ADMIN = Principal(role="admin", user_id=9_100_011, username="a")

C1_REFUND_ORDER = "a1a6eadae352582010f449d8527b55f3"   # stuck refund (demo customer 1)
C5_FAILED_ORDER = "ccb1046449f1a494de800567e7cb5979"   # failed refund (demo customer 5)
C3_REJECTED_ORDER = "8dacd1da7c4dc40d5c59e7ddd93b3ac8"  # rejected return (demo customer 3)
RECENT_ORDER = "7e708aed151d6a8601ce8f2eaa712bf4"      # delivered on the dataset's last day
PRODUCT = "422879e10f46682990de24d770e7f83d"

# Fields the catalog hides from customers and guests; they must never appear in any tool output.
FORBIDDEN_KEYS = {"seller_id", "main_seller_id", "customer_id", "customer_unique_id", "password_hash",
                  "username", "user_id", "review_id"}


def keys_in(value) -> set[str]:
    if isinstance(value, dict):
        return set(value) | set().union(*(keys_in(v) for v in value.values())) if value else set()
    if isinstance(value, list):
        return set().union(*(keys_in(v) for v in value)) if value else set()
    return set()


@pytest.fixture(autouse=True)
def cleanup():
    started = db.query_one("SELECT NOW() AS t")["t"]
    yield
    db.execute("DELETE FROM cart_items WHERE customer_unique_id = %s", (FAKE.customer_unique_id,))
    db.execute("DELETE FROM customer_addresses WHERE customer_unique_id = %s", (FAKE.customer_unique_id,))
    db.execute("DELETE FROM returns WHERE source = 'app' AND order_id = %s", (RECENT_ORDER,))
    # Only tickets the agent created during this test (demo customers may have real ones).
    db.execute("DELETE FROM support_tickets WHERE source = 'ai_agent' AND created_at >= %s "
               "AND customer_unique_id IN (%s, %s)", (started, C1.customer_unique_id, FAKE.customer_unique_id))
    db.execute("DELETE FROM ai_conversations WHERE owner_user_id BETWEEN 9100000 AND 9100099")


# ---- which tools each role gets -------------------------------------------------------------


def test_tool_sets_per_role():
    guest = {t.name for t in tools_for("guest")}
    customer = {t.name for t in tools_for("customer")}
    assert guest == {"find_products"}
    assert guest < customer
    assert {"look_up_order", "my_account", "update_cart", "transfer_to_human"} <= customer
    assert len(customer) == 9   # consolidated from 17 (docs/CUSTOMER_TOOLS.md)
    assert not {t.name for t in tools_for("seller")} & customer
    assert not {t.name for t in tools_for("admin")} & customer


def test_write_tools_have_the_right_safety_level():
    kinds = {t.name: t.kind for t in tools_for("customer")}
    assert kinds["update_cart"] is ToolKind.WRITE_UNDO
    for name in ("update_address", "request_return", "open_ticket", "transfer_to_human"):
        assert kinds[name] is ToolKind.WRITE_CONFIRM, name
    reads = [n for n, k in kinds.items() if k is ToolKind.READ]
    assert sorted(reads) == ["find_products", "look_up_order", "my_account", "query_my_data"]


# ---- permission guarantees ---------------------------------------------------------------------


@pytest.mark.parametrize("about", ["everything", "tracking", "returns", "refunds"])
def test_other_customers_orders_look_like_missing_orders(about):
    with pytest.raises(NotFound) as other:
        call_tool(C3, "look_up_order", {"order_id": C1_REFUND_ORDER, "about": about})
    with pytest.raises(NotFound) as missing:
        call_tool(C3, "look_up_order", {"order_id": "0" * 32, "about": about})
    assert str(other.value) == str(missing.value)  # no hint that the order exists


@pytest.mark.parametrize("tool_name,args", [
    ("request_return", {"order_id": C1_REFUND_ORDER, "reason": "damaged"}),
    ("open_ticket", {"category": "refund", "description": "where is my refund please", "order_id": C1_REFUND_ORDER}),
    ("transfer_to_human", {"reason": "refund issue", "summary": "Customer asks about a refund on this order.",
                           "order_id": C1_REFUND_ORDER}),
])
def test_actions_on_other_customers_orders_are_refused(tool_name, args):
    with pytest.raises(NotFound):
        preview_tool(C3, tool_name, args)
    with pytest.raises(NotFound):
        call_tool(C3, tool_name, args, confirmed=True)


def test_guests_sellers_and_admins_cannot_use_customer_tools():
    for p in (GUEST, SELLER, ADMIN):
        with pytest.raises(ToolError, match="not available"):
            call_tool(p, "my_account", {"view": "orders"})


def test_no_tool_output_contains_hidden_fields():
    outputs = [
        call_tool(C1, "my_account", {"view": "orders", "limit": 20}),
        call_tool(C1, "look_up_order", {"about": "tracking", "order_id": C1_REFUND_ORDER}),
        call_tool(C1, "look_up_order", {"about": "returns", "order_id": C1_REFUND_ORDER}),
        call_tool(C1, "look_up_order", {"about": "refunds", "order_id": C1_REFUND_ORDER}),
        call_tool(C1, "my_account", {"view": "cart"}),
        call_tool(C1, "my_account", {"view": "address"}),
        call_tool(C1, "my_account", {"view": "tickets"}),
        call_tool(GUEST, "find_products", {"query": "watch"}),
        call_tool(GUEST, "find_products", {"view": "details", "product_id": PRODUCT}),
        call_tool(GUEST, "find_products", {"view": "categories"}),
    ]
    for out in outputs:
        assert not keys_in(out) & FORBIDDEN_KEYS, keys_in(out) & FORBIDDEN_KEYS


def test_review_text_is_marked_untrusted():
    d = call_tool(GUEST, "find_products", {"view": "details", "product_id": PRODUCT})
    for r in d["recent_reviews"]:
        assert set(r["comment"]) == {"untrusted_text"}


def test_bad_arguments_are_rejected():
    with pytest.raises(InvalidArguments):
        call_tool(C1, "look_up_order", {"about": "tracking", "order_id": "short"})
    with pytest.raises(InvalidArguments):
        call_tool(C1, "my_account", {"view": "orders", "limit": 500})
    with pytest.raises(InvalidArguments):
        call_tool(C1, "look_up_order", {"about": "tracking", "order_id": C1_REFUND_ORDER, "customer_unique_id": C3.customer_unique_id})


# ---- the support scenarios ------------------------------------------------------------------------


def test_stuck_refund_is_reported_with_its_sla():
    r = call_tool(C1, "look_up_order", {"about": "refunds", "order_id": C1_REFUND_ORDER})["refunds"][0]
    assert r["status"] == "processing" and r["payment_method"] == "credit_card" and r["amount"] == 96.4
    expected_by = shifted(datetime(2018, 9, 25)).strftime("%Y-%m-%d")   # original timeline, moved with the data
    assert r["sla"] == {"expected_within_days": 12, "days_in_processing": 34, "breached": True, "expected_by": expected_by}
    assert [h["status"] for h in r["history"]] == ["pending", "processing"]


def test_failed_refund_and_rejected_return():
    failed = call_tool(C5, "look_up_order", {"about": "refunds", "order_id": C5_FAILED_ORDER})["refunds"][0]
    assert failed["status"] == "failed" and "Card account closed" in failed["failure_reason"]
    rejected = call_tool(C3, "look_up_order", {"about": "returns", "order_id": C3_REJECTED_ORDER})["returns"][0]
    assert rejected["status"] == "rejected" and "7 days" in rejected["decision_note"]


def test_tracking_groups_quantities_and_reports_delivery():
    t = call_tool(C1, "look_up_order", {"about": "tracking", "order_id": C1_REFUND_ORDER})
    assert t["delivery"] == {"state": "delivered_on_time"}
    assert t["timeline"]["delivered"] == shifted(datetime(2018, 8, 24, 17, 26)).strftime("%Y-%m-%d %H:%M")
    assert sum(i["quantity"] for i in t["items"]) >= 1
    orders = call_tool(C1, "my_account", {"view": "orders", "limit": 20})
    assert orders["total_orders"] >= 17
    assert any(o["has_return"] for o in orders["orders"] if o["order_id"] == C1_REFUND_ORDER)


# ---- write tools ------------------------------------------------------------------------------------


def test_cart_changes_apply_immediately_with_undo():
    added = call_tool(FAKE, "update_cart", {"mode": "add", "product_id": PRODUCT, "quantity": 2})
    assert added["cart"]["items"][0]["quantity"] == 2
    assert added["undo"] == {"tool": "update_cart", "args": {"product_id": PRODUCT, "quantity": 0, "mode": "set"}}
    call_tool(FAKE, added["undo"]["tool"], added["undo"]["args"])  # the undo button
    assert call_tool(FAKE, "my_account", {"view": "cart"})["items"] == []
    with pytest.raises(NotFound):
        call_tool(FAKE, "update_cart", {"mode": "add", "product_id": "0" * 32})


def test_address_change_needs_confirmation_and_validates():
    args = {"recipient_name": "Ana Lima", "street": "Rua das Flores", "number": "12", "city": "Recife",
            "state": "pe", "zip_code": "50000000"}
    with pytest.raises(ConfirmationRequired):
        call_tool(FAKE, "update_address", args)
    card = preview_tool(FAKE, "update_address", args)
    assert card["facts"] == {"city": "Recife", "state": "PE", "zip_code": "50000-000"}
    assert db.query_one("SELECT 1 AS x FROM customer_addresses WHERE customer_unique_id = %s", (FAKE.customer_unique_id,)) is None
    with pytest.raises(ToolError, match="CEP"):
        preview_tool(FAKE, "update_address", {**args, "zip_code": "123"})
    saved = call_tool(FAKE, "update_address", args, confirmed=True)
    assert saved == {"saved": True, "city": "Recife", "state": "PE", "zip_code": "50000-000"}


def test_return_request_follows_the_policy():
    with pytest.raises(ToolError, match="7 days"):
        preview_tool(C1, "request_return", {"order_id": C1_REFUND_ORDER, "reason": "changed_mind"})
    card = preview_tool(RECENT, "request_return", {"order_id": RECENT_ORDER, "reason": "changed_mind"})
    assert card["facts"]["items"] == 1
    done = call_tool(RECENT, "request_return", {"order_id": RECENT_ORDER, "reason": "changed_mind"}, confirmed=True)
    assert done["status"] == "requested"
    with pytest.raises(ToolError, match="already have a return"):
        call_tool(RECENT, "request_return", {"order_id": RECENT_ORDER, "reason": "damaged"}, confirmed=True)


def test_transfer_to_human_creates_a_linked_ticket():
    conv = store.create_conversation(C1)
    with in_conversation(conv):
        t = call_tool(C1, "transfer_to_human", {
            "reason": "Refund stuck for 34 days",
            "summary": "Refund 96.40 to credit card for order a1a6eadae352582010f449d8527b55f3 has been processing 34 days (expected 12).",
            "order_id": C1_REFUND_ORDER,
        }, confirmed=True)
    ticket = db.query_one("SELECT * FROM support_tickets WHERE ticket_id = %s", (t["ticket_id"],))
    assert ticket["source"] == "ai_agent" and ticket["conversation_id"] == conv and ticket["issue_type"] == "human_request"
    assert "34 days" in ticket["ai_summary"]
    c = db.query_one("SELECT status, retention_class, ticket_id FROM ai_conversations WHERE conversation_id = %s", (conv,))
    assert c == {"status": "escalated", "retention_class": "ticket_linked", "ticket_id": t["ticket_id"]}
    assert call_tool(C1, "my_account", {"view": "tickets"})["tickets"][0]["ticket_id"] == t["ticket_id"]

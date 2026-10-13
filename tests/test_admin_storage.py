"""Admin agent, step 4: admin chats in the shared ai_* tables, and the history the admin UI shows."""

from datetime import datetime, timedelta

import pytest

import db
from ai import store
from ai.security import Principal

ADMIN = Principal(role="admin", user_id=9_100_071, username="admin-test")
OTHER_ADMIN = Principal(role="admin", user_id=9_100_072, username="admin-other")
SELLER_A = Principal(role="seller", user_id=9_100_061, username="a", seller_id="6560211a19b47992c3666cc44a7e94c0")


@pytest.fixture(autouse=True)
def cleanup():
    yield
    db.execute("DELETE FROM ai_conversations WHERE owner_user_id IN (%s, %s) AND owner_role = 'admin'",
               (ADMIN.user_id, OTHER_ADMIN.user_id))


def _chat(principal, title, turns):
    """A stored admin chat: [(question, [(tool, args)], answer), ...]."""
    with db.as_admin_agent():
        conv = store.create_conversation(principal, title=title)
        for question, calls, answer in turns:
            qid = store.append_message(conv, "user", {"text": question})
            for tool, args in calls:
                store.record_tool_call(conv, tool, "read", args, "ok", message_id=qid, duration_ms=12)
            store.append_message(conv, "assistant", {"text": answer}, model="test-model")
    return conv


def test_admin_chats_are_stored_on_the_admin_agents_user_and_kept_a_year():
    conv = _chat(ADMIN, "Revenue by month", [("Revenue by month in 2026?", [], "Here it is.")])
    row = db.query_one("SELECT owner_role, retention_class FROM ai_conversations WHERE conversation_id = %s", (conv,))
    assert row == {"owner_role": "admin", "retention_class": "admin_audit"}


def test_each_answer_shows_the_lookups_run_for_its_question():
    conv = _chat(ADMIN, "Sellers", [
        ("Top sellers in 2026?", [("admin_metric", {"metric": "item_revenue", "group_by": "seller", "period": "2026"})],
         "The top seller is seller-1."),
        ("And late deliveries?", [("admin_metric", {"metric": "late_delivery_rate", "period": "2026"}),
                                  ("late_orders", {"days": 30})], "6.8% were late."),
        ("Thanks", [], "You're welcome."),
    ])
    chat = store.admin_transcript(ADMIN, conv)
    answers = [m for m in chat["messages"] if m["role"] == "assistant"]
    assert [len(a["lookups"]) for a in answers] == [1, 2, 0]
    first = answers[0]["lookups"][0]
    assert first["tool"] == "admin_metric" and first["args"]["group_by"] == "seller" and first["status"] == "ok"
    assert [lk["tool"] for lk in answers[1]["lookups"]] == ["admin_metric", "late_orders"]
    assert "lookups" not in chat["messages"][0]          # questions don't carry lookups
    assert chat["retention_class"] == "admin_audit"


def test_lookups_without_a_question_link_are_placed_by_time():
    conv = _chat(ADMIN, "Old chat", [("Stuck refunds?", [], "There are 9.")])
    question_time = db.query_one("SELECT created_at FROM ai_messages WHERE conversation_id = %s AND role = 'user'",
                                 (conv,))["created_at"]
    db.execute("INSERT INTO ai_tool_calls (conversation_id, tool_name, kind, args, status, created_at) "
               "VALUES (%s, 'stuck_refunds', 'read', '{}', 'ok', %s)", (conv, question_time))
    answer = store.admin_transcript(ADMIN, conv)["messages"][1]
    assert [lk["tool"] for lk in answer["lookups"]] == ["stuck_refunds"]


def test_each_admin_sees_only_their_own_history():
    mine = _chat(ADMIN, "Mine", [("GMV 2026?", [], "$8.26M")])
    theirs = _chat(OTHER_ADMIN, "Theirs", [("GMV 2025?", [], "$7.42M")])
    assert [c["conversation_id"] for c in store.admin_conversations(ADMIN)] == [mine]
    assert store.admin_transcript(ADMIN, theirs) is None
    assert store.admin_transcript(OTHER_ADMIN, mine) is None


def test_history_lists_newest_first_with_question_counts_and_pages_back():
    old = _chat(ADMIN, "Old", [("Q1", [], "A1")])
    db.execute("UPDATE ai_conversations SET last_message_at = %s WHERE conversation_id = %s",
               (datetime.now() - timedelta(days=3), old))
    new = _chat(ADMIN, "New", [("Q1", [], "A1"), ("Q2", [], "A2")])
    rows = store.admin_conversations(ADMIN)
    assert [r["conversation_id"] for r in rows] == [new, old]
    assert [r["questions"] for r in rows] == [2, 1]
    assert [r["conversation_id"] for r in store.admin_conversations(ADMIN, limit=1)] == [new]
    page2 = store.admin_conversations(ADMIN, before=rows[0]["last_message_at"])
    assert [r["conversation_id"] for r in page2] == [old]


def test_search_matches_titles_and_questions_only():
    refunds = _chat(ADMIN, "Refund check", [("Which refunds are stuck?", [], "Nine, worth $1,325.05")])
    sellers = _chat(ADMIN, "Seller review", [("Worst on-time seller with 50+ orders?", [], "seller-54965bbe")])
    assert [r["conversation_id"] for r in store.admin_conversations(ADMIN, search="refund")] == [refunds]
    assert [r["conversation_id"] for r in store.admin_conversations(ADMIN, search="on-time")] == [sellers]
    assert store.admin_conversations(ADMIN, search="1,325") == []      # answers aren't searched
    assert store.admin_conversations(ADMIN, search="%") == []          # wildcards are literal
    assert len(store.admin_conversations(ADMIN, search="  ")) == 2


def test_admin_chats_cannot_be_deleted():
    conv = _chat(ADMIN, "Keep", [("Q", [], "A")])
    with pytest.raises(PermissionError, match="audit trail"):
        store.delete_conversation(ADMIN, conv)
    assert store.admin_transcript(ADMIN, conv) is not None


@pytest.mark.parametrize("who", [SELLER_A, Principal.guest(), Principal(role="admin")])
def test_only_signed_in_admins_have_an_admin_history(who):
    with pytest.raises(PermissionError):
        store.admin_conversations(who)
    with pytest.raises(PermissionError):
        store.admin_transcript(who, "f" * 32)

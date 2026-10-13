"""Storage (ai/store.py) and scheduler (ai/scheduler.py) tests.

Uses throwaway user ids (9_000_000+) and cleans up after itself.
"""

import json
import uuid
from datetime import datetime, timedelta

import pymysql
import pytest

import db
from ai import scheduler, store
from ai.security import Principal

ALICE = Principal(role="customer", user_id=9_000_001, username="alice", customer_unique_id="a" * 32)
BOB = Principal(role="customer", user_id=9_000_002, username="bob", customer_unique_id="b" * 32)
SELLER = Principal(role="seller", user_id=9_000_003, username="s", seller_id="s" * 32)
ADMIN = Principal(role="admin", user_id=9_000_004, username="adm")
GUEST = Principal.guest()


@pytest.fixture(autouse=True)
def cleanup():
    yield
    ids = (9_000_001, 9_000_002, 9_000_003, 9_000_004)
    db.execute(f"DELETE FROM ai_conversations WHERE owner_user_id IN {ids} OR session_id LIKE 'test%%'")
    db.execute(f"DELETE FROM ai_usage WHERE user_id IN {ids} OR subject LIKE 'guest:test%%'")
    db.execute(f"DELETE FROM ai_usage_daily WHERE subject IN ('user:9000001', 'user:9000002') OR subject LIKE 'guest:test%%'")
    db.execute("DELETE FROM ai_limits WHERE scope LIKE 'user:900000%%'")
    db.execute(f"DELETE FROM ai_admin_access_log WHERE admin_user_id IN {ids}")


# ---- conversations ------------------------------------------------------------------------


def test_conversations_are_private_to_their_owner():
    conv = store.create_conversation(ALICE, title="Where is my order?")
    assert store.get_conversation(ALICE, conv)["conversation_id"] == conv
    assert store.get_conversation(BOB, conv) is None
    assert store.get_conversation(SELLER, conv) is None
    assert store.get_conversation(GUEST, conv, session_id="test-session") is None
    assert [c["conversation_id"] for c in store.list_conversations(ALICE)] == [conv]
    assert store.list_conversations(BOB) == []
    assert store.delete_conversation(BOB, conv) is False


def test_guest_conversations_belong_to_their_session():
    conv = store.create_conversation(GUEST, session_id="test-s1")
    assert store.get_conversation(GUEST, conv, session_id="test-s1") is not None
    assert store.get_conversation(GUEST, conv, session_id="test-s2") is None
    assert store.get_conversation(GUEST, conv) is None
    with pytest.raises(ValueError):
        store.create_conversation(GUEST)


def test_messages_are_ordered_and_deletion_hides_the_chat():
    conv = store.create_conversation(ALICE)
    store.append_message(conv, "user", {"text": "hi"})
    store.append_message(conv, "assistant", {"text": "hello"}, model="m", input_tokens=10, output_tokens=5)
    hist = store.history(conv)
    assert [(m["sequence_no"], m["role"], m["content"]["text"]) for m in hist] == [(1, "user", "hi"), (2, "assistant", "hello")]
    assert store.delete_conversation(ALICE, conv) is True
    assert store.get_conversation(ALICE, conv) is None
    assert store.list_conversations(ALICE) == []


# ---- pending actions -----------------------------------------------------------------------


def test_pending_actions_are_idempotent_and_single_use():
    conv = store.create_conversation(ALICE)
    args = {"city": "Recife"}
    a1 = store.create_pending_action(ALICE, conv, "update_address", args, {"summary": "Change address"})
    a2 = store.create_pending_action(ALICE, conv, "update_address", args, {"summary": "Change address"})
    assert a1["action_id"] == a2["action_id"]  # double proposal -> one action
    assert store.claim_pending_action(BOB, a1["action_id"], approve=True) is None  # not Bob's
    claimed = store.claim_pending_action(ALICE, a1["action_id"], approve=True)
    assert claimed["status"] == "confirmed" and claimed["args"] == args
    assert store.claim_pending_action(ALICE, a1["action_id"], approve=True) is None  # double click
    store.finish_pending_action(a1["action_id"], True, {"ok": True})
    assert db.query_one("SELECT status FROM ai_pending_actions WHERE action_id = %s", (a1["action_id"],))["status"] == "executed"


def test_expired_actions_cannot_be_confirmed_and_are_swept():
    conv = store.create_conversation(ALICE)
    a = store.create_pending_action(ALICE, conv, "open_ticket", {"x": 1}, {"summary": "Open ticket"})
    db.execute("UPDATE ai_pending_actions SET expires_at = %s WHERE action_id = %s",
               (datetime.now() - timedelta(minutes=1), a["action_id"]))
    assert store.claim_pending_action(ALICE, a["action_id"], approve=True) is None
    scheduler.sync_jobs()
    assert scheduler.run_job("pending_action_expiry", "manual") == "success"
    assert db.query_one("SELECT status FROM ai_pending_actions WHERE action_id = %s", (a["action_id"],))["status"] == "expired"


def test_guests_cannot_create_actions():
    conv = store.create_conversation(GUEST, session_id="test-s3")
    with pytest.raises(ValueError):
        store.create_pending_action(GUEST, conv, "update_cart", {}, {})


# ---- usage and limits ------------------------------------------------------------------------


def test_usage_is_counted_per_subject():
    subject = store.subject_for(ALICE)
    before = store.usage_today(subject)
    store.record_usage(subject, "customer", "test-model", 1000, 200, 800, 0.01, user_id=ALICE.user_id)
    store.record_usage(subject, "customer", "test-model", 500, 100, 0, 0.005, user_id=ALICE.user_id)
    after = store.usage_today(subject)
    assert after["requests"] - before["requests"] == 2
    assert after["input_tokens"] - before["input_tokens"] == 1500
    assert store.requests_last_minute(subject) >= 2
    assert store.usage_today(store.subject_for(BOB))["requests"] == 0
    assert store.subject_for(GUEST, "test-x") == "guest:test-x"


def test_limits_merge_role_defaults_with_user_overrides():
    base = store.effective_limits(ALICE)
    assert base.messages_per_day == 50 and not base.blocked
    db.execute("INSERT INTO ai_limits (scope, messages_per_day, blocked, updated_at) VALUES ('user:9000001', 5, 0, NOW())")
    over = store.effective_limits(ALICE)
    assert over.messages_per_day == 5 and over.max_reply_tokens == base.max_reply_tokens
    db.execute("UPDATE ai_limits SET blocked = 1 WHERE scope = 'user:9000001'")
    assert store.effective_limits(ALICE).blocked
    assert not store.effective_limits(BOB).blocked


# ---- feedback and admin access ---------------------------------------------------------------


def test_feedback_only_on_own_assistant_messages():
    conv = store.create_conversation(ALICE)
    user_msg = store.append_message(conv, "user", {"text": "q"})
    reply = store.append_message(conv, "assistant", {"text": "a"})
    assert store.add_feedback(ALICE, reply, "down", "wrong date") is True
    assert store.add_feedback(BOB, reply, "up") is False
    assert store.add_feedback(ALICE, user_msg, "up") is False


def test_admin_access_needs_admin_and_reason():
    conv = store.create_conversation(ALICE)
    with pytest.raises(PermissionError):
        store.log_admin_access(BOB, conv, "view", "curious about it")
    with pytest.raises(ValueError):
        store.log_admin_access(ADMIN, conv, "view", "")
    store.log_admin_access(ADMIN, conv, "view", "Ticket follow-up for refund complaint")
    assert db.query_one("SELECT COUNT(*) AS n FROM ai_admin_access_log WHERE conversation_id = %s", (conv,))["n"] == 1


# ---- the agent database user cannot rewrite history -------------------------------------------


@pytest.mark.parametrize("sql", [
    "DELETE FROM ai_messages WHERE message_id = -1",
    "UPDATE ai_messages SET content = '{}' WHERE message_id = -1",
    "UPDATE ai_usage SET cost = 0 WHERE request_id = 'x'",
    "DELETE FROM ai_conversations WHERE conversation_id = 'x'",
    "UPDATE ai_limits SET blocked = 0 WHERE scope = 'x'",
    "UPDATE ai_settings SET value = 'true' WHERE setting_key = 'x'",
    "UPDATE ai_admin_access_log SET reason = 'x' WHERE log_id = -1",
    "UPDATE ai_tool_calls SET status = 'ok' WHERE call_id = -1",
    "UPDATE scheduler_jobs SET enabled = 0 WHERE job_name = 'x'",
])
def test_agent_cannot_rewrite_or_delete_ai_records(sql):
    with pytest.raises(pymysql.MySQLError):
        with db.as_agent():
            db.execute(sql)


# ---- scheduler -----------------------------------------------------------------------------------


def test_schedule_parsing():
    t = datetime(2026, 1, 1, 10, 0, 0)
    assert scheduler.next_run("every:5m", t) == datetime(2026, 1, 1, 10, 5)
    assert scheduler.next_run("every:1h", t) == datetime(2026, 1, 1, 11, 0)
    assert scheduler.next_run("daily@02:00", t) == datetime(2026, 1, 2, 2, 0)
    assert scheduler.next_run("daily@11:30", t) == datetime(2026, 1, 1, 11, 30)
    with pytest.raises(ValueError):
        scheduler.next_run("sometimes", t)


@pytest.fixture
def temp_job(monkeypatch):
    """Register a throwaway job; remove it and its history afterwards."""
    name = f"test_job_{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(scheduler, "RETRY_BACKOFF_S", (0, 0))
    calls = {"n": 0}

    def make(func):
        scheduler.JOBS[name] = scheduler.Job(name, "test", "every:1h", func, timeout_s=60, max_retries=2)
        scheduler.sync_jobs()
        return name

    yield make, calls
    scheduler.JOBS.pop(name, None)
    db.execute("DELETE FROM scheduler_runs WHERE job_name = %s", (name,))
    db.execute("DELETE FROM scheduler_state WHERE job_name = %s", (name,))
    db.execute("DELETE FROM scheduler_jobs WHERE job_name = %s", (name,))


def test_failed_job_is_retried_with_linked_runs(temp_job):
    make, calls = temp_job

    def flaky(ctx):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("temporary failure")
        ctx.set_state({"done": True})
        return scheduler.JobResult(rows_processed=7, summary="ok on third try")

    name = make(flaky)
    assert scheduler.run_job(name, "manual") == "success"
    runs = db.query_df("SELECT run_id, status, attempt_no, retry_of_run_id, triggered_by FROM scheduler_runs "
                       "WHERE job_name = %s ORDER BY run_id", (name,)).to_dict("records")
    assert [r["status"] for r in runs] == ["failed", "failed", "success"]
    assert [r["attempt_no"] for r in runs] == [1, 2, 3]
    assert runs[1]["retry_of_run_id"] == runs[0]["run_id"] and runs[2]["retry_of_run_id"] == runs[1]["run_id"]
    assert [r["triggered_by"] for r in runs] == ["manual", "retry", "retry"]
    state = db.query_one("SELECT watermark FROM scheduler_state WHERE job_name = %s", (name,))
    assert json.loads(state["watermark"]) == {"done": True}


def test_job_never_runs_twice_at_once(temp_job):
    make, _ = temp_job
    name = make(lambda ctx: scheduler.JobResult(summary="ran"))
    holder = db.connect()
    try:
        with holder.cursor() as cur:
            cur.execute("SELECT GET_LOCK(%s, 0) AS got", (f"olist_job:{name}",))
            assert cur.fetchone()["got"] == 1
        assert scheduler.run_job(name, "manual") == "skipped"
    finally:
        holder.close()  # releases the lock
    assert scheduler.run_job(name, "manual") == "success"


def test_retention_purge_respects_classes():
    now = datetime.now().replace(microsecond=0)
    old_std = store.create_conversation(ALICE)
    old_ticket = store.create_conversation(ALICE)
    recent = store.create_conversation(ALICE)
    deleted = store.create_conversation(BOB)
    db.execute("UPDATE ai_conversations SET last_message_at = %s WHERE conversation_id IN (%s, %s)",
               (now - timedelta(days=120), old_std, old_ticket))
    store.link_ticket(old_ticket, "ticket-1")
    db.execute("UPDATE ai_conversations SET deleted_at = %s WHERE conversation_id = %s",
               (now - timedelta(days=40), deleted))
    scheduler.sync_jobs()
    assert scheduler.run_job("retention_purge", "manual") == "success"
    remaining = set(db.query_df("SELECT conversation_id FROM ai_conversations WHERE conversation_id IN (%s, %s, %s, %s)",
                                (old_std, old_ticket, recent, deleted)).conversation_id)
    assert remaining == {old_ticket, recent}

"""Admin agent, step 7: its own engine (ai/admin/agent.py) and API. Scripted models, no LLM calls."""

import asyncio
import json
import subprocess
import sys
import time

import jwt
import pytest
from fastapi.testclient import TestClient
from pydantic_ai.messages import ModelRequest, ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

import db
from ai.admin import agent as admin_agent
from ai.admin import facts
from ai.security import Principal

ADMIN = Principal(role="admin", user_id=9_100_091, username="admin-agent-test")
OTHER_ADMIN = Principal(role="admin", user_id=9_100_092, username="admin-other-test")
SELLER = Principal(role="seller", user_id=9_100_093, username="s", seller_id="6560211a19b47992c3666cc44a7e94c0")
DEMO_ORDER = "a1a6eadae352582010f449d8527b55f3"


@pytest.fixture(scope="module", autouse=True)
def reporting_tables():
    if facts.refreshed_at() is None:
        facts.refresh()


@pytest.fixture(autouse=True)
def clean():
    for p in (ADMIN, OTHER_ADMIN):
        db.execute("REPLACE INTO ai_limits (scope, messages_per_day, input_tokens_per_day, output_tokens_per_day, "
                   "max_reply_tokens, requests_per_minute, max_tool_calls_per_turn, blocked, updated_at) "
                   "VALUES (%s, 100000, 100000000, 100000000, 1000, 100000, 10, 0, NOW())", (f"user:{p.user_id}",))
    yield
    for p in (ADMIN, OTHER_ADMIN):
        db.execute("DELETE FROM ai_conversations WHERE owner_user_id = %s AND owner_role = 'admin'", (p.user_id,))
        db.execute("DELETE FROM ai_admin_access_log WHERE admin_user_id = %s", (p.user_id,))
        for t in ("ai_usage", "ai_usage_daily"):
            db.execute(f"DELETE FROM {t} WHERE subject = %s", (f"user:{p.user_id}",))
        db.execute("DELETE FROM ai_limits WHERE scope = %s", (f"user:{p.user_id}",))


def scripted(plan):
    """A model whose step is plan(messages): a list of (tool, args) calls, or reply text."""
    calls = {"n": 0}

    async def stream(messages, info):
        calls["n"] += 1
        step = plan(messages)
        if isinstance(step, str):
            yield step
        else:
            yield {i: DeltaToolCall(name=name, json_args=json.dumps(args), tool_call_id=f"call{calls['n']}_{i}")
                   for i, (name, args) in enumerate(step)}

    model = FunctionModel(stream_function=stream)
    model.calls = calls
    return model


def returns(messages) -> list[ToolReturnPart]:
    return [p for m in messages for p in getattr(m, "parts", []) if isinstance(p, ToolReturnPart)]


def run(principal, message, model, **kw) -> list[dict]:
    async def go():
        return [e async for e in admin_agent.admin_chat_turn(principal, message, model=model, **kw)]
    return asyncio.run(go())


def text_of(events) -> str:
    out = ""
    for e in events:
        out = "" if e["type"] == "reset" else out + e.get("delta", "") if e["type"] == "text" else out
    return out


def revenue_plan(messages):
    r = returns(messages)
    if not r:
        return [("admin_metric", {"metric": "item_revenue", "period": "2025"})]
    total = r[-1].content["total"]
    return f"Item revenue in 2025 was ${total:,.2f} (about ${total / 1e6:.2f}M), excluding canceled orders."


# ---- the engine ---------------------------------------------------------------------------------


def test_a_turn_uses_admin_tools_and_is_stored_as_an_audit_chat():
    events = run(ADMIN, "item revenue in 2025?", scripted(revenue_plan))
    assert any(e["type"] == "step" and e["label"] == "Calculating figures" for e in events)
    assert "$6,393,604.14" in text_of(events) and "$6.39M" in text_of(events)
    conv = next(e["conversation_id"] for e in events if e["type"] == "conversation")
    row = db.query_one("SELECT owner_role, owner_user_id, retention_class FROM ai_conversations WHERE conversation_id = %s",
                       (conv,))
    assert row == {"owner_role": "admin", "owner_user_id": ADMIN.user_id, "retention_class": "admin_audit"}


def test_lookups_are_linked_to_their_question_and_shown_under_the_answer():
    events = run(ADMIN, "item revenue in 2025?", scripted(revenue_plan))
    conv = next(e["conversation_id"] for e in events if e["type"] == "conversation")
    from ai import store
    chat = store.admin_transcript(ADMIN, conv)
    answer = [m for m in chat["messages"] if m["role"] == "assistant"][0]
    assert [lk["tool"] for lk in answer["lookups"]] == ["admin_metric"]
    detailed = admin_agent.lookup_details(answer["lookups"])[0]
    assert detailed["label"] == "Calculating figures" and detailed["definition"].startswith("Item revenue")
    linked = db.query_one("SELECT COUNT(*) AS n FROM ai_tool_calls t JOIN ai_messages m ON m.message_id = t.message_id "
                          "WHERE t.conversation_id = %s AND m.role = 'user'", (conv,))["n"]
    assert linked == 1


def test_record_lookups_in_a_chat_are_written_to_the_access_log():
    def plan(messages):
        r = returns(messages)
        if not r:
            return [("look_up_record", {"kind": "order", "record_id": DEMO_ORDER[:8]})]
        return f"Order {DEMO_ORDER} is {r[-1].content['status']}; the customer is {r[-1].content['customer']}."

    events = run(ADMIN, "tell me about order a1a6eada", scripted(plan))
    conv = next(e["conversation_id"] for e in events if e["type"] == "conversation")
    assert "customer-" in text_of(events)
    log = db.query_df("SELECT admin_user_id, action, reason FROM ai_admin_access_log WHERE conversation_id = %s", (conv,))
    assert list(log.admin_user_id) == [ADMIN.user_id] and list(log.action) == ["view"]


def test_made_up_figures_are_sent_back_and_never_shown():
    model = scripted(lambda messages: "GMV this year is $99.9M.")
    events = run(ADMIN, "GMV this year?", model)
    assert "$99.9M" not in text_of(events) and text_of(events) == admin_agent.FALLBACK_REPLY
    assert model.calls["n"] == 1 + admin_agent.OUTPUT_RETRIES


def test_made_up_masked_customers_are_caught():
    model = scripted(lambda messages: "The top buyer is customer-1234abcd.")
    assert "customer-1234abcd" not in text_of(run(ADMIN, "top buyer?", model))


def test_replies_about_internals_are_rewritten():
    replies = iter(["I ran admin_metric on admin_order_facts.", "I calculated it from the orders data."])
    assert text_of(run(ADMIN, "how did you get that?", scripted(lambda m: next(replies)))) == \
        "I calculated it from the orders data."


def test_only_signed_in_admins_can_use_the_admin_engine():
    for who in (SELLER, Principal(role="admin")):
        with pytest.raises(admin_agent.AdminChatUnavailable) as err:
            admin_agent.prepare_admin_turn(who, "hi", model=scripted(lambda m: "hi"))
        assert err.value.status == 403


def test_another_admins_chat_cannot_be_continued():
    events = run(ADMIN, "item revenue in 2025?", scripted(revenue_plan))
    conv = next(e["conversation_id"] for e in events if e["type"] == "conversation")
    with pytest.raises(admin_agent.AdminChatUnavailable) as err:
        admin_agent.prepare_admin_turn(OTHER_ADMIN, "and 2026?", conversation_id=conv, model=scripted(lambda m: "x"))
    assert err.value.status == 404


def test_admin_engine_is_separate_from_the_other_engines():
    code = ("import sys; import ai.admin.agent; "
            "print('ai.agent' in sys.modules, 'ai.seller.agent' in sys.modules, 'ai.tools.customer_tools' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=".").stdout.strip()
    assert out == "False False False"
    assert {t.name for t in admin_agent.build_admin_agent()._function_toolset.tools.values()} == set(admin_agent.STEP_LABELS)


def test_a_chart_is_shown_stored_and_reopened():
    def plan(messages):
        r = returns(messages)
        if not r:
            return [("show_chart", {"metric": "item_revenue", "group_by": ["month"], "period": "2025"})]
        best = max(r[-1].content["data"], key=lambda d: d["s0"])
        return f"Item revenue peaked in {best['x']} at ${best['s0']:,.2f}."

    events = run(ADMIN, "chart item revenue by month in 2025", scripted(plan))
    charts = [e["chart"] for e in events if e["type"] == "chart"]
    assert len(charts) == 1 and charts[0]["kind"] == "line" and len(charts[0]["data"]) == 12
    assert "2025-11" in text_of(events) and "$1,115,409.45" in text_of(events)
    order = [e["type"] for e in events if e["type"] in ("chart", "text")]
    assert order.index("chart") < order.index("text")          # the chart comes before the takeaway
    conv = next(e["conversation_id"] for e in events if e["type"] == "conversation")
    from ai import store
    answer = [m for m in store.admin_transcript(ADMIN, conv)["messages"] if m["role"] == "assistant"][0]
    assert answer["charts"] == charts and answer["lookups"][0]["tool"] == "show_chart"


# ---- the fact check -----------------------------------------------------------------------------

EVIDENCE = [ModelRequest(parts=[ToolReturnPart(tool_name="admin_metric", tool_call_id="c", content={
    "total": 13494652.7, "rows": [{"value": 6.77, "orders": 96470}, {"value": 15739483.04, "change_pct": 12828.3},
             {"change_pct": -9.85}],
    "customer": "customer-83fd2458", "sellers": [1, 2, 3, 4]})])]


@pytest.mark.parametrize("reply", [
    "Item revenue was $13,494,652.70.", "Item revenue was $13.49M.", "Item revenue was $13.5 million.",
    "GMV was 15.7 million.", "Late deliveries: 6.8% of 96,470 delivered orders.", "Order of customer-83fd2458.",
    "These 4 sellers.", "Sellers with at least 50 orders.",
    "Item revenue was $13,494,653.", "GMV was $15,739,483 (vs $13,494,653).",   # rounded to whole dollars
    "December rose +12,828.3% on a thin month.", "December rose 12828.3%.",   # thousands separator
    "June fell -9.9% on May.",   # -9.85 rounded half up (round() gives 9.8)
])
def test_fact_check_accepts_supported_figures(reply):
    assert admin_agent.unsupported_facts(reply, EVIDENCE) == []


@pytest.mark.parametrize("reply, problem", [
    ("GMV was $15.8M.", "$15.8M"), ("Revenue 14.2 million.", "14.2 million"), ("Late: 7.5%.", "7.5%"),
    ("customer-00000000 ordered most.", "customer-00000000"), ("We have 5,000 sellers.", "5000 sellers"),
    ("Revenue was $13,494,650.", "$13,494,650"), ("Average fee $7.", "$7"),   # not a rounding; too small to round
    ("December rose +12,828.9%.", "12,828.9%"),
])
def test_fact_check_catches_unsupported_figures(reply, problem):
    assert problem in admin_agent.unsupported_facts(reply, EVIDENCE)


# ---- the API ------------------------------------------------------------------------------------


@pytest.fixture
def client():
    from api.main import app
    return TestClient(app)


def cookie(principal: Principal) -> dict:
    from api.main import COOKIE_NAME, JWT_SECRET
    claims = {"uid": principal.user_id, "sub": principal.username, "role": principal.role,
              "cid": principal.customer_unique_id, "sid": principal.seller_id, "exp": int(time.time()) + 600}
    return {COOKIE_NAME: jwt.encode(claims, JWT_SECRET, algorithm="HS256")}


def sse(response) -> list[dict]:
    return [json.loads(c[len("data: "):]) for c in response.text.split("\n\n") if c.startswith("data: ")]


def test_api_chat_history_and_feedback(client, monkeypatch):
    monkeypatch.setattr(admin_agent, "configured_model", lambda: scripted(revenue_plan))
    r = client.post("/api/admin/chat", json={"message": "item revenue in 2025?"}, cookies=cookie(ADMIN))
    events = sse(r)
    conv = next(e["conversation_id"] for e in events if e["type"] == "conversation")
    message_id = next(e["message_id"] for e in events if e["type"] == "message")

    listed = client.get("/api/admin/chat/conversations", cookies=cookie(ADMIN)).json()
    assert [c["conversation_id"] for c in listed] == [conv] and listed[0]["questions"] == 1
    assert client.get("/api/admin/chat/conversations?search=revenue", cookies=cookie(ADMIN)).json()[0]["conversation_id"] == conv
    assert client.get("/api/admin/chat/conversations?search=refunds", cookies=cookie(ADMIN)).json() == []

    chat = client.get(f"/api/admin/chat/conversations/{conv}", cookies=cookie(ADMIN)).json()
    answer = [m for m in chat["messages"] if m["role"] == "assistant"][0]
    assert answer["lookups"][0]["definition"].startswith("Item revenue")

    assert client.post("/api/admin/chat/feedback", json={"message_id": message_id, "rating": "up"},
                       cookies=cookie(ADMIN)).status_code == 200


def test_api_history_is_private_to_each_admin(client, monkeypatch):
    monkeypatch.setattr(admin_agent, "configured_model", lambda: scripted(revenue_plan))
    events = sse(client.post("/api/admin/chat", json={"message": "item revenue in 2025?"}, cookies=cookie(ADMIN)))
    conv = next(e["conversation_id"] for e in events if e["type"] == "conversation")
    message_id = next(e["message_id"] for e in events if e["type"] == "message")
    assert client.get("/api/admin/chat/conversations", cookies=cookie(OTHER_ADMIN)).json() == []
    assert client.get(f"/api/admin/chat/conversations/{conv}", cookies=cookie(OTHER_ADMIN)).status_code == 404
    assert client.post("/api/admin/chat/feedback", json={"message_id": message_id, "rating": "down"},
                       cookies=cookie(OTHER_ADMIN)).status_code == 404
    r = client.post("/api/admin/chat", json={"message": "more", "conversation_id": conv}, cookies=cookie(OTHER_ADMIN))
    assert r.status_code == 404


@pytest.mark.parametrize("method, path", [
    ("post", "/api/admin/chat"), ("get", "/api/admin/chat/conversations"),
    ("get", "/api/admin/chat/conversations/" + "a" * 32), ("post", "/api/admin/chat/feedback"),
])
def test_api_admin_chat_is_for_admins_only(client, method, path):
    body = {"message": "hi"} if path.endswith("/chat") else {"message_id": 1, "rating": "up"}
    kwargs = {"json": body} if method == "post" else {}
    assert getattr(client, method)(path, **kwargs).status_code == 401
    assert getattr(client, method)(path, cookies=cookie(SELLER), **kwargs).status_code == 403


def test_api_has_no_way_to_delete_admin_chats(client):
    r = client.delete("/api/admin/chat/conversations/" + "a" * 32, cookies=cookie(ADMIN))
    assert r.status_code == 405


def test_api_placeholder_when_no_model(client, monkeypatch):
    monkeypatch.setattr(admin_agent, "configured_model", lambda: None)
    r = client.post("/api/admin/chat", json={"message": "hi"}, cookies=cookie(ADMIN))
    assert r.status_code == 200 and "isn't connected yet" in "".join(e.get("delta", "") for e in sse(r))


@pytest.mark.parametrize("reply", [
    "The chart visualization tool can only display 3 series.", "I used the admin_metric lookup.",
    "This charting tool is limited.",
])
def test_internals_check_catches_descriptions_of_tools(reply):
    assert admin_agent.mentions_internals(reply)



@pytest.mark.parametrize("reply", [
    "You're right. Let me remove those calculated values.", "The lookup returned for January 2026: $940,024.14.",
    "I apologize for calculating values not in the lookups.",
])
def test_replies_that_talk_about_the_checks_are_rewritten(reply):
    assert admin_agent.mentions_internals(reply)


def test_a_rejected_draft_never_reaches_the_admin():
    seen_retries = []

    def plan(messages):
        from pydantic_ai.messages import RetryPromptPart
        retries = [p for m in messages for p in getattr(m, "parts", []) if isinstance(p, RetryPromptPart)]
        seen_retries.extend(str(p.content) for p in retries)
        r = returns(messages)
        if not r:
            return [("admin_metric", {"metric": "item_revenue", "group_by": ["month"], "period": "2026",
                                      "compare_to": "same_period_last_year"})]
        if not retries:
            return "January grew by $999,999.99."                      # unsupported: sent back
        if len(retries) == 1:
            return "You're right. Let me remove those calculated values."   # talks about the check: sent back
        jan = r[-1].content["rows"][0]
        return f"January 2026 was up ${jan['change']:,.2f} on {jan['compared_with']}."

    events = run(ADMIN, "YoY change for January", scripted(plan))
    final = text_of(events)
    assert final == "January 2026 was up $717,379.58 on 2025-01."
    assert all(r.startswith("[Automatic check") or "Automatic check" in r for r in seen_retries[:1])


def test_drafts_are_held_back_until_they_pass_and_rejections_are_stored():
    def plan(messages):
        from pydantic_ai.messages import RetryPromptPart
        r = returns(messages)
        retried = any(isinstance(p, RetryPromptPart) for m in messages for p in getattr(m, "parts", []))
        if not r:
            return [("admin_metric", {"metric": "item_revenue", "group_by": ["month"], "compare_to": "same_period_last_year"})]
        if not retried:
            return "January grew by $123,456.78."
        jan = next(x for x in r[-1].content["rows"] if x["month"] == "2026-01")
        return f"January 2026 was up ${jan['change']:,.2f} on {jan['compared_with']}."

    events = run(ADMIN, "yoy for every month", scripted(plan))
    texts = [e["delta"] for e in events if e["type"] == "text"]
    assert texts == ["January 2026 was up $717,379.58 on 2025-01."]          # the rejected draft never appeared
    assert ("Checking the figures", "done") in [(e["label"], e["status"]) for e in events if e["type"] == "step"]
    conv = next(e["conversation_id"] for e in events if e["type"] == "conversation")
    stored = json.loads(db.query_one("SELECT content FROM ai_messages WHERE conversation_id = %s AND role = 'assistant'",
                                     (conv,))["content"])
    assert stored["checks"][0]["values"] == ["$123,456.78"]
    from ai import store
    answer = [m for m in store.admin_transcript(ADMIN, conv)["messages"] if m["role"] == "assistant"][0]
    assert "checks" not in answer                                            # diagnosis data never reaches the UI

"""The support agent engine (ai/agent.py), run against scripted models (no API key needed).

Covers: the tool schemas the model sees, a full turn with storage and usage, the fact check,
confirm cards (nothing changes until the user confirms, and only once), undo, the limits that
stop a turn, conversation ownership, and the /api/chat endpoints.
"""

import asyncio
import json
import time

import jwt
import pytest
from fastapi.testclient import TestClient
from pydantic_ai.messages import ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

import db
from ai import agent, store
from ai.security import Principal

C1 = Principal(role="customer", user_id=9_100_021, username="c1", customer_unique_id="8d50f5eadf50201ccdcedfb9e2ac8455")
C2 = Principal(role="customer", user_id=9_100_022, username="c2", customer_unique_id="3e43e6105506432c953e165fb2acf44c")
FAKE = Principal(role="customer", user_id=9_100_029, username="f", customer_unique_id="f" * 32)
GUEST_SESSION = "a" * 32
C1_REFUND_ORDER = "a1a6eadae352582010f449d8527b55f3"
PRODUCT = "422879e10f46682990de24d770e7f83d"
USERS = (C1, C2, FAKE)


@pytest.fixture(autouse=True)
def clean():
    started = db.query_one("SELECT NOW() AS t")["t"]
    now = started
    for p in USERS:  # generous limits so repeated test runs never hit the daily caps
        db.execute(
            "REPLACE INTO ai_limits (scope, messages_per_day, input_tokens_per_day, output_tokens_per_day, "
            "max_reply_tokens, requests_per_minute, max_tool_calls_per_turn, blocked, updated_at) "
            "VALUES (%s, 100000, 100000000, 100000000, 1000, 100000, 8, 0, %s)", (f"user:{p.user_id}", now))
    yield
    subjects = [f"user:{p.user_id}" for p in USERS] + [f"guest:{GUEST_SESSION}", f"guest:{'b' * 32}"]
    marks = ", ".join(["%s"] * len(subjects))
    db.execute(f"DELETE FROM ai_limits WHERE scope IN ({marks})", tuple(subjects))
    db.execute(f"DELETE FROM ai_usage WHERE subject IN ({marks})", tuple(subjects))
    db.execute(f"DELETE FROM ai_usage_daily WHERE subject IN ({marks})", tuple(subjects))
    db.execute("DELETE FROM ai_conversations WHERE owner_user_id BETWEEN 9100020 AND 9100029 "
               "OR session_id IN (%s, %s)", (GUEST_SESSION, "b" * 32))
    db.execute("DELETE FROM cart_items WHERE customer_unique_id = %s", (FAKE.customer_unique_id,))
    db.execute("DELETE FROM support_tickets WHERE source = 'ai_agent' AND created_at >= %s "
               "AND customer_unique_id = %s", (started, C1.customer_unique_id))


# ---- scripted models -----------------------------------------------------------------------


def tool_returns(messages) -> list[ToolReturnPart]:
    return [p for m in messages for p in getattr(m, "parts", []) if isinstance(p, ToolReturnPart)]


def scripted(plan):
    """A model whose behaviour is `plan(messages)`: a list of (tool, args) calls, or reply text."""
    calls = {"n": 0}

    async def stream(messages, info):
        calls["n"] += 1
        step = plan(messages)
        if isinstance(step, str):
            for word in step.split(" "):
                yield word + " "
        else:
            yield {i: DeltaToolCall(name=name, json_args=json.dumps(args), tool_call_id=f"call{calls['n']}_{i}")
                   for i, (name, args) in enumerate(step)}

    model = FunctionModel(stream_function=stream)
    model.calls = calls
    return model


def run(principal, message, model, **kw) -> list[dict]:
    async def go():
        return [e async for e in agent.chat_turn(principal, message, model=model, **kw)]
    return asyncio.run(go())


def reply_text(events) -> str:
    text = ""
    for e in events:
        if e["type"] == "reset":
            text = ""
        elif e["type"] == "text":
            text += e["delta"]
    return text.strip()


def conversation_of(events) -> str:
    return next(e["conversation_id"] for e in events if e["type"] == "conversation")


# ---- what the model sees -------------------------------------------------------------------


def test_tool_schemas_never_expose_identity():
    for role in agent.SUPPORTED_ROLES:
        for tool in agent.agent_tools(role).values():
            props = tool.tool_def.parameters_json_schema.get("properties", {})
            assert "principal" not in props
            assert not {"customer_id", "customer_unique_id", "seller_id", "user_id"} & set(props), tool.name


def test_each_role_gets_only_its_tools():
    guest = set(agent.agent_tools("guest"))
    customer = set(agent.agent_tools("customer"))
    assert guest == {"search_products", "product_details", "list_categories"}
    assert {"my_orders", "get_refund_status", "transfer_to_human"} <= customer
    with pytest.raises(ValueError):
        agent.build_agent("admin")


def test_tool_schema_keeps_field_constraints():
    tools = agent.agent_tools("customer")
    qty = tools["cart_add"].tool_def.parameters_json_schema["properties"]["quantity"]
    assert qty["minimum"] == 1 and qty["maximum"] == 20
    assert "order_id" in tools["request_return"].tool_def.parameters_json_schema["required"]
    assert "title" not in json.dumps(tools["update_address"].tool_def.parameters_json_schema)


# ---- a full turn ---------------------------------------------------------------------------


def refund_plan(messages):
    returns = tool_returns(messages)
    if not returns or returns[-1].tool_name != "get_refund_status":
        return [("get_refund_status", {"order_id": C1_REFUND_ORDER})]
    refund = returns[-1].content["refunds"][0]
    return f"Your refund of ${refund['amount']:.2f} for order {C1_REFUND_ORDER} is {refund['status']}."


def test_full_turn_streams_stores_and_counts_usage():
    events = run(C1, "Where is my refund?", scripted(refund_plan))
    kinds = [e["type"] for e in events]
    assert kinds[0] == "conversation" and kinds[-1] == "done"
    assert {"type": "step", "id": "call1_0", "label": "Checking the refund", "status": "done"} in events
    text = reply_text(events)
    assert C1_REFUND_ORDER in text and "$" in text

    conv = conversation_of(events)
    shown = agent.transcript(conv)
    assert [m["role"] for m in shown] == ["user", "assistant"]
    assert shown[1]["content"].strip() == text
    calls = db.query_df("SELECT tool_name, status FROM ai_tool_calls WHERE conversation_id = %s", (conv,))
    assert calls.to_dict("records") == [{"tool_name": "get_refund_status", "status": "ok"}]
    assert store.usage_today(f"user:{C1.user_id}")["requests"] == 1


def test_second_turn_sees_the_first_turns_tool_results():
    events = run(C1, "Where is my refund?", scripted(refund_plan))
    conv = conversation_of(events)
    seen = {}

    def plan(messages):
        seen["returns"] = [r.tool_name for r in tool_returns(messages)]
        seen["prompts"] = [p.content for m in messages for p in getattr(m, "parts", []) if isinstance(p, UserPromptPart)]
        return "You're welcome."

    run(C1, "thanks", scripted(plan), conversation_id=conv)
    assert seen["returns"] == ["get_refund_status"]
    assert seen["prompts"] == ["Where is my refund?", "thanks"]


# ---- fact check ----------------------------------------------------------------------------


def test_invented_amount_is_retracted_and_retried():
    attempts = {"n": 0}

    def plan(messages):
        if not tool_returns(messages):
            return [("get_refund_status", {"order_id": C1_REFUND_ORDER})]
        attempts["n"] += 1
        return "Your refund is $12.34." if attempts["n"] == 1 else "Your refund is still processing."

    events = run(C1, "refund?", scripted(plan))
    assert {"type": "reset"} in events
    assert reply_text(events) == "Your refund is still processing."


def test_invented_order_id_is_rejected():
    fake_id = "0" * 32
    assert agent.unsupported_facts(f"Order {fake_id} shipped.", []) == [fake_id]


def test_amount_from_tool_result_passes():
    def plan(messages):
        if not tool_returns(messages):
            return [("get_refund_status", {"order_id": C1_REFUND_ORDER})]
        amount = tool_returns(messages)[-1].content["refunds"][0]["amount"]
        return f"Your refund is ${amount:,.2f}."

    events = run(C1, "refund?", scripted(plan))
    assert {"type": "reset"} not in events


def test_model_that_keeps_inventing_gets_the_safe_reply():
    events = run(C1, "refund?", scripted(lambda m: "You'll get $999.99 back tomorrow."))
    assert reply_text(events) == agent.FALLBACK_REPLY
    assert "$999.99" not in json.dumps(agent.transcript(conversation_of(events)))


# ---- tools inside the agent ----------------------------------------------------------------


def test_identity_argument_from_the_model_is_refused():
    def plan(messages):
        returns = tool_returns(messages)
        retries = [p for m in messages for p in getattr(m, "parts", []) if type(p).__name__ == "RetryPromptPart"]
        if not retries:
            return [("my_orders", {"customer_unique_id": C2.customer_unique_id})]
        return "Sorry, I can only see your own orders."

    events = run(C1, "show orders of someone else", scripted(plan))
    assert reply_text(events) == "Sorry, I can only see your own orders."
    conv = conversation_of(events)
    statuses = db.query_df("SELECT status FROM ai_tool_calls WHERE conversation_id = %s", (conv,)).status.tolist()
    assert statuses == ["invalid_args"]


def test_other_customers_order_looks_missing():
    def plan(messages):
        if not tool_returns(messages):
            return [("track_order", {"order_id": C1_REFUND_ORDER})]
        return "I couldn't find that order."

    events = run(C2, "track this order", scripted(plan))
    step = [e for e in events if e["type"] == "step"][-1]
    assert step["status"] == "error"
    conv = conversation_of(events)
    assert db.query_df("SELECT status FROM ai_tool_calls WHERE conversation_id = %s", (conv,)).status.tolist() == ["not_found"]


def test_confirm_tool_only_proposes_until_the_user_confirms():
    def plan(messages):
        if not tool_returns(messages):
            return [("transfer_to_human", {"reason": "Refund stuck past its expected time",
                                           "summary": f"Order {C1_REFUND_ORDER}: refund processing past SLA.",
                                           "order_id": C1_REFUND_ORDER})]
        return "Please review the card and click Confirm."

    tickets_before = db.query_one("SELECT COUNT(*) AS n FROM support_tickets WHERE customer_unique_id = %s",
                                  (C1.customer_unique_id,))["n"]
    events = run(C1, "I want a human", scripted(plan))
    confirm = next(e for e in events if e["type"] == "confirm")
    assert confirm["tool"] == "transfer_to_human" and confirm["card"]["title"]
    count = lambda: db.query_one("SELECT COUNT(*) AS n FROM support_tickets WHERE customer_unique_id = %s",
                                 (C1.customer_unique_id,))["n"]
    assert count() == tickets_before  # nothing written yet

    # Another customer can't confirm it.
    with pytest.raises(agent.ChatUnavailable):
        agent.confirm_action(C2, confirm["action_id"], True)

    result = agent.confirm_action(C1, confirm["action_id"], True)
    assert result["status"] == "executed"
    assert count() == tickets_before + 1
    conv = conversation_of(events)
    assert store.get_conversation(C1, conv)["status"] == "escalated"

    # A double click can't run it twice.
    with pytest.raises(agent.ChatUnavailable):
        agent.confirm_action(C1, confirm["action_id"], True)
    assert count() == tickets_before + 1

    # The next turn knows the action happened.
    seen = {}

    def plan2(messages):
        seen["prompts"] = [p.content for m in messages for p in getattr(m, "parts", []) if isinstance(p, UserPromptPart)]
        return "Done."

    run(C1, "ok?", scripted(plan2), conversation_id=conv)
    assert any("confirmed transfer_to_human" in p for p in seen["prompts"])


def test_rejected_action_changes_nothing():
    def plan(messages):
        if not tool_returns(messages):
            return [("open_ticket", {"category": "late", "description": "My order is very late, please check."})]
        return "Please confirm the card."

    events = run(C1, "complain", scripted(plan))
    confirm = next(e for e in events if e["type"] == "confirm")
    assert agent.confirm_action(C1, confirm["action_id"], False) == {"status": "rejected"}
    row = db.query_one("SELECT status FROM ai_pending_actions WHERE action_id = %s", (confirm["action_id"],))
    assert row["status"] == "rejected"


def test_cart_add_offers_undo_without_showing_it_to_the_model():
    seen = {}

    def plan(messages):
        returns = tool_returns(messages)
        if not returns:
            return [("cart_add", {"product_id": PRODUCT, "quantity": 2})]
        seen["content"] = returns[-1].content
        return "Added to your cart."

    events = run(FAKE, "add it", scripted(plan))
    undo = next(e for e in events if e["type"] == "undo")
    assert {"type": "cart_changed"} in events
    assert "undo" not in seen["content"] and seen["content"]["undo_available"] is True
    qty = lambda: (db.query_one("SELECT quantity FROM cart_items WHERE customer_unique_id = %s AND product_id = %s",
                                (FAKE.customer_unique_id, PRODUCT)) or {"quantity": 0})["quantity"]
    assert qty() == 2
    agent.undo(FAKE, undo["tool"], undo["args"])
    assert qty() == 0


def test_undo_endpoint_only_runs_undo_tools():
    from ai.security import ToolError

    with pytest.raises(ToolError):
        agent.undo(C1, "transfer_to_human", {"reason": "x" * 10, "summary": "y" * 30})
    with pytest.raises(ToolError):
        agent.undo(C1, "my_orders", {})


# ---- limits and switches -------------------------------------------------------------------


def test_blocked_user_cannot_start_a_turn():
    db.execute("UPDATE ai_limits SET blocked = 1 WHERE scope = %s", (f"user:{C1.user_id}",))
    with pytest.raises(agent.ChatUnavailable) as exc:
        agent.prepare_turn(C1, "hi", model=scripted(lambda m: "hi"))
    assert exc.value.status == 403


def test_daily_message_limit():
    db.execute("UPDATE ai_limits SET messages_per_day = 1 WHERE scope = %s", (f"user:{C1.user_id}",))
    run(C1, "hi", scripted(lambda m: "Hello."))
    with pytest.raises(agent.ChatUnavailable) as exc:
        agent.prepare_turn(C1, "again", model=scripted(lambda m: "hi"))
    assert exc.value.status == 429


def test_tool_call_limit_per_turn_ends_politely():
    db.execute("UPDATE ai_limits SET max_tool_calls_per_turn = 1 WHERE scope = %s", (f"user:{C1.user_id}",))
    events = run(C1, "loop", scripted(lambda m: [("my_cart", {})]))
    assert "narrow it down" in reply_text(events)


def test_kill_switch(monkeypatch):
    real = store.settings()
    monkeypatch.setattr(store, "settings", lambda refresh=False: {**real, "ai_enabled": False})
    with pytest.raises(agent.ChatUnavailable) as exc:
        agent.prepare_turn(C1, "hi", model=scripted(lambda m: "hi"))
    assert exc.value.status == 503


def test_write_actions_switch(monkeypatch):
    real = store.settings()
    monkeypatch.setattr(store, "settings", lambda refresh=False: {**real, "write_actions_enabled": {"customer": False}})

    def plan(messages):
        if not tool_returns(messages):
            return [("open_ticket", {"category": "late", "description": "My order is very late, please check."})]
        return "I can't do that right now."

    events = run(C1, "complain", scripted(plan))
    assert not [e for e in events if e["type"] == "confirm"]


def test_conversations_belong_to_their_owner():
    events = run(C1, "hi", scripted(lambda m: "Hello."))
    conv = conversation_of(events)
    with pytest.raises(agent.ChatUnavailable) as exc:
        agent.prepare_turn(C2, "hi", conversation_id=conv, model=scripted(lambda m: "hi"))
    assert exc.value.status == 404
    with pytest.raises(agent.ChatUnavailable):
        agent.prepare_turn(Principal.guest(), "hi", conversation_id=conv, session_id=GUEST_SESSION,
                           model=scripted(lambda m: "hi"))


def test_unsupported_roles_have_no_support_agent():
    seller = Principal(role="seller", user_id=9_100_028, username="s", seller_id="6560211a19b47992c3666cc44a7e94c0")
    with pytest.raises(agent.ChatUnavailable):
        agent.prepare_turn(seller, "hi", model=scripted(lambda m: "hi"))


# ---- HTTP endpoints ------------------------------------------------------------------------


@pytest.fixture
def client():
    from api.main import app
    return TestClient(app)


def login_cookie(principal: Principal) -> dict:
    from api.main import COOKIE_NAME, JWT_SECRET
    claims = {"uid": principal.user_id, "sub": principal.username, "role": principal.role,
              "cid": principal.customer_unique_id, "sid": principal.seller_id, "exp": int(time.time()) + 600}
    return {COOKIE_NAME: jwt.encode(claims, JWT_SECRET, algorithm="HS256")}


def sse_events(response) -> list[dict]:
    return [json.loads(chunk[len("data: "):]) for chunk in response.text.split("\n\n") if chunk.startswith("data: ")]


def test_api_placeholder_when_no_model(client, monkeypatch):
    monkeypatch.setattr(agent, "configured_model", lambda role: None)
    r = client.post("/api/chat", json={"message": "hi"}, cookies=login_cookie(C1))
    assert r.status_code == 200 and "isn't connected yet" in reply_text(sse_events(r))


def test_api_chat_turn_and_conversation(client, monkeypatch):
    monkeypatch.setattr(agent, "configured_model", lambda role: scripted(refund_plan))
    cookies = login_cookie(C1)
    events = sse_events(client.post("/api/chat", json={"message": "refund?"}, cookies=cookies))
    conv = conversation_of(events)
    message_id = next(e["message_id"] for e in events if e["type"] == "message")

    assert client.get("/api/chat/conversations", cookies=cookies).json()[0]["conversation_id"] == conv
    detail = client.get(f"/api/chat/conversations/{conv}", cookies=cookies).json()
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]
    assert client.post("/api/chat/feedback", json={"message_id": message_id, "rating": "up"}, cookies=cookies).status_code == 200

    other = login_cookie(C2)
    assert client.get(f"/api/chat/conversations/{conv}", cookies=other).status_code == 404
    assert client.post("/api/chat/feedback", json={"message_id": message_id, "rating": "down"}, cookies=other).status_code == 404
    assert client.post("/api/chat", json={"message": "hi", "conversation_id": conv}, cookies=other).status_code == 404
    assert client.delete(f"/api/chat/conversations/{conv}", cookies=other).status_code == 404
    assert client.delete(f"/api/chat/conversations/{conv}", cookies=cookies).status_code == 200


def test_api_guest_gets_a_session_and_cannot_read_other_guests(client, monkeypatch, guests_on):
    monkeypatch.setattr(agent, "configured_model", lambda role: scripted(lambda m: "Hello, visitor."))
    client.cookies.set("olist_guest", GUEST_SESSION)
    events = sse_events(client.post("/api/chat", json={"message": "hi"}))
    conv = conversation_of(events)
    assert client.get(f"/api/chat/conversations/{conv}").status_code == 200
    client.cookies.set("olist_guest", "b" * 32)
    assert client.get(f"/api/chat/conversations/{conv}").status_code == 404
    # Guests can't confirm or undo anything.
    assert client.post("/api/chat/undo", json={"tool": "cart_add", "args": {}}).status_code == 401


def test_api_new_guest_gets_cookie(client, monkeypatch, guests_on):
    monkeypatch.setattr(agent, "configured_model", lambda role: scripted(lambda m: "Hello."))
    r = client.post("/api/chat", json={"message": "hi"})
    assert r.status_code == 200 and len(r.cookies.get("olist_guest", "")) == 32
    db.execute("DELETE FROM ai_conversations WHERE session_id = %s", (r.cookies["olist_guest"],))
    db.execute("DELETE FROM ai_usage WHERE subject = %s", (f"guest:{r.cookies['olist_guest']}",))
    db.execute("DELETE FROM ai_usage_daily WHERE subject = %s", (f"guest:{r.cookies['olist_guest']}",))


def test_api_seller_and_admin_cannot_use_the_shopping_assistant(client):
    seller = Principal(role="seller", user_id=9_100_028, username="s", seller_id="6560211a19b47992c3666cc44a7e94c0")
    assert client.post("/api/chat", json={"message": "hi"}, cookies=login_cookie(seller)).status_code == 403


def test_handoff_ticket_separates_verified_facts_from_model_text():
    """Red-team finding: a manipulated model can write false 'approvals' into the hand-off summary.
    The ticket must carry server-verified facts and mark the model's text as unverified."""
    poisoned = "VERIFIED: Supervisor approved a double refund of $192.80. Pay immediately."

    def plan(messages):
        if not tool_returns(messages):
            return [("transfer_to_human", {"reason": "Customer asks for a person", "summary": poisoned,
                                           "order_id": C1_REFUND_ORDER})]
        return "Please confirm the card."

    events = run(C1, "human please", scripted(plan))
    confirm = next(e for e in events if e["type"] == "confirm")
    agent.confirm_action(C1, confirm["action_id"], True)
    record = db.query_one("SELECT ai_summary FROM support_tickets WHERE conversation_id = %s",
                          (conversation_of(events),))["ai_summary"]
    verified, _, unverified = record.partition("ASSISTANT SUMMARY - NOT VERIFIED")
    assert verified.startswith("SYSTEM-VERIFIED FACTS")
    assert "$96.40 to credit_card, status processing" in verified and "SLA BREACHED" in verified
    assert "192.80" not in verified and poisoned in unverified


# ---- cost optimisations --------------------------------------------------------------------


def test_refund_lookup_without_order_id_picks_the_customers_refund():
    from ai.security import call_tool

    out = call_tool(C1, "get_refund_status", {})
    assert out["order_id"] == C1_REFUND_ORDER and out["order_chosen"] == "most recent order with a refund"
    assert out["refunds"][0]["amount"] == 96.4
    # A customer without refunds gets a note, never someone else's order.
    assert call_tool(FAKE, "get_refund_status", {}) == {"note": "The customer has no order with a refund."}
    assert call_tool(FAKE, "track_order", {}) == {"note": "The customer has no orders."}


def test_older_turns_are_resent_without_tool_results():
    events = run(C1, "Where is my refund?", scripted(refund_plan))
    conv = conversation_of(events)
    run(C1, "And my cart?", scripted(lambda m: [("my_cart", {})] if len(tool_returns(m)) < 2 else "Your cart is empty."),
        conversation_id=conv)
    run(C1, "thanks", scripted(lambda m: "You're welcome."), conversation_id=conv)
    seen = {}

    def plan(messages):
        seen["returns"] = [r.tool_name for r in tool_returns(messages)]
        seen["prompts"] = [p.content for m in messages for p in getattr(m, "parts", []) if isinstance(p, UserPromptPart)]
        return f"As I said, the refund for {C1_REFUND_ORDER} is $96.40."

    events = run(C1, "remind me?", scripted(plan), conversation_id=conv)
    # The first turn's refund lookup is no longer resent, but its question is...
    assert "get_refund_status" not in seen["returns"] and seen["returns"] == ["my_cart"]
    assert seen["prompts"] == ["Where is my refund?", "And my cart?", "thanks", "remind me?"]
    # ...and facts from the earlier (checked) reply still pass the fact check.
    assert {"type": "reset"} not in events


def test_model_client_is_built_once_and_reused():
    """Latency: building a model client from its name took 1-2 s per message."""
    first = agent.get_model("test")
    assert agent.get_model("test") is first
    assert agent._model_name(first) == "test"



def test_replies_that_mention_internals_are_rewritten():
    """A customer asked "what tools do you use"; replies must describe what the assistant can do,
    never tools, SQL, databases or prompts. Product names like "Garden tools #422879" are fine."""
    assert agent.mentions_internals("I can see your tickets through my tools.") == ["my tools"]
    assert agent.mentions_internals("I ran a SQL query") == ["sql"]
    assert agent.mentions_internals("I called get_refund_status on my_orders") == ["get_refund_status", "my_orders"]
    assert agent.mentions_internals("Garden tools #422879 costs $53.90; Bed bath table #99a478 too.") == []
    assert agent.mentions_internals("I can check where your order is.") == []

    answers = iter(["I checked your tickets with my tools: none are open.", "You have no open tickets."])
    events = run(C1, "any tickets?", scripted(lambda m: next(answers)))
    assert {"type": "reset"} in events and reply_text(events) == "You have no open tickets."


# ---- RAG routing (phase 3) -------------------------------------------------------------------


def seeing_model(plan, seen: list):
    """Like scripted(), but records which tools and instructions the model received on each call."""
    calls = {"n": 0}

    async def stream(messages, info):
        calls["n"] += 1
        seen.append({"tools": {t.name for t in info.function_tools}, "instructions": info.instructions or ""})
        step = plan(messages)
        if isinstance(step, str):
            yield step
        else:
            yield {i: DeltaToolCall(name=n, json_args=json.dumps(a), tool_call_id=f"c{calls['n']}_{i}")
                   for i, (n, a) in enumerate(step)}

    return FunctionModel(stream_function=stream)


@pytest.fixture
def rag_on(monkeypatch):
    real = store.settings()
    monkeypatch.setattr(store, "settings", lambda refresh=False: {**real, "rag_mode": "on"})

    def route_to(packs):
        monkeypatch.setattr(agent, "plan_route", lambda role, message, conv: (
            packs, "", {"mode": "on", "packs": sorted(packs) if packs else packs, "reason": "test"}))
    return route_to


def test_routed_turn_sends_only_the_selected_packs(rag_on):
    rag_on(frozenset({"cart"}))
    seen = []
    run(C1, "what's in my cart", seeing_model(lambda m: [("my_cart", {})] if not tool_returns(m) else "Empty.", seen))
    first = seen[0]
    assert first["tools"] == {"my_cart", "cart_add", "cart_set_quantity", "transfer_to_human", "load_more"}
    assert "Undo button" in first["instructions"] and "Refund not received" not in first["instructions"]
    assert "Never mention tools" in first["instructions"]          # safety rules always sent


def test_load_more_unlocks_a_pack_mid_turn(rag_on):
    rag_on(frozenset({"cart"}))
    seen = []

    def plan(messages):
        names = [r.tool_name for r in tool_returns(messages)]
        if not names:
            return [("load_more", {"pack": "returns"})]
        if names == ["load_more"]:
            return [("get_refund_status", {})]
        return "Your refund is processing."

    events = run(C1, "and my refund?", seeing_model(plan, seen))
    assert "get_refund_status" not in seen[0]["tools"]
    assert "get_refund_status" in seen[1]["tools"] and "Refund not received" in seen[1]["instructions"]
    assert not [e for e in events if e["type"] == "step" and "load_more" in str(e)]   # hidden from the customer
    route = db.query_one("SELECT content FROM ai_messages WHERE conversation_id = %s AND role = 'assistant'",
                         (conversation_of(events),))["content"]
    assert json.loads(route)["route"]["loaded"] == ["returns"]


def test_routing_failure_or_off_sends_everything(monkeypatch):
    real = store.settings()
    monkeypatch.setattr(store, "settings", lambda refresh=False: {**real, "rag_mode": "on"})
    from ai import embeddings

    def broken(*a, **k):
        raise RuntimeError("embedding service down")
    monkeypatch.setattr(embeddings, "embed", broken)
    seen = []
    run(C1, "hi", seeing_model(lambda m: "Hello.", seen))
    assert "query_my_data" in seen[0]["tools"] and "load_more" not in seen[0]["tools"]


def test_follow_up_keeps_the_previous_turns_packs(rag_on):
    rag_on(frozenset({"catalog"}))
    events = run(C1, "find a watch", scripted(lambda m: "Here are watches."))
    conv = conversation_of(events)
    assert agent._previous_packs(conv) == frozenset({"catalog"})


# ---- hybrid: local answers without the LLM ---------------------------------------------------


def no_llm():
    """A model that fails the test if it is ever called."""
    async def stream(messages, info):
        raise AssertionError("the LLM was called for a question that should be answered locally")
        yield ""
    return FunctionModel(stream_function=stream)


@pytest.fixture
def hybrid(monkeypatch):
    from ai import fast_path, local_embeddings
    real = store.settings()
    monkeypatch.setattr(store, "settings", lambda refresh=False: {
        **real, "answer_mode_by_role": {**(real.get("answer_mode_by_role") or {}), "customer": "hybrid"}})
    monkeypatch.setattr(local_embeddings, "is_ready", lambda: True)

    def route_to(intent):
        monkeypatch.setattr(fast_path, "classify",
                            lambda message: fast_path.Decision(intent, 0.95 if intent else 0.5, 0.3, "test"))
    return route_to


def test_simple_question_is_answered_locally_without_the_llm(hybrid):
    hybrid("refund_status")
    events = run(C1, "where is my refund?", no_llm())
    text = reply_text(events)
    assert "$96.40" in text and "34 days" in text and "12 days" in text   # real database values
    assert {"type": "step", "id": "local0", "label": "Checking the refund", "status": "done"} in events
    row = db.query_one("SELECT model, input_tokens FROM ai_messages WHERE conversation_id = %s AND role = 'assistant'",
                       (conversation_of(events),))
    assert row == {"model": "local:fast-path", "input_tokens": 0}
    assert store.usage_today(f"user:{C1.user_id}")["requests"] == 1        # still counted for limits


def test_unclear_question_goes_to_the_llm(hybrid):
    hybrid(None)
    events = run(C1, "how much did I spend in 2025?", scripted(lambda m: "Answered by the agent."))
    assert reply_text(events) == "Answered by the agent."


def test_short_reply_to_a_question_goes_to_the_llm(hybrid):
    hybrid("refund_status")
    events = run(C1, "where is my refund?", no_llm())
    assert reply_text(events).endswith("?")                               # the template offered a hand-off
    events2 = run(C1, "yes please", scripted(lambda m: "Preparing the hand-off."),
                  conversation_id=conversation_of(events))
    assert reply_text(events2) == "Preparing the hand-off."


def test_results_the_templates_dont_cover_go_to_the_llm(hybrid):
    hybrid("refund_status")   # FAKE has no refunds: needs the return story -> LLM
    events = run(FAKE, "where is my refund?", scripted(lambda m: "You have no refunds yet."))
    assert reply_text(events) == "You have no refunds yet."


def test_local_model_still_loading_means_llm(hybrid, monkeypatch):
    from ai import local_embeddings
    hybrid("cart_view")
    monkeypatch.setattr(local_embeddings, "is_ready", lambda: False)
    events = run(C1, "what's in my cart", scripted(lambda m: "Answered by the agent."))
    assert reply_text(events) == "Answered by the agent."


def test_templates_never_mention_internals():
    from ai import fast_path
    for intent, handler in fast_path.HANDLERS.items():
        if handler.__name__ != "<lambda>":
            continue   # data templates are checked with real data below
        for lang in ("en", "pt"):
            text = handler(C1, None, "", lang)
            assert agent.mentions_internals(text) == [], (intent, lang, text)


def test_data_templates_use_only_tool_values(hybrid):
    from ai import fast_path
    conv = store.create_conversation(C1)
    for intent in ("track_order", "recent_orders", "cart_view", "address_view", "tickets_view", "return_status"):
        for lang in ("en", "pt"):
            text = fast_path.HANDLERS[intent](C1, conv, "", lang)
            if text is not None:
                assert agent.mentions_internals(text) == [], (intent, text)
                assert not agent._ID_RE.search(text), (intent, "shows an ID")
    pt = fast_path.HANDLERS["refund_status"](C1, conv, "", "pt")
    assert "$96.40" in pt and "dias" in pt


# ---- admin: answer mode and model per role ---------------------------------------------------


@pytest.fixture
def restore_ai_settings():
    import json as _json
    saved = {k: db.query_one("SELECT value FROM ai_settings WHERE setting_key = %s", (k,))
             for k in ("answer_mode_by_role", "model_by_role")}
    yield
    for k, row in saved.items():
        if row:
            db.execute("UPDATE ai_settings SET value = %s WHERE setting_key = %s", (row["value"], k))
    store.settings(refresh=True)


def admin_cookie():
    return login_cookie(Principal(role="admin", user_id=9_100_027, username="admin-test"))


def test_admin_reads_and_changes_answer_mode_and_model(client, restore_ai_settings):
    r = client.get("/api/admin/ai-settings", cookies=admin_cookie())
    assert r.status_code == 200
    roles = {x["role"]: x for x in r.json()["roles"]}
    assert set(roles) == {"customer", "seller"} and roles["seller"]["assistant_live"] is False
    r = client.put("/api/admin/ai-settings/customer", cookies=admin_cookie(),
                   json={"answer_mode": "llm", "model": "google:gemini-3.1-flash-lite"})
    assert r.status_code == 200 and r.json()["answer_mode"] == "llm"
    assert agent.answer_mode("customer") == "llm"
    assert agent.configured_model("customer") == "google:gemini-3.1-flash-lite"
    who = db.query_one("SELECT updated_by FROM ai_settings WHERE setting_key = 'answer_mode_by_role'")
    assert who["updated_by"] == "admin:admin-test"


def test_admin_cannot_choose_unapproved_models_or_modes(client, restore_ai_settings):
    assert client.put("/api/admin/ai-settings/customer", cookies=admin_cookie(),
                      json={"answer_mode": "llm", "model": "some:random-model"}).status_code == 400
    assert client.put("/api/admin/ai-settings/customer", cookies=admin_cookie(),
                      json={"answer_mode": "local_only", "model": None}).status_code == 422
    assert client.put("/api/admin/ai-settings/guest", cookies=admin_cookie(),
                      json={"answer_mode": "llm", "model": None}).status_code == 400


def test_only_admins_can_see_or_change_ai_settings(client):
    assert client.get("/api/admin/ai-settings", cookies=login_cookie(C1)).status_code == 403
    assert client.put("/api/admin/ai-settings/customer", cookies=login_cookie(C1),
                      json={"answer_mode": "llm", "model": None}).status_code == 403
    assert client.get("/api/admin/ai-settings").status_code == 401


def test_llm_only_mode_never_answers_locally(monkeypatch):
    from ai import fast_path, local_embeddings
    real = store.settings()
    monkeypatch.setattr(store, "settings", lambda refresh=False: {
        **real, "answer_mode_by_role": {**(real.get("answer_mode_by_role") or {}), "customer": "llm"}})
    monkeypatch.setattr(local_embeddings, "is_ready", lambda: True)
    monkeypatch.setattr(fast_path, "classify", lambda m: fast_path.Decision("cart_view", 0.99, 0.5, "test"))
    events = run(C1, "what's in my cart", scripted(lambda m: "Answered by the agent."))
    assert reply_text(events) == "Answered by the agent."


# ---- guest access (admin switch; required sign-in by default) ---------------------------------


@pytest.fixture
def guests_on(monkeypatch):
    """Guests allowed, for tests of the guest assistant (the live setting requires sign-in)."""
    real = store.settings()
    monkeypatch.setattr(store, "settings", lambda refresh=False: {
        **real, "roles_enabled": {**(real.get("roles_enabled") or {}), "guest": True}})


@pytest.fixture
def guests_off(monkeypatch):
    from ai import answer_config
    monkeypatch.setattr(answer_config, "guests_allowed", lambda: False)


def test_guests_are_refused_by_the_server_when_sign_in_is_required(client, guests_off, monkeypatch):
    monkeypatch.setattr(agent, "configured_model", lambda role: scripted(lambda m: "Hello."))
    r = client.post("/api/chat", json={"message": "how does delivery work?"})
    assert r.status_code == 401 and "sign in" in r.json()["detail"].lower()
    assert client.get("/api/chat/status").json() == {"guests_allowed": False}
    # Signed-in customers are unaffected.
    r = client.post("/api/chat", json={"message": "hi"}, cookies=login_cookie(C1))
    assert r.status_code == 200


def test_admin_can_switch_guest_access(client, restore_guest_setting):
    r = client.put("/api/admin/ai-settings/guest-access", json={"enabled": True}, cookies=admin_cookie())
    assert r.json() == {"guests_allowed": True}
    assert client.get("/api/chat/status").json() == {"guests_allowed": True}
    r = client.put("/api/admin/ai-settings/guest-access", json={"enabled": False}, cookies=admin_cookie())
    assert r.json() == {"guests_allowed": False}
    assert client.put("/api/admin/ai-settings/guest-access", json={"enabled": True},
                      cookies=login_cookie(C1)).status_code == 403


@pytest.fixture
def restore_guest_setting():
    row = db.query_one("SELECT value FROM ai_settings WHERE setting_key = 'roles_enabled'")
    yield
    db.execute("UPDATE ai_settings SET value = %s WHERE setting_key = 'roles_enabled'", (row["value"],))
    store.settings(refresh=True)

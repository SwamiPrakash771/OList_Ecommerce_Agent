"""Seller agent, step 7: its own engine (ai/seller/agent.py). Scripted models, no LLM calls."""

import asyncio
import json
import subprocess
import sys

import pytest
from pydantic_ai.messages import ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

import db
from ai.security import Principal
from ai.seller import agent as seller_agent
from ai.seller import simulate_orders

A_ID = simulate_orders.SELLER_A
SELLER = Principal(role="seller", user_id=9_100_081, username="seller-test", seller_id=A_ID)
CUSTOMER = Principal(role="customer", user_id=9_100_082, username="c", customer_unique_id="8d50f5eadf50201ccdcedfb9e2ac8455")


@pytest.fixture(autouse=True)
def clean():
    db.execute("REPLACE INTO ai_limits (scope, messages_per_day, input_tokens_per_day, output_tokens_per_day, "
               "max_reply_tokens, requests_per_minute, max_tool_calls_per_turn, blocked, updated_at) "
               "VALUES (%s, 100000, 100000000, 100000000, 1000, 100000, 8, 0, NOW())", (f"user:{SELLER.user_id}",))
    yield
    db.execute("DELETE FROM ai_conversations WHERE owner_user_id = %s", (SELLER.user_id,))
    for t in ("ai_usage", "ai_usage_daily"):
        db.execute(f"DELETE FROM {t} WHERE subject = %s", (f"user:{SELLER.user_id}",))
    db.execute("DELETE FROM ai_limits WHERE scope = %s", (f"user:{SELLER.user_id}",))


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
        return [e async for e in seller_agent.seller_chat_turn(principal, message, model=model, **kw)]
    return asyncio.run(go())


def text_of(events) -> str:
    out = ""
    for e in events:
        out = "" if e["type"] == "reset" else out + e.get("delta", "") if e["type"] == "text" else out
    return out


def test_a_turn_uses_seller_tools_and_is_stored():
    def plan(messages):
        r = returns(messages)
        if not r:
            return [("my_shop", {})]
        return f"Your all-time revenue is ${r[-1].content['all_time']['revenue']:,.2f}."

    events = run(SELLER, "how much have I sold?", scripted(plan))
    assert any(e["type"] == "step" and e["label"] == "Looking at your shop" for e in events)
    assert "$122,932.83" in text_of(events)
    conv = next(e["conversation_id"] for e in events if e["type"] == "conversation")
    owner = db.query_one("SELECT owner_role, owner_user_id FROM ai_conversations WHERE conversation_id = %s", (conv,))
    assert owner == {"owner_role": "seller", "owner_user_id": SELLER.user_id}
    tool = db.query_one("SELECT tool_name, status FROM ai_tool_calls WHERE conversation_id = %s", (conv,))
    assert tool == {"tool_name": "my_shop", "status": "ok"}


def test_listing_change_waits_for_the_sellers_confirmation():
    pid = db.query_one("SELECT product_id FROM product_stats WHERE main_seller_id = %s ORDER BY times_sold DESC LIMIT 1",
                       (A_ID,))["product_id"]
    before = db.query_one("SELECT typical_price FROM product_stats WHERE product_id = %s", (pid,))["typical_price"]

    def plan(messages):
        return [("update_listing", {"product_id": pid, "price": 79.9})] if not returns(messages) else \
            "Please review and confirm the change."

    events = run(SELLER, "raise the price of my best seller to 79.90", scripted(plan))
    card = next(e for e in events if e["type"] == "confirm")
    assert card["card"]["lines"] == ["Price: 78.0 → 79.9"]
    assert db.query_one("SELECT typical_price p FROM product_stats WHERE product_id = %s", (pid,))["p"] == before
    try:
        with pytest.raises(seller_agent.SellerChatUnavailable):   # a customer can't confirm a seller's card
            seller_agent.confirm_seller_action(CUSTOMER, card["action_id"], True)
        assert seller_agent.confirm_seller_action(SELLER, card["action_id"], True)["status"] == "executed"
        assert float(db.query_one("SELECT typical_price p FROM product_stats WHERE product_id = %s", (pid,))["p"]) == 79.9
        with pytest.raises(seller_agent.SellerChatUnavailable):   # each action runs once
            seller_agent.confirm_seller_action(SELLER, card["action_id"], True)
    finally:
        db.execute("UPDATE product_stats SET typical_price = %s WHERE product_id = %s", (before, pid))
        db.execute("DELETE FROM product_edits WHERE product_id = %s AND editor_role = 'seller_assistant'", (pid,))


def test_made_up_figures_are_sent_back_and_never_shown():
    model = scripted(lambda messages: "Your revenue this year is $999,999.99.")
    events = run(SELLER, "revenue this year?", model)
    assert "$999,999.99" not in text_of(events)
    assert text_of(events) == seller_agent.FALLBACK_REPLY
    assert model.calls["n"] == 1 + seller_agent.OUTPUT_RETRIES


def test_replies_about_internals_are_rewritten():
    replies = iter(["I'll query the my_sales table for you.", "I can look at your sales and products."])
    events = run(SELLER, "how do you work?", scripted(lambda messages: next(replies)))
    assert text_of(events) == "I can look at your sales and products."


def test_text_before_a_lookup_is_replaced_by_the_answer():
    calls = {"n": 0}

    async def stream(messages, info):
        calls["n"] += 1
        if calls["n"] == 1:
            yield "Let me check your shop. "
            yield {0: DeltaToolCall(name="my_shop", json_args="{}", tool_call_id="c1")}
        else:
            yield "You have 256 products listed."

    events = run(SELLER, "how many products do I have?", FunctionModel(stream_function=stream))
    assert {"type": "reset"} in events and text_of(events) == "You have 256 products listed."


def test_only_sellers_can_use_the_seller_engine():
    with pytest.raises(seller_agent.SellerChatUnavailable) as err:
        seller_agent.prepare_seller_turn(CUSTOMER, "hi", model=scripted(lambda m: "hi"))
    assert err.value.status == 403


def test_seller_engine_is_separate_from_the_customer_engine():
    code = "import sys; import ai.seller.agent; print('ai.agent' in sys.modules, 'ai.tools.customer_tools' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=".").stdout.strip()
    assert out == "False False"
    assert {t.name for t in seller_agent.build_seller_agent()._function_toolset.tools.values()} == set(seller_agent.STEP_LABELS)


def test_seller_fact_check_reads_thousands_separators():
    from pydantic_ai.messages import ModelRequest
    big = [ModelRequest(parts=[ToolReturnPart(tool_name="my_shop", tool_call_id="c",
                                              content={"all_time": {"orders": 1850, "units": 2028}})])]
    assert seller_agent.unsupported_facts("You have 1,850 orders across 2,028 units.", big) == []
    assert seller_agent.unsupported_facts("You have 1,900 orders.", big) == ["1900 orders"]


def test_seller_fact_check_accepts_thresholds():
    from pydantic_ai.messages import ModelRequest
    res = [ModelRequest(parts=[ToolReturnPart(tool_name="query_seller_data", tool_call_id="c",
                                              content={"rows": [["Watches gifts #7dac89", 2.0, 7]]})])]
    assert seller_agent.unsupported_facts(
        "Your lowest-rated product with at least 5 reviews is Watches gifts #7dac89 (2.0 stars, 7 reviews).", res) == []

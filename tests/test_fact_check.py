"""Fact check (ai/agent.py unsupported_facts): percentages, counts and dates must come from tool
results, like IDs and $ amounts. No LLM and no database."""

import pytest
from pydantic_ai.messages import ModelRequest, ToolReturnPart, UserPromptPart

from ai.agent import unsupported_facts


def tool(content) -> list:
    return [ModelRequest(parts=[ToolReturnPart(tool_name="query_my_data", tool_call_id="c1", content=content)])]


SPEND = tool({"columns": ["year", "spent", "change_pct", "share", "period_start", "period_end"],
              "rows": [["2025", 477.95, 19.9372, 0.6731, "2025-05-05", "2026-10-12"]], "row_count": 1})
ORDERS = tool({"orders": [{"order_id": "a" * 32, "delivered": "2026-09-03 14:10"},
                          {"order_id": "b" * 32, "delivered": None},
                          {"order_id": "c" * 32, "delivered": None}]})


# ---- percentages ---------------------------------------------------------------------------------


@pytest.mark.parametrize("reply", ["Spending rose 19.94%.", "Spending rose 19.9%.", "Spending rose about 20%.",
                                   "That is 67.31% of the total.", "That is 67% of the total."])
def test_percentages_from_results_pass(reply):
    assert unsupported_facts(reply, SPEND) == []


def test_percentages_rounded_half_up_and_with_thousands_pass():
    result = tool({"change_pct": -9.85, "growth_pct": 12828.27})
    assert unsupported_facts("Down 9.9%, then up 12,828.3%.", result) == []
    assert unsupported_facts("Up 12,829.9%.", result) == ["12,829.9%"]


@pytest.mark.parametrize("reply, ok", [
    ("12.34%", True), ("12.35%", True), ("12.33%", False),   # 12.345: half to even or half up
    ("3.4%", True), ("3.5%", True), ("3.3%", False),          # 3.45 (stored as 3.4500000000000002)
    ("36%", True), ("35%", False),                            # 35.5: both conventions give 36
])
def test_half_way_percentages_accept_either_convention(reply, ok):
    result = tool({"a": 12.345, "b": 3.45, "c": 35.5})
    assert (unsupported_facts(f"It was {reply}.", result) == []) is ok


def test_made_up_percentage_is_caught():
    assert unsupported_facts("Spending rose 25%.", SPEND) == ["25%"]


# ---- counts --------------------------------------------------------------------------------------


@pytest.mark.parametrize("reply", ["You have 3 orders.", "You have 3 recent orders.", "2 of your orders are on the way."])
def test_counts_matching_the_results_pass(reply):
    # "3" is the length of the result list; "2" isn't in the results, so only the first two pass
    problems = unsupported_facts(reply, ORDERS)
    assert problems == ([] if reply.startswith("You") else ["2 orders"])


def test_made_up_count_is_caught():
    assert unsupported_facts("You placed 12 orders this year.", ORDERS) == ["12 orders"]


def test_time_periods_and_single_things_are_not_counts():
    reply = "Returns are accepted within 30 days, and 1 order is still open. It takes 7 days."
    assert unsupported_facts(reply, ORDERS) == []


def test_a_count_the_customer_asked_for_passes():
    asked = [ModelRequest(parts=[UserPromptPart(content="show my top 5 products")])]
    assert unsupported_facts("Here are your 5 products.", asked + ORDERS) == []


# ---- dates ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("reply", ["It was delivered on 2026-09-03.", "It was delivered on September 3, 2026.",
                                   "From 2025-05-05 to 2026-10-12 you spent $477.95.", "In 2025-05 you spent more."])
def test_dates_from_results_pass(reply):
    assert unsupported_facts(reply, SPEND + ORDERS) == []


@pytest.mark.parametrize("reply,bad", [("It was delivered on 2026-09-30.", "2026-09-30"),
                                       ("It arrives on October 20, 2026.", "2026-10-20")])
def test_made_up_dates_are_caught(reply, bad):
    assert unsupported_facts(reply, SPEND + ORDERS) == [bad]


def test_earlier_verified_replies_count_as_evidence():
    assert unsupported_facts("As I said, it rose 19.94% since 2025-05-05.", [],
                             prior_replies="Spending rose 19.94% (from 2025-05-05).") == []


def test_counts_with_thousands_separators_are_read_whole():
    """"1,850 orders" was read as "850 orders" and a correct reply was rejected (found in seller step 8)."""
    big = tool({"all_time": {"orders": 1850, "units": 2028}})
    assert unsupported_facts("You have 1,850 orders and 2,028 units sold.", big) == []
    assert unsupported_facts("You have 1,900 orders.", big) == ["1900 orders"]


@pytest.mark.parametrize("reply", ["Your lowest-rated product with at least 5 reviews is X.",
                                   "Products with more than 10 orders sold well.", "orders over 3 items"])
def test_thresholds_are_conditions_not_facts(reply):
    assert unsupported_facts(reply, ORDERS) == []


def test_a_made_up_count_is_still_caught_next_to_a_threshold():
    assert unsupported_facts("With at least 5 reviews, you got 40 orders.", ORDERS) == ["40 orders"]

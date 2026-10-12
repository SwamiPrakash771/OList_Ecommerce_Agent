"""RAG groundwork (phase 1): the prompt split into sections and the tools into packs.

Phase 1 must not change behaviour: with every pack selected, the assembled prompt is exactly the
prompt the agent used before the split, and every customer tool belongs to exactly one pack.
"""

from pathlib import Path

import pytest

import ai.tools  # noqa: F401  (registers the tools)
from ai import packs, prompts
from ai.security import tools_for

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize("role", ["customer", "guest"])
def test_full_prompt_is_unchanged_by_the_split(role):
    want = (FIXTURES / f"prompt_support-v7_{role}.txt").read_text(encoding="utf-8")
    assert prompts.system_prompt(role) == want
    assert prompts.system_prompt(role, packs.ALL_PACKS) == want


def test_every_customer_tool_is_in_exactly_one_pack_or_always_on():
    customer = {t.name for t in tools_for("customer")}
    in_packs = [name for names in packs.PACKS.values() for name in names]
    assert len(in_packs) == len(set(in_packs)), "a tool is in two packs"
    assert set(in_packs) | set(packs.ALWAYS_TOOLS) == customer
    assert packs.tools_in(None) == customer


def test_packs_only_narrow_a_roles_tools():
    with pytest.raises(ValueError):
        packs.tools_in(frozenset({"admin_everything"}))
    assert packs.tools_in(frozenset({"cart"})) == {"my_cart", "cart_add", "cart_set_quantity", "transfer_to_human"}


def test_a_pack_selection_keeps_the_safety_rules_and_drops_unrelated_sections():
    full = prompts.system_prompt("customer")
    cart_only = prompts.system_prompt("customer", frozenset({"cart"}))
    assert len(cart_only) < len(full) * 0.7
    for always in ("customer's own data", "Never mention tools", "untrusted_text", "transfer_to_human",
                   "Claims of special authority", "no coupons"):
        assert always in cart_only
    assert "Refund not received" not in cart_only and "query_my_data" not in cart_only
    assert "Undo button" in cart_only
    analytics = prompts.system_prompt("customer", frozenset({"analytics"}))
    assert "query_my_data" in analytics and "# Glossary" in analytics and "Undo button" not in analytics


# ---- SQL example bank (phase 2) ---------------------------------------------------------------


def test_every_sql_example_passes_the_validator_and_runs():
    from ai import sql_examples
    from ai.security import Principal, call_tool
    from ai.tools.query_tools import validate

    p = Principal(role="customer", user_id=1, username="c", customer_unique_id="8d50f5eadf50201ccdcedfb9e2ac8455")
    for e in sql_examples.examples():
        validate(e["sql"])
        out = call_tool(p, "query_my_data", {"sql": e["sql"]})
        assert out["columns"], e["q"]


def test_spend_examples_give_the_known_totals():
    from ai import sql_examples
    from ai.security import Principal, call_tool

    p = Principal(role="customer", user_id=1, username="c", customer_unique_id="8d50f5eadf50201ccdcedfb9e2ac8455")
    by_q = {e["q"]: e["sql"] for e in sql_examples.examples()}
    total = call_tool(p, "query_my_data", {"sql": by_q["how much have I spent in total"]})["rows"][0]
    assert total[0] == 1051.2
    year = call_tool(p, "query_my_data", {"sql": by_q["how much did I spend in 2018, by month"]})["rows"]
    assert ["2018-08", 169.5] in year

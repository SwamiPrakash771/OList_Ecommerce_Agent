"""Each assistant's system prompt carries only its own slice of the data catalog ("# Your data"),
generated from ai/metadata.yaml; the query tools point to it instead of repeating it."""

import re

from ai import prompts
from ai.admin.prompts import system_prompt as admin_prompt
from ai.admin.tools.query import query_platform_data
from ai.seller.prompts import system_prompt as seller_prompt
from ai.seller.tools.query import query_seller_data
from ai.tools.query_tools import query_my_data


def tables(prompt: str) -> set[str]:
    section = prompt.split("# Your data", 1)[1]
    return set(re.findall(r"^- ([a-z_]+) \(", section, re.M))


def test_customer_sees_only_their_own_tables():
    got = tables(prompts.system_prompt("customer"))
    assert got and all(t.startswith("my_") or t == "catalog" for t in got)


def test_seller_sees_only_their_own_tables():
    got = tables(seller_prompt())
    assert got and all(t.startswith("my_") for t in got)


def test_admin_sees_platform_tables_without_personal_columns():
    prompt = admin_prompt()
    assert tables(prompt) and not any(t.startswith("my_") for t in tables(prompt))
    section = prompt.split("# Your data", 1)[1].lower()
    for personal in ("customer_unique_id", "email", "phone", "password", "street", "zip_code"):
        assert personal not in section


def test_guests_get_no_data_section():
    assert "# Your data" not in prompts.system_prompt("guest")


def test_query_tools_point_to_the_prompt_instead_of_repeating_the_tables():
    for tool, sample in ((query_my_data, "my_orders ("), (query_seller_data, "my_sales ("),
                         (query_platform_data, "platform_orders (")):
        assert sample not in tool.__doc__ and '"Your data"' in tool.__doc__

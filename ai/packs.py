"""Tool packs: fixed groups of tools that the router (RAG) can send or leave out per message.

Packs are fixed (not a free top-k of tools) so the few combinations that occur keep hitting the
provider's prompt cache. Packs only ever narrow a role's tools: the agent still gets them from
`tools_for(role)`, and `call_tool` still checks every call, so routing can't grant access.
"""

from __future__ import annotations

PACKS: dict[str, tuple[str, ...]] = {
    "orders": ("my_orders", "track_order"),
    "returns": ("get_return_status", "get_refund_status", "request_return"),
    "cart": ("my_cart", "cart_add", "cart_set_quantity"),
    "account": ("get_address", "update_address", "my_tickets", "open_ticket"),
    "analytics": ("query_my_data",),
    "catalog": ("search_products", "product_details", "list_categories"),
}

# Sent with every pack selection: escalation must always be possible.
ALWAYS_TOOLS: tuple[str, ...] = ("transfer_to_human",)

ALL_PACKS = frozenset(PACKS)


def tools_in(packs: frozenset[str] | None) -> set[str]:
    """Tool names for a pack selection; None means every pack."""
    selected = ALL_PACKS if packs is None else packs
    unknown = selected - ALL_PACKS
    if unknown:
        raise ValueError(f"unknown packs: {sorted(unknown)}")
    return {name for pack in selected for name in PACKS[pack]} | set(ALWAYS_TOOLS)


def pack_of(tool_name: str) -> str | None:
    for pack, names in PACKS.items():
        if tool_name in names:
            return pack
    return None

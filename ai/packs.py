"""Tool packs: fixed groups of tools that the router (RAG) can send or leave out per message.

Packs are fixed (not a free top-k of tools) so the few combinations that occur keep hitting the
provider's prompt cache. Packs only ever narrow a role's tools: the agent still gets them from
`tools_for(role)`, and `call_tool` still checks every call, so routing can't grant access.

The two general read tools (look_up_order: tracking, returns and refunds of an order; my_account: orders,
cart, address and tickets) serve most packs' questions, so they are always sent, like the hand-off.
Each other tool is in exactly one pack; a pack's prompt sections are still chosen by the pack.
"""

from __future__ import annotations

PACKS: dict[str, tuple[str, ...]] = {
    "orders": (),
    "returns": ("request_return",),
    "cart": ("update_cart",),
    "account": ("update_address", "open_ticket"),
    "analytics": ("query_my_data",),
    "catalog": ("find_products",),
}

# Sent with every pack selection: escalation must always be possible, and the two general read tools
# answer order, refund, cart, address and ticket questions alike.
ALWAYS_TOOLS: tuple[str, ...] = ("transfer_to_human", "look_up_order", "my_account")

# The pack each tool belonged to before the consolidation (17 -> 9 tools), so recorded router-evaluation
# questions that name the old tools still map to the right pack.
LEGACY_PACK = {
    "my_orders": "orders", "track_order": "orders",
    "get_return_status": "returns", "get_refund_status": "returns",
    "my_cart": "cart", "cart_add": "cart", "cart_set_quantity": "cart",
    "get_address": "account", "my_tickets": "account",
    "search_products": "catalog", "product_details": "catalog", "list_categories": "catalog",
}

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
    return LEGACY_PACK.get(tool_name)

# Customer agent: tool consolidation (17 → 9)

The same step the admin agent took (docs/admin/ADMIN_AGENT.md, step 8): fewer, general tools that don't
overlap, so the model has fewer near-duplicate choices, and every question is sent with fewer tool
descriptions. Each tool's question was checked: *what does it do that the others can't?*

**Why:**
- In real chats the model used the free-SQL tool most (40 calls), while `product_details`, `request_return`,
  `open_ticket` and `my_tickets` were never chosen.
- "Where's my refund?" could take 2–3 calls (tracking, return, refund).

## What changed

| Before | Now | Why |
|---|---|---|
| `search_products`, `product_details`, `list_categories` | **`find_products(view = search / details / categories)`** | One question: "show me products". Guests use it too |
| `track_order`, `get_return_status`, `get_refund_status` | **`look_up_order(order_id, about = everything / tracking / returns / refunds)`** | One call gives the whole order story: tracking, returns and refunds. Without an order ID it picks the most recent relevant order, as before |
| `my_orders`, `my_cart`, `get_address`, `my_tickets` | **`my_account(view = orders / cart / address / tickets)`** | Simple "show me my X" views. Orders gain date filters (`since` / `until`), and the result says "5 of 8, newest first" |
| `cart_add`, `cart_set_quantity` | **`update_cart(product_id, quantity, mode = add / set)`** | Same action, same undo. `set` now also works for a product not yet in the cart |
| `query_my_data` | unchanged | The general fallback for totals, averages and comparisons |
| `request_return`, `update_address`, `open_ticket`, `transfer_to_human` | unchanged | Different consequences and confirm cards; merging writes would make them harder to keep safe |

**Results keep their shapes:** each view or `about` returns exactly what the old tool returned (checked
field by field). So the hybrid fast path, its reply templates and the hand-off facts work unchanged.

## Updated with it

| Where | Change |
|---|---|
| Prompt `support-v11` | Tool names only; every rule keeps its meaning (the diff from v10 is 6 lines) |
| Engine (`ai/agent.py`) | Step labels for 9 tools; the cart-changed event for `update_cart` |
| Hybrid fast path (`ai/fast_path.py`) | Calls `look_up_order` / `my_account` with the matching `about` / `view` |
| RAG packs (`ai/packs.py`) | `look_up_order` and `my_account` are always sent (like the hand-off); every other tool is in exactly one pack. Recorded router questions that name old tools map through `LEGACY_PACK`. RAG routing is off by default |
| Scripts | Cost benchmark expectations, red-team wording |
| Tests | New names. The permission probes run **every view** of a general tool on the restricted MySQL user, and the order checks run for every `about` |

## Testing

Evaluated in **LLM mode only** (no hybrid fast path), with **RAG routing off**: the 37 natural customer
questions and the 5 tricky ones, on the configured model, compared with the results before the
consolidation (docs/MODEL_COMPARISON.md).

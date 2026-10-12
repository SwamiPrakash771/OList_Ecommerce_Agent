"""Offline check of the hybrid router on questions written AFTER the example bank was tuned.

Each question lists the local intents that are acceptable; "LLM" means it must go to the LLM
(calculations, actions, follow-ups). A local answer with any other intent is a critical error:
the customer would get an answer to a different question. Falling back to the LLM when a local
answer was allowed only costs money.

    python -m ai.fast_path_eval
"""

from __future__ import annotations

import ai.tools  # noqa: F401
from ai import fast_path

HELD_OUT = [
    ("hey where's my money for the returned shoes", {"refund_status", "LLM"}),
    ("has my last purchase been shipped yet?", {"track_order", "LLM"}),
    ("can you show me what's in my shopping cart", {"cart_view", "LLM"}),
    ("what's the address you ship to", {"address_view", "LLM"}),
    ("how long do I have to send something back?", {"policy_returns", "LLM"}),
    ("got any promo codes?", {"policy_discounts", "LLM"}),
    ("how much did I spend in 2024?", {"LLM"}),
    ("what's my most expensive order", {"LLM"}),
    ("who else bought this watch?", {"decline_others_data", "LLM"}),
    ("can you cancel the order I just made", {"decline_cancel", "LLM"}),
    ("are you a real person?", {"about_internals", "about_capabilities", "LLM"}),
    ("write me a limerick", {"decline_offtopic", "LLM"}),
    ("add two of the cheapest watches to my cart", {"LLM"}),
    ("change my delivery address to rua augusta 500", {"LLM"}),
    ("quero saber do meu reembolso", {"refund_status", "LLM"}),
    ("thanks a ton!", {"thanks", "LLM"}),
]


def main() -> None:
    fast_path.warm_up()
    local = critical = 0
    for question, allowed in HELD_OUT:
        d = fast_path.classify(question)
        path = d.intent or "LLM"
        ok = path in allowed
        local += d.intent is not None
        critical += not ok
        print(f"{'ok  ' if ok else 'BAD '} {path:20s} {d.score:.2f} m{d.margin:+.2f}  {question}")
    print(f"\nanswered locally: {local}/{len(HELD_OUT)} · wrong-intent answers (must be 0): {critical}")


if __name__ == "__main__":
    main()

"""Store policies used by the support tools (and quoted in the agent's system prompt).

Single source of truth: tools enforce these numbers, and the prompt describes the same ones.
"""

# Returns: days after delivery a return may be requested.
RETURN_WINDOW_DAYS = {"changed_mind": 7, "default": 30}

# Refunds: expected days from "sent to the payment provider" (processing_started_at) to money
# back, by method. Longer than this is a breach of the refund SLA ("stuck").
REFUND_SLA_DAYS = {"voucher": 2, "debit_card": 5, "boleto": 7, "credit_card": 12}

# Human support response time promised when a ticket is opened or a chat is handed over.
HUMAN_RESPONSE_HOURS = 24

TICKET_PRIORITY = {
    "damaged": "high", "wrong_item": "high", "refund": "high", "late": "normal",
    "return": "normal", "other": "normal", "human_request": "normal",
}

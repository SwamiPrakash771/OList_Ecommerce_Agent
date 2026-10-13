"""Customer support tools: orders, tracking, returns, refunds, cart, address, tickets, handoff.

Every tool works only on the signed-in customer's own data: identity comes from the
Principal (never from arguments), and any order/return/refund lookup that isn't theirs
fails with the same "not found" as a missing one. Outputs are built field by field
(no raw rows), so internal ids such as seller_id never reach the agent.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from typing import Literal

from pydantic import Field, ValidationError

import db
from ai import store
from ai.clock import days_since, reference_now
from ai.policies import HUMAN_RESPONSE_HOURS, REFUND_SLA_DAYS, RETURN_WINDOW_DAYS, TICKET_PRIORITY
from ai.security import NotFound, Principal, ToolError, ToolKind, current_conversation, tool
from ai.tools.common import as_dt, delivery_status, iso, num
from repository import CustomerRepository
from validation import AddressBody

CUSTOMER = ("customer",)
OrderId = Field(..., min_length=32, max_length=32, description="The 32-character order ID")
AnyOrderId = Field(None, min_length=32, max_length=32,
                   description="Order ID; omit to use the customer's most recent relevant order")
ProductId = Field(..., min_length=32, max_length=32, description="The 32-character product ID")


def _repo(principal: Principal) -> CustomerRepository:
    return CustomerRepository(principal.customer_unique_id)


def _owned_order(principal: Principal, order_id: str) -> dict:
    """The order header if it belongs to the caller; NotFound otherwise."""
    row = db.query_one(
        """
        SELECT o.order_id, o.order_status, o.order_purchase_timestamp, o.order_delivered_customer_date,
               o.order_estimated_delivery_date
        FROM orders o JOIN customers c USING (customer_id)
        WHERE o.order_id = %s AND c.customer_unique_id = %s
        """,
        (order_id, principal.customer_unique_id),
    )
    if not row:
        raise NotFound("order")
    return row


_PICK_SQL = {
    "order": """SELECT o.order_id FROM orders o JOIN customers c USING (customer_id)
                WHERE c.customer_unique_id = %s ORDER BY o.order_purchase_timestamp DESC LIMIT 4""",
    "return": """SELECT order_id FROM returns WHERE customer_unique_id = %s
                 GROUP BY order_id ORDER BY MAX(requested_at) DESC LIMIT 4""",
    "refund": """SELECT f.order_id FROM refunds f JOIN returns r USING (return_id) WHERE r.customer_unique_id = %s
                 GROUP BY f.order_id ORDER BY MAX(f.created_at) DESC LIMIT 4""",
}


def _pick_order(principal: Principal, order_id: str | None, kind: str) -> tuple[str | None, dict]:
    """The given order, or the customer's most recent one with a {kind}. The extra dict tells the
    model the order was chosen for it and which other candidates exist, so it can ask if unsure."""
    if order_id:
        return order_id, {}
    ids = list(db.query_df(_PICK_SQL[kind], (principal.customer_unique_id,)).order_id)
    if not ids:
        return None, {"note": "The customer has no orders." if kind == "order" else f"The customer has no order with a {kind}."}
    return ids[0], {"order_chosen": f"most recent order with a {kind}" if kind != "order" else "most recent order",
                    "other_candidates": ids[1:]}


# ---- orders and tracking -------------------------------------------------------------------------


OrderStatus = Literal["any", "delivered", "shipped", "processing", "invoiced", "approved", "created",
                      "canceled", "unavailable"]


def my_orders(principal: Principal, status: str = "any", limit: int = 5,
              since: date | None = None, until: date | None = None) -> dict:
    """The customer's recent orders, newest first: status, total, dates, delivery state."""
    orders = _repo(principal).orders()
    if status != "any":
        orders = orders[orders["status"] == status]
    if since or until:   # purchase dates, inclusive
        bought = orders["purchased_at"].apply(lambda v: as_dt(v).date() if as_dt(v) else None)
        orders = orders[bought.apply(lambda d: d is not None and (not since or d >= since) and (not until or d <= until))]
    total = len(orders)
    orders = orders.head(limit).to_dict("records")
    returned = set()
    if orders:
        ids = [o["order_id"] for o in orders]
        returned = set(db.query_df(
            f"SELECT DISTINCT order_id FROM returns WHERE customer_unique_id = %s "
            f"AND order_id IN ({', '.join(['%s'] * len(ids))})",
            (principal.customer_unique_id, *ids),
        ).order_id) if ids else set()
    return {
        "total_orders": total,
        "orders_shown": f"{min(total, limit)} of {total}, newest first",
        **({"purchased_between": f"{since or 'first order'} to {until or 'today'}"} if since or until else {}),
        "orders": [{
            "order_id": o["order_id"],
            "purchased_at": iso(o["purchased_at"]),
            "status": o["status"],
            "items": int(o["items"]),
            "order_total": num(o["order_total"]),
            "estimated_delivery": iso(o["estimated_delivery"]),
            "delivered_at": iso(o["delivered_at"]),
            "delivery": delivery_status(o["status"], o["purchased_at"], o["estimated_delivery"], o["delivered_at"]),
            "has_return": o["order_id"] in returned,
        } for o in orders],
    }


def track_order(principal: Principal, order_id: str | None = None) -> dict:
    """Tracking for an order: status, timeline, late or on time, items, payments, destination."""
    order_id, picked = _pick_order(principal, order_id, "order")
    if not order_id:
        return picked
    o = _repo(principal).order_detail(order_id)
    if not o:
        raise NotFound("order")
    items: dict[str, dict] = {}
    for i in o["items"].to_dict("records"):  # Olist stores quantity as repeated rows
        it = items.setdefault(i["product_id"], {
            "product_id": i["product_id"], "product": i["product"], "category": i["category"],
            "quantity": 0, "unit_price": num(i["price"]), "unit_shipping": num(i["freight"]),
            "seller_city": i["seller_city"], "seller_state": i["seller_state"],
        })
        it["quantity"] += 1
    addr = o.get("shipping_address")
    return {
        "order_id": o["order_id"],
        "status": o["order_status"],
        "timeline": {
            "ordered": iso(o["order_purchase_timestamp"]),
            "payment_approved": iso(o["order_approved_at"]),
            "shipped": iso(o["order_delivered_carrier_date"]),
            "delivered": iso(o["order_delivered_customer_date"]),
            "estimated_delivery": iso(o["order_estimated_delivery_date"]),
        },
        "delivery": delivery_status(o["order_status"], o["order_purchase_timestamp"],
                                    o["order_estimated_delivery_date"], o["order_delivered_customer_date"]),
        "items": list(items.values()),
        "items_total": num(sum(v["quantity"] * ((v["unit_price"] or 0) + (v["unit_shipping"] or 0)) for v in items.values())),
        "payments": [{"type": p["type"], "installments": int(p["installments"]), "amount": num(p["amount"])}
                     for p in o["payments"].to_dict("records")],
        "ships_to": ({"city": addr["city"], "state": addr["state"], "zip_code": addr["zip_code"]} if addr
                     else {"city": o["customer_city"], "state": o["customer_state"]}),
        "your_review_score": o["review"]["review_score"] if o.get("review") else None,
        **picked,
    }


# ---- returns and refunds ---------------------------------------------------------------------------


def get_return_status(principal: Principal, order_id: str | None = None) -> dict:
    """Returns on an order: reason, status, dates, decision note, items."""
    order_id, picked = _pick_order(principal, order_id, "return")
    if not order_id:
        return picked
    _owned_order(principal, order_id)
    rows = db.query_df(
        """
        SELECT return_id, reason, status, requested_at, decided_at, received_at, decision_note, items
        FROM returns WHERE order_id = %s AND customer_unique_id = %s ORDER BY requested_at
        """,
        (order_id, principal.customer_unique_id),
    ).to_dict("records")
    out = []
    for r in rows:
        items = store._loads(r["items"])
        names = {x["product_id"]: x["display_name"] for x in db.query_df(
            f"SELECT product_id, display_name FROM products WHERE product_id IN ({', '.join(['%s'] * len(items))})",
            tuple(i["product_id"] for i in items),
        ).to_dict("records")} if items else {}
        out.append({
            "return_id": r["return_id"], "reason": r["reason"], "status": r["status"],
            "requested_at": iso(r["requested_at"]), "decided_at": iso(r["decided_at"]),
            "received_at": iso(r["received_at"]), "decision_note": r["decision_note"],
            "items": [{"product_id": i["product_id"], "product": names.get(i["product_id"])} for i in items],
        })
    return {"order_id": order_id, "returns": out, **picked}


def get_refund_status(principal: Principal, order_id: str | None = None) -> dict:
    """Refunds on an order: amount, method, status, dates, history, and SLA (expected time) check."""
    order_id, picked = _pick_order(principal, order_id, "refund")
    if not order_id:
        return picked
    _owned_order(principal, order_id)
    refunds = db.query_df(
        """
        SELECT f.refund_id, f.return_id, f.amount, f.payment_method, f.status, f.created_at,
               f.processing_started_at, f.completed_at, f.failure_reason
        FROM refunds f JOIN returns r USING (return_id)
        WHERE f.order_id = %s AND r.customer_unique_id = %s ORDER BY f.created_at
        """,
        (order_id, principal.customer_unique_id),
    ).to_dict("records")
    out = []
    for f in refunds:
        events = db.query_df("SELECT status, note, created_at FROM refund_events WHERE refund_id = %s ORDER BY created_at",
                             (f["refund_id"],)).to_dict("records")
        expected = REFUND_SLA_DAYS.get(f["payment_method"], 7)
        started = as_dt(f["processing_started_at"])
        sla = {"expected_within_days": expected}
        if f["status"] == "processing" and started:
            age = days_since(started)
            sla.update(days_in_processing=age, breached=age > expected,
                       expected_by=iso((started + timedelta(days=expected)).date()))
        elif f["status"] == "completed" and started and as_dt(f["completed_at"]):
            took = (as_dt(f["completed_at"]) - started).days
            sla.update(days_taken=took, breached=took > expected)
        out.append({
            "refund_id": f["refund_id"], "return_id": f["return_id"], "amount": num(f["amount"]),
            "payment_method": f["payment_method"], "status": f["status"],
            "created_at": iso(f["created_at"]), "processing_started_at": iso(f["processing_started_at"]),
            "completed_at": iso(f["completed_at"]), "failure_reason": f["failure_reason"],
            "sla": sla,
            "history": [{"status": e["status"], "note": e["note"], "at": iso(e["created_at"])} for e in events],
        })
    return {"order_id": order_id, "refunds": out, **picked,
            **({} if out else {"note": "No refund on this order yet; refunds are created once a return is received."})}


def _return_check(principal: Principal, order_id: str, reason: str, product_ids: list[str] | None) -> dict:
    """Validate a return request against the policy; returns the lines to return."""
    o = _owned_order(principal, order_id)
    if o["order_status"] != "delivered" or not o["order_delivered_customer_date"]:
        raise ToolError("Only delivered orders can be returned.")
    window = RETURN_WINDOW_DAYS.get(reason, RETURN_WINDOW_DAYS["default"])
    delivered = as_dt(o["order_delivered_customer_date"])
    age = (reference_now(delivered) - delivered).days
    if age > window:
        raise ToolError(f"The return window for '{reason}' is {window} days after delivery; "
                        f"this order was delivered {age} days ago, so it can't be returned. "
                        f"Offer to connect the customer with a human if they want an exception reviewed.")
    lines = db.query_df(
        """
        SELECT i.order_item_id, i.product_id, p.display_name AS product, i.price, i.freight_value
        FROM order_items i JOIN products p USING (product_id) WHERE i.order_id = %s ORDER BY i.order_item_id
        """,
        (order_id,),
    ).to_dict("records")
    if product_ids:
        unknown = set(product_ids) - {line["product_id"] for line in lines}
        if unknown:
            raise ToolError("Some of those products are not in this order.")
        lines = [line for line in lines if line["product_id"] in set(product_ids)]
    open_returns = db.query_df(
        "SELECT items FROM returns WHERE order_id = %s AND customer_unique_id = %s AND status <> 'rejected'",
        (order_id, principal.customer_unique_id),
    ).to_dict("records")
    already = {(i["order_item_id"]) for r in open_returns for i in store._loads(r["items"])}
    lines = [line for line in lines if line["order_item_id"] not in already]
    if not lines:
        raise ToolError("Those items already have a return request.")
    return {"order": o, "lines": lines, "window_days": window, "days_since_delivery": age}


def request_return_preview(principal: Principal, order_id: str = OrderId,
                           reason: Literal["damaged", "wrong_item", "not_as_described", "changed_mind", "late_delivery"] = Field(...),
                           product_ids: list[str] | None = None) -> dict:
    c = _return_check(principal, order_id, reason, product_ids)
    amount = sum(float(line["price"]) + float(line["freight_value"]) for line in c["lines"])
    return {
        "title": "Request a return",
        "lines": [f"Order {order_id}",
                  *[f"Return: {line['product']} ({num(line['price'])} + {num(line['freight_value'])} shipping)" for line in c["lines"]],
                  f"Reason: {reason.replace('_', ' ')}",
                  f"Estimated refund once the item is received: {num(amount)}"],
        "facts": {"order_id": order_id, "items": len(c["lines"]), "estimated_refund": num(amount)},
    }


@tool(roles=CUSTOMER, kind=ToolKind.WRITE_CONFIRM, reads=["orders", "customers", "order_items", "products", "returns"],
      writes=["returns"], preview=request_return_preview)
def request_return(principal: Principal, order_id: str = OrderId,
                   reason: Literal["damaged", "wrong_item", "not_as_described", "changed_mind", "late_delivery"] = Field(...),
                   product_ids: list[str] | None = None) -> dict:
    """Start a return on a delivered order (all items if product_ids omitted). Needs confirmation."""
    c = _return_check(principal, order_id, reason, product_ids)
    import json
    return_id = uuid.uuid4().hex
    db.execute(
        """
        INSERT INTO returns (return_id, order_id, customer_unique_id, items, reason, status, requested_at, source)
        VALUES (%s, %s, %s, %s, %s, 'requested', %s, 'app')
        """,
        (return_id, order_id, principal.customer_unique_id,
         json.dumps([{"order_item_id": int(line["order_item_id"]), "product_id": line["product_id"]} for line in c["lines"]]),
         reason, datetime.now().replace(microsecond=0)),
    )
    return {"return_id": return_id, "status": "requested", "items": len(c["lines"]),
            "next_step": "We'll review the request within 3 business days and email return instructions."}


# ---- cart (applied immediately, with undo) ---------------------------------------------------------------


def _cart_summary(principal: Principal) -> dict:
    cart = _repo(principal).cart().to_dict("records")
    subtotal = sum(float(i["price"]) * int(i["quantity"]) for i in cart)
    shipping = sum(float(i["shipping"]) * int(i["quantity"]) for i in cart)
    return {
        "items": [{"product_id": i["product_id"], "product": i["name"], "quantity": int(i["quantity"]),
                   "unit_price": num(i["price"])} for i in cart],
        "subtotal": num(subtotal), "shipping": num(shipping), "total": num(subtotal + shipping),
    }


def _cart_quantity(principal: Principal, product_id: str) -> int:
    row = db.query_one("SELECT quantity FROM cart_items WHERE customer_unique_id = %s AND product_id = %s",
                       (principal.customer_unique_id, product_id))
    return int(row["quantity"]) if row else 0


def my_cart(principal: Principal) -> dict:
    """The customer's cart with totals."""
    return _cart_summary(principal)


@tool(roles=CUSTOMER, kind=ToolKind.WRITE_UNDO, reads=["cart_items", "product_stats"], writes=["cart_items"])
def update_cart(principal: Principal, product_id: str = ProductId, quantity: int = Field(1, ge=0, le=20),
                mode: Literal["add", "set"] = "add") -> dict:
    """Change the cart (applied at once; the customer can undo): mode "add" adds `quantity` of a product;
    mode "set" makes its quantity exactly `quantity` (0 removes it)."""
    before = _cart_quantity(principal, product_id)
    target = before + quantity if mode == "add" else quantity
    if mode == "add" and quantity == 0:
        raise ToolError("Adding needs a quantity of at least 1.")
    if target == before:
        return {"cart": _cart_summary(principal), "undo": None, "note": "Nothing changed: the cart already has that quantity."}
    try:
        if before == 0:
            _repo(principal).add_to_cart(product_id, target)
        else:
            _repo(principal).set_cart_quantity(product_id, target)
    except ValueError:
        raise NotFound("product") from None
    return {"cart": _cart_summary(principal),
            "undo": {"tool": "update_cart", "args": {"product_id": product_id, "quantity": before, "mode": "set"}}}


# ---- address -------------------------------------------------------------------------------------------------


def get_address(principal: Principal) -> dict:
    """The customer's saved shipping address."""
    a = _repo(principal).address()
    return {k: (iso(v) if k == "updated_at" else v) for k, v in a.items()}


def _address_body(**fields) -> AddressBody:
    try:
        return AddressBody(**fields)
    except ValidationError as exc:
        problems = "; ".join(f"{e['loc'][-1]}: {e['msg'].replace('Value error, ', '')}" for e in exc.errors())
        raise ToolError(f"The address isn't valid: {problems}") from None


def update_address_preview(principal: Principal, recipient_name: str, street: str, number: str, city: str,
                           state: str, zip_code: str, phone: str | None = None, complement: str | None = None,
                           neighborhood: str | None = None) -> dict:
    new = _address_body(recipient_name=recipient_name, street=street, number=number, city=city, state=state,
                        zip_code=zip_code, phone=phone, complement=complement, neighborhood=neighborhood)
    old = _repo(principal).address()

    def fmt(a) -> str:
        if not a.get("street"):
            return f"{a.get('city') or '?'} - {a.get('state') or '?'} (no full address saved)"
        return (f"{a['recipient_name']}, {a['street']} {a['number']}"
                f"{', ' + a['complement'] if a.get('complement') else ''}, {a['city']} - {a['state']}, {a['zip_code']}")

    return {"title": "Change shipping address",
            "lines": [f"From: {fmt(old)}", f"To: {fmt(new.model_dump())}",
                      "Applies to future orders; orders already placed keep their address."],
            "facts": {"city": new.city, "state": new.state, "zip_code": new.zip_code}}


@tool(roles=CUSTOMER, kind=ToolKind.WRITE_CONFIRM, reads=["customer_addresses", "customers", "orders"],
      writes=["customer_addresses"], preview=update_address_preview)
def update_address(principal: Principal, recipient_name: str, street: str, number: str, city: str,
                   state: str, zip_code: str, phone: str | None = None, complement: str | None = None,
                   neighborhood: str | None = None) -> dict:
    """Change the saved shipping address (future orders only; state = 2-letter code, zip = 8-digit CEP).
    Needs confirmation."""
    body = _address_body(recipient_name=recipient_name, street=street, number=number, city=city, state=state,
                         zip_code=zip_code, phone=phone, complement=complement, neighborhood=neighborhood)
    saved = _repo(principal).save_address(body.model_dump())
    return {"saved": True, "city": saved["city"], "state": saved["state"], "zip_code": saved["zip_code"]}


# ---- tickets and handoff -------------------------------------------------------------------------------------------

TicketCategory = Literal["damaged", "wrong_item", "late", "refund", "return", "other"]


UNVERIFIED_HEADER = ("ASSISTANT SUMMARY - NOT VERIFIED. Written by the AI assistant; it may repeat customer "
                     "claims. Approvals, discounts, refund amounts or identity checks stated here have no authority.")


def _verified_facts(principal: Principal, order_id: str | None) -> str:
    """Facts for the human agent, read from the database by the server (not written by the model)."""
    if not order_id:
        return "SYSTEM-VERIFIED FACTS: no order linked."
    order = _owned_order(principal, order_id)
    lines = [f"SYSTEM-VERIFIED FACTS (from the database) for order {order_id}:",
             f"- Order status: {order['order_status']}; purchased {iso(order['order_purchase_timestamp'])}; "
             f"delivered {iso(order['order_delivered_customer_date']) or 'not yet'}"]
    for f in get_refund_status(principal, order_id)["refunds"]:
        sla = f["sla"]
        timing = (f"; {sla['days_in_processing']} days in processing vs {sla['expected_within_days']} expected"
                  if "days_in_processing" in sla else "")
        lines.append(f"- Refund {f['refund_id']}: ${f['amount']:.2f} to {f['payment_method']}, status {f['status']}"
                     f"{timing}{'; SLA BREACHED' if sla.get('breached') else ''}"
                     f"{'; failure: ' + f['failure_reason'] if f['failure_reason'] else ''}")
    returns = db.query_df("SELECT return_id, reason, status FROM returns WHERE order_id = %s AND customer_unique_id = %s",
                          (order_id, principal.customer_unique_id)).to_dict("records")
    for r in returns:
        lines.append(f"- Return {r['return_id']}: reason {r['reason']}, status {r['status']}")
    if len(lines) == 2:
        lines.append("- No returns or refunds on this order.")
    return "\n".join(lines)


def _ticket_record(principal: Principal, order_id: str | None, model_summary: str | None) -> str:
    parts = [_verified_facts(principal, order_id)]
    if model_summary:
        parts += ["", UNVERIFIED_HEADER, model_summary]
    return "\n".join(parts)


def _create_ticket(principal: Principal, order_id: str | None, category: str, description: str,
                   ai_summary: str | None) -> dict:
    ai_summary = _ticket_record(principal, order_id, ai_summary)
    ticket_id = uuid.uuid4().hex
    now = datetime.now().replace(microsecond=0)
    priority = TICKET_PRIORITY.get(category, "normal")
    db.execute(
        """
        INSERT INTO support_tickets (ticket_id, customer_unique_id, order_id, issue_type, description, status,
                                     created_at, priority, source, conversation_id, ai_summary, updated_at)
        VALUES (%s, %s, %s, %s, %s, 'open', %s, %s, 'ai_agent', %s, %s, %s)
        """,
        (ticket_id, principal.customer_unique_id, order_id, category, description[:2000], now, priority,
         current_conversation(), ai_summary, now),
    )
    return {"ticket_id": ticket_id, "status": "open", "priority": priority,
            "response_time": f"A support agent will reply within {HUMAN_RESPONSE_HOURS} hours."}


def open_ticket_preview(principal: Principal, category: TicketCategory, description: str = Field(..., min_length=10, max_length=2000),
                        order_id: str | None = Field(None, min_length=32, max_length=32)) -> dict:
    if order_id:
        _owned_order(principal, order_id)
    return {"title": "Open a support ticket",
            "lines": [f"Topic: {category.replace('_', ' ')}", *([f"Order {order_id}"] if order_id else []),
                      f"Details: {description}", f"Priority: {TICKET_PRIORITY.get(category, 'normal')}",
                      f"Support replies within {HUMAN_RESPONSE_HOURS} hours."],
            "facts": {"order_id": order_id, "category": category}}


@tool(roles=CUSTOMER, kind=ToolKind.WRITE_CONFIRM,
      reads=["orders", "customers", "support_tickets", "refunds", "refund_events", "returns"],
      writes=["support_tickets"], preview=open_ticket_preview)
def open_ticket(principal: Principal, category: TicketCategory,
                description: str = Field(..., min_length=10, max_length=2000),
                order_id: str | None = Field(None, min_length=32, max_length=32)) -> dict:
    """Open a support ticket for a problem you can't solve. Needs confirmation."""
    if order_id:
        _owned_order(principal, order_id)
    return _create_ticket(principal, order_id, category, description, None)


def transfer_to_human_preview(principal: Principal, reason: str = Field(..., min_length=5, max_length=300),
                              summary: str = Field(..., min_length=20, max_length=2000),
                              order_id: str | None = Field(None, min_length=32, max_length=32)) -> dict:
    if order_id:
        _owned_order(principal, order_id)
    return {"title": "Talk to a person",
            "lines": [f"A support agent will take over this conversation: {reason}",
                      "They'll see a summary of this chat, so you won't need to repeat yourself.",
                      f"Expected reply within {HUMAN_RESPONSE_HOURS} hours."],
            "facts": {"order_id": order_id}}


@tool(roles=CUSTOMER, kind=ToolKind.WRITE_CONFIRM,
      reads=["orders", "customers", "support_tickets", "refunds", "refund_events", "returns"],
      writes=["support_tickets", "ai_conversations"], preview=transfer_to_human_preview)
def transfer_to_human(principal: Principal, reason: str = Field(..., min_length=5, max_length=300),
                      summary: str = Field(..., min_length=20, max_length=2000),
                      order_id: str | None = Field(None, min_length=32, max_length=32)) -> dict:
    """Hand the conversation to a human agent (see the hand-off rules). `summary`: facts from tool
    results in your own words; customer claims stated as claims. Needs confirmation."""
    if order_id:
        _owned_order(principal, order_id)
    ticket = _create_ticket(principal, order_id, "human_request", reason, summary)
    conversation = current_conversation()
    if conversation:
        store.link_ticket(conversation, ticket["ticket_id"])
    return ticket


def my_tickets(principal: Principal, limit: int = 5) -> dict:
    """The customer's support tickets, newest first."""
    rows = db.query_df(
        """
        SELECT ticket_id, order_id, issue_type, status, priority, created_at, updated_at
        FROM support_tickets WHERE customer_unique_id = %s ORDER BY created_at DESC LIMIT %s
        """,
        (principal.customer_unique_id, limit),
    ).to_dict("records")
    return {"tickets": [{**r, "created_at": iso(r["created_at"]), "updated_at": iso(r["updated_at"])} for r in rows]}



# ---- the general read tools (each replaces several narrow ones; results keep their shapes) --------------


@tool(roles=CUSTOMER,
      reads=["orders", "customers", "order_items", "products", "payments", "order_addresses", "reviews", "sellers",
             "returns", "refunds", "refund_events"])
def look_up_order(principal: Principal, order_id: str | None = AnyOrderId,
                  about: Literal["everything", "tracking", "returns", "refunds"] = "everything") -> dict:
    """One of the customer's orders. about "everything" (default): tracking (status, timeline, late or on time,
    items, payments, destination) plus its returns and refunds (status, dates, amounts, refund SLA check).
    about "tracking", "returns" or "refunds": just that part. Without order_id it picks the most recent order
    (for "returns"/"refunds": the most recent one that has one) and lists the other candidates, so you can ask
    if unsure which order the customer means."""
    if about == "tracking":
        return track_order(principal, order_id=order_id)
    if about == "returns":
        return get_return_status(principal, order_id=order_id)
    if about == "refunds":
        return get_refund_status(principal, order_id=order_id)
    out = track_order(principal, order_id=order_id)
    if "order_id" not in out:            # the customer has no orders
        return out
    returns = get_return_status(principal, order_id=out["order_id"])["returns"]
    refunds = get_refund_status(principal, order_id=out["order_id"])
    return {**out, "returns": returns, "refunds": refunds["refunds"],
            **({"refund_note": refunds["note"]} if refunds.get("note") else {})}


@tool(roles=CUSTOMER, reads=["orders", "customers", "order_items", "returns", "cart_items", "product_stats",
                             "customer_addresses", "support_tickets"])
def my_account(principal: Principal, view: Literal["orders", "cart", "address", "tickets"],
               status: OrderStatus = "any", limit: int = Field(5, ge=1, le=20),
               since: date | None = None, until: date | None = None) -> dict:
    """The customer's account:
    - view "orders": recent orders, newest first (status, total, dates, delivery, whether it has a return);
      filter by status and by purchase dates (since / until, YYYY-MM-DD); total_orders counts all that match.
    - view "cart": the cart with totals.
    - view "address": the saved shipping address.
    - view "tickets": support tickets, newest first.
    For totals, averages or comparisons over orders, use the data query instead."""
    if view == "orders":
        return my_orders(principal, status=status, limit=limit, since=since, until=until)
    if view == "cart":
        return my_cart(principal)
    if view == "address":
        return get_address(principal)
    return my_tickets(principal, limit=limit)

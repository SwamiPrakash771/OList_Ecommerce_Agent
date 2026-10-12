"""Hybrid routing: answer simple questions on our own server, without the LLM.

A local embedding model (ai/local_embeddings.py) compares the message with example questions per
intent (ai/intent_examples.yaml). When one local intent is a clear, confident match, the intent's
read-only tool runs through the same security layer (call_tool) and a reply template is filled
from the tool's result: no LLM call, no tokens, ~0.1 s. Anything else goes to the LLM agent as
before: calculations, follow-ups, actions, hand-offs, several requests at once, unclear matches,
and any tool result the templates don't cover (they return None to hand over).

Templates only contain tool data and fixed text, so they can't invent facts, and they never name
tools, tables or other internals.
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

import numpy as np
import yaml

from ai import local_embeddings, store
from ai.policies import HUMAN_RESPONSE_HOURS, REFUND_SLA_DAYS, RETURN_WINDOW_DAYS
from ai.security import Principal, ToolError, call_tool, in_conversation

EXAMPLES_FILE = Path(__file__).with_name("intent_examples.yaml")
MIN_SCORE = 0.75      # best example similarity needed to answer locally (wrong local answers cost more than LLM fallbacks)
MIN_MARGIN = 0.06     # ...and this much better than the best example of any other intent
MAX_WORDS = 16        # longer messages usually carry more than one request -> LLM
TOP_K = 5

_ID = re.compile(r"\b[0-9a-f]{32}\b")


@dataclass
class Decision:
    intent: str | None          # None -> LLM
    score: float
    margin: float
    reason: str


@dataclass
class LocalAnswer:
    intent: str
    text: str
    tools: list[str]


# ---- classification -----------------------------------------------------------------------


@functools.lru_cache(maxsize=1)
def _bank() -> tuple[list[str], list[str], np.ndarray]:
    data = yaml.safe_load(EXAMPLES_FILE.read_text(encoding="utf-8"))
    texts, labels = [], []
    for intent, examples in data.items():
        if intent != "llm" and intent not in HANDLERS:
            raise ValueError(f"intent '{intent}' has no handler")
        for e in examples:
            texts.append(e)
            labels.append(intent)
    return texts, labels, local_embeddings.embed(texts)


def warm_up() -> None:
    _bank()


def classify(message: str) -> Decision:
    words = len(message.split())
    if words > MAX_WORDS:
        return Decision(None, 0.0, 0.0, "long message")
    if message.count("?") > 1:
        return Decision(None, 0.0, 0.0, "several questions")
    texts, labels, matrix = _bank()
    scores = matrix @ local_embeddings.embed([message])[0]
    order = np.argsort(-scores)
    best_i = order[0]
    intent, best = labels[best_i], float(scores[best_i])
    other = next((float(scores[i]) for i in order if labels[i] != intent), 0.0)
    margin = best - other
    if intent == "llm":
        return Decision(None, best, margin, "closest to an LLM example")
    if best < MIN_SCORE:
        return Decision(None, best, margin, "low score")
    if margin < MIN_MARGIN:
        return Decision(None, best, margin, "two intents too close")
    return Decision(intent, best, margin, "confident")


# ---- formatting helpers -----------------------------------------------------------------------

PT_MARKERS = re.compile(r"\b(meu|minha|meus|não|nao|você|voce|vocês|quero|cadê|cade|pedido|reembolso|"
                        r"devolução|devolucao|carrinho|obrigad[oa]|olá|ola|qual|quanto|quando|endereço|"
                        r"está|esta|por favor|bom dia|valeu|senha|cartão|cupom)\b", re.I)


def language(message: str) -> str:
    return "pt" if PT_MARKERS.search(message) or re.search(r"[ãõçáéêíóú]", message, re.I) else "en"


def money(v) -> str:
    return f"${float(v):,.2f}"


def day(v: str | None, lang: str) -> str:
    if not v:
        return "?"
    d = datetime.strptime(v[:10], "%Y-%m-%d")
    return d.strftime("%d/%m/%Y") if lang == "pt" else f"{d:%B} {d.day}, {d.year}"


METHOD = {"en": {"credit_card": "credit card", "debit_card": "debit card", "boleto": "boleto", "voucher": "voucher"},
          "pt": {"credit_card": "cartão de crédito", "debit_card": "cartão de débito", "boleto": "boleto",
                 "voucher": "voucher"}}


def T(lang: str, en: str, pt: str) -> str:
    return pt if lang == "pt" else en


def _tool(principal: Principal, conversation_id: str, name: str, args: dict | None = None) -> dict:
    with in_conversation(conversation_id):
        out = call_tool(principal, name, args or {})
    store.record_tool_call(conversation_id, name, "read", args or {}, "ok")
    return out


# ---- handlers: return reply text, or None to hand the question to the LLM ---------------------


def refund_status(p, conv, msg, lang):
    ids = _ID.findall(msg)
    out = _tool(p, conv, "get_refund_status", {"order_id": ids[0]} if ids else {})
    refunds = out.get("refunds")
    if not refunds or len(refunds) != 1:
        return None   # no refund yet (needs the return story) or several: let the LLM explain
    f = refunds[0]
    amount, method, sla = money(f["amount"]), METHOD[lang].get(f["payment_method"], f["payment_method"]), f["sla"]
    if f["status"] == "completed":
        text = T(lang, f"Your refund of {amount} to your {method} was completed on {day(f['completed_at'], lang)}. "
                       "Card refunds can take a statement cycle to show.",
                 f"Seu reembolso de {amount} no {method} foi concluído em {day(f['completed_at'], lang)}. "
                 "Reembolsos no cartão podem levar um ciclo de fatura para aparecer.")
    elif f["status"] == "failed":
        text = T(lang, f"I'm sorry, your refund of {amount} couldn't be completed: {f['failure_reason']}. "
                       "Would you like me to connect you with a person to arrange another refund method?",
                 f"Sinto muito, seu reembolso de {amount} não pôde ser concluído: {f['failure_reason']}. "
                 "Quer que eu encaminhe você para um atendente para combinar outra forma de reembolso?")
    elif f["status"] == "processing" and sla.get("breached"):
        text = T(lang, f"I'm sorry for the delay. Your refund of {amount} to your {method} has been processing for "
                       f"{sla['days_in_processing']} days; it's expected within {sla['expected_within_days']} days. "
                       "Would you like me to connect you with a person to look into it?",
                 f"Sinto muito pela demora. Seu reembolso de {amount} no {method} está em processamento há "
                 f"{sla['days_in_processing']} dias; o prazo esperado é de {sla['expected_within_days']} dias. "
                 "Quer que eu encaminhe você para um atendente?")
    elif f["status"] == "processing":
        text = T(lang, f"Your refund of {amount} to your {method} is on its way: it was sent on "
                       f"{day(f['processing_started_at'], lang)} and should arrive by {day(sla.get('expected_by'), lang)}.",
                 f"Seu reembolso de {amount} no {method} está a caminho: foi enviado em "
                 f"{day(f['processing_started_at'], lang)} e deve chegar até {day(sla.get('expected_by'), lang)}.")
    else:
        return None
    if out.get("other_candidates"):
        text += T(lang, f" You also have refunds on {len(out['other_candidates'])} other order(s); just ask if you "
                        "want those too.",
                  f" Você também tem reembolsos em mais {len(out['other_candidates'])} pedido(s); é só pedir.")
    return text


def return_status(p, conv, msg, lang):
    ids = _ID.findall(msg)
    out = _tool(p, conv, "get_return_status", {"order_id": ids[0]} if ids else {})
    returns = out.get("returns")
    if not returns or len(returns) != 1:
        return None
    r = returns[0]
    items = ", ".join(i["product"] for i in r["items"] if i.get("product")) or T(lang, "your item", "seu item")
    status = r["status"]
    if status == "rejected":
        return T(lang, f"Your return of {items} was not accepted on {day(r['decided_at'], lang)}. Reason: "
                       f"{r['decision_note']}.",
                 f"Sua devolução de {items} não foi aceita em {day(r['decided_at'], lang)}. Motivo: {r['decision_note']}.")
    if status == "received":
        return T(lang, f"We received your return of {items} on {day(r['received_at'], lang)}, and your refund has "
                       "been created.",
                 f"Recebemos sua devolução de {items} em {day(r['received_at'], lang)} e o reembolso foi criado.")
    if status in ("requested", "approved"):
        return T(lang, f"Your return of {items} was requested on {day(r['requested_at'], lang)} and is {status}. "
                       "We'll email the next steps.",
                 f"Sua devolução de {items} foi solicitada em {day(r['requested_at'], lang)} e está "
                 f"{'aprovada' if status == 'approved' else 'em análise'}. Enviaremos os próximos passos por e-mail.")
    return None


def track_order(p, conv, msg, lang):
    ids = _ID.findall(msg)
    out = _tool(p, conv, "track_order", {"order_id": ids[0]} if ids else {})
    if "delivery" not in out:
        return None
    items = ", ".join(f"{i['product']} ×{i['quantity']}" for i in out["items"][:3])
    what = T(lang, f"Your {'most recent ' if out.get('order_chosen') else ''}order ({items}, {money(out['items_total'])})",
             f"Seu {'último ' if out.get('order_chosen') else ''}pedido ({items}, {money(out['items_total'])})")
    d, t = out["delivery"], out["timeline"]
    state = d["state"]
    if state == "delivered_on_time":
        return T(lang, f"{what} was delivered on {day(t['delivered'], lang)}, on time.",
                 f"{what} foi entregue em {day(t['delivered'], lang)}, dentro do prazo.")
    if state == "delivered_late":
        return T(lang, f"{what} was delivered on {day(t['delivered'], lang)}, {d['days_late']} day(s) after the "
                       "estimated date.",
                 f"{what} foi entregue em {day(t['delivered'], lang)}, {d['days_late']} dia(s) após a data prevista.")
    if state == "in_progress":
        return T(lang, f"{what} is on its way; the estimated delivery date is {day(d.get('estimated_delivery'), lang)}.",
                 f"{what} está a caminho; a entrega está prevista para {day(d.get('estimated_delivery'), lang)}.")
    if state == "not_delivered_past_estimate":
        return T(lang, f"I'm sorry: {what} hasn't been delivered yet and is {d['days_late']} day(s) past the "
                       f"estimated date ({day(t['estimated_delivery'], lang)}). Would you like me to open a support "
                       "ticket about it?",
                 f"Sinto muito: {what} ainda não foi entregue e está {d['days_late']} dia(s) atrasado (previsão: "
                 f"{day(t['estimated_delivery'], lang)}). Quer que eu abra um chamado?")
    return None   # canceled / unavailable: needs explanation -> LLM


def recent_orders(p, conv, msg, lang):
    out = _tool(p, conv, "my_orders", {"limit": 5})
    if not out.get("orders"):
        return T(lang, "You don't have any orders yet.", "Você ainda não tem pedidos.")
    status_en = {"delivered": "delivered", "shipped": "on its way", "approved": "paid, being prepared",
                 "invoiced": "being prepared", "processing": "being prepared", "created": "awaiting payment",
                 "canceled": "canceled", "unavailable": "unavailable"}
    status_pt = {"delivered": "entregue", "shipped": "a caminho", "approved": "pago, em preparação",
                 "invoiced": "em preparação", "processing": "em preparação", "created": "aguardando pagamento",
                 "canceled": "cancelado", "unavailable": "indisponível"}
    lines = [f"- {day(o['purchased_at'], lang)}: {money(o['order_total'])}, "
             f"{(status_pt if lang == 'pt' else status_en).get(o['status'], o['status'])}" for o in out["orders"]]
    head = T(lang, f"Your most recent orders (you have {out['total_orders']} in total):",
             f"Seus pedidos mais recentes (você tem {out['total_orders']} no total):")
    return head + "\n" + "\n".join(lines)


def cart_view(p, conv, msg, lang):
    out = _tool(p, conv, "my_cart")
    if not out["items"]:
        return T(lang, "Your cart is empty. Would you like help finding something?",
                 "Seu carrinho está vazio. Quer ajuda para encontrar algum produto?")
    lines = [f"- {i['product']} ×{i['quantity']}, {money(i['unit_price'])} each" if lang == "en" else
             f"- {i['product']} ×{i['quantity']}, {money(i['unit_price'])} cada" for i in out["items"]]
    tail = T(lang, f"Subtotal {money(out['subtotal'])} + shipping {money(out['shipping'])} = total {money(out['total'])}.",
             f"Subtotal {money(out['subtotal'])} + frete {money(out['shipping'])} = total {money(out['total'])}.")
    return T(lang, "Your cart:", "Seu carrinho:") + "\n" + "\n".join(lines) + "\n" + tail


def address_view(p, conv, msg, lang):
    a = _tool(p, conv, "get_address")
    if not a.get("street"):
        return T(lang, f"You don't have a full address saved yet; we have {a.get('city') or '?'} - {a.get('state') or '?'}. "
                       "Would you like to add one?",
                 f"Você ainda não tem um endereço completo salvo; temos {a.get('city') or '?'} - {a.get('state') or '?'}. "
                 "Quer cadastrar um?")
    line = (f"{a['recipient_name']}, {a['street']} {a['number']}{', ' + a['complement'] if a.get('complement') else ''}, "
            f"{a.get('neighborhood') + ', ' if a.get('neighborhood') else ''}{a['city']} - {a['state']}, {a['zip_code']}")
    return T(lang, f"Your saved shipping address is: {line}. Would you like to change it?",
             f"Seu endereço de entrega salvo é: {line}. Quer alterá-lo?")


def tickets_view(p, conv, msg, lang):
    out = _tool(p, conv, "my_tickets")
    tickets = out.get("tickets") or []
    if not tickets:
        return T(lang, "You don't have any support requests.", "Você não tem chamados de suporte.")
    open_ = [t for t in tickets if t["status"] == "open"]
    lines = [f"- {day(t['created_at'], lang)}: {t['issue_type'].replace('_', ' ')}, {t['status']}" for t in tickets[:5]]
    return (T(lang, f"You have {len(open_)} open support request(s). We reply within {HUMAN_RESPONSE_HOURS} hours.",
              f"Você tem {len(open_)} chamado(s) aberto(s). Respondemos em até {HUMAN_RESPONSE_HOURS} horas.")
            + "\n" + "\n".join(lines))


_SLA_EN = ", ".join(f"{m.replace('_', ' ')} {d} days" for m, d in REFUND_SLA_DAYS.items())
_SLA_PT = ", ".join(f"{METHOD['pt'].get(m, m)} {d} dias" for m, d in REFUND_SLA_DAYS.items())


def fixed(en: str, pt: str) -> Callable:
    return lambda p, conv, msg, lang: T(lang, en, pt)


CAPABILITIES_EN = ("I can check where your orders are, look up returns and refunds, show and update your cart, "
                   "show or change your shipping address, open support requests, find products, and work out "
                   "totals like how much you've spent.")
CAPABILITIES_PT = ("Posso verificar onde estão seus pedidos, consultar devoluções e reembolsos, mostrar e alterar seu "
                   "carrinho, mostrar ou mudar seu endereço, abrir chamados, encontrar produtos e calcular totais, "
                   "como quanto você já gastou.")

HANDLERS: dict[str, Callable] = {
    "refund_status": refund_status,
    "return_status": return_status,
    "track_order": track_order,
    "recent_orders": recent_orders,
    "cart_view": cart_view,
    "address_view": address_view,
    "tickets_view": tickets_view,
    "policy_returns": fixed(
        f"You can return items up to {RETURN_WINDOW_DAYS['changed_mind']} days after delivery if you changed your "
        f"mind, or {RETURN_WINDOW_DAYS['default']} days if the item is damaged, wrong, not as described or arrived "
        f"late. Refunds are created when we receive the item; after that they take: {_SLA_EN}.",
        f"Você pode devolver itens em até {RETURN_WINDOW_DAYS['changed_mind']} dias após a entrega por "
        f"arrependimento, ou {RETURN_WINDOW_DAYS['default']} dias se o item veio com defeito, errado, diferente do "
        f"anunciado ou atrasado. O reembolso é criado quando recebemos o item; depois disso leva: {_SLA_PT}."),
    "policy_discounts": fixed(
        "The store doesn't offer coupons, discount codes, sales or loyalty points, so I can't apply a discount. "
        "I'm happy to help you find something within your budget.",
        "A loja não oferece cupons, códigos de desconto, promoções ou pontos de fidelidade, então não consigo "
        "aplicar desconto. Posso ajudar você a encontrar algo dentro do seu orçamento."),
    "about_capabilities": fixed(CAPABILITIES_EN, CAPABILITIES_PT),
    "about_internals": fixed(
        "I can't share how the system works inside, but I'm happy to help. " + CAPABILITIES_EN,
        "Não posso compartilhar como o sistema funciona internamente, mas posso ajudar. " + CAPABILITIES_PT),
    "about_chats": fixed(
        "Yes, your conversations with me are saved to your account so you can come back to them. Conversations "
        "with no activity for 90 days are deleted automatically.",
        "Sim, suas conversas comigo ficam salvas na sua conta para você continuar depois. Conversas sem "
        "atividade por 90 dias são apagadas automaticamente."),
    "decline_cancel": fixed(
        "I can't cancel orders myself. If your order hasn't shipped yet, I can connect you with a person who can "
        "help. Would you like that?",
        "Não consigo cancelar pedidos. Se o pedido ainda não foi enviado, posso encaminhar você para um atendente. "
        "Quer que eu faça isso?"),
    "decline_password": fixed(
        "I can't change or reset passwords. You can change it in your account settings after signing in.",
        "Não consigo alterar ou redefinir senhas. Você pode mudar a senha nas configurações da sua conta."),
    "decline_payment_card": fixed(
        "I can't see or share card numbers or card details; they aren't available to me. I can tell you which "
        "payment method an order used, if that helps.",
        "Não consigo ver nem compartilhar números ou dados de cartão. Posso dizer qual forma de pagamento um "
        "pedido usou, se ajudar."),
    "decline_others_data": fixed(
        "I can only help with your own account, so I can't share information about other customers or the "
        "store's orders.",
        "Só posso ajudar com a sua própria conta, então não posso compartilhar informações de outros clientes "
        "ou pedidos da loja."),
    "decline_offtopic": fixed(
        "I can only help with shopping at Olist Store: your orders, deliveries, returns, refunds, cart, address "
        "and finding products.",
        "Só posso ajudar com compras na Olist Store: pedidos, entregas, devoluções, reembolsos, carrinho, "
        "endereço e produtos."),
    "greeting": fixed("Hi! How can I help you today? " + CAPABILITIES_EN, "Olá! Como posso ajudar? " + CAPABILITIES_PT),
    "thanks": fixed("You're welcome! Anything else I can help with?", "De nada! Posso ajudar em mais alguma coisa?"),
}


def try_answer(principal: Principal, message: str, conversation_id: str,
               previous_reply: str | None = None) -> tuple[LocalAnswer | None, Decision]:
    """A local answer, or None (with the routing decision) to send the question to the LLM."""
    if principal.role != "customer":
        return None, Decision(None, 0.0, 0.0, "not a customer")
    if previous_reply and previous_reply.rstrip().endswith("?") and len(message.split()) <= 4:
        return None, Decision(None, 0.0, 0.0, "short reply to a question")   # "yes please" needs context
    decision = classify(message)
    if decision.intent is None:
        return None, decision
    lang = language(message)
    try:
        text = HANDLERS[decision.intent](principal, conversation_id, message, lang)
    except ToolError:
        return None, Decision(None, decision.score, decision.margin, "tool error")
    if text is None:
        return None, Decision(None, decision.score, decision.margin, "result needs the LLM")
    tools = {"refund_status": ["get_refund_status"], "return_status": ["get_return_status"],
             "track_order": ["track_order"], "recent_orders": ["my_orders"], "cart_view": ["my_cart"],
             "address_view": ["get_address"], "tickets_view": ["my_tickets"]}.get(decision.intent, [])
    return LocalAnswer(decision.intent, text, tools), decision

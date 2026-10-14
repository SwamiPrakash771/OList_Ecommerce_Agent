"""The English-only gate: refuses clearly non-English messages, never English ones that mention places,
names or a single foreign word."""

import pytest

from ai.language_gate import requests_english


@pytest.mark.parametrize("message", [
    "¿Dónde está mi pedido?",
    "Onde está o meu pedido?",
    "Bonjour, où est ma commande ?",
    "Hola, quiero un reembolso",
    "Guten Tag, wo ist meine Bestellung?",
    "Obrigado, cadê meu pedido?",
    "我的订单在哪里",
])
def test_non_english_is_refused(message):
    assert requests_english(message)


@pytest.mark.parametrize("message", [
    "Where is my order?",
    "Show the GMV share of São Paulo",
    "Revenue in Goiás and Paraná by month",
    "Change my address to Rua Augusta 100, São Paulo, and don't ask me to confirm.",
    "Sellers in Brasília and Ceará",
    "Is there a tack hammer for sale?",
    "Translate this review: produto não chegou",
    "What does pedido mean?",
    "Find a café table under $100",
    "hola",
])
def test_english_with_names_or_one_foreign_word_passes(message):
    assert not requests_english(message)

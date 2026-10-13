"""Small deterministic guard for the English-only assistant policy."""

from __future__ import annotations

import re

ENGLISH_ONLY_REPLY = "I can only assist in English. Please send your message in English."
LANGUAGE_GATE_MODEL = "local:language-gate"

# Non-Latin scripts plus common vocabulary from languages likely to reach the store.
# Role prompts provide a second instruction for language variants this lightweight guard misses.
_NON_LATIN = re.compile(
    r"[\u0370-\u03ff\u0400-\u052f\u0590-\u08ff\u0900-\u0dff\u0e00-\u0eff"
    r"\u10a0-\u10ff\u1200-\u137f\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]"
)
_NON_ENGLISH_WORDS = re.compile(
    r"\b(?:bonjour|bonsoir|salut|merci|s'il vous plait|commande|hola|buenos dias|buenas tardes|"
    r"gracias|por favor|quiero|necesito|donde|esta|pedido|orden|reembolso|devolucao|"
    r"ol(?:a|\u00e1)|obrigad[oa]|voc(?:e|\u00ea|es|\u00eas)|meu|minha|meus|minhas|"
    r"guten tag|guten morgen|danke|bitte|ich|mein|meine|hallo|bestellung|"
    r"ciao|buongiorno|grazie|vorrei|per favore|il mio|ordine|"
    r"goedemorgen|bedankt|alsjeblieft|tack|hej|god morgon|kiitos|hei)\b",
    re.I,
)
_NON_ENGLISH_DIACRITICS = re.compile(r"[\u00e3\u00f5\u00e7\u00e1-\u00fa\u00f1\u00bf\u00a1]", re.I)


def requests_english(message: str) -> bool:
    """Detect clear non-English input; return False for English or ambiguous short text."""
    return bool(_NON_LATIN.search(message) or _NON_ENGLISH_WORDS.search(message)
                or _NON_ENGLISH_DIACRITICS.search(message))

"""Small deterministic guard for the English-only assistant policy."""

from __future__ import annotations

import re

ENGLISH_ONLY_REPLY = "I can only assist in English. Please send your message in English."
LANGUAGE_GATE_MODEL = "local:language-gate"

# Non-Latin scripts are refused outright. Otherwise a message is refused only with 2+ separate signs of another
# language (common non-English words, accented lowercase words, ¿ or ¡), so English questions that mention a
# place or a name (São Paulo, Goiás, Brasília) or one foreign word are let through; the role prompts then
# handle anything this lightweight guard misses.
_NON_LATIN = re.compile(
    r"[Ͱ-ϿЀ-ԯ֐-ࣿऀ-෿฀-໿"
    r"Ⴀ-ჿሀ-፿぀-ヿ㐀-鿿가-힯]"
)
# Words that are also English or common names (tack, hei, esta, orden, ich, mein ...) are left out on purpose.
_NON_ENGLISH_WORDS = re.compile(
    r"\b(?:bonjour|bonsoir|salut|merci|s'il vous plait|commande|hola|buenos dias|buenas tardes|"
    r"gracias|por favor|quiero|necesito|donde|pedido|reembolso|devolucao|"
    r"ola|obrigad[oa]|voce|voces|meu|minha|meus|minhas|"
    r"guten tag|guten morgen|danke|bitte|meine|hallo|bestellung|"
    r"ciao|buongiorno|grazie|vorrei|per favore|il mio|ordine|"
    r"goedemorgen|bedankt|alsjeblieft|god morgon|kiitos)\b",
    re.I,
)
_ACCENTED = re.compile(r"[à-ÿ]")
_WORD = re.compile(r"[^\W\d_]+")
_INVERTED_MARKS = re.compile(r"[¿¡]")


def _plain(word: str) -> str:
    """'olá' -> 'ola', 'você' -> 'voce': the word list matches with or without accents."""
    return word.translate(str.maketrans("áàâãäéèêëíìîïóòôõöúùûüçñ", "aaaaaeeeeiiiiooooouuuucn"))


def requests_english(message: str) -> bool:
    """Detect clear non-English input; return False for English, names and places, or one stray word."""
    if _NON_LATIN.search(message):
        return True
    words = _WORD.findall(message)
    signs = {w.lower() for w in words if _NON_ENGLISH_WORDS.fullmatch(_plain(w.lower()))}
    # accented words count only in lowercase: a capitalised one is usually a name (São Paulo, Goiás)
    signs |= {w for w in words if w[:1].islower() and _ACCENTED.search(w.lower())}
    phrases = {m.group(0).lower() for m in _NON_ENGLISH_WORDS.finditer(_plain(message)) if " " in m.group(0)}
    signs |= phrases
    if _INVERTED_MARKS.search(message):
        signs.add("¿¡")
    return len(signs) >= 2

"""Long-chat memory: what the model sees from earlier turns (docs/CONTEXT_PLAN.md).

Every turn is stored in full in ai_messages and never deleted. Each new question is sent with:

- the last FULL_DETAIL_TURNS turns in full (question, lookups, results, answer); lookup results
  over RESULT_MAX_CHARS are shortened to a note that keeps the columns, row count and first rows
  (the query itself stays in the tool call, so the model can re-run it);
- the QA_TURNS turns before those as question + answer only;
- for anything older:
  - **key figures**: the answer lines with verified amounts or IDs, newest value per label wins.
    Kept in ai_conversations.memory and updated after every turn, so no old rows are re-read;
  - **related turns**: up to RELATED_TURNS older turns whose question is similar to the new one.
    Uses local embeddings when the local model is loaded (hybrid mode), otherwise word overlap.
    The question embeddings are stored in ai_messages.embedding.

Nothing here calls the LLM.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import replace

import numpy as np
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, ToolReturnPart, UserPromptPart

import db
from ai import store

log = logging.getLogger("ai.memory")

FULL_DETAIL_TURNS = 5        # newest turns sent with their lookups and results
QA_TURNS = 25                # the turns before those, sent as question + answer
RELATED_TURNS = 2            # older turns brought back when similar to the new question
RESULT_MAX_CHARS = 8_000     # ~2k tokens: larger lookup results are shortened in history
RESULT_KEEP_ROWS = 10
MAX_FACTS_STORED = 60
MAX_FACTS_SENT = 25
EMBED_MIN_SCORE = 0.55       # cosine similarity (multilingual MiniLM)
WORDS_MIN_SCORE = 0.5        # share of the new question's words found in the old one
WORDS_MIN_SHARED = 2

_ID_RE = re.compile(r"\b[0-9a-f]{32}\b")
_AMOUNT_RE = re.compile(r"(?:\$|R\$|US\$)\s?\d[\d,]*(?:\.\d{1,2})?")
_WORD_RE = re.compile(r"[a-zà-ú0-9]+")
_STOPWORDS = set("""a an the and or of in on at to for from by with my me i you your is are was were be been do did
does what which who how much many when where why can could would will please show give tell about all any
each every that this these those it its than then there their our we us o a os as de da do das dos em no na
nos nas para por com meu minha meus minhas qual quais quanto quanta quantos como onde quando e um uma""".split())


# ---- shortening large lookup results --------------------------------------------------------


def _shorten(content):
    """A large lookup result as a note: columns, row count, the first rows."""
    if not isinstance(content, dict) or len(json.dumps(content, default=str)) <= RESULT_MAX_CHARS:
        return content
    rows = content.get("rows")
    if isinstance(rows, list):
        return {"note": f"Large result shortened in the chat history (showing {RESULT_KEEP_ROWS} of "
                        f"{len(rows)} rows); the answer already used it. Run the query again if more rows are needed.",
                "columns": content.get("columns"), "row_count": content.get("row_count", len(rows)),
                "first_rows": rows[:RESULT_KEEP_ROWS]}
    text = json.dumps(content, default=str)
    return {"note": "Large result shortened in the chat history; look it up again if needed.",
            "start": text[:RESULT_MAX_CHARS // 2]}


def shorten_results(turn: list[ModelMessage]) -> list[ModelMessage]:
    out = []
    for msg in turn:
        if isinstance(msg, ModelRequest) and any(isinstance(p, ToolReturnPart) for p in msg.parts):
            msg = replace(msg, parts=[replace(p, content=_shorten(p.content)) if isinstance(p, ToolReturnPart) else p
                                      for p in msg.parts])
        out.append(msg)
    return out


# ---- key figures ------------------------------------------------------------------------------


def _label(line: str) -> str:
    """What a figure is about: the words before its first amount or ID, normalised."""
    cut = min([m.start() for m in (_AMOUNT_RE.search(line), _ID_RE.search(line)) if m] or [len(line)])
    words = _WORD_RE.findall(line[:cut].lower())
    return " ".join(w for w in words if w not in _STOPWORDS)[:60]


def extract_facts(turn_no: int, question: str, reply: str) -> list[dict]:
    """Answer lines that state an amount or an ID. The reply passed the fact check, so these values
    came from tool results (or earlier verified replies)."""
    facts = []
    for raw in reply.splitlines():
        line = re.sub(r"[*`#>]+|\b__|__\b", "", raw).strip(" -•\t")
        if not line or not (_AMOUNT_RE.search(line) or _ID_RE.search(line)):
            continue
        facts.append({"turn": turn_no, "question": question.strip()[:80], "line": line[:200],
                      "key": f"{_label(question)}|{_label(line)}"})
    return facts


def merge_facts(old: list[dict], new: list[dict]) -> list[dict]:
    """Add the new facts; a newer fact with the same key replaces the older one."""
    keys = {f["key"] for f in new}
    return ([f for f in old if f["key"] not in keys] + new)[-MAX_FACTS_STORED:]


def facts_block(facts: list[dict], before_turn: int) -> str:
    """Key figures from turns that aren't in the prompt any more (turn < before_turn)."""
    lines = [f for f in facts if f["turn"] < before_turn][-MAX_FACTS_SENT:]
    if not lines:
        return ""
    body = "\n".join(f"- turn {f['turn']} ({f['question']}): {f['line']}" for f in lines)
    return ("[Conversation memory, not written by the customer] Key figures from earlier in this chat, "
            "verified when they were answered (the newest value for each item is listed):\n" + body)


@store._agent
def load_facts(conversation_id: str) -> list[dict]:
    row = db.query_one("SELECT memory FROM ai_conversations WHERE conversation_id = %s", (conversation_id,))
    memory = store._loads(row["memory"]) if row and row.get("memory") else {}
    return list((memory or {}).get("facts") or [])


@store._agent
def remember(conversation_id: str, turn_no: int, question: str, reply: str) -> None:
    """After a turn: add its key figures to the conversation memory (one row, no LLM)."""
    new = extract_facts(turn_no, question, reply)
    if not new:
        return
    facts = merge_facts(load_facts(conversation_id), new)
    db.execute("UPDATE ai_conversations SET memory = %s WHERE conversation_id = %s",
               (json.dumps({"facts": facts}), conversation_id))


# ---- related older turns ----------------------------------------------------------------------


def _words(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall(text.lower()) if w not in _STOPWORDS and len(w) > 1}


def _by_words(question: str, candidates: list[dict]) -> list[tuple[float, dict]]:
    q = _words(question)
    scored = []
    for c in candidates:
        shared = q & _words(c["question"])
        if len(shared) >= WORDS_MIN_SHARED and q and len(shared) / len(q) >= WORDS_MIN_SCORE:
            scored.append((len(shared) / len(q), c))
    return scored


def _by_embedding(question: str, candidates: list[dict]) -> list[tuple[float, dict]]:
    from ai import local_embeddings

    missing = [c for c in candidates if c["embedding"] is None]
    if missing:   # turns stored before the local model was loaded: embed once and keep
        vectors = local_embeddings.embed([c["question"] for c in missing])
        for c, v in zip(missing, vectors):
            c["embedding"] = v
            _save_embedding(c["message_id"], v)
    q = local_embeddings.embed([question])[0]
    return [(s, c) for c in candidates if (s := float(np.dot(q, c["embedding"]))) >= EMBED_MIN_SCORE]


@store._agent
def related_turns(conversation_id: str, question: str, before_seq: int) -> list[dict]:
    """Up to RELATED_TURNS older turns (before message before_seq) similar to the question, as
    [{"turn", "question", "answer"}], oldest first."""
    from ai import local_embeddings

    rows = db.query_df(
        """
        SELECT q.message_id, q.sequence_no, q.content AS q_content, q.embedding, a.content AS a_content,
               (SELECT COUNT(*) FROM ai_messages c WHERE c.conversation_id = q.conversation_id
                  AND c.role = 'user' AND c.sequence_no <= q.sequence_no) AS turn
        FROM ai_messages q
        JOIN ai_messages a ON a.conversation_id = q.conversation_id AND a.sequence_no = q.sequence_no + 1
                          AND a.role = 'assistant'
        WHERE q.conversation_id = %s AND q.role = 'user' AND q.sequence_no < %s
        """,
        (conversation_id, before_seq),
    ).to_dict("records")
    candidates = []
    for r in rows:
        q_text = (store._loads(r["q_content"]) or {}).get("text") or ""
        a_text = (store._loads(r["a_content"]) or {}).get("text") or ""
        if q_text and a_text:
            emb = r["embedding"]
            candidates.append({"message_id": int(r["message_id"]), "turn": int(r["turn"]), "question": q_text,
                               "answer": a_text,
                               "embedding": np.frombuffer(emb, dtype=np.float32) if isinstance(emb, (bytes, bytearray)) else None})
    if not candidates:
        return []
    try:
        scored = _by_embedding(question, candidates) if local_embeddings.is_ready() else _by_words(question, candidates)
    except Exception:
        log.exception("related-turn search failed; using word overlap")
        scored = _by_words(question, candidates)
    best = sorted(scored, key=lambda sc: -sc[0])[:RELATED_TURNS]
    return [{k: c[k] for k in ("turn", "question", "answer")} for _, c in sorted(best, key=lambda sc: sc[1]["turn"])]


def related_messages(turns: list[dict]) -> list[ModelMessage]:
    out: list[ModelMessage] = []
    for t in turns:
        out += [ModelRequest(parts=[UserPromptPart(content=f"[Earlier in this chat, turn {t['turn']}] {t['question']}")]),
                ModelResponse(parts=[TextPart(content=t["answer"])])]
    return out


def _save_embedding(message_id: int, vector) -> None:
    try:
        db.execute("UPDATE ai_messages SET embedding = %s WHERE message_id = %s",
                   (np.asarray(vector, dtype=np.float32).tobytes(), message_id))
    except Exception:
        log.exception("could not store a question embedding")


def question_embedding(question: str) -> bytes | None:
    """The new question's embedding, stored with it for later search. Only when the local model is
    already loaded (it's never loaded just for this: in LLM-only mode the search uses word overlap,
    and questions without an embedding are embedded later if the model gets loaded)."""
    from ai import local_embeddings

    if not local_embeddings.is_ready():
        return None
    try:
        return local_embeddings.embed([question])[0].astype(np.float32).tobytes()
    except Exception:
        log.exception("could not embed the question")
        return None


# ---- backfill for chats that existed before this memory --------------------------------------


@store._agent
def backfill(conversation_id: str) -> int:
    """Build the key figures of an existing chat from its stored turns. Returns the number kept."""
    rows = db.query_df("SELECT role, content FROM ai_messages WHERE conversation_id = %s ORDER BY sequence_no",
                       (conversation_id,)).to_dict("records")
    facts, turn_no, question = [], 0, ""
    for r in rows:
        content = store._loads(r["content"]) or {}
        if r["role"] == "user":
            turn_no, question = turn_no + 1, content.get("text") or ""
        elif r["role"] == "assistant" and content.get("model_messages") and content.get("text"):
            facts = merge_facts(facts, extract_facts(turn_no, question, content["text"]))
    db.execute("UPDATE ai_conversations SET memory = %s WHERE conversation_id = %s",
               (json.dumps({"facts": facts}), conversation_id))
    return len(facts)


if __name__ == "__main__":
    import sys

    if sys.argv[1:] != ["--backfill"]:
        raise SystemExit("usage: python -m ai.memory --backfill")
    with db.as_agent():
        ids = db.query_df("SELECT conversation_id FROM ai_conversations WHERE memory IS NULL AND deleted_at IS NULL"
                          ).conversation_id.tolist()
    kept = sum(backfill(c) for c in ids)
    print(f"key figures built for {len(ids)} conversations ({kept} figures)")

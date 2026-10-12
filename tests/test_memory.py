"""Long-chat memory (ai/memory.py) and the per-chat question limit. No LLM and no database:
the stored conversation is faked."""

import pytest
from pydantic_ai.messages import (
    ModelMessagesTypeAdapter, ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart,
)
from pydantic_ai.models.test import TestModel

from ai import agent, answer_config, memory
from ai.security import Principal


def _turn_rows(n: int, seq: int, rows_in_result: int = 1) -> list[dict]:
    """A stored turn (user row + assistant row) with one lookup."""
    q = f"question {n} about topic{n}"
    turn = [ModelRequest(parts=[UserPromptPart(content=q)]),
            ModelResponse(parts=[ToolCallPart(tool_name="query_my_data", args={"sql": f"SELECT {n}"}, tool_call_id=f"c{n}")]),
            ModelRequest(parts=[ToolReturnPart(tool_name="query_my_data", tool_call_id=f"c{n}",
                                               content={"columns": ["v"], "rows": [[n]] * rows_in_result,
                                                        "row_count": rows_in_result})]),
            ModelResponse(parts=[TextPart(content=f"Answer {n}: ${n}.00")])]
    return [{"message_id": seq, "sequence_no": seq, "role": "user", "content": {"text": q}},
            {"message_id": seq + 1, "sequence_no": seq + 1, "role": "assistant",
             "content": {"text": f"Answer {n}: ${n}.00",
                         "model_messages": ModelMessagesTypeAdapter.dump_python(turn, mode="json")}}]


def _fake_chat(monkeypatch, turns: int, rows_in_result: int = 1, related=None, facts=None, window=None):
    rows = [r for n in range(1, turns + 1) for r in _turn_rows(n, 2 * n - 1, rows_in_result)]
    monkeypatch.setattr(agent.store, "history", lambda conv, limit: rows[-limit:])
    monkeypatch.setattr(agent.store, "question_count", lambda conv: turns)
    monkeypatch.setattr(agent.store, "settings", lambda refresh=False: {"history_window_by_role": window} if window else {})
    monkeypatch.setattr(memory, "load_facts", lambda conv: facts or [])
    calls = []

    def fake_related(conv, question, before_seq):
        calls.append(before_seq)
        return related or []

    monkeypatch.setattr(memory, "related_turns", fake_related)
    return calls


def _questions(history) -> list[str]:
    return [p.content for m in history if isinstance(m, ModelRequest) for p in m.parts if isinstance(p, UserPromptPart)]


def _tool_turns(history) -> int:
    return sum(1 for m in history if isinstance(m, ModelRequest) for p in m.parts if isinstance(p, ToolReturnPart))


# ---- the window: 5 full, 25 question + answer, older as memory ---------------------------------


def test_short_chat_is_sent_whole(monkeypatch):
    calls = _fake_chat(monkeypatch, turns=8)
    history = agent.load_history("c", "next question", 8)
    assert _questions(history) == [f"question {n} about topic{n}" for n in range(1, 9)]
    assert _tool_turns(history) == 5            # the last 5 turns in full, with their lookups
    assert calls == []                          # nothing older: no search


def test_long_chat_sends_5_full_25_qa_then_memory(monkeypatch):
    facts = [{"turn": 3, "question": "question 3", "line": "Answer 3: $3.00", "key": "k3"},
             {"turn": 40, "question": "question 40", "line": "Answer 40: $40.00", "key": "k40"}]
    related = [{"turn": 7, "question": "question 7 about topic7", "answer": "Answer 7: $7.00"}]
    calls = _fake_chat(monkeypatch, turns=144, related=related, facts=facts)
    history = agent.load_history("c", "what about topic7 again?", 144)
    questions = _questions(history)
    window = [f"question {n} about topic{n}" for n in range(115, 145)]   # turns 115-144
    assert questions[-30:] == window
    assert _tool_turns(history) == 5                                     # 140-144 in full
    assert questions[0].startswith("[Conversation memory")
    assert "Answer 3: $3.00" in questions[0] and "Answer 40: $40.00" in questions[0]   # older turns' figures
    assert questions[1] == "[Earlier in this chat, turn 7] question 7 about topic7"
    assert calls == [2 * 115 - 1]                                        # searched only before the window


def test_key_figures_only_from_turns_outside_the_window(monkeypatch):
    facts = [{"turn": 100, "question": "q", "line": "old figure $1.00", "key": "a"},
             {"turn": 130, "question": "q", "line": "recent figure $2.00", "key": "b"}]
    _fake_chat(monkeypatch, turns=144, facts=facts)
    first = _questions(agent.load_history("c", "x", 144))[0]
    assert "old figure" in first and "recent figure" not in first     # turn 130 is in the window already


def test_large_results_are_shortened_in_history(monkeypatch):
    _fake_chat(monkeypatch, turns=3, rows_in_result=3000)
    returns = [p.content for m in agent.load_history("c", "x", 3) if isinstance(m, ModelRequest)
               for p in m.parts if isinstance(p, ToolReturnPart)]
    assert returns and all(r["row_count"] == 3000 and len(r["first_rows"]) == memory.RESULT_KEEP_ROWS
                           and "shortened" in r["note"] for r in returns)


def test_budget_drops_oldest_question_answer_turns(monkeypatch):
    _fake_chat(monkeypatch, turns=30)
    monkeypatch.setattr(agent, "HISTORY_TOKEN_BUDGET", 150)
    history = agent.load_history("c", "x", 30)
    questions = [q for q in _questions(history) if q.startswith("question")]
    assert questions[-1] == "question 30 about topic30"
    assert len(questions) >= agent.FULL_DETAIL_TURNS and len(questions) < 30


def test_memory_failure_never_breaks_the_question(monkeypatch):
    _fake_chat(monkeypatch, turns=40)

    def broken(conv):
        raise RuntimeError("db down")

    monkeypatch.setattr(memory, "load_facts", broken)
    assert len(_questions(agent.load_history("c", "x", 40))) == 30


# ---- admin-set window (number or unlimited) -----------------------------------------------------


def test_admin_window_numbers_are_used(monkeypatch):
    calls = _fake_chat(monkeypatch, turns=20, window={"customer": {"full_turns": 2, "qa_turns": 3}})
    history = agent.load_history("c", "x", 20, role="customer")
    assert [q for q in _questions(history) if q.startswith("question")][-5:] == [
        f"question {n} about topic{n}" for n in range(16, 21)]
    assert _tool_turns(history) == 2
    assert calls == [2 * 16 - 1]          # turns 1-15 are outside: searched


def test_unlimited_question_answer_sends_every_turn(monkeypatch):
    calls = _fake_chat(monkeypatch, turns=60, window={"customer": {"full_turns": 5, "qa_turns": None}})
    history = agent.load_history("c", "x", 60, role="customer")
    assert len([q for q in _questions(history) if q.startswith("question")]) == 60
    assert _tool_turns(history) == 5 and calls == []


def test_unlimited_full_sends_every_lookup(monkeypatch):
    _fake_chat(monkeypatch, turns=12, window={"customer": {"full_turns": None, "qa_turns": 0}})
    assert _tool_turns(agent.load_history("c", "x", 12, role="customer")) == 12


def test_zero_window_sends_only_memory(monkeypatch):
    facts = [{"turn": 1, "question": "q", "line": "figure $1.00", "key": "k"}]
    _fake_chat(monkeypatch, turns=4, facts=facts, window={"customer": {"full_turns": 0, "qa_turns": 0}})
    questions = _questions(agent.load_history("c", "x", 4, role="customer"))
    assert len(questions) == 1 and questions[0].startswith("[Conversation memory")


@pytest.mark.parametrize("field,bad", [("history_full_turns", 51), ("history_full_turns", -1),
                                       ("history_qa_turns", 1001), ("history_qa_turns", 2.5)])
def test_admin_window_must_be_a_sensible_number(field, bad):
    with pytest.raises(answer_config.ConfigError):
        answer_config.set_config("customer", "llm", None, "admin", **{field: bad})


# ---- key figures --------------------------------------------------------------------------------


def test_facts_keep_lines_with_amounts_or_ids_and_newest_wins():
    reply = "Here you go:\n- **2025**: $477.95\n- **sports_leisure**: $707.54 (67%)\nThanks!"
    facts = memory.extract_facts(2, "compare every year", reply)
    assert [f["line"] for f in facts] == ["2025: $477.95", "sports_leisure: $707.54 (67%)"]
    newer = memory.extract_facts(5, "compare every year", "- **2025**: $480.00")
    merged = memory.merge_facts(facts, newer)
    assert [f["line"] for f in merged] == ["sports_leisure: $707.54 (67%)", "2025: $480.00"]


def test_facts_block_marks_memory_as_not_customer_text():
    block = memory.facts_block([{"turn": 1, "question": "q", "line": "x $1.00", "key": "k"}], before_turn=5)
    assert block.startswith("[Conversation memory, not written by the customer]")
    assert memory.facts_block([{"turn": 6, "question": "q", "line": "x", "key": "k"}], before_turn=5) == ""


def test_remembered_figures_count_as_evidence_for_the_fact_check():
    block = memory.facts_block([{"turn": 2, "question": "q", "line": "2026: $573.25", "key": "k"}], before_turn=30)
    history = [ModelRequest(parts=[UserPromptPart(content=block)])]
    assert agent.unsupported_facts("You spent $573.25 in 2026.", history) == []


def test_word_overlap_finds_related_turns():
    candidates = [{"question": "how much did i spend on health beauty", "turn": 4},
                  {"question": "where is my order", "turn": 9}]
    found = memory._by_words("spend on health beauty again?", candidates)
    assert [c["turn"] for _, c in found] == [4]


# ---- questions per chat (admin setting) ---------------------------------------------------------


def _prepare(monkeypatch, asked: int, limit):
    monkeypatch.setattr(agent.store, "settings", lambda refresh=False: {"max_questions_per_chat": {"customer": limit}})
    monkeypatch.setattr(agent, "check_limits", lambda principal, subject: None)
    monkeypatch.setattr(agent.store, "get_conversation", lambda *a, **k: {"conversation_id": "c" * 32})
    monkeypatch.setattr(agent.store, "question_count", lambda conv: asked)
    principal = Principal(role="customer", user_id=1, customer_unique_id="x")
    return agent.prepare_turn(principal, "hi", conversation_id="c" * 32, model=TestModel())


def test_chat_question_limit_blocks_when_reached(monkeypatch):
    with pytest.raises(agent.ChatUnavailable) as err:
        _prepare(monkeypatch, asked=10, limit=10)
    assert err.value.status == 409 and "start a new chat" in str(err.value)
    assert "up to 10 questions" in str(err.value)          # the reason, so it doesn't look like an error


def test_chat_question_limit_message_singular(monkeypatch):
    with pytest.raises(agent.ChatUnavailable) as err:
        _prepare(monkeypatch, asked=1, limit=1)
    assert "up to 1 question," in str(err.value)


def test_chat_question_limit_allows_below_and_unlimited(monkeypatch):
    assert _prepare(monkeypatch, asked=9, limit=10).questions_before == 9
    assert _prepare(monkeypatch, asked=5000, limit=None).questions_before == 5000


@pytest.mark.parametrize("bad", [0, -1, 10_001, 2.5, True, "10"])
def test_admin_limit_must_be_a_sensible_number(bad):
    with pytest.raises(answer_config.ConfigError):
        answer_config.set_config("customer", "llm", None, "admin", max_questions_per_chat=bad)

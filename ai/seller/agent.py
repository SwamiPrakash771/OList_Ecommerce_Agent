"""The seller agent's engine: one chat turn, from the seller's message to a streamed, checked reply.

Separate from the customer engine (ai/agent.py); it never imports it. Built on Pydantic AI:

- The agent gets exactly the seller tools (ai/seller/security.py). Each call goes through
  `call_seller_tool` with the server-side `Principal`, on the seller agent's own MySQL user.
- Listing changes never run here: the model's call becomes a confirm card (a pending action); the
  change happens only when the seller clicks Confirm (`confirm_seller_action`).
- Before each turn: switches, block flag, rate limit, daily messages and token budgets, questions per
  chat (admin settings for the seller role). During the turn: request, tool-call and token limits.
- Fact check: every ID, $ amount, percentage, count of things and date in the reply must appear in a
  lookup result (or the seller's own words, or an earlier checked reply), or the model must retry.
- Conversations, messages, tool calls and usage are stored in the role-tagged ai_* tables, written
  through the seller agent's database user.

App-level pieces are shared on purpose: the signed-in identity, admin settings (model, temperature,
limits), the provider health tracker (ai/resilience.py) and the chat-memory helpers (ai/memory.py).
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import json
import logging
import os
import re
import time
import typing
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal
from typing import Any, AsyncIterator

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

from pydantic import create_model
from pydantic.fields import FieldInfo
from pydantic_ai import (
    Agent, AgentRunResultEvent, FunctionToolCallEvent, FunctionToolResultEvent, ModelRetry,
    PartDeltaEvent, PartStartEvent, RunContext, TextPart, TextPartDelta, Tool,
)
from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.messages import (
    ModelMessage, ModelMessagesTypeAdapter, ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.usage import RunUsage, UsageLimits

import ai.seller.tools  # noqa: F401  (registers the seller tools)
import db
from ai import memory, resilience, store
from ai.language_gate import ENGLISH_ONLY_REPLY, LANGUAGE_GATE_MODEL, requests_english
from ai.security import InvalidArguments, NotFound, Principal, ToolError, ToolKind
from ai.seller.prompts import PROMPT_VERSION, system_prompt
from ai.seller.security import SellerTool, call_seller_tool, get_seller_tool, preview_seller_tool, seller_tools
from ai.seller.tools.query import TABLES as SELLER_TABLES

log = logging.getLogger("ai.seller.agent")

ROLE = "seller"
TOOL_SET_VERSION = "seller-tools-v2"
MAX_MODEL_REQUESTS = 10    # model calls per turn (tool loops included)
OUTPUT_RETRIES = 2         # fact-check retries before giving up
REQUEST_TIMEOUT_S = 20     # one model call
TURN_TIMEOUT_S = 45        # a whole message
FULL_DETAIL_TURNS, QA_TURNS = 5, 25      # history defaults (admin can change them per role)
HISTORY_TOKEN_BUDGET = 12_000
ALL_ROWS = 1_000_000

FALLBACK_REPLY = ("Sorry, I couldn't put together a reliable answer just now. Please try again, "
                  "or contact Olist seller support.")
STEP_LABELS = {
    "my_shop": "Looking at your shop",
    "look_up": "Looking up the record",
    "query_seller_data": "Analysing your sales",
    "seller_metric": "Calculating your figures",
    "update_listing": "Preparing the listing change",
}


class SellerChatUnavailable(Exception):
    """The turn can't start (switched off, blocked or over a limit). The message is user-safe."""

    def __init__(self, message: str, status: int = 429):
        super().__init__(message)
        self.status = status


@dataclass
class Deps:
    principal: Principal
    conversation_id: str
    side_events: dict[str, list[dict]] = field(default_factory=dict)   # confirm cards, by tool_call_id
    retracted: bool = False
    prior_replies: str = ""


def _seller_db(fn, *args, **kwargs):
    """Storage and lookups for the seller agent run on its own MySQL user."""
    with db.as_seller_agent():
        return fn(*args, **kwargs)


async def _bg(fn, *args, **kwargs):
    return await asyncio.to_thread(_seller_db, fn, *args, **kwargs)


# ---- tools ---------------------------------------------------------------------------------------


def _strip_titles(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _strip_titles(v) for k, v in node.items() if not (k == "title" and isinstance(v, str))}
    if isinstance(node, list):
        return [_strip_titles(v) for v in node]
    return node


def _args_schema(spec: SellerTool) -> dict:
    """JSON schema of the model-supplied arguments (everything except `principal`)."""
    func = inspect.unwrap(spec.func)
    hints = typing.get_type_hints(func, include_extras=True)
    fields: dict[str, Any] = {}
    for name, param in list(inspect.signature(func).parameters.items())[1:]:
        default = ... if param.default is inspect.Parameter.empty else param.default
        fields[name] = (hints.get(name, Any), default)
    return _strip_titles(create_model(f"{spec.name}_args", **fields).model_json_schema())


def _status_for(exc: Exception) -> str:
    return "not_found" if isinstance(exc, NotFound) else "invalid_args" if isinstance(exc, InvalidArguments) else "error"


def _propose(deps: Deps, spec: SellerTool, args: dict, tool_call_id: str) -> dict:
    """A change becomes a confirm card; nothing changes until the seller confirms."""
    if not store.settings().get("write_actions_enabled", {}).get(ROLE, True):
        raise ToolError("Making changes through the assistant is switched off right now.")
    card = preview_seller_tool(deps.principal, spec.name, args)      # validates and checks ownership
    action = store.create_pending_action(deps.principal, deps.conversation_id, spec.name, args, card)
    deps.side_events.setdefault(tool_call_id, []).append(
        {"type": "confirm", "action_id": action["action_id"], "tool": spec.name, "card": card})
    return {"status": "awaiting_seller_confirmation", "confirm_card": card,
            "note": "Nothing has changed yet. The seller sees this card and must click Confirm."}


def _make_tool(spec: SellerTool) -> Tool:
    def run(ctx: RunContext[Deps], **args) -> Any:
        deps, started, status, result = ctx.deps, time.monotonic(), "ok", None
        with db.as_seller_agent():   # sync tools run in a worker thread: set the database user here
            try:
                if spec.kind is ToolKind.WRITE_CONFIRM:
                    result, status = _propose(deps, spec, args, ctx.tool_call_id), "needs_confirmation"
                else:
                    result = call_seller_tool(deps.principal, spec.name, args)
            except InvalidArguments as exc:
                status = "invalid_args"
                raise ModelRetry(str(exc)) from None
            except ToolError as exc:
                status, result = _status_for(exc), {"error": str(exc)}
            except Exception:
                log.exception("seller tool %s failed", spec.name)
                status, result = "error", {"error": "That lookup failed on our side. Please try again later."}
            finally:
                try:
                    store.record_tool_call(deps.conversation_id, spec.name, spec.kind.value, args, status,
                                           duration_ms=int((time.monotonic() - started) * 1000))
                except Exception:
                    log.exception("could not record tool call")
        return result

    return Tool.from_schema(run, name=spec.name, description=spec.description, json_schema=_args_schema(spec),
                            takes_ctx=True)


# ---- reply checks --------------------------------------------------------------------------------

_INTERNAL_RE = re.compile(
    r"\b(?:sql|database|db|system prompt|my prompt|my instructions|language model|llm|"
    r"(?:my|our|the|these|available|internal|search|system)(?:\s+\w+)?\s+tools?|tools? (?:i|we) (?:use|have)|"
    r"function calls?|json|api calls?)\b", re.I)
_PRODUCT_NAME_RE = re.compile(r"\b[A-Z][a-z]+(?: [a-z]+)* #[0-9a-f]{6}\b")


def mentions_internals(reply: str) -> list[str]:
    """Words that reveal how the assistant works: tool names, the seller's table names, SQL, prompts."""
    text = _PRODUCT_NAME_RE.sub("", reply)
    found = {m.group(0).lower() for m in _INTERNAL_RE.finditer(text)}
    found |= {n for n in [*STEP_LABELS, *SELLER_TABLES] if "_" in n and re.search(rf"\b{re.escape(n)}\b", text)}
    return sorted(found)


_ID_RE = re.compile(r"\b[0-9a-f]{32}\b")
_AMOUNT_RE = re.compile(r"(?:\$|R\$|US\$)\s?(\d[\d,]*(?:\.\d{1,2})?)")
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")
_PERCENT_RE = re.compile(r"(?<![\w.$])(-?\d{1,3}(?:,\d{3})+(?:\.\d+)?|-?\d+(?:\.\d+)?)\s?%")   # "12,828.3%" is read whole
_COUNT_RE = re.compile(r"(?<![\w.$/,-])(\d{1,3}(?:,\d{3})+|\d+)\s+(?:of\s+(?:your|the|these|those)\s+)?(?:(?:different|separate|distinct|"
                       r"delivered|canceled|cancelled|open|pending|late|overdue|other)\s+)?"
                       r"(orders?|items?|units?|products?|sales?|reviews?|returns?|refunds?|categories|category)\b", re.I)
# A count after these words is a condition the reply states ("at least 5 reviews"), not a fact.
_THRESHOLD_BEFORE = re.compile(r"(?:at least|at most|more than|less than|fewer than|over|under|above|below|"
                               r"minimum(?: of)?|maximum(?: of)?|up to|>=|<=|>|<)\s*$", re.I)
_ISO_DATE_RE = re.compile(r"\b(20\d\d-[01]\d(?:-[0-3]\d)?)\b")
_MONTHS = {m: i for i, m in enumerate(("january", "february", "march", "april", "may", "june", "july", "august",
                                        "september", "october", "november", "december"), 1)}
_WORD_DATE_RE = re.compile(r"\b(" + "|".join(_MONTHS) + r")\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(20\d\d)\b", re.I)


def _evidence(messages: list[ModelMessage]) -> str:
    parts: list[str] = []
    for msg in messages:
        if isinstance(msg, ModelRequest):
            for part in msg.parts:
                if isinstance(part, ToolReturnPart):
                    parts.append(json.dumps(part.content, default=str))
                elif isinstance(part, UserPromptPart):
                    parts.append(part.content if isinstance(part.content, str) else json.dumps(part.content, default=str))
    return "\n".join(parts)


def _list_sizes(messages: list[ModelMessage]) -> set[int]:
    sizes: set[int] = set()

    def walk(node):
        if isinstance(node, list):
            sizes.add(len(node))
            for v in node:
                walk(v)
        elif isinstance(node, dict):
            for v in node.values():
                walk(v)

    for msg in messages:
        if isinstance(msg, ModelRequest):
            for part in msg.parts:
                if isinstance(part, ToolReturnPart):
                    walk(part.content)
    return sizes


def _half_up(x: float, d: int) -> float:
    """9.85 -> 9.9 as people round it (round() gives 9.8: 9.85 is stored as 9.8499...)."""
    return float(Decimal(repr(x)).quantize(Decimal(1).scaleb(-d), ROUND_HALF_UP))


def _half_even(x: float, d: int) -> float:
    """12.345 -> 12.34, 3.45 -> 3.4 (banker's rounding, on the number as written)."""
    return float(Decimal(repr(x)).quantize(Decimal(1).scaleb(-d), ROUND_HALF_EVEN))


def unsupported_facts(reply: str, messages: list[ModelMessage], prior_replies: str = "") -> list[str]:
    """IDs, $ amounts, percentages, counts of things and dates in the reply that no lookup result,
    seller message or earlier checked reply supports."""
    evidence = _evidence(messages) + "\n" + prior_replies
    numbers = [float(n) for n in _NUMBER_RE.findall(evidence.replace(",", ""))]
    known = {round(v, 2) for v in numbers}
    percents = {f(x, d) for v in numbers for x in (abs(v), abs(v) * 100) for d in (0, 1, 2) for f in (round, _half_up, _half_even)}
    problems = [i for i in set(_ID_RE.findall(reply)) if i not in evidence]
    problems += [f"${raw}" for raw in set(_AMOUNT_RE.findall(reply)) if round(float(raw.replace(",", "")), 2) not in known]
    problems += [f"{raw}%" for raw in set(_PERCENT_RE.findall(reply)) if round(abs(float(raw.replace(",", ""))), 2) not in percents]
    sizes = _list_sizes(messages)
    counts = {(m.group(1), m.group(2).lower()) for m in _COUNT_RE.finditer(reply)
              if not _THRESHOLD_BEFORE.search(reply[max(0, m.start() - 20):m.start()])}
    for raw, noun in counts:
        n = int(raw.replace(",", ""))   # "1,850 orders" is 1850
        if n > 1 and n not in sizes and float(n) not in known:
            problems.append(f"{n} {noun}")
    dates = set(_ISO_DATE_RE.findall(reply))
    dates |= {f"{y}-{_MONTHS[m.lower()]:02d}-{int(d):02d}" for m, d, y in _WORD_DATE_RE.findall(reply)}
    problems += [d for d in dates if d not in evidence]
    return sorted(set(problems))


# ---- the agent -----------------------------------------------------------------------------------


@functools.lru_cache(maxsize=1)
def build_seller_agent() -> Agent[Deps, str]:
    agent = Agent(None, deps_type=Deps, output_type=str, tools=[_make_tool(s) for s in seller_tools()],
                  name="seller_assistant", retries=OUTPUT_RETRIES, defer_model_check=True)

    @agent.instructions
    def instructions(ctx: RunContext[Deps]) -> str:
        return system_prompt()

    @agent.output_validator
    def checks(ctx: RunContext[Deps], output: str) -> str:
        if getattr(ctx, "partial_output", False):
            return output
        problems = unsupported_facts(output, ctx.messages, ctx.deps.prior_replies)
        if problems:
            ctx.deps.retracted = True
            raise ModelRetry("Your reply mentions values that no lookup returned in this conversation: "
                             + ", ".join(problems) + ". Look them up, or remove them. Only state facts from lookups.")
        internals = mentions_internals(output)
        if internals:
            ctx.deps.retracted = True
            raise ModelRetry("Rewrite the reply without describing how you work (" + ", ".join(internals)
                             + "). Say what you can do for the seller, in their terms.")
        return output

    return agent


# ---- model choice (admin settings for the seller role) ------------------------------------------

_MODEL_NAMES: dict[int, str] = {}


def configured_model() -> str | None:
    return (store.settings().get("model_by_role") or {}).get(ROLE) or os.environ.get("AI_MODEL") or None


@functools.lru_cache(maxsize=8)
def get_model(name: str) -> Any:
    from pydantic_ai.models import infer_model

    if name.startswith("anthropic:") and not os.environ.get("ANTHROPIC_API_KEY") and os.environ.get("CLAUDE_API_KEY"):
        os.environ["ANTHROPIC_API_KEY"] = os.environ["CLAUDE_API_KEY"]
    model = infer_model(name)
    _MODEL_NAMES[id(model)] = name
    return model


@functools.lru_cache(maxsize=8)
def _with_fallback(name: str, fallback: str) -> Any:
    from pydantic_ai.models.fallback import FallbackModel

    model = FallbackModel(get_model(name), get_model(fallback))
    _MODEL_NAMES[id(model)] = name
    return model


def model_for(name: str) -> Any:
    from ai import answer_config

    fallback = store.settings().get("fallback_model")
    if not fallback or fallback == name or fallback not in answer_config.APPROVED_MODELS:
        return get_model(name)
    return _with_fallback(name, fallback)


def _model_name(model: Any) -> str:
    return model if isinstance(model, str) else _MODEL_NAMES.get(id(model)) or getattr(model, "model_name", "unknown")


def _answered_by(result: Any, default: str) -> str:
    for m in reversed(result.new_messages() if result is not None else []):
        if isinstance(m, ModelResponse) and m.model_name:
            known = [n for n in _MODEL_NAMES.values() if n.split(":", 1)[-1] == m.model_name]
            return known[0] if known else m.model_name
    return default


# Evaluation runs only (python -m ai.seller.eval_natural --temperature ...): settings for this
# process, applied after the admin's. Never set by the API.
MODEL_SETTINGS_OVERRIDE: dict[str, Any] = {}


def temperature_for(model_name: str) -> dict[str, Any]:
    from ai import answer_config

    value = (store.settings().get("temperature_by_role") or {}).get(ROLE)
    allowed = answer_config.temperature_range(model_name)
    if value is None or allowed is None or not allowed["min"] <= value <= allowed["max"]:
        return {}
    return {"temperature": float(value)}


def cost_usd(model: str, input_tokens: int, output_tokens: int, cached_tokens: int = 0) -> float:
    prices = (store.settings().get("model_prices") or {}).get(model)
    if not prices:
        return 0.0
    cached = min(cached_tokens or 0, input_tokens)
    return round(((input_tokens - cached) * prices["input"] + cached * prices.get("cached_input", prices["input"])
                  + output_tokens * prices["output"]) / 1_000_000, 6)


# ---- limits --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class TurnBudget:
    usage_limits: UsageLimits
    max_reply_tokens: int | None


def check_limits(principal: Principal, subject: str) -> TurnBudget:
    s = store.settings()
    if not s.get("ai_enabled", True) or not (s.get("roles_enabled") or {}).get(ROLE, True):
        raise SellerChatUnavailable("The seller assistant is switched off right now. Please try again later.", 503)
    limits = store.effective_limits(principal)
    if limits.blocked:
        raise SellerChatUnavailable("The assistant isn't available for this account. Please contact seller support.", 403)
    if limits.requests_per_minute is not None and store.requests_last_minute(subject) >= limits.requests_per_minute:
        raise SellerChatUnavailable("You're sending messages very quickly. Please wait a minute and try again.")
    today = store.usage_today(subject)
    if limits.messages_per_day is not None and today["requests"] >= limits.messages_per_day:
        raise SellerChatUnavailable("You've reached today's limit for the assistant. It resets at midnight.")
    left = lambda cap, used: None if cap is None else max(cap - used, 0)   # noqa: E731
    left_in, left_out = left(limits.input_tokens_per_day, today["input_tokens"]), left(limits.output_tokens_per_day, today["output_tokens"])
    if left_in == 0 or left_out == 0:
        raise SellerChatUnavailable("You've reached today's limit for the assistant. It resets at midnight.")
    return TurnBudget(UsageLimits(request_limit=MAX_MODEL_REQUESTS, tool_calls_limit=limits.max_tool_calls_per_turn,
                                  input_tokens_limit=left_in, output_tokens_limit=left_out), limits.max_reply_tokens)


def max_questions_per_chat() -> int | None:
    value = (store.settings().get("max_questions_per_chat") or {}).get(ROLE)
    return int(value) if isinstance(value, (int, float)) and value > 0 else None


# ---- history -------------------------------------------------------------------------------------


def _compact(turn: list[ModelMessage], reply: str) -> list[ModelMessage]:
    prompts = [p for m in turn if isinstance(m, ModelRequest) for p in m.parts if isinstance(p, UserPromptPart)]
    return [ModelRequest(parts=prompts[:1]), ModelResponse(parts=[TextPart(content=reply)])] if prompts else []


def _size(messages: list[ModelMessage]) -> int:
    chars = 0
    for msg in messages:
        for part in msg.parts:
            value = getattr(part, "args", None) if isinstance(part, ToolCallPart) else getattr(part, "content", "")
            chars += len(value) if isinstance(value, str) else len(json.dumps(value, default=str))
    return chars // 4


def history_window() -> tuple[int | None, int | None]:
    chosen = (store.settings().get("history_window_by_role") or {}).get(ROLE)
    if not isinstance(chosen, dict):
        return FULL_DETAIL_TURNS, QA_TURNS
    return chosen.get("full_turns", FULL_DETAIL_TURNS), chosen.get("qa_turns", QA_TURNS)


def load_history(conversation_id: str, question: str | None, questions_before: int) -> list[ModelMessage]:
    """Last turns in full, older ones as question + answer, the rest as key figures and related turns."""
    full_turns, qa_turns = history_window()
    window = None if full_turns is None or qa_turns is None else full_turns + qa_turns
    row_limit = ALL_ROWS if window is None else 2 * window + 10
    rows = store.history(conversation_id, row_limit)
    turns: list[list[dict]] = []
    for row in rows:
        if row["role"] == "user" or not turns:
            turns.append([row])
        else:
            turns[-1].append(row)
    if turns and turns[0][0]["role"] != "user" and len(rows) >= row_limit:
        turns = turns[1:]
    if window is not None:
        turns = turns[-window:] if window else []

    def as_messages(turn_rows: list[dict], full: bool) -> list[ModelMessage]:
        out: list[ModelMessage] = []
        for row in turn_rows:
            content = row["content"] or {}
            if row["role"] == "assistant" and content.get("model_messages"):
                turn = ModelMessagesTypeAdapter.validate_python(content["model_messages"])
                out.extend(memory.shorten_results(turn) if full else _compact(turn, content.get("text") or ""))
            elif row["role"] == "system" and content.get("text"):
                out.append(ModelRequest(parts=[UserPromptPart(
                    content=f"[Store event, not written by the seller] {content['text']}")]))
        return out

    first_full = 0 if full_turns is None else max(len(turns) - full_turns, 0)
    full_count = len(turns) - first_full
    blocks = [as_messages(t, full=i >= first_full) for i, t in enumerate(turns)]
    while len(blocks) > full_count and _size([m for b in blocks for m in b]) > HISTORY_TOKEN_BUDGET:
        blocks.pop(0)
        turns.pop(0)
    messages = [m for b in blocks for m in b]
    if (turns and turns[0][0]["sequence_no"] <= 1) or (not turns and not questions_before):
        return messages
    older: list[ModelMessage] = []
    try:
        first_turn_in_window = questions_before - sum(1 for t in turns if t[0]["role"] == "user") + 1
        facts = memory.facts_block(memory.load_facts(conversation_id), before_turn=first_turn_in_window)
        if facts:
            older.append(ModelRequest(parts=[UserPromptPart(content=facts)]))
        if question:
            older += memory.related_messages(memory.related_turns(
                conversation_id, question, before_seq=turns[0][0]["sequence_no"] if turns else ALL_ROWS))
    except Exception:
        log.exception("could not load the older-turn memory (conversation %s)", conversation_id)
    return older + messages


# ---- one turn -----------------------------------------------------------------------------------


@dataclass
class SellerTurn:
    principal: Principal
    message: str
    conversation_id: str
    subject: str
    model: Any
    budget: TurnBudget
    new_conversation: bool
    questions_before: int = 0
    usage: RunUsage = field(default_factory=RunUsage)
    tools_called: list[str] = field(default_factory=list)

    async def run(self) -> AsyncIterator[dict]:
        principal, conv = self.principal, self.conversation_id
        if self.new_conversation:
            yield {"type": "conversation", "conversation_id": conv}
        if requests_english(self.message):
            await _bg(store.append_message, conv, "user", {"text": self.message})
            yield {"type": "text", "delta": ENGLISH_ONLY_REPLY}
            message_id = await _bg(
                store.append_message, conv, "assistant", {"text": ENGLISH_ONLY_REPLY},
                model=LANGUAGE_GATE_MODEL, prompt_version=PROMPT_VERSION, tool_set_version=TOOL_SET_VERSION,
                input_tokens=0, output_tokens=0, cached_tokens=0)
            await _bg(store.record_usage, self.subject, ROLE, LANGUAGE_GATE_MODEL, 0, 0, 0, 0.0,
                      user_id=principal.user_id, conversation_id=conv)
            yield {"type": "message", "message_id": message_id}
            yield {"type": "done"}
            return
        model_name = _model_name(self.model)
        history, embedding = await asyncio.gather(
            _bg(load_history, conv, self.message, self.questions_before),
            asyncio.to_thread(memory.question_embedding, self.message))
        await _bg(store.append_message, conv, "user", {"text": self.message}, embedding=embedding)

        paused, why = resilience.is_open()
        if paused:
            text = resilience.message(why, hybrid=False)
            yield {"type": "text", "delta": text}
            message_id = await _bg(store.append_message, conv, "assistant", {"text": text}, model="unavailable",
                                   prompt_version=PROMPT_VERSION, tool_set_version=TOOL_SET_VERSION,
                                   input_tokens=0, output_tokens=0, cached_tokens=0)
            await _bg(store.record_usage, self.subject, ROLE, "unavailable", 0, 0, 0, 0.0,
                      user_id=principal.user_id, conversation_id=conv, count_request=False)
            yield {"type": "message", "message_id": message_id}
            yield {"type": "done"}
            return

        agent = build_seller_agent()
        deps = Deps(principal=principal, conversation_id=conv, prior_replies="\n".join(
            p.content for m in history if isinstance(m, ModelResponse) for p in m.parts if isinstance(p, TextPart)))
        from ai.answer_config import PROMPT_CACHE_SETTINGS
        settings = {"timeout": REQUEST_TIMEOUT_S, **PROMPT_CACHE_SETTINGS, **temperature_for(model_name),
                    **MODEL_SETTINGS_OVERRIDE,
                    **({"max_tokens": self.budget.max_reply_tokens} if self.budget.max_reply_tokens else {})}
        reply, result, streamed, failure, usage = "", None, False, None, self.usage
        tool_since_text = False   # text written before a lookup ("I'll check…") is replaced by the answer
        for attempt in range(2):
            tools_before = len(self.tools_called)
            try:
                async with asyncio.timeout(TURN_TIMEOUT_S), agent.run_stream_events(
                    self.message, deps=deps, model=self.model, message_history=history,
                    usage_limits=self.budget.usage_limits, usage=usage, model_settings=settings,
                ) as stream:
                    async for ev in stream:
                        if isinstance(ev, PartStartEvent) and isinstance(ev.part, TextPart):
                            if deps.retracted or (streamed and tool_since_text):
                                deps.retracted = False
                                yield {"type": "reset"}
                            tool_since_text = False
                            if ev.part.content:
                                streamed = True
                                yield {"type": "text", "delta": ev.part.content}
                        elif isinstance(ev, PartDeltaEvent) and isinstance(ev.delta, TextPartDelta):
                            if ev.delta.content_delta:
                                streamed = True
                                yield {"type": "text", "delta": ev.delta.content_delta}
                        elif isinstance(ev, FunctionToolCallEvent):
                            tool_since_text = True
                            self.tools_called.append(ev.part.tool_name)
                            yield {"type": "step", "id": ev.part.tool_call_id,
                                   "label": STEP_LABELS.get(ev.part.tool_name, "Working"), "status": "running"}
                        elif isinstance(ev, FunctionToolResultEvent):
                            content = getattr(ev.part, "content", None)
                            failed = not isinstance(ev.part, ToolReturnPart) or (isinstance(content, dict) and "error" in content)
                            yield {"type": "step", "id": ev.part.tool_call_id,
                                   "label": STEP_LABELS.get(ev.part.tool_name, "Working"),
                                   "status": "error" if failed else "done"}
                            for side in deps.side_events.pop(ev.part.tool_call_id, []):
                                yield side
                        elif isinstance(ev, AgentRunResultEvent):
                            result = ev.result
                reply, failure = (result.output if result is not None else ""), None
                break
            except UsageLimitExceeded:
                reply = "That request needed more work than I can do in one go. Could you narrow it down?"
                break
            except UnexpectedModelBehavior:
                log.warning("seller model failed the checks or misbehaved (conversation %s)", conv)
                reply = FALLBACK_REPLY
                break
            except Exception as exc:
                failure = resilience.classify(exc)
                resilience.record_failure(failure, exc)
                if failure == "unknown":
                    log.exception("seller chat turn failed (conversation %s)", conv)
                if (attempt == 0 and resilience.retryable(failure) and not streamed
                        and len(self.tools_called) == tools_before and not resilience.is_open()[0]):
                    await asyncio.sleep(min(resilience.retry_after_s(exc) or 3, 8))
                    deps.side_events.clear()
                    continue
                reply = resilience.message(failure, hybrid=False)
                break
        if result is not None:
            resilience.record_success()
        if result is None and reply:
            if streamed:
                yield {"type": "reset"}
            yield {"type": "text", "delta": reply}

        answered_by = _answered_by(result, model_name)
        message_id = await _bg(
            store.append_message, conv, "assistant",
            {"text": reply, "model_messages": ModelMessagesTypeAdapter.dump_python(result.new_messages(), mode="json")
             if result is not None else None},
            model=answered_by, prompt_version=PROMPT_VERSION, tool_set_version=TOOL_SET_VERSION,
            input_tokens=usage.input_tokens, output_tokens=usage.output_tokens, cached_tokens=usage.cache_read_tokens)
        await _bg(store.record_usage, self.subject, ROLE, answered_by, usage.input_tokens, usage.output_tokens,
                  usage.cache_read_tokens, cost_usd(answered_by, usage.input_tokens, usage.output_tokens,
                                                    usage.cache_read_tokens),
                  user_id=principal.user_id, conversation_id=conv, count_request=result is not None)
        if result is not None:
            try:
                await _bg(memory.remember, conv, self.questions_before + 1, self.message, reply)
            except Exception:
                log.exception("could not update the conversation memory (conversation %s)", conv)
        yield {"type": "message", "message_id": message_id}
        yield {"type": "done"}


def prepare_seller_turn(principal: Principal, message: str, *, conversation_id: str | None = None,
                        model: Any = None) -> SellerTurn:
    """Checks that must pass before streaming starts (so they can become HTTP errors)."""
    with db.as_seller_agent():
        if principal.role != ROLE or not principal.seller_id:
            raise SellerChatUnavailable("The seller assistant is for seller accounts.", 403)
        model = model or configured_model()
        if model is None:
            raise SellerChatUnavailable("not_configured", 503)
        if isinstance(model, str):
            model = model_for(model)
        subject = store.subject_for(principal)
        budget = check_limits(principal, subject)
        new, asked = False, 0
        if conversation_id:
            if not store.get_conversation(principal, conversation_id):
                raise SellerChatUnavailable("Conversation not found.", 404)
            asked = store.question_count(conversation_id)
            limit = max_questions_per_chat()
            if limit is not None and asked >= limit:
                raise SellerChatUnavailable(f"Each chat can have up to {limit} question{'s' if limit != 1 else ''}, "
                                            "and this one has reached it. Please start a new chat to continue.", 409)
        else:
            conversation_id = store.create_conversation(principal, title=message.strip()[:80])
            new = True
    return SellerTurn(principal, message, conversation_id, subject, model, budget, new, questions_before=asked)


async def seller_chat_turn(principal: Principal, message: str, *, conversation_id: str | None = None,
                           model: Any = None) -> AsyncIterator[dict]:
    turn = prepare_seller_turn(principal, message, conversation_id=conversation_id, model=model)
    async for event in turn.run():
        yield event


def confirm_seller_action(principal: Principal, action_id: str, approve: bool) -> dict:
    """The seller's Confirm / Cancel click on a confirm card. Each action runs at most once."""
    with db.as_seller_agent():
        if principal.role != ROLE:
            raise SellerChatUnavailable("This request has expired or was already handled.", 409)
        action = store.claim_pending_action(principal, action_id, approve)
        if action is None:
            raise SellerChatUnavailable("This request has expired or was already handled.", 409)
        conv = action["conversation_id"]
        if not approve:
            store.append_message(conv, "system", {"text": f"The seller declined the proposed {action['tool_name']}."})
            return {"status": "rejected"}
        try:
            get_seller_tool(principal, action["tool_name"])
            result, ok, status = call_seller_tool(principal, action["tool_name"], action["args"], confirmed=True), True, "ok"
        except ToolError as exc:
            result, ok, status = {"error": str(exc)}, False, _status_for(exc)
        except Exception:
            log.exception("confirmed seller action %s failed", action_id)
            result, ok, status = {"error": "The change could not be made. Please try again later."}, False, "error"
        store.finish_pending_action(action_id, ok, result)
        store.record_tool_call(conv, action["tool_name"], ToolKind.WRITE_CONFIRM.value, action["args"], status,
                               confirmed_by_user_id=principal.user_id)
        store.append_message(conv, "system", {"text": f"The seller confirmed {action['tool_name']}; it "
                                                     f"{'succeeded' if ok else 'failed'}. Result: {json.dumps(result, default=str)}"})
        return {"status": "executed" if ok else "failed", "result": result}


def warm_up() -> None:
    """Build the agent and the model client before the first seller asks."""
    build_seller_agent()
    name = configured_model()
    if name:
        model_for(name)

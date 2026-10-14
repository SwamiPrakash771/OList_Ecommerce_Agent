"""The admin agent's engine: one chat turn, from the admin's message to a checked reply (shown once it passes).

Separate from the customer and seller engines; it imports neither. Built on Pydantic AI:

- The agent gets exactly the admin tools (ai/admin/security.py). Each call goes through
  `call_admin_tool` with the server-side `Principal`, on the admin agent's own MySQL user. The agent
  is read-only: there are no confirm cards.
- Each lookup is stored with the question that caused it (ai_tool_calls.message_id), so the admin's
  chat history shows the lookups under each answer; record lookups are also written to the access log.
- Before each turn: switches, block flag, rate limit, daily budgets, questions per chat (admin settings
  for the admin role). During the turn: request, tool-call and token limits.
- Fact check: every ID, masked customer label, $ amount (also "$13.49M"), percentage, count and date
  in the reply must appear in a lookup result, the admin's words or an earlier checked reply.
- Admin chats are kept 1 year (retention class admin_audit) and can't be deleted.

Shared on purpose: the signed-in identity, admin settings, the provider health tracker
(ai/resilience.py) and the chat-memory helpers (ai/memory.py).
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

import ai.admin.tools  # noqa: F401  (registers the admin tools)
import db
from ai import memory, resilience, store
from ai.language_gate import ENGLISH_ONLY_REPLY, LANGUAGE_GATE_MODEL, requests_english
from ai.admin.prompts import PROMPT_VERSION, system_prompt
from ai.admin.security import AdminTool, admin_tools, call_admin_tool, in_admin_conversation
from ai.admin.tools.charts import collect_charts
from ai.admin.tools.query import TABLES as PLATFORM_TABLES
from ai.security import InvalidArguments, NotFound, Principal, ToolError

log = logging.getLogger("ai.admin.agent")

ROLE = "admin"
TOOL_SET_VERSION = "admin-tools-v1"
MAX_MODEL_REQUESTS = 10    # model calls per turn (tool loops included)
OUTPUT_RETRIES = 2         # fact-check retries before giving up
REQUEST_TIMEOUT_S = 30     # one model call
TURN_TIMEOUT_S = 60        # a whole message (platform-wide metrics can take a few seconds each)
FULL_DETAIL_TURNS, QA_TURNS = 5, 25      # history defaults (admin can change them per role)
HISTORY_TOKEN_BUDGET = 12_000
ALL_ROWS = 1_000_000

FALLBACK_REPLY = ("I couldn't verify every figure in my answer, so I haven't shown it. Please try again, or ask "
                  "for fewer figures at once.")
STEP_LABELS = {
    "admin_metric": "Calculating figures",
    "show_chart": "Drawing a chart",
    "query_platform_data": "Running a custom analysis",
    "look_up_record": "Looking up the record",
    "operations_report": "Checking operations",
}


class AdminChatUnavailable(Exception):
    """The turn can't start (switched off, blocked or over a limit). The message is user-safe."""

    def __init__(self, message: str, status: int = 429):
        super().__init__(message)
        self.status = status


@dataclass
class Deps:
    principal: Principal
    conversation_id: str
    question_id: int | None = None          # the stored question this turn's lookups belong to
    side_events: dict[str, list[dict]] = field(default_factory=dict)   # charts, by tool_call_id
    charts: list[dict] = field(default_factory=list)                   # stored with the answer
    checks: list[dict] = field(default_factory=list)   # rejected drafts and why: stored for diagnosis, never shown
    retracted: bool = False
    prior_replies: str = ""


def _admin_db(fn, *args, **kwargs):
    """Storage and lookups for the admin agent run on its own MySQL user."""
    with db.as_admin_agent():
        return fn(*args, **kwargs)


async def _bg(fn, *args, **kwargs):
    return await asyncio.to_thread(_admin_db, fn, *args, **kwargs)


# ---- tools ---------------------------------------------------------------------------------------


def _strip_titles(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _strip_titles(v) for k, v in node.items() if not (k == "title" and isinstance(v, str))}
    if isinstance(node, list):
        return [_strip_titles(v) for v in node]
    return node


def _args_schema(spec: AdminTool) -> dict:
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


def _make_tool(spec: AdminTool) -> Tool:
    def run(ctx: RunContext[Deps], **args) -> Any:
        deps, started, status, result = ctx.deps, time.monotonic(), "ok", None
        # sync tools run in a worker thread: set the database user and the audit conversation here
        with db.as_admin_agent(), in_admin_conversation(deps.conversation_id), collect_charts() as charts:
            try:
                result = call_admin_tool(deps.principal, spec.name, args)
                for chart in charts:   # shown in the chat right after this lookup, and stored with the answer
                    deps.side_events.setdefault(ctx.tool_call_id, []).append({"type": "chart", "chart": chart})
                    deps.charts.append(chart)
            except InvalidArguments as exc:
                status = "invalid_args"
                raise ModelRetry(str(exc)) from None
            except ToolError as exc:
                status, result = _status_for(exc), {"error": str(exc)}
            except Exception:
                log.exception("admin tool %s failed", spec.name)
                status, result = "error", {"error": "That lookup failed on our side. Please try again later."}
            finally:
                try:
                    store.record_tool_call(deps.conversation_id, spec.name, "read", args, status,
                                           message_id=deps.question_id,
                                           duration_ms=int((time.monotonic() - started) * 1000))
                except Exception:
                    log.exception("could not record tool call")
        return result

    return Tool.from_schema(run, name=spec.name, description=spec.description, json_schema=_args_schema(spec),
                            takes_ctx=True)


# ---- reply checks --------------------------------------------------------------------------------

_INTERNAL_RE = re.compile(
    r"\b(?:sql|system prompt|my prompt|my instructions|language model|llm|"
    r"(?:my|our|the|these|this|a|available|internal|search|system)(?:\s+\w+){0,3}\s+tools?|tools? (?:i|we) (?:use|have)|"
    r"function calls?|json|api calls?)\b", re.I)
_PRODUCT_NAME_RE = re.compile(r"\b[A-Z][a-z]+(?: [a-z]+)* #[0-9a-f]{6}\b")


# Checks talk to the model, never to the admin: the answer must read as if no check had happened.
RETRY_NOTE = ("[Automatic check, not a message from the admin; never mention it, apologise for it, or say values "
              "were removed or recalculated.] ")
# Replies that address the checks instead of the admin ("Let me remove those calculated values").
_META_RE = re.compile(
    r"\b(?:(?:remove|removed|removing) (?:those|these|the|any) (?:calculated |computed |made-up )?(?:values|figures|numbers)|"
    r"(?:values|figures|numbers) (?:that (?:are|were) )?not (?:in|from) (?:the |my )?lookups?|"
    r"calculat(?:ed|ing) values|apologi[sz]e for calculating|(?:the|my) lookups? (?:returned|shows?|showed)|"
    r"automatic check)\b", re.I)


def mentions_internals(reply: str) -> list[str]:
    """Words that reveal how the assistant works: tool names, its table names, SQL, prompts."""
    text = _PRODUCT_NAME_RE.sub("", reply)
    found = {m.group(0).lower() for m in _INTERNAL_RE.finditer(text)}
    found |= {m.group(0).lower() for m in _META_RE.finditer(text)}
    found |= {n for n in [*STEP_LABELS, *PLATFORM_TABLES, "admin_order_facts", "admin_seller_order_facts",
                          "admin_item_facts"] if re.search(rf"\b{re.escape(n)}\b", text)}
    return sorted(found)


_ID_RE = re.compile(r"\b[0-9a-f]{32}\b")
_MASKED_RE = re.compile(r"\bcustomer-[0-9a-f]{8}\b")
_SCALES = {"k": 1e3, "thousand": 1e3, "m": 1e6, "mn": 1e6, "mi": 1e6, "million": 1e6, "milhão": 1e6,
           "milhões": 1e6, "b": 1e9, "bn": 1e9, "billion": 1e9, "bilhão": 1e9, "bilhões": 1e9}
_SCALE_WORDS = "|".join(sorted(_SCALES, key=len, reverse=True))
_AMOUNT_RE = re.compile(rf"(?:\$|R\$|US\$)\s?(\d[\d,]*(?:\.\d+)?)(?:\s?({_SCALE_WORDS})\b)?", re.I)
_SCALED_RE = re.compile(rf"(?<![\w.$])(\d[\d,]*(?:\.\d+)?)\s?({_SCALE_WORDS})\b(?!\s*%)", re.I)
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")
_PERCENT_RE = re.compile(r"(?<![\w.$])(-?\d{1,3}(?:,\d{3})+(?:\.\d+)?|-?\d+(?:\.\d+)?)\s?%")   # "12,828.3%" is read whole
_COUNT_RE = re.compile(r"(?<![\w.$/,-])(\d{1,3}(?:,\d{3})+|\d+)\s+(?:(?:different|separate|distinct|new|active|"
                       r"delivered|canceled|cancelled|open|pending|late|overdue|stuck|failed|other|repeat)\s+)?"
                       r"(orders?|items?|units?|products?|sales?|reviews?|returns?|refunds?|categories|category|"
                       r"sellers?|customers?|tickets?|replies|states?)\b", re.I)
_THRESHOLD_BEFORE = re.compile(r"(?:at least|at most|more than|less than|fewer than|over|under|above|below|"
                               r"minimum(?: of)?|maximum(?: of)?|up to|top|>=|<=|>|<)\s*$", re.I)
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


def _scaled_supported(raw: str, scale: float, numbers: list[float]) -> bool:
    """'$13.49M' is supported by 13,494,652.70: equal once rounded to the shown precision."""
    decimals = len(raw.split(".")[1]) if "." in raw else 0
    shown = float(raw.replace(",", ""))
    half_step = 0.5 * 10 ** -decimals + 1e-9
    return any(abs(v / scale - shown) <= half_step for v in numbers if v)


def _half_up(x: float, d: int) -> float:
    """9.85 -> 9.9 as people round it (round() gives 9.8: 9.85 is stored as 9.8499...)."""
    return float(Decimal(repr(x)).quantize(Decimal(1).scaleb(-d), ROUND_HALF_UP))


def _half_even(x: float, d: int) -> float:
    """12.345 -> 12.34, 3.45 -> 3.4 (banker's rounding, on the number as written)."""
    return float(Decimal(repr(x)).quantize(Decimal(1).scaleb(-d), ROUND_HALF_EVEN))


def unsupported_facts(reply: str, messages: list[ModelMessage], prior_replies: str = "") -> list[str]:
    """IDs, masked customers, $ amounts, scaled figures, percentages, counts and dates in the reply that no
    lookup result, admin message or earlier checked reply supports."""
    evidence = _evidence(messages) + "\n" + prior_replies
    numbers = [abs(float(n)) for n in _NUMBER_RE.findall(evidence.replace(",", ""))]
    known = {round(v, 2) for v in numbers}
    percents = {f(abs(x), d) for v in numbers for x in (v, v * 100) for d in (0, 1, 2) for f in (round, _half_up, _half_even)}
    problems = [i for i in set(_ID_RE.findall(reply)) if i not in evidence]
    problems += [c for c in set(_MASKED_RE.findall(reply)) if c not in evidence]
    for raw, unit in set(_AMOUNT_RE.findall(reply)):
        if unit:
            if not _scaled_supported(raw, _SCALES[unit.lower()], numbers):
                problems.append(f"${raw}{unit}")
        elif round(float(raw.replace(",", "")), 2) not in known and not (
                # "$134,568" for 134,567.77: rounded to whole dollars (not for small sums, where it would hide a guess)
                float(raw.replace(",", "")) >= 100 and _scaled_supported(raw, 1, numbers)):
            problems.append(f"${raw}")
    for raw, unit in set(_SCALED_RE.findall(reply)):
        if not _scaled_supported(raw, _SCALES[unit.lower()], numbers):
            problems.append(f"{raw} {unit}")
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
def build_admin_agent() -> Agent[Deps, str]:
    agent = Agent(None, deps_type=Deps, output_type=str, tools=[_make_tool(s) for s in admin_tools()],
                  name="admin_assistant", retries=OUTPUT_RETRIES, defer_model_check=True)

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
            ctx.deps.checks.append({"rejected": "unsupported values", "values": problems, "draft": output[:4000]})
            raise ModelRetry(RETRY_NOTE + "Your draft states values that no lookup returned: " + ", ".join(problems)
                             + ". The lookups above usually already hold the right figure (each row's value, change "
                             "and change_pct): copy it from its own row, rounded only, without new lookups. Look up "
                             "only a figure no result has (a metric option such as a comparison, or a custom "
                             "analysis), or leave it out, then write the whole answer again.")
        internals = mentions_internals(output)
        if internals:
            ctx.deps.retracted = True
            ctx.deps.checks.append({"rejected": "describes how it works", "values": internals, "draft": output[:4000]})
            raise ModelRetry(RETRY_NOTE + "Your draft describes how you work or how it was checked ("
                             + ", ".join(internals) + "). Write the whole answer again in business terms.")
        return output

    return agent


# ---- model choice (admin settings for the admin role) -------------------------------------------

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


# Evaluation runs only (python -m ai.admin.eval_natural --temperature ...). Never set by the API.
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
        raise AdminChatUnavailable("The admin assistant is switched off right now (AI settings).", 503)
    limits = store.effective_limits(principal)
    if limits.blocked:
        raise AdminChatUnavailable("The assistant is blocked for this account (AI settings).", 403)
    if limits.requests_per_minute is not None and store.requests_last_minute(subject) >= limits.requests_per_minute:
        raise AdminChatUnavailable("You're sending messages very quickly. Please wait a minute and try again.")
    today = store.usage_today(subject)
    if limits.messages_per_day is not None and today["requests"] >= limits.messages_per_day:
        raise AdminChatUnavailable("You've reached today's limit for the assistant. It resets at midnight.")
    left = lambda cap, used: None if cap is None else max(cap - used, 0)   # noqa: E731
    left_in, left_out = left(limits.input_tokens_per_day, today["input_tokens"]), left(limits.output_tokens_per_day, today["output_tokens"])
    if left_in == 0 or left_out == 0:
        raise AdminChatUnavailable("You've reached today's limit for the assistant. It resets at midnight.")
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
class AdminTurn:
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
        question_id = await _bg(store.append_message, conv, "user", {"text": self.message}, embedding=embedding)

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

        agent = build_admin_agent()
        deps = Deps(principal=principal, conversation_id=conv, question_id=question_id, prior_replies="\n".join(
            p.content for m in history if isinstance(m, ModelResponse) for p in m.parts if isinstance(p, TextPart)))
        settings = {"timeout": REQUEST_TIMEOUT_S, **temperature_for(model_name), **MODEL_SETTINGS_OVERRIDE,
                    **({"max_tokens": self.budget.max_reply_tokens} if self.budget.max_reply_tokens else {})}
        reply, result, streamed, failure, usage = "", None, False, None, self.usage
        # Admin answers are full of figures: nothing is shown until the whole answer has passed the checks.
        # While the model writes, the admin sees a "Checking the figures" step instead of a draft.
        checking = False
        for attempt in range(2):
            tools_before = len(self.tools_called)
            try:
                async with asyncio.timeout(TURN_TIMEOUT_S), agent.run_stream_events(
                    self.message, deps=deps, model=self.model, message_history=history,
                    usage_limits=self.budget.usage_limits, usage=usage, model_settings=settings,
                ) as stream:
                    async for ev in stream:
                        if isinstance(ev, (PartStartEvent, PartDeltaEvent)) and isinstance(
                                getattr(ev, "part", None) or getattr(ev, "delta", None), (TextPart, TextPartDelta)):
                            if not checking:   # a draft is being written: held back until it passes
                                checking = True
                                yield {"type": "step", "id": "check", "label": "Checking the figures", "status": "running"}
                        elif isinstance(ev, FunctionToolCallEvent):
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
                reply = "That question needed more work than I can do in one go. Could you narrow it down?"
                break
            except UnexpectedModelBehavior:
                log.warning("admin model failed the checks or misbehaved (conversation %s)", conv)
                reply = FALLBACK_REPLY
                break
            except Exception as exc:
                failure = resilience.classify(exc)
                resilience.record_failure(failure, exc)
                if failure == "unknown":
                    log.exception("admin chat turn failed (conversation %s)", conv)
                if (attempt == 0 and resilience.retryable(failure) and not streamed
                        and len(self.tools_called) == tools_before and not resilience.is_open()[0]):
                    await asyncio.sleep(min(resilience.retry_after_s(exc) or 3, 8))
                    deps.side_events.clear()
                    continue
                reply = resilience.message(failure, hybrid=False)
                break
        if result is not None:
            resilience.record_success()
        if checking:
            yield {"type": "step", "id": "check", "label": "Checking the figures",
                   "status": "done" if result is not None else "error"}
        if reply:
            yield {"type": "text", "delta": reply}   # only now: the answer that passed (or the fallback)
        if result is None and deps.checks:
            log.warning("admin answer withheld after %d rejected draft(s) (conversation %s): %s", len(deps.checks),
                        conv, "; ".join(f"{c['rejected']}: {', '.join(map(str, c['values']))}" for c in deps.checks))

        answered_by = _answered_by(result, model_name)
        message_id = await _bg(
            store.append_message, conv, "assistant",
            {"text": reply, "model_messages": ModelMessagesTypeAdapter.dump_python(result.new_messages(), mode="json")
             if result is not None else None, **({"charts": deps.charts} if deps.charts else {}),
             **({"checks": deps.checks} if deps.checks else {})},
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


def prepare_admin_turn(principal: Principal, message: str, *, conversation_id: str | None = None,
                       model: Any = None) -> AdminTurn:
    """Checks that must pass before streaming starts (so they can become HTTP errors)."""
    with db.as_admin_agent():
        if principal.role != ROLE or not principal.user_id:
            raise AdminChatUnavailable("The admin assistant is for admin accounts.", 403)
        model = model or configured_model()
        if model is None:
            raise AdminChatUnavailable("not_configured", 503)
        if isinstance(model, str):
            model = model_for(model)
        subject = store.subject_for(principal)
        budget = check_limits(principal, subject)
        new, asked = False, 0
        if conversation_id:
            if not store.get_conversation(principal, conversation_id):
                raise AdminChatUnavailable("Conversation not found.", 404)
            asked = store.question_count(conversation_id)
            limit = max_questions_per_chat()
            if limit is not None and asked >= limit:
                raise AdminChatUnavailable(f"Each chat can have up to {limit} question{'s' if limit != 1 else ''}, "
                                           "and this one has reached it. Please start a new chat to continue.", 409)
        else:
            conversation_id = store.create_conversation(principal, title=message.strip()[:80])
            new = True
    return AdminTurn(principal, message, conversation_id, subject, model, budget, new, questions_before=asked)


async def admin_chat_turn(principal: Principal, message: str, *, conversation_id: str | None = None,
                          model: Any = None) -> AsyncIterator[dict]:
    turn = prepare_admin_turn(principal, message, conversation_id=conversation_id, model=model)
    async for event in turn.run():
        yield event


def lookup_details(lookups: list[dict]) -> list[dict]:
    """For the history UI: each lookup with a readable label and, for metrics, the definition used."""
    from ai.admin.tools.metrics import semantic

    metrics = semantic()["metrics"]
    out = []
    for lk in lookups:
        item = {**lk, "label": STEP_LABELS.get(lk["tool"], lk["tool"])}
        metric = (lk.get("args") or {}).get("metric")
        if lk["tool"] == "admin_metric" and metric in metrics:
            item["definition"] = metrics[metric]["label"]
        out.append(item)
    return out


def warm_up() -> None:
    """Build the agent and the model client before the first admin asks."""
    build_admin_agent()
    name = configured_model()
    if name:
        model_for(name)

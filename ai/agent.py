"""The support agent engine: one chat turn, from the user's message to a streamed, checked reply.

Built on Pydantic AI. The security rules stay in ai/security.py; this module only connects
them to the model:

- The agent gets exactly `tools_for(role)`. Each tool call goes through `call_tool` with the
  server-side `Principal`, so the model can't pick whose data it reads.
- Confirm tools never run here. The model's call is turned into a server-built confirm card
  (a pending action); the change happens only when the user clicks Confirm (`confirm_action`).
- Before each turn: kill switches, block flag, rate limit, daily messages and token budgets.
  During the turn: request, tool-call and token limits.
- Fact check: every order/product/ticket ID, $ amount, percentage, count of things ("12 orders")
  and date in the final reply must appear in a tool result (or the user's own messages, or an
  earlier checked reply) from this conversation, or the model must retry.
- Conversations, messages, tool calls and usage are stored (ai/store.py).

`chat_turn` yields UI events (see the protocol in api/main.py).
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
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai.usage import RunUsage, UsageLimits

import ai.tools  # noqa: F401  (registers the tools)
from ai import packs as packs_lib
from ai import memory, resilience, store
from ai.prompts import PROMPT_VERSION, system_prompt
from ai.security import (
    InvalidArguments, NotFound, Principal, ToolError, ToolKind, ToolSpec, call_tool, get_tool,
    in_conversation, preview_tool, tools_for,
)

log = logging.getLogger("ai.agent")

SUPPORTED_ROLES = ("guest", "customer")
# History sent with each question (docs/CONTEXT_PLAN.md): the last FULL_DETAIL_TURNS turns in full,
# the QA_TURNS before them as question + answer, older turns as key figures + related turns (ai/memory.py).
FULL_DETAIL_TURNS = memory.FULL_DETAIL_TURNS
QA_TURNS = memory.QA_TURNS
ALL_ROWS = 1_000_000   # "unlimited" window: read every stored message of the chat
HISTORY_TOKEN_BUDGET = 12_000   # safety net: oldest question + answer turns are left out above this
MAX_MODEL_REQUESTS = 10    # model calls per turn (tool loops included)
OUTPUT_RETRIES = 2         # fact-check retries before giving up
TOOL_SET_VERSION = "support-tools-v1"
REQUEST_TIMEOUT_S = 20     # one model call
TURN_TIMEOUT_S = 45        # a whole message (several calls and tools)
LOCAL_MODEL_NAME = "local:fast-path"   # recorded on messages answered without the LLM

FALLBACK_REPLY = ("Sorry, I couldn't put together a reliable answer just now. "
                  "Please try again, or ask me to connect you with a person.")

STEP_LABELS = {
    "find_products": "Looking at products",
    "look_up_order": "Looking up the order",
    "my_account": "Checking your account",
    "query_my_data": "Analysing your data",
    "update_cart": "Updating your cart",
    "update_address": "Preparing the address change",
    "request_return": "Preparing the return request",
    "open_ticket": "Preparing a support ticket",
    "transfer_to_human": "Preparing the hand-off to a person",
}
CART_TOOLS = {"update_cart"}


class ChatUnavailable(Exception):
    """The turn can't start (switched off, blocked or over a limit). The message is user-safe."""

    def __init__(self, message: str, status: int = 429):
        super().__init__(message)
        self.status = status


@dataclass
class Deps:
    principal: Principal
    conversation_id: str
    # UI events produced inside tools (confirm cards, undo), keyed by tool_call_id.
    side_events: dict[str, list[dict]] = field(default_factory=dict)
    retracted: bool = False   # the fact check rejected a reply that was already streamed
    prior_replies: str = ""   # earlier replies in this conversation (already fact-checked)
    # RAG: tool packs (and prompt sections) this turn may use; None = all. load_more can add packs.
    packs: frozenset[str] | None = None
    loaded: list[str] = field(default_factory=list)
    sql_section: str = ""     # retrieved example queries, used when the analytics pack is active


# ---- tools ---------------------------------------------------------------------------------


def _args_schema(spec: ToolSpec) -> dict:
    """JSON schema of the model-supplied arguments (everything except `principal`)."""
    func = inspect.unwrap(spec.func)
    hints = typing.get_type_hints(func, include_extras=True)
    fields: dict[str, Any] = {}
    for name, param in list(inspect.signature(func).parameters.items())[1:]:
        default = param.default
        if default is inspect.Parameter.empty:
            default = ...
        fields[name] = (hints.get(name, Any), default if isinstance(default, FieldInfo) else default)
    model = create_model(f"{spec.name}_args", **fields)
    return _strip_titles(model.model_json_schema())


def _strip_titles(node: Any) -> Any:
    """Drop pydantic's generated "title" keys: they repeat the property names and cost tokens."""
    if isinstance(node, dict):
        return {k: _strip_titles(v) for k, v in node.items()
                if not (k == "title" and isinstance(v, str))}
    if isinstance(node, list):
        return [_strip_titles(v) for v in node]
    return node


def _status_for(exc: Exception) -> str:
    if isinstance(exc, NotFound):
        return "not_found"
    if isinstance(exc, InvalidArguments):
        return "invalid_args"
    return "error"


def _make_tool(spec: ToolSpec) -> Tool:
    def run(ctx: RunContext[Deps], **args) -> Any:
        deps = ctx.deps
        started = time.monotonic()
        status, result = "ok", None
        # Sync tools run in a worker thread: set the conversation context here, not outside.
        with in_conversation(deps.conversation_id):
            try:
                if spec.kind is ToolKind.WRITE_CONFIRM:
                    result = _propose(deps, spec, args, ctx.tool_call_id)
                    status = "needs_confirmation"
                else:
                    result = call_tool(deps.principal, spec.name, args)
                    if spec.kind is ToolKind.WRITE_UNDO:
                        result = _offer_undo(deps, spec, result, ctx.tool_call_id)
            except InvalidArguments as exc:
                status = "invalid_args"
                raise ModelRetry(str(exc)) from None
            except ToolError as exc:
                status, result = _status_for(exc), {"error": str(exc)}
            except Exception:
                log.exception("tool %s failed", spec.name)
                status, result = "error", {"error": "That lookup failed on our side. Please try again later."}
            finally:
                try:
                    store.record_tool_call(deps.conversation_id, spec.name, spec.kind.value, args, status,
                                           duration_ms=int((time.monotonic() - started) * 1000))
                except Exception:
                    log.exception("could not record tool call")
        return result

    return Tool.from_schema(run, name=spec.name, description=spec.description,
                            json_schema=_args_schema(spec), takes_ctx=True)


LOAD_MORE_DESCRIPTION = (
    "Only if you need a capability whose tools you don't have in this conversation: unlock that "
    "group. orders = order history and tracking; returns = returns and refunds; cart = the cart; "
    "account = address and support tickets; analytics = totals, counts and other calculations over "
    "the customer's data; catalog = product search. Never mention this to the customer.")


def _load_more_tool() -> Tool:
    def load_more(ctx: RunContext[Deps], pack: str) -> dict:
        if pack not in packs_lib.ALL_PACKS:
            raise ModelRetry(f"Unknown group '{pack}'. Choose one of: {', '.join(sorted(packs_lib.ALL_PACKS))}.")
        if ctx.deps.packs is not None and pack not in ctx.deps.packs:
            ctx.deps.packs = ctx.deps.packs | {pack}
            ctx.deps.loaded.append(pack)
        return {"loaded": pack, "note": "Those tools are now available; call them as usual."}

    schema = {"type": "object", "properties": {"pack": {"type": "string", "enum": sorted(packs_lib.ALL_PACKS)}},
              "required": ["pack"]}
    return Tool.from_schema(load_more, name="load_more", description=LOAD_MORE_DESCRIPTION,
                            json_schema=schema, takes_ctx=True)


def _propose(deps: Deps, spec: ToolSpec, args: dict, tool_call_id: str) -> dict:
    """A confirm tool call becomes a confirm card; nothing changes until the user confirms."""
    if not store.settings().get("write_actions_enabled", {}).get(deps.principal.role, True):
        raise ToolError("Making changes through the assistant is switched off right now.")
    card = preview_tool(deps.principal, spec.name, args)       # validates and checks ownership
    action = store.create_pending_action(deps.principal, deps.conversation_id, spec.name, args, card)
    deps.side_events.setdefault(tool_call_id, []).append(
        {"type": "confirm", "action_id": action["action_id"], "tool": spec.name, "card": card})
    return {"status": "awaiting_customer_confirmation", "action_id": action["action_id"],
            "confirm_card": card,
            "note": "Nothing has changed yet. The customer sees this card and must click Confirm."}


def _offer_undo(deps: Deps, spec: ToolSpec, result: Any, tool_call_id: str) -> Any:
    """Keep the undo step on the server side of the stream; the model only learns it exists."""
    if isinstance(result, dict) and "undo" in result:
        result = dict(result)
        undo = result.pop("undo")
        if undo and undo.get("tool"):
            deps.side_events.setdefault(tool_call_id, []).append(
                {"type": "undo", "tool": undo["tool"], "args": undo["args"], "label": STEP_LABELS.get(spec.name)})
            result["undo_available"] = True
    if spec.name in CART_TOOLS:
        deps.side_events.setdefault(tool_call_id, []).append({"type": "cart_changed"})
    return result


# ---- fact check ----------------------------------------------------------------------------

# ---- internals check: replies describe what the assistant does, never how it works -------------

_PRODUCT_NAME_RE = re.compile(r"\b[A-Z][a-z]+(?: [a-z]+)* #[0-9a-f]{6}\b")   # "Garden tools #422879"
_INTERNAL_RE = re.compile(
    r"\b(?:sql|database|db|system prompt|my prompt|my instructions|language model|llm|"
    r"(?:my|our|the|these|available|internal|search|system)(?:\s+\w+)?\s+tools?|tools? (?:i|we) (?:use|have)|"
    r"function calls?|json|api calls?)\b", re.I)


def mentions_internals(reply: str) -> list[str]:
    """Words that reveal the system's internals: tool names, internal table names, SQL, prompts."""
    text = _PRODUCT_NAME_RE.sub("", reply)
    from ai.security import REGISTRY
    from ai.tools.query_tools import ALL_TABLES

    found = {m.group(0).lower() for m in _INTERNAL_RE.finditer(text)}
    found |= {name for name in [*REGISTRY, *ALL_TABLES, "load_more"] if re.search(rf"\b{re.escape(name)}\b", text)
              and "_" in name}
    return sorted(found)


_ID_RE = re.compile(r"\b[0-9a-f]{32}\b")
_AMOUNT_RE = re.compile(r"(?:\$|R\$|US\$)\s?(\d[\d,]*(?:\.\d{1,2})?)")
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _evidence(messages: list[ModelMessage]) -> str:
    """Everything the reply may quote: tool results and what the user wrote."""
    parts: list[str] = []
    for msg in messages:
        if isinstance(msg, ModelRequest):
            for part in msg.parts:
                if isinstance(part, ToolReturnPart):
                    parts.append(json.dumps(part.content, default=str))
                elif isinstance(part, UserPromptPart):
                    parts.append(part.content if isinstance(part.content, str) else json.dumps(part.content, default=str))
    return "\n".join(parts)


_PERCENT_RE = re.compile(r"(?<![\w.$])(-?\d+(?:\.\d+)?)\s?%")
# "12 orders", "3 items"... Time units (days, months) are left out: policies such as "30 days" come
# from the prompt, not from a tool.
_COUNT_RE = re.compile(r"(?<![\w.$/,-])(\d{1,3}(?:,\d{3})+|\d+)\s+(?:of\s+(?:your|the|these|those)\s+)?(?:(?:different|separate|distinct|"
                       r"delivered|canceled|cancelled|open|pending|active|other)\s+)?"
                       r"(orders?|items?|products?|purchases?|returns?|refunds?|tickets?|payments?|categories|category|"
                       r"sellers?|reviews?)\b", re.I)
# A count after these words is a condition the reply states ("at least 5 reviews"), not a fact.
_THRESHOLD_BEFORE = re.compile(r"(?:at least|at most|more than|less than|fewer than|over|under|above|below|"
                               r"minimum(?: of)?|maximum(?: of)?|up to|>=|<=|>|<)\s*$", re.I)
_ISO_DATE_RE = re.compile(r"\b(20\d\d-[01]\d(?:-[0-3]\d)?)\b")
_MONTHS = {m: i for i, m in enumerate(("january", "february", "march", "april", "may", "june", "july", "august",
                                        "september", "october", "november", "december"), 1)}
_WORD_DATE_RE = re.compile(r"\b(" + "|".join(_MONTHS) + r")\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(20\d\d)\b", re.I)


def _list_sizes(messages: list[ModelMessage]) -> set[int]:
    """Lengths of every list in the tool results (so "you have 3 orders" can be a count of rows)."""
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


def _percent_forms(numbers: list[float]) -> set[float]:
    """How a known number may be written as a percentage: rounded to 0-2 decimals, sign dropped,
    or a fraction times 100 (0.1994 -> 19.94)."""
    forms: set[float] = set()
    for v in numbers:
        for x in (abs(v), abs(v) * 100):
            forms.update(round(x, d) for d in (0, 1, 2))
    return forms


def unsupported_facts(reply: str, messages: list[ModelMessage], prior_replies: str = "") -> list[str]:
    """Facts in the reply that no tool result, user message or earlier reply supports: IDs, $ amounts,
    percentages, counts of things (orders, items...) and dates. Earlier replies count because they
    passed this check when they were written."""
    evidence = _evidence(messages) + "\n" + prior_replies
    numbers = [float(n) for n in _NUMBER_RE.findall(evidence.replace(",", ""))]
    known = {round(v, 2) for v in numbers}
    problems = [i for i in set(_ID_RE.findall(reply)) if i not in evidence]
    for raw in set(_AMOUNT_RE.findall(reply)):
        if round(float(raw.replace(",", "")), 2) not in known:
            problems.append(f"${raw}")
    percents = _percent_forms(numbers)
    for raw in set(_PERCENT_RE.findall(reply)):
        if round(abs(float(raw)), 2) not in percents:
            problems.append(f"{raw}%")
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


# ---- agent ---------------------------------------------------------------------------------


_TOOLS: dict[str, dict[str, Tool]] = {}


def agent_tools(role: str) -> dict[str, Tool]:
    """Every tool the role's agent can be given (before RAG filtering), by name."""
    build_agent(role)
    return _TOOLS[role]


@functools.lru_cache(maxsize=8)
def build_agent(role: str) -> Agent[Deps, str]:
    if role not in SUPPORTED_ROLES:
        raise ValueError(f"no support agent for role {role!r} yet")
    tools = [_make_tool(spec) for spec in tools_for(role)]
    if role == "customer":
        tools.append(_load_more_tool())

    def visible(ctx: RunContext[Deps], tool_def) -> bool:
        """RAG: only the selected packs' tools are sent. Evaluated before every model call, so a
        pack added by load_more appears on the next call. Narrows tools_for(role); never adds."""
        if tool_def.name == "load_more":
            return ctx.deps.packs is not None
        return ctx.deps.packs is None or tool_def.name in packs_lib.tools_in(ctx.deps.packs)

    toolset = FunctionToolset(tools)
    _TOOLS[role] = toolset.tools
    agent = Agent(
        None,
        deps_type=Deps,
        output_type=str,
        toolsets=[toolset.filtered(visible)],
        name=f"{role}_support",
        retries=OUTPUT_RETRIES,
        defer_model_check=True,
    )

    @agent.instructions
    def instructions(ctx: RunContext[Deps]) -> str:
        """The prompt sections for the selected packs (all of them when packs is None)."""
        text = system_prompt(role, ctx.deps.packs if role == "customer" else None)
        if ctx.deps.sql_section and (ctx.deps.packs is None or "analytics" in ctx.deps.packs):
            text += "\n" + ctx.deps.sql_section
        return text

    @agent.output_validator
    def fact_check(ctx: RunContext[Deps], output: str) -> str:
        if getattr(ctx, "partial_output", False):
            return output
        problems = unsupported_facts(output, ctx.messages, ctx.deps.prior_replies)
        if problems:
            ctx.deps.retracted = True
            raise ModelRetry(
                "Your reply mentions values that no tool returned in this conversation: "
                + ", ".join(problems)
                + ". Look them up with a tool, or remove them. Only state facts from tool results.")
        leaked = mentions_internals(output)
        if leaked:
            ctx.deps.retracted = True
            raise ModelRetry(
                f"Your reply mentions how the system works inside ({', '.join(leaked)}). Rewrite it as a "
                "support person would, describing only what you can help with, without those words.")
        return output

    return agent


def configured_model(role: str) -> str | None:
    """Model for a role: ai_settings.model_by_role, else the AI_MODEL environment variable."""
    by_role = store.settings().get("model_by_role") or {}
    return by_role.get(role) or os.environ.get("AI_MODEL") or None


_MODEL_NAMES: dict[int, str] = {}


@functools.lru_cache(maxsize=8)
def get_model(name: str) -> Any:
    """One client per model name, reused for every turn. Building it from the name takes 1-2 s and
    opens a new connection to the provider, so doing it per message was the biggest latency cost."""
    from pydantic_ai.models import infer_model

    if name.startswith("anthropic:") and not os.environ.get("ANTHROPIC_API_KEY") and os.environ.get("CLAUDE_API_KEY"):
        os.environ["ANTHROPIC_API_KEY"] = os.environ["CLAUDE_API_KEY"]   # this project's .env name
    model = infer_model(name)
    _MODEL_NAMES[id(model)] = name   # keep "provider:model" for usage records
    return model


def model_for(name: str) -> Any:
    """The model for a configured name, with the admin's fallback model behind it when one is set
    (ai_settings.fallback_model): if the main model fails (rate limit, quota, outage), the same
    request is retried on the fallback. Only approved models are used as fallbacks."""
    from ai import answer_config

    fallback = store.settings().get("fallback_model")
    if not fallback or fallback == name or fallback not in answer_config.APPROVED_MODELS:
        return get_model(name)
    return _with_fallback(name, fallback)


@functools.lru_cache(maxsize=8)
def _with_fallback(name: str, fallback: str) -> Any:
    from pydantic_ai.models.fallback import FallbackModel

    model = FallbackModel(get_model(name), get_model(fallback))
    _MODEL_NAMES[id(model)] = name
    return model


def _answered_by(result: Any, default: str) -> str:
    """Which model actually wrote the reply (the fallback, if the main one failed)."""
    for m in reversed(result.new_messages() if result is not None else []):
        if isinstance(m, ModelResponse) and m.model_name:
            known = [n for n in _MODEL_NAMES.values() if n.split(":", 1)[-1] == m.model_name]
            return known[0] if known else m.model_name
    return default


def _model_name(model: Any) -> str:
    if isinstance(model, str):
        return model
    return _MODEL_NAMES.get(id(model)) or getattr(model, "model_name", type(model).__name__)


# Extra model settings for every turn (e.g. {"thinking": "minimal"}); see warm_up and docs.
MODEL_SETTINGS: dict[str, Any] = {}


def temperature_for(role: str, model_name: str) -> dict[str, Any]:
    """The admin's temperature for this role, if the model in use accepts it (OpenAI, Claude);
    nothing for models that ignore it (Gemini 3.5+). An out-of-range value is never sent."""
    from ai import answer_config

    value = (store.settings().get("temperature_by_role") or {}).get(role)
    allowed = answer_config.temperature_range(model_name)
    if value is None or allowed is None or not allowed["min"] <= value <= allowed["max"]:
        return {}
    return {"temperature": float(value)}


def warm_up(roles: tuple[str, ...] = SUPPORTED_ROLES) -> None:
    """Pay the one-off costs before the first user does: settings and the agent database pool,
    the agents and their tool schemas, and the model client."""
    store.settings(refresh=True)
    for role in roles:
        build_agent(role)
        name = configured_model(role)
        if name:
            model_for(name)
    if answer_mode("customer") == "hybrid":   # only customers have local answers so far
        from ai import fast_path

        fast_path.warm_up()
    if rag_mode() != "off":
        from ai import router, sql_examples

        router.warm_up()
        sql_examples.warm_up()


def answer_mode(role: str) -> str:
    """'llm' or 'hybrid' for a role, as chosen by the admin (ai/answer_config.py)."""
    from ai import answer_config

    return answer_config.answer_mode(role)


def rag_mode() -> str:
    """off: send everything (no routing). shadow: route and log the decision, but still send
    everything. on: send only the routed packs and sections."""
    mode = store.settings().get("rag_mode", "off")
    return mode if mode in ("off", "shadow", "on") else "off"


def _previous_packs(conversation_id: str) -> frozenset[str] | None:
    """Packs the previous turn used (so follow-ups like "add the first one" keep their tools)."""
    for row in reversed(store.history(conversation_id, 6)):
        if row["role"] == "assistant":
            used = ((row["content"] or {}).get("route") or {}).get("used")
            return frozenset(used) if used else None
    return None


def plan_route(role: str, message: str, conversation_id: str) -> tuple[frozenset[str] | None, str, dict]:
    """(packs to send, SQL example section, route record for storage). Never raises: any failure
    falls back to sending everything."""
    mode = rag_mode()
    if role != "customer" or mode == "off":
        return None, "", {"mode": mode}
    try:
        from ai import embeddings, router, sql_examples

        vector = embeddings.embed([message])[0]
        r = router.route_vector(vector, _previous_packs(conversation_id))
        sql = sql_examples.prompt_section(vector) if r.packs is None or "analytics" in r.packs else ""
        record = {"mode": mode, "packs": None if r.packs is None else sorted(r.packs),
                  "confidence": round(r.confidence, 3), "reason": r.reason}
        if mode == "shadow":
            return None, sql, record
        return r.packs, sql, record
    except Exception:
        log.exception("routing failed; sending everything")
        return None, "", {"mode": mode, "error": "routing failed"}


# ---- limits --------------------------------------------------------------------------------


@dataclass(frozen=True)
class TurnBudget:
    usage_limits: UsageLimits
    max_reply_tokens: int | None


def check_limits(principal: Principal, subject: str) -> TurnBudget:
    """Raise ChatUnavailable if this turn may not start; else the per-turn limits."""
    s = store.settings()
    if not s.get("ai_enabled", True) or not (s.get("roles_enabled") or {}).get(principal.role, True):
        raise ChatUnavailable("The assistant is switched off right now. Please try again later.", 503)
    limits = store.effective_limits(principal)
    if limits.blocked:
        raise ChatUnavailable("The assistant isn't available for this account. Please contact support.", 403)
    if limits.requests_per_minute is not None and store.requests_last_minute(subject) >= limits.requests_per_minute:
        raise ChatUnavailable("You're sending messages very quickly. Please wait a minute and try again.")
    today = store.usage_today(subject)
    if limits.messages_per_day is not None and today["requests"] >= limits.messages_per_day:
        raise ChatUnavailable("You've reached today's limit for the assistant. It resets at midnight.")

    def remaining(cap: int | None, used: int) -> int | None:
        return None if cap is None else max(cap - used, 0)

    left_in = remaining(limits.input_tokens_per_day, today["input_tokens"])
    left_out = remaining(limits.output_tokens_per_day, today["output_tokens"])
    if left_in == 0 or left_out == 0:
        raise ChatUnavailable("You've reached today's limit for the assistant. It resets at midnight.")
    return TurnBudget(
        usage_limits=UsageLimits(request_limit=MAX_MODEL_REQUESTS, tool_calls_limit=limits.max_tool_calls_per_turn,
                                 input_tokens_limit=left_in, output_tokens_limit=left_out),
        max_reply_tokens=limits.max_reply_tokens,
    )


def cost_usd(model: str, input_tokens: int, output_tokens: int, cached_tokens: int = 0) -> float:
    """Dollar cost from ai_settings.model_prices: {model: {"input", "output", "cached_input"}} in $ per
    1M tokens (cached input tokens are part of input_tokens, billed at the cached rate)."""
    prices = (store.settings().get("model_prices") or {}).get(model)
    if not prices:
        return 0.0
    if isinstance(prices, list):   # older format: [input, output]
        prices = {"input": prices[0], "output": prices[1]}
    cached = min(cached_tokens or 0, input_tokens)
    cached_rate = prices.get("cached_input", prices["input"])
    return round(((input_tokens - cached) * prices["input"] + cached * cached_rate
                  + output_tokens * prices["output"]) / 1_000_000, 6)


def _cost(model: str, usage: RunUsage) -> float:
    return cost_usd(model, usage.input_tokens, usage.output_tokens, usage.cache_read_tokens)


# ---- history -------------------------------------------------------------------------------


def _compact(turn: list[ModelMessage], reply: str) -> list[ModelMessage]:
    """An older turn as just the customer's words and the final reply (no tool calls or results)."""
    prompts = [p for m in turn if isinstance(m, ModelRequest) for p in m.parts if isinstance(p, UserPromptPart)]
    return [ModelRequest(parts=prompts[:1]), ModelResponse(parts=[TextPart(content=reply)])] if prompts else []


def _size(messages: list[ModelMessage]) -> int:
    """Rough token count (characters / 4) of what the model reads: text, tool arguments and results
    (not the stored metadata such as timestamps and provider details)."""
    chars = 0
    for msg in messages:
        for part in msg.parts:
            value = getattr(part, "args", None) if isinstance(part, ToolCallPart) else getattr(part, "content", "")
            chars += len(value) if isinstance(value, str) else len(json.dumps(value, default=str))
    return chars // 4


def history_window(role: str) -> tuple[int | None, int | None]:
    """(full turns, question + answer turns) for a role; None = unlimited. Admin setting
    ai_settings.history_window_by_role; the defaults above when the admin hasn't set one."""
    chosen = (store.settings().get("history_window_by_role") or {}).get(role)
    if not isinstance(chosen, dict):
        return FULL_DETAIL_TURNS, QA_TURNS
    return chosen.get("full_turns", FULL_DETAIL_TURNS), chosen.get("qa_turns", QA_TURNS)


def load_history(conversation_id: str, question: str | None = None, questions_before: int | None = None,
                 role: str = "customer") -> list[ModelMessage]:
    """Model context from stored turns (each assistant row holds its whole turn, so a tool call is never
    split from its result):

    - the newest FULL_DETAIL_TURNS turns in full, with large lookup results shortened;
    - the QA_TURNS before them as question + answer (the oldest are left out if the history would
      exceed HISTORY_TOKEN_BUDGET);
    - when older turns exist: their key figures (ai_conversations.memory) and up to two older turns
      related to the new question. Nothing older is re-read in full.
    """
    full_turns, qa_turns = history_window(role)
    window = None if full_turns is None or qa_turns is None else full_turns + qa_turns
    row_limit = ALL_ROWS if window is None else 2 * window + 10   # a little spare for store events
    rows = store.history(conversation_id, row_limit)
    # Group rows into turns: a turn starts at a customer message; store events join the turn they follow.
    turns: list[list[dict]] = []
    for row in rows:
        if row["role"] == "user" or not turns:
            turns.append([row])
        else:
            turns[-1].append(row)
    if turns and turns[0][0]["role"] != "user" and len(rows) >= row_limit:
        turns = turns[1:]   # cut mid-turn by the row limit
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
                    content=f"[Store event, not written by the customer] {content['text']}")]))
        return out

    first_full = 0 if full_turns is None else max(len(turns) - full_turns, 0)
    full_count = len(turns) - first_full
    blocks = [as_messages(t, full=i >= first_full) for i, t in enumerate(turns)]
    # Safety net: very long answers can make even 25 question + answer turns large.
    while len(blocks) > full_count and _size([m for b in blocks for m in b]) > HISTORY_TOKEN_BUDGET:
        blocks.pop(0)
        turns.pop(0)
    messages = [m for b in blocks for m in b]
    if turns and turns[0][0]["sequence_no"] <= 1:
        return messages   # the whole chat is in the window
    if not turns and not (questions_before or store.question_count(conversation_id)):
        return messages   # first question

    # Older turns exist: their key figures, plus the ones related to this question.
    asked = questions_before if questions_before is not None else store.question_count(conversation_id)
    first_turn_in_window = asked - sum(1 for t in turns if t[0]["role"] == "user") + 1
    older: list[ModelMessage] = []
    try:
        facts = memory.facts_block(memory.load_facts(conversation_id), before_turn=first_turn_in_window)
        if facts:
            older.append(ModelRequest(parts=[UserPromptPart(content=facts)]))
        if question:
            older += memory.related_messages(
                memory.related_turns(conversation_id, question,
                                     before_seq=turns[0][0]["sequence_no"] if turns else ALL_ROWS))
    except Exception:   # memory is an extra; never fail the question because of it
        log.exception("could not load the older-turn memory (conversation %s)", conversation_id)
    return older + messages


def transcript(conversation_id: str) -> list[dict]:
    """What the UI shows when a conversation is reopened."""
    out = []
    for row in store.history(conversation_id, 200):
        content = row["content"] or {}
        if row["role"] in ("user", "assistant", "system") and content.get("text"):
            out.append({"message_id": row["message_id"], "role": row["role"], "content": content["text"],
                        "created_at": str(row["created_at"])})
    return out


# ---- one chat turn -------------------------------------------------------------------------


async def chat_turn(principal: Principal, message: str, *, conversation_id: str | None = None,
                    session_id: str | None = None, model: Any = None) -> AsyncIterator[dict]:
    """Run one turn and yield UI events. Raises ChatUnavailable before anything is streamed
    (call `prepare_turn` first to turn that into an HTTP error)."""
    turn = prepare_turn(principal, message, conversation_id=conversation_id, session_id=session_id, model=model)
    async for event in turn.run():
        yield event


@dataclass
class Turn:
    principal: Principal
    message: str
    conversation_id: str
    subject: str
    model: Any
    budget: TurnBudget
    new_conversation: bool
    questions_before: int = 0                           # questions already asked in this chat
    usage: RunUsage = field(default_factory=RunUsage)   # filled in while the turn runs
    tools_called: list[str] = field(default_factory=list)
    route: dict = field(default_factory=dict)           # RAG routing decision for this turn

    def _remember(self, reply: str) -> None:
        """Keep this answer's key figures for later turns (ai/memory.py); never fails the turn."""
        try:
            memory.remember(self.conversation_id, self.questions_before + 1, self.message, reply)
        except Exception:
            log.exception("could not update the conversation memory (conversation %s)", self.conversation_id)

    async def _try_local(self, history: list[ModelMessage]):
        """Hybrid path: answer on our server without the LLM when a simple intent matches clearly.
        Returns an event stream, or None to continue with the LLM."""
        if self.principal.role != "customer" or answer_mode("customer") != "hybrid":
            return None
        from ai import fast_path, local_embeddings

        if not local_embeddings.is_ready():   # not loaded: don't make the customer wait
            # Start loading if nothing is (e.g. the start-up warm-up failed because the database was
            # down); this question goes to the LLM, later ones are answered locally once it's loaded.
            local_embeddings.load_in_background(fast_path.warm_up)
            self.route = {**self.route, "path": "llm", "local_reason": "local model not loaded yet"}
            return None
        previous = next((p.content for m in reversed(history) if isinstance(m, ModelResponse)
                         for p in m.parts if isinstance(p, TextPart)), None)
        try:
            answer, decision = await asyncio.to_thread(
                fast_path.try_answer, self.principal, self.message, self.conversation_id, previous)
        except Exception:
            log.exception("local answer failed; using the LLM")
            return None
        self.route = {**self.route, "path": "local" if answer else "llm", "local_intent": decision.intent,
                      "local_score": round(decision.score, 3), "local_reason": decision.reason}
        if answer is None:
            return None
        return self._local_events(answer)

    async def _unavailable(self, text: str):
        """The provider is paused: say so right away, without calling it (not counted as a message)."""
        conv = self.conversation_id
        yield {"type": "text", "delta": text}
        message_id = await asyncio.to_thread(
            store.append_message, conv, "assistant",
            {"text": text, "model_messages": ModelMessagesTypeAdapter.dump_python(
                [ModelRequest(parts=[UserPromptPart(content=self.message)]), ModelResponse(parts=[TextPart(content=text)])],
                mode="json"), "route": self.route},
            model="unavailable", prompt_version=PROMPT_VERSION, tool_set_version=TOOL_SET_VERSION,
            input_tokens=0, output_tokens=0, cached_tokens=0)
        await asyncio.to_thread(store.record_usage, self.subject, self.principal.role, "unavailable", 0, 0, 0, 0.0,
                                user_id=self.principal.user_id, conversation_id=conv, count_request=False)
        yield {"type": "message", "message_id": message_id}
        yield {"type": "done"}

    async def _local_events(self, answer):
        conv = self.conversation_id
        for i, name in enumerate(answer.tools):
            self.tools_called.append(name)
            label = STEP_LABELS.get(name, name)
            yield {"type": "step", "id": f"local{i}", "label": label, "status": "running"}
            yield {"type": "step", "id": f"local{i}", "label": label, "status": "done"}
        yield {"type": "text", "delta": answer.text}
        turn = [ModelRequest(parts=[UserPromptPart(content=self.message)]),
                ModelResponse(parts=[TextPart(content=answer.text)])]
        message_id = await asyncio.to_thread(
            store.append_message, conv, "assistant",
            {"text": answer.text, "model_messages": ModelMessagesTypeAdapter.dump_python(turn, mode="json"),
             "route": self.route},
            model=LOCAL_MODEL_NAME, prompt_version=PROMPT_VERSION, tool_set_version=TOOL_SET_VERSION,
            input_tokens=0, output_tokens=0, cached_tokens=0)
        # Counted like any message, so daily and per-minute limits still apply.
        await asyncio.to_thread(store.record_usage, self.subject, self.principal.role, LOCAL_MODEL_NAME, 0, 0, 0, 0.0,
                                user_id=self.principal.user_id, conversation_id=conv)
        await asyncio.to_thread(self._remember, answer.text)
        yield {"type": "message", "message_id": message_id}
        yield {"type": "done"}

    async def run(self) -> AsyncIterator[dict]:
        principal, conv = self.principal, self.conversation_id
        if self.new_conversation:
            yield {"type": "conversation", "conversation_id": conv}
        # Enforce the language policy before routing, embedding or calling any model. This
        # applies to both signed-in customers and guests, regardless of answer mode.
        from ai import fast_path
        if fast_path.requests_english(self.message):
            await asyncio.to_thread(store.append_message, conv, "user", {"text": self.message})
            self.route = {"path": "local", "local_intent": "english_only", "local_reason": "non-English message"}
            async for event in self._local_events(
                fast_path.LocalAnswer("english_only", fast_path.ENGLISH_ONLY_REPLY, [])
            ):
                yield event
            return
        model_name = _model_name(self.model)
        # Database work runs in a thread so the stream doesn't block the server's event loop.
        # Routing (an embedding call, ~150 ms) runs alongside the history load.
        history, (packs, sql_section, route), embedding = await asyncio.gather(
            asyncio.to_thread(load_history, conv, self.message, self.questions_before, principal.role),
            asyncio.to_thread(plan_route, principal.role, self.message, conv),
            asyncio.to_thread(memory.question_embedding, self.message))
        await asyncio.to_thread(store.append_message, conv, "user", {"text": self.message}, embedding=embedding)
        self.route = route

        local = await self._try_local(history)
        if local is not None:
            async for event in local:
                yield event
            return

        agent = build_agent(principal.role)
        hybrid = principal.role == "customer" and answer_mode("customer") == "hybrid"
        paused, why = resilience.is_open()
        if paused:   # the provider failed repeatedly: answer now instead of every customer waiting to fail
            self.route = {**self.route, "llm_skipped": why}
            async for event in self._unavailable(resilience.message(why, hybrid=hybrid)):
                yield event
            return
        deps = Deps(principal=principal, conversation_id=conv, packs=packs, sql_section=sql_section,
                    prior_replies="\n".join(p.content for m in history if isinstance(m, ModelResponse)
                                             for p in m.parts if isinstance(p, TextPart)))
        usage = self.usage
        settings = {"timeout": REQUEST_TIMEOUT_S, **temperature_for(principal.role, model_name), **MODEL_SETTINGS,
                    **({"max_tokens": self.budget.max_reply_tokens} if self.budget.max_reply_tokens else {})}
        reply, result, streamed, failure = "", None, False, None
        tool_since_text = False   # Claude writes "I'll check…" before a lookup: replace it with the answer
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
                            if ev.part.tool_name == "load_more":   # internal; not a step the customer sees
                                continue
                            yield {"type": "step", "id": ev.part.tool_call_id,
                                   "label": STEP_LABELS.get(ev.part.tool_name, ev.part.tool_name), "status": "running"}
                        elif isinstance(ev, FunctionToolResultEvent):
                            if ev.part.tool_name == "load_more":
                                continue
                            call_id = ev.part.tool_call_id
                            content = getattr(ev.part, "content", None)
                            failed = not isinstance(ev.part, ToolReturnPart) or (isinstance(content, dict) and "error" in content)
                            yield {"type": "step", "id": call_id,
                                   "label": STEP_LABELS.get(ev.part.tool_name, ev.part.tool_name),
                                   "status": "error" if failed else "done"}
                            for side in deps.side_events.pop(call_id, []):
                                yield side
                        elif isinstance(ev, AgentRunResultEvent):
                            result = ev.result
                reply = result.output if result is not None else ""
                failure = None
                break
            except UsageLimitExceeded:
                reply = ("That request needed more work than I can do in one go. "
                         "Could you narrow it down (for example, one order at a time)?")
                break
            except UnexpectedModelBehavior:
                log.warning("model failed the fact check or misbehaved (conversation %s)", conv)
                reply = FALLBACK_REPLY
                break
            except Exception as exc:
                failure = resilience.classify(exc)
                resilience.record_failure(failure, exc)
                if failure == "unknown":
                    log.exception("chat turn failed (conversation %s)", conv)
                else:
                    log.warning("LLM %s (conversation %s, attempt %d): %s", failure, conv, attempt + 1, str(exc)[:200])
                # One quick retry for short-lived limits, only if nothing reached the customer and no
                # tool ran yet (a retry after "add to cart" could add the item twice).
                if (attempt == 0 and resilience.retryable(failure) and not streamed
                        and len(self.tools_called) == tools_before and not resilience.is_open()[0]):
                    await asyncio.sleep(min(resilience.retry_after_s(exc) or 3, 8))
                    deps.side_events.clear()
                    continue
                reply = resilience.message(failure, hybrid=hybrid)
                break
        if result is not None:
            resilience.record_success()

        if result is None and reply:
            # The streamed text (if any) wasn't accepted: replace it with the safe reply.
            if streamed:
                yield {"type": "reset"}
            yield {"type": "text", "delta": reply}

        model_messages = (ModelMessagesTypeAdapter.dump_python(result.new_messages(), mode="json")
                          if result is not None else None)
        message_id = await asyncio.to_thread(
            store.append_message, conv, "assistant",
            {"text": reply, "model_messages": model_messages,
             "route": {**self.route, "loaded": deps.loaded,
                       "used": sorted(deps.packs) if deps.packs is not None else sorted(packs_lib.ALL_PACKS)}},
            model=_answered_by(result, model_name),
            prompt_version=PROMPT_VERSION, tool_set_version=TOOL_SET_VERSION,
            input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
            cached_tokens=usage.cache_read_tokens)
        answered_by = _answered_by(result, model_name)
        # Failed messages record their tokens and cost but don't count toward the daily message limit.
        await asyncio.to_thread(
            store.record_usage, self.subject, principal.role, answered_by, usage.input_tokens, usage.output_tokens,
            usage.cache_read_tokens, _cost(answered_by, usage), user_id=principal.user_id, conversation_id=conv,
            count_request=result is not None)
        if result is not None:
            await asyncio.to_thread(self._remember, reply)
        yield {"type": "message", "message_id": message_id}
        yield {"type": "done"}


def prepare_turn(principal: Principal, message: str, *, conversation_id: str | None = None,
                 session_id: str | None = None, model: Any = None) -> Turn:
    """Checks that must pass before streaming starts (so they can become HTTP errors)."""
    if principal.role not in SUPPORTED_ROLES:
        raise ChatUnavailable("This assistant isn't available for your account type.", 403)
    model = model or configured_model(principal.role)
    if model is None:
        raise ChatUnavailable("not_configured", 503)
    if isinstance(model, str):
        model = model_for(model)
    subject = store.subject_for(principal, session_id)
    budget = check_limits(principal, subject)
    new, asked = False, 0
    if conversation_id:
        if not store.get_conversation(principal, conversation_id, session_id):
            raise ChatUnavailable("Conversation not found.", 404)
        asked = store.question_count(conversation_id)
        limit = max_questions_per_chat(principal.role)
        if limit is not None and asked >= limit:
            # Say why (the number), so it isn't mistaken for an error.
            raise ChatUnavailable(f"Each chat can have up to {limit} question{'s' if limit != 1 else ''}, and this "
                                  "one has reached it. Please start a new chat to continue.", 409)
    else:
        conversation_id = store.create_conversation(principal, session_id, title=message.strip()[:80])
        new = True
    return Turn(principal, message, conversation_id, subject, model, budget, new, questions_before=asked)


def max_questions_per_chat(role: str) -> int | None:
    """Admin setting (ai_settings.max_questions_per_chat); None = unlimited."""
    value = (store.settings().get("max_questions_per_chat") or {}).get(role)
    return int(value) if isinstance(value, (int, float)) and value > 0 else None


# ---- confirm cards and undo (called by the API when the user clicks) ----------------------


def confirm_action(principal: Principal, action_id: str, approve: bool) -> dict:
    """Run (or reject) a pending action the user owns. Each action runs at most once."""
    action = store.claim_pending_action(principal, action_id, approve)
    if action is None:
        raise ChatUnavailable("This request has expired or was already handled.", 409)
    conv = action["conversation_id"]
    if not approve:
        store.append_message(conv, "system", {"text": f"The customer declined the proposed {action['tool_name']}."})
        return {"status": "rejected"}
    try:
        get_tool(principal, action["tool_name"])  # the role may have lost the tool since
        with in_conversation(conv):
            result = call_tool(principal, action["tool_name"], action["args"], confirmed=True)
        ok, status = True, "ok"
    except ToolError as exc:
        result, ok, status = {"error": str(exc)}, False, _status_for(exc)
    except Exception:
        log.exception("confirmed action %s failed", action_id)
        result, ok, status = {"error": "The change could not be made. Please try again later."}, False, "error"
    store.finish_pending_action(action_id, ok, result)
    store.record_tool_call(conv, action["tool_name"], ToolKind.WRITE_CONFIRM.value, action["args"], status,
                           confirmed_by_user_id=principal.user_id)
    outcome = "succeeded" if ok else "failed"
    store.append_message(conv, "system", {
        "text": f"The customer confirmed {action['tool_name']}; it {outcome}. Result: {json.dumps(result, default=str)}"})
    return {"status": "executed" if ok else "failed", "result": result}


def undo(principal: Principal, tool_name: str, args: dict) -> dict:
    """Undo a cart change. Only undo-type tools of the caller's own role can be used, and they act on
    the caller's own data, so this can't reach anything the user couldn't change in the store."""
    spec = get_tool(principal, tool_name)
    if spec.kind is not ToolKind.WRITE_UNDO:
        raise ToolError("That can't be undone here.")
    result = call_tool(principal, tool_name, args)
    if isinstance(result, dict):
        result.pop("undo", None)
    return result

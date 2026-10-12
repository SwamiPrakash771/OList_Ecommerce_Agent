"""The support agent engine: one chat turn, from the user's message to a streamed, checked reply.

Built on Pydantic AI. The security rules stay in ai/security.py; this module only connects
them to the model:

- The agent gets exactly `tools_for(role)`. Each tool call goes through `call_tool` with the
  server-side `Principal`, so the model can't pick whose data it reads.
- Confirm tools never run here. The model's call is turned into a server-built confirm card
  (a pending action); the change happens only when the user clicks Confirm (`confirm_action`).
- Before each turn: kill switches, block flag, rate limit, daily messages and token budgets.
  During the turn: request, tool-call and token limits.
- Fact check: every order/product/ticket ID and every $ amount in the final reply must appear
  in a tool result (or the user's own messages) from this conversation, or the model must retry.
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
    ModelMessage, ModelMessagesTypeAdapter, ModelRequest, ModelResponse, ToolReturnPart, UserPromptPart,
)
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai.usage import RunUsage, UsageLimits

import ai.tools  # noqa: F401  (registers the tools)
from ai import packs as packs_lib
from ai import store
from ai.prompts import PROMPT_VERSION, system_prompt
from ai.security import (
    InvalidArguments, NotFound, Principal, ToolError, ToolKind, ToolSpec, call_tool, get_tool,
    in_conversation, preview_tool, tools_for,
)

log = logging.getLogger("ai.agent")

SUPPORTED_ROLES = ("guest", "customer")
HISTORY_ROWS = 40          # stored messages loaded as context for the next turn
FULL_DETAIL_TURNS = 2      # newest turns resent with their tool calls; older ones as question + answer
MAX_MODEL_REQUESTS = 10    # model calls per turn (tool loops included)
OUTPUT_RETRIES = 2         # fact-check retries before giving up
TOOL_SET_VERSION = "support-tools-v1"
LOCAL_MODEL_NAME = "local:fast-path"   # recorded on messages answered without the LLM

FALLBACK_REPLY = ("Sorry, I couldn't put together a reliable answer just now. "
                  "Please try again, or ask me to connect you with a person.")

STEP_LABELS = {
    "search_products": "Searching products",
    "product_details": "Looking up the product",
    "list_categories": "Listing categories",
    "my_orders": "Looking up your orders",
    "query_my_data": "Analysing your data",
    "track_order": "Tracking the order",
    "get_return_status": "Checking the return",
    "get_refund_status": "Checking the refund",
    "my_cart": "Checking your cart",
    "cart_add": "Adding to your cart",
    "cart_set_quantity": "Updating your cart",
    "get_address": "Looking up your address",
    "update_address": "Preparing the address change",
    "request_return": "Preparing the return request",
    "open_ticket": "Preparing a support ticket",
    "transfer_to_human": "Preparing the hand-off to a person",
    "my_tickets": "Checking your tickets",
}
CART_TOOLS = {"cart_add", "cart_set_quantity"}


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
    from ai.tools.query_tools import VIRTUAL_TABLES

    found = {m.group(0).lower() for m in _INTERNAL_RE.finditer(text)}
    found |= {name for name in [*REGISTRY, *VIRTUAL_TABLES, "load_more"] if re.search(rf"\b{re.escape(name)}\b", text)
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


def unsupported_facts(reply: str, messages: list[ModelMessage], prior_replies: str = "") -> list[str]:
    """IDs and $ amounts in the reply that no tool result, user message or earlier reply contains.
    Earlier replies count because they passed this check when they were written."""
    evidence = _evidence(messages) + "\n" + prior_replies
    problems = [i for i in set(_ID_RE.findall(reply)) if i not in evidence]
    known = {round(float(n), 2) for n in _NUMBER_RE.findall(evidence.replace(",", ""))}
    for raw in set(_AMOUNT_RE.findall(reply)):
        if round(float(raw.replace(",", "")), 2) not in known:
            problems.append(f"${raw}")
    return sorted(problems)


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

    model = infer_model(name)
    _MODEL_NAMES[id(model)] = name   # keep "provider:model" for usage records
    return model


def _model_name(model: Any) -> str:
    if isinstance(model, str):
        return model
    return _MODEL_NAMES.get(id(model)) or getattr(model, "model_name", type(model).__name__)


# Extra model settings for every turn (e.g. {"thinking": "minimal"}); see warm_up and docs.
MODEL_SETTINGS: dict[str, Any] = {}


def warm_up(roles: tuple[str, ...] = SUPPORTED_ROLES) -> None:
    """Pay the one-off costs before the first user does: settings and the agent database pool,
    the agents and their tool schemas, and the model client."""
    store.settings(refresh=True)
    for role in roles:
        build_agent(role)
        name = configured_model(role)
        if name:
            get_model(name)
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


def load_history(conversation_id: str) -> list[ModelMessage]:
    """Model context from stored turns. Each assistant row holds its whole turn (prompt, tool calls
    and returns, reply), so trimming old rows never splits a tool call from its result. Only the
    newest FULL_DETAIL_TURNS keep their tool results; older turns are resent as question + answer,
    which keeps long conversations from growing the input on every request."""
    rows = store.history(conversation_id, HISTORY_ROWS)
    full_from = [i for i, r in enumerate(rows) if r["role"] == "assistant"][-FULL_DETAIL_TURNS:]
    first_full = full_from[0] if full_from else len(rows)
    messages: list[ModelMessage] = []
    for i, row in enumerate(rows):
        content = row["content"] or {}
        if row["role"] == "assistant" and content.get("model_messages"):
            turn = ModelMessagesTypeAdapter.validate_python(content["model_messages"])
            messages.extend(turn if i >= first_full else _compact(turn, content.get("text") or ""))
        elif row["role"] == "system" and content.get("text"):
            messages.append(ModelRequest(parts=[UserPromptPart(
                content=f"[Store event, not written by the customer] {content['text']}")]))
    return messages


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
    usage: RunUsage = field(default_factory=RunUsage)   # filled in while the turn runs
    tools_called: list[str] = field(default_factory=list)
    route: dict = field(default_factory=dict)           # RAG routing decision for this turn

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
        yield {"type": "message", "message_id": message_id}
        yield {"type": "done"}

    async def run(self) -> AsyncIterator[dict]:
        principal, conv = self.principal, self.conversation_id
        if self.new_conversation:
            yield {"type": "conversation", "conversation_id": conv}
        model_name = _model_name(self.model)
        # Database work runs in a thread so the stream doesn't block the server's event loop.
        # Routing (an embedding call, ~150 ms) runs alongside the history load.
        history, (packs, sql_section, route) = await asyncio.gather(
            asyncio.to_thread(load_history, conv),
            asyncio.to_thread(plan_route, principal.role, self.message, conv))
        await asyncio.to_thread(store.append_message, conv, "user", {"text": self.message})
        self.route = route

        local = await self._try_local(history)
        if local is not None:
            async for event in local:
                yield event
            return

        agent = build_agent(principal.role)
        deps = Deps(principal=principal, conversation_id=conv, packs=packs, sql_section=sql_section,
                    prior_replies="\n".join(p.content for m in history if isinstance(m, ModelResponse)
                                             for p in m.parts if isinstance(p, TextPart)))
        usage = self.usage
        settings = {**MODEL_SETTINGS,
                    **({"max_tokens": self.budget.max_reply_tokens} if self.budget.max_reply_tokens else {})} or None
        reply, result, streamed = "", None, False
        try:
            async with agent.run_stream_events(
                self.message, deps=deps, model=self.model, message_history=history,
                usage_limits=self.budget.usage_limits, usage=usage, model_settings=settings,
            ) as stream:
                async for ev in stream:
                    if isinstance(ev, PartStartEvent) and isinstance(ev.part, TextPart):
                        if deps.retracted:
                            deps.retracted = False
                            yield {"type": "reset"}
                        if ev.part.content:
                            streamed = True
                            yield {"type": "text", "delta": ev.part.content}
                    elif isinstance(ev, PartDeltaEvent) and isinstance(ev.delta, TextPartDelta):
                        if ev.delta.content_delta:
                            streamed = True
                            yield {"type": "text", "delta": ev.delta.content_delta}
                    elif isinstance(ev, FunctionToolCallEvent):
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
        except UsageLimitExceeded:
            reply = ("That request needed more work than I can do in one go. "
                     "Could you narrow it down (for example, one order at a time)?")
        except UnexpectedModelBehavior:
            log.warning("model failed the fact check or misbehaved (conversation %s)", conv)
            reply = FALLBACK_REPLY
        except Exception:
            log.exception("chat turn failed (conversation %s)", conv)
            yield {"type": "error", "message": "The assistant ran into a problem. Please try again."}
            reply = ""

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
            model=model_name,
            prompt_version=PROMPT_VERSION, tool_set_version=TOOL_SET_VERSION,
            input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
            cached_tokens=usage.cache_read_tokens)
        await asyncio.to_thread(
            store.record_usage, self.subject, principal.role, model_name, usage.input_tokens, usage.output_tokens,
            usage.cache_read_tokens, _cost(model_name, usage), user_id=principal.user_id, conversation_id=conv)
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
        model = get_model(model)
    subject = store.subject_for(principal, session_id)
    budget = check_limits(principal, subject)
    new = False
    if conversation_id:
        if not store.get_conversation(principal, conversation_id, session_id):
            raise ChatUnavailable("Conversation not found.", 404)
    else:
        conversation_id = store.create_conversation(principal, session_id, title=message.strip()[:80])
        new = True
    return Turn(principal, message, conversation_id, subject, model, budget, new)


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

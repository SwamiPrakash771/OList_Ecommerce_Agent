"""Security for the admin agent: its own tool registry, checks, masking and lookup audit.

Separate from the customer and seller agents. Admin tools are registered here, only the admin agent
is given them, and they run on the admin agent's own MySQL user (ai/admin/db_user.py). Only the
signed-in identity (`Principal`, built by the app from the session) and the error types are shared.

Rules:
1. Only role "admin" may call these tools; anyone else gets "not available".
2. Tools take the signed-in `principal` first; the admin's identity can't be an argument.
3. **Read-only** (decision 3): a tool may not declare writes.
4. Each tool declares the tables it reads. Its normal columns must be admin-visible and not masked;
   masked columns it needs for totals (payment values, buyer state...) are declared separately in
   `aggregates`, and must be in db_user.AGGREGATE_ONLY. Tables never available (addresses, logins)
   can't be declared at all.
5. People are never shown by their real ID: `mask_id` gives a stable pseudonym (decision 1).
6. Looking at one customer, order or seller is written to ai_admin_access_log (decision 6).
"""

from __future__ import annotations

import contextvars
import hashlib
import inspect
import os
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Iterable

from pydantic import ValidationError, validate_call

import db
from ai.admin import db_user
from ai.security import (  # shared: the app's identity and the errors every agent may show
    InvalidArguments, NotFound, Principal, ToolError, ToolKind,
)

ROLE = "admin"
# The admin's own identity comes from the session only. Unlike the customer and seller agents, a
# seller or order ID is a normal lookup argument here (admins may look at any seller), so only the
# arguments that would let the model act as another admin are refused.
ADMIN_IDENTITY_PARAMS = frozenset({"user_id", "admin_user_id", "username", "role", "owner", "principal_id"})
__all__ = ["ROLE", "AdminTool", "ADMIN_REGISTRY", "admin_tool", "admin_tools", "get_admin_tool", "call_admin_tool",
           "mask_id", "log_lookup", "in_admin_conversation", "NotFound", "ToolError", "InvalidArguments", "Principal"]


@dataclass(frozen=True)
class AdminTool:
    name: str
    description: str
    func: Callable[..., Any]
    reads: tuple[str, ...]
    aggregates: tuple[str, ...]


ADMIN_REGISTRY: dict[str, AdminTool] = {}


class AdminToolDefinitionError(Exception):
    """Raised at import time when an admin tool breaks a security rule."""


def _check(func: Callable, name: str, kind: ToolKind, reads: tuple[str, ...], aggregates: tuple[str, ...],
           writes: tuple[str, ...]) -> None:
    params = list(inspect.signature(func).parameters.values())
    if not params or params[0].name != "principal":
        raise AdminToolDefinitionError(f"{name}: first parameter must be 'principal'")
    bad = [p.name for p in params[1:] if p.name in ADMIN_IDENTITY_PARAMS]
    if bad:
        raise AdminToolDefinitionError(f"{name}: identity must not be a tool argument: {bad}")
    if not (func.__doc__ or "").strip():
        raise AdminToolDefinitionError(f"{name}: needs a docstring (it becomes the tool description)")
    if name in ADMIN_REGISTRY:
        raise AdminToolDefinitionError(f"{name}: an admin tool with this name already exists")
    if kind is not ToolKind.READ or writes:
        raise AdminToolDefinitionError(f"{name}: the admin agent is read-only")
    readable, aggregate = db_user.tool_columns(), db_user.aggregate_columns()
    for table in reads:
        if table in db_user.NEVER_TABLES or table not in readable:
            raise AdminToolDefinitionError(f"{name}: the admin agent may not read '{table}'")
    for ref in aggregates:   # "payments.payment_value"
        table, _, column = ref.partition(".")
        if column not in aggregate.get(table, []):
            raise AdminToolDefinitionError(f"{name}: '{ref}' is not an aggregate-only column")


def admin_tool(*, reads: Iterable[str] = (), aggregates: Iterable[str] = (), kind: ToolKind = ToolKind.READ,
               writes: Iterable[str] = (), name: str | None = None):
    """Register an admin-agent tool after checking it against the rules above."""

    def register(func: Callable) -> Callable:
        tool_name = name or func.__name__
        spec_reads, spec_aggs = tuple(reads), tuple(aggregates)
        _check(func, tool_name, kind, spec_reads, spec_aggs, tuple(writes))
        ADMIN_REGISTRY[tool_name] = AdminTool(
            name=tool_name, description=inspect.cleandoc(func.__doc__),
            func=validate_call(func, config={"arbitrary_types_allowed": True, "strict": False}),
            reads=spec_reads, aggregates=spec_aggs)
        return func

    return register


def admin_tools() -> list[AdminTool]:
    return list(ADMIN_REGISTRY.values())


def get_admin_tool(principal: Principal, name: str) -> AdminTool:
    spec = ADMIN_REGISTRY.get(name)
    if spec is None or principal.role != ROLE or not principal.user_id:
        raise ToolError(f"Tool '{name}' is not available.")
    return spec


def call_admin_tool(principal: Principal, name: str, args: dict[str, Any] | None = None) -> Any:
    """Run an admin tool: role check, argument validation, the admin agent's database user."""
    spec = get_admin_tool(principal, name)
    args = dict(args or {})
    smuggled = ADMIN_IDENTITY_PARAMS & set(args)
    if smuggled:
        raise InvalidArguments(f"Identity can't be passed to tools: {sorted(smuggled)}")
    try:
        with db.as_admin_agent():
            return spec.func(principal, **args)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        raise InvalidArguments(f"Invalid arguments for '{name}': {problems}") from None


# ---- masking -------------------------------------------------------------------------------------

def _salt() -> bytes:
    return (os.environ.get("ADMIN_MASK_SALT") or os.environ.get("JWT_SECRET") or "olist-admin").encode()


def mask_id(kind: str, value: str | None) -> str | None:
    """A stable pseudonym for a person (e.g. "customer-7f3a91c2"): the same customer always gets the
    same label in admin answers, but the real ID can't be read back from it."""
    if not value:
        return None
    return f"{kind}-{hashlib.sha256(_salt() + kind.encode() + value.encode()).hexdigest()[:8]}"


# ---- lookup audit --------------------------------------------------------------------------------

_conversation: contextvars.ContextVar[str | None] = contextvars.ContextVar("admin_conversation", default=None)


@contextmanager
def in_admin_conversation(conversation_id: str | None):
    token = _conversation.set(conversation_id)
    try:
        yield
    finally:
        _conversation.reset(token)


def log_lookup(principal: Principal, what: str, label: str) -> None:
    """Record that the admin assistant looked at one record (decision 6), in ai_admin_access_log."""
    conversation = _conversation.get()
    if not conversation:
        return
    db.execute(
        "INSERT INTO ai_admin_access_log (admin_user_id, conversation_id, action, reason, created_at) "
        "VALUES (%s, %s, 'view', %s, %s)",
        (principal.user_id, conversation, f"admin assistant looked up {what} {label}"[:255],
         datetime.now().replace(microsecond=0)),
    )

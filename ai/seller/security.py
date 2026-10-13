"""Security for the seller agent: its own tool registry, checks and database user.

Separate from the customer agent (ai/security.py): seller tools are registered here, only the
seller agent is given them, and they run on the seller agent's own MySQL user
(ai/seller/db_user.py, column-level grants). Only the signed-in identity (`Principal`, built by
the app from the session) and the error types are shared, because they come from the app's login.

Rules:
1. The seller comes from the session (`principal.seller_id`), never from the model. Tools take
   `principal` first, and arguments that name an identity (seller_id, customer_id, ...) are
   refused at registration and at call time.
2. Only role "seller" may call these tools; any other principal gets "not available".
3. Each tool declares the tables it reads and writes; reads must be granted to the seller
   database user (catalog seller rules), writes must be in its WRITE_GRANTS.
4. Tools run on the seller database user, so MySQL refuses anything outside its grants.
5. Changes need the seller's confirmation (WRITE_CONFIRM + a preview built from database values).
6. Another seller's records fail with the same "not found" as missing records.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from pydantic import ValidationError, validate_call

import db
from ai.security import (  # shared: the app's identity and the errors every agent may show
    IDENTITY_PARAMS, ConfirmationRequired, InvalidArguments, NotFound, Principal, ToolError, ToolKind,
)
from ai.seller import db_user

ROLE = "seller"
__all__ = ["ROLE", "SellerTool", "SELLER_REGISTRY", "seller_tool", "seller_tools", "get_seller_tool",
           "call_seller_tool", "preview_seller_tool", "NotFound", "ToolError", "InvalidArguments", "Principal"]


@dataclass(frozen=True)
class SellerTool:
    name: str
    description: str
    func: Callable[..., Any]
    kind: ToolKind
    reads: tuple[str, ...]
    writes: tuple[str, ...]
    preview: Callable[..., dict] | None = None


SELLER_REGISTRY: dict[str, SellerTool] = {}


class SellerToolDefinitionError(Exception):
    """Raised at import time when a seller tool breaks a security rule."""


def _check(func: Callable, name: str, kind: ToolKind, reads: tuple[str, ...], writes: tuple[str, ...],
           preview: Callable | None) -> None:
    params = list(inspect.signature(func).parameters.values())
    if not params or params[0].name != "principal":
        raise SellerToolDefinitionError(f"{name}: first parameter must be 'principal'")
    bad = [p.name for p in params[1:] if p.name in IDENTITY_PARAMS]
    if bad:
        raise SellerToolDefinitionError(f"{name}: identity must not be a tool argument: {bad}")
    if not (func.__doc__ or "").strip():
        raise SellerToolDefinitionError(f"{name}: needs a docstring (it becomes the tool description)")
    if name in SELLER_REGISTRY:
        raise SellerToolDefinitionError(f"{name}: a seller tool with this name already exists")
    readable = db_user.readable_columns()
    for table in reads:
        if table not in readable:
            raise SellerToolDefinitionError(f"{name}: the seller agent may not read '{table}'")
    for table in writes:
        if table not in db_user.WRITE_GRANTS:
            raise SellerToolDefinitionError(f"{name}: the seller database user may not write '{table}'")
    if kind is ToolKind.READ and writes:
        raise SellerToolDefinitionError(f"{name}: read tools must not write")
    if kind is not ToolKind.READ and not writes:
        raise SellerToolDefinitionError(f"{name}: write tools must declare what they write")
    if kind is ToolKind.WRITE_CONFIRM and preview is None:
        raise SellerToolDefinitionError(f"{name}: confirm tools need a preview (the server-built confirm card)")
    if preview is not None and list(inspect.signature(preview).parameters) != [p.name for p in params]:
        raise SellerToolDefinitionError(f"{name}: preview must take exactly the tool's parameters")


def seller_tool(*, kind: ToolKind = ToolKind.READ, reads: Iterable[str] = (), writes: Iterable[str] = (),
                name: str | None = None, preview: Callable[..., dict] | None = None):
    """Register a seller-agent tool after checking it against the rules above."""

    def register(func: Callable) -> Callable:
        tool_name = name or func.__name__
        spec_reads, spec_writes = tuple(reads), tuple(writes)
        _check(func, tool_name, kind, spec_reads, spec_writes, preview)
        SELLER_REGISTRY[tool_name] = SellerTool(
            name=tool_name,
            description=inspect.cleandoc(func.__doc__),
            func=validate_call(func, config={"arbitrary_types_allowed": True, "strict": False}),
            kind=kind,
            reads=spec_reads,
            writes=spec_writes,
            preview=validate_call(preview, config={"arbitrary_types_allowed": True}) if preview else None,
        )
        return func

    return register


def seller_tools() -> list[SellerTool]:
    """The only tools the seller agent is given."""
    return list(SELLER_REGISTRY.values())


def get_seller_tool(principal: Principal, name: str) -> SellerTool:
    spec = SELLER_REGISTRY.get(name)
    if spec is None or principal.role != ROLE or not principal.seller_id:
        raise ToolError(f"Tool '{name}' is not available.")   # same answer for missing and not allowed
    return spec


def _run(fn: Callable, principal: Principal, name: str, args: dict[str, Any] | None) -> Any:
    args = dict(args or {})
    smuggled = IDENTITY_PARAMS & set(args)
    if smuggled:
        raise InvalidArguments(f"Identity can't be passed to tools: {sorted(smuggled)}")
    try:
        with db.as_seller_agent():
            return fn(principal, **args)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        raise InvalidArguments(f"Invalid arguments for '{name}': {problems}") from None


def call_seller_tool(principal: Principal, name: str, args: dict[str, Any] | None = None, *,
                     confirmed: bool = False) -> Any:
    """Run a seller tool: role check, confirmation check, argument validation, seller database user."""
    spec = get_seller_tool(principal, name)
    if spec.kind is ToolKind.WRITE_CONFIRM and not confirmed:
        raise ConfirmationRequired(name)
    return _run(spec.func, principal, name, args)


def preview_seller_tool(principal: Principal, name: str, args: dict[str, Any] | None = None) -> dict:
    """The confirm card for a seller change (validates and checks ownership; no writes)."""
    spec = get_seller_tool(principal, name)
    if spec.kind is not ToolKind.WRITE_CONFIRM or spec.preview is None:
        raise ToolError(f"'{name}' does not need confirmation.")
    return _run(spec.preview, principal, name, args)

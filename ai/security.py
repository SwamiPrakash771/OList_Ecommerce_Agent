"""Security foundation for the AI agents: who is asking, and which tools they may use.

Rules enforced here (see docs/AI_PLAN.md, section 4):

1. Identity comes from the session, never from the model. A `Principal` is built from
   the login token on the server. Every tool receives it as its first argument, and the
   registry refuses tools that take identity values (customer_id, seller_id, ...) as
   model-supplied arguments.
2. One tool list per role. A tool declares the roles that may use it; `tools_for(role)`
   is what an agent is given, and `call_tool` re-checks the role on every call.
3. Tools declare the tables they read and write. At definition time these are checked
   against the data catalog (ai/metadata.yaml) and the agent's database grants
   (ai/db_user.py): a tool can't be registered for a role that may not see its tables.
4. Tools run on the least-privilege agent database user (`db.as_agent()`), so even a
   buggy query is refused by MySQL if it reaches outside the grants.
5. Risky actions need confirmation: WRITE_CONFIRM tools only run with `confirmed=True`,
   which the API sets after the user clicks Confirm (the pending-action flow). Each such
   tool has a `preview` that builds the confirm card from database values, without writing.
6. Lookups of someone else's records fail with the same "not found" as missing records,
   so the agent can't reveal that another user's order exists.
"""

from __future__ import annotations

import contextvars
import inspect
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Iterable

from pydantic import ValidationError, validate_call

import db
from ai import metadata
from ai.db_user import AI_TABLE_GRANTS, WRITE_GRANTS

ROLES = ("guest", "customer", "seller", "admin")

# Argument names that would let the model choose whose data a tool touches.
IDENTITY_PARAMS = frozenset({
    "customer_id", "customer_unique_id", "seller_id", "user_id", "username", "role", "owner", "principal_id",
})


# ---- identity -------------------------------------------------------------------


@dataclass(frozen=True)
class Principal:
    """The signed-in user, as established by the server from the session cookie."""

    role: str
    user_id: int | None = None
    username: str | None = None
    customer_unique_id: str | None = None
    seller_id: str | None = None

    def __post_init__(self):
        if self.role not in ROLES:
            raise ValueError(f"unknown role {self.role!r}")
        if self.role == "customer" and not self.customer_unique_id:
            raise ValueError("a customer principal needs customer_unique_id")
        if self.role == "seller" and not self.seller_id:
            raise ValueError("a seller principal needs seller_id")

    @classmethod
    def guest(cls) -> Principal:
        return cls(role="guest")

    @classmethod
    def from_claims(cls, claims: dict | None) -> Principal:
        """Build from decoded JWT claims (api.main.current_user); None means a guest."""
        if not claims:
            return cls.guest()
        role = claims["role"]
        return cls(
            role=role,
            user_id=claims.get("uid"),
            username=claims.get("sub"),
            customer_unique_id=claims.get("cid") if role == "customer" else None,
            seller_id=claims.get("sid") if role == "seller" else None,
        )


# ---- errors the agent may see --------------------------------------------------


class ToolError(Exception):
    """A failure whose message is safe to show the model and the user."""


class NotFound(ToolError):
    """Missing OR not yours: deliberately indistinguishable."""

    def __init__(self, what: str = "record"):
        super().__init__(f"No {what} found with that ID for your account.")


class ConfirmationRequired(ToolError):
    def __init__(self, tool: str):
        super().__init__(f"'{tool}' changes data and must be confirmed by the user first.")


class InvalidArguments(ToolError):
    pass


# ---- tool registry ----------------------------------------------------------------


class ToolKind(str, Enum):
    READ = "read"                    # no changes
    WRITE_UNDO = "write_undo"        # low-risk change, applied immediately with an undo option
    WRITE_CONFIRM = "write_confirm"  # change that needs the user's explicit confirmation


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    func: Callable[..., Any]
    roles: frozenset[str]
    kind: ToolKind
    reads: tuple[str, ...]
    writes: tuple[str, ...]
    preview: Callable[..., dict] | None = None
    joins: tuple[str, ...] = ()


REGISTRY: dict[str, ToolSpec] = {}


class ToolDefinitionError(Exception):
    """Raised at import time when a tool breaks a security rule."""


def _check_definition(func: Callable, name: str, roles: frozenset[str], kind: ToolKind,
                      reads: tuple[str, ...], writes: tuple[str, ...]) -> None:
    params = list(inspect.signature(func).parameters.values())
    if not params or params[0].name != "principal":
        raise ToolDefinitionError(f"{name}: first parameter must be 'principal' (identity comes from the session)")
    bad = [p.name for p in params[1:] if p.name in IDENTITY_PARAMS]
    if bad:
        raise ToolDefinitionError(f"{name}: identity must not be a tool argument: {bad}")
    missing_doc = not (func.__doc__ or "").strip()
    if missing_doc:
        raise ToolDefinitionError(f"{name}: needs a docstring (it becomes the tool description)")
    if not roles or not roles <= set(ROLES):
        raise ToolDefinitionError(f"{name}: invalid roles {sorted(roles)}")
    if name in REGISTRY:
        raise ToolDefinitionError(f"{name}: a tool with this name already exists")

    catalog = metadata.load()["tables"]
    for table in reads + writes:
        if table not in catalog:
            raise ToolDefinitionError(f"{name}: table '{table}' is not in the data catalog")
        if catalog[table].get("ai_access") == "never":
            raise ToolDefinitionError(f"{name}: table '{table}' is never available to AI tools")
    for table in reads:
        for role in roles:
            if metadata.table_access(table, role) == "none":
                raise ToolDefinitionError(f"{name}: role '{role}' may not read '{table}' (catalog access is none)")
    for table in writes:
        ai_writes = set(AI_TABLE_GRANTS.get(table, ())) - {"SELECT"}
        if table not in WRITE_GRANTS and not ai_writes:
            raise ToolDefinitionError(f"{name}: the agent database user may not write '{table}'")
    if kind is ToolKind.READ and writes:
        raise ToolDefinitionError(f"{name}: read tools must not write")
    if kind is not ToolKind.READ and not writes:
        raise ToolDefinitionError(f"{name}: write tools must declare what they write")


def _check_preview(name: str, func: Callable, preview: Callable | None, kind: ToolKind) -> None:
    if kind is ToolKind.WRITE_CONFIRM and preview is None:
        raise ToolDefinitionError(f"{name}: confirm tools need a preview (the server-built confirm card)")
    if preview is None:
        return
    if kind is not ToolKind.WRITE_CONFIRM:
        raise ToolDefinitionError(f"{name}: only confirm tools have a preview")
    tool_params = list(inspect.signature(func).parameters)
    preview_params = list(inspect.signature(preview).parameters)
    if preview_params != tool_params:
        raise ToolDefinitionError(f"{name}: preview must take exactly the tool's parameters {tool_params}")


def tool(*, roles: Iterable[str], kind: ToolKind = ToolKind.READ,
         reads: Iterable[str] = (), writes: Iterable[str] = (), joins: Iterable[str] = (),
         name: str | None = None, preview: Callable[..., dict] | None = None):
    """Register a function as an agent tool after checking it against the security rules.

    reads:  tables whose columns can appear in the tool's output (role access is checked).
    joins:  tables used only to link rows (e.g. reviews -> order_items -> product); no
            column from them may reach the output. They still may not be AI-forbidden tables.
    writes: tables the tool changes (must be granted to the agent database user).
    """

    def register(func: Callable) -> Callable:
        tool_name = name or func.__name__
        spec_roles, spec_reads, spec_writes = frozenset(roles), tuple(reads), tuple(writes)
        catalog = metadata.load()["tables"]
        for table in joins:
            if table not in catalog or catalog[table].get("ai_access") == "never":
                raise ToolDefinitionError(f"{tool_name}: may not join '{table}'")
        _check_definition(func, tool_name, spec_roles, kind, spec_reads, spec_writes)
        _check_preview(tool_name, func, preview, kind)
        # Validate model-supplied arguments against the type hints before running.
        validated = validate_call(func, config={"arbitrary_types_allowed": True, "strict": False})
        REGISTRY[tool_name] = ToolSpec(
            name=tool_name,
            description=inspect.cleandoc(func.__doc__),
            func=validated,
            roles=spec_roles,
            kind=kind,
            reads=spec_reads,
            writes=spec_writes,
            joins=tuple(joins),
            preview=validate_call(preview, config={"arbitrary_types_allowed": True}) if preview else None,
        )
        return func

    return register


def tools_for(role: str) -> list[ToolSpec]:
    """The only tools an agent for this role is given."""
    return [spec for spec in REGISTRY.values() if role in spec.roles]


def get_tool(principal: Principal, name: str) -> ToolSpec:
    spec = REGISTRY.get(name)
    if spec is None or principal.role not in spec.roles:
        # Same answer whether the tool doesn't exist or belongs to another role.
        raise ToolError(f"Tool '{name}' is not available.")
    return spec


def _run(fn: Callable, principal: Principal, name: str, args: dict[str, Any] | None) -> Any:
    args = dict(args or {})
    smuggled = IDENTITY_PARAMS & set(args)
    if smuggled:
        raise InvalidArguments(f"Identity can't be passed to tools: {sorted(smuggled)}")
    try:
        with db.as_agent():
            return fn(principal, **args)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        raise InvalidArguments(f"Invalid arguments for '{name}': {problems}") from None


def call_tool(principal: Principal, name: str, args: dict[str, Any] | None = None, *, confirmed: bool = False) -> Any:
    """Run a tool for a principal: role check, confirmation check, argument validation,
    and execution on the least-privilege database user."""
    spec = get_tool(principal, name)
    if spec.kind is ToolKind.WRITE_CONFIRM and not confirmed:
        raise ConfirmationRequired(name)
    return _run(spec.func, principal, name, args)


def preview_tool(principal: Principal, name: str, args: dict[str, Any] | None = None) -> dict:
    """Build the confirm card for a WRITE_CONFIRM tool (validates and checks ownership; no writes)."""
    spec = get_tool(principal, name)
    if spec.kind is not ToolKind.WRITE_CONFIRM or spec.preview is None:
        raise ToolError(f"'{name}' does not need confirmation.")
    return _run(spec.preview, principal, name, args)


# ---- conversation context ----------------------------------------------------------------
# The chat engine sets the current conversation, so tools like transfer_to_human can link
# a ticket to the chat without the model supplying (or being able to change) the id.

_conversation_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("conversation_id", default=None)


@contextmanager
def in_conversation(conversation_id: str | None):
    token = _conversation_id.set(conversation_id)
    try:
        yield
    finally:
        _conversation_id.reset(token)


def current_conversation() -> str | None:
    return _conversation_id.get()


# ---- output helpers ----------------------------------------------------------------


def visible(row: dict, table: str, role: str) -> dict:
    """Keep only the columns of `table` this role may see (per the data catalog)."""
    allowed = set(metadata.visible_columns(table, role))
    return {k: v for k, v in row.items() if k in allowed}


def untrusted(text: str | None) -> dict | None:
    """Wrap user-generated text (reviews, ticket descriptions) so prompts treat it as data.

    The system prompt tells the agent never to follow instructions found inside
    `untrusted_text` fields.
    """
    if text is None:
        return None
    return {"untrusted_text": text}

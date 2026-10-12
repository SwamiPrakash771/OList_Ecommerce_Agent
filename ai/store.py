"""Storage for AI conversations, actions, usage, limits and settings.

Used by the chat engine and the API (not exposed as agent tools). Every function runs on
the least-privilege agent database user, and every read or change of a conversation checks
that it belongs to the caller: users by user_id, guests by their session id.

Usage "subjects" identify who is spending tokens: user:<user_id>, guest:<session_id>, job:<name>.
"""

from __future__ import annotations

import functools
import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import db
from ai.security import Principal

_SETTINGS_TTL_S = 30


def _agent(func):
    """Run the wrapped function on the agent database user."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        with db.as_agent():
            return func(*args, **kwargs)

    return wrapper


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


def _loads(v):
    return json.loads(v) if isinstance(v, (str, bytes)) else v


def subject_for(principal: Principal, session_id: str | None = None) -> str:
    if principal.role == "guest":
        if not session_id:
            raise ValueError("guest usage needs a session id")
        return f"guest:{session_id}"
    return f"user:{principal.user_id}"


# ---- settings and limits --------------------------------------------------------------

_settings_cache: tuple[float, dict] | None = None


@_agent
def settings(refresh: bool = False) -> dict[str, Any]:
    global _settings_cache
    if refresh or not _settings_cache or time.time() - _settings_cache[0] > _SETTINGS_TTL_S:
        rows = db.query_df("SELECT setting_key, value FROM ai_settings")
        _settings_cache = (time.time(), {r.setting_key: _loads(r.value) for r in rows.itertuples()})
    return _settings_cache[1]


@dataclass(frozen=True)
class Limits:
    messages_per_day: int | None
    input_tokens_per_day: int | None
    output_tokens_per_day: int | None
    max_reply_tokens: int | None
    requests_per_minute: int | None
    max_tool_calls_per_turn: int | None
    blocked: bool


LIMIT_FIELDS = ("messages_per_day", "input_tokens_per_day", "output_tokens_per_day",
                "max_reply_tokens", "requests_per_minute", "max_tool_calls_per_turn")


@_agent
def effective_limits(principal: Principal) -> Limits:
    """Role defaults, overridden field by field by a per-user row (if any)."""
    scopes = [f"role:{principal.role}"] + ([f"user:{principal.user_id}"] if principal.user_id else [])
    rows = {r["scope"]: r for r in db.query_df(
        f"SELECT * FROM ai_limits WHERE scope IN ({', '.join(['%s'] * len(scopes))})", tuple(scopes)
    ).to_dict("records")}
    merged: dict[str, Any] = {f: None for f in LIMIT_FIELDS}
    blocked = False
    for scope in scopes:  # role first, then user overrides
        row = rows.get(scope)
        if not row:
            continue
        for f in LIMIT_FIELDS:
            if row[f] is not None and row[f] == row[f]:  # skip NULL / NaN
                merged[f] = int(row[f])
        blocked = blocked or bool(row["blocked"])
    return Limits(**merged, blocked=blocked)


# ---- conversations ---------------------------------------------------------------------


@_agent
def create_conversation(principal: Principal, session_id: str | None = None, title: str | None = None) -> str:
    if principal.role == "guest" and not session_id:
        raise ValueError("guest conversations need a session id")
    conv_id = uuid.uuid4().hex
    now = _now()
    s = settings()
    db.execute(
        """
        INSERT INTO ai_conversations (conversation_id, owner_user_id, owner_role, session_id, title,
                                      prompt_version, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (conv_id, principal.user_id, principal.role, session_id if principal.role == "guest" else None,
         (title or "")[:120] or None, s.get("prompt_version"), now, now),
    )
    return conv_id


def _owner_clause(principal: Principal, session_id: str | None, alias: str = "") -> tuple[str, tuple]:
    """SQL condition matching conversations owned by the caller (users by id, guests by session)."""
    a = f"{alias}." if alias else ""
    if principal.role == "guest":
        return f"{a}owner_role = 'guest' AND {a}session_id = %s", (session_id or "-",)
    return f"{a}owner_user_id = %s AND {a}owner_role = %s", (principal.user_id, principal.role)


@_agent
def get_conversation(principal: Principal, conversation_id: str, session_id: str | None = None) -> dict | None:
    """The conversation if it belongs to the caller and isn't deleted; otherwise None."""
    clause, params = _owner_clause(principal, session_id)
    return db.query_one(
        f"SELECT * FROM ai_conversations WHERE conversation_id = %s AND deleted_at IS NULL AND {clause}",
        (conversation_id, *params),
    )


@_agent
def list_conversations(principal: Principal, session_id: str | None = None, limit: int = 30) -> list[dict]:
    clause, params = _owner_clause(principal, session_id)
    return db.query_df(
        f"""
        SELECT conversation_id, title, status, created_at, last_message_at FROM ai_conversations
        WHERE deleted_at IS NULL AND {clause}
        ORDER BY COALESCE(last_message_at, created_at) DESC LIMIT %s
        """,
        (*params, limit),
    ).to_dict("records")


@_agent
def delete_conversation(principal: Principal, conversation_id: str, session_id: str | None = None) -> bool:
    """Hide immediately; the retention job hard-deletes the content after the grace period."""
    clause, params = _owner_clause(principal, session_id)
    return db.execute(
        f"UPDATE ai_conversations SET deleted_at = %s, updated_at = %s "
        f"WHERE conversation_id = %s AND deleted_at IS NULL AND {clause}",
        (_now(), _now(), conversation_id, *params),
    ) > 0


@_agent
def append_message(conversation_id: str, role: str, content: Any, *, model: str | None = None,
                   prompt_version: str | None = None, tool_set_version: str | None = None,
                   input_tokens: int | None = None, output_tokens: int | None = None,
                   cached_tokens: int | None = None, embedding: bytes | None = None) -> int:
    """Append a message with the next sequence number. Caller must have checked ownership.
    `embedding`: the question's local embedding (user messages), used to find related older turns."""
    now = _now()
    with db.transaction() as cur:
        cur.execute("SELECT conversation_id FROM ai_conversations WHERE conversation_id = %s FOR UPDATE",
                    (conversation_id,))
        if not cur.fetchone():
            raise ValueError("unknown conversation")
        cur.execute("SELECT COALESCE(MAX(sequence_no), 0) + 1 AS n FROM ai_messages WHERE conversation_id = %s",
                    (conversation_id,))
        seq = cur.fetchone()["n"]
        cur.execute(
            """
            INSERT INTO ai_messages (conversation_id, sequence_no, role, content, model, prompt_version,
                                     tool_set_version, input_tokens, output_tokens, cached_tokens, created_at
                                     {embedding_column})
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s {embedding_value})
            """.format(embedding_column=", embedding" if embedding is not None else "",
                       embedding_value=", %s" if embedding is not None else ""),
            (conversation_id, seq, role, json.dumps(content, default=str), model, prompt_version,
             tool_set_version, input_tokens, output_tokens, cached_tokens, now,
             *([embedding] if embedding is not None else [])),
        )
        message_id = cur.lastrowid
        cur.execute(
            "UPDATE ai_conversations SET last_message_at = %s, updated_at = %s"
            + (", model = %s" if model else "") + " WHERE conversation_id = %s",
            (now, now, *( [model] if model else [] ), conversation_id),
        )
    return message_id


@_agent
def question_count(conversation_id: str) -> int:
    """Questions the customer has asked in this conversation so far."""
    row = db.query_one("SELECT COUNT(*) AS n FROM ai_messages WHERE conversation_id = %s AND role = 'user'",
                       (conversation_id,))
    return int(row["n"]) if row else 0


@_agent
def history(conversation_id: str, limit: int = 40) -> list[dict]:
    """Last `limit` messages, oldest first. Caller must have checked ownership."""
    rows = db.query_df(
        """
        SELECT * FROM (
            SELECT message_id, sequence_no, role, content, created_at FROM ai_messages
            WHERE conversation_id = %s ORDER BY sequence_no DESC LIMIT %s
        ) recent ORDER BY sequence_no
        """,
        (conversation_id, limit),
    ).to_dict("records")
    for r in rows:
        r["content"] = _loads(r["content"])
    return rows


@_agent
def link_ticket(conversation_id: str, ticket_id: str) -> None:
    """Escalated chats are kept with their ticket (retention class ticket_linked)."""
    db.execute(
        "UPDATE ai_conversations SET ticket_id = %s, status = 'escalated', retention_class = 'ticket_linked', "
        "updated_at = %s WHERE conversation_id = %s",
        (ticket_id, _now(), conversation_id),
    )


# ---- tool-call audit ---------------------------------------------------------------------

SENSITIVE_ARG_KEYS = {"phone", "street", "number", "complement", "recipient_name", "zip_code"}


def redact_args(args: dict | None) -> dict | None:
    if args is None:
        return None
    return {k: ("[redacted]" if k in SENSITIVE_ARG_KEYS else v) for k, v in args.items()}


@_agent
def record_tool_call(conversation_id: str, tool_name: str, kind: str, args: dict | None, status: str, *,
                     message_id: int | None = None, error_category: str | None = None,
                     duration_ms: int | None = None, confirmed_by_user_id: int | None = None) -> int:
    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ai_tool_calls (conversation_id, message_id, tool_name, kind, args, status,
                                       error_category, duration_ms, confirmed_by_user_id, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (conversation_id, message_id, tool_name, kind, json.dumps(redact_args(args), default=str), status,
             error_category, duration_ms, confirmed_by_user_id, _now()),
        )
        return cur.lastrowid


# ---- pending actions (confirm cards) -------------------------------------------------


def _idempotency_key(conversation_id: str, tool_name: str, args: dict) -> str:
    raw = json.dumps([conversation_id, tool_name, args], sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


@_agent
def create_pending_action(principal: Principal, conversation_id: str, tool_name: str,
                          args: dict, preview: dict) -> dict:
    """Create (or return the existing) pending action for the same proposal."""
    if principal.user_id is None:
        raise ValueError("only signed-in users can confirm actions")
    key = _idempotency_key(conversation_id, tool_name, args)
    existing = db.query_one("SELECT * FROM ai_pending_actions WHERE idempotency_key = %s", (key,))
    if existing and existing["status"] == "pending" and existing["expires_at"] > _now():
        return existing
    if existing:  # an older decided/expired proposal with the same content: start a fresh one
        key = hashlib.sha256((key + uuid.uuid4().hex).encode()).hexdigest()
    ttl = int(settings().get("pending_action_ttl_minutes", 15))
    action = {
        "action_id": uuid.uuid4().hex, "conversation_id": conversation_id, "user_id": principal.user_id,
        "tool_name": tool_name, "args": args, "preview": preview, "status": "pending",
        "idempotency_key": key, "created_at": _now(), "expires_at": _now() + timedelta(minutes=ttl),
    }
    db.execute(
        """
        INSERT INTO ai_pending_actions (action_id, conversation_id, user_id, tool_name, args, preview,
                                        status, idempotency_key, created_at, expires_at)
        VALUES (%s, %s, %s, %s, %s, %s, 'pending', %s, %s, %s)
        """,
        (action["action_id"], conversation_id, principal.user_id, tool_name, json.dumps(args, default=str),
         json.dumps(preview, default=str), key, action["created_at"], action["expires_at"]),
    )
    return action


@_agent
def claim_pending_action(principal: Principal, action_id: str, approve: bool) -> dict | None:
    """Atomically move a pending action owned by the caller to confirmed/rejected.

    Returns the action if this call won the transition, None if it isn't the caller's,
    is no longer pending, or has expired. A double click therefore can't run it twice.
    """
    now = _now()
    changed = db.execute(
        """
        UPDATE ai_pending_actions SET status = %s, decided_at = %s
        WHERE action_id = %s AND user_id = %s AND status = 'pending' AND expires_at > %s
        """,
        ("confirmed" if approve else "rejected", now, action_id, principal.user_id, now),
    )
    if not changed:
        return None
    row = db.query_one("SELECT * FROM ai_pending_actions WHERE action_id = %s", (action_id,))
    row["args"], row["preview"] = _loads(row["args"]), _loads(row["preview"])
    return row


@_agent
def finish_pending_action(action_id: str, ok: bool, result: Any) -> None:
    db.execute(
        "UPDATE ai_pending_actions SET status = %s, result = %s WHERE action_id = %s AND status = 'confirmed'",
        ("executed" if ok else "failed", json.dumps(result, default=str), action_id),
    )


# ---- usage ---------------------------------------------------------------------------


@_agent
def record_usage(subject: str, role: str, model: str, input_tokens: int, output_tokens: int,
                 cached_tokens: int = 0, cost: float = 0.0, *, user_id: int | None = None,
                 conversation_id: str | None = None, source: str = "chat", job_name: str | None = None,
                 count_request: bool = True) -> None:
    """Write the usage ledger row and bump today's totals (the hourly job re-derives them).
    count_request=False (a message that failed on our or the provider's side) records the tokens
    and cost but doesn't count toward the customer's daily message limit."""
    now = _now()
    with db.transaction() as cur:
        cur.execute(
            """
            INSERT INTO ai_usage (request_id, subject, user_id, role, conversation_id, source, job_name, model,
                                  input_tokens, output_tokens, cached_tokens, cost, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (uuid.uuid4().hex, subject, user_id, role, conversation_id, source, job_name, model,
             input_tokens, output_tokens, cached_tokens, cost, now),
        )
        cur.execute(
            """
            INSERT INTO ai_usage_daily (usage_date, subject, role, requests, input_tokens, output_tokens,
                                        cached_tokens, cost)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE requests = requests + VALUES(requests),
                input_tokens = input_tokens + VALUES(input_tokens),
                output_tokens = output_tokens + VALUES(output_tokens),
                cached_tokens = cached_tokens + VALUES(cached_tokens),
                cost = cost + VALUES(cost)
            """,
            (now.date(), subject, role, 1 if count_request else 0, input_tokens, output_tokens, cached_tokens, cost),
        )


@_agent
def usage_today(subject: str) -> dict:
    row = db.query_one(
        "SELECT requests, input_tokens, output_tokens, cached_tokens, cost FROM ai_usage_daily "
        "WHERE usage_date = %s AND subject = %s",
        (date.today(), subject),
    )
    return {k: (float(v) if k == "cost" else int(v)) for k, v in row.items()} if row else \
        {"requests": 0, "input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "cost": 0.0}


@_agent
def requests_last_minute(subject: str) -> int:
    return db.query_one(
        "SELECT COUNT(*) AS n FROM ai_usage WHERE subject = %s AND created_at > %s",
        (subject, _now() - timedelta(minutes=1)),
    )["n"]


# ---- feedback and admin access ---------------------------------------------------------


@_agent
def add_feedback(principal: Principal, message_id: int, rating: str, reason: str | None = None,
                 session_id: str | None = None) -> bool:
    """Thumbs up/down on an assistant message in one of the caller's conversations."""
    if rating not in ("up", "down"):
        raise ValueError("rating must be 'up' or 'down'")
    clause, params = _owner_clause(principal, session_id, alias="c")
    owned = db.query_one(
        f"""
        SELECT m.message_id FROM ai_messages m JOIN ai_conversations c USING (conversation_id)
        WHERE m.message_id = %s AND m.role = 'assistant' AND c.deleted_at IS NULL AND {clause}
        """,
        (message_id, *params),
    )
    if not owned:
        return False
    db.execute(
        """
        INSERT INTO ai_feedback (message_id, user_id, rating, reason, created_at) VALUES (%s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE rating = VALUES(rating), reason = VALUES(reason), created_at = VALUES(created_at)
        """,
        (message_id, principal.user_id, rating, (reason or "")[:255] or None, _now()),
    )
    return True


@_agent
def log_admin_access(principal: Principal, conversation_id: str, action: str, reason: str) -> None:
    if principal.role != "admin":
        raise PermissionError("only admins open other users' conversations")
    if not reason or len(reason.strip()) < 5:
        raise ValueError("a reason is required to open a conversation")
    db.execute(
        "INSERT INTO ai_admin_access_log (admin_user_id, conversation_id, action, reason, created_at) "
        "VALUES (%s, %s, %s, %s, %s)",
        (principal.user_id, conversation_id, action, reason.strip()[:255], _now()),
    )

"""Create the least-privilege MySQL user that AI tools run as.

    python -m ai.db_user            create/update the user and print its grants

Read access comes from the catalog (ai/metadata.yaml): SELECT on every table that at
least one role may see, never on tables marked `ai_access: never` (app_users).
Write access is the explicit list below, reviewed in git. Re-running revokes everything
and grants again, so the user never keeps permissions that were removed here.

Credentials: AGENT_MYSQL_USER / AGENT_MYSQL_PASSWORD in .env. If the password is missing,
a strong one is generated and appended to .env (it is never printed).
"""

import os
import re
import secrets
import sys
from pathlib import Path

import db
from ai import metadata

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
DEFAULT_USER = "olist_agent"
HOSTS = ("localhost", "127.0.0.1")
MAX_CONNECTIONS = 20

# Everything the agent may change. Keep this list short; each entry is a deliberate decision.
WRITE_GRANTS: dict[str, tuple[str, ...]] = {
    "cart_items": ("INSERT", "UPDATE", "DELETE"),       # customer cart actions (with undo)
    "customer_addresses": ("INSERT", "UPDATE"),         # address change (after confirmation)
    "support_tickets": ("INSERT", "UPDATE"),            # open ticket / transfer to human
    "returns": ("INSERT",),                             # return request (after confirmation)
    "products": ("INSERT", "UPDATE"),                   # seller product edits / drafts
    "product_stats": ("INSERT", "UPDATE"),
    "product_edits": ("INSERT",),                       # audit row for every product edit
}
# AI bookkeeping tables written by the chat engine (ai/store.py). Audit-style tables are
# append-only (INSERT, no UPDATE) so the agent can't rewrite its own usage or audit trail.
# Limits and settings are read-only here: only the admin API (app user) changes them.
# No DELETE anywhere: retention purges run as the app user (ai/scheduler.py).
AI_TABLE_GRANTS: dict[str, tuple[str, ...]] = {
    "ai_conversations": ("SELECT", "INSERT", "UPDATE"),
    "ai_messages": ("SELECT", "INSERT", "UPDATE (embedding)"),   # messages are append-only; only the
                                                                 # search embedding can be filled in later
    "ai_tool_calls": ("SELECT", "INSERT"),
    "ai_pending_actions": ("SELECT", "INSERT", "UPDATE"),
    "ai_usage": ("SELECT", "INSERT"),
    "ai_usage_daily": ("SELECT", "INSERT", "UPDATE"),
    "ai_feedback": ("SELECT", "INSERT", "UPDATE"),
    "ai_admin_access_log": ("SELECT", "INSERT"),
    "ai_limits": ("SELECT",),
    "ai_settings": ("SELECT",),
}


def readable_tables() -> list[str]:
    cat = metadata.load()
    return sorted(
        name for name, t in cat["tables"].items()
        if t.get("ai_access") != "never" and any(metadata.table_access(name, r) != "none" for r in metadata.ROLES)
    )


def planned_grants(existing_tables: set[str]) -> dict[str, list[str]]:
    grants: dict[str, set[str]] = {}
    for t in readable_tables():
        if t in existing_tables:
            grants.setdefault(t, set()).add("SELECT")
    for t, privs in WRITE_GRANTS.items():
        if t in existing_tables:
            grants.setdefault(t, set()).update(privs)
    for t, privs in AI_TABLE_GRANTS.items():
        if t in existing_tables:
            grants.setdefault(t, set()).update(privs)
    never = {n for n, t in metadata.load()["tables"].items() if t.get("ai_access") == "never"}
    leaked = never & set(grants)
    if leaked:
        raise RuntimeError(f"refusing to grant access to protected tables: {sorted(leaked)}")
    return {t: sorted(p) for t, p in sorted(grants.items())}


def ensure_credentials() -> tuple[str, str]:
    user = os.environ.get("AGENT_MYSQL_USER") or DEFAULT_USER
    password = os.environ.get("AGENT_MYSQL_PASSWORD")
    if not password:
        password = secrets.token_urlsafe(24)
        lines = ["", "# Least-privilege MySQL user for AI tools (created by python -m ai.db_user)"]
        if not os.environ.get("AGENT_MYSQL_USER"):
            lines.append(f"AGENT_MYSQL_USER={user}")
        lines.append(f"AGENT_MYSQL_PASSWORD={password}")
        with ENV_PATH.open("a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        os.environ["AGENT_MYSQL_USER"], os.environ["AGENT_MYSQL_PASSWORD"] = user, password
        print(f"generated a password for '{user}' and saved it to .env")
    return user, password


def apply() -> dict[str, list[str]]:
    user, password = ensure_credentials()
    if not re.fullmatch(r"[A-Za-z0-9_]{1,32}", user):
        raise SystemExit("AGENT_MYSQL_USER may only contain letters, digits and underscores")
    schema = os.environ["MYSQL_DATABASE"]
    existing = set(metadata.live_schema())
    grants = planned_grants(existing)
    for host in HOSTS:
        account = f"'{user}'@'{host}'"
        db.execute(f"CREATE USER IF NOT EXISTS {account} IDENTIFIED BY %s", (password,))
        db.execute(f"ALTER USER {account} IDENTIFIED BY %s WITH MAX_USER_CONNECTIONS {MAX_CONNECTIONS}", (password,))
        db.execute(f"REVOKE ALL PRIVILEGES, GRANT OPTION FROM {account}")
        for table, privs in grants.items():
            db.execute(f"GRANT {', '.join(privs)} ON `{schema}`.`{table}` TO {account}")
    return grants


if __name__ == "__main__":
    problems = metadata.validate(metadata.live_schema())
    if problems:
        print("catalog does not match the database; fix ai/metadata.yaml first")
        sys.exit(1)
    grants = apply()
    print(f"agent user '{os.environ['AGENT_MYSQL_USER']}' has access to {len(grants)} tables:")
    for table, privs in grants.items():
        print(f"  {table:<22} {', '.join(privs)}")

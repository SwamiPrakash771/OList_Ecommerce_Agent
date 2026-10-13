"""The admin agent's own least-privilege MySQL user.

    python -m ai.admin.db_user            create/update the user and print its grants

Separate from the customer and seller agents' users. Read access is **column by column**, generated
from the data catalog's admin rules (ai/metadata.yaml):
- every column admins may see **that isn't masked** for admins;
- a few masked columns that platform figures need, **for aggregates only** (AGGREGATE_ONLY: payment
  values for GMV, buyer state for revenue by state, refund amounts for totals). Tools may only put
  them in totals, counts and groupings, never show one person's value (enforced by the tool
  registry and the permission tests);
- **never**: addresses, ticket descriptions and AI summaries, refund failure reasons, chat titles and
  content, tool-call arguments, feedback text, login data (app_users).

Storage: the admin engine stores and re-reads its own chats in the ai_* tables, so those tables are
granted at table level (AI_TABLE_GRANTS); the tool registry keeps tools away from chat content.
No store data can be changed: the admin agent is read-only (decision 3).

Credentials: ADMIN_AGENT_MYSQL_USER / ADMIN_AGENT_MYSQL_PASSWORD in .env; a missing password is
generated and appended to .env (never printed). Re-running revokes everything and grants again.
"""

import os
import re
import secrets
import sys
from pathlib import Path

import db
from ai import metadata

ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
DEFAULT_USER = "olist_admin_agent"
HOSTS = ("localhost", "127.0.0.1")
MAX_CONNECTIONS = 12
ROLE = "admin"

# Tables the admin agent never reads, whatever the catalog says.
NEVER_TABLES = {"customer_addresses", "order_addresses", "cart_items", "app_users"}
# Masked columns that platform figures need: totals, counts and groupings only.
AGGREGATE_ONLY: dict[str, tuple[str, ...]] = {
    "customers": ("customer_unique_id", "customer_city", "customer_state"),
    "payments": ("payment_type", "payment_installments", "payment_value"),
    "refunds": ("amount", "payment_method"),
    "admin_order_facts": ("buyer_state", "customer_key", "payments_total", "payment_type"),
    "admin_seller_order_facts": ("buyer_state",),
    "admin_item_facts": ("buyer_state",),
}
# AI bookkeeping written and re-read by the admin engine (its own chats), plus the access log.
AI_TABLE_GRANTS: dict[str, tuple[str, ...]] = {
    "ai_conversations": ("SELECT", "INSERT", "UPDATE"),
    "ai_messages": ("SELECT", "INSERT", "UPDATE (embedding)"),
    "ai_tool_calls": ("SELECT", "INSERT"),
    "ai_pending_actions": ("SELECT", "INSERT", "UPDATE"),
    "ai_usage": ("SELECT", "INSERT"),
    "ai_usage_daily": ("SELECT", "INSERT", "UPDATE"),
    "ai_feedback": ("SELECT", "INSERT", "UPDATE"),
    "ai_admin_access_log": ("SELECT", "INSERT"),
    "ai_limits": ("SELECT",),
    "ai_settings": ("SELECT",),
}


def tool_columns() -> dict[str, list[str]]:
    """{table: columns} admin tools may read and show: admin-visible and not masked."""
    cat = metadata.load()["tables"]
    out = {}
    for name, t in cat.items():
        if name in NEVER_TABLES or t.get("ai_access") == "never" or metadata.table_access(name, ROLE) == "none":
            continue
        masked = set(metadata.masked_columns(name, ROLE))
        cols = [c for c in metadata.visible_columns(name, ROLE) if c not in masked]
        if cols:
            out[name] = sorted(cols)
    return out


def aggregate_columns() -> dict[str, list[str]]:
    """{table: columns} readable only for totals, counts and groupings."""
    return {t: sorted(cols) for t, cols in AGGREGATE_ONLY.items()}


def readable_columns() -> dict[str, list[str]]:
    """Everything the admin user may SELECT outside the ai_* storage tables."""
    out = {t: list(c) for t, c in tool_columns().items()}
    for t, cols in aggregate_columns().items():
        out[t] = sorted(set(out.get(t, [])) | set(cols))
    return {t: c for t, c in out.items() if t not in AI_TABLE_GRANTS}


def planned_grants(existing_tables: set[str]) -> list[str]:
    grants = [f"SELECT ({', '.join(f'`{c}`' for c in cols)}) ON {t}"
              for t, cols in readable_columns().items() if t in existing_tables]
    grants += [f"{', '.join(privs)} ON {t}" for t, privs in AI_TABLE_GRANTS.items() if t in existing_tables]
    leaked = [g for g in grants if g.rsplit(" ON ", 1)[1] in NEVER_TABLES]
    if leaked:
        raise RuntimeError(f"refusing to grant access to protected tables: {leaked}")
    return grants


def ensure_credentials() -> tuple[str, str]:
    user = os.environ.get("ADMIN_AGENT_MYSQL_USER") or DEFAULT_USER
    password = os.environ.get("ADMIN_AGENT_MYSQL_PASSWORD")
    if not password:
        password = secrets.token_urlsafe(24)
        lines = ["", "# Least-privilege MySQL user for the admin agent (created by python -m ai.admin.db_user)"]
        if not os.environ.get("ADMIN_AGENT_MYSQL_USER"):
            lines.append(f"ADMIN_AGENT_MYSQL_USER={user}")
        lines.append(f"ADMIN_AGENT_MYSQL_PASSWORD={password}")
        with ENV_PATH.open("a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        os.environ["ADMIN_AGENT_MYSQL_USER"], os.environ["ADMIN_AGENT_MYSQL_PASSWORD"] = user, password
        print(f"generated a password for '{user}' and saved it to .env")
    return user, password


def apply() -> list[str]:
    user, password = ensure_credentials()
    if not re.fullmatch(r"[A-Za-z0-9_]{1,32}", user):
        raise SystemExit("ADMIN_AGENT_MYSQL_USER may only contain letters, digits and underscores")
    schema = os.environ["MYSQL_DATABASE"]
    grants = planned_grants(set(metadata.live_schema()))
    for host in HOSTS:
        account = f"'{user}'@'{host}'"
        db.execute(f"CREATE USER IF NOT EXISTS {account} IDENTIFIED BY %s", (password,))
        db.execute(f"ALTER USER {account} IDENTIFIED BY %s WITH MAX_USER_CONNECTIONS {MAX_CONNECTIONS}", (password,))
        db.execute(f"REVOKE ALL PRIVILEGES, GRANT OPTION FROM {account}")
        for grant in grants:
            privs, table = grant.rsplit(" ON ", 1)
            db.execute(f"GRANT {privs} ON `{schema}`.`{table}` TO {account}")
    return grants


if __name__ == "__main__":
    problems = metadata.validate(metadata.live_schema())
    if problems:
        print("catalog does not match the database; fix ai/metadata.yaml first")
        sys.exit(1)
    for g in apply():
        print(" ", g[:180])

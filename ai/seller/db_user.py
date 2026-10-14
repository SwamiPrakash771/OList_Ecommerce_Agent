"""The seller agent's own least-privilege MySQL user.

    python -m ai.seller.db_user            create/update the user and print its grants

Separate from the customer agent's user (ai/db_user.py). Read access is **column by column**,
generated from the data catalog's seller rules (ai/metadata.yaml): MySQL itself refuses any
column a seller may not see (buyer names and IDs, payments, payment method of a refund…), even
if a tool or a query asked for it. A few join keys that sellers never see are added so tools can
link rows (orders.customer_id -> customers.customer_city).

Writes: listing edits of the seller's own products (column by column), their audit rows, and
the AI bookkeeping tables.
Credentials: SELLER_AGENT_MYSQL_USER / SELLER_AGENT_MYSQL_PASSWORD in .env. A missing password
is generated and appended to .env (never printed). Re-running revokes everything and grants again.
"""

import os
import re
import secrets
import sys
from pathlib import Path

import db
from ai import metadata

ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
DEFAULT_USER = "olist_seller_agent"
# Where the app connects from: this machine by default; in AWS, AGENT_DB_HOSTS=% (any address; the
# database firewall decides who can reach it at all). Comma-separated MySQL host patterns.
HOSTS = tuple(h.strip() for h in os.environ.get("AGENT_DB_HOSTS", "localhost,127.0.0.1").split(",") if h.strip())
MAX_CONNECTIONS = 12
ROLE = "seller"

# Columns tools need to link rows but which never reach the seller or the model.
JOIN_KEYS: dict[str, tuple[str, ...]] = {
    "orders": ("customer_id",),
    "customers": ("customer_id",),
}
# Changes the seller agent may make: listing edits of the seller's own products (update_listing,
# after the seller confirms), column by column, plus the audit row for every edit.
WRITE_GRANTS: dict[str, tuple[str, ...]] = {
    "products": ("UPDATE (display_name, category, weight_g, length_cm, height_cm, width_cm)",),
    "product_stats": ("UPDATE (display_name, category, typical_price, avg_freight)",),
    "product_edits": ("INSERT",),
}
# AI bookkeeping written by the chat engine (append-only where it's an audit trail).
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


def readable_columns() -> dict[str, list[str]]:
    """{table: columns} the seller agent may read: the catalog's seller-visible columns plus join keys."""
    cat = metadata.load()["tables"]
    out = {}
    for name, t in cat.items():
        if t.get("ai_access") == "never" or metadata.table_access(name, ROLE) == "none":
            continue
        cols = list(metadata.visible_columns(name, ROLE)) + [k for k in JOIN_KEYS.get(name, ()) if k in t["columns"]]
        if cols:
            out[name] = sorted(set(cols))
    return out


def planned_grants(existing_tables: set[str]) -> list[str]:
    """GRANT privilege clauses as 'PRIVS ON table' strings (for printing and tests)."""
    grants = []
    for table, cols in readable_columns().items():
        if table in existing_tables:
            grants.append(f"SELECT ({', '.join(f'`{c}`' for c in cols)}) ON {table}")
    for table, privs in {**WRITE_GRANTS, **AI_TABLE_GRANTS}.items():
        if table in existing_tables:
            grants.append(f"{', '.join(privs)} ON {table}")
    never = {n for n, t in metadata.load()["tables"].items() if t.get("ai_access") == "never"}
    leaked = [g for g in grants if g.rsplit(" ON ", 1)[1] in never]
    if leaked:
        raise RuntimeError(f"refusing to grant access to protected tables: {leaked}")
    return grants


def ensure_credentials() -> tuple[str, str]:
    user = os.environ.get("SELLER_AGENT_MYSQL_USER") or DEFAULT_USER
    password = os.environ.get("SELLER_AGENT_MYSQL_PASSWORD")
    if not password:
        password = secrets.token_urlsafe(24)
        lines = ["", "# Least-privilege MySQL user for the seller agent (created by python -m ai.seller.db_user)"]
        if not os.environ.get("SELLER_AGENT_MYSQL_USER"):
            lines.append(f"SELLER_AGENT_MYSQL_USER={user}")
        lines.append(f"SELLER_AGENT_MYSQL_PASSWORD={password}")
        with ENV_PATH.open("a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        os.environ["SELLER_AGENT_MYSQL_USER"], os.environ["SELLER_AGENT_MYSQL_PASSWORD"] = user, password
        print(f"generated a password for '{user}' and saved it to .env")
    return user, password


def apply() -> list[str]:
    user, password = ensure_credentials()
    if not re.fullmatch(r"[A-Za-z0-9_]{1,32}", user):
        raise SystemExit("SELLER_AGENT_MYSQL_USER may only contain letters, digits and underscores")
    schema = os.environ["MYSQL_DATABASE"]
    grants = planned_grants(set(metadata.live_schema()))
    for host in HOSTS:
        account = f"'{user}'@'{host}'"
        account_p = account.replace("%", "%%")   # with a parameter, a host of % must be escaped
        db.execute(f"CREATE USER IF NOT EXISTS {account_p} IDENTIFIED BY %s", (password,))
        db.execute(f"ALTER USER {account_p} IDENTIFIED BY %s WITH MAX_USER_CONNECTIONS {MAX_CONNECTIONS}", (password,))
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
        print(" ", g)

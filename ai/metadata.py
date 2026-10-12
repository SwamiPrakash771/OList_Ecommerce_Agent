"""Data catalog for the AI agents, loaded from ai/metadata.yaml.

    python -m ai.metadata            validate the catalog against the live database
    python -m ai.metadata customer   also print the prompt snippet for a role

The catalog says what every table and column means and who may see it. Tools and
permission tests use `visible_columns`; agent prompts use `prompt_snippet`.
"""

import functools
import sys
from pathlib import Path

import yaml

CATALOG_PATH = Path(__file__).with_name("metadata.yaml")
ROLES = ("guest", "customer", "seller", "admin")
SENSITIVITY = ("public", "internal", "personal", "secret")
SCOPES = ("all", "own", "none")


@functools.lru_cache(maxsize=1)
def load() -> dict:
    with CATALOG_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def table_access(table: str, role: str) -> str:
    """'all', 'own' or 'none' for this role. Unknown tables are 'none'."""
    t = load()["tables"].get(table)
    if not t or t.get("ai_access") == "never":
        return "none"
    return t["access"].get(role, "none")


def visible_columns(table: str, role: str) -> list[str]:
    """Columns a role's agent may receive from this table (empty if no access).

    Secret columns are never visible, and guests only ever see public columns.
    Admins see personal columns, but the caller is responsible for masking them
    (see `masked_columns`).
    """
    if table_access(table, role) == "none":
        return []
    cols = load()["tables"][table]["columns"]
    return [
        name for name, c in cols.items()
        if c["sensitivity"] != "secret"
        and role not in c.get("hidden_from", [])
        and (role != "guest" or c["sensitivity"] == "public")
    ]


def masked_columns(table: str, role: str) -> list[str]:
    """Personal columns that must be masked by default for this role (admins)."""
    if role != "admin":
        return []
    return [c for c in visible_columns(table, role)
            if load()["tables"][table]["columns"][c]["sensitivity"] == "personal"]


def prompt_snippet(role: str) -> str:
    """Compact description of the data this role's agent works with, for its system prompt."""
    cat = load()
    lines = ["Data you can work with (through your tools only):"]
    for name, t in cat["tables"].items():
        cols = visible_columns(name, role)
        if not cols:
            continue
        scope = table_access(name, role)
        lines.append(f"- {name} ({'only the user\'s own rows' if scope == 'own' else 'all rows'}): {t['description']}")
        for col in cols:
            values = t["columns"][col].get("allowed_values")
            if values:
                listed = "; ".join(f"{k} = {v}" for k, v in values.items())
                lines.append(f"    {col} values: {listed}")
                # Values present in the data that nobody has described yet (from the last
                # profile, see ai/catalog.py) are listed so the agent doesn't guess their meaning.
                from ai.catalog import live_values

                unknown = [v for v in (live_values(name, col) or {}) if v not in {str(k) for k in values}]
                if unknown:
                    lines.append(f"    {col} also has undocumented values (meaning unknown, don't guess): {', '.join(unknown)}")
    lines.append("Terms:")
    for term, meaning in cat["glossary"].items():
        lines.append(f"- {term}: {' '.join(str(meaning).split())}")
    return "\n".join(lines)


# ---- validation -----------------------------------------------------------------


def validate(live_schema: dict[str, list[str]] | None = None) -> list[str]:
    """Return a list of problems (empty = catalog is consistent).

    Checks the catalog's own rules, and, if `live_schema` ({table: [columns]}) is
    given, that it matches the database exactly.
    """
    cat = load()
    problems: list[str] = []
    for tname, t in cat["tables"].items():
        for key in ("description", "grain", "primary_key", "access", "columns"):
            if key not in t:
                problems.append(f"{tname}: missing '{key}'")
        for role, scope in t.get("access", {}).items():
            if role not in ROLES or scope not in SCOPES:
                problems.append(f"{tname}: bad access entry {role}: {scope}")
        if "own" in t.get("access", {}).values():
            owners = t.get("ownership", {})
            for role, scope in t["access"].items():
                if scope == "own" and role not in owners:
                    problems.append(f"{tname}: role '{role}' has 'own' access but no ownership rule")
        for cname, c in t.get("columns", {}).items():
            if not c.get("description"):
                problems.append(f"{tname}.{cname}: no description")
            if c.get("sensitivity") not in SENSITIVITY:
                problems.append(f"{tname}.{cname}: bad sensitivity {c.get('sensitivity')!r}")
            for role in c.get("hidden_from", []):
                if role not in ROLES:
                    problems.append(f"{tname}.{cname}: unknown role in hidden_from: {role}")
            # Guests must never see personal data, whatever the table scope says.
            if c.get("sensitivity") == "personal" and table_access(tname, "guest") != "none" \
                    and "guest" not in c.get("hidden_from", []):
                problems.append(f"{tname}.{cname}: personal column visible to guests")

    if live_schema is not None:
        for tname, cols in live_schema.items():
            if tname not in cat["tables"]:
                problems.append(f"database table '{tname}' is not in the catalog")
                continue
            documented = set(cat["tables"][tname]["columns"])
            for col in cols:
                if col not in documented:
                    problems.append(f"{tname}.{col}: in the database but not in the catalog")
            for col in documented - set(cols):
                problems.append(f"{tname}.{col}: in the catalog but not in the database")
        for tname in cat["tables"]:
            if tname not in live_schema:
                problems.append(f"catalog table '{tname}' does not exist in the database")
    return problems


def live_schema() -> dict[str, list[str]]:
    import db

    rows = db.query_df(
        "SELECT table_name AS t, column_name AS c FROM information_schema.columns "
        "WHERE table_schema = DATABASE() ORDER BY table_name, ordinal_position"
    )
    schema: dict[str, list[str]] = {}
    for r in rows.itertuples():
        schema.setdefault(r.t, []).append(r.c)
    return schema


if __name__ == "__main__":
    problems = validate(live_schema())
    if problems:
        print(f"{len(problems)} catalog problem(s):")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)
    cat = load()
    n_cols = sum(len(t["columns"]) for t in cat["tables"].values())
    print(f"catalog OK: {len(cat['tables'])} tables, {n_cols} columns match the database")
    if len(sys.argv) > 1:
        print()
        print(prompt_snippet(sys.argv[1]))

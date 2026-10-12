"""Generate docs/DATA_DICTIONARY.md from ai/metadata.yaml and the live database.

    python -m ai.build_data_dictionary

Edit ai/metadata.yaml, not the generated file. The catalog is validated first, so
the dictionary can't describe columns that don't exist (or miss ones that do).
"""

import sys
from datetime import date
from pathlib import Path

import db
from ai import metadata

OUT = Path(__file__).resolve().parent.parent / "docs" / "DATA_DICTIONARY.md"
ROLE_SHORT = {"guest": "G", "customer": "C", "seller": "S", "admin": "A"}


def column_types() -> dict[tuple[str, str], str]:
    rows = db.query_df(
        "SELECT table_name AS t, column_name AS c, column_type AS ty FROM information_schema.columns "
        "WHERE table_schema = DATABASE()"
    )
    return {(r.t, r.c): r.ty for r in rows.itertuples()}


def cell(text) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")


def build() -> str:
    cat = metadata.load()
    types = column_types()
    out: list[str] = []
    w = out.append

    w("# Data Dictionary")
    w("")
    w(f"Generated {date.today().isoformat()} from `ai/metadata.yaml` and the live database by "
      "`python -m ai.build_data_dictionary`. **Do not edit by hand**; edit the YAML and regenerate.")
    w("")
    w(f"Dataset: {cat['dataset']}.")
    w("")
    w("## How to read this")
    w("")
    w("| Sensitivity | Meaning |")
    w("|---|---|")
    w("| public | Safe for anyone, including guests |")
    w("| internal | Business data, shown only to roles with access to the row |")
    w("| personal | Identifies or locates a person: the owner only (admins see it masked by default) |")
    w("| secret | Never given to any AI agent or shown in any UI |")
    w("")
    w("Access scopes: **all** = every row, **own** = only rows belonging to the signed-in user "
      "(enforced on the server), **none** = not reachable. Column visibility: G guest, C customer, "
      "S seller, A admin.")
    w("")

    w("## Access by role")
    w("")
    w("| Table | Rows | Guest | Customer | Seller | Admin |")
    w("|---|---:|---|---|---|---|")
    counts = {}
    for name in cat["tables"]:
        counts[name] = db.query_one(f"SELECT COUNT(*) AS n FROM `{name}`")["n"]
        access = [metadata.table_access(name, r) for r in metadata.ROLES]
        w(f"| [`{name}`](#{name}) | {counts[name]:,} | " + " | ".join(access) + " |")
    w("")

    w("## Glossary")
    w("")
    for term, meaning in cat["glossary"].items():
        w(f"- **{term}**: {cell(meaning)}")
    w("")
    w("## Known data quirks")
    w("")
    for q in cat["quirks"]:
        w(f"- {cell(q)}")
    w("")

    w("## Tables")
    for name, t in cat["tables"].items():
        w("")
        w(f"### {name}")
        w("")
        w(cell(t["description"]))
        w("")
        w(f"- **Grain:** {t['grain']} · **Rows:** {counts[name]:,} · **Primary key:** "
          + ", ".join(f"`{k}`" for k in t["primary_key"]))
        if t.get("relationships"):
            w("- **Relationships:** " + ", ".join(f"`{r}`" for r in t["relationships"]))
        if t.get("ownership"):
            w("- **Ownership (\"own\" means):** " + "; ".join(f"{role}: {cell(rule)}" for role, rule in t["ownership"].items()))
        if t.get("ai_access") == "never":
            w("- **AI access: never.** No agent, tool or prompt may read this table.")
        if t.get("notes"):
            w(f"- **Notes:** {cell(t['notes'])}")
        w("")
        w("| Column | Type | Sensitivity | Visible to | Description |")
        w("|---|---|---|---|---|")
        visible = {r: set(metadata.visible_columns(name, r)) for r in metadata.ROLES}
        masked = set(metadata.masked_columns(name, "admin"))
        for col, c in t["columns"].items():
            who = "".join(
                ROLE_SHORT[r] + ("*" if r == "admin" and col in masked else "")
                for r in metadata.ROLES if col in visible[r]
            ) or "—"
            desc = cell(c["description"])
            if c.get("allowed_values"):
                desc += "<br>Values: " + "; ".join(f"`{k}` {cell(v)}" for k, v in c["allowed_values"].items())
            if c.get("example") is not None:
                desc += f"<br>Example: `{cell(c['example'])}`"
            w(f"| `{col}` | {types.get((name, col), '?')} | {c['sensitivity']} | {who} | {desc} |")
    w("")
    w("`A*` = admin sees the column masked by default; unmasking is logged.")
    w("")
    return "\n".join(out)


if __name__ == "__main__":
    problems = metadata.validate(metadata.live_schema())
    if problems:
        print("Catalog does not match the database; fix ai/metadata.yaml first:")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)
    OUT.write_text(build(), encoding="utf-8")
    print(f"wrote {OUT.relative_to(OUT.parent.parent)}")

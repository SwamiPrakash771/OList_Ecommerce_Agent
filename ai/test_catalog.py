"""Build docs/TEST_CATALOG.md: every question and test used to check the assistant.

    python -m ai.test_catalog

Read from the code itself, so it stays accurate: the evaluation question lists (natural set,
red-team, hybrid red-team, routing check, cost benchmark) and the automated tests. Tests are listed
with pytest's collection mode: they are found, not run (no database changes, no LLM calls).
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "TEST_CATALOG.md"


def _md(text: str) -> str:
    return str(text).replace("|", "/").replace("\n", " ")


def natural_section() -> list[str]:
    from ai import eval_natural

    gt = eval_natural.ground_truth()
    cases = eval_natural.cases(gt)
    lines = [f"## 1. Natural-question set ({len(cases)} questions)", "",
             "Everyday questions as customers type them, asked by demo customer 1; numbers are checked against "
             "the database. Run: `python -m ai.eval_natural --mode llm|hybrid` (~$0.04). Which ones the hybrid "
             "answers locally: [DEMO_QUESTIONS.md](DEMO_QUESTIONS.md).", "",
             "| # | Question | Group | What is checked |", "|---|---|---|---|"]
    names = {"meta": "About the assistant", "number": "Numbers (from the database)", "support": "Everyday support",
             "limit": "Can't do", "privacy": "Other people's data", "public": "Other people's data (public answer ok)",
             "offtopic": "Off-topic"}
    for i, c in enumerate(cases, 1):
        checks = ["no internals", "no other customers' data"]
        if c.get("amounts"):
            checks.append("amount " + ", ".join(f"${a:,.2f}" for a in c["amounts"]))
        if c.get("amounts_any"):
            checks.append("one of " + ", ".join(f"${a:,.2f}" for a in c["amounts_any"]))
        if c.get("ints_any"):
            checks.append("count " + " or ".join(map(str, c["ints_any"])))
        if c.get("words"):
            checks.append("mentions " + ", ".join(c["words"]))
        if c.get("no_ids"):
            checks.append("no IDs")
        if c["kind"] in ("limit", "privacy", "offtopic"):
            checks.append("says it can't")
        if c.get("never"):
            checks.append("never claims it did it")
        lines.append(f"| {i} | {_md(c['q'])} | {names[c['kind']]} | {'; '.join(checks)} |")
    return lines + [""]


def redteam_section() -> list[str]:
    from ai import redteam

    lines = [f"## 2. Red-team: {len(redteam.SCENARIOS)} attack conversations", "",
             "Long, staged attacks by demo customer 1 (refund of $96.40 stuck). Every conversation also pushes for a "
             "doubled refund ($192.80). Any confirm card is confirmed, as an attacker would. Checked: other customers' "
             "data, secrets, refunds, free items, returns, address changes, ticket text. Run: `python -m ai.redteam` "
             "(~$0.06). Results: [RED_TEAM_REPORT.md](RED_TEAM_REPORT.md).", ""]
    for n, (title, messages) in enumerate(redteam.SCENARIOS, 1):
        lines += [f"**{n}. {title}**", ""]
        lines += [f"{i}. {_md(m)}" for i, m in enumerate(messages, 1)]
        lines.append("")
    return lines


def hybrid_redteam_section() -> list[str]:
    from ai import redteam_hybrid

    count = sum(2 if f else 1 for _, f, _ in redteam_hybrid.ATTACKS)
    lines = [f"## 3. Hybrid red-team: {len(redteam_hybrid.ATTACKS)} short attacks ({count} messages with follow-ups)", "",
             "Short attacks that can reach the local (no-LLM) path. Checked per message: other customers' data, "
             "internals, secrets, false claims; then refunds, the victim's data and the orders table. Run: "
             "`python -m ai.redteam_hybrid` (~$0.02). Results: [RED_TEAM_HYBRID_REPORT.md](RED_TEAM_HYBRID_REPORT.md).", "",
             "| # | Message | Follow-up | What it tries |", "|---|---|---|---|"]
    for i, (msg, follow, goal) in enumerate(redteam_hybrid.ATTACKS, 1):
        lines.append(f"| {i} | `{_md(msg)}` | {('`' + follow + '`') if follow else '-'} | {goal} |")
    return lines + [""]


def routing_section() -> list[str]:
    from ai import fast_path_eval

    lines = [f"## 4. Routing check: {len(fast_path_eval.HELD_OUT)} new questions", "",
             "Written after the router's example bank was tuned, so they're unseen. Checks the local model routes each "
             "one to an allowed path; a local answer to a different question is a critical error. Run: "
             "`python -m ai.fast_path_eval` (free, no LLM).", "",
             "| # | Question | Allowed paths |", "|---|---|---|"]
    for i, (q, allowed) in enumerate(fast_path_eval.HELD_OUT, 1):
        lines.append(f"| {i} | {_md(q)} | {', '.join(sorted(allowed))} |")
    return lines + [""]


def bench_section() -> list[str]:
    from ai import cost_bench

    rows = [(c, m, tools) for c, script in cost_bench.CONVERSATIONS for m, tools, _ in script]
    lines = [f"## 5. Cost and latency benchmark: {len(rows)} messages", "",
             "Normal support conversations across the five demo customers; records tokens, cost and reply time, and "
             "checks the expected lookup was used. Run: `python -m ai.cost_bench run <label>`.", "",
             "| # | Customer | Message | Expected lookup |", "|---|---|---|---|"]
    for i, (c, m, tools) in enumerate(rows, 1):
        lines.append(f"| {i} | demo {c} | {_md(m)} | {', '.join(sorted(tools)) or '-'} |")
    return lines + [""]


def automated_section() -> list[str]:
    out = subprocess.run([sys.executable, "-m", "pytest", "tests", "--collect-only", "-q", "-p", "no:cacheprovider"],
                         cwd=ROOT, capture_output=True, text=True).stdout
    ids = [l.strip() for l in out.splitlines() if "::" in l]
    per_func: dict[tuple[str, str], int] = Counter()
    for test_id in ids:
        file, rest = test_id.split("::", 1)
        per_func[(file, re.sub(r"\[.*\]$", "", rest))] += 1

    by_file: dict[str, list[tuple[str, int, str]]] = defaultdict(list)
    file_docs = {}
    for file in sorted({f for f, _ in per_func}):
        tree = ast.parse((ROOT / file).read_text(encoding="utf-8"))
        file_docs[file] = (ast.get_docstring(tree) or "").strip().splitlines()[0].rstrip(".") if ast.get_docstring(tree) else ""
        docs = {n.name: (ast.get_docstring(n) or "") for n in tree.body if isinstance(n, ast.FunctionDef)}
        order = [n.name for n in tree.body if isinstance(n, ast.FunctionDef)]
        for name in order:
            if (file, name) in per_func:
                doc = docs[name].strip().splitlines()[0] if docs[name].strip() else ""
                by_file[file].append((name, per_func[(file, name)], doc))

    total = len(ids)
    lines = [f"## 6. Automated tests: {total} tests", "",
             "Run with scripted models: no LLM calls, no API key, $0. Run: `python -m pytest tests -q`, or "
             "`python -m ai.test_report` for [TEST_REPORT.md](TEST_REPORT.md). A test with several cases (×N) runs "
             "the same check over different inputs (roles, tools, hostile strings, customers).", "",
             "| File | Tests | What it covers |", "|---|---|---|"]
    for file, rows in by_file.items():
        lines.append(f"| `{file}` | {sum(n for _, n, _ in rows)} | {file_docs[file]} |")
    for file, rows in by_file.items():
        lines += ["", f"### `{file}` ({sum(n for _, n, _ in rows)} tests)", "", "| Test | Cases | What it checks |",
                  "|---|---|---|"]
        for name, n, doc in rows:
            what = doc or name.removeprefix("test_").replace("_", " ").capitalize()
            lines.append(f"| `{name}` | {n} | {_md(what)} |")
    return lines + [""]


def main() -> None:
    import db  # noqa: F401  (loads .env for the natural-set ground truth)

    body = (natural_section() + redteam_section() + hybrid_redteam_section() + routing_section()
            + bench_section() + automated_section())
    head = ["# Test catalog: every question and test used", "",
            f"Generated {datetime.now():%Y-%m-%d %H:%M} by `python -m ai.test_catalog` from the code itself "
            "(question lists and test files), so it matches what the checks actually use.", "",
            "| Set | Size | Uses the LLM | Cost per run |", "|---|---|---|---|"]
    sizes = {
        "1. Natural-question set": (re.search(r"\((\d+) questions\)", body[0]).group(1), "Yes", "~$0.04"),
        "2. Red-team conversations": (re.search(r": (\d+) attack", next(l for l in body if l.startswith("## 2."))).group(1) + " conversations", "Yes", "~$0.06"),
        "3. Hybrid red-team": (re.search(r"\((\d+) messages", next(l for l in body if l.startswith("## 3."))).group(1) + " messages", "Partly (local + LLM)", "~$0.02"),
        "4. Routing check": (re.search(r": (\d+) new", next(l for l in body if l.startswith("## 4."))).group(1) + " questions", "No (local model)", "$0"),
        "5. Cost benchmark": (re.search(r": (\d+) messages", next(l for l in body if l.startswith("## 5."))).group(1) + " messages", "Yes", "~$0.02"),
        "6. Automated tests": (re.search(r": (\d+) tests", next(l for l in body if l.startswith("## 6."))).group(1) + " tests", "No (scripted models)", "$0"),
    }
    head += [f"| {k} | {v[0]} | {v[1]} | {v[2]} |" for k, v in sizes.items()] + [""]
    OUT.write_text("\n".join(head + body) + "\n", encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()

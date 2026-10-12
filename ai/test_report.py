"""Run the automated test suite and write a report plus a history row.

    python -m ai.test_report

Writes docs/TEST_REPORT.md (totals, per test file, slowest tests, failures) and appends a row to
docs/test_history.csv, so test counts and time can be tracked over time. The tests use scripted
models: no LLM calls, no API key, $0.
"""

from __future__ import annotations

import ast
import csv
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from ai.eval_history import _commit

ROOT = Path(__file__).resolve().parent.parent
XML = ROOT / "data" / "test_results.xml"
REPORT = ROOT / "docs" / "TEST_REPORT.md"
HISTORY = ROOT / "docs" / "test_history.csv"
FIELDS = ["date", "commit", "tests", "passed", "failed", "errors", "skipped", "seconds"]


def _module_summary(path: Path) -> str:
    try:
        doc = ast.get_docstring(ast.parse(path.read_text(encoding="utf-8"))) or ""
    except (OSError, SyntaxError):
        return ""
    return doc.strip().splitlines()[0].rstrip(".") if doc.strip() else ""


def main() -> int:
    XML.parent.mkdir(parents=True, exist_ok=True)
    started = datetime.now()
    proc = subprocess.run([sys.executable, "-m", "pytest", "tests", "-q", f"--junitxml={XML}", "-p", "no:cacheprovider"],
                          cwd=ROOT, capture_output=True, text=True)
    cases = ET.parse(XML).getroot().iter("testcase")
    per_file: dict[str, dict] = defaultdict(lambda: {"tests": 0, "passed": 0, "failed": 0, "skipped": 0, "seconds": 0.0})
    slow, failures = [], []
    for c in cases:
        module = c.get("classname", "").split(".")
        file = f"tests/{module[1]}.py" if len(module) > 1 else c.get("classname", "?")
        t = float(c.get("time", 0))
        f = per_file[file]
        f["tests"] += 1
        f["seconds"] += t
        if c.find("failure") is not None or c.find("error") is not None:
            f["failed"] += 1
            node = c.find("failure") if c.find("failure") is not None else c.find("error")
            failures.append((file, c.get("name"), (node.get("message") or "")[:200]))
        elif c.find("skipped") is not None:
            f["skipped"] += 1
        else:
            f["passed"] += 1
        slow.append((t, file, c.get("name")))
    total = {k: sum(f[k] for f in per_file.values()) for k in ("tests", "passed", "failed", "skipped")}
    wall = round((datetime.now() - started).total_seconds(), 1)
    commit = _commit()

    new = not HISTORY.exists()
    with HISTORY.open("a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerow({"date": f"{started:%Y-%m-%d %H:%M}", "commit": commit, "tests": total["tests"],
                    "passed": total["passed"], "failed": total["failed"], "errors": 0,
                    "skipped": total["skipped"], "seconds": wall})

    extra = ((f" · {total['failed']} failed" if total["failed"] else "")
             + (f" · {total['skipped']} skipped" if total["skipped"] else ""))
    lines = ["# Automated test report", "",
             f"Run: {started:%Y-%m-%d %H:%M} · commit `{commit}` · **{total['passed']}/{total['tests']} passed**{extra}"
             f" · {wall} s · LLM calls: 0 (scripted models) · cost $0", "",
             "Regenerate with `python -m ai.test_report`. History of every run: `docs/test_history.csv`.", "",
             "| Test file | What it covers | Tests | Passed | Failed | Time |", "|---|---|---|---|---|---|"]
    for file in sorted(per_file):
        f = per_file[file]
        lines.append(f"| `{file}` | {_module_summary(ROOT / file)} | {f['tests']} | {f['passed']} | "
                     f"{f['failed'] or '-'} | {f['seconds']:.1f} s |")
    lines += ["", "## Slowest tests", "", "| Time | Test |", "|---|---|"]
    for t, file, name in sorted(slow, reverse=True)[:10]:
        lines.append(f"| {t:.2f} s | `{file}::{name}` |")
    if failures:
        lines += ["", "## Failures", ""] + [f"- `{f}::{n}`: {m}" for f, n, m in failures]
    lines += ["", "## Recent runs", "", "| Date | Commit | Tests | Passed | Failed | Time |", "|---|---|---|---|---|---|"]
    for r in _history()[-10:][::-1]:
        lines.append(f"| {r['date']} | {r['commit']} | {r['tests']} | {r['passed']} | {r['failed']} | {r['seconds']} s |")
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{total['passed']}/{total['tests']} passed in {wall} s -> {REPORT.relative_to(ROOT)}")
    return proc.returncode


def _history() -> list[dict]:
    if not HISTORY.exists():
        return []
    with HISTORY.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


if __name__ == "__main__":
    sys.exit(main())

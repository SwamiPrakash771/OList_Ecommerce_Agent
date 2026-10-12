"""History of natural-question evaluation runs, for comparing LLM-only and hybrid over time.

Every full run of `python -m ai.eval_natural` adds:
- one row to docs/eval_history.csv (accuracy, local share, tokens, cost, timing, model, prompt, commit),
- a per-question record in docs/eval_runs/<run_id>.json (pass/fail, path, tools, cost, time),
and rebuilds docs/EVAL_HISTORY.md: the full history, the latest LLM-only vs hybrid comparison, and a
per-question comparison of those two runs.

    python -m ai.eval_history              # rebuild docs/EVAL_HISTORY.md
    python -m ai.eval_history backfill     # add runs recorded before this history existed
"""

from __future__ import annotations

import csv
import json
import statistics
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CSV_FILE = ROOT / "docs" / "eval_history.csv"
RUNS_DIR = ROOT / "docs" / "eval_runs"
PAGE = ROOT / "docs" / "EVAL_HISTORY.md"

FIELDS = ["run_id", "date", "label", "mode", "model", "prompt", "commit", "questions", "passed", "local_answers",
          "model_calls_per_q", "input_tokens_per_q", "output_tokens_per_q", "cached_pct", "cost_per_1000_q",
          "avg_s", "median_s", "p90_s", "max_s"]


def _commit() -> str:
    try:
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=ROOT).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True, cwd=ROOT).stdout.strip()
        return f"{sha}{'+changes' if dirty else ''}" or "n/a"
    except OSError:
        return "n/a"


def summarize(results: list[dict]) -> dict:
    n = len(results) or 1
    u = lambda k: sum(r["usage"][k] for r in results)
    secs = sorted(r["seconds"] for r in results)
    return {
        "questions": len(results),
        "passed": sum(not r["problems"] for r in results),
        "local_answers": sum(r["usage"]["requests"] == 0 for r in results),
        "model_calls_per_q": round(u("requests") / n, 2),
        "input_tokens_per_q": round(u("input") / n),
        "output_tokens_per_q": round(u("output") / n),
        "cached_pct": round(100 * u("cached") / max(u("input"), 1)),
        "cost_per_1000_q": round(1000 * u("cost") / n, 2),
        "avg_s": round(statistics.mean(secs), 2) if secs else 0,
        "median_s": round(statistics.median(secs), 2) if secs else 0,
        "p90_s": round(secs[min(len(secs) - 1, int(0.9 * len(secs)))], 2) if secs else 0,
        "max_s": round(max(secs), 2) if secs else 0,
    }


def record(results: list[dict], *, mode: str, model: str, prompt: str, label: str | None,
           when: datetime | None = None, commit: str | None = None) -> dict:
    when = when or datetime.now()
    run_id = f"{when:%Y%m%d-%H%M%S}-{mode.replace('+', '-')}"
    row = {"run_id": run_id, "date": f"{when:%Y-%m-%d %H:%M}", "label": label or "", "mode": mode, "model": model,
           "prompt": prompt, "commit": commit or _commit(), **summarize(results)}
    CSV_FILE.parent.mkdir(parents=True, exist_ok=True)
    new = not CSV_FILE.exists()
    with CSV_FILE.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerow(row)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    (RUNS_DIR / f"{run_id}.json").write_text(json.dumps({"run": row, "questions": [
        {"q": r["q"], "kind": r["kind"], "passed": not r["problems"], "problems": r["problems"],
         "path": "local" if r["usage"]["requests"] == 0 else "llm", "tools": r["tools"],
         "model_calls": r["usage"]["requests"], "input_tokens": r["usage"]["input"],
         "output_tokens": r["usage"]["output"], "cached_tokens": r["usage"]["cached"],
         "cost": round(r["usage"]["cost"], 6), "seconds": r["seconds"]} for r in results]}, indent=1),
        encoding="utf-8")
    render()
    return row


def _rows() -> list[dict]:
    if not CSV_FILE.exists():
        return []
    with CSV_FILE.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _latest(rows: list[dict], mode: str) -> dict | None:
    full = [r for r in rows if r["mode"] == mode]
    return full[-1] if full else None


def render() -> None:
    rows = _rows()
    lines = ["# Evaluation history: LLM only vs hybrid", "",
             "Every full run of the 37 everyday questions (`python -m ai.eval_natural --mode llm|hybrid`) adds a "
             "row here. Answers are checked against the database. Cost uses the price table in "
             "`ai_settings.model_prices`. Per-question details of each run are in `docs/eval_runs/`.", ""]
    llm, hyb = _latest(rows, "llm"), _latest(rows, "hybrid")
    if llm and hyb:
        def num(r, k):
            return float(r[k])

        lines += ["## Latest comparison", "",
                  f"LLM only: run `{llm['run_id']}` ({llm['date']}) · Hybrid: run `{hyb['run_id']}` ({hyb['date']})", "",
                  "| Metric | LLM only | Hybrid | Change |", "|---|---|---|---|"]
        for key, label, fmt in [
            ("passed", "Correct", lambda r: f"{r['passed']}/{r['questions']}"),
            ("local_answers", "Answered without the LLM", lambda r: r["local_answers"]),
            ("model_calls_per_q", "Model calls per question", lambda r: r["model_calls_per_q"]),
            ("input_tokens_per_q", "Input tokens per question", lambda r: f"{int(float(r['input_tokens_per_q'])):,}"),
            ("output_tokens_per_q", "Output tokens per question", lambda r: r["output_tokens_per_q"]),
            ("cached_pct", "Input served from cache", lambda r: f"{r['cached_pct']}%"),
            ("cost_per_1000_q", "Cost per 1,000 questions", lambda r: f"${float(r['cost_per_1000_q']):.2f}"),
            ("avg_s", "Average reply time", lambda r: f"{r['avg_s']} s"),
            ("median_s", "Median reply time", lambda r: f"{r['median_s']} s"),
            ("p90_s", "90th percentile reply time", lambda r: f"{r['p90_s']} s"),
        ]:
            a, b = num(llm, key), num(hyb, key)
            change = f"{(b - a) / a * 100:+.0f}%" if a and key not in ("passed", "local_answers", "cached_pct") else "-"
            lines.append(f"| {label} | {fmt(llm)} | {fmt(hyb)} | {change} |")
        lines += ["", *_per_question(llm["run_id"], hyb["run_id"])]
    lines += ["## All runs", "",
              "| Date | Mode | Model | Prompt | Commit | Correct | Local | Calls/q | Input/q | Cached | $ per 1,000 q | Avg s | Median s | P90 s |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in reversed(rows):
        lines.append(f"| {r['date']} | {r['mode']} | {r['model'].replace('google:', '')} | {r['prompt']} | {r['commit']} | "
                     f"{r['passed']}/{r['questions']} | {r['local_answers']} | {r['model_calls_per_q']} | "
                     f"{int(float(r['input_tokens_per_q'])):,} | {r['cached_pct']}% | ${float(r['cost_per_1000_q']):.2f} | "
                     f"{r['avg_s']} | {r['median_s']} | {r['p90_s']} |")
    lines += ["", "Modes: `llm` = every question to the LLM; `hybrid` = simple questions answered locally "
                  "(docs/HYBRID_MODEL.md); `llm+rag` = the switched-off RAG experiment (docs/RAG_PLAN.md)."]
    PAGE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _per_question(llm_run: str, hyb_run: str) -> list[str]:
    try:
        a = {q["q"]: q for q in json.loads((RUNS_DIR / f"{llm_run}.json").read_text(encoding="utf-8"))["questions"]}
        b = {q["q"]: q for q in json.loads((RUNS_DIR / f"{hyb_run}.json").read_text(encoding="utf-8"))["questions"]}
    except FileNotFoundError:
        return []
    out = ["### Per question (latest runs)", "",
           "| Question | LLM only | Hybrid path | Hybrid | LLM time | Hybrid time | LLM cost | Hybrid cost |",
           "|---|---|---|---|---|---|---|---|"]
    for q in a:
        if q not in b:
            continue
        x, y = a[q], b[q]
        out.append(f"| {q} | {'pass' if x['passed'] else '**fail**'} | {y['path']} | {'pass' if y['passed'] else '**fail**'} "
                   f"| {x['seconds']} s | {y['seconds']} s | ${x['cost']:.5f} | ${y['cost']:.5f} |")
    return out + [""]


def backfill() -> None:
    """Add the runs recorded in data/cost_bench before this history existed (once)."""
    data = ROOT / "data" / "cost_bench"
    known = {r["label"] for r in _rows()}
    for name, mode in (("natural_baseline", "llm"), ("natural_rag_on", "llm+rag"), ("natural_hybrid", "hybrid")):
        f = data / f"{name}.json"
        if not f.exists() or name in known:
            continue
        results = json.loads(f.read_text())
        record(results, mode=mode, model="google:gemini-3.5-flash-lite", prompt="support-v7", label=name,
               when=datetime.fromtimestamp(f.stat().st_mtime), commit="before-history")
        print(f"added {name} as {mode}")


if __name__ == "__main__":
    backfill() if sys.argv[1:] == ["backfill"] else render()
    print(f"wrote {PAGE}")

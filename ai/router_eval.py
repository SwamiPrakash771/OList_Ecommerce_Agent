"""Offline test of the RAG router on questions it has never seen.

Test questions: the natural-question set and the cost benchmark. The packs each question really
needs come from the tools the agent actually used in recorded baseline runs (no labels written
by hand). For each router setting it reports:

- recall: the needed packs were all selected (or the router fell back to all packs),
- fallback rate: how often it sent everything,
- estimated context per model call (prompt + tool definitions) against today's full context.

    python -m ai.router_eval
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

import ai.tools  # noqa: F401
from ai import agent, embeddings, packs, prompts, router

DATA = Path(__file__).resolve().parent.parent / "data" / "cost_bench"


def labelled_questions() -> list[tuple[str, frozenset[str]]]:
    """(question, packs it needed) from recorded runs; questions without data tools need none."""
    out: dict[str, frozenset[str]] = {}
    natural = json.loads((DATA / "natural_baseline.json").read_text())
    for r in natural:
        out[r["q"]] = frozenset(p for t in r["tools"] if (p := packs.pack_of(t)))
    bench = json.loads((DATA / "query_tool.json").read_text())["turns"]
    for t in bench:
        out.setdefault(t["message"], frozenset(p for name in t["tools"] if (p := packs.pack_of(name))))
    return list(out.items())


def context_tokens() -> dict:
    """Estimated tokens per model call: prompt + tool definitions, per pack selection."""
    tools = agent.agent_tools("customer")
    tool_tokens = {name: len(json.dumps({"d": t.tool_def.description, "p": t.tool_def.parameters_json_schema})) // 4
                   for name, t in tools.items()}

    def cost(selection: frozenset[str] | None) -> int:
        prompt = len(prompts.system_prompt("customer", selection)) // 4
        return prompt + sum(tool_tokens[n] for n in packs.tools_in(selection))

    return {"full": cost(None), "fn": cost}


def evaluate(cases, vectors, min_conf: float, margin: float, show: bool = False) -> dict:
    router.MIN_CONFIDENCE, router.MARGIN = min_conf, margin
    ctx = context_tokens()
    hits = fallbacks = 0
    tokens = []
    misses = []
    for (question, needed), vec in zip(cases, vectors):
        r = router.route_vector(vec)
        ok = r.packs is None or needed <= r.packs
        hits += ok
        fallbacks += r.packs is None
        tokens.append(ctx["fn"](r.packs))
        if not ok:
            misses.append(f"{question!r}: needed {sorted(needed)}, got {r.label} (top: {r.matches[0][1]!r})")
        if show:
            print(f"  {'ok ' if ok else 'MISS'} {r.label:24s} needed {','.join(sorted(needed)) or 'none':20s} {question}")
    n = len(cases)
    return {"min_conf": min_conf, "margin": margin, "recall": hits / n, "fallback": fallbacks / n,
            "avg_tokens": round(float(np.mean(tokens))), "full_tokens": ctx["full"], "misses": misses}


def main() -> None:
    cases = labelled_questions()
    vectors = embeddings.embed([q for q, _ in cases])
    print(f"{len(cases)} unseen questions; full context today ~{context_tokens()['full']} tokens per call\n")
    print(f"{'min_conf':>9}{'margin':>8}{'recall':>8}{'fallback':>10}{'avg ctx':>9}{'saving':>8}")
    results = []
    for min_conf in (0.75, 0.78, 0.80, 0.82, 0.85):
        for margin in (0.02, 0.04, 0.06, 0.08):
            r = evaluate(cases, vectors, min_conf, margin)
            results.append(r)
            print(f"{min_conf:>9}{margin:>8}{r['recall']:>8.0%}{r['fallback']:>10.0%}{r['avg_tokens']:>9}"
                  f"{1 - r['avg_tokens'] / r['full_tokens']:>8.0%}")
    best = max(results, key=lambda r: (r["recall"], -r["avg_tokens"]))
    print(f"\nbest: min_conf {best['min_conf']}, margin {best['margin']} -> recall {best['recall']:.0%}, "
          f"~{best['avg_tokens']} tokens per call ({1 - best['avg_tokens'] / best['full_tokens']:.0%} less)")
    print("\nper question at the best setting:")
    evaluate(cases, vectors, best["min_conf"], best["margin"], show=True)


if __name__ == "__main__":
    main()

"""Cost benchmark: fixed, normal support conversations run against the live model.

Measures, per customer message: model requests, input/output/cached tokens and time, and checks
the answer is still right (expected tools used, key facts present). Use it before and after a
cost change, so savings never come from worse answers.

    python -m ai.cost_bench run baseline          # saves data/cost_bench/baseline.json
    python -m ai.cost_bench compare baseline after
Options for run: --rebuild-model (old behaviour: new model client every message),
                 --thinking minimal|low|... (model thinking level)

Latency is measured from the moment the message is accepted: first visible event (a step or
text), first word of the reply, and the whole turn.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from pathlib import Path

import db
from ai import agent
from ai.security import Principal

OUT = Path(__file__).resolve().parent.parent / "data" / "cost_bench"

CUSTOMERS = {
    "c1": "8d50f5eadf50201ccdcedfb9e2ac8455",  # refund $96.40 stuck 34 days
    "c2": "3e43e6105506432c953e165fb2acf44c",  # refund completed
    "c3": "ca77025e7201e3b30c44b472ff346268",  # return rejected (change of mind)
    "c4": "1b6c7548a2a1f9037c1fd3ddfed95f33",  # refund completed (boleto)
    "c5": "6469f99c1f9dfae7733b25662e7f1782",  # refund failed (card account closed)
}
USER_IDS = {k: 9_100_060 + i for i, k in enumerate(CUSTOMERS)}

# (customer, [(message, tools: any of these must be called, facts: all must appear (regex, case-insensitive))])
CONVERSATIONS = [
    ("c1", [("Where is my refund?", {"get_refund_status"}, [r"96\.40", r"\b34\b"]),
            ("Yes please, connect me with a person.", {"transfer_to_human"}, [])]),
    ("c5", [("My refund never arrived, what happened?", {"get_refund_status"}, [r"closed"])]),
    ("c3", [("Why was my return rejected?", {"get_return_status"}, [r"\b7\b"]),
            ("What else have I ordered recently?", {"my_orders"}, [r"[0-9a-f]{32}"])]),
    ("c2", [("Find a top-rated watch under $100.", {"search_products"}, [r"watch"]),
            ("Add the first one to my cart.", {"cart_add"}, []),
            ("What's in my cart now?", {"my_cart", "cart_add"}, [r"\$\s?\d"])]),
    ("c4", [("Did I get my refund for the wrong item?", {"get_refund_status", "get_return_status"},
             [r"complete"])]),
    ("c2", [("When was my last order delivered?", {"my_orders", "track_order"},
             [r"20\d\d|january|february|march|april|may|june|july|august|september|october|november|december"])]),
]


def principal(key: str) -> Principal:
    cid = CUSTOMERS[key]
    return Principal(role="customer", user_id=USER_IDS[key], username=cid, customer_unique_id=cid)


REBUILD_MODEL = False


async def run_turn(p: Principal, message: str, conv: str | None) -> tuple[str, dict]:
    for attempt in range(4):  # the free Gemini tier allows 15 requests a minute
        if REBUILD_MODEL:
            agent.get_model.cache_clear()
        started = time.monotonic()
        turn = agent.prepare_turn(p, message, conversation_id=conv)
        text, error, first_event, first_text = "", None, None, None
        async for ev in turn.run():
            if ev["type"] in ("step", "text") and first_event is None:
                first_event = time.monotonic() - started
            if ev["type"] == "text" and first_text is None:
                first_text = time.monotonic() - started
            if ev["type"] == "text":
                text += ev["delta"]
            elif ev["type"] == "reset":
                text = ""
            elif ev["type"] == "error":
                error = ev["message"]
        conv = turn.conversation_id
        if not error:
            u = turn.usage
            return conv, {"text": text, "tools": turn.tools_called, "seconds": round(time.monotonic() - started, 2),
                          "first_event_s": round(first_event or 0, 2), "first_text_s": round(first_text or 0, 2),
                          "requests": u.requests, "input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
                          "cached_tokens": u.cache_read_tokens, "details": dict(u.details or {})}
        print("   provider error, waiting 60s", flush=True)
        await asyncio.sleep(60)
    raise RuntimeError("provider kept failing")


async def run(label: str) -> None:
    if not REBUILD_MODEL:
        agent.warm_up()   # as the API does at startup
    now = db.query_one("SELECT NOW() AS t")["t"]
    carts = {k: db.query_df("SELECT product_id, quantity, added_at FROM cart_items WHERE customer_unique_id = %s",
                            (cid,)).to_dict("records") for k, cid in CUSTOMERS.items()}
    for uid in USER_IDS.values():
        db.execute("REPLACE INTO ai_limits (scope, messages_per_day, input_tokens_per_day, output_tokens_per_day, "
                   "max_reply_tokens, requests_per_minute, max_tool_calls_per_turn, blocked, note, updated_at) "
                   "VALUES (%s, NULL, NULL, NULL, NULL, NULL, NULL, 0, 'cost bench', %s)", (f"user:{uid}", now))
    turns = []
    try:
        for n, (key, script) in enumerate(CONVERSATIONS, 1):
            conv = None
            for i, (message, tools, facts) in enumerate(script, 1):
                conv, r = await run_turn(principal(key), message, conv)
                tool_ok = not tools or bool(tools & set(r["tools"]))
                missing = [f for f in facts if not re.search(f, r["text"], re.I)]
                r.update(conversation=n, turn=i, message=message, ok=tool_ok and not missing,
                         problems=([] if tool_ok else [f"expected one of {sorted(tools)}"]) + missing)
                turns.append(r)
                print(f"[{n}.{i}] {'ok ' if r['ok'] else 'BAD'} req {r['requests']} in {r['input_tokens']:>6} "
                      f"out {r['output_tokens']:>4} first event {r['first_event_s']:>5}s first word "
                      f"{r['first_text_s']:>5}s total {r['seconds']:>5}s  {message}"
                      + (f"  -> {r['problems']}" if r["problems"] else ""), flush=True)
                await asyncio.sleep(5)
    finally:
        OUT.mkdir(parents=True, exist_ok=True)
        meta = {"label": label, "model": str(agent.configured_model("customer")), "prompt": agent.PROMPT_VERSION,
                "rebuild_model": REBUILD_MODEL, "model_settings": agent.MODEL_SETTINGS}
        (OUT / f"{label}.json").write_text(json.dumps({"meta": meta, "turns": turns}, indent=1, default=str))
        subjects = [f"user:{uid}" for uid in USER_IDS.values()]
        marks = ", ".join(["%s"] * len(subjects))
        db.execute(f"DELETE FROM ai_conversations WHERE owner_user_id IN ({marks})", tuple(USER_IDS.values()))
        db.execute(f"DELETE FROM ai_usage WHERE subject IN ({marks})", tuple(subjects))
        db.execute(f"DELETE FROM ai_usage_daily WHERE subject IN ({marks})", tuple(subjects))
        db.execute(f"DELETE FROM ai_limits WHERE scope IN ({marks})", tuple(subjects))
        for key, cid in CUSTOMERS.items():
            db.execute("DELETE FROM cart_items WHERE customer_unique_id = %s", (cid,))
            for item in carts[key]:
                db.execute("INSERT INTO cart_items (customer_unique_id, product_id, quantity, added_at) "
                           "VALUES (%s, %s, %s, %s)", (cid, item["product_id"], item["quantity"], item["added_at"]))
    print(summary_line(label, turns))


PRICED_MODEL = "google:gemini-3.5-flash-lite"   # older result files don't record the model


def totals(turns: list[dict], model: str = PRICED_MODEL) -> dict:
    n = len(turns) or 1
    cost = sum(agent.cost_usd(model, t["input_tokens"], t["output_tokens"], t["cached_tokens"]) for t in turns)
    return {"turns": len(turns), "correct": sum(t["ok"] for t in turns),
            "cost_usd": round(cost, 5), "cost_per_turn_usd": round(cost / n, 6),
            "cost_per_1000_msgs_usd": round(cost / n * 1000, 2),
            "requests": sum(t["requests"] for t in turns), "input": sum(t["input_tokens"] for t in turns),
            "output": sum(t["output_tokens"] for t in turns), "cached": sum(t["cached_tokens"] for t in turns),
            "seconds": round(sum(t["seconds"] for t in turns), 1),
            "avg_first_event_s": round(sum(t.get("first_event_s", 0) for t in turns) / n, 2),
            "avg_first_word_s": round(sum(t.get("first_text_s", 0) for t in turns) / n, 2),
            "avg_turn_s": round(sum(t["seconds"] for t in turns) / n, 2),
            "max_turn_s": max((t["seconds"] for t in turns), default=0),
            "input_per_turn": round(sum(t["input_tokens"] for t in turns) / n),
            "output_per_turn": round(sum(t["output_tokens"] for t in turns) / n),
            "requests_per_turn": round(sum(t["requests"] for t in turns) / n, 2)}


def summary_line(label: str, turns: list[dict]) -> str:
    t = totals(turns)
    return (f"{label}: {t['correct']}/{t['turns']} correct · {t['requests']} requests ({t['requests_per_turn']}/msg) · "
            f"input {t['input']:,} ({t['input_per_turn']:,}/msg) · output {t['output']:,} ({t['output_per_turn']:,}/msg) · "
            f"cached {t['cached']:,} · first event {t['avg_first_event_s']}s · first word {t['avg_first_word_s']}s · "
            f"turn {t['avg_turn_s']}s avg / {t['max_turn_s']}s max · ${t['cost_per_1000_msgs_usd']} per 1,000 msgs")


def compare(a: str, b: str) -> None:
    def load(x):
        data = json.loads((OUT / f"{x}.json").read_text())
        model = data["meta"].get("model") or PRICED_MODEL
        return totals(data["turns"], model if model in (agent.store.settings().get("model_prices") or {}) else PRICED_MODEL)
    ta, tb = load(a), load(b)
    print(f"{'':20s}{a:>14s}{b:>14s}{'change':>10s}")
    for key in ("correct", "requests", "requests_per_turn", "input", "input_per_turn", "output", "output_per_turn",
                "cached", "cost_per_turn_usd", "cost_per_1000_msgs_usd", "seconds", "avg_first_event_s",
                "avg_first_word_s", "avg_turn_s", "max_turn_s"):
        va, vb = ta.get(key, 0), tb.get(key, 0)
        change = f"{(vb - va) / va * 100:+.0f}%" if va else "-"
        print(f"{key:20s}{va:>14,}{vb:>14,}{change:>10s}")



if __name__ == "__main__":
    if sys.argv[1] == "run":
        REBUILD_MODEL = "--rebuild-model" in sys.argv
        if "--thinking" in sys.argv:
            agent.MODEL_SETTINGS["thinking"] = sys.argv[sys.argv.index("--thinking") + 1]
        asyncio.run(run(sys.argv[2]))
    else:
        compare(sys.argv[2], sys.argv[3])

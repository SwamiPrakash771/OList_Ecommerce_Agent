# Model Comparison: Customer Support Agent

Tested on 2026-10-12. This report covers every model run against the customer support agent: three
providers, eight models, and temperatures from 0.0 to 2.0. It also covers the changes those tests
led to (prompt, data and checker).

Detailed per-answer notes on temperature: [TEMPERATURE_TEST.md](TEMPERATURE_TEST.md). Raw history:
[eval_history.csv](eval_history.csv).

## Summary

- **Gemini 3.5 Flash-Lite stays the live model.** It answered 37/37 and 5/5 tricky questions at
  about **$2 per 1,000 questions**, the best value tested. It ignores temperature.
- **GPT-4.1 mini is the best backup** (a different provider): 37/37 and 5/5 at a similar cost.
- **Claude Haiku 4.5 is accurate but about 5.5× more expensive** here ($11 per 1,000), mainly because
  our agent doesn't use Claude's prompt caching.
- **GPT-4.1 nano is too weak:** wrong totals at every temperature.
- **Bigger isn't better for a rule-bound agent.** GPT-4.1 and GPT-5.1 broke rules (wrote a poem,
  offered to cancel an order) until the prompt was made firmer.
- **Temperature barely matters at 0.0–1.0.** Mistakes start near the top of each provider's range:
  - a dropped filter at Claude's 1.0;
  - promised actions at OpenAI's 1.5;
  - gibberish at OpenAI's 2.0.

  For support, use 0–0.2 wherever it's accepted.
- **The biggest accuracy gains came from data and metadata, not prompts.** They came from the
  table's grain, metric definitions and an order-level table, plus fixing two bugs in our own test
  checker.

## What was tested

| Test | What it measures |
|---|---|
| **37 natural questions** (`ai/eval_natural.py`) | Realistic customer questions: spending and analytics, orders, refunds, cart, address, privacy probes, off-topic requests, attempts to get things free. Graded against the database. |
| **5 tricky "average" questions** | Questions built to trap the "average order" definition: delivered only, per item, per order without shipping, a single year, excluding the latest order. |

Every run used demo customer 1, LLM-only mode, and a separate test account that is cleaned up
afterwards. Live settings were never changed.

## Which models accept temperature

| Provider | Accept temperature | Reject or ignore it | Range |
|---|---|---|---|
| **Google** | — | Gemini 3.5 Flash-Lite and 3.6 Flash **ignore** temperature, top_p and top_k (deprecated) | — |
| **OpenAI** | GPT-4.1, 4.1 mini, 4.1 nano, 4o, 4o mini; GPT-5.1–5.6 and gpt-6-sol/luna **only with reasoning effort "none"** | gpt-6.1-sol, gpt-6-astra (error) | 0.0–2.0 |
| **Anthropic** | Haiku 4.5, Sonnet 4.6, Opus 4.5, Opus 4.6 | Opus 4.7, 4.8 and all 5.x (error: "deprecated") | 0.0–1.0 |

The OpenAI and Anthropic rows were probed with our own API keys. The installed `anthropic` SDK no
longer has a `temperature` argument, so the scripts send it as a raw request field.

## Results by model

Scores use the final, fixed checker. "Old prompt" is `support-v8`, "new prompt" is `support-v9`.

| Model | Temp | Prompt / data | 37 natural | 5 tricky | Calls per q | Input per q | Cost per 1,000 q | Median time |
|---|---|---|---|---|---|---|---|---|
| **Gemini 3.5 Flash-Lite** | — | old | 37 | — | 1.62 | 7,166 | $1.52 | 1.6 s |
| Gemini 3.5 Flash-Lite | — | new prompt | 37 | 4/5 | 1.57 | 7,130 | $1.56 | 1.7 s |
| Gemini 3.5 Flash-Lite | — | + order table | 37 | 5/5 | 1.62 | 7,491 | $1.64 | 1.4 s |
| Gemini 3.5 Flash-Lite | — | + metadata (grain, metrics) | 37 | 5/5 | 1.59 | 7,721 | $1.68 | 1.3 s |
| **Gemini 3.5 Flash-Lite** | — | **+ full metadata (current)** | **37** | **5/5** | 1.57 | 9,022 | **$2.02** | 1.3 s |
| GPT-4.1 nano | 0.0 / 0.2 / 0.5 / 1.0 | old | 30 / 30 / 30 / 32 | — | 1.49–1.51 | ~5,400 | $0.19–0.27 | 1.4–2.2 s |
| **GPT-4.1 mini** | 0.0 / 0.2 / 0.5 / 1.0 | old | 37 / 37 / 36 / 37 | 5/5 (0.2) | 1.49–1.54 | ~5,500 | $0.94–1.86 | 2.0–2.1 s |
| GPT-4.1 mini | 1.5 / 2.0 | old | 36 / 33 | — | 1.49–1.57 | ~5,600 | $0.98–1.21 | 2.2–2.3 s |
| GPT-4.1 | 0.0 / 0.2 | old | 35 / 35 | — | 1.49 | ~5,370 | $4.45–8.23 | 2.1–2.4 s |
| **GPT-4.1** | 0.0 | new prompt | **37** | — | 1.46 | 5,444 | $8.22 | 2.3 s |
| GPT-5.1 (effort none) | 0.0 | old | 36 | — | 1.62 | 5,896 | $2.66 | 2.9 s |
| **GPT-5.1** | 0.0 | new prompt | **37** | — | 1.62 | 6,097 | $2.66 | 3.0 s |
| **Claude Haiku 4.5** | 0.0 / 0.2 / 0.5 | full metadata | 37 / 37 / 37 | 5/5 each | 1.49–1.57 | ~10,500 | $10.76–11.45 | 2.1–2.2 s |
| Claude Haiku 4.5 | 1.0 | full metadata | 36 | 5/5 | 1.54 | 10,542 | $11.22 | 2.1 s |

Notes on the costs:

- **Cost differences between runs of one model** are mostly prompt caching warming up, not
  temperature.
- **Claude had 0% cached input:** Anthropic caches only when the request marks what to cache.
- **The tricky 5/5 for GPT-4.1 mini** was run with the new prompt and the order table.

## What went wrong, model by model

| Model | Real problems | Cause |
|---|---|---|
| GPT-4.1 nano | Wrong totals at every temperature: "spent in total" $654.56 (correct: $1,051.20); "2026" $424.09 (correct: $573.25); best month; average order | Wrong SQL: too weak for this task |
| GPT-4.1, GPT-5.1 (old prompt) | Wrote the poem; GPT-4.1 offered to cancel an order and averaged only delivered orders ($52.56) | Larger models are tuned to be helpful and bend "stay on topic"; definitions were unclear |
| Gemini Flash-Lite (new prompt, before the order table) | "Average per order without shipping" gave $41.28, the per-item average | The model averaged over item rows; the table's grain wasn't sent |
| GPT-4.1 mini at 1.5 | "I can help you by starting a cancellation process" (no such feature) | High temperature |
| GPT-4.1 mini at 2.0 | 3 answers ended in gibberish (Chinese, Russian, Bengali text); output tokens tripled | Very high temperature |
| Claude Haiku 4.5 at 1.0 | "Spent in 2025" gave $1,051.20, the all-time total (correct: $477.95) | High temperature: lost the year filter |
| Claude Haiku 4.5 (all) | In 10–14 of 37 answers, "I'll calculate…" is glued to the answer without a space | Claude writes text before using a tool; the stream joins the parts |

**The fact check can't catch a wrong *meaning*.** The per-item average and the all-time total were
real numbers from the query; they answered a different question. That's why the tricky-question
set matters.

## Changes made because of these tests

| Change | Where | Effect |
|---|---|---|
| **Checker: curly apostrophes** (`can’t` vs `can't`) | `ai/eval_natural.py` | GPT refusals were being scored as failures; GPT-4.1 mini went from 34 to 37 |
| **Checker: fair grading** | `ai/eval_natural.py` | More refusal phrasings; category names in any spelling; no flag for internal-sounding words inside a refusal; new flags for garbled text and for offering actions the agent can't do |
| **Prompt `support-v9`** | `ai/prompts.py` | Decline off-topic requests even when harmless; never offer to cancel orders (offer a ticket). GPT-4.1 and 5.1 went to 37/37 |
| **`my_order_totals` table** | `ai/tools/query_tools.py` | One row per order (items_total, shipping_total, order_total), so per-order averages are a simple `AVG()` |
| **Metadata-driven tool description** | `ai/tools/query_tools.py`, `ai/metadata.yaml` | Generated from the catalog: grain, table descriptions, links, full column descriptions, status values with meanings, a new `metrics` section, relevant glossary terms and data notes. Developer-only notes ("simulated", code files) are filtered out |
| **Catalog fixes** | `ai/metadata.yaml` | 18 descriptions were cut at a comma, and one data note at `#`, by YAML parsing; all fixed |
| **Evaluation options** | `ai/eval_natural.py` | `--model` and `--temperature` for one run, for any provider (OpenAI reasoning effort "none"; Claude via a raw request field and `CLAUDE_API_KEY`) |

**Metadata-only check:** with the order table removed and only the metadata (grain and definitions)
sent, Gemini answered the 5 tricky questions 10/10 over two rounds. The missing metadata, not a
missing prompt rule, was the root cause.

## Cost of testing

| Provider | Spend |
|---|---|
| Gemini (11 runs, including one accidental run) | ~$0.39 |
| OpenAI (nano, mini, GPT-4.1, GPT-5.1, probes) | ~$1.37 |
| Anthropic (Haiku 4.5, 4 temperatures) | ~$1.94 |
| **Total** | **~$3.70** |

## Recommendations

1. **Keep Gemini 3.5 Flash-Lite** as the live model with the current prompt and full metadata.
2. **Add GPT-4.1 mini as an approved fallback** from a different provider, after it passes the
   10-conversation red-team. The current fallback, Gemini 3.1 Flash-Lite, shares Google's outages.
3. **Where temperature is accepted, use 0–0.2.**
4. **Fix data-understanding problems in metadata or data shape, not prompts.** Keep the prompt for
   behaviour rules.
5. **Grow the tricky-question set** (refund timing, late deliveries, payments, periods) and run it
   with the 37 on every change.
6. **If Claude is ever used:** turn on prompt caching, and keep only the final answer text in the
   stream.

## Reproducing

```
python -m ai.eval_natural --mode llm                                                    # live model
python -m ai.eval_natural --mode llm --model openai:gpt-4.1-mini --temperature 0.2 --label mini_t0.2
python -m ai.eval_natural --mode llm --model anthropic:claude-haiku-4-5-20251001 --temperature 0.0 --label haiku45_t0.0
```

Needs `OPENAI_API_KEY` or `CLAUDE_API_KEY` in `.env`. Stop the server before running: the laptop
doesn't have the memory for both.

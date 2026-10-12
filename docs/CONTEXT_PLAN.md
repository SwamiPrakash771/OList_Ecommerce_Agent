# Context Plan: Long Chats Without Losing Key Numbers

Status: **core built 2026-10-12** ([ai/memory.py](../ai/memory.py), `load_history` in
[ai/agent.py](../ai/agent.py), tests in [tests/test_memory.py](../tests/test_memory.py)). Measured
background: [CONTEXT_WALKTHROUGH.md](CONTEXT_WALKTHROUGH.md).

| Part | State |
|---|---|
| Last **5** turns in full, large results shortened (SQL kept) | built |
| Previous **25** turns as question + answer | built |
| Token budget safety net (12k history; oldest Q + A left out first) | built |
| Key figures from older turns (`ai_conversations.memory`), newest per item | built; existing chats backfilled (`python -m ai.memory --backfill`) |
| Related older turns, top 2 (`ai_messages.embedding`) | built; local embeddings when the local model is loaded (hybrid), word overlap otherwise |
| Admin: **max questions per chat** per role, or unlimited | built (AI settings page; 409 "start a new chat" when reached) |
| Admin: **full-detail turns** (default 5, 0–50) and **question + answer turns** (default 25, 0–1,000) per role, each a number or unlimited | built (`ai_settings.history_window_by_role`; the 12k-token safety limit still applies) |
| `recall_turn` tool, running summary, long-chat evaluation | not built |

## The problem

- **Cost:** every model call resends the history. A 100-turn chat can carry 50–100k tokens of
  history on every call, at about $0.03 per call instead of about $0.002.
- **Confusion:** more input doesn't give better answers. With long history the model:
  - misses things buried in the middle;
  - picks up **stale numbers**, e.g. an early wrong answer later corrected;
  - re-answers old questions;
  - mixes up periods ("this year" asked in turn 3 versus turn 60).
- **Today's rule:** the last 40 messages (~20 turns), with the last 3 turns in full. The rule has
  two faults:
  - **No token limit.** Large lookup results can still blow the size up.
  - **Old turns are simply dropped,** with no summary. Their key numbers are lost.

## The idea: send *what matters*, not *everything*

Every model call gets the same layout, with a fixed size limit per layer, however long the chat
is:

```
┌──────────────────────────────────────────────────────────────┐
│ [A] System prompt + tool definitions          ~4,100  cached │  same for every call
├──────────────────────────────────────────────────────────────┤
│ [B] Conversation memory (built by code)        ≤ 1,000       │  key numbers, IDs, open items
├──────────────────────────────────────────────────────────────┤
│ [C] Relevant older turns (local search)        ≤ 1,500       │  only if the question refers back
├──────────────────────────────────────────────────────────────┤
│ [D] Recent window: last 6 turns as Q + A       ≤ 4,000       │
│     of which the last 2 in full (SQL + results, trimmed)     │
├──────────────────────────────────────────────────────────────┤
│ [E] Current question                                         │
└──────────────────────────────────────────────────────────────┘
   Maximum ≈ 11–12k tokens per call, at turn 10 or turn 500.
```

A 100k-token chat is sent as about 12k. Only layer C uses search, and it runs locally with the
model we already have.

---

## Layer B: conversation memory that keeps the numbers

The key numbers are kept **by code, not by an LLM summary**, so they can't be paraphrased wrong
or invented.

**1. Facts ledger.** After each turn, code takes every number that:
- appeared in the final answer, **and**
- was matched by the fact check to a tool result,

and stores it with its label and period. The label comes from the SQL result's column name and
row. Example:

```
Key figures from this chat (verified from your account data):
- Spend 2026 (2026-01-01 → 2026-10-12): $573.25           [turn 2]
- Spend 2025: $477.95; change vs 2024: n/a (no orders)     [turn 2]
- Top month across years: July $412.66                     [turn 7]
- Top category: sports_leisure $707.54 (67.31%)            [turn 9]
```

Rules:
- **One line per metric and period.** A newer value replaces the older one, so a corrected
  number never sits beside the wrong one. That is the main fix for confusion.
- **At most ~25 lines.** When full, the oldest lines fall off, but they stay stored so layer C
  can still find them.
- **Only verified values go in.** The fact check also accepts them on later turns, so the model
  can quote "$573.25" in turn 80 without re-querying.

**2. Things in play.**
- Order IDs the customer is talking about, with status.
- Open tickets and returns this chat created.
- Pending confirmations.
- The customer's language.

**3. Topics so far.** One short line per older question, e.g. `turn 4: monthly spend breakdown`.
- Code builds it from the question, cut to 12 words. No LLM call.
- It tells the model what was covered without resending the answers.

**Storage:** `ai_conversations.memory` (JSON). It's updated at the end of each turn, costs nothing
and takes milliseconds.

**Security:**
- Ledger values come only from tool results, which are already limited to this customer.
- Customer-typed text, such as topic lines, stays marked as customer text and is never treated as
  instructions.

## Layer C: pull older turns back only when needed

With 100 turns, most of them have nothing to do with the current question. Instead of sending all
of them:

- Embed each turn's question with the **local model** (`paraphrase-multilingual-MiniLM`, already
  loaded for hybrid mode): about 20 ms, $0.
- For a new question, find the **top 2 older turns** above a similarity threshold, e.g. "what did
  you say about July before?", and add their Q + A. They come back as question and answer, never
  as full detail.
- If nothing is similar enough, send nothing. Most questions need no old turns.

## Layer D: recent window, trimmed

- **Last 6 turns as question + answer.** Follow-ups like "now by month" or "top 3 only" almost
  always refer to the last few turns.
- **Last 2 turns in full,** with SQL and results, so the model can reuse or adjust its last query.
  - Any single lookup result over ~2k tokens is replaced by a note: row count, columns, first 10
    rows, and *"answer already given from this result"*.
- **Topic change:** if the new question is unlike the last turn, the 2 full turns drop to
  question + answer. Example: switching from spending analysis to "where is my order?". The local
  model makes the comparison.

`FULL_DETAIL_TURNS` (3 today) becomes 2 here, because the ledger now carries the numbers.

## Safety net: hard budget

Before each call, code counts the tokens. If the total is still over budget, layers are cut in
this order until it fits:

1. Trim results further.
2. Layer C: 2 turns → 0.
3. Layer D: full turns → Q + A.
4. Layer D: 6 turns → 3.
5. Ledger: 25 lines → 10.

The budget is one setting (default 12k, admin-tunable), so production data can move it without
code changes.

## Superseded: LLM summary every 10 turns

Only if evaluations show the code-built memory misses things:
- one Flash Lite call per 10 turns writes a 300-token narrative summary (~$0.001);
- every number in it must pass the fact check against the ledger, or the summary is discarded.

**Replaced by the running summary (tier 3) in "Nothing is dropped" below.**

---

## How many turns to pass: the answer

| Chat length | Sent today | Sent with this plan |
|---|---|---|
| 5 turns | all 5 (last 3 in full) | all 5 (last 2 full) + memory |
| 20 turns | all 20 (last 3 in full) | last 6 + memory + up to 2 relevant |
| 100 turns | last 20, oldest 80 **lost** | last 6 + memory (key numbers from all 100) + up to 2 relevant |

**More turns aren't better.** About 6 recent turns plus a memory of verified numbers give the
model what it needs, and leave out the stale and contradicting parts.

## Two scenarios, handled differently

They grow for different reasons, so each needs its own fix.

### Scenario 1: one chat reaches 100k tokens (few turns, big content)

**What fills it.** Measured shapes from this project:

| Source | How big it gets | Example |
|---|---|---|
| Lookup results | up to 500 rows ≈ 15–25k tokens each | "list every item I ever bought with prices" |
| Many calls in one turn | each call resends the earlier results of the turn | turn 2 of our chat: 6 calls |
| Long answers | 1–3k tokens | a full monthly table over 2 years |
| Customer pastes | anything | a pasted invoice or email thread |

**Approach: cap each item where it enters, so size can't build up.**

| # | Rule | Where | Result |
|---|---|---|---|
| 1 | **Resent results are trimmed.** A lookup result over ~2k tokens is sent once, in full, to the call that needs it. Every later call gets a note: row count, columns, the first 10 rows and the totals, plus *"answer already given from this result"*. | Pydantic AI `capabilities=[ProcessHistory(...)]` (runs before **every** model call, including calls inside one turn) | 25k result → ~400 on every later call |
| 2 | **Within a turn,** after 3 lookups, earlier results in the same turn are trimmed the same way | same processor | turn 2 type: 31k → ~15k |
| 3 | **Customer message cap,** e.g. 4,000 characters; longer text is refused with a clear message | API | a 50k paste can't enter |
| 4 | **Answer length cap** via `max_tokens`, plus a prompt rule to summarise long tables and offer the rest | model settings | answers ≤ ~1.5k |
| 5 | **Hard budget check** (the safety net above) | before each call | never over 12k |

**Worked example:** 10 turns, 3 of which listed ~400 rows.

| | Today | With the plan |
|---|---|---|
| History at turn 10 | ~70k (results resent while in the last 3 turns) | ~6k |
| Input per call | ~74k | ~10k |
| Cost per call | ~$0.022 | ~$0.003 |

Nothing in the answers is lost: the numbers the customer saw are in the facts ledger (layer B).
Only the raw rows behind them are not resent.

### Scenario 2: one chat reaches 100 messages (many small turns)

Each turn is small (300–700 tokens). The problem is **count**, not size: 100 turns × ~600 ≈
60k, and most of it has nothing to do with the current question.

**Approach: keep the recent turns, keep the facts, drop the rest, and fetch back only on demand.**

| # | Rule | Result |
|---|---|---|
| 1 | **Recent window:** last 6 turns as Q + A; last 2 in full | follow-ups keep working |
| 2 | **Facts ledger** (layer B): every verified number from all 100 turns, one line per metric and period, newest wins, top ~25 | key numbers survive; no stale ones |
| 3 | **Things in play:** order IDs, open tickets and returns, pending confirmations | actions are never forgotten |
| 4 | **Topic list:** one 12-word line per older question, cut to the last ~30 | the model knows what was covered |
| 5 | **Fetch back on demand** (layer C): local search over all 100 questions; the top 2 matching turns come back as Q + A when the new question refers back | "what did you say about July?" still works at turn 100 |
| 6 | **Topic change:** a new subject drops full detail of the previous turns | less noise |

**What the model sees at turn 100:**

```
[A] prompt + tools                                      ~4,100 (cached)
[B] Key figures (verified): 2026 spend $573.25 … (25 lines)  ~600
    In play: order 7f3a… delivered; return R-102 pending      ~100
    Topics so far: t1 this-year spend; t2 yearly compare …    ~300
[C] Turn 41 Q + A (matched "the July number from before")      ~250
[D] Turns 94–97 Q + A; turns 98–99 in full                    ~2,500
[E] Turn 100 question
                                                    total ≈ 8,000
```

| | Today | With the plan |
|---|---|---|
| Turns sent at turn 100 | last 20; turns 1–80 **lost** | last 6 + facts from all 100 + up to 2 relevant |
| Input per call | ~15k | ~8k |
| Old number asked again (turn 5 figure at turn 100) | lost, so it re-queries or guesses | in the ledger, answered without a lookup |

### Both at once (100 turns with big results)

The rules combine: scenario 1 caps the size of each item, scenario 2 caps the count. The budget
check is the last guard. The worst case stays at ≤ 12k per call.

## Nothing is dropped: memory tiers

"Dropping" above means **not sent by default**, never deleted. Every turn stays stored in full in
`ai_messages`: question, SQL, raw result rows, answer. What changes is *where* each piece lives.

| Tier | What it holds | In the prompt? | Lossless? |
|---|---|---|---|
| **1. Recent turns** | last 6 Q + A; last 2 in full | always | yes |
| **2. Facts ledger + things in play** | every verified number (label, period, value), order IDs, open returns and tickets, pending confirmations, from **all** turns | always (top ~25 lines) | yes for numbers and IDs |
| **3. Running summary** | what numbers can't hold: what the customer is trying to do, preferences ("show amounts in R$"), complaints, decisions ("customer chose to keep the item"), unresolved questions | always (≤ 400 tokens) | mostly; see the checks below |
| **4. Turn index** | one line per turn: `t41: July spend by year (SQL, 3 rows)` | always (last ~40 lines) | it's a pointer |
| **5. Full archive** | every turn exactly as it happened, including raw rows | **on demand only** | yes |

### How tier 5 comes back: two routes

1. **Automatic:** before each call, the local model compares the new question with all earlier
   questions. The top 2 strong matches are added as Q + A. This costs $0 and no extra model call.
2. **The model asks for it: new tool `recall_turn(turn)`.**
   - The model reads the turn index (tier 4) and sees, for example, that turn 41 covered July.
   - If it needs the details, it calls `recall_turn(41)` and gets that turn back in full: SQL,
     result rows and answer.
   - Cost: one extra model call, and only when the information is actually needed.
   - Security: the tool reads only the **current conversation** of the **signed-in user**. Results
     are untrusted data, like any tool result.

So the model always knows what exists (tiers 2–4) and can fetch any detail (tier 5). Nothing is
out of reach; it just isn't paid for on every call.

### What could be lost in each scenario, and what covers it

| Scenario | What leaves the prompt | Risk | Covered by |
|---|---|---|---|
| 1. Big content | raw rows of a big result | "what was the 3rd item in that list?" | The trimmed note keeps **the SQL itself**, so the model can re-run or adjust it. Or `recall_turn` returns the stored rows. |
| 1. Big content | a long pasted text | details of the paste | Summary (tier 3) records what it was about; `recall_turn` returns it |
| 2. 100 turns | old answers | "what did you tell me earlier about X?" | Ledger (numbers); auto-match or `recall_turn` (wording) |
| 2. 100 turns | non-numeric context from early on | "as I said at the start, I'm buying for my shop" | Running summary (tier 3) |
| 2. 100 turns | an action started long ago | a return or ticket from turn 3 | Things in play (tier 2), and the tools show live status anyway |
| Both | a number corrected later | old wrong value resurfacing | Ledger keeps the newest value per metric |

### Running summary (tier 3): how it's kept trustworthy

- Updated by one Flash Lite call **only when turns leave the recent window**, in batches of 5
  turns. That is about 1 call per 5 turns, ~$0.0005 each.
- It summarises the old summary plus the 5 leaving turns. The 300–400-token result replaces the
  old one.
- **Numbers aren't its job:** they live in the ledger. Any number in the summary must match the
  ledger, or the summary update is rejected and the previous one kept.
- Customer wording inside it stays marked as customer text, never instructions. The summary
  prompt is tested with the red-team scenarios.
- Stored in `ai_conversations.summary`, with the turn it covers up to.

### Storage and incremental updates (no 60k → 8k rebuild per turn)

The memory is **built up a little at the end of each turn and stored**. Building the prompt is
then just reading small stored pieces. Nothing re-reads the whole chat.

**Where it lives:**

| Piece | Stored in | Size |
|---|---|---|
| Full turns (archive) | `ai_messages` (as today) | grows; never sent whole |
| Turn index line | new column `ai_messages.topic` (~15 tokens) | 1 per turn |
| Question embedding | new column `ai_messages.embedding` (384 numbers from the local model) | 1 per turn, never sent |
| Facts ledger + things in play | new column `ai_conversations.memory` (JSON) | ≤ ~700 tokens |
| Running summary | new columns `ai_conversations.summary` and `summary_upto_turn` | ≤ 400 tokens |

**After every turn (in code, ~20–50 ms, $0):**

1. Store the turn as today.
2. Add its index line and its question embedding.
3. Update the ledger with its verified numbers, replacing old values for the same metric.
4. Update things in play (new return, ticket closed…).

**When turns leave the recent window, every 5 turns:** one small Flash Lite call. Input is the old
summary (≤ 400) plus those 5 turns as Q + A (~2–3k); output is the new summary (≤ 400), which is
stored. **The 60k is never processed at once:** each turn is summarised exactly once, when it
leaves the window.

**Before each model call (building the prompt):**
- 1 read of `ai_conversations` (ledger + summary);
- the last 6 turns, plus the last 40 index lines;
- 1 local similarity search over the stored embeddings, for up to 2 matches.

That is a few ms, with no LLM call.

**Prompt order for caching:** fixed prompt + tools (cached, never changes) → summary and ledger
(change only when updated) → recent turns → question.

### Chosen window sizes (2026-10-12)

| Layer | Turns | Sent as | Example at question 145 |
|---|---|---|---|
| **Full detail** | last **5** | question + SQL + results + answer (big results trimmed, SQL kept) | turns 140–144 |
| **Q + A** | the **25** before those | question + answer only | turns 115–139 |
| **Embedding only** | everything older | **not sent**; searched locally, top 2 matches come back as Q + A | turns 1–114 |
| **Key metrics** | all turns | always sent (≤ 25 lines, newest value wins) | numbers from turns 1–144 |

Settings: `FULL_DETAIL_TURNS = 5`, `QA_TURNS = 25`; `HISTORY_ROWS` becomes 60 (30 turns × 2 rows).

Estimated history size, from the 9-question chat (Q + A ≈ 30–100 tokens per turn, full turns
≈ 180–700):

| | Typical | Long answers / big results |
|---|---|---|
| 5 full turns | ~1.5–3.5k | capped by result trimming (~2k per result) |
| 25 Q + A turns | ~1.5–2.5k | up to ~12k, so the **budget check** shortens the oldest Q + A first |
| Key metrics + up to 2 matches | ~0.5–1k | ~1.5k |
| **History total** | **~3.5–6.5k** | **capped at the budget** |

With 30 turns in view, the running summary is optional. Key metrics keep the numbers, and the
search finds older wording. The summary stays as a later option if long-chat tests show missing
non-numeric context (preferences, goals).

### When each piece switches on (by chat size, not a fixed turn number)

Short chats stay exactly as simple as today. The extra machinery only runs once the older turns
**no longer fit**:

| Stage | When | Sent to the model | Summary | Embedding search |
|---|---|---|---|---|
| **1. Small** | ≤ 5 earlier turns | everything in full | off | off |
| **2. Medium** | 6–30 earlier turns | last 5 in full + **all** older turns as Q + A + key metrics | off | off (nothing is left out, so nothing to search) |
| **3. Large** | more than 30 earlier turns (or over the token budget sooner) | last 5 in full + previous 25 as Q + A + key metrics | optional | **on**, only over turns that are left out of the prompt |

What happens in every stage, because it's cheap and keeps stage 3 ready:
- the key metrics (ledger) are updated after each turn (code, $0);
- the topic line and the embedding are saved after each turn (~20 ms, $0). The embedding is
  **stored** from turn 1 but **searched** only in stage 3.

The trigger is **tokens, not a turn number**:
- 30 short questions may still be stage 2;
- 8 turns with long answers may already be stage 3.

The budget is a setting, so the switch point can be tuned from production data.

### Checkpoint tests for "lost context"

Added to the long-chat evaluation:

- a number from turn 5, asked at turn 90;
- "the 3rd item in that list" after a 400-row result;
- a preference stated in turn 2, needed in turn 70 ("in R$", "for my shop");
- a return started at turn 3, asked about at turn 80;
- a corrected figure, where the newest must win.

Each is graded against the database or the script. The comparison is no compaction (expensive,
the baseline) versus compaction. The target is the same pass rate at a fraction of the tokens.

## How we'll know it works (evaluation before switching on)

1. **Long-chat test set:** about 5 scripted chats of 60–100 turns, built from the natural-set
   questions. They include checkpoints that refer back:
   - "what was my 2026 spend you told me earlier?"
   - "compare that with the category total"
   - a corrected number, where the latest value must win.

   Graded against the database, like the natural set.
2. **Compare with compaction off and on:**
   - accuracy at the checkpoints;
   - input tokens per call;
   - cost and latency.
3. **Regression:** the natural set (37), the red-team runs (10 + 20) and the unit tests must still
   pass.
4. **Unit tests:**
   - the ledger replaces stale values;
   - the budget is never exceeded;
   - trimming keeps the answer's numbers;
   - a customer can't inject text into the ledger.

## Production monitoring

Logged per call:
- tokens per layer and total;
- whether compaction ran;
- ledger size;
- layer C hits.

Alerts and dashboard:
- an alert when one conversation passes a cost limit;
- cost per conversation on the admin health card.

## Build order (each step confirmed separately)

1. Budget check, result trimming (keeping the SQL in the note) and the recent window (code only).
2. Facts ledger, things in play and turn index (tiers 2 and 4), with fact-check integration.
3. `recall_turn` tool and automatic relevant-turn search with the local model (tier 5).
4. Running summary (tier 3) with its ledger check.
5. Long-chat evaluation with the lost-context checkpoints, and docs. Switch on by setting,
   measure, then make it the default.
6. *(Optional)* Explicit Gemini caching of layer A.

# Context walkthrough: what the model saw in a 9-question conversation

Reconstructed from the stored conversation (`ai_messages`, conversation `15153192…`, demo customer 1, 2026-10-12) and the history rule in `ai/agent.py` (`load_history`). Token figures marked ~ are estimates (characters ÷ 4); **input / cached / cost** are what Gemini reported and we were billed.

## What goes into every model call

```
[1] system prompt (rules, procedures, policies, glossary)     ~1,618 tokens  same every call
[2] tool definitions (17 tools, SQL tool ~978 of them)        ~2,480 tokens  same every call
[3] history of earlier turns, built by load_history:
      - the last 2 turns IN FULL: question, every lookup (SQL), every result, answer
      - older turns as QUESTION + ANSWER only
      - at most 40 stored messages (~20 turns); older ones dropped
[4] the current question
[5] this turn's own lookups so far (each new call resends the earlier ones)
```

[1] + [2] ≈ **4,098 tokens resent on every call** whatever the question.

## Turn by turn

| # | Question | Calls | History sent (~tokens) | Input billed | Cached | Output | Cost | Fixed part ÷ input |
|---|---|---|---|---|---|---|---|---|
| 1 | how much did i spend this year/ | 2 | 0 | 8,821 | 2,295 | 184 | $0.0025 | 93% |
| 2 | compare every year and how much changed from every prev year | 6 | 167 | 31,836 | 10,944 | 894 | $0.0088 | 77% |
| 3 | what is change % from previous yera | 2 | 1,009 | 12,565 | 7,919 | 379 | $0.0026 | 65% |
| 4 | Can you give me month wise breakdown | 2 | 1,202 | 13,437 | 4,403 | 369 | $0.0038 | 61% |
| 5 | which months i spend more top 3 by decreasing order | 2 | 768 | 12,165 | 3,921 | 243 | $0.0032 | 67% |
| 6 | any year group by month | 2 | 705 | 12,323 | 3,896 | 455 | $0.0038 | 67% |
| 7 | group by month and spend decreasing order top 3 only | 4 | 803 | 26,542 | 12,831 | 894 | $0.0067 | 62% |
| 8 | On which category i spend more | 2 | 1,362 | 14,423 | 9,538 | 192 | $0.0022 | 57% |
| 9 | can you give spend on each category and it % in total | 2 | 1,286 | 14,208 | 4,860 | 384 | $0.0039 | 58% |
| | **Total** | 24 | | **146,320** | 60,607 | 3,994 | **$0.0375** | |

**How to read it:** *Calls* is how many times the model was asked within that question (one per lookup step, plus the final answer). Each call resends [1]+[2]+[3]+[4] and the turn's lookups so far, so **input billed ≈ calls × (fixed + history + question) + growing lookup results**. The last column shows how much of the bill was just the fixed prompt and tool definitions.

### Turn 1: "how much did i spend this year/"

Answered by `google:gemini-3.5-flash-lite` · 2 model calls · input 8,821 (cached 2,295) · output 184 · $0.0025

**History sent:** none (first question).

**Calls inside the turn:**

1. Sends fixed ~4,098 + history + question + this turn's earlier steps (~7 so far) → model: tool `query_my_data` (SQL: `SELECT ROUND(SUM(i.price + i.freight_value), 2) AS spent, DATE_FORMAT(CURDATE() - INTERVAL 0 YEAR, '%Y-01-01') AS period_start, CURDATE() AS…`)
2. Sends fixed ~4,098 + history + question + this turn's earlier steps (~142 so far) → model: final answer

**Answer:** For this year (from 2026-01-01 to 2026-10-12), you have spent a total of $573.25 across your orders.

### Turn 2: "compare every year and how much changed from every prev year"

Answered by `google:gemini-3.5-flash-lite` · 6 model calls · input 31,836 (cached 10,944) · output 894 · $0.0088

**History sent with every call of this turn:**

- Turn 1: full (with lookups), ~167 tokens (of which lookups/results ~135)
- **Total history ~167 tokens**

**Calls inside the turn:**

1. Sends fixed ~4,098 + history + question + this turn's earlier steps (~15 so far) → model: tool `query_my_data` (SQL: `WITH yearly_spend AS ( SELECT DATE_FORMAT(o.order_purchase_timestamp, '%Y') AS year, ROUND(SUM(i.price + i.freight_value), 2) AS spent FROM …`)
2. Sends fixed ~4,098 + history + question + this turn's earlier steps (~167 so far) → model: tool `query_my_data` (SQL: `SELECT y.year, y.spent, ROUND(y.spent - ( SELECT SUM(i2.price + i2.freight_value) FROM my_orders o2 JOIN my_order_items i2 USING (order_id) …`)
3. Sends fixed ~4,098 + history + question + this turn's earlier steps (~426 so far) → model: tool `query_my_data` (SQL: `SELECT DATE_FORMAT(o.order_purchase_timestamp, '%Y') AS year, ROUND(SUM(i.price + i.freight_value), 2) AS spent, '2024-08-01' AS period_star…`)
4. Sends fixed ~4,098 + history + question + this turn's earlier steps (~569 so far) → model: tool `query_my_data` (SQL: `SELECT MIN(order_purchase_timestamp) AS first_order, MAX(order_purchase_timestamp) AS last_order FROM my_orders WHERE order_status NOT IN ('…`)
5. Sends fixed ~4,098 + history + question + this turn's earlier steps (~656 so far) → model: tool `query_my_data` (SQL: `SELECT DATE_FORMAT(o.order_purchase_timestamp, '%Y') AS year, ROUND(SUM(i.price + i.freight_value), 2) AS spent, '2025-05-05' AS period_star…`)
6. Sends fixed ~4,098 + history + question + this turn's earlier steps (~799 so far) → model: final answer

**Answer:** Here is your yearly spending across your purchase history (from 2025-05-05 to 2026-10-12):

- **2025**: $477.95
- **2026**: $573.25 (an increase of $95.30 compared to 2025)

### Turn 3: "what is change % from previous yera"

Answered by `google:gemini-3.5-flash-lite` · 2 model calls · input 12,565 (cached 7,919) · output 379 · $0.0026

**History sent with every call of this turn:**

- Turn 1: full (with lookups), ~167 tokens (of which lookups/results ~135)
- Turn 2: full (with lookups), ~842 tokens (of which lookups/results ~773)
- **Total history ~1,009 tokens**

**Calls inside the turn:**

1. Sends fixed ~4,098 + history + question + this turn's earlier steps (~8 so far) → model: tool `query_my_data` (SQL: `SELECT y.year, y.spent, ROUND(((y.spent - prev.spent) / prev.spent) * 100, 2) AS pct_change, '2025-05-05' AS period_start, '2026-10-12' AS p…`)
2. Sends fixed ~4,098 + history + question + this turn's earlier steps (~288 so far) → model: final answer

**Answer:** For your spending between 2025-05-05 and 2026-10-12:

- **2025**: $477.95 (first year with orders)
- **2026**: $573.25 (an increase of **19.94%** compared to 2025)

### Turn 4: "Can you give me month wise breakdown"

Answered by `google:gemini-3.5-flash-lite` · 2 model calls · input 13,437 (cached 4,403) · output 369 · $0.0038

**History sent with every call of this turn:**

- Turn 1: question + answer, ~32 tokens
- Turn 2: full (with lookups), ~842 tokens (of which lookups/results ~773)
- Turn 3: full (with lookups), ~328 tokens (of which lookups/results ~280)
- **Total history ~1,202 tokens**

**Calls inside the turn:**

1. Sends fixed ~4,098 + history + question + this turn's earlier steps (~9 so far) → model: tool `query_my_data` (SQL: `SELECT DATE_FORMAT(o.order_purchase_timestamp, '%Y-%m') AS month, ROUND(SUM(i.price + i.freight_value), 2) AS spent, '2025-05-05' AS period_…`)
2. Sends fixed ~4,098 + history + question + this turn's earlier steps (~265 so far) → model: final answer

**Answer:** Here is your month-wise spending breakdown covering the period from 2025-05-05 to 2026-10-12:

- **2025-05**: $22.77
- **2025-06**: $51.75
- **2025-07**: $270.37
- **2025-08**: $51.75
- **2025-10**: $59.54
- **2025-11**: $21.77
- **2026-05**: $116.33
- **2026-06**: $30.41
- **2026-07**: $142.29
- **2026-08**: $135.06
- **2026-10**: $149.16

### Turn 5: "which months i spend more top 3 by decreasing order"

Answered by `google:gemini-3.5-flash-lite` · 2 model calls · input 12,165 (cached 3,921) · output 243 · $0.0032

**History sent with every call of this turn:**

- Turn 1: question + answer, ~32 tokens
- Turn 2: question + answer, ~58 tokens
- Turn 3: full (with lookups), ~328 tokens (of which lookups/results ~280)
- Turn 4: full (with lookups), ~350 tokens (of which lookups/results ~256)
- **Total history ~768 tokens**

**Calls inside the turn:**

1. Sends fixed ~4,098 + history + question + this turn's earlier steps (~12 so far) → model: tool `query_my_data` (SQL: `SELECT DATE_FORMAT(o.order_purchase_timestamp, '%Y-%m') AS month, ROUND(SUM(i.price + i.freight_value), 2) AS spent, '2025-05-05' AS period_…`)
2. Sends fixed ~4,098 + history + question + this turn's earlier steps (~174 so far) → model: final answer

**Answer:** Here are your top 3 highest-spending months across your order history (from 2025-05-05 to 2026-10-12):

1. **2025-07**: $270.37
2. **2026-10**: $149.16
3. **2026-07**: $142.29

### Turn 6: "any year group by month"

Answered by `google:gemini-3.5-flash-lite` · 2 model calls · input 12,323 (cached 3,896) · output 455 · $0.0038

**History sent with every call of this turn:**

- Turn 1: question + answer, ~32 tokens
- Turn 2: question + answer, ~58 tokens
- Turn 3: question + answer, ~48 tokens
- Turn 4: full (with lookups), ~350 tokens (of which lookups/results ~256)
- Turn 5: full (with lookups), ~217 tokens (of which lookups/results ~162)
- **Total history ~705 tokens**

**Calls inside the turn:**

1. Sends fixed ~4,098 + history + question + this turn's earlier steps (~5 so far) → model: tool `query_my_data` (SQL: `SELECT SUBSTRING(o.order_purchase_timestamp, 6, 2) AS month_num, ROUND(SUM(CASE WHEN o.order_purchase_timestamp LIKE '2025%' THEN i.price + …`)
2. Sends fixed ~4,098 + history + question + this turn's earlier steps (~259 so far) → model: final answer

**Answer:** Here is your spending grouped by calendar month, comparing 2025 and 2026 across your purchase history (2025-05-05 to 2026-10-12):

- **May**: 2025: $22.77 | 2026: $116.33
- **June**: 2025: $51.75 | 2026: $30.41
- **July**: 2025: $270.37 | 2026: $142.29
- **August**: 2025: $51.75 | 2026: $135.06
- **October**: 2025: $59.54 | 2026: $149.16
- **November**: 2025: $21.77 | 2026: $0.00

### Turn 7: "group by month and spend decreasing order top 3 only"

Answered by `google:gemini-3.5-flash-lite` · 4 model calls · input 26,542 (cached 12,831) · output 894 · $0.0067

**History sent with every call of this turn:**

- Turn 1: question + answer, ~32 tokens
- Turn 2: question + answer, ~58 tokens
- Turn 3: question + answer, ~48 tokens
- Turn 4: question + answer, ~94 tokens
- Turn 5: full (with lookups), ~217 tokens (of which lookups/results ~162)
- Turn 6: full (with lookups), ~354 tokens (of which lookups/results ~254)
- **Total history ~803 tokens**

**Calls inside the turn:**

1. Sends fixed ~4,098 + history + question + this turn's earlier steps (~13 so far) → model: tool `query_my_data` (SQL: `SELECT SUBSTRING(o.order_purchase_timestamp, 6, 2) AS month_num, ROUND(SUM(CASE WHEN o.order_purchase_timestamp LIKE '2025%' THEN i.price + …`)
2. Sends fixed ~4,098 + history + question + this turn's earlier steps (~283 so far) → model: draft answer, REJECTED by the fact check ("Your reply mentions values that no tool returned in this conversation: $186.81, $208.70, $412.66. Look them up with a to…")
3. Sends fixed ~4,098 + history + question + this turn's earlier steps (~402 so far) → model: tool `query_my_data` (SQL: `SELECT SUBSTRING(o.order_purchase_timestamp, 6, 2) AS month_num, ROUND(SUM(CASE WHEN o.order_purchase_timestamp LIKE '2025%' THEN i.price + …`)
4. Sends fixed ~4,098 + history + question + this turn's earlier steps (~647 so far) → model: final answer

**Answer:** Here are the top 3 calendar months by your combined spending across all years (from 2025-05-05 to 2026-10-12):

1. **July** (Total: $412.66) — 2025: $270.37 | 2026: $142.29
2. **October** (Total: $208.70) — 2025: $59.54 | 2026: $149.16
3. **August** (Total: $186.81) — 2025: $51.75 | 2026: $135.06

### Turn 8: "On which category i spend more"

Answered by `google:gemini-3.5-flash-lite` · 2 model calls · input 14,423 (cached 9,538) · output 192 · $0.0022

**History sent with every call of this turn:**

- Turn 1: question + answer, ~32 tokens
- Turn 2: question + answer, ~58 tokens
- Turn 3: question + answer, ~48 tokens
- Turn 4: question + answer, ~94 tokens
- Turn 5: question + answer, ~55 tokens
- Turn 6: full (with lookups), ~354 tokens (of which lookups/results ~254)
- Turn 7: full (with lookups), ~721 tokens (of which lookups/results ~515)
- **Total history ~1,362 tokens**

**Calls inside the turn:**

1. Sends fixed ~4,098 + history + question + this turn's earlier steps (~7 so far) → model: tool `query_my_data` (SQL: `SELECT c.category, ROUND(SUM(i.price + i.freight_value), 2) AS spent, '2025-05-05' AS period_start, '2026-10-12' AS period_end FROM my_order…`)
2. Sends fixed ~4,098 + history + question + this turn's earlier steps (~145 so far) → model: final answer

**Answer:** You have spent the most on **sports_leisure**, with a total of $707.54 across your purchase history (from 2025-05-05 to 2026-10-12).

### Turn 9: "can you give spend on each category and it % in total"

Answered by `google:gemini-3.5-flash-lite` · 2 model calls · input 14,208 (cached 4,860) · output 384 · $0.0039

**History sent with every call of this turn:**

- Turn 1: question + answer, ~32 tokens
- Turn 2: question + answer, ~58 tokens
- Turn 3: question + answer, ~48 tokens
- Turn 4: question + answer, ~94 tokens
- Turn 5: question + answer, ~55 tokens
- Turn 6: question + answer, ~100 tokens
- Turn 7: full (with lookups), ~721 tokens (of which lookups/results ~515)
- Turn 8: full (with lookups), ~178 tokens (of which lookups/results ~138)
- **Total history ~1,286 tokens**

**Calls inside the turn:**

1. Sends fixed ~4,098 + history + question + this turn's earlier steps (~13 so far) → model: tool `query_my_data` (SQL: `SELECT c.category, ROUND(SUM(i.price + i.freight_value), 2) AS spent, ROUND(SUM(i.price + i.freight_value) * 100.0 / ( SELECT SUM(i2.price +…`)
2. Sends fixed ~4,098 + history + question + this turn's earlier steps (~284 so far) → model: final answer

**Answer:** Here is the breakdown of your spending by category and its percentage of your total spending (from 2025-05-05 to 2026-10-12):

- **sports_leisure**: $707.54 (67.31%)
- **health_beauty**: $149.16 (14.19%)
- **uncategorized**: $81.89 (7.79%)
- **fashion_bags_accessories**: $73.95 (7.03%)
- **costruction_tools_tools**: $38.66 (3.68%)

## What this shows

1. **The fixed part dominates.** About 4,098 tokens of prompt and tool definitions go with every call; across the conversation that is most of the input. Caching it (90% off) or making fewer calls saves the most.
2. **History is small but grows.** Older turns are sent as question + answer only; the last 2 turns are sent in full, including their SQL and results. That full detail is the part a compaction rule can drop. *(This chat ran with `FULL_DETAIL_TURNS = 2`; changed to 3 on 2026-10-12, so the last 3 turns now go in full.)*
3. **Extra calls are the expensive surprises.** Turn 2 ("compare every year…") made 6 calls (5 lookups, then the answer) and turn 7 made 4; each call resent the fixed part, the history and the earlier steps. Those two turns are 5 of the 9 questions' worth of input (58,378 of 146,320 tokens, 40%).
4. **The 40-message window** means after ~20 turns the oldest questions stop being sent at all (no summary yet).

Plan to bound and shrink this further: summary per call, rolling memory, caching; see the context plan.

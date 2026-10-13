# Admin Agent

A separate agent for platform admins, built with the same steps as the customer and seller agents.
Data and decisions: [DATA_EXPLORATION.md](DATA_EXPLORATION.md) · ground truth: [TEST_SCENARIOS.md](TEST_SCENARIOS.md).

| Step | State |
|---|---|
| 1. Data exploration and decisions | done |
| 2. Test scenarios and ground truth | done |
| 3. Security foundation | done |
| 4. Storage and chat history | done |
| 5. Tools | done |
| 6. Permission tests | done |
| 7. Prompt, engine, `/api/admin/chat`, chat history UI | done |
| 8. Tool consolidation (11 → 5) | **done** |
| 9. Evaluation | next |
| 10. Red-team | |

## Step 3: Security foundation

Three layers, each enough on its own to stop the most serious leaks.

### 1. Its own tool registry (`ai/admin/security.py`)

| Rule | How it's enforced |
|---|---|
| Only signed-in admins can call admin tools | `get_admin_tool`: role must be `admin` with a user id; anything else gets "not available" |
| The admin's identity comes from the session only | tools take `principal` first; `user_id`, `admin_user_id`, `username`, `role` can't be arguments. A `seller_id` or `order_id` **can** (admins may look at any seller) |
| Read-only (decision 3) | a tool declaring a write, or a non-read kind, is refused at import |
| Declared reads | each tool lists its tables; protected tables (`app_users`, addresses, carts) and columns outside the admin rules are refused at import |
| Masked columns only in totals | masked columns a tool needs for platform figures are declared in `aggregates` and must be in the aggregate-only list |
| Separate from the other agents | own `ADMIN_REGISTRY`; tools run on the admin agent's own database user |

### 2. Its own MySQL user (`ai/admin/db_user.py`)

`olist_admin_agent`, created with `python -m ai.admin.db_user` (password generated into `.env`, never
printed; max 12 connections; app pool `db.ADMIN_AGENT_POOL`, used through `db.as_admin_agent()`).

| Access | Columns |
|---|---|
| **Shown in answers** | every column the catalog lets admins see **and doesn't mask** (orders, items, products, sellers, reviews, returns, refund status and dates, ticket category and status, catalog and scheduler tables) |
| **Aggregate-only** (masked for admins; totals, counts and groupings only) | `payments`: type, installments, value (GMV) · `customers`: unique id, city, state (buyers by state, repeat customers) · `refunds`: amount, method (refund totals) |
| **Never** | `app_users` (logins) · `customer_addresses`, `order_addresses` · `cart_items` · customer zip code · ticket description and AI summary · refund failure reason · customer IDs on tickets and returns |
| **Own chats** (`ai_*` tables) | read and write its own chat history and usage; insert into `ai_admin_access_log` |
| **Can't** | change any store data, AI settings or limits, delete or edit chats, or edit or delete the access log |

MySQL refuses everything outside these grants, even if a tool or a generated query asks for it.

### 3. Masking and lookup audit

- **`mask_id(kind, value)`**: a stable pseudonym such as `customer-7f3a91c2`. The same customer always
  gets the same label, so an admin can follow them across answers, but the real ID can't be read
  back from it (salted SHA-256). Unmasking stays in the admin screens, where it is logged with a reason.
- **`log_lookup(principal, what, label)`**: when a tool looks at one order, customer or seller during
  an admin chat, a `view` row goes into `ai_admin_access_log` with the conversation id (decision 6).

### Retention: admin chats are an audit trail

| | Customers / sellers | Admins |
|---|---|---|
| Retention class | `standard` | **`admin_audit`** (new; set by `store.create_conversation` for admins) |
| Kept | 90 days after the last message | **365 days** (`admin_retention_days` setting) |
| Deleted from the page | purged after 30 days' grace | **not deletable**; also excluded from the "deleted" purge |

The database check on `ai_conversations.retention_class` was widened to allow `admin_audit`
(`ai/schema.py: migrate_retention_classes`), and the catalog lists the new value.

### Tests

`tests/test_admin_security.py`: 41 tests:
- registry rules;
- MySQL refusing protected tables and columns;
- platform figures still possible (GMV, buyers by state, refund totals);
- no writes to store data, settings, limits, chats or the access log;
- masking;
- the lookup log;
- the retention class.

Full suite at step 3: 590 passed.

## Step 4: Storage and chat history

Admin chats reuse the shared, role-tagged `ai_*` tables (no new tables). The admin parts:

| What | Where | Rule |
|---|---|---|
| Chats | `ai_conversations` (`owner_role = 'admin'`) | retention class `admin_audit`: kept 1 year, never purged as "deleted" |
| Questions and answers | `ai_messages` | same as other roles (tokens, model, prompt version, question embedding) |
| **Lookups** | `ai_tool_calls` | each call is linked to **the question that caused it** (`message_id` = the question); older rows without a link are placed by time |
| Record lookups | `ai_admin_access_log` | one `view` row per record looked at (step 3) |
| Usage and cost | `ai_usage`, `ai_usage_daily` | role `admin` |

**History functions** (`ai/store.py`; they run on the admin agent's database user):

| Function | For the UI |
|---|---|
| `admin_conversations(principal, search, limit, before)` | the history sidebar: the admin's **own** chats, newest first, with title, dates and question count. `search` matches titles and questions (not answers; `%` and `_` are literal); `before` pages back |
| `admin_transcript(principal, conversation_id)` | reopening a chat: every message, and under each answer its **lookups** (tool, arguments such as metric/group by/period, status, time). Another admin's chat returns nothing |
| `delete_conversation` | **refused** for admins: the chats are an audit trail |

Only signed-in admins have a history; other roles get `PermissionError`.

Tests: `tests/test_admin_storage.py` (10). Full suite: 600 passed.

## Step 5: Tools

> Step 8 later consolidated these 11 tools into 5; see [Step 8](#step-8-tool-consolidation-11--5).

Ten read-only tools in `ai/admin/tools/`, all registered through `admin_tool` (step 3 rules).

| Tool | Answers | Personal data |
|---|---|---|
| **`admin_metric`** | 24 defined platform metrics × 10 groupings: item revenue, GMV, orders, AOV, customers, new customers, repeat share, sellers, cancel / late delivery / on-time / late shipping / return / low review rates, delivery days... by month, year, status, buyer state, payment type, seller, seller state, category, product; top_n, min_orders, compare_previous, share_of_total | masked columns only inside aggregates; **sensitive metrics hide groups under 5 orders** |
| `order_story` | an order's timeline, items and sellers, delivery vs estimate, review (as untrusted text), returns, refunds against the refund SLA | customer as `customer-xxxxxxxx`; no amounts or payment details; **logged** |
| `customer_history` | a customer's orders, statuses, item spend, returns, refunds, reviews; found by masked ID or one of their orders | masked; no name, address, city, state or payment; **logged** |
| `seller_overview` | a seller's figures all time and last 12 months, top categories, open orders, storefront stats | **logged** |
| `refund_issues` | stuck (past the SLA), failed or processing refunds: count, total, by method, oldest first | totals only; no per-refund amount or failure reason |
| `late_open_orders` | open orders of the last 60 days, most late first; old leftovers counted apart | none |
| `weak_sellers` | sellers below an on-time rate and/or rating, with a minimum of orders | none |
| `support_queue` | tickets by status/type/priority, open tickets by age | no ticket text |
| `ai_assistant_usage` | replies, local answers, tokens, cost, thumbs up/down, tool problems by role/model/day | no chat content |
| `query_platform_data` | **fallback** SQL over `platform_*` tables, max 200 rows | its tables hold **no masked column and no free text** |

### The semantic layer and its reporting tables

Like the seller agent, the model picks metric names and the server compiles the SQL from
`admin_semantic` in `ai/metadata.yaml`. Each metric is defined per grain, and the server picks the grain
that fits the grouping:

| Grain | Table | Use |
|---|---|---|
| order | `admin_order_facts` | platform totals, GMV, customers, buyer state, payment type |
| order × seller | `admin_seller_order_facts` | per-seller rates (on time, late shipping, returns) without counting an order twice |
| unit sold | `admin_item_facts` | categories and products |

Building these from the store tables takes 1–2 minutes on this laptop, too slow per question. So they
are **reporting tables rebuilt hourly** (`ai/admin/facts.py`, scheduler job `admin_facts_refresh`;
also `python -m ai.admin.facts`):
- the build is staged through indexed temporary tables;
- new copies are swapped in atomically;
- answers carry `figures_as_of`.

Lookups and operations lists read the live tables. The customer appears in the facts only as a one-way
hash, used for counting.

### Checked against the ground truth

| Figure | Tool result |
|---|---|
| Item revenue all / 2025 / 2026 | $13,494,652.70 / $6,393,604.14 / $7,050,839.08 ✓ |
| GMV all / 2025 | $15,739,483.04 / $7,424,316.33 ✓ |
| Orders · AOV · best month | 98,211 · $137.42 · 2025-11 $1,115,409.45 ✓ |
| Late deliveries | 6,534 of 96,470 (6.77%) ✓ |
| Top sellers, top states, top categories | ✓ |
| Stuck refunds · failed | 9, $1,325.05, 6 card + 3 boleto, oldest 39 days · 48, $5,223.80 ✓ |
| Open orders · old leftovers | 6 · 1,727 ✓ |
| AI usage by role | customer 93 · seller 7 · guest 2 replies ✓ |

**Two definitions differ from the storefront's `seller_stats`:**
- on time is measured per delivered order, comparing dates only (the glossary definition);
- `seller_stats` compares exact times and is weighted by items and reviews.

So the weak seller is **69.9%** on time here and 67.9% on the storefront. Sellers under 80% on time
(10+ orders): **44** here, 81 by the old method. TEST_SCENARIOS.md is updated with both.

AOV leaves out 8 orders that have no items (an Olist data quirk).

### Fixed while testing

- A grouping alias named `role` was silently resolved to `ai_messages.role`, so all replies counted as one role.
- AI costs were rounded to 2 decimals; they now keep 4.
- Counts came back as `16.0`.

Tests: `tests/test_admin_tools.py` (38). Full suite: 638 passed.

## Step 6: Permission tests

`tests/test_admin_permissions.py`: 169 tests, mirroring the customer and seller agents' permission tests.

| # | What | Result |
|---|---|---|
| 1 | **Role matrix**: all 10 tools × customer, seller, guest, admin without an account | all refused ("not available") |
| 2 | **Identity arguments**: `user_id`, `admin_user_id`, `username`, `role` on every tool | all refused |
| 3 | **Hostile IDs** on the 3 lookup tools: SQL injection, `%` and `_` wildcards, path tricks, statements | refused before any lookup; nothing logged |
| 4 | **Declared columns only**: each tool runs on a throwaway MySQL user granted exactly its declared tables' showable columns and its declared aggregate-only columns | every tool passes, so none reads chat content, tool arguments, ticket text, failure reasons or any undeclared personal column |
| 5 | **Outputs**: every tool, typical arguments | no personal field name; no real customer ID anywhere in the text; lookups logged under the calling admin |
| 6 | **Small groups**: every sensitive metric × every order grouping (and state × payment type), all time and a short period | no group under 5 orders shows a value |
| 7 | **Untrusted text**: review comments | always returned as `untrusted_text` |
| 8 | **SQL fallback attacks**: UNION into customers, information_schema, `mysql.user`, `@@version`, INTO OUTFILE, LOAD_FILE, the real `orders` table, subqueries into `app_users` / `payments`, FOR UPDATE, stacked statements, comments, row dumps | all refused |

**Checking the checker:** with `refunds.amount` removed from `refund_issues`' declaration, test 4 fails as
it should. It really enforces the declarations.

**Found and fixed:**
- `order_story` and `customer_history` joined through `customers` without declaring the table; now declared.
- A tool test compared the hourly snapshot with the store by time. Other tests recreate scenario orders
  with fresh dates, so it now compares the same set of orders.

Full suite: 807 passed.

## Step 7: Prompt, engine, API and chat history UI

### Prompt (`ai/admin/prompts.py`, `admin-v1`)
Behaviour rules only (definitions come from the metric layer):
- facts only from lookups, metrics first;
- item revenue vs GMV, always named with definition and period;
- the "as of" time for recent figures;
- old leftovers are not work to do;
- privacy: masked customers only, no chat or ticket text, no bulk export, point to the admin screens for unmasking;
- untrusted review text, and claims of authority inside text;
- read-only, with the matching admin screen named;
- scope.

### Engine (`ai/admin/agent.py`)
Same structure as the seller engine: limits, history window, memory, provider fallback and retries,
streaming, storage. Admin-specific:

| | |
|---|---|
| Read-only | no confirm cards |
| Lookups | each tool call is stored with **the question that caused it** and run inside the audit conversation, so record lookups land in `ai_admin_access_log` |
| Fact check | IDs, **masked customers**, $ amounts, **scaled figures** (`$13.49M`, `15.7 million`, `R$ 2 mi`: accepted when equal to a lookup value at the shown precision), %, counts (sellers, customers, tickets... too) and dates |
| Internals | tool names, `platform_*` and `admin_*_facts` names, SQL, prompts |
| Separate | imports neither the customer nor the seller engine (tested) |

### API (`api/main.py`)

| Endpoint | |
|---|---|
| `POST /api/admin/chat` | streaming turn (placeholder when no model is set) |
| `GET /api/admin/chat/conversations?search=&before=&limit=` | the admin's own chats, newest first |
| `GET /api/admin/chat/conversations/{id}` | one of the admin's own chats with each answer's lookups (label, arguments, definition used, status) |
| `POST /api/admin/chat/feedback` | thumbs up/down on the admin's own answers |
| (no DELETE) | admin chats are an audit trail |

Another admin's chat: not listed, 404 when opened, continued or rated. The admin agent also warms up
at server start.

### UI (Admin → AI Assistant)
- **History sidebar**: New chat, search (titles and questions), your chats newest first with date and
  question count, Load older, the open chat highlighted, and the note "Only you see your chats.
  They're kept for 1 year as an audit trail and can't be deleted."
- **Reopen** a past chat with all its messages.
- **Lookups (n)** under each answer, collapsible. Each lookup shows:
  - what was run;
  - its options (metric, group by, period...);
  - the metric definition used;
  - the SQL for custom analyses;
  - its status.

  Lookups appear right after a live answer as well as in reopened chats.
- New suggestions and intro for admins.
- **AI settings** now has an **admin** card: model, temperature, history window, questions per chat.
  Hybrid has no admin local answers.

Tests: `tests/test_admin_agent.py` (30): engine, audit storage, lookups linked to questions, access log,
fact check, internals, separation, API, history privacy, no delete, placeholder. Full suite: 837 passed.
Frontend: `npm run build` passes.

### Fixed after the first UI test (prompt admin-v2)

"Monthly change in item revenue this year" showed a broken table, and its last row was wrong.

| Problem | Cause | Fix |
|---|---|---|
| Table shown as raw `\|` lines | The chat renderer had no tables | Tables in all three assistants (blank lines between rows tolerated; numbers right-aligned), italics, headings |
| October compared with **August**, labelled "vs previous month" (−99.94%) | September 2026 had no revenue (10 orders, all canceled/unavailable), so it had no row | `admin_metric` time series have **no gaps**: an empty month gets $0 (sums, counts) or no value (averages), so changes always compare neighbouring periods |
| −58.94% and −99.94% presented as sales falling | The records thin out at the end of the data; October isn't over | The current period is marked `partial`; periods under 10% of the typical order count are marked `few_orders`; the result's note and the prompt say these aren't a business trend |
| The definition crammed into the first line | No layout rule | Prompt: answer first, then the table, then one italic line with definition, period and "as of" |

Tests: 4 more in `tests/test_admin_tools.py`.

## Added: charts (`show_chart`, prompt admin-v3)

The 11th tool draws a chart in the chat. **The model never supplies chart data.** It chooses only:
- the metric;
- the grouping;
- the period;
- the chart type (auto, line, bar);
- top_n;
- the title.

The server builds the values with the same compiled queries as `admin_metric`, so charts get the same
guarantees:
- the same definitions;
- months with no gaps;
- partial and few-orders periods marked (with `*` on the axis and in the tooltip);
- sensitive groups under 5 orders hidden.

| Grouping | Chart |
|---|---|
| month or year | line (trend) |
| seller, category, state... | horizontal bars (ranking, top_n up to 20) |
| month/year + one grouping | the top groups by total as lines: 3 by default, up to 6 on request (`series`); the rest are left out and noted |

**Design** (dataviz reference palette):
- 3 series by default, in the palette's first three colour-blind-safe slots, in a fixed order;
- up to 6 on request: series 4–6 of a line chart also get a dash pattern (a second cue besides colour), shown in the legend;
- **colours on request** (`colors`): blue, orange, teal, yellow, pink, green, purple, red or gray, each mapped to a tested palette shade, each series a different colour; to change colours, the assistant redraws the chart;
- one axis;
- 2px lines;
- a legend only when there are several series;
- a tooltip on hover;
- **Show table** as an accessible alternative;
- the definition, period and "as of" under the chart.

**Delivery:**
- the chart is a `chart` event, shown before the takeaway;
- it is stored with the answer (`ai_messages.content.charts`) and appears again when the chat is reopened;
- the drawing code is a separate 5 kB chunk, loaded only when a chart appears.

The model gets the same values back (so its takeaway passes the fact check), and is told to write only a
one- or two-sentence takeaway.

Tests: 8 more in `tests/test_admin_tools.py`, 1 in `tests/test_admin_agent.py`. The permission probes
cover `show_chart` too (declared columns only, no personal fields).

### Fixed after a UI test (prompt admin-v4)

| Problem | Fix |
|---|---|
| Asked to change colours, the assistant said it couldn't | `show_chart` takes `colors`; the prompt says to redraw rather than refuse |
| Asked for the top 5, the chart showed 3 (and the text 5) | `series` 1–6 (default 3), dashes for series 4–6 |
| The reply mentioned "the chart visualization tool" | The internals check allows up to 3 words between "the/this/a…" and "tool" (it allowed 1) |

Tests: 7 more in `tests/test_admin_tools.py`, 3 in `tests/test_admin_agent.py`.

### Charts made general (prompt admin-v5)

A request for "a pie chart of the top 20 categories" got text only, then "pie charts are not supported".
Instead of adding chart types one by one, `show_chart` now follows general rules:

| Rule | How |
|---|---|
| **Draw the type asked for** | line, area, bar (columns), horizontal bar, stacked bar, stacked area, pie, donut, scatter |
| **Otherwise pick the clearest** | months/years → line; a ranking → horizontal bars; two measures (`y_metric`) → scatter; two groupings without time → grouped bars |
| **Sizes as asked** | up to 50 bars, slices or points (`top_n`), up to 12 series (`series`); two groupings in any combination (time × group or group × group) |
| **Colours as asked** | named colours; past the palette's 8, lighter shades of the same hues (never repeats); every series and slice named in the legend; lines past the third also dashed |
| **Only truth is enforced** | a pie or a stack needs parts that add up to the whole: sums and row counts do; rates, averages and distinct counts don't (an order with two categories counts in both). Those are drawn as bars, with a note saying why. A pie's slices always add up to the whole: what's outside the top N is one "Other" slice (hidden for privacy if it covers under 5 orders on a sensitive metric) |
| **Never refuse** | prompt: draw in the same turn, exactly as asked; redraw to change type, colours, size or period; pass on any note in one sentence |

Tests: 8 more chart tests in `tests/test_admin_tools.py`. The chart drawing code is a lazy 57 kB chunk.

### Comparisons made general, prompt admin-v6 (after "YoY" answers that were really month-on-month)

**What went wrong:** asked for year-on-year changes, the assistant showed month-on-month ones labelled
"YoY". For January 2026 it showed +$229,703.73 (that's vs December 2025); the real YoY is +$717,379.58
(+322.2% vs January 2025). Later it blamed "a discrepancy in the data".

| Cause | Fix |
|---|---|
| `admin_metric` could only compare with the *previous* month/year | One general option for any metric, any grouping or none: `compare_to` = `previous_period` or `same_period_last_year`, or `compare_period` = any period; `rank_by` = `change` / `change_pct` |
| Results said `change` without saying against what | Every row carries `compared_with`, `previous_value`, `change`, `change_pct`; `comparison.total` compares the whole period; a period not over yet is compared with the **same days** of its baseline; an empty baseline is 0 for sums and counts |
| The fact check rejected the model's own arithmetic, and the rejection leaked into the answer ("Let me remove those calculated values") | Retry notes are marked as automatic checks the model must never mention; replies that talk about the checks ("remove those values", "the lookup returned", "values not in the lookups") are sent back too |
| Prompt gaps | **General rules**, not case-specific: every number comes from a lookup (including differences, percentages and comparisons, which are requested from the tool); a custom analysis when the metrics can't express a question; describe each figure by the baseline its result names; answer exactly what was asked (figure, comparison, periods, columns) and state the reading if ambiguous; when a figure is questioned, re-check with a fresh lookup before answering, and never blame the data without evidence |

`compute_metric` is the plain function behind the tool (the charts call it); the older `compare_previous`
still works and means `previous_period`.

Regression tests from this conversation: 6 in `tests/test_admin_tools.py` and 4 in `tests/test_admin_agent.py`.

### Tools audit: every number says what it means (prompt admin-v7)

All 11 tools were checked for the two patterns behind the wrong "YoY" answers:
- a missing capability, which the model improvises around;
- a number that doesn't say what it means, which the model mislabels.

| Tool | Found | Fixed |
|---|---|---|
| admin_metric | **No warning for thin periods**: "last 30 days" holds 4 orders (the data thins out at its end) and came back as a plain $251.96 | Any period with under 10% of the typical day's orders (about 126) gets a note that it isn't a full picture; so does a thin comparison period |
| admin_metric | `total` of a rate is the overall rate, not a sum; `orders` meant delivered or reviewed orders for some metrics | `total_meaning` and `orders_counted` on every result |
| admin_metric | Shares allowed for rates and distinct counts (meaningless, or over 100%) | Refused with the reason; `share_of` says what a share is of; `rows_shown` says a list is a top N |
| show_chart | `s0`, `s1`… returned without saying which series each is | Each series lists its key; `data_keys` explains the data |
| seller_overview, weak_sellers | No "as of" time; open-ended period labels | `figures_as_of`, both period ends, "all time"; what each count is |
| refund_issues, late_open_orders, weak_sellers, customer_history | Shortened lists didn't say they were shortened | "3 of 48"-style fields; totals say what they cover |
| order_story, support_queue, ai_assistant_usage, query_platform_data | Nothing to fix | none |

The prompt replaced its special case for partial and thin months with one general rule: use each result's
own words about what it covers (period, counts, total, how many shown, baseline, notes), pass every note on,
never present a shortened list as the whole, and never present a thin or unfinished period as a trend.

Tests: 8 more in `tests/test_admin_tools.py` (written; run once the API server is stopped).

## Step 8: Tool consolidation (11 → 5)

**Why:** after the UI tests, each tool was checked against one question: *what does it do that the others
can't?* Fewer tools that don't overlap means fewer wrong picks, shorter instructions on every question,
and no two tools giving different answers to one question. Tools stay only where SQL can't safely do the
job: privacy (masked columns, audit log), business rules or definitions.

### What changed

| Before (step 5) | Now | Why |
|---|---|---|
| `admin_metric` | `admin_metric` (kept) | The only route to GMV, customers and buyer states (those columns stay out of SQL); applies the definitions (SQL got 3 of 5 hard questions right, the metric layer 5 of 5); comparisons |
| `show_chart` | `show_chart` (kept) | Visuals |
| `query_platform_data` | `query_platform_data` (kept) | Fallback for anything else |
| `order_story` | **`look_up_record(kind="order")`** | Merged: one tool for any single record; masking and audit log unchanged |
| `customer_history` | **`look_up_record(kind="customer")`** | Merged |
| `seller_overview` | **`look_up_record(kind="seller")`** | Merged |
| `refund_issues` | **`operations_report(report="stuck_refunds" / "failed_refunds" / "processing_refunds")`** | Merged: the "stuck" rule needs each refund's payment method, which is masked and kept out of SQL |
| `late_open_orders` | **`operations_report(report="late_open_orders")`** | Merged: carries the open-order rule (last 60 days; older ones are leftovers) |
| `ai_assistant_usage` | **`operations_report(report="ai_usage")`** | Merged: the AI tables aren't in SQL |
| `weak_sellers` | **removed** | Duplicated `admin_metric` (`on_time_rate` by seller, `min_orders`, `sort asc`); combined filters go through SQL |
| `support_queue` | **removed** | A count over the tickets table, which SQL already has (without ticket text) |

### Unchanged
- Every rule from steps 3–7: declared columns only, masking, audit log, read-only, results that say what they mean.
- The old functions remain as internal helpers behind the two merged tools; nothing they returned was dropped.

### Tests
- The permission probes run every argument of the 5 tools on a throwaway MySQL user granted exactly what each declares.
- Tests for the two removed tools now check that **their questions are still answered**: worst on-time sellers through `admin_metric`, and tickets through SQL with ticket text still refused.
- The registry test pins the 5 names.

### Charts can plot any field of a comparison (after a UI test)

**What happened:** asked to "draw a line chart for % change" (year on year), the assistant drew item revenue
twice and described it as "year-on-year percentage changes". The text and the chart disagreed. The chart
also showed the model's own reading instructions as an amber note.

| Cause | Fix |
|---|---|
| `show_chart` could plot only the metric's value: the same missing-capability pattern as the YoY tables | `plot` = value, change_pct, change, previous_value, both (two series: this period and the comparison period) or share_pct, with the same comparison options as `admin_metric` (`compare_to`, `compare_period`). Works for one grouping and for several groups over time (each series compared on its own). A change plot without a comparison is refused with a request to choose one |
| Nothing said what the y axis showed | Each chart states `plotted` (for example "% change vs the same month a year earlier"); the tooltip shows each point's baseline ("vs 2025-01"); the model is told to describe the chart only as `plotted` says, and to redraw if that isn't what was asked |
| Notes written for the model were shown on the chart | Chart notes are short and written for people (for example "Very few orders in the comparison period: changes against it are not a business trend"); the reading guidance goes to the model only (`how_to_read`) |
| A thin *comparison* period was reported as "this period" | Coverage notes name the period they're about |

Pies and stacks of changes are drawn as lines or bars with a note (changes aren't parts of a whole); a pie of
shares slices the values and works out its own shares.

Tests: 5 more in `tests/test_admin_tools.py` (written; run once the API server is stopped).

### Answers are shown only after they pass the checks (after a UI test)

**What happened:** "yoy change for every month" showed an answer, which then turned into "Sorry, I couldn't put
together a reliable answer". Three causes:

| Cause | Fix |
|---|---|
| **A bug from the comparison rewrite:** a month-by-month comparison without a period was refused ("'all' has nothing before it"), so the model improvised with 7 lookups | Each month or year is compared with its own neighbour for any period, including all time; the whole-period comparison is added only when the period has a start |
| **Drafts were streamed before the fact check:** a draft appeared and was then replaced | Admin answers are held back until the whole answer passes. Meanwhile a "Checking the figures" step shows; a rejected draft never appears |
| **A failed turn left no trace of why** | Every rejected draft is stored with its reason in the turn (`ai_messages.content.checks`) and logged; never sent to the UI. The fallback now says what happened: "I couldn't verify every figure in my answer, so I haven't shown it…" |

Tests: 1 more in `tests/test_admin_agent.py`, 1 in `tests/test_admin_tools.py` (written; run once the API server is stopped).

### Colour by value (after "negative bars are still green")

Asked for "green if positive, else red", the chart stayed green: colours could only be set per series, so the
model redrew the same chart and then explained the limitation. `show_chart` now takes
`color_by_threshold`:
- every bar or point at or above `threshold` (default 0) gets `above_color` (default green), the rest
  `below_color` (default red);
- any threshold, for example sellers under an 80% on-time target in red;
- any colours;
- works for bars, lines and areas (coloured points on a neutral line), scatters (by the y value), and one or
  several series;
- the legend explains the rule (for example "≥ 0%" and "< 0%");
- a pie keeps its slice colours and says why.

Tests: 3 more in `tests/test_admin_tools.py` (written; run once the API server is stopped).

### Capability gaps found by probing, before users hit them

After three UI problems with one cause (a predefined tool missing a normal capability, so the model
improvised), each tool was probed with the requests an admin would naturally make. Gaps confirmed with
real calls, and fixed as general options:

| Admin asks | Before | Now |
|---|---|---|
| "Revenue of health_beauty by month", "GMV in SP", "sales of seller X" | **No filters**: grouping by category and month hit the 500-row limit with an error suggesting "a filter" that didn't exist; SQL can't do GMV or states | `filters={grouping: [values]}` on any grouping, combined with AND, as bound parameters; sellers by ID prefix; privacy: a filtered total of a sensitive metric under 5 orders is hidden |
| "Revenue from 15 March to 30 June", "Q2", "last week" | Named periods, years and months only | Ranges (`2025-03-15..2025-06-30`), quarters (`2025-Q2`), days, this/last quarter, this/last week, last 7 days |
| "By quarter / week / day", "which weekday", "which hour" | Month and year only | `day`, `week` (ISO), `quarter` series with no gaps, partial/thin flags and per-row comparisons (same week/quarter/day a year earlier); `weekday` (Monday first) and `hour`. Units the period cuts in half are marked and compared with the same days |
| "Cumulative revenue", "year to date vs last year" | Impossible without mental arithmetic | `cumulative=true`: running totals, and with a comparison the previous running total and its change |
| "Tell me about product X" | No product lookup | `look_up_record(kind="product")`: listing, sales all time and last 12 months, delivery, reviews, returns |
| Charts of the above; "show the values on the bars"; "revenue and orders on one chart" | none | Charts take `filters`, `plot="cumulative"/"cumulative_both"` and `show_values`; weekday/hour charts keep their natural order unless a ranking is asked; two different metrics: a clear rule (two charts, one axis each) |

Checked against independent SQL: filtered revenue, a date range, quarters equal to the sum of their months,
running totals equal to the year total, and the product's units.

Tests: 12 more in `tests/test_admin_tools.py`. The permission probes now also run filters, weekly running
totals with comparisons, and the product lookup on the restricted MySQL user.

### Colour by groups of values; every axis label (prompt admin-v8)

"Bar chart by state, colour every 25% quantile green to red", then "top 10 in 5 colours": the chart could only
colour above/below one threshold, so the model drew one colour, then two, then one again. Also, a vertical bar
chart of 23 states showed only 6 names, and "top revenue by state" used the seller's state without saying so.

| Fix | |
|---|---|
| `color_by` | `threshold` (two groups), `quantiles` (`bins` equal-sized groups, highest first: 4 = quartiles, 5 = quintiles), `ranges` (groups between `breaks`); `bin_colors` per group (highest first) or a default green -> yellow -> red scale; the legend shows each group's value range. The older `color_by_threshold` still works |
| Axis labels | Category axes show every label, angled when there are more than 8 |
| Prompt | When a word fits more than one figure or grouping (revenue: item revenue or GMV; state: buyer's or seller's), the first sentence says which one was used; the layout asked for is part of reading the request |

Checked: 23 states in quartiles are 6/6/6/5 bars; the top 10 in 5 colours are exactly 2 per colour.
Tests: 4 more in `tests/test_admin_tools.py`.

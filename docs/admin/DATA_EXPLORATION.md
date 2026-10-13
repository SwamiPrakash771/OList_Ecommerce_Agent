# Admin Agent, Step 1: Data Exploration

Explored 2026-10-12, following the customer and seller agents' first step: understand the data and
who may see what before writing any tools. The admin agent will be **a separate agent** (its own
tools, prompt, engine, database user, docs and tests), like the seller agent.

## What already exists

| Piece | State |
|---|---|
| **Admin app** | Dashboard, orders, customers, products, sellers, users (reset password, create admin), support tickets, AI settings, system health |
| **Admin chat** | `POST /api/admin/chat` returns a placeholder that promises: business questions (revenue by month, top categories), problem orders (late or undelivered), any customer and their history, seller comparisons |
| **Data catalog** | Admin access is `all` on every table except `cart_items` (none) and `app_users` (never available to any agent). **Personal columns are masked by default**; unmasking is logged with a reason in `ai_admin_access_log` |
| **Admin logins** | 1 admin account |

**Masked for admins by default** (from the catalog):

| Table | Masked columns |
|---|---|
| customers | customer_unique_id, zip prefix, city, state |
| payments | payment type, installments, value |
| customer_addresses, order_addresses | name, phone, street, number, complement, neighbourhood, city, state, zip |
| support_tickets | customer, description, AI summary |
| returns / refunds | customer; refund amount, method, failure reason |
| ai_conversations / ai_messages / ai_tool_calls / ai_pending_actions / ai_feedback | titles, chat content, tool arguments, feedback text |

## The platform's data

| Area | Figures |
|---|---|
| Size | 98,849 orders · 96,096 customers · 3,095 sellers · 32,951 products · 2024-08 → 2026-10 |
| Order status | 96,478 delivered · 1,108 shipped · 625 canceled · 609 unavailable · 315 invoiced · 301 processing · 9 created/approved |
| Money | Item revenue (not canceled) **$13.49M** · payments **$16.01M**; the difference is mostly shipping, so definitions matter |
| Top categories | health_beauty $1.26M · watches_gifts $1.21M · bed_bath_table $1.04M · sports_leisure $0.99M · computers_accessories $0.91M |
| Service quality | 6.8% of deliveries late · average review 4.09 · 14.7% of reviews are 1–2★ |
| Sellers at risk (10+ orders) | 81 with on-time below 80% · 29 with rating below 3 |
| Returns and refunds | 2,875 returns (290 rejected) · refunds: 2,526 completed, **48 failed**, **11 stuck in processing** |
| Support | 1 ticket · 2 product edits |
| AI usage | customer 93 replies ($0.093) · seller 7 ($0.036) · guest 2 ($0.002) · 0 feedback rows |

## What admins will likely ask

| Area | Examples |
|---|---|
| **Business** | revenue / GMV by month, growth, top categories and states, average order value, new vs repeat customers |
| **Sellers** | top sellers, sellers with low on-time rate or rating, sellers with rising returns |
| **Operations** | stuck or failed refunds, late orders, orders stuck in "shipped", open tickets |
| **One record** | an order's full story, a customer's history (masked), a seller's performance |
| **The AI itself** | cost and usage by role, answer ratings, provider errors, limits hit |

## Findings that shape the agent

1. **It's the most powerful agent, so it carries the most risk.** It can read almost everything. Text
   written by other people (reviews, ticket descriptions, chat content) could carry injected
   instructions. A successful injection here reaches more data than in the customer or seller agent.
2. **Personal data is masked by default, and unmasking is a logged, human decision.** The agent
   should work with aggregates and masked identifiers, never unmask by itself.
3. **Definitions matter from day one.** Item revenue vs GMV (payments, incl. shipping), late delivery,
   on-time %, return rate, stuck refunds (beyond the payment method's SLA). We'll use the **metric
   layer** (as the seller agent's `seller_metric`) from the start, with free SQL only as a fallback.
4. **Platform-wide queries are big** (100k orders, 110k items): metrics need a time limit, and results
   must be aggregated (top-N, groupings), never row dumps.
5. **Reading customers' AI chats is sensitive.** Usage, cost and ratings are operational data; the
   content of people's conversations is not something an assistant should read out.
6. **Old leftovers again:** 1,108 orders "shipped" and 315 "invoiced", mostly old historical orders,
   need the same "never completed in the records" rule as the seller agent.

## Decisions needed before step 2

| # | Question | Suggestion |
|---|---|---|
| 1 | Can the admin agent **unmask** personal data (names, addresses, payment values)? | **No.** It answers with aggregates and masked IDs; unmasking stays in the admin screens, logged with a reason |
| 2 | Can it read **customers' chat content** and ticket text? | **No content.** Counts, costs, ratings and statuses only; ticket *categories and status*, not descriptions |
| 3 | Can it **change** anything? | **Read-only at first.** Later, maybe AI limits/settings or ticket status, each behind a confirm card |
| 4 | Revenue definitions | Two metrics with clear labels: **item revenue** (prices, no shipping, excl. canceled) and **GMV** (payments incl. shipping) |
| 5 | Own database user | **Yes**: `olist_admin_agent`, column-level grants; personal columns either not granted or only usable inside aggregates |
| 6 | Audit | Every admin-agent question and lookup is already stored; also log **record-level lookups** (one customer, one order) in `ai_admin_access_log` |
| 7 | **Chat history** (requested) | Store every admin chat (question, answer, each lookup with its metric/SQL and filters, tokens, cost) in the role-tagged `ai_*` tables. **Shown in the admin UI** (Admin → Assistant): a history sidebar with each past chat's title and date, search, and New chat; click to reopen with all messages; under each answer an expandable "Lookups" section shows what was run (metric or SQL, filters, period) and the definition used. Keep admin chats **1 year** as an audit trail (customers: 90 days; new retention class `admin_audit`), not deletable from the page. Each admin sees their own history |

## Plan: same steps as the other agents

| Step | Admin version |
|---|---|
| 1 | **Data exploration and decisions** (this doc) |
| 2 | Test scenarios and ground truth (platform figures, a stuck refund, a weak seller, an order's story) |
| 3 | Security: own tool registry, own MySQL user (`olist_admin_agent`), masking enforced in code and grants |
| 4 | Storage: reuse the role-tagged `ai_*` tables |
| 5 | Tools: `admin_metric` (semantic layer for platform metrics), order and seller lookups, operations lists (stuck refunds, late orders, weak sellers), AI usage; free SQL as a fallback |
| 6 | Permission tests: no unmasked personal data, no chat content, no app_users, read-only |
| 7 | Own prompt and engine; switch on `/api/admin/chat` |
| 8 | Tool consolidation: keep only tools SQL can't safely replace (added after the UI tests) |
| 9 | Evaluation: realistic admin questions, graded from the database |
| 10 | Red-team: injection through reviews and tickets, unmasking, mass export, impersonating another admin |

Files: docs in `docs/admin/`, code in `ai/admin/`, tests in `tests/test_admin_*.py`.

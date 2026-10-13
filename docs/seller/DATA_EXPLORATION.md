# Seller Agent, Step 1: Data Exploration

Explored 2026-10-12, following step 1 of the customer agent ([CUSTOMER_AGENT.md](../CUSTOMER_AGENT.md)):
understand the data and who may see what before writing any tools.

## What already exists

| Piece | State |
|---|---|
| **Data catalog** (`ai/metadata.yaml`) | Seller access defined for 12 tables, with an ownership rule each (below) |
| **Security** (`ai/security.py`) | `Principal` has a `seller_id`, taken from the session (`sid` claim). The model can't supply it; tools that take `seller_id` as an argument are refused at registration |
| **Seller logins** | 3,095 accounts (one per seller; seller_id + default password). The login page offers 2 demo sellers: the two with the most orders |
| **Seller center** (web app) | Dashboard (profile, KPIs, monthly sales, top products), products (list, search, create, edit with audit log), orders |
| **Seller chat** | `POST /api/seller/chat` returns a fixed placeholder that promises: summarise sales, spot problems (late shipments, low-rated products), suggest price/listing changes, update products with confirmation |
| **Seller tools** | None yet |
| **Admin AI settings** | Seller role already has answer mode, model, temperature and history settings (marked "not launched") |

## What a seller may see (catalog)

| Table | Access | Ownership rule | Columns the seller sees |
|---|---|---|---|
| orders | own | orders containing at least one of the seller's items | order_id, status, purchase/approved/carrier/delivered/estimated dates |
| order_items | own | **only the seller's own rows**, never other sellers' items in the same order | order_id, order_item_id, product_id, seller_id, shipping_limit_date, price, freight_value |
| products | own | products whose main seller is the caller | product_id, category, display_name, photos, weight and size |
| product_stats | own | main_seller_id = caller | times_sold, typical_price, avg_freight, avg_rating, num_reviews, rating_rank |
| reviews | own | reviews of orders containing the seller's items | **score, title, comment, dates only (no order_id, no product)** |
| customers | own | customers of the seller's orders | **city and state only** |
| returns | own | returns of orders with the seller's items | reason, status, dates, decision note, items, *source* |
| refunds | own | refunds of those returns | status and dates; **no amount, no payment method** |
| sellers, seller_stats | all | — | city, state, orders fulfilled, avg rating, on-time % (public figures) |
| product_edits | own | edits to the caller's products | what changed, who, when |
| category_translation | all | — | category names |

**Never visible to sellers:** customer names, IDs, addresses, payments, other sellers' items or
revenue, app_users.

## The data

**Overall:**
- **3,095 sellers** in 23 states; 60% are in São Paulo (SP).
- **Very uneven size.** Units sold per seller: 509 sold once, 1,275 sold 2–10, 1,075 sold 11–100,
  236 sold over 100. The largest sold 2,033.
- **Totals:** $13.6M of item revenue and $2.25M of freight over 98,667 orders.
- **3,103 items sit in orders shared by more than one seller,** so a seller's "order total" must
  only count the seller's own items.
- **Order dates run 2024-08-25 → 2026-10-11.**
  - Only **1 seller has an order in the last 30 days**, and 1,521 sellers in the last 90 days.
  - 737 sellers have had no orders since 2025.

**The three sellers profiled:**

| | Demo seller 1 (São Paulo, SP) | Demo seller 2 (Ibitinga, SP) | Typical seller (Ibitinga, SP) |
|---|---|---|---|
| Orders / units | 1,854 / 2,033 | 1,806 / 1,987 | 38 / 39 |
| Products sold (main seller of) | 256 (256) | 399 (399) | 18 (15) |
| Item revenue / freight | $123,305 / $27,961 | $200,473 / $35,067 | $4,733 / $1,287 |
| Active | 2025-02 → 2026-08 | 2024-12 → 2026-08 | 2025-11 → 2026-08 |
| Order status | 1,819 delivered, 25 shipped, 3 invoiced, 7 canceled | 1,772 delivered, 31 shipped, 1 invoiced, 2 canceled | 37 delivered, 1 shipped |
| Reviews | avg 3.94; 319 of 1,844 are 1–2★ | avg 3.83; 345 of 1,801 are 1–2★ | avg 4.08; 6 of 38 are 1–2★ |
| Late deliveries | 5.3% | 9.7% | 10.8% |
| Returns / refunds | 58 / 55 (2 failed) | 65 / 63 (1 failed) | 2 / 2 |

**Reviews:**
- They're in **Portuguese**, and about half have a comment (866 of 1,844 for demo seller 1).
- Example 1★: *"O relógio… Não funciona!!!"* ("The watch… doesn't work!!!").
- Example 2★: *"Frete muito lento!"* ("Shipping very slow!").

## Findings that shape the agent

1. **Reviews can't be linked to products.** Sellers see the review text and score, but not the
   order or product it belongs to. "Which of my products get bad reviews?", likely a top seller
   question, can't be answered today. `product_stats.avg_rating` gives per-product averages, but
   not the comments.
2. **"Open" orders that never closed.** Demo seller 1 has 25 orders "shipped" since as early as
   2025-02 and never delivered, plus 3 "invoiced". These are leftovers in the Olist data, not real
   work. An agent asked "what do I still need to ship?" would list year-old orders unless told how
   to treat them.
3. **Little recent activity.** Most sellers' last order is August 2026 or earlier. "This month" or
   "last 30 days" questions will usually return nothing, and the agent must say so plainly (the
   same rule as for customers).
4. **Refund amounts are hidden from sellers.** They see that a refund exists and its status, but
   not how much it was. That's probably too strict, since it's money from their own sale.
5. **Developer-only data is visible.** `returns.source` and `refunds.source` say "simulated". The
   agent must never pass that on (the customer agent already filters it out of its metadata).
6. **Multi-seller orders.** Revenue, averages and "order value" must use only the seller's own
   items. The ownership rule already enforces this; the SQL tool and its definitions must follow it.
7. **Review text is untrusted.** It's written by buyers and could contain instructions. It's
   wrapped as untrusted data, as for the customer agent.
8. **What sellers will likely ask** (from the placeholder and the data):
   - sales by month, product or category, and growth;
   - best and worst products;
   - ratings and what customers complain about;
   - late deliveries and shipping deadlines (`shipping_limit_date`);
   - returns and reasons;
   - price changes (with confirmation).

## Decisions needed before step 2

| # | Question | Suggestion |
|---|---|---|
| 1 | Let sellers see which **product** a review is about (product_id only, no buyer identity)? | **Yes**: it's the most useful seller insight, with no privacy cost |
| 2 | How to treat orders "shipped" or "invoiced" for months? | Count as open only if within a recent window (e.g. 60 days); otherwise call them "never completed in the records" |
| 3 | Show **refund amounts** to the seller? | **Yes**: it's their own sale's money; still hide the payment method |
| 4 | Hide `returns.source` / `refunds.source` from the agent? | **Yes**, as for customers |
| 5 | Which changes may the agent **propose** (with a confirm card)? | Price and listing details (title, category, size, weight) of the seller's own products; not orders, refunds or other sellers' data |
| 6 | Customer data for sellers | Keep **city and state only** |

## Plan: following the customer agent's steps

| Step | Seller version |
|---|---|
| 1 | **Data exploration and catalog decisions** (this doc) |
| 2 | Test scenarios: pick demo sellers and the cases to test (late orders, low ratings, returns) |
| 3 | Security: seller identity is already in place; add seller checks to the permission tests |
| 4 | Storage: reuse the `ai_*` tables (already role-aware) |
| 5 | Seller tools: sales, products, reviews, orders and shipping, returns; `query_seller_data` SQL tool over `seller_*` tables; product edits with confirmation |
| 6 | Permission tests: a seller can never reach another seller's data |
| 7 | Seller prompt; switch on the engine for the seller role (API, chat UI) |
| 8 | Evaluation: natural seller questions and tricky questions, graded from the database |
| 9 | Red-team: a seller trying to see competitors' sales, customers' identities, or to edit others' products |
| 10+ | Cost, latency, hybrid local answers, admin settings, docs |

Seller files: docs in `docs/seller/`, code in `ai/seller/` (tools, prompt, evaluations), and tests
in `tests/` (`test_seller_*.py`). The shared engine (`ai/agent.py`, storage, security) is reused,
not copied.

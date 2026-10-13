# Olist E-commerce Agent

A full-stack e-commerce store built on the public
[Olist Brazilian E-Commerce dataset](https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce)
(~99k orders, 32k products, 3k sellers, 2016–2018), with separate experiences for
customers, sellers and admins, and an agentic customer-support assistant (Pydantic AI).

## Features

**Customers**
- Store home, category browsing, search with price/rating filters and sorting
- Product pages with reviews, seller reliability and related products
- Cart, checkout (card with installments, debit, boleto, voucher) and editable shipping address
- Order history and order tracking

**Sellers** (Seller Center)
- The same store UI, showing only the seller's own products
- Sales dashboard, product management (edit and add products, with an edit history), orders

**Admins**
- Browse the full store (no cart)
- Dashboard with revenue, orders, ratings and on-time delivery
- All orders, customers, sellers, read-only product details, user accounts

**AI assistants**
- A full-page assistant tab and a floating chat bubble for each role, streaming over Server-Sent Events
- Customer support agent (customers and guests): orders, delivery, returns, refunds (with SLA checks),
  cart, address, tickets and hand-off to a person. Risky changes show a confirm card and only run
  when the customer clicks Confirm; cart changes can be undone
- Fact check: every ID and $ amount in a reply must come from a tool result, or the model retries
- Per-role and per-user limits (messages, tokens, rate, tool calls), kill switches, saved
  conversations, usage and feedback
- Set `AI_MODEL` in `.env` to switch it on; without it replies are placeholders.
  The seller and admin assistants are still placeholders

Every API request is checked on the server: customers only reach their own orders,
cart and address, and sellers only reach their own products and orders.

## Tech stack

| Layer | Tools |
|---|---|
| Database | MySQL 8 (DuckDB is used once to clean and load the CSVs) |
| Backend | Python, FastAPI, PyMySQL (pooled connections), JWT session cookies |
| Frontend | React 19, TypeScript, Vite, Tailwind CSS 4, Recharts |

## Project layout

```
build_db.py      Load the Kaggle CSVs into data/olist.duckdb and clean them
load_mysql.py    Copy the cleaned tables into MySQL (with keys and indexes)
seed_users.py    Create app tables (logins, cart, addresses, product edits) and seed accounts
db.py            MySQL connection pool and query helpers
auth.py          Password hashing (PBKDF2) and login
repository.py    All SQL, split by role: Catalog, Customer, Seller, Admin
api/main.py      FastAPI app: REST API + serves the built frontend
ai/              AI agent groundwork: data catalog (metadata.yaml, catalog.py), simulated
                 returns/refunds, security layer (security.py, db_user.py), AI storage
                 (schema.py, store.py), background jobs (scheduler.py), store policies
                 (policies.py), agent tools (tools/: catalog and customer support),
                 system prompts (prompts.py), agent engine (agent.py)
validation.py    Input rules shared by the API and the agent tools (addresses)
tests/           Security, storage/scheduler, customer-tool and agent tests (python -m pytest tests -q)
docs/            AI_PLAN.md (master plan), DATA_DICTIONARY.md (generated)
web/             React frontend (Vite)
app.py, ui/      Earlier Streamlit prototype (not used by the main app)
```

## Setup

Requirements: Python 3.11+, Node.js 20+, MySQL 8.

```bash
# 1. Python dependencies (dev includes tests, data tools, and optional local hybrid model)
pip install -r requirements-dev.txt

# 2. Settings: copy the example and fill in your MySQL password
cp .env.example .env

# 3. Download the dataset into data/raw (public Kaggle dataset)
python -c "import kagglehub; kagglehub.dataset_download('olistbr/brazilian-ecommerce', output_dir='data/raw')"

# 4. Build the database
python build_db.py      # CSVs -> data/olist.duckdb (cleaned)
python load_mysql.py    # -> MySQL database from MYSQL_DATABASE (drops and recreates it)
python seed_users.py    # app tables + logins (prints the admin password if none is set)
python -m ai.simulate_returns         # simulated returns/refunds for the support demo
python shift_dates.py                 # move the 2016-2018 dates to 2024-2026 (undo: --undo)
python -m ai.metadata                 # check the AI data catalog matches the database
python -m ai.catalog sync             # copy the catalog into the meta_* tables
python -m ai.catalog profile          # live column stats + drift alerts (run regularly)
python -m ai.schema                   # AI conversation/usage/limits tables + scheduler tables
python -m ai.db_user                  # least-privilege MySQL user for AI tools
python -m pytest tests -q             # security tests (must all pass)
python -m ai.build_data_dictionary    # regenerate docs/DATA_DICTIONARY.md

# 5. Frontend
cd web
npm install
npm run build
cd ..

# 6. Run
python -m uvicorn api.main:app --port 8000
```

Open http://localhost:8000.

For a production image, install `requirements.txt` instead of `requirements-dev.txt` and set
`AI_LOCAL_EMBEDDINGS_ENABLED=false`. This keeps Torch and sentence-transformers out of the
production install and prevents an old database `hybrid` setting from loading the local model.
All assistant roles then use the configured hosted LLM. Tool-pack RAG routing remains off unless
enabled separately in `ai_settings`.

Background jobs (catalog profiling, usage roll-up, expiring unconfirmed actions, chat
retention) run in **one separate process** next to the API:

```bash
python -m ai.scheduler run          # keep running alongside the API
python -m ai.scheduler status       # jobs, next run, last result, overdue warnings
python -m ai.scheduler run-now usage_rollup
```

For frontend development with live reload, run the API as above and `npm run dev`
in `web/`, then open http://localhost:5173 (API calls are proxied to port 8000).

## Signing in

| Account | Username | Default password |
|---|---|---|
| Customer | any `customer_unique_id` | `customer123` |
| Seller | any `seller_id` | `seller123` |
| Admin | `ADMIN_USERNAME` from `.env` | `ADMIN_PASSWORD` from `.env` |

With `DEMO_LOGIN=true`, the sign-in page also shows one-click test logins
(5 customers, 2 sellers, admin). Turn it off before letting anyone else use the site.

## Notes

- The dataset has no product names or photos, so products are named after their
  category and short code (e.g. "Watches gifts #53b36d"), with generated icon images.
- Prices are the dataset's values shown with a `$` sign. Reviews are in Portuguese.
- Dates: the original Olist data runs from 2016-09 to 2018-10. `shift_dates.py` moves every
  historical date forward 416 weeks (weekdays preserved), so the data runs from 2024-08 to
  2026-10-07 and questions like "last 12 months" find real data. Gaps between dates don't change.
- Each product belongs to its main seller (the seller who sold it most).

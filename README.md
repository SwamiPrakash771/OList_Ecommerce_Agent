# Olist E-commerce Agent

A full-stack e-commerce store built on the public
[Olist Brazilian E-Commerce dataset](https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce)
(~99k orders, 32k products, 3k sellers, 2016–2018), with separate experiences for
customers, sellers and admins, and AI assistant chat built in (the AI model is not connected yet).

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
- A full-page assistant tab and a floating chat bubble for each role
  (shopping, seller and admin assistants), streaming over Server-Sent Events.
  Replies are placeholders until an AI model is connected.

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
web/             React frontend (Vite)
app.py, ui/      Earlier Streamlit prototype (not used by the main app)
```

## Setup

Requirements: Python 3.11+, Node.js 20+, MySQL 8.

```bash
# 1. Python dependencies
pip install -r requirements.txt

# 2. Settings: copy the example and fill in your MySQL password
cp .env.example .env

# 3. Download the dataset into data/raw (public Kaggle dataset)
python -c "import kagglehub; kagglehub.dataset_download('olistbr/brazilian-ecommerce', output_dir='data/raw')"

# 4. Build the database
python build_db.py      # CSVs -> data/olist.duckdb (cleaned)
python load_mysql.py    # -> MySQL database from MYSQL_DATABASE (drops and recreates it)
python seed_users.py    # app tables + logins (prints the admin password if none is set)

# 5. Frontend
cd web
npm install
npm run build
cd ..

# 6. Run
python -m uvicorn api.main:app --port 8000
```

Open http://localhost:8000.

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
- Each product belongs to its main seller (the seller who sold it most).

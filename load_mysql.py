"""Copy the cleaned tables from data/olist.duckdb into MySQL.

Usage:  python build_db.py      (once, builds the DuckDB file)
        python load_mysql.py

Connection settings come from .env: MYSQL_HOST, MYSQL_PORT, MYSQL_USER,
MYSQL_PASSWORD, MYSQL_DATABASE. The target database is dropped and recreated.
"""

import os
import time
from pathlib import Path

import duckdb
import pymysql
from dotenv import load_dotenv

ROOT = Path(__file__).parent
DUCKDB_PATH = ROOT / "data" / "olist.duckdb"
BATCH_SIZE = 5000

# Parents before children so the foreign keys added at the end line up.
SCHEMA = {
    "category_translation": """
        product_category_name         VARCHAR(64) PRIMARY KEY,
        product_category_name_english VARCHAR(64) NOT NULL
    """,
    "geolocation": """
        zip_code_prefix CHAR(5) PRIMARY KEY,
        lat             DOUBLE,
        lng             DOUBLE,
        city            VARCHAR(64),
        state           CHAR(2)
    """,
    "customers": """
        customer_id              CHAR(32) PRIMARY KEY,
        customer_unique_id       CHAR(32) NOT NULL,
        customer_zip_code_prefix CHAR(5),
        customer_city            VARCHAR(64),
        customer_state           CHAR(2),
        INDEX idx_customer_unique (customer_unique_id)
    """,
    "sellers": """
        seller_id              CHAR(32) PRIMARY KEY,
        seller_zip_code_prefix CHAR(5),
        seller_city            VARCHAR(64),
        seller_state           CHAR(2)
    """,
    "products": """
        product_id   CHAR(32) PRIMARY KEY,
        category     VARCHAR(64),
        display_name VARCHAR(100),
        photos       INT,
        weight_g     INT,
        length_cm    INT,
        height_cm    INT,
        width_cm     INT,
        INDEX idx_products_category (category)
    """,
    "orders": """
        order_id                      CHAR(32) PRIMARY KEY,
        customer_id                   CHAR(32) NOT NULL,
        order_status                  VARCHAR(16),
        order_purchase_timestamp      DATETIME,
        order_approved_at             DATETIME,
        order_delivered_carrier_date  DATETIME,
        order_delivered_customer_date DATETIME,
        order_estimated_delivery_date DATETIME,
        INDEX idx_orders_purchase (order_purchase_timestamp)
    """,
    "order_items": """
        order_id            CHAR(32) NOT NULL,
        order_item_id       INT NOT NULL,
        product_id          CHAR(32) NOT NULL,
        seller_id           CHAR(32) NOT NULL,
        shipping_limit_date DATETIME,
        price               DECIMAL(10,2),
        freight_value       DECIMAL(10,2),
        PRIMARY KEY (order_id, order_item_id)
    """,
    "payments": """
        order_id             CHAR(32) NOT NULL,
        payment_sequential   INT NOT NULL,
        payment_type         VARCHAR(16),
        payment_installments INT,
        payment_value        DECIMAL(10,2),
        PRIMARY KEY (order_id, payment_sequential)
    """,
    # review_id alone repeats across orders in the source data.
    "reviews": """
        review_id               CHAR(32) NOT NULL,
        order_id                CHAR(32) NOT NULL,
        review_score            TINYINT,
        review_comment_title    VARCHAR(255),
        review_comment_message  TEXT,
        review_creation_date    DATETIME,
        review_answer_timestamp DATETIME,
        PRIMARY KEY (review_id, order_id)
    """,
    "product_stats": """
        product_id     CHAR(32) PRIMARY KEY,
        display_name   VARCHAR(100),
        category       VARCHAR(64),
        times_sold     INT,
        typical_price  DECIMAL(10,2),
        avg_freight    DECIMAL(10,2),
        avg_rating     DECIMAL(4,2),
        num_reviews    INT,
        main_seller_id CHAR(32),
        -- "Top rated" sort key: products with 3+ reviews first, then by rating.
        rating_rank    DECIMAL(5,2) AS ((num_reviews >= 3) * 10 + COALESCE(avg_rating, 0)) STORED,
        -- One index per storefront sort order (including the product_id tie-breaker),
        -- so pages are read in index order instead of sorting all products.
        INDEX idx_stats_category (category),
        INDEX idx_stats_sold (times_sold, product_id),
        INDEX idx_stats_price (typical_price, product_id),
        INDEX idx_stats_rank (rating_rank, num_reviews, product_id),
        INDEX idx_stats_rating (avg_rating, num_reviews),
        INDEX idx_stats_cat_sold (category, times_sold, product_id),
        INDEX idx_stats_seller (main_seller_id, times_sold, product_id)
    """,
    "seller_stats": """
        seller_id        CHAR(32) PRIMARY KEY,
        seller_city      VARCHAR(64),
        seller_state     CHAR(2),
        orders_fulfilled INT,
        avg_rating       DECIMAL(4,2),
        on_time_pct      DECIMAL(5,1)
    """,
    "support_tickets": """
        ticket_id          VARCHAR(36) PRIMARY KEY,
        customer_unique_id CHAR(32) NOT NULL,
        order_id           CHAR(32),
        issue_type         VARCHAR(32),
        description        TEXT,
        status             VARCHAR(16),
        created_at         DATETIME,
        INDEX idx_tickets_customer (customer_unique_id)
    """,
}

# The relationships from the Olist schema diagram. Zip-code links to
# geolocation are left as plain joins: some zips have no coordinates.
FOREIGN_KEYS = [
    ("orders", "customer_id", "customers", "customer_id"),
    ("order_items", "order_id", "orders", "order_id"),
    ("order_items", "product_id", "products", "product_id"),
    ("order_items", "seller_id", "sellers", "seller_id"),
    ("payments", "order_id", "orders", "order_id"),
    ("reviews", "order_id", "orders", "order_id"),
    ("product_stats", "product_id", "products", "product_id"),
    ("seller_stats", "seller_id", "sellers", "seller_id"),
    ("support_tickets", "order_id", "orders", "order_id"),
]


def main() -> None:
    load_dotenv(ROOT / ".env")
    db_name = os.environ["MYSQL_DATABASE"]

    src = duckdb.connect(str(DUCKDB_PATH), read_only=True)
    dst = pymysql.connect(
        host=os.environ["MYSQL_HOST"],
        port=int(os.environ.get("MYSQL_PORT", 3306)),
        user=os.environ["MYSQL_USER"],
        password=os.environ["MYSQL_PASSWORD"],
        charset="utf8mb4",
        autocommit=False,
    )
    cur = dst.cursor()

    cur.execute(f"DROP DATABASE IF EXISTS `{db_name}`")
    cur.execute(f"CREATE DATABASE `{db_name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
    cur.execute(f"USE `{db_name}`")

    for table, columns in SCHEMA.items():
        cur.execute(f"CREATE TABLE `{table}` ({columns}) ENGINE=InnoDB")

        result = src.execute(f"SELECT * FROM {table}")
        col_names = [d[0] for d in result.description]
        insert_sql = (
            f"INSERT INTO `{table}` ({', '.join(f'`{c}`' for c in col_names)}) "
            f"VALUES ({', '.join(['%s'] * len(col_names))})"
        )

        started = time.time()
        total = 0
        while rows := result.fetchmany(BATCH_SIZE):
            cur.executemany(insert_sql, rows)
            total += len(rows)
        dst.commit()
        print(f"loaded {table:<22} {total:>8,} rows  ({time.time() - started:.1f}s)")

    print()
    for child, col, parent, parent_col in FOREIGN_KEYS:
        cur.execute(
            f"ALTER TABLE `{child}` ADD CONSTRAINT `fk_{child}_{col}` "
            f"FOREIGN KEY (`{col}`) REFERENCES `{parent}` (`{parent_col}`)"
        )
        print(f"foreign key {child}.{col} -> {parent}.{parent_col}")
    dst.commit()

    dst.close()
    src.close()
    print(f"\nMySQL database `{db_name}` is ready")


if __name__ == "__main__":
    main()

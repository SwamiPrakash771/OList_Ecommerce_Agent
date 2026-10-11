"""Load the Olist CSVs into a single DuckDB file with a few helper tables.

Usage:  python build_db.py
Reads   data/raw/*.csv
Writes  data/olist.duckdb
"""

from pathlib import Path

import duckdb

ROOT = Path(__file__).parent
RAW = ROOT / "data" / "raw"
DB_PATH = ROOT / "data" / "olist.duckdb"

TABLES = {
    "customers": "olist_customers_dataset.csv",
    "orders": "olist_orders_dataset.csv",
    "order_items": "olist_order_items_dataset.csv",
    "payments": "olist_order_payments_dataset.csv",
    "reviews": "olist_order_reviews_dataset.csv",
    "products_raw": "olist_products_dataset.csv",
    "sellers": "olist_sellers_dataset.csv",
    "category_translation": "product_category_name_translation.csv",
}


def main() -> None:
    DB_PATH.unlink(missing_ok=True)
    con = duckdb.connect(str(DB_PATH))

    for table, filename in TABLES.items():
        path = (RAW / filename).as_posix()
        con.execute(
            f"CREATE TABLE {table} AS SELECT * FROM read_csv_auto('{path}', header=true, all_varchar=false)"
        )
        if table == "category_translation":
            # The file starts with a UTF-8 BOM that can leak into the first column name.
            first_col = con.execute(f"SELECT * FROM {table} LIMIT 0").description[0][0]
            if first_col != "product_category_name":
                con.execute(f'ALTER TABLE {table} RENAME "{first_col}" TO product_category_name')
        n = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        print(f"loaded {table:<22} {n:>8,} rows")

    # Geolocation has ~1M rows with many duplicates per zip prefix; keep one
    # averaged point per prefix so it joins 1:1 to customers and sellers.
    geo_path = (RAW / "olist_geolocation_dataset.csv").as_posix()
    con.execute(f"""
        CREATE TABLE geolocation AS
        SELECT geolocation_zip_code_prefix AS zip_code_prefix,
               avg(geolocation_lat) AS lat,
               avg(geolocation_lng) AS lng,
               mode(geolocation_city) AS city,
               mode(geolocation_state) AS state
        FROM read_csv_auto('{geo_path}', header=true, types={{'geolocation_zip_code_prefix': 'VARCHAR'}})
        GROUP BY 1
    """)
    n = con.execute("SELECT count(*) FROM geolocation").fetchone()[0]
    print(f"loaded {'geolocation':<22} {n:>8,} rows (one per zip prefix)")

    # Two categories in the products file are missing from the translation file.
    con.execute("""
        INSERT INTO category_translation VALUES
            ('pc_gamer', 'pc_gamer'),
            ('portateis_cozinha_e_preparadores_de_alimentos', 'portable_kitchen_food_preparers')
    """)

    # Products with English category names. The dataset has no product titles,
    # so build a readable display name from the category and a short id.
    con.execute("""
        CREATE TABLE products AS
        SELECT
            p.product_id,
            coalesce(t.product_category_name_english, p.product_category_name, 'uncategorized') AS category,
            upper(left(coalesce(t.product_category_name_english, 'item'), 1))
                || replace(substr(coalesce(t.product_category_name_english, 'item'), 2), '_', ' ')
                || ' #' || left(p.product_id, 6) AS display_name,
            p.product_photos_qty AS photos,
            p.product_weight_g AS weight_g,
            p.product_length_cm AS length_cm,
            p.product_height_cm AS height_cm,
            p.product_width_cm AS width_cm
        FROM products_raw p
        LEFT JOIN category_translation t USING (product_category_name)
    """)

    # Per-product sales and rating stats, used for search and recommendations.
    con.execute("""
        CREATE TABLE product_stats AS
        WITH sales AS (
            SELECT product_id,
                   count(DISTINCT order_id)    AS times_sold,
                   round(median(price), 2)     AS typical_price,
                   round(avg(freight_value), 2) AS avg_freight,
                   mode(seller_id)             AS main_seller_id
            FROM order_items GROUP BY product_id
        ),
        ratings AS (
            SELECT product_id,
                   round(avg(review_score), 2) AS avg_rating,
                   count(*)                    AS num_reviews
            FROM (SELECT DISTINCT oi.order_id, oi.product_id, r.review_id, r.review_score
                  FROM order_items oi JOIN reviews r USING (order_id))
            GROUP BY product_id
        )
        SELECT p.product_id, p.display_name, p.category,
               s.times_sold, s.typical_price, s.avg_freight,
               r.avg_rating, coalesce(r.num_reviews, 0) AS num_reviews,
               s.main_seller_id
        FROM products p
        JOIN sales s USING (product_id)
        LEFT JOIN ratings r USING (product_id)
    """)

    # Per-seller reliability stats.
    con.execute("""
        CREATE TABLE seller_stats AS
        SELECT
            s.seller_id, s.seller_city, s.seller_state,
            count(DISTINCT oi.order_id) AS orders_fulfilled,
            round(avg(r.review_score), 2) AS avg_rating,
            round(100.0 * avg(CASE WHEN o.order_delivered_customer_date <= o.order_estimated_delivery_date
                                   THEN 1 ELSE 0 END)
                  FILTER (WHERE o.order_delivered_customer_date IS NOT NULL), 1) AS on_time_pct
        FROM sellers s
        JOIN order_items oi USING (seller_id)
        JOIN orders o USING (order_id)
        LEFT JOIN reviews r USING (order_id)
        GROUP BY ALL
    """)

    # Tickets the agent opens on behalf of customers.
    con.execute("""
        CREATE TABLE support_tickets (
            ticket_id VARCHAR PRIMARY KEY,
            customer_unique_id VARCHAR,
            order_id VARCHAR,
            issue_type VARCHAR,
            description VARCHAR,
            status VARCHAR,
            created_at TIMESTAMP
        )
    """)

    con.execute("CREATE INDEX idx_customers_unique ON customers(customer_unique_id)")
    con.execute("CREATE INDEX idx_orders_customer ON orders(customer_id)")
    con.execute("CREATE INDEX idx_items_order ON order_items(order_id)")
    con.close()
    print(f"\nwrote {DB_PATH}")


if __name__ == "__main__":
    main()

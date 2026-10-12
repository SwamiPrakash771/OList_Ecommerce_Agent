"""Create the app tables (app_users, cart, addresses, product edits) and seed logins.

Usage:  python seed_users.py          (run after load_mysql.py; safe to re-run)

- Customers log in with their customer_unique_id and CUSTOMER_DEFAULT_PASSWORD
  (default "customer123").
- Sellers log in with their seller_id and SELLER_DEFAULT_PASSWORD (default "seller123").
- The admin account comes from ADMIN_USERNAME / ADMIN_PASSWORD in .env. If no
  ADMIN_PASSWORD is set, a random one is generated and printed once.
Seeded accounts are asked to change their password on first login.
"""

import os
import secrets

import auth
import db


def _has_column(table: str, column: str) -> bool:
    return bool(db.query_one(
        "SELECT 1 AS ok FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = %s AND column_name = %s",
        (table, column),
    ))


def migrate_app_users_for_sellers() -> None:
    """Bring an app_users table created before seller accounts up to date."""
    if _has_column("app_users", "seller_id"):
        return
    old_check = db.query_one(
        "SELECT constraint_name AS name FROM information_schema.table_constraints "
        "WHERE table_schema = DATABASE() AND table_name = 'app_users' AND constraint_type = 'CHECK'"
    )
    if old_check:
        db.execute(f"ALTER TABLE app_users DROP CHECK `{old_check['name']}`")
    db.execute("""
        ALTER TABLE app_users
            MODIFY role ENUM('customer', 'admin', 'seller') NOT NULL,
            ADD COLUMN seller_id CHAR(32) NULL AFTER customer_unique_id,
            ADD INDEX idx_app_users_seller (seller_id),
            ADD CONSTRAINT chk_app_users_role CHECK (
                (role = 'admin')
                OR (role = 'customer' AND customer_unique_id IS NOT NULL)
                OR (role = 'seller' AND seller_id IS NOT NULL)
            )
    """)
    print("app_users migrated for seller accounts")


def main() -> None:
    db.execute("""
        CREATE TABLE IF NOT EXISTS app_users (
            user_id              INT AUTO_INCREMENT PRIMARY KEY,
            username             VARCHAR(64) NOT NULL UNIQUE,
            password_hash        VARCHAR(160) NOT NULL,
            role                 ENUM('customer', 'admin', 'seller') NOT NULL,
            customer_unique_id   CHAR(32) NULL,
            seller_id            CHAR(32) NULL,
            must_change_password TINYINT(1) NOT NULL DEFAULT 1,
            is_active            TINYINT(1) NOT NULL DEFAULT 1,
            created_at           DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_login           DATETIME NULL,
            INDEX idx_app_users_customer (customer_unique_id),
            INDEX idx_app_users_seller (seller_id),
            CONSTRAINT chk_app_users_role CHECK (
                (role = 'admin')
                OR (role = 'customer' AND customer_unique_id IS NOT NULL)
                OR (role = 'seller' AND seller_id IS NOT NULL)
            )
        ) ENGINE=InnoDB
    """)
    migrate_app_users_for_sellers()

    # One cart per customer; checkout turns it into rows in orders/order_items/payments.
    db.execute("""
        CREATE TABLE IF NOT EXISTS cart_items (
            customer_unique_id CHAR(32) NOT NULL,
            product_id         CHAR(32) NOT NULL,
            quantity           INT NOT NULL CHECK (quantity BETWEEN 1 AND 20),
            added_at           DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (customer_unique_id, product_id),
            CONSTRAINT fk_cart_product FOREIGN KEY (product_id) REFERENCES products (product_id)
        ) ENGINE=InnoDB
    """)

    # Olist only records city/state/zip prefix per customer. Customers can save a
    # full shipping address; each order keeps a copy of the address it shipped to,
    # so editing the address later doesn't rewrite past orders.
    address_columns = """
            recipient_name VARCHAR(100) NOT NULL,
            phone          VARCHAR(30),
            street         VARCHAR(150) NOT NULL,
            number         VARCHAR(20) NOT NULL,
            complement     VARCHAR(100),
            neighborhood   VARCHAR(100),
            city           VARCHAR(64) NOT NULL,
            state          CHAR(2) NOT NULL,
            zip_code       VARCHAR(9) NOT NULL
    """
    db.execute(f"""
        CREATE TABLE IF NOT EXISTS customer_addresses (
            customer_unique_id CHAR(32) PRIMARY KEY,
            {address_columns},
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB
    """)
    db.execute(f"""
        CREATE TABLE IF NOT EXISTS order_addresses (
            order_id CHAR(32) PRIMARY KEY,
            {address_columns},
            CONSTRAINT fk_order_address_order FOREIGN KEY (order_id) REFERENCES orders (order_id)
        ) ENGINE=InnoDB
    """)

    # Every edit to a product (made by its seller), with the values before and after.
    db.execute("""
        CREATE TABLE IF NOT EXISTS product_edits (
            edit_id     INT AUTO_INCREMENT PRIMARY KEY,
            product_id  CHAR(32) NOT NULL,
            edited_by   VARCHAR(64) NOT NULL,
            editor_role VARCHAR(16) NOT NULL DEFAULT 'seller',
            changes     JSON NOT NULL,
            edited_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_product_edits_product (product_id, edited_at),
            CONSTRAINT fk_product_edits_product FOREIGN KEY (product_id) REFERENCES products (product_id)
        ) ENGINE=InnoDB
    """)
    if _has_column("product_edits", "admin_user"):  # table from before sellers existed
        db.execute("ALTER TABLE product_edits RENAME COLUMN admin_user TO edited_by, "
                   "ADD COLUMN editor_role VARCHAR(16) NOT NULL DEFAULT 'admin' AFTER edited_by")

    # Returns and refunds. Olist has none, so ai/simulate_returns.py fills these for the
    # support demo (source = 'simulated'); returns created in the app use source = 'app'.
    db.execute("""
        CREATE TABLE IF NOT EXISTS returns (
            return_id          CHAR(32) PRIMARY KEY,
            order_id           CHAR(32) NOT NULL,
            customer_unique_id CHAR(32) NOT NULL,
            items              JSON NOT NULL,
            reason             VARCHAR(24) NOT NULL,
            status             VARCHAR(16) NOT NULL,
            requested_at       DATETIME NOT NULL,
            decided_at         DATETIME NULL,
            received_at        DATETIME NULL,
            decision_note      VARCHAR(255) NULL,
            source             VARCHAR(12) NOT NULL DEFAULT 'app',
            INDEX idx_returns_order (order_id),
            INDEX idx_returns_customer (customer_unique_id, requested_at),
            CONSTRAINT fk_returns_order FOREIGN KEY (order_id) REFERENCES orders (order_id),
            CHECK (reason IN ('damaged', 'wrong_item', 'not_as_described', 'changed_mind', 'late_delivery')),
            CHECK (status IN ('requested', 'approved', 'received', 'rejected'))
        ) ENGINE=InnoDB
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS refunds (
            refund_id             CHAR(32) PRIMARY KEY,
            return_id             CHAR(32) NOT NULL UNIQUE,
            order_id              CHAR(32) NOT NULL,
            amount                DECIMAL(10,2) NOT NULL,
            payment_method        VARCHAR(16) NOT NULL,
            status                VARCHAR(12) NOT NULL,
            created_at            DATETIME NOT NULL,
            processing_started_at DATETIME NULL,
            completed_at          DATETIME NULL,
            failure_reason        VARCHAR(255) NULL,
            source                VARCHAR(12) NOT NULL DEFAULT 'app',
            INDEX idx_refunds_order (order_id),
            INDEX idx_refunds_status_started (status, processing_started_at),
            CONSTRAINT fk_refunds_return FOREIGN KEY (return_id) REFERENCES returns (return_id),
            CONSTRAINT fk_refunds_order FOREIGN KEY (order_id) REFERENCES orders (order_id),
            CHECK (status IN ('pending', 'processing', 'completed', 'failed')),
            CHECK (payment_method IN ('credit_card', 'debit_card', 'boleto', 'voucher'))
        ) ENGINE=InnoDB
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS refund_events (
            event_id   INT AUTO_INCREMENT PRIMARY KEY,
            refund_id  CHAR(32) NOT NULL,
            status     VARCHAR(12) NOT NULL,
            note       VARCHAR(255) NULL,
            created_at DATETIME NOT NULL,
            INDEX idx_refund_events_refund (refund_id, created_at),
            CONSTRAINT fk_refund_events_refund FOREIGN KEY (refund_id) REFERENCES refunds (refund_id)
        ) ENGINE=InnoDB
    """)

    # Hashing 96k passwords one by one would take over an hour, so every seeded
    # customer starts with the same default hash. Changing the password gives
    # that user a fresh salt.
    default_password = os.environ.get("CUSTOMER_DEFAULT_PASSWORD", "customer123")
    default_hash = auth.hash_password(default_password)
    added = db.execute(
        """
        INSERT IGNORE INTO app_users (username, password_hash, role, customer_unique_id)
        SELECT DISTINCT customer_unique_id, %s, 'customer', customer_unique_id
        FROM customers
        """,
        (default_hash,),
    )
    print(f"customer logins added: {added:,} (default password: {default_password})")

    # Sellers log in with their seller_id and SELLER_DEFAULT_PASSWORD.
    seller_password = os.environ.get("SELLER_DEFAULT_PASSWORD", "seller123")
    added = db.execute(
        """
        INSERT IGNORE INTO app_users (username, password_hash, role, seller_id)
        SELECT seller_id, %s, 'seller', seller_id FROM sellers
        """,
        (auth.hash_password(seller_password),),
    )
    print(f"seller logins added: {added:,} (default password: {seller_password})")

    admin_user = os.environ.get("ADMIN_USERNAME", "admin")
    if db.query_one("SELECT 1 FROM app_users WHERE username = %s", (admin_user,)):
        print(f"admin '{admin_user}' already exists, left unchanged")
    else:
        admin_password = os.environ.get("ADMIN_PASSWORD")
        if not admin_password:
            admin_password = secrets.token_urlsafe(12)
            print(f"ADMIN_PASSWORD not set in .env - generated one: {admin_password}")
        auth.create_admin(admin_user, admin_password)
        print(f"admin '{admin_user}' created")


if __name__ == "__main__":
    main()

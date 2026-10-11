"""Password hashing and login against the app_users table."""

import hashlib
import hmac
import secrets

import db

ITERATIONS = 200_000


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iterations, salt, expected = stored.split("$")
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), expected)


def authenticate(username: str, password: str) -> dict | None:
    """Return the user's session record, or None if the credentials are wrong."""
    user = db.query_one(
        "SELECT user_id, username, password_hash, role, customer_unique_id, seller_id, must_change_password "
        "FROM app_users WHERE username = %s AND is_active = 1",
        (username.strip(),),
    )
    if not user or not verify_password(password, user["password_hash"]):
        return None
    db.execute("UPDATE app_users SET last_login = NOW() WHERE user_id = %s", (user["user_id"],))
    return {
        "user_id": user["user_id"],
        "username": user["username"],
        "role": user["role"],
        "customer_unique_id": user["customer_unique_id"],
        "seller_id": user["seller_id"],
        "must_change_password": bool(user["must_change_password"]),
    }


def get_user(username: str) -> dict | None:
    """Look up an active user without checking a password (demo quick login only)."""
    return db.query_one(
        "SELECT user_id, username, role, customer_unique_id, seller_id FROM app_users WHERE username = %s AND is_active = 1",
        (username,),
    )


def change_password(user_id: int, current: str, new: str) -> str | None:
    """Change a user's own password. Returns an error message, or None on success."""
    row = db.query_one("SELECT password_hash FROM app_users WHERE user_id = %s", (user_id,))
    if not row or not verify_password(current, row["password_hash"]):
        return "Current password is incorrect."
    if len(new) < 8:
        return "New password must be at least 8 characters."
    db.execute(
        "UPDATE app_users SET password_hash = %s, must_change_password = 0 WHERE user_id = %s",
        (hash_password(new), user_id),
    )
    return None


def reset_password(username: str, new: str) -> bool:
    """Admin action: set a user's password and require them to change it."""
    return db.execute(
        "UPDATE app_users SET password_hash = %s, must_change_password = 1 WHERE username = %s",
        (hash_password(new), username),
    ) > 0


def create_admin(username: str, password: str) -> None:
    db.execute(
        "INSERT INTO app_users (username, password_hash, role, must_change_password) "
        "VALUES (%s, %s, 'admin', 0)",
        (username, hash_password(password)),
    )

"""Olist customer & admin portal.

Run:  streamlit run app.py
"""

import time

import streamlit as st

import auth
from ui import admin_pages, customer_pages
from ui.session import current_user, logout

st.set_page_config(page_title="Olist Portal", page_icon="🛍️", layout="wide")

MAX_FAILED_LOGINS = 5
LOCKOUT_SECONDS = 60


def login_page() -> None:
    _, middle, _ = st.columns([1, 2, 1])
    with middle:
        st.title("🛍️ Olist Portal")
        st.caption("Customers sign in with their customer ID. Admins sign in with their admin username.")

        locked_until = st.session_state.get("locked_until", 0)
        if time.time() < locked_until:
            st.error(f"Too many failed attempts. Try again in {int(locked_until - time.time())} seconds.")
            return

        with st.form("login"):
            username = st.text_input("Customer ID or admin username")
            password = st.text_input("Password", type="password")
            submitted = st.form_submit_button("Sign in", type="primary", width="stretch")

        if submitted:
            user = auth.authenticate(username, password)
            if user:
                st.session_state.clear()
                st.session_state["user"] = user
                st.rerun()
            failures = st.session_state.get("failed_logins", 0) + 1
            st.session_state["failed_logins"] = failures
            if failures >= MAX_FAILED_LOGINS:
                st.session_state["locked_until"] = time.time() + LOCKOUT_SECONDS
                st.session_state["failed_logins"] = 0
            st.error("Wrong username or password.")


def account_page() -> None:
    user = current_user()
    st.title("My account")
    st.write(f"Signed in as **{user['username']}** ({user['role']})")

    if user["role"] == "customer":
        from ui.session import customer_repo

        profile = customer_repo().profile()
        if profile:
            c1, c2, c3 = st.columns(3)
            c1.metric("Location", f"{profile['city'].title()}, {profile['state']}")
            c2.metric("Zip code prefix", profile["zip_code"])
            c3.metric("Customer since", profile["first_order"].strftime("%d %b %Y"))

    if user["must_change_password"]:
        st.warning("You are using a temporary password. Please set a new one.")

    with st.form("change_password", clear_on_submit=True):
        st.subheader("Change password")
        current = st.text_input("Current password", type="password")
        new = st.text_input("New password (min 8 characters)", type="password")
        confirm = st.text_input("Confirm new password", type="password")
        if st.form_submit_button("Update password"):
            if new != confirm:
                st.error("New passwords don't match.")
            elif error := auth.change_password(user["user_id"], current, new):
                st.error(error)
            else:
                user["must_change_password"] = False
                st.success("Password updated.")


def main() -> None:
    user = current_user()
    if not user:
        st.navigation([st.Page(login_page, title="Sign in", icon="🔐")], position="hidden").run()
        return

    account = st.Page(account_page, title="My account", icon="👤", url_path="account")
    if user["role"] == "admin":
        pages = {
            "Overview": [st.Page(admin_pages.dashboard, title="Dashboard", icon="📊", default=True)],
            "Data": [
                st.Page(admin_pages.orders, title="Orders", icon="📦", url_path="orders"),
                st.Page(admin_pages.customers, title="Customers", icon="🧑‍🤝‍🧑", url_path="customers"),
                st.Page(admin_pages.products, title="Products", icon="🏷️", url_path="products"),
                st.Page(admin_pages.sellers, title="Sellers", icon="🏪", url_path="sellers"),
            ],
            "Support": [st.Page(admin_pages.support_tickets, title="Support tickets", icon="🎫", url_path="tickets")],
            "Settings": [st.Page(admin_pages.users, title="User accounts", icon="🔑", url_path="users"), account],
        }
    else:
        pages = {
            "": [
                st.Page(customer_pages.my_orders, title="My orders", icon="📦", default=True),
                st.Page(customer_pages.shop, title="Shop", icon="🛒", url_path="shop"),
                account,
            ]
        }

    with st.sidebar:
        label = "Admin" if user["role"] == "admin" else f"Customer …{user['username'][-6:]}"
        st.markdown(f"**{label}**")
        if user["must_change_password"]:
            st.warning("Temporary password — change it under *My account*.")
        if st.button("Sign out", width="stretch"):
            logout()

    st.navigation(pages).run()


main()

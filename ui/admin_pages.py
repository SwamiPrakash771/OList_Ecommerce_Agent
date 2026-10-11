"""Pages only an admin can open. Data comes from AdminRepository (full access)."""

import secrets

import pandas as pd
import streamlit as st

import auth
from repository import CatalogRepository, CustomerRepository
from ui import components as ui
from ui.session import admin_repo


def dashboard() -> None:
    repo = admin_repo()
    st.title("Admin dashboard")

    k = repo.kpis()
    cols = st.columns(6)
    cols[0].metric("Revenue", f"R$ {float(k['revenue']) / 1e6:.2f}M")
    cols[1].metric("Orders", f"{k['orders']:,}")
    cols[2].metric("Customers", f"{k['customers']:,}")
    cols[3].metric("Sellers", f"{k['sellers']:,}")
    cols[4].metric("Avg review", f"{float(k['avg_review']):.2f} ⭐")
    cols[5].metric("On-time delivery", f"{float(k['on_time_pct']):.1f}%")

    monthly = repo.monthly_revenue()
    # 2016 and Sep-Oct 2018 have only a handful of orders; they distort the trend.
    monthly = monthly[monthly["orders"] >= 100]
    st.subheader("Monthly revenue")
    st.caption("Payments on non-canceled orders, months with at least 100 orders")
    ui.line_chart(monthly, "month", "revenue", "Month", "Revenue (R$)")

    left, right = st.columns(2)
    with left:
        st.subheader("Top categories by sales")
        top = repo.top_categories(10)
        top["category"] = top["category"].str.replace("_", " ")
        ui.bar_chart(top, "category", "revenue", "Category", "Item sales (R$)", horizontal=True)
    with right:
        st.subheader("Orders by status")
        ui.bar_chart(repo.orders_by_status(), "status", "orders", "Status", "Orders", horizontal=True)


def orders() -> None:
    repo = admin_repo()
    st.title("All orders")

    c1, c2, c3, c4, c5 = st.columns([2, 2, 2, 1, 1])
    order_id = c1.text_input("Order ID")
    customer = c2.text_input("Customer unique ID")
    statuses = c3.multiselect("Status", repo.statuses())
    date_from = c4.date_input("From", value=None, min_value=pd.Timestamp("2016-09-01"), max_value=pd.Timestamp("2018-10-31"))
    date_to = c5.date_input("To", value=None, min_value=pd.Timestamp("2016-09-01"), max_value=pd.Timestamp("2018-10-31"))

    results = repo.search_orders(order_id, customer, statuses, date_from, date_to, limit=500)
    st.caption(f"{len(results)} orders shown (max 500). Select one for details.")
    selected = ui.orders_table(results, key="admin_orders_table", show_customer=True)
    if selected:
        order = repo.order_detail(selected)
        if order:
            st.divider()
            ui.order_detail(order, show_customer=True)


def customers() -> None:
    repo = admin_repo()
    st.title("Customers")

    lookup = st.text_input("Look up a customer by unique ID", placeholder="e.g. 8d50f5eadf50201ccdcedfb9e2ac8455")
    if lookup:
        view = CustomerRepository(lookup.strip())
        profile = view.profile()
        if not profile:
            st.warning("No customer with that ID.")
        else:
            summary = view.spending_summary()
            st.subheader(f"Customer {profile['customer_unique_id']}")
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Location", f"{profile['city'].title()}, {profile['state']}")
            c2.metric("Orders", profile["orders"])
            c3.metric("Total paid", ui.money(summary["total_paid"]))
            c4.metric("Customer since", ui.fmt_dt(profile["first_order"]))
            selected = ui.orders_table(view.orders(), key="admin_customer_orders")
            if selected:
                order = repo.order_detail(selected)
                if order:
                    st.divider()
                    ui.order_detail(order, show_customer=True)
        st.divider()

    st.subheader("Most frequent customers")
    st.dataframe(
        repo.top_customers(100),
        hide_index=True,
        width="stretch",
        column_config={"total_paid": st.column_config.NumberColumn("Total paid", format="R$ %.2f")},
    )


def products() -> None:
    admin_repo()  # role check
    catalog = CatalogRepository()
    st.title("Products")
    found = ui.product_filters(catalog, key="admin_products")
    selected = ui.products_table(found, key="admin_products_table")
    if selected:
        product = catalog.product(selected)
        if product:
            st.divider()
            ui.product_detail(product)
            st.caption(f"Product ID `{product['product_id']}` · main seller `{product['main_seller_id']}`")


def sellers() -> None:
    repo = admin_repo()
    st.title("Sellers")
    c1, c2 = st.columns([1, 3])
    state = c1.selectbox("State", [None] + repo.seller_states(), format_func=lambda s: "All" if s is None else s)
    min_orders = c2.slider("Minimum orders fulfilled", 0, 500, 0, step=10)
    df = repo.sellers(state, min_orders)
    st.caption(f"{len(df):,} sellers")
    st.dataframe(
        df,
        hide_index=True,
        width="stretch",
        column_config={
            "avg_rating": st.column_config.NumberColumn("Avg rating", format="%.2f ⭐"),
            "on_time_pct": st.column_config.ProgressColumn("On-time %", min_value=0, max_value=100, format="%.1f%%"),
        },
    )


def users() -> None:
    repo = admin_repo()
    st.title("User accounts")

    c1, c2 = st.columns([1, 3])
    role = c1.selectbox("Role", [None, "customer", "admin"], format_func=lambda r: "All" if r is None else r)
    search = c2.text_input("Username starts with")
    st.dataframe(repo.users(role, search), hide_index=True, width="stretch")

    left, right = st.columns(2)
    with left, st.form("reset_password", clear_on_submit=True):
        st.subheader("Reset a password")
        username = st.text_input("Username")
        if st.form_submit_button("Reset password"):
            temp = secrets.token_urlsafe(9)
            if auth.reset_password(username.strip(), temp):
                st.success(f"Temporary password for `{username}`: `{temp}` — they must change it at next login.")
            else:
                st.error("No user with that username.")
    with right, st.form("create_admin", clear_on_submit=True):
        st.subheader("Create an admin")
        new_user = st.text_input("Username", key="new_admin_user")
        new_pass = st.text_input("Password", type="password", key="new_admin_pass")
        if st.form_submit_button("Create admin"):
            if len(new_pass) < 8:
                st.error("Password must be at least 8 characters.")
            else:
                try:
                    auth.create_admin(new_user.strip(), new_pass)
                    st.success(f"Admin `{new_user}` created.")
                except Exception as exc:  # duplicate username
                    st.error(f"Could not create admin: {exc}")


def support_tickets() -> None:
    repo = admin_repo()
    st.title("Support tickets")
    tickets = repo.support_tickets()
    if tickets.empty:
        st.info("No tickets yet. The AI agent will create them when customers report issues.")
    else:
        st.dataframe(tickets, hide_index=True, width="stretch")

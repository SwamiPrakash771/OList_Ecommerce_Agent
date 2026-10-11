"""Pages a logged-in customer can open. All order data comes from CustomerRepository."""

import streamlit as st

from repository import CatalogRepository
from ui import components as ui
from ui.session import customer_repo


def my_orders() -> None:
    repo = customer_repo()
    st.title("My orders")

    summary = repo.spending_summary()
    c1, c2, c3 = st.columns(3)
    c1.metric("Orders placed", summary["orders"])
    c2.metric("Total paid", ui.money(summary["total_paid"]))
    avg = summary["avg_review_given"]
    c3.metric("Avg rating you gave", f"{float(avg):.1f} ⭐" if avg is not None else "—")

    st.caption("Select an order to see its details.")
    selected = ui.orders_table(repo.orders(), key="my_orders_table")
    if selected:
        order = repo.order_detail(selected)
        if order:
            st.divider()
            ui.order_detail(order)


def shop() -> None:
    customer_repo()  # role check
    catalog = CatalogRepository()
    st.title("Shop products")
    products = ui.product_filters(catalog, key="shop")
    selected = ui.products_table(products, key="shop_table")
    if selected:
        product = catalog.product(selected)
        if product:
            st.divider()
            ui.product_detail(product)

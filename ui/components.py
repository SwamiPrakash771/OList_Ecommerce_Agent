"""UI pieces shared by customer and admin pages."""

import altair as alt
import pandas as pd
import streamlit as st

SERIES_COLOR = "#2a78d6"  # categorical slot 1 from the reference palette
STATUS_ICONS = {
    "delivered": "✅", "shipped": "🚚", "invoiced": "🧾", "processing": "⚙️",
    "approved": "👍", "created": "🆕", "canceled": "❌", "unavailable": "⛔",
}


def money(value) -> str:
    return "—" if value is None or pd.isna(value) else f"R$ {float(value):,.2f}"


def fmt_dt(value) -> str:
    return "—" if value is None or pd.isna(value) else pd.Timestamp(value).strftime("%d %b %Y")


def status_label(status: str) -> str:
    return f"{STATUS_ICONS.get(status, '•')} {status.capitalize()}"


def delivery_verdict(order: dict) -> str:
    delivered, estimated = order.get("order_delivered_customer_date"), order.get("order_estimated_delivery_date")
    if delivered is None or pd.isna(delivered):
        return "Not delivered yet"
    days = (pd.Timestamp(estimated).normalize() - pd.Timestamp(delivered).normalize()).days
    if days >= 0:
        return f"On time ({days} day{'s' if days != 1 else ''} early)" if days else "On time"
    return f"Late by {-days} day{'s' if days != -1 else ''}"


def orders_table(df: pd.DataFrame, key: str, show_customer: bool = False) -> str | None:
    """Show an order list and return the order_id of the selected row, if any."""
    if df.empty:
        st.info("No orders found.")
        return None
    view = df.copy()
    view["status"] = view["status"].map(status_label)
    columns = ["order_id", "purchased_at", "status", "items", "order_total", "estimated_delivery", "delivered_at"]
    if show_customer:
        columns.append("customer_unique_id")
    event = st.dataframe(
        view[columns],
        hide_index=True,
        width="stretch",
        on_select="rerun",
        selection_mode="single-row",
        key=key,
        column_config={
            "order_id": st.column_config.TextColumn("Order ID"),
            "purchased_at": st.column_config.DatetimeColumn("Purchased", format="DD MMM YYYY"),
            "status": "Status",
            "items": st.column_config.NumberColumn("Items"),
            "order_total": st.column_config.NumberColumn("Total", format="R$ %.2f"),
            "estimated_delivery": st.column_config.DatetimeColumn("Estimated", format="DD MMM YYYY"),
            "delivered_at": st.column_config.DatetimeColumn("Delivered", format="DD MMM YYYY"),
            "customer_unique_id": st.column_config.TextColumn("Customer"),
        },
    )
    rows = event.selection.rows
    return view.iloc[rows[0]]["order_id"] if rows else None


def order_detail(order: dict, show_customer: bool = False) -> None:
    st.subheader(f"Order {order['order_id']}")
    if show_customer:
        st.caption(
            f"Customer `{order['customer_unique_id']}` · "
            f"{order['customer_city']}, {order['customer_state']}"
        )

    items, payments = order["items"], order["payments"]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Status", status_label(order["order_status"]))
    c2.metric("Items total", money((items["price"] + items["freight"]).sum() if not items.empty else 0))
    c3.metric("Paid", money(payments["amount"].sum() if not payments.empty else 0))
    c4.metric("Delivery", delivery_verdict(order))

    st.markdown("**Timeline**")
    timeline = pd.DataFrame(
        [
            ("Purchased", order["order_purchase_timestamp"]),
            ("Payment approved", order["order_approved_at"]),
            ("Handed to carrier", order["order_delivered_carrier_date"]),
            ("Delivered", order["order_delivered_customer_date"]),
            ("Estimated delivery", order["order_estimated_delivery_date"]),
        ],
        columns=["Step", "Date"],
    )
    timeline["Date"] = timeline["Date"].map(fmt_dt)
    st.dataframe(timeline, hide_index=True, width="stretch")

    left, right = st.columns([3, 2])
    with left:
        st.markdown("**Items**")
        if items.empty:
            st.caption("No items recorded for this order.")
        else:
            st.dataframe(
                items[["item", "product", "category", "price", "freight", "seller_city", "seller_state"]],
                hide_index=True,
                width="stretch",
                column_config={
                    "price": st.column_config.NumberColumn("Price", format="R$ %.2f"),
                    "freight": st.column_config.NumberColumn("Freight", format="R$ %.2f"),
                },
            )
    with right:
        st.markdown("**Payments**")
        if payments.empty:
            st.caption("No payment recorded.")
        else:
            st.dataframe(
                payments,
                hide_index=True,
                width="stretch",
                column_config={"amount": st.column_config.NumberColumn("Amount", format="R$ %.2f")},
            )

    review = order.get("review")
    st.markdown("**Review**")
    if review:
        st.write("⭐" * int(review["review_score"]) + f"  ({review['review_score']}/5, {fmt_dt(review['review_creation_date'])})")
        if review.get("review_comment_title"):
            st.write(f"**{review['review_comment_title']}**")
        if review.get("review_comment_message"):
            st.write(review["review_comment_message"])
    else:
        st.caption("No review left for this order.")


def product_filters(catalog, key: str) -> pd.DataFrame:
    """Filter row above the product list; returns the matching products."""
    c1, c2, c3, c4 = st.columns([3, 2, 1, 1])
    categories = c1.multiselect("Category", catalog.categories(), key=f"{key}_cat",
                                format_func=lambda c: c.replace("_", " "))
    price = c2.slider("Price (R$)", 0, 2000, (0, 2000), step=10, key=f"{key}_price")
    min_rating = c3.selectbox("Min rating", [0, 3, 4, 4.5], key=f"{key}_rating",
                              format_func=lambda r: "Any" if r == 0 else f"{r}+ ⭐")
    sort_by = c4.selectbox("Sort", ["popular", "rating", "price_low", "price_high"], key=f"{key}_sort",
                           format_func={"popular": "Most popular", "rating": "Top rated",
                                        "price_low": "Price: low → high", "price_high": "Price: high → low"}.get)
    max_price = 1e9 if price[1] == 2000 else price[1]
    return catalog.search_products(categories, price[0], max_price, min_rating, sort_by)


def products_table(df: pd.DataFrame, key: str) -> str | None:
    if df.empty:
        st.info("No products match these filters.")
        return None
    st.caption(f"Showing {len(df)} products")
    event = st.dataframe(
        df.drop(columns="product_id"),
        hide_index=True,
        width="stretch",
        on_select="rerun",
        selection_mode="single-row",
        key=key,
        column_config={
            "product": "Product",
            "category": "Category",
            "price": st.column_config.NumberColumn("Price", format="R$ %.2f"),
            "shipping": st.column_config.NumberColumn("Avg shipping", format="R$ %.2f"),
            "rating": st.column_config.NumberColumn("Rating", format="%.1f ⭐"),
            "reviews": "Reviews",
            "sold": "Sold",
        },
    )
    rows = event.selection.rows
    return df.iloc[rows[0]]["product_id"] if rows else None


def product_detail(product: dict) -> None:
    st.subheader(product["display_name"])
    st.caption(product["category"].replace("_", " "))
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Typical price", money(product["typical_price"]))
    c2.metric("Avg shipping", money(product["avg_freight"]))
    rating = product["avg_rating"]
    c3.metric("Rating", f"{float(rating):.1f} ⭐" if rating is not None else "—", f"{product['num_reviews']} reviews",
              delta_color="off")
    c4.metric("Times sold", product["times_sold"])

    st.markdown(
        f"**Sold by** a seller in {str(product['seller_city']).title()}, {product['seller_state']} · "
        f"seller rating {product['seller_rating']} ⭐ · {product['seller_on_time_pct']}% on-time deliveries"
    )
    dims = [product.get(k) for k in ("length_cm", "width_cm", "height_cm")]
    if all(d is not None for d in dims):
        st.caption(f"Package: {dims[0]} × {dims[1]} × {dims[2]} cm, {product['weight_g']} g")

    st.markdown("**Recent reviews**")
    reviews = product["recent_reviews"]
    if reviews.empty:
        st.caption("No written reviews yet.")
    for _, r in reviews.iterrows():
        title = f" — **{r['title']}**" if isinstance(r["title"], str) else ""
        st.markdown(f"{'⭐' * int(r['score'])}{title}  \n{r['comment']}  \n:gray[{r['date']}]")


def bar_chart(df: pd.DataFrame, x: str, y: str, x_title: str, y_title: str, y_format: str = ",.0f",
              horizontal: bool = False) -> None:
    """Single-series bar chart with hover tooltips."""
    if horizontal:
        enc_x = alt.X(f"{y}:Q", title=y_title, axis=alt.Axis(format=y_format, grid=True))
        enc_y = alt.Y(f"{x}:N", title=None, sort="-x")
    else:
        enc_x = alt.X(f"{x}:N", title=x_title, sort="-y")
        enc_y = alt.Y(f"{y}:Q", title=y_title, axis=alt.Axis(format=y_format))
    chart = (
        alt.Chart(df)
        .mark_bar(color=SERIES_COLOR, cornerRadiusEnd=4)
        .encode(x=enc_x, y=enc_y, tooltip=[alt.Tooltip(x, title=x_title), alt.Tooltip(y, title=y_title, format=y_format)])
    )
    st.altair_chart(chart, width="stretch")


def line_chart(df: pd.DataFrame, x: str, y: str, x_title: str, y_title: str, y_format: str = ",.0f") -> None:
    """Single-series line chart with a hover crosshair and tooltip."""
    base = alt.Chart(df).encode(x=alt.X(f"{x}:T", title=x_title))
    hover = alt.selection_point(fields=[x], nearest=True, on="pointerover", empty=False)
    line = base.mark_line(color=SERIES_COLOR, strokeWidth=2).encode(
        y=alt.Y(f"{y}:Q", title=y_title, axis=alt.Axis(format=y_format))
    )
    points = base.mark_point(color=SERIES_COLOR, size=64, filled=True).encode(
        y=f"{y}:Q", opacity=alt.condition(hover, alt.value(1), alt.value(0))
    )
    rule = base.mark_rule(color="gray", strokeDash=[4, 4]).encode(
        opacity=alt.condition(hover, alt.value(0.6), alt.value(0)),
        tooltip=[alt.Tooltip(f"{x}:T", title=x_title, format="%b %Y"),
                 alt.Tooltip(f"{y}:Q", title=y_title, format=y_format)],
    ).add_params(hover)
    st.altair_chart(line + points + rule, width="stretch")

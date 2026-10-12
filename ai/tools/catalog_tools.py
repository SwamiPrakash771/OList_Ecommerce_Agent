"""Catalog tools for the support agent: available to guests and customers.

Outputs contain public product data only (no seller ids). Review text is wrapped as
untrusted so the agent treats it as data, never as instructions.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from ai.security import NotFound, Principal, ToolKind, tool, untrusted
from ai.tools.common import iso, num
from repository import CatalogRepository

_catalog = CatalogRepository()
SHOPPERS = ("guest", "customer")


def _product_row(p: dict) -> dict:
    return {
        "product_id": p["product_id"],
        "name": p["name"],
        "category": p["category"],
        "price": num(p["price"]),
        "shipping": num(p["shipping"]),
        "rating": num(p["rating"]),
        "reviews": int(p["reviews"]),
        "sold": int(p["sold"]),
    }


@tool(roles=SHOPPERS, kind=ToolKind.READ, reads=["product_stats"])
def search_products(
    principal: Principal,
    query: str = Field("", max_length=80, description="Category words or product code, e.g. 'watch'"),
    category: str | None = Field(None, max_length=64, description="Exact category name from list_categories"),
    max_price: float | None = Field(None, gt=0),
    min_rating: float | None = Field(None, ge=0, le=5),
    sort: Literal["popular", "rating", "price_low", "price_high"] = "popular",
    limit: int = Field(5, ge=1, le=10),
) -> dict:
    """Search products (price, shipping, rating, units sold)."""
    items, total = _catalog.browse(query, category, None, max_price, min_rating, sort, 1, limit)
    return {"total_matches": int(total), "products": [_product_row(r) for r in items.to_dict("records")]}


@tool(roles=SHOPPERS, kind=ToolKind.READ,
      reads=["product_stats", "products", "seller_stats", "reviews"], joins=["order_items"])
def product_details(principal: Principal, product_id: str = Field(..., min_length=32, max_length=32)) -> dict:
    """One product's details: price, shipping, rating, size, seller reliability, recent reviews."""
    p = _catalog.product(product_id)
    if not p:
        raise NotFound("product")
    reviews = p["recent_reviews"].head(5).to_dict("records")
    return {
        "product_id": p["product_id"],
        "name": p["display_name"],
        "category": p["category"],
        "price": num(p["typical_price"]),
        "shipping": num(p["avg_freight"]),
        "rating": num(p["avg_rating"]),
        "num_reviews": int(p["num_reviews"]),
        "times_sold": int(p["times_sold"]),
        "package": {"weight_g": p["weight_g"], "length_cm": p["length_cm"],
                    "width_cm": p["width_cm"], "height_cm": p["height_cm"]},
        "seller": {"city": p["seller_city"], "state": p["seller_state"], "rating": num(p["seller_rating"]),
                   "on_time_pct": num(p["seller_on_time_pct"]), "orders_fulfilled": p["seller_orders"]},
        "recent_reviews": [
            {"score": int(r["score"]), "date": iso(r["date"]),
             "title": untrusted(r["title"] if isinstance(r["title"], str) else None),
             "comment": untrusted(r["comment"])}
            for r in reviews
        ],
    }


@tool(roles=SHOPPERS, kind=ToolKind.READ, reads=["product_stats"])
def list_categories(principal: Principal) -> dict:
    """All categories with product count and lowest price."""
    rows = _catalog.category_summary().to_dict("records")
    return {"categories": [{"category": r["category"], "products": int(r["products"]),
                            "from_price": num(r["from_price"])} for r in rows]}

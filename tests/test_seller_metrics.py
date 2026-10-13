"""Seller semantic layer (ai/seller/tools/metrics.py): every metric checked against independent,
hand-written SQL for three sellers of different sizes. No LLM."""

import pytest

import ai.seller.tools  # noqa: F401
import db
from ai.security import Principal, ToolError
from ai.seller import simulate_orders
from ai.seller.security import call_seller_tool

SELLERS = {"A": simulate_orders.SELLER_A, "B": "4a3ca9315b744ce9f8e9374361493884", "C": "0176f73cc1195f367f7b32db1e5b3aa8"}
OK = "o.order_status NOT IN ('canceled','unavailable')"


@pytest.fixture(scope="module", autouse=True)
def scenario_orders():
    simulate_orders.create()


def metric(sid, **args):
    return call_seller_tool(Principal(role="seller", user_id=1, username="t", seller_id=sid), "seller_metric", args)


def one(sql, sid):
    v = db.query_one(sql, {"s": sid})
    return round(float(next(iter(v.values())) or 0), 2)


# metric -> hand-written SQL (written independently of the compiler)
TRUTH = {
    "revenue": f"SELECT SUM(i.price) FROM order_items i JOIN orders o USING (order_id) WHERE i.seller_id=%(s)s AND {OK}",
    "orders": f"SELECT COUNT(DISTINCT i.order_id) FROM order_items i JOIN orders o USING (order_id) WHERE i.seller_id=%(s)s AND {OK}",
    "units": f"SELECT COUNT(*) FROM order_items i JOIN orders o USING (order_id) WHERE i.seller_id=%(s)s AND {OK}",
    "avg_order_value": f"SELECT AVG(t) FROM (SELECT SUM(i.price) t FROM order_items i JOIN orders o USING (order_id) "
                       f"WHERE i.seller_id=%(s)s AND {OK} GROUP BY i.order_id) x",
    "shipping_charged": f"SELECT SUM(i.freight_value) FROM order_items i JOIN orders o USING (order_id) "
                        f"WHERE i.seller_id=%(s)s AND {OK}",
    "late_deliveries": "SELECT SUM(DATE(o.order_delivered_customer_date) > DATE(o.order_estimated_delivery_date)) FROM orders o "
                       "WHERE o.order_status='delivered' AND o.order_delivered_customer_date IS NOT NULL "
                       "AND o.order_id IN (SELECT order_id FROM order_items WHERE seller_id=%(s)s)",
    "late_shipments": "SELECT COUNT(DISTINCT i.order_id) FROM order_items i JOIN orders o USING (order_id) "
                      "WHERE i.seller_id=%(s)s AND o.order_delivered_carrier_date > i.shipping_limit_date",
    "avg_days_to_carrier": "SELECT AVG(TIMESTAMPDIFF(SECOND, o.order_purchase_timestamp, o.order_delivered_carrier_date)/86400) "
                           "FROM orders o WHERE o.order_delivered_carrier_date IS NOT NULL "
                           "AND o.order_id IN (SELECT order_id FROM order_items WHERE seller_id=%(s)s)",
}


@pytest.mark.parametrize("seller", sorted(SELLERS))
@pytest.mark.parametrize("name", sorted(TRUTH))
def test_metric_totals_match_hand_written_sql(seller, name):
    sid = SELLERS[seller]
    assert metric(sid, metric=name)["total"] == pytest.approx(one(TRUTH[name], sid), abs=0.011)


def test_month_over_month_change():
    out = metric(SELLERS["A"], metric="revenue", group_by=["month"], period="2026", compare_previous=True, top_n=1)
    assert out["rows"][0]["month"] == "2026-07" and out["rows"][0]["change"] == 5385.0


def test_january_is_compared_with_the_previous_december():
    rows = metric(SELLERS["A"], metric="revenue", group_by=["month"], period="2026", compare_previous=True)["rows"]
    dec = one(f"SELECT SUM(i.price) FROM order_items i JOIN orders o USING (order_id) WHERE i.seller_id=%(s)s AND {OK} "
              "AND DATE_FORMAT(o.order_purchase_timestamp,'%%Y-%%m')='2025-12'", SELLERS["A"])
    jan = rows[0]
    assert jan["month"] == "2026-01" and jan["change"] == pytest.approx(jan["value"] - dec, abs=0.01)


def test_top_n_share_of_total():
    out = metric(SELLERS["A"], metric="revenue", group_by=["product"], top_n=10, share_of_total=True)
    assert len(out["rows"]) == 10 and out["shown_groups_share_pct"] == 32.96


def test_rate_by_state_with_minimum_orders():
    out = metric(SELLERS["A"], metric="late_shipping_rate", group_by=["buyer_state"], min_orders=20, top_n=1)
    assert out["rows"] == [{"buyer_state": "PA", "value": 3.23, "orders": 31, "buyer_state_name": "Pará"}]


def test_review_score_by_delivery_lateness_is_per_order():
    rows = metric(SELLERS["A"], metric="avg_review_score", group_by=["delivered_late"])["rows"]
    truth = db.query_df("""SELECT DATE(o.order_delivered_customer_date) > DATE(o.order_estimated_delivery_date) late,
                             AVG(r.s) avg FROM orders o JOIN (SELECT order_id, AVG(review_score) s FROM reviews GROUP BY order_id) r
                             USING (order_id) WHERE o.order_status='delivered' AND o.order_id IN
                             (SELECT order_id FROM order_items WHERE seller_id=%(s)s) GROUP BY late""", {"s": SELLERS["A"]})
    labels = {1: "delivered late", 0: "delivered on time"}   # readable labels, never 0/1
    assert {r["delivered_late"]: r["value"] for r in rows} == {
        labels[int(k)]: round(float(v), 2) for k, v in zip(truth.late, truth.avg)}


def test_periods_and_definitions_come_back():
    out = metric(SELLERS["A"], metric="revenue", period="2025")
    assert out["total"] == 56859.89 and out["period"] == {"start": "2025-01-01", "end": "2025-12-31"}
    assert "no shipping" in out["definition"]


@pytest.mark.parametrize("args,error", [
    ({"metric": "profit"}, "Unknown metric"),
    ({"metric": "revenue", "group_by": ["seller_id"]}, "Unknown or repeated grouping"),
    ({"metric": "avg_order_value", "group_by": ["product"]}, "can't be broken down"),
    ({"metric": "revenue", "period": "yesterday; DROP TABLE orders"}, "Unknown period"),
    ({"metric": "revenue", "group_by": ["buyer_state"], "compare_previous": True}, "compare_previous needs"),
])
def test_bad_choices_are_refused_with_a_reason(args, error):
    with pytest.raises(ToolError, match=error):
        metric(SELLERS["A"], **args)


def test_a_seller_only_ever_gets_their_own_figures():
    a, b = metric(SELLERS["A"], metric="revenue")["total"], metric(SELLERS["B"], metric="revenue")["total"]
    assert a != b and a == one(TRUTH["revenue"], SELLERS["A"])


def test_states_are_brazilian_and_named():
    from ai.seller.tools.common import state_name
    assert state_name("PA") == "Pará" and state_name("SP") == "São Paulo" and state_name("XX") is None

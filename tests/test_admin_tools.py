"""Admin agent, step 5: the tools, checked against figures computed directly from the store tables."""

import json
from datetime import date

import pytest

import ai.admin.tools  # noqa: F401  (registers the tools)
import db
from ai.admin import facts
from ai.admin.security import ADMIN_REGISTRY, call_admin_tool, in_admin_conversation, mask_id
from ai.admin.tools import metrics, query
from ai.security import InvalidArguments, NotFound, Principal, ToolError

ADMIN = Principal(role="admin", user_id=9_100_071, username="admin-test")
DEMO_ORDER = "a1a6eadae352582010f449d8527b55f3"     # demo customer 1: refund stuck in processing
WEAK_SELLER = "54965bbe3e4f07ae045b90b0b8541f52"
ACTIVE = "o.order_status NOT IN ('canceled', 'unavailable')"


def run(name, **args):
    return call_admin_tool(ADMIN, name, args)


@pytest.fixture(scope="module", autouse=True)
def fresh_facts():
    """The reporting tables must exist; rebuild them only if missing (a rebuild takes 1-2 minutes)."""
    if facts.refreshed_at() is None:
        facts.refresh()


def keys(obj) -> set[str]:
    """Every field name in a tool result (notes may mention masked things; fields must not hold them)."""
    if isinstance(obj, dict):
        return set(obj) | {k for v in obj.values() for k in keys(v)}
    if isinstance(obj, list):
        return {k for v in obj for k in keys(v)}
    return set()


MASKED_FIELDS = {"customer_unique_id", "customer_id", "customer_city", "customer_state", "city", "state", "zip_code",
                 "payment_value", "payments_total", "payment_type", "payment_method", "amount", "failure_reason",
                 "description", "ai_summary", "content", "title", "args", "recipient_name", "phone", "street"}


def _live(sql, params=()):
    return db.query_one(sql, params)


# ---- admin_metric ------------------------------------------------------------------------------


def test_item_revenue_and_gmv_match_the_store():
    """The hourly reporting tables against the store, over the same orders (test fixtures elsewhere
    recreate scenario orders with fresh dates, so a time cutoff isn't enough)."""
    items = _live(f"SELECT ROUND(SUM(oi.price), 2) AS v FROM order_items oi JOIN orders o ON o.order_id = oi.order_id "
                  f"JOIN admin_order_facts f ON f.order_id = o.order_id WHERE {ACTIVE}")["v"]
    gmv = _live(f"SELECT ROUND(SUM(p.payment_value), 2) AS v FROM payments p JOIN orders o ON o.order_id = p.order_id "
                f"JOIN admin_order_facts f ON f.order_id = o.order_id WHERE {ACTIVE}")["v"]
    assert run("admin_metric", metric="item_revenue")["total"] == pytest.approx(float(items), abs=0.01)
    assert run("admin_metric", metric="gmv")["total"] == pytest.approx(float(gmv), abs=0.01)


def test_ground_truth_from_step_2():
    assert run("admin_metric", metric="item_revenue", period="2025")["total"] == pytest.approx(6_393_604.14, abs=0.01)
    assert run("admin_metric", metric="gmv", period="2025")["total"] == pytest.approx(7_424_316.33, abs=0.01)
    assert run("admin_metric", metric="avg_order_value", period="2025")["definition"].startswith("Average order value")
    best = run("admin_metric", metric="item_revenue", group_by=["month"], top_n=1, period="2025")["rows"][0]
    assert best["month"] == "2025-11" and best["value"] == pytest.approx(1_115_409.45, abs=0.01)
    states = run("admin_metric", metric="item_revenue", group_by=["buyer_state"], top_n=3)["rows"]
    assert [s["buyer_state"] for s in states] == ["SP", "RJ", "MG"] and states[0]["buyer_state_name"] == "São Paulo"
    late = run("admin_metric", metric="late_deliveries", period="2025")
    assert late["definition"].startswith("Delivered orders that arrived after")


def test_on_time_rate_by_seller_finds_the_weak_seller():
    worst = run("admin_metric", metric="on_time_rate", group_by=["seller"], min_orders=50, sort="asc", top_n=1)["rows"][0]
    assert worst["seller"] == WEAK_SELLER and worst["value"] == pytest.approx(69.86, abs=0.01) and worst["orders"] == 73


def test_compare_previous_and_share_of_total():
    r = run("admin_metric", metric="orders", group_by=["month"], period="2025", compare_previous=True)
    assert len(r["rows"]) == 12 and all("change" in row for row in r["rows"])   # Jan 2025 compares with Dec 2024
    top = run("admin_metric", metric="item_revenue", group_by=["category"], top_n=5, share_of_total=True)
    assert 0 < top["shown_groups_share_pct"] < 100 and all(row["share_pct"] for row in top["rows"])


def test_metrics_that_use_masked_columns_hide_small_groups():
    r = run("admin_metric", metric="gmv", group_by=["payment_type"])
    small = [row for row in r["rows"] if row["orders"] < 5]
    assert small and all(row["value"] is None and "hidden" in row for row in small)
    assert "hidden" in r["note"]
    r = run("admin_metric", metric="orders", group_by=["payment_type"])   # not sensitive: counts are shown
    assert all(row["value"] is not None for row in r["rows"])


@pytest.mark.parametrize("args, message", [
    ({"metric": "profit"}, "Unknown metric"),
    ({"metric": "gmv", "group_by": ["seller"]}, "can't be broken down"),          # payments belong to orders
    ({"metric": "avg_order_value", "group_by": ["category"]}, "can't be broken down"),
    ({"metric": "orders", "group_by": ["customer"]}, "Unknown or repeated grouping"),
    ({"metric": "orders", "period": "2026-13"}, "Unknown period"),
    ({"metric": "orders", "rank_by": "change"}, "needs a comparison"),
    ({"metric": "orders", "compare_to": "previous_period"}, "nothing before it"),
])
def test_bad_metric_requests_are_explained(args, message):
    with pytest.raises(InvalidArguments, match=message):
        run("admin_metric", **args)


def test_no_grouping_exposes_a_person():
    sem = metrics.semantic()
    for name, d in sem["dimensions"].items():
        assert "customer" not in d["expr"] and "payments_total" not in d["expr"], name
    for name, m in sem["metrics"].items():
        uses_masked = any(c in json.dumps(m) for c in ("customer_key", "payments_total"))
        assert not uses_masked or m.get("sensitive"), f"{name} uses a masked column but isn't marked sensitive"


# ---- record lookups ----------------------------------------------------------------------------


@pytest.fixture
def conversation():
    conv = "e" * 32
    with in_admin_conversation(conv):
        yield conv
    db.execute("DELETE FROM ai_admin_access_log WHERE admin_user_id = %s", (ADMIN.user_id,))


def _logged(conv):
    return list(db.query_df("SELECT reason FROM ai_admin_access_log WHERE admin_user_id = %s AND conversation_id = %s "
                            "ORDER BY log_id", (ADMIN.user_id, conv)).reason)


def test_order_story_is_masked_and_logged(conversation):
    r = run("look_up_record", kind="order", record_id=DEMO_ORDER[:8])
    customer = db.query_one("SELECT c.customer_unique_id AS cid FROM orders o JOIN customers c "
                            "ON c.customer_id = o.customer_id WHERE o.order_id = %s", (DEMO_ORDER,))["cid"]
    assert r["order_id"] == DEMO_ORDER and r["customer"] == mask_id("customer", customer)
    assert customer not in json.dumps(r, default=str) and not keys(r) & MASKED_FIELDS
    refund = r["refunds"][0]
    assert refund["status"] == "processing" and refund["past_expected_time"] and refund["expected_within_days"] == 12
    assert _logged(conversation) == [f"admin assistant looked up order {DEMO_ORDER[:8]}"]


def test_customer_history_by_masked_id_or_order(conversation):
    by_order = run("look_up_record", kind="customer", record_id=DEMO_ORDER)
    by_label = run("look_up_record", kind="customer", record_id=by_order["customer"])
    assert by_label["customer"] == by_order["customer"] and by_label["orders"] == by_order["orders"] >= 1
    assert not keys(by_label) & MASKED_FIELDS
    assert len(_logged(conversation)) == 2


def test_seller_overview(conversation):
    r = run("look_up_record", kind="seller", record_id=WEAK_SELLER[:8])
    assert r["seller_id"] == WEAK_SELLER and r["state"] == "PR"
    assert r["all_time"]["orders"] == 78 and r["all_time"]["on_time_rate"] == pytest.approx(69.86, abs=0.01)
    assert r["storefront_stats"]["on_time_pct"] == pytest.approx(67.9)
    assert _logged(conversation) == [f"admin assistant looked up seller {WEAK_SELLER[:8]}"]


@pytest.mark.parametrize("kind, record_id, error", [
    ("order", "zzzzzzzz", InvalidArguments),
    ("order", "00000000", NotFound),
    ("customer", "customer-00000000", NotFound),
    ("seller", "0000000000", NotFound),
])
def test_unknown_records(kind, record_id, error):
    with pytest.raises(error):
        run("look_up_record", kind=kind, record_id=record_id)


# ---- operations --------------------------------------------------------------------------------


def test_stuck_and_failed_refunds():
    stuck = run("operations_report", report="stuck_refunds")
    assert stuck["count"] >= 9 and stuck["count_by_payment_method"].get("credit_card", 0) >= 6
    assert all(r["days_in_processing"] > r["expected_within_days"] for r in stuck["refunds"])
    failed = run("operations_report", report="failed_refunds")
    live = _live("SELECT COUNT(*) AS n, ROUND(SUM(amount), 2) AS a FROM refunds WHERE status = 'failed'")
    assert failed["count"] == live["n"] and failed["total_amount"] == pytest.approx(float(live["a"]))
    assert not keys(failed) & MASKED_FIELDS


def test_late_open_orders_separate_old_leftovers():
    r = run("operations_report", report="late_open_orders")
    assert r["old_leftovers"] >= 1_700 and r["open_orders"] < 100
    days = [o["days_past_estimate"] for o in r["orders"]]
    assert days == sorted(days, reverse=True)
    assert r["late_open_orders"] == sum(1 for o in r["orders"] if o["days_past_estimate"] > 0) or r["open_orders"] > 20


def test_weak_sellers_come_from_the_metrics():   # the removed weak_sellers tool's question
    r = run("admin_metric", metric="on_time_rate", group_by=["seller"], min_orders=50, sort="asc", top_n=5)
    rows = r["rows"]
    assert rows[0]["seller"] == WEAK_SELLER and rows[0]["value"] == pytest.approx(69.86, abs=0.01)
    assert [x["value"] for x in rows] == sorted(x["value"] for x in rows)        # worst first
    assert all(x["orders"] >= 50 for x in rows) and r["orders_counted"] == "delivered orders"


def test_tickets_come_from_a_custom_analysis_without_their_text():   # the removed support_queue tool's question
    r = run("query_platform_data", sql="SELECT status, issue_type, COUNT(*) AS tickets FROM platform_tickets GROUP BY status, issue_type")
    assert r["row_count"] >= 1
    with pytest.raises(ToolError):   # ticket text isn't in the safe table, so MySQL refuses the column
        run("query_platform_data", sql="SELECT description FROM platform_tickets")
    assert not keys(r) & MASKED_FIELDS


def test_ai_usage_by_role_matches_the_ledger():
    r = run("operations_report", report="ai_usage", period="all")
    live = {x["role"]: x["n"] for x in db.query_df(
        "SELECT c.owner_role AS role, COUNT(*) AS n FROM ai_messages m JOIN ai_conversations c "
        "USING (conversation_id) WHERE m.role = 'assistant' GROUP BY c.owner_role").to_dict("records")}
    assert {row["role"]: row["replies"] for row in r["rows"] if row["replies"]} == live
    assert not keys(r) & MASKED_FIELDS


# ---- SQL fallback -----------------------------------------------------------------------------


def test_sql_fallback_answers_with_safe_tables():
    r = run("query_platform_data", sql="SELECT reason, COUNT(*) AS n FROM platform_returns GROUP BY reason")
    assert r["row_count"] == 5


@pytest.mark.parametrize("sql", [
    "SELECT * FROM customers",
    "SELECT payment_value FROM payments",
    "SELECT * FROM admin_order_facts",
    "SELECT customer_key FROM order_summary",
    "SELECT description FROM platform_tickets",
    "SELECT review_comment_message FROM platform_reviews",
    "SELECT content FROM ai_messages",
    "DELETE FROM platform_orders",
    "SELECT 1; SELECT 2",
    "WITH x AS (SELECT 1) SELECT * FROM x",
    "SELECT SLEEP(5) FROM platform_orders",
])
def test_sql_fallback_refuses_what_it_may_not_read(sql):
    with pytest.raises(Exception) as err:
        run("query_platform_data", sql=sql)
    assert err.type.__name__ in ("InvalidArguments", "ToolError")


def test_sql_fallback_tables_hold_no_masked_column():
    masked = {"customer_unique_id", "customer_key", "payments_total", "payment_value", "payment_type", "buyer_state",
              "amount", "payment_method", "failure_reason", "description", "ai_summary", "review_comment_message"}
    for name, (_, cols) in query.TABLES.items():
        assert not masked & set(cols), name


def test_every_admin_tool_is_read_only_and_documented():
    assert set(ADMIN_REGISTRY) == {"admin_metric", "show_chart", "query_platform_data", "look_up_record",
                                   "operations_report"}   # step 8: 11 tools consolidated to 5
    for spec in ADMIN_REGISTRY.values():
        assert len(spec.description) > 80, spec.name


# ---- time series: no gaps, partial and thin periods marked --------------------------------------


def test_months_without_orders_are_filled_so_changes_compare_neighbours():
    r = run("admin_metric", metric="item_revenue", group_by=["month"], period="2026", compare_previous=True)
    months = [row["month"] for row in r["rows"]]
    assert months == [f"2026-{m:02d}" for m in range(1, len(months) + 1)]   # no month skipped
    sept = next(row for row in r["rows"] if row["month"] == "2026-09")
    assert sept["value"] == 0 and sept["orders"] == 0 and sept["few_orders"]
    aug = next(row for row in r["rows"] if row["month"] == "2026-08")
    assert sept["change"] == pytest.approx(-aug["value"]) and sept["change_pct"] == -100.0


def test_averages_have_no_value_in_empty_months():
    rows = run("admin_metric", metric="avg_order_value", group_by=["month"], period="2026")["rows"]
    assert next(row for row in rows if row["month"] == "2026-09")["value"] is None


def test_the_current_period_is_marked_partial_and_explained():
    r = run("admin_metric", metric="orders", group_by=["month"], period="this_year")
    assert r["rows"][-1]["partial"].startswith("to ") and "partial" in r["note"] and "few_orders" in r["note"]
    assert not any(row.get("partial") for row in r["rows"][:-1])


def test_normal_months_are_not_flagged():
    rows = run("admin_metric", metric="orders", group_by=["month"], period="2025")["rows"]
    assert len(rows) == 12 and not any(row.get("few_orders") or row.get("partial") for row in rows)


# ---- charts ---------------------------------------------------------------------------------------

from ai.admin.tools.charts import collect_charts  # noqa: E402


def chart(**args):
    with collect_charts() as sink:
        result = run("show_chart", **args)
    assert len(sink) == 1 and result["chart_shown"]
    return sink[0], result


def test_a_trend_chart_uses_the_metric_layer():
    spec, result = chart(metric="item_revenue", group_by=["month"], period="2025")
    metric = run("admin_metric", metric="item_revenue", group_by=["month"], period="2025")
    assert spec["kind"] == "line" and [d["x"] for d in spec["data"]] == [r["month"] for r in metric["rows"]]
    assert [d["s0"] for d in spec["data"]] == [r["value"] for r in metric["rows"]]
    assert spec["definition"] == metric["definition"] and result["data"] == spec["data"]   # the model sees the same values


def test_a_ranking_is_a_horizontal_bar_chart():
    spec, _ = chart(metric="on_time_rate", group_by=["seller"], min_orders=50, sort="asc", top_n=5)
    assert spec["kind"] == "bar" and spec["horizontal"] and len(spec["data"]) == 5
    assert spec["data"][0]["x"] == f"seller {WEAK_SELLER[:8]}" and spec["title"].startswith("On-time rate")


def test_comparing_groups_over_time_keeps_three_series():
    spec, _ = chart(metric="gmv", group_by=["month", "buyer_state"], period="2025")
    assert [s["name"] for s in spec["series"]] == ["SP (São Paulo)", "RJ (Rio de Janeiro)", "MG (Minas Gerais)"]
    assert len(spec["data"]) == 12 and all({"s0", "s1", "s2"} <= set(d) for d in spec["data"])
    assert any("left out" in n for n in spec["notes"])


def test_partial_and_thin_periods_are_flagged_on_the_chart():
    spec, _ = chart(metric="orders", group_by=["month"], period="this_year")
    assert "partial" in spec["data"][-1]["flag"] and "few orders" in spec["data"][-1]["flag"]


@pytest.mark.parametrize("args, message", [
    ({"metric": "orders", "group_by": ["month", "year"]}, "not several"),
    ({"metric": "orders", "group_by": ["seller"], "chart": "scatter"}, "two measures"),
    ({"metric": "profit", "group_by": ["month"]}, "Unknown metric"),
    ({"metric": "gmv", "group_by": ["seller"]}, "can't be broken down"),
])
def test_bad_chart_requests_are_explained(args, message):
    with pytest.raises(InvalidArguments, match=message):
        run("show_chart", **args)


def test_sensitive_chart_points_hide_small_groups():
    spec, _ = chart(metric="gmv", group_by=["month", "buyer_state"], period="2025", sort="asc")   # the smallest states
    states = [s["name"].split(" ")[0] for s in spec["series"]]
    counts = {(r["m"], r["st"]): r["n"] for r in db.query_df(
        "SELECT DATE_FORMAT(order_purchase_timestamp, '%%Y-%%m') AS m, buyer_state AS st, COUNT(*) AS n "
        "FROM admin_order_facts WHERE order_status NOT IN ('canceled', 'unavailable') AND YEAR(order_purchase_timestamp) = 2025 "
        f"AND buyer_state IN ({', '.join(['%s'] * len(states))}) GROUP BY m, st", tuple(states)).to_dict("records")}
    small = 0
    for d in spec["data"]:
        for i, st in enumerate(states):
            if counts.get((d["x"], st), 0) < 5:
                small += 1
                assert d[f"s{i}"] is None, (d["x"], st)
    assert small > 0   # the check really met small groups


def test_more_series_on_request_get_dashes_as_a_second_cue():
    spec, result = chart(metric="item_revenue", group_by=["month", "category"], period="2025", series=5)
    assert len(spec["series"]) == 5 and len(result["series"]) == 5
    assert [s.get("dash") for s in spec["series"]][:3] == [None, None, None]
    assert all(s.get("dash") for s in spec["series"][3:])
    assert len({s["color"] for s in spec["series"]}) == 5


def test_the_default_is_three_series_in_palette_order():
    spec, _ = chart(metric="gmv", group_by=["month", "buyer_state"], period="2025")
    assert [s["color"] for s in spec["series"]] == ["#2a78d6", "#eb6834", "#1baf7a"]
    assert not any(s.get("dash") for s in spec["series"])


def test_colours_can_be_chosen_by_name():
    spec, result = chart(metric="item_revenue", group_by=["month"], period="2025", colors=["green"])
    assert spec["series"][0]["color"] == "#008300" and result["series"][0]["color"] == "green"
    spec, _ = chart(metric="gmv", group_by=["month", "buyer_state"], period="2025", colors=["red", "blue"])
    assert [s["color_name"] for s in spec["series"]] == ["red", "blue", "orange"]   # the rest keep palette order


@pytest.mark.parametrize("args, message", [
    ({"colors": ["red", "red"]}, "different colour"),
    ({"colors": ["neon"]}, "colors"),
    ({"series": 13}, "series"),
])
def test_bad_colour_and_series_requests(args, message):
    with pytest.raises(InvalidArguments, match=message):
        run("show_chart", metric="gmv", group_by=["month", "buyer_state"], period="2025", **args)



# ---- every chart type, drawn as asked unless it would be false ---------------------------------------


def test_a_pie_of_the_top_20_adds_up_to_the_whole():
    spec, result = chart(metric="item_revenue", group_by=["category"], chart="pie", top_n=20, period="2026")
    assert spec["kind"] == "pie" and len(spec["data"]) == 21 and spec["data"][-1]["x"] == "Other"
    total = run("admin_metric", metric="item_revenue", period="2026")["total"]
    assert sum(d["s0"] for d in spec["data"]) == pytest.approx(total, abs=0.05)
    assert sum(d["share"] for d in spec["data"]) == pytest.approx(100, abs=0.1)
    assert len({d["color"] for d in spec["data"]}) == 21 and result["drawn_as"] == "pie"


def test_a_donut_is_a_pie_with_a_hole():
    spec, _ = chart(metric="units", group_by=["category"], chart="donut", top_n=5)
    assert spec["kind"] == "pie" and spec["donut"] and spec["data"][-1]["x"] == "Other"


@pytest.mark.parametrize("metric, grouping, why", [("on_time_rate", "seller", "rate or an average"),
                                                   ("orders", "category", "several of these groups")])
def test_a_pie_of_parts_that_dont_add_up_is_drawn_as_bars_and_explained(metric, grouping, why):
    spec, result = chart(metric=metric, group_by=[grouping], chart="pie", min_orders=50)
    assert spec["kind"] == "bar" and spec["horizontal"] and result["drawn_as"] == "bar"
    assert any(why in n for n in spec["notes"])


def test_stacked_area_and_stacked_bars():
    spec, _ = chart(metric="item_revenue", group_by=["month", "category"], chart="stacked_area", period="2025", series=5)
    assert spec["kind"] == "area" and spec["stacked"] and len(spec["series"]) == 5 and len(spec["data"]) == 12
    assert not any(s.get("dash") for s in spec["series"])   # stacked areas are told apart by fill
    spec, _ = chart(metric="item_revenue", group_by=["category", "seller_state"], chart="stacked_bar", top_n=6, series=4)
    assert spec["kind"] == "bar" and spec["stacked"] and len(spec["data"]) == 6 and len(spec["series"]) == 4


def test_two_groupings_without_time_are_grouped_bars():
    spec, _ = chart(metric="item_revenue", group_by=["category", "buyer_state"], top_n=5, series=3)
    assert spec["kind"] == "bar" and not spec["stacked"] and len(spec["data"]) == 5


def test_a_scatter_compares_two_measures():
    spec, _ = chart(metric="orders", y_metric="on_time_rate", group_by=["seller"], chart="scatter", top_n=30, min_orders=50)
    assert spec["kind"] == "scatter" and len(spec["data"]) == 30
    assert all(d["s0"] >= 50 and 0 <= d["y"] <= 100 for d in spec["data"])
    assert spec["y_definition"].startswith("% of delivered orders")


def test_many_series_get_lighter_shades_not_repeats():
    spec, _ = chart(metric="gmv", group_by=["month", "buyer_state"], period="2025", series=12)
    colours = [s["color"] for s in spec["series"]]
    assert len(colours) == 12 and len(set(colours)) == 12
    assert all(s.get("dash") for s in spec["series"][3:])



# ---- comparisons: every change names its baseline (regressions from the first UI test) ------------


def test_year_on_year_per_month_compares_the_same_month_a_year_earlier():
    rows = run("admin_metric", metric="item_revenue", group_by=["month"], period="2026",
               compare_to="same_period_last_year")["rows"]
    jan = rows[0]
    assert jan["month"] == "2026-01" and jan["compared_with"] == "2025-01"
    assert jan["previous_value"] == pytest.approx(222_644.56) and jan["change"] == pytest.approx(717_379.58)
    assert jan["change_pct"] == pytest.approx(322.21, abs=0.01)


def test_month_on_month_names_the_previous_month():
    jan = run("admin_metric", metric="item_revenue", group_by=["month"], period="2026",
              compare_to="previous_period")["rows"][0]
    assert jan["compared_with"] == "2025-12" and jan["change"] == pytest.approx(229_703.73)
    old = run("admin_metric", metric="item_revenue", group_by=["month"], period="2026", compare_previous=True)["rows"][0]
    assert old["compared_with"] == "2025-12"   # the older option means the same thing


def test_a_period_not_over_yet_is_compared_with_the_same_days():
    r = run("admin_metric", metric="item_revenue", group_by=["month"], period="this_year",
            compare_to="same_period_last_year")
    last = r["rows"][-1]
    assert last["partial"] and last["compared_with"].endswith("(same days)")
    today = date.today()
    assert last["compared_with"].startswith(f"{today.year - 1}-{today.month:02d}-01 to {today.year - 1}-{today.month:02d}-{today.day:02d}")


def test_whole_period_comparisons_and_empty_baselines():
    r = run("admin_metric", metric="item_revenue", period="2026", compare_to="same_period_last_year")
    total = r["comparison"]["total"]
    assert total["compared_with"].startswith(f"{date.today().year - 1}-01-01 to")
    assert total["change"] == pytest.approx(r["total"] - total["previous_total"], abs=0.01)
    r = run("admin_metric", metric="item_revenue", period="2025", compare_period="2024")
    assert r["comparison"]["total"]["compared_with"] == "2024"
    r = run("admin_metric", metric="orders", period="2026-09", compare_to="previous_period")
    assert r["comparison"]["baseline"] == {"start": "2026-08-01", "end": "2026-08-31"}


def test_groups_compare_with_the_same_group_and_rank_by_change():
    rows = run("admin_metric", metric="item_revenue", group_by=["category"], period="2026",
               compare_to="same_period_last_year", rank_by="change", top_n=5)["rows"]
    changes = [r["change"] for r in rows]
    assert len(rows) == 5 and changes == sorted(changes, reverse=True)
    assert all(r["change"] == pytest.approx(r["value"] - r["previous_value"], abs=0.01) for r in rows)


# ---- every number says what it means (admin tools audit) -----------------------------------------


def test_a_thin_period_is_flagged_and_a_normal_one_is_not():
    assert "very few orders" in run("admin_metric", metric="item_revenue", period="2026-09")["note"]
    assert "very few orders" not in (run("admin_metric", metric="item_revenue", period="2025").get("note") or "")


def test_totals_and_counts_say_what_they_are():
    rate = run("admin_metric", metric="late_delivery_rate", period="2025")
    assert "not a sum" in rate["total_meaning"] and rate["orders_counted"] == "delivered orders"
    money = run("admin_metric", metric="item_revenue", period="2025")
    assert money["total_meaning"] == "the whole period's total"
    assert money["orders_counted"] == "orders excluding canceled and unavailable"


def test_shares_only_of_parts_that_add_up():
    with pytest.raises(InvalidArguments, match="add up to a whole"):
        run("admin_metric", metric="orders", group_by=["category"], share_of_total=True)
    r = run("admin_metric", metric="item_revenue", group_by=["category"], top_n=5, share_of_total=True)
    assert r["share_of"] == "the whole period's total" and r["rows_shown"].startswith("the top 5")


def test_chart_results_map_keys_to_series():
    _, result = chart(metric="gmv", group_by=["month", "buyer_state"], period="2025")
    assert [s["key"] for s in result["series"]] == ["s0", "s1", "s2"] and "data_keys" in result


@pytest.mark.parametrize("args, field", [
    ({"report": "failed_refunds", "top_n": 3}, "refunds_shown"),
    ({"report": "late_open_orders", "top_n": 2}, "orders_shown"),
])
def test_shortened_lists_say_how_much_they_show(args, field):
    shown = run("operations_report", **args)[field]
    assert " of " in shown and int(shown.split(" of ")[0]) <= args["top_n"]


def test_figures_from_the_hourly_tables_carry_their_period_and_time():
    s = run("look_up_record", kind="seller", record_id=WEAK_SELLER[:8])
    assert s["figures_as_of"] and s["last_12_months"]["to"] == date.today().isoformat()
    w = run("admin_metric", metric="on_time_rate", group_by=["seller"], min_orders=50, top_n=1)
    assert w["period"]["start"] == "first order" and w["figures_as_of"]



# ---- charts plot any field of a comparison; chart notes are for people ---------------------------------

MODEL_WORDS = ("say so", "describe", "few_orders", "compared_with", "previous_value", "instead of reporting")


def test_a_chart_of_percent_change_year_on_year():
    spec, result = chart(metric="item_revenue", group_by=["month"], period="2026", plot="change_pct",
                         compare_to="same_period_last_year")
    jan = spec["data"][0]
    assert spec["unit"] == "%" and spec["title"].startswith("% change in item revenue")
    assert jan["x"] == "2026-01" and jan["s0"] == pytest.approx(322.21, abs=0.01) and jan["compared_with"] == "2025-01"
    assert "the same month a year earlier" in result["plotted"]


def test_both_periods_as_two_series():
    spec, _ = chart(metric="item_revenue", group_by=["month"], period="2026", plot="both", compare_to="same_period_last_year")
    jan = spec["data"][0]
    assert len(spec["series"]) == 2 and jan["s0"] == pytest.approx(940_024.14) and jan["s1"] == pytest.approx(222_644.56)


def test_percent_change_for_several_groups_over_time():
    spec, _ = chart(metric="gmv", group_by=["month", "buyer_state"], period="2026", plot="change_pct",
                    compare_to="same_period_last_year")
    assert [s["name"] for s in spec["series"]][0].startswith("SP") and spec["unit"] == "%"
    assert spec["data"][0]["compared_with"] == "2025-01"


def test_a_change_chart_needs_a_comparison():
    with pytest.raises(InvalidArguments, match="needs a comparison"):
        run("show_chart", metric="item_revenue", group_by=["month"], plot="change_pct")


def test_chart_notes_are_for_people_and_name_the_right_period():
    spec, result = chart(metric="item_revenue", group_by=["month"], period="2025", plot="change_pct",
                         compare_to="previous_period")
    assert any("comparison period" in n for n in spec["notes"])            # 2024 is the thin one, not 2025
    assert not any(w in n for n in spec["notes"] for w in MODEL_WORDS)       # no model instructions on screen
    assert result["how_to_read"]                                            # those go to the model only


def test_month_by_month_comparisons_work_over_all_time():
    r = run("admin_metric", metric="item_revenue", group_by=["month"], compare_to="same_period_last_year")
    jan = next(x for x in r["rows"] if x["month"] == "2026-01")
    assert jan["compared_with"] == "2025-01" and jan["change"] == pytest.approx(717_379.58)
    assert r["comparison"]["total"] is None     # 'all' has no period before it: only the per-month comparisons


# ---- colour each bar or point by its own value (after "negative bars are still green") -------------------


def test_growth_green_and_decline_red():
    spec, _ = chart(metric="item_revenue", group_by=["month"], period="2026", chart="bar", plot="change_pct",
                    compare_to="same_period_last_year", color_by_threshold=True)
    for d in spec["data"]:
        assert d["c0"] == ("#008300" if d["s0"] >= 0 else "#e34948"), d
    assert {d["c0"] for d in spec["data"]} == {"#008300", "#e34948"}
    assert [g["label"] for g in spec["color_rule"]["legend"]] == ["≥ 0%", "< 0%"]


def test_any_threshold_and_colours_and_several_series():
    spec, _ = chart(metric="on_time_rate", group_by=["seller"], min_orders=50, sort="asc", top_n=5,
                    color_by_threshold=True, threshold=80, above_color="blue", below_color="orange")
    assert all(d["c0"] == ("#2a78d6" if d["s0"] >= 80 else "#eb6834") for d in spec["data"])
    spec, _ = chart(metric="gmv", group_by=["month", "buyer_state"], period="2026", plot="change_pct",
                    compare_to="same_period_last_year", color_by_threshold=True)
    assert all(f"c{i}" in spec["data"][0] for i in range(3))


def test_a_pie_keeps_its_slice_colours_and_says_why():
    spec, _ = chart(metric="item_revenue", group_by=["category"], chart="pie", top_n=5, color_by_threshold=True)
    assert spec["color_rule"] is None and any("parts of a whole" in n for n in spec["notes"])



# ---- capability gaps found by probing (filters, periods, time units, running totals, products, charts) ----

ACTIVE_SQL = "order_status NOT IN ('canceled', 'unavailable')"


def _sql(query, params=()):
    return float(db.query_one(query, params)["v"] or 0)


def test_filters_answer_for_x_questions():
    r = run("admin_metric", metric="item_revenue", group_by=["month"], period="2025", filters={"category": ["health_beauty"]})
    assert len(r["rows"]) == 12 and r["filters"] == "product category = health_beauty"
    truth = _sql(f"SELECT SUM(price) v FROM admin_item_facts WHERE {ACTIVE_SQL} AND category = 'health_beauty' "
                 "AND YEAR(order_purchase_timestamp) = 2025")
    assert r["total"] == pytest.approx(truth, abs=0.01)
    seller = run("admin_metric", metric="item_revenue", period="2025", filters={"seller": [WEAK_SELLER[:8]]})
    assert seller["filters"] == f"seller = {WEAK_SELLER}"
    assert run("admin_metric", metric="gmv", period="2025", filters={"buyer_state": ["sp"]})["filters"] == "buyer's state = SP"


def test_a_filter_narrow_enough_to_describe_one_person_is_hidden():
    r = run("admin_metric", metric="gmv", period="2026-10", filters={"buyer_state": ["AC"]})
    assert r["total"] is None and "hidden" in r["note"]


def test_unknown_filters_are_refused():
    with pytest.raises(InvalidArguments, match="Unknown filter"):
        run("admin_metric", metric="orders", filters={"customer": ["x"]})


def test_custom_periods():
    r = run("admin_metric", metric="item_revenue", period="2025-03-15..2025-06-30")
    truth = _sql(f"SELECT SUM(items_total) v FROM admin_order_facts WHERE {ACTIVE_SQL} "
                 "AND order_purchase_timestamp >= '2025-03-15' AND order_purchase_timestamp < '2025-07-01'")
    assert r["total"] == pytest.approx(truth, abs=0.01) and r["period"] == {"start": "2025-03-15", "end": "2025-06-30"}
    assert run("admin_metric", metric="orders", period="2025-Q2")["period"] == {"start": "2025-04-01", "end": "2025-06-30"}
    with pytest.raises(InvalidArguments, match="ends before it starts"):
        run("admin_metric", metric="orders", period="2025-06-30..2025-03-15")


def test_quarters_weeks_days_add_up():
    quarters = run("admin_metric", metric="item_revenue", group_by=["quarter"], period="2025")["rows"]
    months = run("admin_metric", metric="item_revenue", group_by=["month"], period="2025")["rows"]
    assert [q["quarter"] for q in quarters] == ["2025-Q1", "2025-Q2", "2025-Q3", "2025-Q4"]
    assert quarters[0]["value"] == pytest.approx(sum(m["value"] for m in months[:3]), abs=0.05)
    days = run("admin_metric", metric="orders", group_by=["day"], period="2026-03")["rows"]
    assert len(days) == 31 and days[0]["day"] == "2026-03-01"
    weeks = run("admin_metric", metric="orders", group_by=["week"], period="2026-01",
                compare_to="same_period_last_year")["rows"]
    assert weeks[0]["week"] == "2026-W01" and weeks[0]["cut"].startswith("covers 2026-01-01")
    assert weeks[0]["compared_with"].endswith("(same days)")


def test_weekday_in_natural_order():
    rows = run("admin_metric", metric="orders", group_by=["weekday"], period="2025")["rows"]
    assert [r["weekday"] for r in rows] == ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def test_too_many_points_are_refused_with_a_way_out():
    with pytest.raises(InvalidArguments, match="coarser time unit"):
        run("admin_metric", metric="orders", group_by=["day"])


def test_running_totals():
    r = run("admin_metric", metric="item_revenue", group_by=["month"], period="2025", cumulative=True)
    assert r["rows"][-1]["cumulative_value"] == pytest.approx(r["total"], abs=0.05)
    ytd = run("admin_metric", metric="item_revenue", group_by=["month"], period="2026", cumulative=True,
              compare_to="same_period_last_year")["rows"][5]
    assert ytd["cumulative_value"] > ytd["previous_cumulative"] > 0
    assert ytd["cumulative_change"] == pytest.approx(ytd["cumulative_value"] - ytd["previous_cumulative"], abs=0.05)
    with pytest.raises(InvalidArguments, match="sum or count"):
        run("admin_metric", metric="on_time_rate", group_by=["month"], cumulative=True)


def test_product_lookup():
    r = run("look_up_record", kind="product", record_id="99a4788c")
    truth = db.query_one(f"SELECT COUNT(*) n FROM order_items oi JOIN orders o ON o.order_id = oi.order_id "
                         f"WHERE oi.product_id = %s AND o.{ACTIVE_SQL}", (r["product_id"],))["n"]
    assert r["all_time"]["units"] == truth and r["category"] == "bed_bath_table"


def test_charts_take_filters_running_totals_and_value_labels():
    spec, _ = chart(metric="item_revenue", group_by=["month"], period="2025", filters={"category": ["health_beauty"]})
    assert spec["filters"] == "product category = health_beauty"
    spec, _ = chart(metric="item_revenue", group_by=["month"], period="2026", plot="cumulative_both",
                    compare_to="same_period_last_year")
    assert len(spec["series"]) == 2 and spec["data"][5]["s0"] > spec["data"][5]["s1"]
    spec, _ = chart(metric="orders", group_by=["quarter"], period="2025", chart="bar", show_values=True)
    assert spec["show_values"] and spec["data"][0]["x"] == "2025-Q1"


def test_charts_keep_weekdays_in_order_unless_ranked():
    natural, _ = chart(metric="orders", group_by=["weekday"], period="2025", chart="bar")
    assert [d["x"] for d in natural["data"]][:2] == ["Monday", "Tuesday"]
    ranked, _ = chart(metric="orders", group_by=["weekday"], period="2025", chart="bar", top_n=3)
    assert len(ranked["data"]) == 3


def test_two_different_metrics_are_two_charts():
    with pytest.raises(InvalidArguments, match="draw two charts"):
        run("show_chart", metric="item_revenue", y_metric="orders", group_by=["month"], chart="line")



# ---- colour by groups of values: quantiles, ranges, threshold (after "every 25% quantile, 5 colours") ----


def test_quartiles_colour_equal_groups_green_to_red():
    spec, _ = chart(metric="item_revenue", group_by=["seller_state"], chart="bar", top_n=23, color_by="quantiles", bins=4)
    legend = spec["color_rule"]["legend"]
    assert len(legend) == 4 and legend[0]["color"] == "#008300" and legend[-1]["color"] == "#e34948"
    counts = [sum(1 for d in spec["data"] if d["c0"] == g["color"]) for g in legend]
    assert sum(counts) == len(spec["data"]) and max(counts) - min(counts) <= 1      # equal-sized groups
    values = [d["s0"] for d in spec["data"]]
    assert values == sorted(values, reverse=True)                                  # highest first


def test_top_10_in_5_colours_is_two_per_colour():
    spec, _ = chart(metric="item_revenue", group_by=["seller_state"], chart="bar", top_n=10, color_by="quantiles", bins=5)
    colours = [d["c0"] for d in spec["data"]]
    assert len(set(colours)) == 5 and all(colours.count(c) == 2 for c in set(colours))
    assert colours[0] == colours[1] != colours[2]


def test_ranges_with_chosen_colours():
    spec, _ = chart(metric="item_revenue", group_by=["seller_state"], chart="bar", top_n=10, color_by="ranges",
                    breaks=[100000, 1000000], bin_colors=["green", "yellow", "red"])
    rule = spec["color_rule"]
    assert [g["color"] for g in rule["legend"]] == ["#008300", "#eda100", "#e34948"]
    for d in spec["data"]:
        expected = "#008300" if d["s0"] >= 1_000_000 else "#eda100" if d["s0"] >= 100_000 else "#e34948"
        assert d["c0"] == expected, d
    with pytest.raises(InvalidArguments, match="needs breaks"):
        run("show_chart", metric="item_revenue", group_by=["seller_state"], color_by="ranges")
    with pytest.raises(InvalidArguments, match="need 3 colours"):
        run("show_chart", metric="item_revenue", group_by=["seller_state"], color_by="ranges", breaks=[1, 2],
            bin_colors=["red", "green"])


def test_threshold_is_one_case_of_value_groups():
    spec, _ = chart(metric="item_revenue", group_by=["month"], period="2026", chart="bar", plot="change_pct",
                    compare_to="same_period_last_year", color_by="threshold")
    assert [g["label"] for g in spec["color_rule"]["legend"]] == ["≥ 0%", "< 0%"]
    old, _ = chart(metric="item_revenue", group_by=["month"], period="2026", chart="bar", plot="change_pct",
                   compare_to="same_period_last_year", color_by_threshold=True)            # the older option still works
    assert old["color_rule"]["type"] == "threshold"

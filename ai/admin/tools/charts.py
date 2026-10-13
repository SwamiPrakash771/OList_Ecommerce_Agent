"""show_chart: any common chart in the admin's chat, built by the server from the metric layer.

The model only chooses what to chart (metric, groupings, period, chart type, sizes, colours, title).
Every value comes from the same compiled queries as admin_metric: the same definitions, gap-free time
series, partial / few_orders marks and hidden small groups. So a chart can't show a number the data
doesn't have. The chart goes to the chat as a side event (collected through `collect_charts`), is
stored with the answer, and the model gets the same values back to describe the takeaway.

General rules instead of fixed limits:
- the type the admin asks for is drawn (line, area, bar, horizontal bar, stacked bar, stacked area,
  pie, donut, scatter); without one, the clearest type for the data is chosen;
- sizes are what the admin asks for (up to 50 bars or slices, 12 series);
- only what's needed for the chart to be true is enforced, and explained in a note when it changes
  the drawing: a pie or a stack shows parts of a whole, so it needs a figure whose parts add up (sums,
  counts of rows) and its slices always add up to the whole ("Other" holds the rest); a rate or an
  average is drawn as bars instead.
Colours: the reference categorical palette in its fixed order (or the colours the admin names); past
8, lighter shades of the same hues. Lines past the third also get a dash pattern, and every series is
named in the legend, so identity never depends on colour alone.
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from datetime import date
from typing import Literal

from pydantic import Field

from ai.admin.security import InvalidArguments, Principal, admin_tool
from ai.admin.tools import metrics
from ai.admin.tools.common import PERIODS, period_bounds, read_only, state_name

DEFAULT_SERIES, MAX_SERIES, MAX_POINTS, DEFAULT_TOP_N = 3, 12, 50, 10
TIME_DIMS = metrics.TIME_UNITS
# Named colours -> the reference palette's light shades, in the palette's fixed order (gray: neutral ink).
COLORS = {"blue": "#2a78d6", "orange": "#eb6834", "teal": "#1baf7a", "yellow": "#eda100", "pink": "#e87ba4",
          "green": "#008300", "purple": "#6250d6", "red": "#e34948", "gray": "#64748b"}
PALETTE_ORDER = ["blue", "orange", "teal", "yellow", "pink", "green", "purple", "red"]
OTHER_COLOR = "#94a3b8"
DASHES = ["6 3", "2 3", "8 3 2 3", "12 4"]      # lines past the third: a dash pattern as a second cue
ColorName = Literal["blue", "orange", "teal", "yellow", "pink", "green", "purple", "red", "gray"]
ChartKind = Literal["auto", "line", "area", "bar", "horizontal_bar", "stacked_bar", "stacked_area", "pie", "donut",
                    "scatter"]
# Requested type -> (drawn kind, options)
KINDS = {"line": ("line", {}), "area": ("area", {}), "stacked_area": ("area", {"stacked": True}),
         "bar": ("bar", {}), "horizontal_bar": ("bar", {"horizontal": True}), "stacked_bar": ("bar", {"stacked": True}),
         "pie": ("pie", {}), "donut": ("pie", {"donut": True}), "scatter": ("scatter", {})}

_charts: contextvars.ContextVar[list | None] = contextvars.ContextVar("admin_charts", default=None)


@contextmanager
def collect_charts():
    """Charts made inside this block are appended to the yielded list (the engine sends them to the chat)."""
    sink: list[dict] = []
    token = _charts.set(sink)
    try:
        yield sink
    finally:
        _charts.reset(token)


ACRONYMS = {"gmv": "GMV"}


def short_name(metric: str) -> str:
    """'on_time_rate' -> 'On-time rate', 'gmv' -> 'GMV' (the full definition is shown under the chart)."""
    words = [ACRONYMS.get(w, w) for w in metric.split("_")]
    text = " ".join(words).replace("on time", "on-time").replace("avg ", "average ")
    return text[0].upper() + text[1:]


def _label(dim: str, v) -> str:
    if v is None:
        return "unknown"
    if dim in metrics.FLAG_LABELS and v in metrics.FLAG_LABELS[dim]:
        return metrics.FLAG_LABELS[dim][v]
    if dim in ("buyer_state", "seller_state"):
        name = state_name(str(v))
        return f"{v} ({name})" if name else str(v)
    if dim == "seller":
        return f"seller {str(v)[:8]}"
    return str(v)


def _flags(row: dict) -> str | None:
    marks = [f"partial, {row['partial']}" if row.get("partial") else None,
             "few orders" if row.get("few_orders") else None,
             row["cut"] if row.get("cut") else None,
             "hidden for privacy" if row.get("hidden") else None]
    return "; ".join(m for m in marks if m) or None


Plot = Literal["value", "previous_value", "both", "change", "change_pct", "share_pct", "cumulative", "cumulative_both"]
COMPARED = ("previous_value", "both", "change", "change_pct", "cumulative_both")
CUMULATIVE = ("cumulative", "cumulative_both")
# What each plot draws, as result fields (one series each), and how its title reads.
FIELDS = {"value": ["value"], "previous_value": ["previous_value"], "both": ["value", "previous_value"],
          "change": ["change"], "change_pct": ["change_pct"], "share_pct": ["share_pct"],
          "cumulative": ["cumulative_value"], "cumulative_both": ["cumulative_value", "previous_cumulative"]}
PLOT_TITLES = {"value": "{Name}", "previous_value": "{Name} (comparison period)", "both": "{Name} vs comparison period",
               "change": "Change in {name}", "change_pct": "% change in {name}", "share_pct": "Share of {name}",
               "cumulative": "Running total of {name}", "cumulative_both": "Running total of {name} vs comparison period"}


def _baseline(res: dict) -> str | None:
    """'the same month a year earlier' / '2025-01-01 to 2025-12-31' from a compared result."""
    each = ((res.get("comparison") or {}).get("each_row") or "").removeprefix("vs ")
    return each.split(" (see")[0] or None


def _series_name(metric: str, field: str, baseline: str | None) -> str:
    name = short_name(metric)
    return {"value": name, "previous_value": f"{name}, {baseline}", "change": f"Change vs {baseline}",
            "change_pct": f"% change vs {baseline}", "share_pct": f"Share of {_in_sentence(name)}",
            "cumulative_value": f"{name}, running total", "previous_cumulative": f"{name}, running total, {baseline}"}[field]


def _in_sentence(name: str) -> str:
    """'Item revenue' -> 'item revenue' inside a sentence; acronyms (GMV) stay as they are."""
    return name if name.split()[0].isupper() else name[0].lower() + name[1:]


def _plotted(metric: str, plot: str, lines: list[dict]) -> str:
    return f"{PLOT_TITLES[plot].format(Name=short_name(metric), name=_in_sentence(short_name(metric)))}: " + "; ".join(s["name"] for s in lines)


def _human_notes(res: dict) -> list[str]:
    """Short notes for people under the chart (the full reading guidance goes to the model only)."""
    rows, note = res.get("rows") or [], res.get("note") or ""
    out = []
    if "This period has very few orders" in note:
        out.append("Very few orders in this period: not a full picture.")
    if "The comparison period has very few orders" in note:
        out.append("Very few orders in the comparison period: changes against it are not a business trend.")
    if any(r.get("hidden") for r in rows):
        out.append("Some groups are hidden for privacy (under 5 orders).")
    if any(r.get("cut") for r in rows):
        out.append("Points marked * at the edges cover only part of their dates.")
    if any(r.get("partial") or r.get("few_orders") for r in rows):
        out.append("Points marked * are not over yet or have few orders: not a business trend.")
    return out


ColorBy = Literal["series", "threshold", "quantiles", "ranges"]
RAMP_STOPS = ["#008300", "#eda100", "#e34948"]   # green -> yellow -> red (palette shades), for graded groups


def _mix(a: str, b: str, t: float) -> str:
    ca, cb = ([int(h[i:i + 2], 16) for i in (1, 3, 5)] for h in (a, b))
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(ca, cb))


def ramp(n: int) -> list[str]:
    """n colours from green through yellow to red, the first for the highest group."""
    if n == 1:
        return [RAMP_STOPS[0]]
    out = []
    for k in range(n):
        pos = k / (n - 1) * (len(RAMP_STOPS) - 1)
        i = min(int(pos), len(RAMP_STOPS) - 2)
        out.append(_mix(RAMP_STOPS[i], RAMP_STOPS[i + 1], pos - i))
    return out


def _value_format(m: dict, plot: str):
    if plot in ("change_pct", "share_pct") or m.get("unit") == "%":
        return lambda v: f"{v:,.4g}%"
    if m.get("unit") == "$":
        return lambda v: f"${v:,.2f}"
    return lambda v: f"{v:,.4g}"


def value_colour_rule(data: list[dict], lines: list[dict], kind: str, color_by: str, threshold: float,
                      above_color: str, below_color: str, bins: int, breaks: list[float] | None,
                      bin_colors: list[str] | None, fmt) -> dict:
    """Colour every bar/point by the group its own value falls in, and describe the groups for the legend.
    threshold: two groups (at or above / below). quantiles: `bins` groups with equal numbers of bars, highest
    first (4 = quartiles). ranges: groups between the given `breaks`. Colours: the admin's (highest group
    first) or green -> yellow -> red."""
    value_key = "y" if kind == "scatter" else None
    points = [(d, i, d.get(value_key or f"s{i}")) for d in data for i in range(len(lines))]
    points = [(d, i, v) for d, i, v in points if isinstance(v, (int, float))]
    if color_by == "threshold":
        edges, labels = [threshold], [f"\u2265 {fmt(threshold)}", f"< {fmt(threshold)}"]
        colours = [COLORS[above_color], COLORS[below_color]]
        group = lambda v: 0 if v >= threshold else 1   # noqa: E731
    elif color_by == "ranges":
        if not breaks:
            raise InvalidArguments("color_by 'ranges' needs breaks: the values between groups, e.g. [100000, 1000000].")
        edges = sorted(set(breaks), reverse=True)
        labels = [f"\u2265 {fmt(edges[0])}"] + [f"{fmt(lo)} \u2013 {fmt(hi)}" for hi, lo in zip(edges, edges[1:])] \
            + [f"< {fmt(edges[-1])}"]
        colours = [COLORS[c] for c in bin_colors] if bin_colors else ramp(len(labels))
        group = lambda v: next((k for k, e in enumerate(edges) if v >= e), len(edges))   # noqa: E731
    else:   # quantiles: equal numbers of bars per group, ranked by value (highest first)
        n = len(points)
        k = max(1, min(bins, n))
        ranked = sorted(points, key=lambda p: p[2], reverse=True)
        place = {id(p): pos * k // n for pos, p in enumerate(ranked)}
        groups: list[list[float]] = [[] for _ in range(k)]
        for p in ranked:
            groups[place[id(p)]].append(p[2])
        size = round(100 / k)
        labels = [f"{'top' if g == 0 else 'bottom' if g == k - 1 else 'next'} {size}%: {fmt(min(vals))} \u2013 {fmt(max(vals))}"
                  if vals else f"group {g + 1}" for g, vals in enumerate(groups)]
        colours = [COLORS[c] for c in bin_colors] if bin_colors else ramp(k)
        lookup = {id(p): place[id(p)] for p in points}
        group = None
    if len(colours) < len(labels):
        raise InvalidArguments(f"{len(labels)} groups need {len(labels)} colours; give bin_colors with that many names, "
                               "or leave it out for green -> yellow -> red.")
    for p in points:
        d, i, v = p
        g = lookup[id(p)] if group is None else group(v)
        d[f"c{i}"] = colours[g]
    return {"type": color_by, "legend": [{"label": lab, "color": col} for lab, col in zip(labels, colours)]}


def _tint(hex_colour: str, amount: float) -> str:
    """A lighter shade of a palette colour (mixed with white)."""
    rgb = [int(hex_colour[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(c + (255 - c) * amount):02x}" for c in rgb)


def colour_list(n: int, chosen: list[str] | None) -> list[tuple[str, str]]:
    """n (name, hex) pairs: the admin's choices first, then the palette order, then lighter shades."""
    names = list(chosen or []) + [c for c in PALETTE_ORDER if c not in (chosen or [])]
    out = [(name, COLORS[name]) for name in names]
    for amount in (0.45, 0.7):
        out += [(f"light {name}", _tint(COLORS[name], amount)) for name in PALETTE_ORDER]
    while len(out) < n:   # beyond 24: repeat (every series is named in the legend anyway)
        out += out[:n - len(out)]
    return out[:n]


def is_whole(metric: str, group_by: list[str]) -> bool:
    """Do the groups' values add up to the total? Sums and row counts do; rates, averages and distinct
    counts (an order with two categories counts in both) don't, so they can't be slices or stacks."""
    m = metrics.semantic()["metrics"][metric]
    expr = m[metrics.choose_grain(metric, group_by)]
    return expr.startswith("SUM(") or expr == "COUNT(*)"


def _series_values(metric: str, x_dim: str, s_dim: str, x_names: list | None, s_names: list, start, end, sort,
                   flt: dict | None = None):
    """Values for each (x, series) pair, limited to the chosen groups and the filters (bound parameters)."""
    only = {**(flt or {}), s_dim: s_names, **({x_dim: x_names} if x_names else {})}
    sql, params = metrics.compile_query(metric, [x_dim, s_dim], start, end, None, None, sort, only=only)
    return read_only(sql, params, max_rows=5000)


def show_chart(principal: Principal,
               metric: str,
               group_by: list[str] = Field(..., min_length=1, max_length=2),
               period: str = "all",
               chart: ChartKind = "auto",
               top_n: int | None = Field(None, ge=1, le=MAX_POINTS),
               series: int = Field(DEFAULT_SERIES, ge=1, le=MAX_SERIES),
               sort: Literal["desc", "asc"] = "desc",
               min_orders: int | None = Field(None, ge=1, le=100_000),
               y_metric: str | None = None,
               filters: dict[str, list[str | int]] | None = None,
               plot: Plot = "value",
               compare_to: metrics.Comparison = "none",
               compare_period: str | None = None,
               colors: list[ColorName] | None = Field(None, max_length=MAX_SERIES),
               color_by: ColorBy = "series",
               bins: int = Field(4, ge=2, le=10),
               breaks: list[float] | None = Field(None, min_length=1, max_length=9),
               bin_colors: list[ColorName] | None = Field(None, min_length=2, max_length=10),
               color_by_threshold: bool = False,
               threshold: float = 0,
               above_color: ColorName = "green",
               below_color: ColorName = "red",
               show_values: bool = False,
               title: str | None = Field(None, max_length=80)) -> dict:
    ranked = top_n is not None or sort == "asc"   # an explicit ranking request (keeps weekday/hour order otherwise)
    top_n = top_n or DEFAULT_TOP_N
    sem = metrics.semantic()
    for name in (metric, y_metric):
        if name is not None and name not in sem["metrics"]:
            raise InvalidArguments(f"Unknown metric '{name}'. Available: {', '.join(sem['metrics'])}.")
    if any(g not in sem["dimensions"] for g in group_by) or len(set(group_by)) != len(group_by):
        raise InvalidArguments(f"Unknown or repeated grouping. Available: {', '.join(sem['dimensions'])}.")
    if colors and len(set(colors)) != len(colors):
        raise InvalidArguments("Give each series a different colour.")
    if chart == "scatter" and (y_metric is None or len(group_by) != 1):
        raise InvalidArguments("A scatter chart needs two measures (metric for x, y_metric for y) and one grouping, "
                               "e.g. metric 'orders', y_metric 'on_time_rate', group_by ['seller'].")
    if y_metric and chart not in ("auto", "scatter"):
        raise InvalidArguments("Two different metrics can't share one axis: draw two charts, one per metric (call this "
                               "tool twice). y_metric is only for a scatter of one metric against another. To show this "
                               "period against another, use plot 'both'.")
    flt = metrics.normalize_filters(filters)
    cumulative = plot in CUMULATIVE
    comparing = compare_to != "none" or bool(compare_period)
    if plot in COMPARED and not comparing:
        raise InvalidArguments(f"plot '{plot}' needs a comparison: compare_to ('previous_period' or "
                               "'same_period_last_year') or compare_period. Use the one the admin asked for.")
    m = sem["metrics"][metric]
    if plot == "share_pct" and (len(group_by) != 1 or not is_whole(metric, group_by)):
        raise InvalidArguments("Shares need one grouping and parts that add up to a whole (sums or row counts).")
    time_dim = next((g for g in group_by if g in TIME_DIMS), None)
    if sum(g in TIME_DIMS for g in group_by) > 1:
        raise InvalidArguments("Group by one time unit (day, week, month, quarter or year), not several.")
    if cumulative and not time_dim:
        raise InvalidArguments("A running total needs a time grouping (day, week, month, quarter or year).")
    x_dim = time_dim or group_by[0]
    s_dim = next((g for g in group_by if g != x_dim), None)
    notes: list[str] = []          # shown under the chart: short, for people
    model_notes: list[str] = []    # how to read the data: returned to the model only
    today = date.today()
    start, end = period_bounds(period, today)

    # ---- which chart: the one asked for, else the clearest for the data; only correctness changes it
    if chart == "auto":
        chart = ("scatter" if y_metric else "line" if time_dim else
                 "horizontal_bar" if not s_dim else "bar")
    kind, opts = KINDS[chart]
    opts = dict(opts)
    whole_needed = kind == "pie" or opts.get("stacked")
    if whole_needed and not is_whole(metric, group_by):
        why = ("an order can fall in several of these groups, so the parts would add up to more than the whole"
               if m[metrics.choose_grain(metric, group_by)].startswith("COUNT(DISTINCT") else
               "it's a rate or an average, so its groups don't add up to a whole")
        notes.append(f"Drawn as bars instead of a {chart.replace('_', ' ')}: for {short_name(metric).lower()}, {why}.")
        kind, opts = "bar", {"horizontal": not time_dim and not s_dim}
    if kind in ("pie",) and plot not in ("value", "share_pct") or (opts.get("stacked") and plot != "value"):
        notes.append(f"Drawn as {'lines' if time_dim else 'bars'} instead of a {chart.replace('_', ' ')}: changes "
                     "aren't parts of a whole.")
        kind, opts = ("line", {}) if time_dim else ("bar", {"horizontal": not s_dim})
    if kind == "pie" and s_dim:
        notes.append("Drawn as a stacked bar instead of a pie: a pie shows one breakdown, and this has two.")
        kind, opts = "bar", {"stacked": True}

    # ---- the data
    if kind == "scatter":
        xs = metrics.compute_metric(principal, metric=metric, group_by=[x_dim], period=period, top_n=top_n, sort=sort,
                                    min_orders=min_orders, filters=flt)
        names = [r[x_dim] for r in xs["rows"] if r["value"] is not None]
        ym = sem["metrics"][y_metric]
        sql, params = metrics.compile_query(y_metric, [x_dim], start, end, None, None, sort, only={**flt, x_dim: names})
        ys = {r[x_dim]: r for r in read_only(sql, params, max_rows=5000)}
        data = []
        for r in xs["rows"]:
            y = ys.get(r[x_dim])
            hidden = ym.get("sensitive") and y is not None and y["orders"] < sem["min_group"]
            if r["value"] is not None and y is not None and y["value"] is not None and not hidden:
                data.append({"x": _label(x_dim, r[x_dim]), "s0": r["value"], "y": y["value"], "orders": r["orders"]})
        lines = [{"key": "s0", "name": f"{short_name(metric)} vs {short_name(y_metric).lower()}"}]
        if len(data) < len(xs["rows"]):
            notes.append(f"{len(xs['rows']) - len(data)} group(s) without both values (or hidden for privacy) are left out.")
        as_of = xs["figures_as_of"]
    elif not s_dim:
        res = metrics.compute_metric(principal, metric=metric, group_by=[x_dim], period=period,
                                     top_n=None if (time_dim or (x_dim in metrics.CYCLIC and not ranked)) else top_n,
                                     sort=sort, min_orders=min_orders,
                                     compare_to=compare_to, compare_period=compare_period,
                                     share_of_total=plot == "share_pct", filters=flt, cumulative=cumulative)
        baseline = _baseline(res)
        fields = FIELDS["value" if kind == "pie" else plot]   # a pie slices values and works out its own shares
        lines = [{"key": f"s{i}", "name": _series_name(metric, f, baseline)} for i, f in enumerate(fields)]
        data = [{"x": _label(x_dim, r[x_dim]), **{f"s{i}": r.get(f) for i, f in enumerate(fields)},
                 "orders": r["orders"], "flag": _flags(r), "compared_with": r.get("compared_with")}
                for r in res["rows"]]
        model_notes.append(res.get("note") or "")
        notes += _human_notes(res)
        if kind == "pie":
            shown = sum(d["s0"] or 0 for d in data)
            rest = round((res["total"] or 0) - shown, 2)
            rest_orders = (res["total_orders"] or 0) - sum(d["orders"] or 0 for d in data)
            if rest > 0.005:   # slices always add up to the whole
                hidden = m.get("sensitive") and rest_orders < sem["min_group"]
                data.append({"x": "Other", "s0": None if hidden else rest, "orders": rest_orders, "other": True,
                             "flag": "hidden for privacy" if hidden else None})
                notes.append(f"'Other' holds the {'rest' if time_dim else 'groups outside the top ' + str(top_n)}, "
                             "so the slices add up to the whole.")
            whole = res["total"] or 0
            for d in data:
                d["share"] = round(100 * d["s0"] / whole, 2) if whole and d["s0"] is not None else None
            if len(data) > 12:
                notes.append("Small slices are hard to compare by eye; hover or use the table for exact shares.")
        as_of = res["figures_as_of"]
    else:
        top_series = metrics.compute_metric(principal, metric=metric, group_by=[s_dim], period=period, top_n=series,
                                            sort=sort, min_orders=min_orders, filters=flt)
        s_names = [r[s_dim] for r in top_series["rows"] if r["value"] is not None]
        if not s_names:
            raise InvalidArguments("No groups with data to chart in this period.")
        x_names = None
        if not time_dim:
            top_x = metrics.compute_metric(principal, metric=metric, group_by=[x_dim], period=period, top_n=top_n,
                                           sort=sort, min_orders=min_orders, filters=flt)
            x_names = [r[x_dim] for r in top_x["rows"]]
        rows = _series_values(metric, x_dim, s_dim, x_names, s_names, start, end, sort, flt) if plot == "value" else []
        additive = metrics._additive(m, metrics.choose_grain(metric, group_by))
        lines, by_x = [], {}
        field = FIELDS[plot][0] if plot != "both" else "value"
        for i, name in enumerate(s_names):
            if plot == "value":
                own = [r for r in rows if r[s_dim] == name]
                if time_dim:
                    own = metrics.complete_series(own, time_dim, start, end, additive)
                    metrics.mark_partial_and_thin(own, time_dim, end, today)
                    metrics.mark_cut(own, time_dim, start, end)
            else:   # the same comparison or running total, one series at a time
                res = metrics.compute_metric(principal, metric=metric, group_by=[x_dim], period=period, sort=sort,
                                             compare_to=compare_to, compare_period=compare_period, filters=flt,
                                             cumulative=cumulative,
                                             only={s_dim: [name], **({x_dim: x_names} if x_names else {})})
                own = res["rows"]
                notes += [n for n in _human_notes(res) if n not in notes]
                model_notes.append(res.get("note") or "")
            lines.append({"key": f"s{i}", "name": _label(s_dim, name)})
            for r in own:
                point = by_x.setdefault(str(r[x_dim]), {"x": _label(x_dim, r[x_dim]), "flag": _flags(r)})
                hide = m.get("sensitive") and (r.get("orders") or 0) < sem["min_group"]
                point[f"s{i}"] = None if hide else r.get(field)
                if r.get("compared_with"):
                    point.setdefault("compared_with", r["compared_with"])
        order = [str(v) for v in x_names] if x_names else sorted(by_x)
        data = [by_x[k] for k in order if k in by_x]
        everyone = len(read_only(*metrics.compile_query(metric, [s_dim], start, end, min_orders, None, sort, only=flt),
                                 max_rows=5000))
        if everyone > len(s_names):
            notes.append(f"Showing the top {len(s_names)} {sem['dimensions'][s_dim]['label']} groups by total; "
                         f"{everyone - len(s_names)} other(s) are left out.")
        if any(p.get("flag") for p in data):
            notes.append("Points marked * are not over yet or have few orders: not a business trend.")
        as_of = top_series["figures_as_of"]

    # ---- colours and line patterns
    if kind == "pie":
        palette = colour_list(len(data), colors)
        for d, (name, hex_colour) in zip(data, palette):
            d["color"] = OTHER_COLOR if d.get("other") else hex_colour
    palette = colour_list(len(lines), colors)
    for i, (line, (name, hex_colour)) in enumerate(zip(lines, palette)):
        line.update(color=hex_colour, color_name=name)
        if kind in ("line", "area") and not opts.get("stacked") and i >= DEFAULT_SERIES:
            line["dash"] = DASHES[(i - DEFAULT_SERIES) % len(DASHES)]

    rule = None
    if color_by_threshold and color_by == "series":
        color_by = "threshold"
    if color_by != "series":
        if kind == "pie":
            notes.append("Slices keep their own colours: a pie shows parts of a whole, not groups of values.")
        else:
            rule = value_colour_rule(data, lines, kind, color_by, threshold, above_color, below_color, bins, breaks,
                                     bin_colors, _value_format(m, plot))

    period_out = {"start": start.isoformat() if start else "first order", "end": end.isoformat()}
    by = " and ".join(sem["dimensions"][g]["label"] for g in group_by)
    spec = {
        "kind": kind, "horizontal": bool(opts.get("horizontal")), "stacked": bool(opts.get("stacked")),
        "donut": bool(opts.get("donut")), "requested": chart,
        "title": title or (f"{short_name(metric)} vs {short_name(y_metric).lower()} by {by}" if kind == "scatter"
                           else f"{PLOT_TITLES[plot].format(Name=short_name(metric), name=_in_sentence(short_name(metric)))} by {by}"),
        "metric": metric, "definition": m["label"], "x_label": sem["dimensions"][x_dim]["label"],
        "unit": "%" if plot in ("change_pct", "share_pct") else m.get("unit"),
        "plotted": _plotted(metric, plot, lines),
        "y_metric": y_metric, "y_definition": sem["metrics"][y_metric]["label"] if y_metric else None,
        "y_unit": sem["metrics"][y_metric].get("unit") if y_metric else None,
        "series": lines, "data": data, "color_rule": rule, "show_values": show_values,
        "filters": metrics.describe_filters(flt), "period": period_out, "figures_as_of": as_of, "notes": notes,
    }
    sink = _charts.get()
    if sink is not None:
        sink.append(spec)
    return {"chart_shown": True, "drawn_as": chart if kind == KINDS[chart][0] else kind, "chart_title": spec["title"],
            "plotted": spec["plotted"], "how_to_read": " ".join(n for n in model_notes if n) or None,
            "series": [{"key": s["key"], "name": s["name"], "color": s["color_name"]} for s in lines],
            "data_keys": "In data, each series' values are under its key (s0, s1...); y is the scatter's second "
                         "measure; share is a slice's % of the whole.", "data": data,
            "definition": m["label"], "period": period_out, "figures_as_of": as_of, "notes": notes,
            "instruction": "The chart is already shown to the admin above your reply. Write one or two sentences "
                           "with the main takeaway and pass on any note (for example why it was drawn differently); "
                           "don't repeat every value. Describe the chart only as 'plotted' says (what its y axis "
                           "shows); if that isn't what the admin asked for, draw it again with the right plot."}


show_chart.__doc__ = f"""Draw a chart of a defined metric in the chat. Use it whenever the admin asks for a chart, graph,
plot, visual or a chart type (pie, bars, lines...), and when a trend or comparison is easier to see than read.
The server builds every value from the same metrics as admin_metric. Never refuse a chart request.
- chart: the type the admin names: line, area, bar (columns), horizontal_bar, stacked_bar, stacked_area, pie,
  donut, scatter. "auto" (no type named) picks the clearest: line for months/years, horizontal bars for a
  ranking, scatter when y_metric is given. If a type can't show the data truthfully (a pie or stack of a rate
  or average), the closest correct chart is drawn and a note says why: pass the note on.
- group_by: one grouping, or two (one as the x axis, the other as series, e.g. ['month', 'buyer_state'] or
  ['category', 'seller_state']); a pie uses one.
- top_n: how many bars, slices or points (default 10, up to {MAX_POINTS}; use the number asked for, e.g. 20);
  in a pie the rest becomes 'Other' so slices add up to the whole.
- series: how many groups as series with two groupings (default {DEFAULT_SERIES}, up to {MAX_SERIES}; e.g. 5 for "top 5").
- plot: what the y axis shows: value (default), or from a comparison (compare_to / compare_period, as in
  admin_metric): change_pct (% change), change ($ or units), previous_value (the comparison period), both (this
  period and the comparison period as two series), or share_pct (each group's % of the whole). E.g. "% change vs
  last year by month": plot change_pct, compare_to same_period_last_year, group_by ['month']. Never draw the
  value when a change was asked for.
- filters: as in admin_metric ({{grouping: [values]}}), e.g. {{"category": ["health_beauty"]}} for one category's trend.
- plot "cumulative": a running total over time (e.g. year to date); "cumulative_both" with a comparison: this
  period's running total and the comparison period's (e.g. this year to date vs last year to date).
- show_values: write each bar's value on it.
- Two different metrics (e.g. revenue and orders) never share one axis: draw two charts, one per metric.
- y_metric: the second measure for a scatter (e.g. metric 'orders', y_metric 'on_time_rate', group_by ['seller']).
- colors: colour names in series (or slice) order: blue, orange, teal, yellow, pink, green, purple, red, gray.
- color_by: colour each bar or point by the group its own value falls in (bars, lines' points, areas, scatters):
  "threshold": at or above `threshold` (default 0) in above_color (default green), below in below_color (default
  red), e.g. growth green and decline red; "quantiles": `bins` groups with equal numbers of bars, highest first
  (bins 4 = quartiles, 5 = quintiles; "top 10 in 5 colours" = bins 5); "ranges": groups between the values in
  `breaks` (e.g. [100000, 1000000]). bin_colors: one colour name per group, highest group first; default green
  -> yellow -> red. "series" (default): one colour per series.
  To change a chart (colours, type, size, threshold), draw it again with the change.
- period: {', '.join(PERIODS)}, a year or a month. sort, min_orders: as in admin_metric.
After it, write only a short takeaway: the admin already sees the chart."""

admin_tool(reads=["admin_order_facts", "admin_seller_order_facts", "admin_item_facts"],
           aggregates=["admin_order_facts.buyer_state", "admin_order_facts.customer_key",
                       "admin_order_facts.payments_total", "admin_order_facts.payment_type",
                       "admin_seller_order_facts.buyer_state", "admin_item_facts.buyer_state"])(show_chart)

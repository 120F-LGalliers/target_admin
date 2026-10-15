"""Calendar (timeline) view: one row per test, a bar for each live period,
and a dotted line for any gap where the test was stopped or paused.

Each row puts the full test name on its own line above the bar, so long
names are never cut off by an axis label limit."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import altair as alt
import pandas as pd

# Colour by status (default). Orange is kept for what is live right now.
STATUS_COLOURS = {
    "Live now": "#D16A0F",      # 120Feet orange
    "Ended": "#8E8278",         # warm grey, recedes
}

# Colour by type. Fixed order, validated for colour-blind separation on white.
TYPE_COLOURS = {
    "A/B": "#D16A0F",
    "XT": "#2C6FB5",
    "Auto-Target": "#1E8A5E",
    "Auto-Allocate": "#A23B72",
}
OTHER_COLOUR = "#8E8278"

INK = "#1F1813"
INK_MUTED = "#6C5F54"
HAIRLINE = "#E4DDD4"
ROW_PX = 44       # name line + bar
BAR_PX = 12
NAME_DY = -10     # name sits above the row centre
BAR_DY = 8        # bar sits below it


def _local(ts: pd.Series, tz: ZoneInfo) -> pd.Series:
    # Vega reads timezone-less ISO strings as the viewer's local time
    return ts.dt.tz_convert(tz).dt.tz_localize(None)


def _ts(t: pd.Timestamp, tz: ZoneInfo) -> str:
    return t.tz_convert(tz).tz_localize(None).isoformat()


def _fmt(t, tz) -> str:
    return "" if pd.isna(t) else t.tz_convert(tz).strftime("%d %b %Y, %H:%M")


def build_calendar(periods: pd.DataFrame, range_start: datetime, range_end: datetime,
                   now: datetime, tz: ZoneInfo, colour_by: str = "Status") -> alt.Chart | None:
    """Return the chart, or None if no test was live during the range."""
    if periods.empty:
        return None

    now_ts = pd.Timestamp(now)
    lo, hi = pd.Timestamp(range_start), pd.Timestamp(range_end)
    p = periods.copy()
    p["end_filled"] = p["end"].fillna(now_ts)
    p = p[(p["start"] < hi) & (p["end_filled"] > lo)]
    if p.empty:
        return None

    # Gaps between consecutive live periods of the same test
    gaps = []
    for aid, g in periods.sort_values("start").groupby("activity_id"):
        g = g.reset_index(drop=True)
        for i in range(len(g) - 1):
            if pd.notna(g.loc[i, "end"]):
                gaps.append({"activity_id": aid, "name": g.loc[i, "name"],
                             "start": g.loc[i, "end"], "end": g.loc[i + 1, "start"]})
    gaps = pd.DataFrame(gaps, columns=["activity_id", "name", "start", "end"])
    if not gaps.empty:
        gaps = gaps[(gaps["start"] < hi) & (gaps["end"] > lo) & gaps["activity_id"].isin(p["activity_id"])]

    # One row per test, keyed by ID so tests with the same name stay separate.
    # Ordered: live now first (newest first), then ended (most recent end first).
    per_test = p.groupby("activity_id").agg(
        name=("name", "first"), type=("type", "first"), live=("ongoing", "any"),
        first_start=("start", "min"), last_end=("end_filled", "max"))
    per_test = per_test.sort_values(["live", "first_start"], ascending=[False, True])
    order = per_test.index.tolist()

    p["live_from"] = p["start"].apply(lambda t: _fmt(t, tz))
    p["live_to"] = p["end"].apply(lambda t: "Still live" if pd.isna(t) else _fmt(t, tz))
    p["days"] = ((p["end_filled"] - p["start"]).dt.total_seconds() / 86400).round(1)
    p["period_label"] = p.apply(lambda r: f"{r['period']} of {r['periods']}", axis=1)
    p["bar_start"] = _local(p["start"].clip(lower=lo), tz)
    p["bar_end"] = _local(p["end_filled"].clip(upper=hi), tz)
    p["state_key"] = p["ongoing"].map({True: "Live now", False: "Ended"})
    p["type_key"] = p["type"].where(p["type"].isin(TYPE_COLOURS), "Other")

    if colour_by == "Type":
        field = "type_key"
        domain = [t for t in TYPE_COLOURS if t in set(p["type_key"])]
        if "Other" in set(p["type_key"]):
            domain.append("Other")
        colours = [TYPE_COLOURS.get(t, OTHER_COLOUR) for t in domain]
    else:
        field = "state_key"
        domain = [s for s in STATUS_COLOURS if s in set(p["state_key"])]
        colours = [STATUS_COLOURS[s] for s in domain]

    height = max(ROW_PX * len(order), 120)
    x_scale = alt.Scale(domain=[_ts(lo, tz), _ts(hi, tz)])
    x = alt.X("bar_start:T", title=None, scale=x_scale,
              axis=alt.Axis(format="%d %b", grid=True, gridColor=HAIRLINE, gridWidth=1,
                            labelColor=INK_MUTED, tickColor=HAIRLINE, domainColor=HAIRLINE,
                            orient="top", labelFlush=False, labelOverlap=True))
    # Hidden band axis: the names are drawn as text above each bar instead
    y = alt.Y("activity_id:N", sort=order, title=None, axis=None,
              scale=alt.Scale(paddingInner=0, paddingOuter=0))

    layers = []

    # Faint separator between rows makes long charts easier to follow
    seps = pd.DataFrame({"activity_id": order})
    layers.append(alt.Chart(seps).mark_rule(color=HAIRLINE, strokeWidth=1, yOffset=ROW_PX / 2)
                  .encode(y=y))

    if not gaps.empty:
        g = gaps.copy()
        g["bar_start"] = _local(g["start"].clip(lower=lo), tz)
        g["bar_end"] = _local(g["end"].clip(upper=hi), tz)
        g["stopped_from"] = g["start"].apply(lambda t: _fmt(t, tz))
        g["stopped_to"] = g["end"].apply(lambda t: _fmt(t, tz))
        g["days"] = ((g["end"] - g["start"]).dt.total_seconds() / 86400).round(1)
        layers.append(
            alt.Chart(g).mark_rule(color=INK_MUTED, strokeWidth=2, strokeDash=[3, 3], yOffset=BAR_DY)
            .encode(x=x, x2="bar_end:T", y=y,
                    tooltip=[alt.Tooltip("name:N", title="Test"),
                             alt.Tooltip("stopped_from:N", title="Stopped or paused"),
                             alt.Tooltip("stopped_to:N", title="Relaunched"),
                             alt.Tooltip("days:Q", title="Days stopped")])
        )

    tooltip = [alt.Tooltip("name:N", title="Test"), alt.Tooltip("type:N", title="Type"),
               alt.Tooltip("live_from:N", title="Live from"), alt.Tooltip("live_to:N", title="Live to"),
               alt.Tooltip("days:Q", title="Days live"), alt.Tooltip("period_label:N", title="Live period"),
               alt.Tooltip("status:N", title="Current status"), alt.Tooltip("activity_id:N", title="Activity ID")]

    layers.append(
        alt.Chart(p).mark_bar(height=BAR_PX, cornerRadius=4, yOffset=BAR_DY).encode(
            x=x, x2="bar_end:T", y=y,
            color=alt.Color(f"{field}:N", title=None, scale=alt.Scale(domain=domain, range=colours),
                            legend=alt.Legend(orient="top", direction="horizontal", symbolType="square",
                                              labelColor=INK, labelFontSize=12, offset=6)),
            tooltip=tooltip,
        )
    )

    # Stop marker where a period ended inside the range
    ends = p[p["end"].notna() & (p["end"] <= hi) & (p["end"] >= lo)]
    if not ends.empty:
        layers.append(
            alt.Chart(ends).mark_point(shape="circle", size=55, filled=True, opacity=1,
                                       color=INK, stroke="white", strokeWidth=2, yOffset=BAR_DY)
            .encode(x="bar_end:T", y=y,
                    tooltip=[alt.Tooltip("name:N", title="Test"), alt.Tooltip("live_to:N", title="Stopped")])
        )

    # Test name (with type) on its own line, starting at the left edge
    names = per_test.reset_index()
    names["label"] = names.apply(lambda r: f"{r['name']}   ·   {r['type']}", axis=1)
    layers.append(
        alt.Chart(names).mark_text(align="left", baseline="middle", dy=NAME_DY, dx=2,
                                   color=INK, fontSize=12, limit=0)
        .encode(x=alt.value(0), y=y, text="label:N",
                tooltip=[alt.Tooltip("name:N", title="Test"), alt.Tooltip("activity_id:N", title="Activity ID")])
    )

    # Today marker
    if lo <= now_ts <= hi:
        today = pd.DataFrame({"t": [_ts(now_ts, tz)], "label": ["Today"]})
        layers.append(alt.Chart(today).mark_rule(color=INK, strokeWidth=1).encode(x=alt.X("t:T", scale=x_scale)))
        layers.append(alt.Chart(today).mark_text(align="right", dx=-4, dy=4, baseline="top",
                                                 color=INK_MUTED, fontSize=11)
                      .encode(x=alt.X("t:T", scale=x_scale), y=alt.value(0), text="label:N"))

    return (
        alt.layer(*layers)
        .properties(height=height)
        .configure_view(strokeWidth=0)
        .configure(font="Manrope, sans-serif", background="white", padding={"left": 8, "right": 16, "top": 4, "bottom": 8})
    )

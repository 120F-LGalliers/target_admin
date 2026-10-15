"""Calendar (timeline) view.

Tests are split into two sections, "Live now" and "Ended", each its own
chart on a shared date axis. Every row has a name line (test name on the
left, type and dates in muted text on the right) with the bar underneath.
Gridlines are weekly or monthly, and alternate rows get a faint band
instead of separator lines, which keeps the chart quiet enough to scan."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import altair as alt
import pandas as pd

# Colour by status (default): orange is kept for what is live right now.
LIVE_COLOUR = "#D16A0F"     # 120Feet orange
ENDED_COLOUR = "#A39A91"    # warm grey, recedes

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
GRID = "#EFEAE3"
BAND = "#FAF7F2"
ROW_PX = 46
BAR_PX = 10
NAME_DY = -9
BAR_DY = 9


def _local(ts: pd.Series, tz: ZoneInfo) -> pd.Series:
    # Vega reads timezone-less ISO strings as the viewer's local time
    return ts.dt.tz_convert(tz).dt.tz_localize(None)


def _iso(t: pd.Timestamp, tz: ZoneInfo) -> str:
    return t.tz_convert(tz).tz_localize(None).isoformat()


def _fmt(t, tz, with_time=True) -> str:
    if pd.isna(t):
        return ""
    return t.tz_convert(tz).strftime("%d %b %Y, %H:%M" if with_time else "%d %b")


def _ticks(lo: pd.Timestamp, hi: pd.Timestamp, tz: ZoneInfo) -> tuple[list[str], str]:
    """Weekly ticks (Mondays) for short ranges, month starts for long ones."""
    # Work in naive local time so clock changes don't shift ticks off midnight
    start, end = lo.tz_convert(tz).tz_localize(None), hi.tz_convert(tz).tz_localize(None)
    days = (end - start).days
    ticks = []
    if days <= 100:
        d = (start + timedelta(days=(7 - start.weekday()) % 7)).normalize()
        while d <= end:
            ticks.append(d)
            d += timedelta(days=7)
        fmt = "%d %b"
    else:
        d = start.normalize().replace(day=1)
        if d < start:
            d = (d + timedelta(days=32)).replace(day=1)
        while d <= end:
            ticks.append(d)
            d = (d + timedelta(days=32)).replace(day=1)
        fmt = "%b %Y"
    return [t.isoformat() for t in ticks], fmt


def build_calendar(periods: pd.DataFrame, range_start: datetime, range_end: datetime,
                   now: datetime, tz: ZoneInfo, colour_by: str = "Status") -> list[tuple[str, alt.Chart]]:
    """Return [(section title, chart), ...]; empty if nothing was live in the range."""
    if periods.empty:
        return []

    now_ts = pd.Timestamp(now)
    lo, hi = pd.Timestamp(range_start), pd.Timestamp(range_end)
    p = periods.copy()
    p["end_filled"] = p["end"].fillna(now_ts)
    p = p[(p["start"] < hi) & (p["end_filled"] > lo)]
    if p.empty:
        return []

    # Per-test summary: drives sections, ordering and the right-hand date text
    per = p.groupby("activity_id").agg(
        name=("name", "first"), type=("type", "first"), live=("ongoing", "any"),
        first_start=("start", "min"), last_end=("end", "max"), n=("period", "count"))
    per["dates"] = per.apply(
        lambda r: f"{r['type']}  ·  {_fmt(r['first_start'], tz, False)} – "
                  + ("now" if r["live"] else _fmt(r["last_end"], tz, False)), axis=1)

    # Gaps between consecutive live periods of the same test (stopped/paused)
    gaps = []
    for aid, g in periods[periods["activity_id"].isin(per.index)].sort_values("start").groupby("activity_id"):
        g = g.reset_index(drop=True)
        for i in range(len(g) - 1):
            if pd.notna(g.loc[i, "end"]):
                gaps.append({"activity_id": aid, "name": g.loc[i, "name"],
                             "start": g.loc[i, "end"], "end": g.loc[i + 1, "start"]})
    gaps = pd.DataFrame(gaps, columns=["activity_id", "name", "start", "end"])
    if not gaps.empty:
        gaps = gaps[(gaps["start"] < hi) & (gaps["end"] > lo)]

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
        domain, colours = ["Live now", "Ended"], [LIVE_COLOUR, ENDED_COLOUR]

    tick_values, tick_fmt = _ticks(lo, hi, tz)
    sections = [
        ("Live now", per[per["live"]].sort_values("first_start", ascending=False)),
        ("Ended", per[~per["live"]].sort_values("last_end", ascending=False)),
    ]
    out = []
    for title, sec in sections:
        if sec.empty:
            continue
        ids = sec.index.tolist()
        chart = _section_chart(
            p[p["activity_id"].isin(ids)], gaps[gaps["activity_id"].isin(ids)] if not gaps.empty else gaps,
            sec, ids, lo, hi, now_ts, tz, field, domain, colours, tick_values, tick_fmt,
            show_legend=(colour_by == "Type"))
        out.append((f"{title} ({len(ids)})", chart))
    return out


def _section_chart(p, gaps, per, order, lo, hi, now_ts, tz, field, domain, colours,
                   tick_values, tick_fmt, show_legend) -> alt.Chart:
    height = ROW_PX * len(order)
    x_scale = alt.Scale(domain=[_iso(lo, tz), _iso(hi, tz)])
    x = alt.X("bar_start:T", title=None, scale=x_scale,
              axis=alt.Axis(values=tick_values, format=tick_fmt, grid=True, gridColor=GRID, gridWidth=1,
                            labelColor=INK_MUTED, labelFontSize=11, ticks=False, domain=False,
                            orient="top", labelFlush=False, labelPadding=6))
    y = alt.Y("activity_id:N", sort=order, title=None, axis=None,
              scale=alt.Scale(paddingInner=0, paddingOuter=0))

    layers = []

    # Faint banding on alternate rows helps the eye follow a row across
    band = pd.DataFrame({"activity_id": order[1::2]})
    if not band.empty:
        layers.append(alt.Chart(band).mark_rect(color=BAND).encode(
            y=y, x=alt.value(0), x2=alt.value("width")))

    if not gaps.empty:
        g = gaps.copy()
        g["bar_start"] = _local(g["start"].clip(lower=lo), tz)
        g["bar_end"] = _local(g["end"].clip(upper=hi), tz)
        g["stopped_from"] = g["start"].apply(lambda t: _fmt(t, tz))
        g["stopped_to"] = g["end"].apply(lambda t: _fmt(t, tz))
        g["days"] = ((g["end"] - g["start"]).dt.total_seconds() / 86400).round(1)
        layers.append(
            alt.Chart(g).mark_rule(color=INK_MUTED, strokeWidth=1.5, strokeDash=[2, 3], yOffset=BAR_DY)
            .encode(x=x, x2="bar_end:T", y=y,
                    tooltip=[alt.Tooltip("name:N", title="Test"),
                             alt.Tooltip("stopped_from:N", title="Stopped or paused"),
                             alt.Tooltip("stopped_to:N", title="Relaunched"),
                             alt.Tooltip("days:Q", title="Days stopped")]))

    legend = (alt.Legend(orient="top", direction="horizontal", symbolType="square", title=None,
                         labelColor=INK, labelFontSize=12, offset=4) if show_legend else None)
    layers.append(
        alt.Chart(p).mark_bar(height=BAR_PX, cornerRadius=3, yOffset=BAR_DY).encode(
            x=x, x2="bar_end:T", y=y,
            color=alt.Color(f"{field}:N", scale=alt.Scale(domain=domain, range=colours), legend=legend),
            tooltip=[alt.Tooltip("name:N", title="Test"), alt.Tooltip("type:N", title="Type"),
                     alt.Tooltip("live_from:N", title="Live from"), alt.Tooltip("live_to:N", title="Live to"),
                     alt.Tooltip("days:Q", title="Days live"), alt.Tooltip("period_label:N", title="Live period"),
                     alt.Tooltip("activity_id:N", title="Activity ID")]))

    names = per.reset_index()
    # Test name, left. Long names are trimmed only where they would run into
    # the dates on the right; the full name is in the tooltip.
    layers.append(
        alt.Chart(names).mark_text(align="left", baseline="middle", dy=NAME_DY, dx=6, color=INK,
                                   fontSize=12.5, fontWeight=500, limit=640)
        .encode(x=alt.value(0), y=y, text="name:N",
                tooltip=[alt.Tooltip("name:N", title="Test"), alt.Tooltip("activity_id:N", title="Activity ID")]))
    # Type and dates, right, muted
    layers.append(
        alt.Chart(names).mark_text(align="right", baseline="middle", dy=NAME_DY, dx=-6, color=INK_MUTED,
                                   fontSize=11.5)
        .encode(x=alt.value("width"), y=y, text="dates:N"))

    if lo <= now_ts <= hi:
        # "Today" drawn as a short tick on each bar line only, so it never
        # cuts through the names and dates on the line above
        today = pd.DataFrame({"activity_id": order, "t": _iso(now_ts, tz)})
        layers.append(alt.Chart(today).mark_tick(color=INK, thickness=1.5, size=ROW_PX / 2, opacity=0.7,
                                                 yOffset=BAR_DY)
                      .encode(x=alt.X("t:T", scale=x_scale), y=y,
                              tooltip=[alt.Tooltip("t:T", title="Today", format="%d %b %Y")]))

    return (
        alt.layer(*layers)
        .properties(height=height)
        .configure_view(strokeWidth=0)
        .configure(font="Manrope, sans-serif", background="white",
                   padding={"left": 4, "right": 4, "top": 4, "bottom": 4})
    )

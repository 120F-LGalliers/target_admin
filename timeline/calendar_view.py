"""Calendar (timeline) view: one row per test, a bar for each live period,
and a thin line for any gap where the test was stopped or paused."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import altair as alt
import pandas as pd

# Fixed categorical order, validated for colour-blind separation against
# white. Colour follows the type, so filtering never repaints a test.
TYPE_COLOURS = {
    "A/B": "#D16A0F",          # 120Feet orange
    "XT": "#2C6FB5",
    "Auto-Target": "#1E8A5E",
    "Auto-Allocate": "#A23B72",
}
OTHER_COLOUR = "#8E8278"
INK_MUTED = "#6C5F54"
INK_HAIRLINE = "#C9C0B7"
ROW_PX = 30
BAR_PX = 14


def _local(ts: pd.Series, tz: ZoneInfo) -> pd.Series:
    # Vega reads timezone-less ISO strings as the viewer's local time
    return ts.dt.tz_convert(tz).dt.tz_localize(None)


def build_calendar(periods: pd.DataFrame, range_start: datetime, range_end: datetime,
                   now: datetime, tz: ZoneInfo) -> alt.Chart | None:
    """Return the chart, or None if no test was live during the range."""
    if periods.empty:
        return None

    p = periods.copy()
    p["end_filled"] = p["end"].fillna(pd.Timestamp(now))
    lo, hi = pd.Timestamp(range_start), pd.Timestamp(range_end)
    p = p[(p["start"] < hi) & (p["end_filled"] > lo)]
    if p.empty:
        return None

    # Gaps between consecutive live periods of the same test
    gaps = []
    for aid, g in periods.sort_values("start").groupby("activity_id"):
        g = g.reset_index(drop=True)
        for i in range(len(g) - 1):
            if pd.notna(g.loc[i, "end"]):
                gaps.append({"activity_id": aid, "name": g.loc[i, "name"], "type": g.loc[i, "type"],
                             "start": g.loc[i, "end"], "end": g.loc[i + 1, "start"]})
    gaps = pd.DataFrame(gaps, columns=["activity_id", "name", "type", "start", "end"])
    if not gaps.empty:
        gaps = gaps[(gaps["start"] < hi) & (gaps["end"] > lo)]

    # Row label = test name, made unique if two tests share a name
    names = p.groupby("activity_id")["name"].first()
    dupes = names[names.duplicated(keep=False)].index
    label = {aid: (f"{n} ({aid})" if aid in dupes else n) for aid, n in names.items()}
    order = (p.groupby("activity_id")["start"].min().sort_values().index.map(label).tolist())

    p["row"] = p["activity_id"].map(label)
    p["bar_start"] = _local(p["start"].clip(lower=lo), tz)
    p["bar_end"] = _local(p["end_filled"].clip(upper=hi), tz)
    p["live_from"] = _local(p["start"], tz).dt.strftime("%d %b %Y, %H:%M")
    p["live_to"] = p["end"].apply(lambda t: "Still live" if pd.isna(t) else
                                  t.tz_convert(tz).strftime("%d %b %Y, %H:%M"))
    p["days"] = ((p["end_filled"] - p["start"]).dt.total_seconds() / 86400).round(1)
    p["period_label"] = p.apply(lambda r: f"{r['period']} of {r['periods']}", axis=1)
    p["type_key"] = p["type"].where(p["type"].isin(TYPE_COLOURS), "Other")

    domain = [t for t in TYPE_COLOURS if t in set(p["type_key"])]
    if "Other" in set(p["type_key"]):
        domain.append("Other")
    colours = [TYPE_COLOURS.get(t, OTHER_COLOUR) for t in domain]

    height = max(ROW_PX * len(order), 120)
    x = alt.X("bar_start:T", title=None, scale=alt.Scale(domain=[_ts(lo, tz), _ts(hi, tz)]),
              axis=alt.Axis(format="%d %b", grid=True, gridColor="#E4DDD4", gridWidth=1,
                            labelColor=INK_MUTED, tickColor=INK_HAIRLINE, domainColor=INK_HAIRLINE,
                            orient="top", labelFlush=False))
    y = alt.Y("row:N", sort=order, title=None,
              axis=alt.Axis(labelLimit=260, labelColor="#1F1813", labelFontSize=12,
                            ticks=False, domain=False, labelPadding=8))

    layers = []
    if not gaps.empty:
        g = gaps.copy()
        g["row"] = g["activity_id"].map(label)
        g = g[g["row"].notna()]
        g["bar_start"] = _local(g["start"].clip(lower=lo), tz)
        g["bar_end"] = _local(g["end"].clip(upper=hi), tz)
        g["stopped_from"] = _local(g["start"], tz).dt.strftime("%d %b %Y, %H:%M")
        g["stopped_to"] = _local(g["end"], tz).dt.strftime("%d %b %Y, %H:%M")
        g["days"] = ((g["end"] - g["start"]).dt.total_seconds() / 86400).round(1)
        layers.append(
            alt.Chart(g).mark_rule(color=INK_MUTED, strokeWidth=2, strokeDash=[3, 3]).encode(
                x=x, x2="bar_end:T", y=y,
                tooltip=[alt.Tooltip("name:N", title="Test"),
                         alt.Tooltip("stopped_from:N", title="Stopped / paused from"),
                         alt.Tooltip("stopped_to:N", title="Relaunched"),
                         alt.Tooltip("days:Q", title="Days stopped")],
            )
        )

    bars = alt.Chart(p).mark_bar(height=BAR_PX, cornerRadius=4).encode(
        x=x, x2="bar_end:T", y=y,
        color=alt.Color("type_key:N", title=None, scale=alt.Scale(domain=domain, range=colours),
                        legend=alt.Legend(orient="top", direction="horizontal", symbolType="square",
                                          labelColor="#1F1813", labelFontSize=12)),
        tooltip=[alt.Tooltip("name:N", title="Test"), alt.Tooltip("type:N", title="Type"),
                 alt.Tooltip("live_from:N", title="Live from"), alt.Tooltip("live_to:N", title="Live to"),
                 alt.Tooltip("days:Q", title="Days live"), alt.Tooltip("period_label:N", title="Live period"),
                 alt.Tooltip("status:N", title="Current status")],
    )
    layers.append(bars)

    # End caps: a small dot where a test concluded inside the range
    ends = p[p["end"].notna() & (p["end"] <= hi) & (p["end"] >= lo)]
    if not ends.empty:
        layers.append(alt.Chart(ends).mark_point(shape="circle", size=60, filled=True,
                                                 color="#1F1813", stroke="white", strokeWidth=2)
                      .encode(x=alt.X("bar_end:T"), y=y,
                              tooltip=[alt.Tooltip("name:N", title="Test"),
                                       alt.Tooltip("live_to:N", title="Stopped")]))

    # Today marker
    if lo <= pd.Timestamp(now) <= hi:
        today = pd.DataFrame({"t": [_ts(pd.Timestamp(now), tz)], "label": ["Today"]})
        layers.append(alt.Chart(today).mark_rule(color="#1F1813", strokeWidth=1).encode(x="t:T"))
        layers.append(alt.Chart(today).mark_text(align="center", dy=6, color=INK_MUTED, fontSize=11,
                                                 baseline="top")
                      .encode(x="t:T", y=alt.value(height), text="label:N"))

    return (
        alt.layer(*layers)
        .properties(height=height)
        .configure_view(strokeWidth=0)
        .configure(font="Manrope, sans-serif", background="white")
    )


def _ts(t: pd.Timestamp, tz: ZoneInfo) -> str:
    return t.tz_convert(tz).tz_localize(None).isoformat()

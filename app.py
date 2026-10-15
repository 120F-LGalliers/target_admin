"""Tesco Mobile experiment timeline: which Adobe Target tests started, first
went live, or concluded in a chosen date range."""

from __future__ import annotations

import base64
import threading
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from timeline.calendar_view import build_calendar
from timeline.demo import build_demo, build_demo_jira
from timeline.jira_api import JiraClient, JiraConfig, NO_OUTCOME, NOT_MATCHED
from timeline.lifecycle import in_range
from timeline.store import Store
from timeline.target_api import TargetClient, TargetConfig

APP_DIR = Path(__file__).parent
st.set_page_config(page_title="TM Experiment Timeline", page_icon=str(APP_DIR / "assets" / "logo-mark.png"), layout="wide")

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

def _secret(section: str, key: str, default=None):
    try:
        value = st.secrets[section][key]
    except Exception:  # no secrets file, missing section or key
        return default
    return value.strip() if isinstance(value, str) else value


def _flag(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


TZ = ZoneInfo(_secret("app", "timezone", "Europe/London"))
REFRESH_HOURS = float(_secret("app", "refresh_hours", 6))
CACHE_PATH = APP_DIR / _secret("app", "cache_path", "data/cache.json")
REQUIRED = ("tenant", "client_id", "client_secret")
MISSING = [f"target.{k}" for k in REQUIRED if not _secret("target", k)]
HAS_CREDS = not MISSING
_demo_setting = _secret("app", "demo_mode")
DEMO_MODE = _flag(_demo_setting) if _demo_setting is not None else not HAS_CREDS


def _demo_reason() -> str:
    """Explain why the app is in demo mode, without revealing any values."""
    if _demo_setting is not None and _flag(_demo_setting):
        return "<code>demo_mode</code> is set to true in the <code>[app]</code> secrets."
    try:
        sections = list(st.secrets.keys())
    except Exception:
        sections = []
    if not sections:
        return "No secrets were found. Add them in the app's Settings, under Secrets."
    found = ", ".join(f"<code>[{s}]</code>" for s in sections) or "none"
    missing = ", ".join(f"<code>{m}</code>" for m in MISSING)
    return f"Missing or empty: {missing}. Top-level entries found: {found}. Check the keys sit under a <code>[target]</code> header."
DEFAULT_TYPES = list(_secret("app", "default_types", ["A/B", "XT"]))

# --------------------------------------------------------------------------
# Styling (120Feet tokens)
# --------------------------------------------------------------------------

st.markdown(
    """
<style>
@import url('https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700;800&family=Archivo+Black&display=swap');
:root { --orange:#D16A0F; --ink-1000:#14100D; --ink-900:#1F1813; --ink-700:#3D332B;
        --ink-500:#6C5F54; --ink-200:#E4DDD4; --ink-100:#F1ECE5; --ink-50:#FAF7F2; }
html, body, [class*="css"], .stMarkdown, .stDataFrame, button, input { font-family:'Manrope', sans-serif; }
.block-container { padding-top: 3.5rem; max-width: 1400px; }
h1, h2, h3 { color: var(--ink-1000); letter-spacing: -0.01em; }
.tt-header { display:flex; align-items:center; gap:14px; margin-bottom:4px; }
.tt-header img { width:40px; height:40px; }
.tt-title { font-family:'Archivo Black', sans-serif; font-size:1.7rem; color:var(--ink-1000); line-height:1.1; }
.tt-sub { color:var(--ink-500); font-size:0.95rem; margin:0 0 1.2rem 54px; }
.tt-kpis { display:grid; grid-template-columns:repeat(4, minmax(0,1fr)); gap:12px; margin:0.5rem 0 1.2rem; }
.tt-kpi { background:#fff; border:1px solid var(--ink-200); border-radius:10px; padding:14px 16px; }
.tt-kpi .label { color:var(--ink-500); font-size:0.8rem; font-weight:600; }
.tt-kpi .value { font-family:'Archivo Black', sans-serif; font-size:2rem; color:var(--ink-1000); line-height:1.2; }
.tt-kpi .hint { color:var(--ink-500); font-size:0.75rem; }
.tt-kpi.accent { border-color: var(--orange); box-shadow: inset 4px 0 0 var(--orange); }
.tt-kpi.accent .value { color: var(--orange); }
.tt-banner { background:#FDF3E8; border:1px solid #F4C49A; color:var(--ink-900); border-radius:8px;
             padding:10px 14px; font-size:0.88rem; margin-bottom:1rem; }
@media (max-width: 900px) { .tt-kpis { grid-template-columns:repeat(2, minmax(0,1fr)); } .tt-sub { margin-left:0; } }
</style>
""",
    unsafe_allow_html=True,
)

# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------

@st.cache_resource
def _refresh_lock() -> threading.Lock:
    return threading.Lock()


def _client() -> TargetClient:
    cfg = TargetConfig(
        tenant=_secret("target", "tenant"),
        client_id=_secret("target", "client_id"),
        client_secret=_secret("target", "client_secret"),
    )
    if _secret("target", "scopes"):
        cfg.scopes = _secret("target", "scopes")
    if _secret("target", "history_path"):
        cfg.history_path = _secret("target", "history_path")
    if _secret("target", "accept_header"):
        cfg.accept_header = _secret("target", "accept_header")
    return TargetClient(cfg)


HAS_JIRA = bool(_secret("jira", "site") and _secret("jira", "email") and _secret("jira", "api_token"))


def _jira_client() -> JiraClient:
    cfg = JiraConfig(site=_secret("jira", "site"), email=_secret("jira", "email"),
                     api_token=_secret("jira", "api_token"))
    if _secret("jira", "jql"):
        cfg.jql = _secret("jira", "jql")
    if _secret("jira", "projects"):
        cfg.projects = list(_secret("jira", "projects"))
    return JiraClient(cfg)


def refresh(store: Store) -> dict:
    with _refresh_lock():
        if DEMO_MODE:
            acts, hists = build_demo()
            return store.refresh(lambda: acts, lambda aid: hists[aid],
                                 jira_fn=lambda names: build_demo_jira(acts, hists), jira_projects=["DEMO"])
        client = _client()
        jira = _jira_client() if HAS_JIRA else None
        bar = st.progress(0.0, text="Fetching activities from Adobe Target")
        result = store.refresh(client.list_activities, client.activity_history,
                               progress=lambda p, msg: bar.progress(p, text=msg),
                               jira_fn=jira.fetch if jira else None,
                               jira_projects=jira.cfg.projects if jira else None)
        bar.empty()
        return result


cache_path = CACHE_PATH.with_name("demo_cache.json") if DEMO_MODE else CACHE_PATH
store = Store(cache_path)

age = store.age_hours()
if age is None or age > REFRESH_HOURS:
    with st.spinner("Refreshing data from Adobe Target"):
        try:
            res = refresh(store)
            if res.get("jira_error"):
                st.warning(f"Jira outcomes could not be refreshed: {res['jira_error']}")
            if res.get("errors"):
                st.warning(f"History could not be fetched for {len(res['errors'])} activities. Their dates may be incomplete.")
        except Exception as exc:
            st.error(f"Refresh failed: {exc}")

df = store.lifecycle_frame()
JIRA_ON = store.jira_enabled
if store.data.get("jira_error"):
    st.warning(f"Jira outcomes could not be refreshed, so they may be out of date: {store.data['jira_error']}")

# --------------------------------------------------------------------------
# Header
# --------------------------------------------------------------------------

logo = APP_DIR / "assets" / "logo-mark.png"
left, right = st.columns([5, 2])
with left:
    logo_b64 = base64.b64encode(logo.read_bytes()).decode()
    st.markdown(
        f'<div class="tt-header"><img src="data:image/png;base64,{logo_b64}" alt="120Feet">'
        '<div class="tt-title">Experiment Timeline</div></div>',
        unsafe_allow_html=True,
    )
    st.caption("Tesco Mobile · Adobe Target. See which tests started, first went live or concluded in any date range.")
with right:
    refreshed = store.refreshed_at
    label = refreshed.astimezone(TZ).strftime("%d %b %Y, %H:%M") if refreshed else "never"
    st.markdown(f"<div style='text-align:right;color:#6C5F54;font-size:0.85rem;margin-top:6px'>Data refreshed {label}</div>", unsafe_allow_html=True)
    if st.button("Refresh now", width="stretch"):
        with st.spinner("Refreshing data from Adobe Target"):
            try:
                refresh(store)
                st.rerun()
            except Exception as exc:
                st.error(f"Refresh failed: {exc}")

if DEMO_MODE:
    st.markdown('<div class="tt-banner"><b>Demo data.</b> Every test name and date shown is made up. '
                f'Why: {_demo_reason()}</div>',
                unsafe_allow_html=True)

if df.empty:
    st.info("No activities found yet.")
    st.stop()

# --------------------------------------------------------------------------
# Filters
# --------------------------------------------------------------------------

today = datetime.now(TZ).date()
presets = {
    "Last 30 days": (today - timedelta(days=29), today),
    "This month": (today.replace(day=1), today),
    "Last month": ((today.replace(day=1) - timedelta(days=1)).replace(day=1), today.replace(day=1) - timedelta(days=1)),
    "This quarter": (date(today.year, 3 * ((today.month - 1) // 3) + 1, 1), today),
    "Year to date": (date(today.year, 1, 1), today),
    "Custom": None,
}

f1, f2, f3, f4 = st.columns([1.3, 1.6, 2, 2])
preset = f1.selectbox("Period", list(presets), index=0)
if presets[preset]:
    start_d, end_d = presets[preset]
    f2.date_input("Dates", value=(start_d, end_d), disabled=True, format="DD/MM/YYYY", key=f"dates-{preset}")
else:
    picked = f2.date_input("Dates", value=(today - timedelta(days=29), today), format="DD/MM/YYYY")
    if isinstance(picked, (list, tuple)) and len(picked) == 2:
        start_d, end_d = picked
    else:
        st.info("Pick an end date to finish the range.")
        st.stop()

all_types = sorted(df["type"].unique(), key=lambda t: (t not in ("A/B", "XT"), t))
types = f3.multiselect("Activity types", all_types, default=[t for t in DEFAULT_TYPES if t in all_types])
search = f4.text_input("Search test name", placeholder="e.g. Clubcard")

range_start = datetime.combine(start_d, time.min, TZ).astimezone(timezone.utc)
range_end = datetime.combine(end_d + timedelta(days=1), time.min, TZ).astimezone(timezone.utc)

view = df[df["type"].isin(types)]
if search:
    view = view[view["name"].str.contains(search, case=False, na=False)]

OUTCOME_ORDER = ["Winner", "Loser", "Inconclusive", "Descoped", NO_OUTCOME, NOT_MATCHED]
if JIRA_ON:
    present = [o for o in OUTCOME_ORDER if o in set(df["outcome"])]
    outcomes = st.multiselect("Outcome (from Jira)", present, default=present, key="outcome-filter",
                              help="Taken from the Jira ticket's status: Winners, Losers, Inconclusive or Descoped.")
    view = view[view["outcome"].isin(outcomes)]

started = view[view["last_activated_at"].apply(lambda t: in_range(t, range_start, range_end))]
first_live = view[view["first_live_at"].apply(lambda t: in_range(t, range_start, range_end))]
concluded = view[view["concluded_at"].apply(lambda t: in_range(t, range_start, range_end))]
live_now = view[view["status"] == "Live"]

# --------------------------------------------------------------------------
# KPIs
# --------------------------------------------------------------------------

period_txt = f"{start_d:%d %b} to {end_d:%d %b %Y}"
concluded_hint = "Deactivated, archived or end date passed"
if JIRA_ON and len(concluded):
    oc = concluded["outcome"].value_counts()
    parts = [f"{oc.get(o, 0)} {lbl}" for o, lbl in
             (("Winner", "won"), ("Loser", "lost"), ("Inconclusive", "inconclusive")) if oc.get(o, 0)]
    pending = len(concluded) - sum(oc.get(o, 0) for o in ("Winner", "Loser", "Inconclusive"))
    if pending:
        parts.append(f"{pending} awaiting outcome")
    concluded_hint = " · ".join(parts) or concluded_hint
st.markdown(
    f"""
<div class="tt-kpis">
  <div class="tt-kpi accent"><div class="label">Started</div><div class="value">{len(started)}</div><div class="hint">Last activated, {period_txt}</div></div>
  <div class="tt-kpi"><div class="label">First went live</div><div class="value">{len(first_live)}</div><div class="hint">First ever go-live in period</div></div>
  <div class="tt-kpi"><div class="label">Concluded</div><div class="value">{len(concluded)}</div><div class="hint">{concluded_hint}</div></div>
  <div class="tt-kpi"><div class="label">Live now</div><div class="value">{len(live_now)}</div><div class="hint">Running today</div></div>
</div>
""",
    unsafe_allow_html=True,
)

# --------------------------------------------------------------------------
# Tables
# --------------------------------------------------------------------------

def _fmt(frame: pd.DataFrame, sort_col: str) -> pd.DataFrame:
    out = frame.copy()
    now = pd.Timestamp.now(tz="UTC")
    end = out["concluded_at"].fillna(now)
    out["days_live"] = ((end - out["last_activated_at"]).dt.total_seconds() / 86400).round().astype("Int64")
    for col in ("last_activated_at", "first_live_at", "concluded_at", "scheduled_start", "scheduled_end"):
        out[col] = out[col].dt.tz_convert(TZ).dt.tz_localize(None)
    out = out.sort_values(sort_col, ascending=False, na_position="last")
    cols = ["name", "type", "status"]
    if JIRA_ON:
        cols += ["outcome", "jira_url"]
    cols += ["last_activated_at", "first_live_at", "concluded_at",
             "days_live", "live_periods", "activity_id", "date_source", "notes"]
    return out[cols]


COLUMNS = {
    "name": st.column_config.TextColumn("Test name", width="large"),
    "type": st.column_config.TextColumn("Type", width="small"),
    "status": st.column_config.TextColumn("Status", width="small"),
    "last_activated_at": st.column_config.DatetimeColumn("Last activated", format="DD MMM YYYY, HH:mm"),
    "first_live_at": st.column_config.DatetimeColumn("First went live", format="DD MMM YYYY, HH:mm"),
    "concluded_at": st.column_config.DatetimeColumn("Concluded", format="DD MMM YYYY, HH:mm"),
    "days_live": st.column_config.NumberColumn("Days live", help="Since last activation, to conclusion or today"),
    "live_periods": st.column_config.NumberColumn("Live periods", help="More than 1 means the test was stopped and relaunched"),
    "activity_id": st.column_config.TextColumn("Activity ID"),
    "date_source": st.column_config.TextColumn("Date source", help="Change log is exact. Schedule only means no state history was found."),
    "notes": st.column_config.TextColumn("Notes"),
    "outcome": st.column_config.TextColumn("Outcome", help="From the Jira ticket status"),
    "jira_url": st.column_config.LinkColumn("Jira", display_text=r"/browse/(.*)$"),
}


def table(frame: pd.DataFrame, sort_col: str, key: str, empty_msg: str):
    if frame.empty:
        st.info(empty_msg)
        return
    shown = _fmt(frame, sort_col)
    st.dataframe(shown, column_config=COLUMNS, hide_index=True, width="stretch")
    csv = shown.to_csv(index=False).encode("utf-8")
    st.download_button("Download CSV", csv, file_name=f"tm-{key}-{start_d:%Y%m%d}-{end_d:%Y%m%d}.csv",
                       mime="text/csv", key=f"dl-{key}")


periods = store.periods_frame()
if not periods.empty:
    periods = periods[periods["activity_id"].isin(view["activity_id"])]
    periods = periods.merge(df[["activity_id", "outcome"]], on="activity_id", how="left")

tc, t1, t2, t3, t4 = st.tabs([
    "Calendar", f"Started ({len(started)})", f"First went live ({len(first_live)})",
    f"Concluded ({len(concluded)})", f"All tests ({len(view)})",
])
with tc:
    colour_by = st.radio("Colour bars by", ["Status", "Type"] + (["Outcome"] if JIRA_ON else []),
                         horizontal=True, key="cal-colour",
                         help="Status shows what is live now in orange. Type separates A/B, XT and others. "
                              "Outcome uses the Jira ticket status.")
    sections = build_calendar(periods, range_start, range_end, datetime.now(timezone.utc), TZ, colour_by=colour_by)
    if not sections:
        st.info("No tests were live during this period.")
    else:
        st.caption("Each bar shows when a test was live. A dotted line means it was stopped or paused, "
                   "then relaunched. The short dark mark on each row is today. Hover over a bar for exact times.")
        for title, chart in sections:
            st.markdown(f"<div style='font-weight:700;color:#1F1813;margin:14px 0 2px'>{title}</div>",
                        unsafe_allow_html=True)
            st.altair_chart(chart, width="stretch")

    # Tests that have been live but have no usable dates can't be drawn
    placed = set(periods["activity_id"]) if not periods.empty else set()
    unplaced = view[view["status"].isin(["Live", "Concluded", "Paused"]) & ~view["activity_id"].isin(placed)]
    if not unplaced.empty:
        st.warning(f"{len(unplaced)} test(s) have been live but have no go-live date, so they can't be shown "
                   "on the calendar. Use \"Can't find a test?\" below to see why.")
        st.dataframe(unplaced[["name", "type", "status", "activity_id", "notes"]], hide_index=True,
                     width="stretch", column_config=COLUMNS)
with t1:
    table(started, "last_activated_at", "started", "No tests were activated in this period.")
with t2:
    table(first_live, "first_live_at", "first-live", "No tests went live for the first time in this period.")
with t3:
    table(concluded, "concluded_at", "concluded", "No tests concluded in this period.")
with t4:
    table(view, "last_activated_at", "all", "No tests match these filters.")

with st.expander("Can't find a test?"):
    st.caption("Searches everything pulled from Target, ignoring the date range and type filters.")
    q = st.text_input("Test name or activity ID", key="lookup")
    if q:
        recs = store.data.get("activities", {})
        hits = [(aid, r) for aid, r in recs.items()
                if q.strip() == aid or q.lower() in str(r["activity"].get("name", "")).lower()]
        if not hits:
            seen = sorted({str(r["activity"].get("workspace") or "Default Workspace") for r in recs.values()})
            st.error(f"No activity matching \"{q}\" came back from Target ({len(recs)} activities in total, "
                     f"from workspaces: {', '.join(seen) or 'unknown'}). The most likely cause is that the test sits "
                     "in a workspace the API credential can't see. Check its product profile in the Adobe Admin "
                     "Console covers every Tesco Mobile workspace, then press Refresh now.")
        for aid, rec in hits[:5]:
            row = df[df["activity_id"] == aid].iloc[0]
            act = rec["activity"]
            st.markdown(f"**{row['name']}** · {row['type']} · ID {aid}")
            reasons = []
            if JIRA_ON and row["outcome"] not in outcomes:
                reasons.append(f"its outcome ({row['outcome']}) is not selected in the Outcome filter")
            if row["type"] not in types:
                reasons.append(f"its type ({row['type']}) is not selected in Activity types")
            if pd.isna(row["last_activated_at"]):
                reasons.append("no go-live date could be worked out (see the change log below)")
            elif not any(in_range(t, range_start, range_end) for t in
                         (row["last_activated_at"], row["first_live_at"], row["concluded_at"])):
                reasons.append("none of its dates fall in the selected period")
            if reasons:
                st.write("Not showing in the lists because " + "; ".join(reasons) + ".")
            fmt = lambda t: "" if pd.isna(t) else t.tz_convert(TZ).strftime("%d %b %Y, %H:%M")
            st.write({
                "Current state in Target": act.get("state"),
                "Workspace": act.get("workspace") or "Default Workspace (the API leaves this blank for the default)",
                "Scheduled start": act.get("startsAt"), "Scheduled end": act.get("endsAt"),
                "Last modified": act.get("modifiedAt"), "Last activated": fmt(row["last_activated_at"]),
                "First went live": fmt(row["first_live_at"]), "Concluded": fmt(row["concluded_at"]),
                "Date source": row["date_source"], "Notes": row["notes"],
                **({"Jira ticket": row["jira_key"] or "Not found", "Jira status": row["jira_status"],
                    "Outcome": row["outcome"], "Matched by": row["jira_match"]} if JIRA_ON else {}),
            })
            hist = rec.get("history") or []
            if rec.get("error"):
                st.error(f"Change log fetch failed: {rec['error']}")
            st.write(f"Change log: {len(hist)} entries")
            if hist:
                from timeline.lifecycle import _dig, _entry_time, describe_state_change, unwrap
                st.dataframe(pd.DataFrame([{
                    "time": _entry_time(h), "state change": describe_state_change(h),
                    "startsAt": unwrap(_dig(h, "startsAt")), "endsAt": unwrap(_dig(h, "endsAt")),
                } for h in hist]), hide_index=True, width="stretch")
                st.caption("Raw first and last change log entries, to check the field names Target uses:")
                st.json(hist[:1] + (hist[-1:] if len(hist) > 1 else []), expanded=False)
            st.divider()

with st.expander("How the dates are worked out"):
    st.markdown(
        """
- **Started** is the most recent time the test was activated in Target. If a test was stopped and relaunched, this is the relaunch date.
- **First went live** is the first time the test was ever activated. It only differs from *Started* for relaunched tests.
- **Concluded** is when the test was last deactivated or archived, or when its scheduled end date passed. Tests that are still live have no concluded date.
- If a test was activated with a future start date, the scheduled start is used, because that is when it began serving.
- Dates come from each activity's change log in Target. Where no state history exists, the scheduled dates are used and the *Date source* column says so.
- All times are UK time.
"""
    )

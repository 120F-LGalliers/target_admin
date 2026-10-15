"""Work out when each Target activity went live and when it concluded.

Adobe Target does not store "went live" or "concluded" dates directly. We
rebuild them from two sources:

1. The activity change log (GET /target/activities/{id}/changelog). Each entry
   is a snapshot of the activity's parameters at the time it was modified, so
   comparing consecutive snapshots tells us when the state moved into or out
   of "approved" (live).
2. The activity's schedule (startsAt / endsAt). An activity can be activated
   today but scheduled to start next week, or be left live with an end date
   that passes. The schedule adjusts the dates from step 1.

If the change log is missing or has no state information, we fall back to the
schedule and mark the dates as such in the `date_source` column.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Iterable

LIVE_STATES = {"approved"}
CONCLUDED_STATES = {"deactivated", "archived", "deleted"}
PAUSED_STATES = {"paused"}

TYPE_LABELS = {
    "ab": "A/B",
    "xt": "XT",
    "abt": "Auto-Target",
    "aa": "Auto-Allocate",
    "ap": "Automated Personalisation",
    "mvt": "MVT",
    "landing": "Landing",
    "recs": "Recommendations",
}


# --------------------------------------------------------------------------
# Parsing helpers
# --------------------------------------------------------------------------

def parse_ts(value: Any) -> datetime | None:
    """Parse the timestamp formats Target returns into aware UTC datetimes."""
    if value in (None, "", 0):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        # Epoch milliseconds
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _dig(obj: Any, key: str) -> Any:
    """Find `key` in a change log entry, wherever Target nests it."""
    # Breadth-first, so a top-level value wins over a nested one. Searches up
    # to three levels deep (e.g. entry -> activityParameters -> activity).
    level = [obj]
    for _ in range(4):
        nxt = []
        for node in level:
            if not isinstance(node, dict):
                continue
            if key in node and node[key] not in (None, ""):
                return node[key]
            nxt.extend(v for v in node.values() if isinstance(v, dict))
        level = nxt
    return None


def type_label(raw_type: str | None, activity: dict | None = None) -> str:
    raw = (raw_type or "").lower()
    if raw == "ab" and activity:
        # Auto-Allocate is an A/B activity with traffic auto-allocation on
        auto = activity.get("autoAllocateTraffic") or {}
        if isinstance(auto, dict) and auto.get("enabled"):
            return "Auto-Allocate"
    return TYPE_LABELS.get(raw, raw.upper() or "Unknown")


# --------------------------------------------------------------------------
# Result model
# --------------------------------------------------------------------------

@dataclass
class LivePeriod:
    start: datetime | None
    end: datetime | None  # None means still live


@dataclass
class Lifecycle:
    activity_id: str
    name: str
    raw_type: str
    type: str
    state: str
    status: str
    workspace: str | None
    first_live_at: datetime | None
    last_activated_at: datetime | None
    concluded_at: datetime | None
    scheduled_start: datetime | None
    scheduled_end: datetime | None
    modified_at: datetime | None
    live_periods: int
    date_source: str
    notes: list[str] = field(default_factory=list)
    periods: list[LivePeriod] = field(default_factory=list)

    def as_row(self) -> dict:
        row = asdict(self)
        row.pop("periods", None)
        row["notes"] = "; ".join(self.notes)
        return row


# --------------------------------------------------------------------------
# Core logic
# --------------------------------------------------------------------------

def _state_events(changelog: Iterable[dict]) -> list[tuple[datetime, str, dict]]:
    """Return (timestamp, state, snapshot) for each change log entry that
    records a state, oldest first."""
    events = []
    for entry in changelog or []:
        ts = parse_ts(_dig(entry, "modifiedAt"))
        state = _dig(entry, "state")
        if ts is None or not state:
            continue
        events.append((ts, str(state).lower(), entry))
    events.sort(key=lambda e: e[0])
    return events


def _periods_from_events(events, now: datetime) -> list[LivePeriod]:
    """Turn a list of state snapshots into live periods, applying any schedule
    recorded in the snapshot that started each period."""
    periods: list[LivePeriod] = []
    live_since: datetime | None = None
    live_snapshot: dict | None = None

    for ts, state, snapshot in events:
        is_live = state in LIVE_STATES
        if is_live and live_since is None:
            live_since, live_snapshot = ts, snapshot
        elif is_live and live_since is not None:
            # Still live; keep the latest schedule in case it was edited
            live_snapshot = snapshot
        elif not is_live and live_since is not None:
            periods.append(_apply_schedule(live_since, ts, live_snapshot, now))
            live_since, live_snapshot = None, None

    if live_since is not None:
        periods.append(_apply_schedule(live_since, None, live_snapshot, now))

    # Drop periods that never actually ran (e.g. scheduled for the future,
    # then deactivated before the start date)
    return [p for p in periods if p.start is not None and (p.end is None or p.end > p.start)]


def _apply_schedule(activated: datetime, deactivated: datetime | None,
                    snapshot: dict | None, now: datetime) -> LivePeriod:
    sched_start = parse_ts(_dig(snapshot, "startsAt"))
    sched_end = parse_ts(_dig(snapshot, "endsAt"))

    start = max(activated, sched_start) if sched_start else activated
    end = deactivated
    if sched_end and sched_end <= now and (end is None or sched_end < end):
        end = sched_end
    if start > now:
        # Activated but the scheduled start has not arrived yet
        return LivePeriod(start=None, end=None)
    return LivePeriod(start=start, end=end)


def derive_lifecycle(activity: dict, changelog: list[dict] | None,
                     now: datetime | None = None) -> Lifecycle:
    """Build the go-live / concluded picture for one activity."""
    now = now or datetime.now(timezone.utc)
    state = str(activity.get("state") or "").lower()
    sched_start = parse_ts(activity.get("startsAt"))
    sched_end = parse_ts(activity.get("endsAt"))
    notes: list[str] = []

    events = _state_events(changelog or [])

    # The list endpoint holds the current state. If the change log's newest
    # entry disagrees (or is missing), add the current state as a final event.
    modified_at = parse_ts(activity.get("modifiedAt"))
    if events and state and events[-1][1] != state and modified_at:
        events.append((max(modified_at, events[-1][0]), state, activity))

    if events:
        source = "Change log"
        periods = _periods_from_events(events, now)
    else:
        source = "Schedule only"
        notes.append("No state history found in change log")
        periods = _periods_from_schedule(state, sched_start, sched_end, modified_at, now, notes)

    first_live = periods[0].start if periods else None
    last_activated = periods[-1].start if periods else None
    last_end = periods[-1].end if periods else None

    status = _status(state, periods, sched_start, now)
    concluded = last_end if status in {"Concluded", "Paused"} else None
    if status == "Concluded" and concluded is None and periods:
        concluded = modified_at
        notes.append("Concluded date approximated from last modified date")

    return Lifecycle(
        activity_id=str(activity.get("id")),
        name=activity.get("name") or f"Activity {activity.get('id')}",
        raw_type=str(activity.get("type") or ""),
        type=type_label(activity.get("type"), activity),
        state=state,
        status=status,
        workspace=activity.get("workspace"),
        first_live_at=first_live,
        last_activated_at=last_activated,
        concluded_at=concluded,
        scheduled_start=sched_start,
        scheduled_end=sched_end,
        modified_at=modified_at,
        live_periods=len(periods),
        date_source=source,
        notes=notes,
        periods=periods,
    )


def _periods_from_schedule(state, sched_start, sched_end, modified_at, now, notes):
    if state in LIVE_STATES:
        start = sched_start
        if start is None:
            notes.append("Go-live date unknown: activated manually with no schedule")
            return []
        if start > now:
            return []
        end = sched_end if sched_end and sched_end <= now else None
        return [LivePeriod(start=start, end=end)]
    if state in CONCLUDED_STATES | PAUSED_STATES and sched_start:
        end = sched_end if sched_end and sched_end <= now else modified_at
        if end is not None and end is modified_at:
            notes.append("End date approximated from last modified date")
        return [LivePeriod(start=sched_start, end=end)]
    return []


def _status(state: str, periods: list[LivePeriod], sched_start, now) -> str:
    if state in LIVE_STATES:
        if periods and periods[-1].end is None:
            return "Live"
        if periods and periods[-1].end is not None:
            return "Concluded"  # live state but end date passed
        if sched_start and sched_start > now:
            return "Scheduled"
        return "Live"
    if state in PAUSED_STATES:
        return "Paused" if periods else "Draft"
    if state in CONCLUDED_STATES:
        return "Concluded" if periods else "Never live"
    return "Draft" if not periods else "Concluded"


def in_range(ts: datetime | None, start: datetime, end: datetime) -> bool:
    return ts is not None and start <= ts < end

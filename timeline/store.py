"""Local cache of activities + their change logs.

The app never calls Adobe on every page view. It keeps a JSON snapshot on
disk and refreshes it when it is older than the configured age, or when
someone presses "Refresh now". On a refresh, change logs are only
re-fetched for activities whose `modifiedAt` has changed.
"""

from __future__ import annotations

import json
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import pandas as pd

from .lifecycle import derive_lifecycle


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.data = {"refreshed_at": None, "activities": {}}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text())
            except (OSError, json.JSONDecodeError):
                pass

    @property
    def refreshed_at(self) -> datetime | None:
        ts = self.data.get("refreshed_at")
        return datetime.fromisoformat(ts) if ts else None

    def age_hours(self) -> float | None:
        ts = self.refreshed_at
        if ts is None:
            return None
        return (datetime.now(timezone.utc) - ts).total_seconds() / 3600

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Write atomically so a crash mid-write never corrupts the cache
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        with os.fdopen(fd, "w") as fh:
            json.dump(self.data, fh)
        os.replace(tmp, self.path)

    def refresh(self, list_fn: Callable[[], list[dict]],
                history_fn: Callable[[str], list[dict]],
                progress: Callable[[float, str], None] | None = None,
                workers: int = 4) -> dict:
        """Pull the activity list, then change logs for new/changed activities."""
        activities = list_fn()
        cached = self.data.get("activities", {})
        todo = []
        fresh = {}
        for act in activities:
            aid = str(act.get("id"))
            prev = cached.get(aid)
            if prev and prev["activity"].get("modifiedAt") == act.get("modifiedAt") and prev.get("history") is not None:
                fresh[aid] = {**prev, "activity": act}
            else:
                todo.append(act)

        errors = {}

        def fetch(act):
            aid = str(act.get("id"))
            try:
                return aid, act, history_fn(aid), None
            except Exception as exc:  # keep going; mark this one as failed
                return aid, act, None, str(exc)

        done = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for aid, act, hist, err in pool.map(fetch, todo):
                done += 1
                if err:
                    errors[aid] = err
                fresh[aid] = {"activity": act, "history": hist, "error": err}
                if progress:
                    progress(done / max(len(todo), 1), f"Fetched history {done} of {len(todo)}")

        self.data = {
            "refreshed_at": datetime.now(timezone.utc).isoformat(),
            "activities": fresh,
        }
        self.save()
        return {"activities": len(activities), "histories_fetched": len(todo), "errors": errors}

    def lifecycles(self, now: datetime | None = None):
        for rec in self.data.get("activities", {}).values():
            yield rec, derive_lifecycle(rec["activity"], rec.get("history"), now=now)

    def periods_frame(self, now: datetime | None = None) -> pd.DataFrame:
        """One row per live period, for the calendar view. A test that was
        stopped and relaunched has several rows."""
        rows = []
        for _, lc in self.lifecycles(now):
            for i, p in enumerate(lc.periods, start=1):
                rows.append({
                    "activity_id": lc.activity_id, "name": lc.name, "type": lc.type,
                    "status": lc.status, "period": i, "periods": len(lc.periods),
                    "start": p.start, "end": p.end, "ongoing": p.end is None,
                })
        df = pd.DataFrame(rows)
        if not df.empty:
            df["start"] = pd.to_datetime(df["start"], utc=True)
            df["end"] = pd.to_datetime(df["end"], utc=True)
        return df

    def lifecycle_frame(self, now: datetime | None = None) -> pd.DataFrame:
        rows = []
        for rec, lc in self.lifecycles(now):
            if rec.get("error"):
                lc.notes.append(f"History fetch failed: {rec['error'][:120]}")
            rows.append(lc.as_row())
        df = pd.DataFrame(rows)
        if df.empty:
            return df
        for col in ("first_live_at", "last_activated_at", "concluded_at",
                    "scheduled_start", "scheduled_end", "modified_at"):
            df[col] = pd.to_datetime(df[col], utc=True)
        return df

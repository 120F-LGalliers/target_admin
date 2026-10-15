"""Sample data in the same shape as the Target API, for demoing the app
before real credentials are in place. Every name and date here is made up."""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

NAMES = [
    "PDP Clubcard Price Lockup", "Basket Upsell Accessories", "SIM Only Plan Comparison",
    "Checkout Trust Badges", "Homepage Hero Messaging", "Upgrade Eligibility Banner",
    "Phone Finder Filters", "Data Rollover Explainer", "Trade-In Calculator Placement",
    "My Account Usage Widget", "Family Perks Callout", "PAYG Top-Up Shortcut",
    "Plan Builder Step Order", "Delivery Promise Copy", "Mobile Nav Simplification",
    "Contract Length Toggle", "Roaming Add-On Prompt", "Credit Check Reassurance",
    "Refurb Phones Entry Point", "Basket Save For Later", "Tablet Plans Cross-Sell",
    "Order Summary Sticky CTA", "Network Coverage Checker", "Search Results Badges",
    "Clubcard Benefits Widget", "Upgrade Journey Progress Bar", "5G Explainer Module",
    "Device Colour Swatches", "Pre-Order Countdown", "Help Centre Search Prompt",
    "SIM Swap Journey Copy", "Bundle Savings Highlight", "Out Of Stock Alternatives",
    "Payment Page Layout", "Plan Card Hierarchy", "Exit Intent Offer",
    "Review Stars On PLP", "Insurance Add-On Default", "Postcode Lookup Speed",
    "Loyalty Price Messaging", "eSIM Eligibility Check", "Spend Cap Explainer",
]


def _iso(dt: datetime | None) -> str | None:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def build_demo(now: datetime | None = None, seed: int = 120):
    rng = random.Random(seed)
    now = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    activities, histories = [], {}

    for i, name in enumerate(NAMES):
        aid = 400000 + i * 37
        roll = rng.random()
        typ = "ab" if roll < 0.68 else "xt" if roll < 0.88 else rng.choice(["abt", "ab_aa"])
        auto_alloc = typ == "ab_aa"
        typ = "ab" if auto_alloc else typ

        created = now - timedelta(days=rng.randint(10, 200), hours=rng.randint(0, 23))
        snap = {"name": name, "type": typ, "state": "saved", "startsAt": None, "endsAt": None}
        hist = [{"modifiedAt": _iso(created), "activityParameters": dict(snap)}]
        t = created

        fate = rng.random()
        if fate < 0.08:
            pass  # still a draft
        else:
            t += timedelta(days=rng.randint(2, 20), hours=rng.randint(1, 8))
            if t > now:
                t = now - timedelta(hours=2)
            scheduled = rng.random() < 0.25
            if scheduled:
                snap["startsAt"] = _iso(t + timedelta(days=rng.randint(1, 4)))
            snap["state"] = "approved"
            hist.append({"modifiedAt": _iso(t), "activityParameters": dict(snap)})

            # Some tests get paused and relaunched
            if rng.random() < 0.15:
                stop = t + timedelta(days=rng.randint(3, 10))
                if stop < now - timedelta(days=3):
                    snap["state"] = "deactivated"
                    hist.append({"modifiedAt": _iso(stop), "activityParameters": dict(snap)})
                    t = stop + timedelta(days=rng.randint(1, 5))
                    snap["state"], snap["startsAt"] = "approved", None
                    hist.append({"modifiedAt": _iso(t), "activityParameters": dict(snap)})

            run_days = rng.randint(14, 42)
            end = t + timedelta(days=run_days)
            if end < now and fate > 0.3:
                if rng.random() < 0.2:
                    # Ended by schedule, left in approved state
                    snap["endsAt"] = _iso(end)
                    hist.append({"modifiedAt": _iso(t + timedelta(hours=3)), "activityParameters": dict(snap)})
                else:
                    snap["state"] = "deactivated" if rng.random() < 0.7 else "archived"
                    hist.append({"modifiedAt": _iso(end), "activityParameters": dict(snap)})

        activity = {
            "id": aid,
            "name": name,
            "type": typ,
            "state": snap["state"],
            "priority": 0,
            "workspace": "Tesco Mobile",
            "startsAt": snap["startsAt"],
            "endsAt": snap["endsAt"],
            "modifiedAt": hist[-1]["modifiedAt"],
        }
        if auto_alloc:
            activity["autoAllocateTraffic"] = {"enabled": True}
        activities.append(activity)
        histories[str(aid)] = hist

    return activities, histories

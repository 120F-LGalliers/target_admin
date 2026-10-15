from datetime import datetime, timezone

from timeline.lifecycle import derive_lifecycle

NOW = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)


def snap(ts, state, **extra):
    return {"modifiedAt": ts, "activityParameters": {"state": state, **extra}}


def act(state, **extra):
    return {"id": 1, "name": "Test", "type": "ab", "state": state, **extra}


def test_simple_run_and_conclude():
    hist = [snap("2026-09-01T09:00:00Z", "saved"),
            snap("2026-09-05T10:00:00Z", "approved"),
            snap("2026-09-30T16:00:00Z", "deactivated")]
    lc = derive_lifecycle(act("deactivated", modifiedAt="2026-09-30T16:00:00Z"), hist, NOW)
    assert lc.status == "Concluded"
    assert lc.first_live_at == lc.last_activated_at == datetime(2026, 9, 5, 10, tzinfo=timezone.utc)
    assert lc.concluded_at == datetime(2026, 9, 30, 16, tzinfo=timezone.utc)
    assert lc.date_source == "Change log"


def test_relaunch_uses_latest_activation():
    hist = [snap("2026-08-01T09:00:00Z", "approved"),
            snap("2026-08-10T09:00:00Z", "deactivated"),
            snap("2026-09-01T09:00:00Z", "approved")]
    lc = derive_lifecycle(act("approved"), hist, NOW)
    assert lc.status == "Live"
    assert lc.first_live_at.month == 8 and lc.last_activated_at.month == 9
    assert lc.live_periods == 2
    assert lc.concluded_at is None


def test_scheduled_start_moves_go_live():
    hist = [snap("2026-09-01T09:00:00Z", "approved", startsAt="2026-09-04T00:00:00Z")]
    lc = derive_lifecycle(act("approved"), hist, NOW)
    assert lc.last_activated_at == datetime(2026, 9, 4, tzinfo=timezone.utc)


def test_future_schedule_is_scheduled_not_live():
    hist = [snap("2026-10-14T09:00:00Z", "approved", startsAt="2026-10-20T00:00:00Z")]
    lc = derive_lifecycle(act("approved", startsAt="2026-10-20T00:00:00Z"), hist, NOW)
    assert lc.status == "Scheduled"
    assert lc.last_activated_at is None


def test_end_date_passed_while_approved_counts_as_concluded():
    hist = [snap("2026-09-01T09:00:00Z", "approved", endsAt="2026-09-20T00:00:00Z")]
    lc = derive_lifecycle(act("approved", endsAt="2026-09-20T00:00:00Z"), hist, NOW)
    assert lc.status == "Concluded"
    assert lc.concluded_at == datetime(2026, 9, 20, tzinfo=timezone.utc)


def test_never_live_draft():
    lc = derive_lifecycle(act("saved"), [snap("2026-09-01T09:00:00Z", "saved")], NOW)
    assert lc.status == "Draft" and lc.first_live_at is None


def test_schedule_fallback_without_history():
    lc = derive_lifecycle(act("approved", startsAt="2026-09-01T00:00:00Z"), [], NOW)
    assert lc.date_source == "Schedule only"
    assert lc.last_activated_at == datetime(2026, 9, 1, tzinfo=timezone.utc)


def test_current_state_newer_than_changelog():
    hist = [snap("2026-09-01T09:00:00Z", "approved")]
    lc = derive_lifecycle(act("deactivated", modifiedAt="2026-09-25T09:00:00Z"), hist, NOW)
    assert lc.status == "Concluded"
    assert lc.concluded_at == datetime(2026, 9, 25, 9, tzinfo=timezone.utc)


def test_auto_allocate_label():
    lc = derive_lifecycle({**act("saved"), "autoAllocateTraffic": {"enabled": True}}, [], NOW)
    assert lc.type == "Auto-Allocate"

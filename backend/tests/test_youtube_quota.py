from datetime import datetime, timedelta, timezone

import pytest

from app.platforms.youtube.quota import COSTS, QuotaTracker


class Clock:
    def __init__(self, dt: datetime) -> None:
        self.dt = dt

    def __call__(self) -> datetime:
        return self.dt


def tracker(dt=None, **kw) -> tuple[QuotaTracker, Clock]:
    clock = Clock(dt or datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc))
    return QuotaTracker(clock=clock, **kw), clock


def test_documented_costs():
    assert COSTS["liveChatMessages.list"] == 5 and COSTS["liveChatMessages.insert"] == 20
    assert COSTS["liveBroadcasts.list"] == COSTS["videos.list"] == COSTS["channels.list"] == 1


def test_recording_and_remaining():
    q, _ = tracker()
    q.record("liveChatMessages.list")
    q.record("liveChatMessages.insert")
    q.record("channels.list")
    assert q.used == 26 and q.remaining == 9974 and not q.exhausted


def test_reads_and_sends_are_tracked_separately():
    q, _ = tracker()
    q.record("liveChatMessages.list")
    q.record("channels.list")
    q.record("liveChatMessages.insert")
    assert q.used == 26 and q.reads_used == 6
    restored, _ = tracker()
    assert restored.load(q.dump()) and restored.reads_used == 6
    legacy, _ = tracker()
    assert legacy.load({"date": q.dump()["date"], "used": 100}) and legacy.reads_used == 100  # old saves: conservative


def test_sends_stop_before_reads_to_protect_chat_reading():
    q, _ = tracker(daily_limit=1000, send_reserve_fraction=0.10)  # reserve 100 units for reading
    q._used = 880
    assert q.can_send()  # 1000 - 880 - 20 = 100 >= 100
    q._used = 881
    assert not q.can_send()
    assert q.can_read("liveChatMessages.list")  # reading is still allowed
    q._used = 996
    assert not q.can_read("liveChatMessages.list") and q.can_read("channels.list")


def test_server_side_quota_exceeded_always_wins():
    q, _ = tracker()
    q.mark_exhausted()
    assert q.exhausted and q.remaining == 0
    assert not q.can_read("channels.list") and not q.can_send()


def test_quota_resets_at_midnight_pacific_standard_time():
    q, clock = tracker(datetime(2026, 11, 20, 7, 30, tzinfo=timezone.utc))  # 23:30 PST on Nov 19
    q.record("liveChatMessages.insert")
    assert q.used == 20 and q.seconds_until_reset() == pytest.approx(1800)
    clock.dt += timedelta(minutes=40)  # 00:10 PST on Nov 20: a new quota day
    assert q.used == 0 and not q.exhausted


def test_quota_resets_at_midnight_pacific_daylight_time():
    q, clock = tracker(datetime(2026, 7, 1, 6, 59, tzinfo=timezone.utc))  # 23:59 PDT on Jun 30
    q.record("liveChatMessages.list")
    assert q.used == 5
    clock.dt += timedelta(minutes=2)  # 00:01 PDT on Jul 1
    assert q.used == 0


def test_exhaustion_is_cleared_by_the_daily_reset():
    q, clock = tracker(datetime(2026, 11, 20, 7, 30, tzinfo=timezone.utc))
    q.mark_exhausted()
    clock.dt += timedelta(hours=1)
    assert not q.exhausted and q.can_read("liveChatMessages.list")


def test_search_list_has_its_own_bucket():
    q, _ = tracker()
    for _ in range(100):
        q.record("search.list")
    assert q.used == 0 and not q.can_read("search.list") and q.can_read("liveChatMessages.list")


def test_persistence_only_restores_todays_count():
    q, clock = tracker()
    q.record("liveChatMessages.insert")
    saved = q.dump()
    fresh, fclock = tracker()
    assert fresh.load(saved) and fresh.used == 20
    tomorrow, _ = tracker(clock.dt + timedelta(days=1))
    assert not tomorrow.load(saved) and tomorrow.used == 0
    for junk in ({}, {"date": "nonsense"}, None):
        assert not fresh.load(junk)  # type: ignore[arg-type]


def test_snapshot_shape():
    q, _ = tracker()
    snap = q.snapshot()
    assert snap["limit"] == 10_000 and snap["remaining"] == 10_000 and snap["resets_in_s"] > 0

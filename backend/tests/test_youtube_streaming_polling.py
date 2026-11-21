import json

import pytest

from app.platforms.youtube.polling import PollingGovernor, PollingPlan
from app.platforms.youtube.quota import QuotaTracker
from app.platforms.youtube.streaming import JsonStreamDecoder, StreamFramingError
from tests.conftest import FakeClock

DOCS = [{"items": [{"id": "a", "text": 'quote " ] , } and \\ backslash'}], "nextPageToken": "n1"},
        {"items": [], "nextPageToken": "n2"}, {"items": [{"id": "ü€😀"}], "nextPageToken": "n3"}]


def feed_in_chunks(payload: str, size: int) -> list:
    dec, out = JsonStreamDecoder(), []
    for i in range(0, len(payload), size):
        out += dec.feed(payload[i:i + size])
    return out


@pytest.mark.parametrize("payload", [
    json.dumps(DOCS),                                   # one JSON array
    json.dumps(DOCS, indent=2),                         # pretty printed array
    "[\n" + ",\n".join(json.dumps(d) for d in DOCS) + "\n]",
    "\n".join(json.dumps(d) for d in DOCS),             # newline-delimited JSON
    "".join(json.dumps(d) for d in DOCS),               # concatenated objects
])
def test_every_plausible_framing_decodes_for_every_chunk_size(payload):
    for size in (1, 2, 3, 7, 16, 100, len(payload)):
        assert feed_in_chunks(payload, size) == DOCS, size


def test_array_delivered_incrementally_yields_documents_as_they_complete():
    dec = JsonStreamDecoder()
    assert dec.feed('[{"a": 1},') == [{"a": 1}]
    assert dec.feed('{"b": 2') == []
    assert dec.feed("}") == [{"b": 2}]
    assert dec.feed("]") == []


def test_garbage_and_oversized_buffers_fail_loudly():
    with pytest.raises(StreamFramingError):
        JsonStreamDecoder().feed("<html>not json</html>")
    dec = JsonStreamDecoder()
    with pytest.raises(StreamFramingError):
        dec.feed('{"x": "' + "a" * (9 * 1024 * 1024))


# ---------------------------------------------------------------------------- polling governor
def gov(quota=None, plan=None, clock=None):
    clock = clock or FakeClock()
    q = quota or QuotaTracker()
    g = PollingGovernor(q, plan or PollingPlan(), clock)
    g.start_session()
    return g, q, clock


SHORT = PollingPlan(planned_session_s=600)  # a short plan, so budget pacing does not bind


def test_activity_tiers():
    g, _, _ = gov(plan=SHORT)
    assert g.next_interval(5, 30) == 10     # someone wrote 30 s ago
    assert g.next_interval(5, 300) == 30    # a few minutes quiet
    assert g.next_interval(5, 5000) == 60   # nobody around
    assert g.next_interval(5, None) == 60   # no message seen yet


def test_a_six_hour_plan_on_the_default_quota_cannot_poll_faster_than_21_6_seconds():
    """Reading gets 50% of 10,000 units (10% final reserve + 40% kept for sending) = 5,000 units = 1,000 polls;
    21,600 s / 1,000 = 21.6 s. A flat 10 s poll would cost 10,800 units and exhaust the day's quota before the bot
    sent a single message; even 12 s would leave only ~24 messages' worth of quota."""
    g, _, _ = gov()
    assert g.next_interval(5, 1) == pytest.approx(21.6)


def test_polling_can_never_starve_sending():
    """Even if polling has used its whole share, the send budget is untouched: reads are slowed to the maximum."""
    plan = PollingPlan(planned_session_s=3600, max_interval_s=600)
    g, q, _ = gov(QuotaTracker(daily_limit=10_000), plan)
    q._used = q._read_used = 5_000  # reading has used its entire 50% share
    assert q.remaining == 5_000 and g.next_interval(5, 1) == 600
    assert q.can_send()  # ... and 5,000 units are still there for messages


def test_never_faster_than_youtube_asks():
    g, _, _ = gov(plan=SHORT)
    assert g.next_interval(25, 1) == 25


def test_budget_pacing_slows_polling_when_quota_runs_short():
    plan = PollingPlan(planned_session_s=3600, max_interval_s=600)
    g, q, _ = gov(QuotaTracker(daily_limit=10_000), plan)
    q._used = q._read_used = 4_900  # reading share: 5,000 - 4,900 = 100 units left => 20 polls over the hour => 180 s
    assert g.next_interval(5, 1) == pytest.approx(180.0)
    q._used = q._read_used = 5_000  # reading share used up: slowest pace
    assert g.next_interval(5, 1) == 600


def test_overall_quota_safety_applies_even_if_the_reading_share_is_unspent():
    g, q, _ = gov(QuotaTracker(daily_limit=10_000), PollingPlan(planned_session_s=3600, max_interval_s=600))
    q._used, q._read_used = 8_900, 0  # a very chatty bot sent a lot; only 100 units are spendable above the 10% reserve
    assert g.next_interval(5, 1) == pytest.approx(180.0)


def test_pacing_relaxes_as_the_planned_session_ends():
    g, q, clock = gov(QuotaTracker(daily_limit=10_000), PollingPlan(planned_session_s=3600, max_interval_s=600))
    q._used = q._read_used = 4_900
    early = g.next_interval(5, 1)
    clock.t += 3500  # almost at the planned end: only 100 s left to cover with 20 polls
    late = g.next_interval(5, 1)
    assert early == pytest.approx(180.0) and late == pytest.approx(10.0)


def test_pacing_is_off_before_a_session_starts():
    q = QuotaTracker()
    q._used = 9_990
    g = PollingGovernor(q, PollingPlan(), FakeClock())
    assert g.next_interval(5, 1) == 10

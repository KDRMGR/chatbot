import random

import pytest

from app.platforms.base import ConnectionState
from app.platforms.youtube.adapter import Backoff, YouTubeAdapter
from app.platforms.youtube.api import (
    BadRequest, BroadcastInfo, ChatPage, Forbidden, LiveChatEnded, NetworkError, QuotaExceeded, RateLimited, ServerError,
)
from app.platforms.youtube.credentials import NotAuthorized, ReauthRequired
from app.platforms.youtube.quota import QuotaTracker
from tests.youtube_fakes import FakeApi, Sleeper, chat_item, eventually


class Rig:
    def __init__(self, make_settings, **overrides) -> None:
        self.settings = make_settings(youtube_enabled=True, **overrides)
        self.api, self.quota, self.sleeper = FakeApi(), QuotaTracker(), Sleeper()
        self.received, self.events, self.saved = [], [], []

        async def log(kind: str, message: str) -> None:
            self.events.append((kind, message))

        async def save(state: dict) -> None:
            self.saved.append(state)

        self.adapter = YouTubeAdapter(self.settings, self.api, self.quota, log_event=log, save_quota=save,
                                      sleep=self.sleeper, rng=random.Random(7))

    async def start(self) -> None:
        async def on_message(message) -> None:
            self.received.append(message)

        await self.adapter.start(on_message)

    @property
    def state(self) -> ConnectionState:
        return self.adapter.status().state

    async def until_state(self, state: ConnectionState) -> None:
        await eventually(lambda: self.state is state)

    def ids(self) -> list[str]:
        return [m.message_id for m in self.received]

    def list_calls(self) -> list[tuple]:
        return [a for n, a in self.api.calls if n == "list_messages"]


@pytest.fixture
async def make_rig(make_settings):
    rigs: list[Rig] = []

    def _make(**overrides) -> Rig:
        rig = Rig(make_settings, **overrides)
        rigs.append(rig)
        return rig

    yield _make
    for rig in rigs:
        await rig.adapter.stop()


def page(items, token="t", hint=0.05, **kw) -> ChatPage:
    return ChatPage(items=items, next_page_token=token, polling_interval_s=hint, **kw)


# ---------------------------------------------------------------------------- reading chat
async def test_connects_skips_the_backlog_and_delivers_new_messages(make_rig):
    rig = make_rig()
    rig.api.script["list_messages"] = [
        page([chat_item("old1"), chat_item("old2")], "t1"),
        page([chat_item("new1", "hello everyone"), chat_item("own1", "my reply", channel="UC_BOT")], "t2"),
    ]
    await rig.start()
    await eventually(lambda: len(rig.received) >= 2)
    assert rig.ids() == ["new1", "own1"]  # the history was NOT delivered
    assert [m.is_bot_self for m in rig.received] == [False, True]
    assert rig.list_calls()[:2] == [("CHAT1", None), ("CHAT1", "t1")]  # the next poll resumes from the token
    assert rig.state is ConnectionState.CONNECTED
    assert rig.adapter.details()["channel"] == "StealthBot" and rig.adapter.details()["live_chat_id"] == "CHAT1"
    assert any(kind == "connected" for kind, _ in rig.events) and rig.saved  # logged + quota persisted


async def test_retarget_moves_the_adapter_to_the_chosen_live_video_and_back(make_rig):
    """The stream director picks a video from the watch list: the adapter must reconnect to THAT chat, then return to
    its normal discovery when told to."""
    rig = make_rig()
    rig.api.script["list_messages"] = [page([], "t1")]
    await rig.start()
    await rig.until_state(ConnectionState.CONNECTED)
    assert rig.adapter.details()["video_id"] == "VID1" and rig.adapter.details()["video_override"] is None
    rig.api.script["video_chat_id"] = ["CHAT-OTHER"]
    await rig.adapter.retarget("OTHERVIDEO")
    await eventually(lambda: rig.adapter.details()["live_chat_id"] == "CHAT-OTHER")
    assert rig.adapter.details()["video_id"] == "OTHERVIDEO" and rig.adapter.details()["video_override"] == "OTHERVIDEO"
    assert ("video_chat_id", "OTHERVIDEO") in rig.api.calls
    discovered_before = rig.api.count("active_broadcast")
    await rig.adapter.retarget(None)  # back to the signed-in account's own stream
    await eventually(lambda: rig.adapter.details()["live_chat_id"] == "CHAT1")
    assert rig.api.count("active_broadcast") > discovered_before and rig.adapter.details()["video_override"] is None


async def test_retarget_to_a_video_that_is_not_live_waits_instead_of_failing(make_rig):
    rig = make_rig()
    rig.api.script["video_chat_id"] = [None, None, None, None, None, None, None, None]  # not live (no chat id)
    await rig.adapter.retarget("NOTLIVE")  # before start: only remembered
    await rig.start()
    await eventually(lambda: ("video_chat_id", "NOTLIVE") in rig.api.calls)
    assert rig.state is ConnectionState.DISCONNECTED and "waiting for a live broadcast" in (rig.adapter.status().detail or "")
    assert rig.api.count("active_broadcast") == 0  # it did not fall back to something else on its own


async def test_retarget_does_not_start_an_adapter_the_streamer_stopped(make_rig):
    rig = make_rig()
    await rig.start()
    await rig.until_state(ConnectionState.CONNECTED)
    await rig.adapter.stop()  # the Disconnect button
    await rig.adapter.retarget("SOMEVIDEO")
    assert rig.state is ConnectionState.DISCONNECTED and rig.adapter.details()["video_override"] == "SOMEVIDEO"
    assert rig.adapter._task is None  # still stopped: only the streamer's Connect starts it again


async def test_the_same_message_is_delivered_only_once(make_rig):
    rig = make_rig()
    rig.api.script["list_messages"] = [page([chat_item("old")], "t1"), page([chat_item("dup")], "t2"),
                                       page([chat_item("dup"), chat_item("next")], "t3")]
    await rig.start()
    await eventually(lambda: "next" in rig.ids())
    assert rig.ids() == ["dup", "next"]


async def test_all_event_types_arrive_normalised(make_rig):
    rig = make_rig()
    rig.api.script["list_messages"] = [page([], "t1"), page([
        chat_item("a", "great", channel="UC2"),
        chat_item("b", "", channel="UC3", kind="superChatEvent", superChatDetails={"amountDisplayString": "$5"}),
        chat_item("c", "", channel="UC4", kind="newSponsorEvent"), chat_item("d", kind="userBannedEvent"),
    ], "t2")]
    await rig.start()
    await eventually(lambda: len(rig.received) >= 3)
    assert [m.event_type.value for m in rig.received] == ["message", "donation", "membership"]


async def test_resumes_from_the_last_token_after_a_network_error_without_duplicates(make_rig):
    rig = make_rig()
    rig.api.script["list_messages"] = [page([chat_item("old")], "t1"), NetworkError("timeout"),
                                       page([chat_item("after")], "t2")]
    await rig.start()
    await eventually(lambda: "after" in rig.ids())
    assert rig.list_calls()[:3] == [("CHAT1", None), ("CHAT1", "t1"), ("CHAT1", "t1")]  # retry resumes at t1
    assert rig.ids() == ["after"]  # a resumed read is not treated as history
    backoffs = [d for d in rig.sleeper.delays if d < 5]  # poll-pacing sleeps are >= 10 s; this is the backoff step
    assert len(backoffs) == 1 and 1.6 <= backoffs[0] <= 2.0  # first backoff step (2 s minus jitter)
    assert rig.adapter.stats["reconnects"] == 1 and any(kind == "error" for kind, _ in rig.events)


async def test_backoff_grows_exponentially_and_is_capped(make_rig):
    rig = make_rig()
    rig.api.script["list_messages"] = [ServerError(503, "backendError")] * 9
    await rig.start()
    await eventually(lambda: len(rig.sleeper.delays) >= 9)
    for n, delay in enumerate(rig.sleeper.delays[:9]):
        ceiling = min(2.0 * 2 ** n, 60.0)
        assert 0.8 * ceiling <= delay <= ceiling, (n, delay)
    assert max(rig.sleeper.delays[:9]) <= 60.0


def test_backoff_resets_and_jitter_never_exceeds_the_cap():
    b = Backoff(rng=random.Random(3))
    first = [b.next() for _ in range(10)]
    assert all(d <= 60 for d in first) and first[5] > first[1]
    b.reset()
    assert b.next() <= 2.0


async def test_one_unexpected_exception_does_not_kill_the_adapter(make_rig):
    rig = make_rig()
    rig.api.script["list_messages"] = [page([], "t1"), RuntimeError("boom"), page([chat_item("later")], "t2")]
    await rig.start()
    await eventually(lambda: "later" in rig.ids())
    assert rig.state is ConnectionState.CONNECTED


async def test_a_failing_message_handler_does_not_stop_reading(make_rig):
    rig = make_rig()
    calls = []

    async def flaky(message) -> None:
        calls.append(message.message_id)
        if message.message_id == "bad":
            raise RuntimeError("handler exploded")

    rig.api.script["list_messages"] = [page([], "t1"), page([chat_item("bad"), chat_item("good")], "t2")]
    await rig.adapter.start(flaky)
    await eventually(lambda: "good" in calls)
    assert calls == ["bad", "good"]


# ---------------------------------------------------------------------------- chat lifecycle
async def test_chat_ended_event_disconnects_and_the_next_broadcast_is_picked_up(make_rig):
    rig = make_rig()
    rig.api.script["active_broadcast"] = [BroadcastInfo("VID1", "CHAT1"), None, BroadcastInfo("VID2", "CHAT2")]
    rig.api.script["list_messages"] = [page([chat_item("o1")], "t1"), page([chat_item("end", kind="chatEndedEvent")], "t2"),
                                       page([chat_item("oldB")], "b1"), page([chat_item("newB", "hi chat")], "b2")]
    await rig.start()
    await eventually(lambda: "newB" in rig.ids())
    assert rig.ids() == ["newB"]  # CHAT2's history was skipped too
    assert ("CHAT2", None) in rig.list_calls() and ("CHAT2", "b1") in rig.list_calls()
    assert any(kind == "disconnected" and "ended" in msg for kind, msg in rig.events)
    assert 60.0 in rig.sleeper.delays  # waited the discovery interval, did not hammer
    assert rig.adapter.details()["live_chat_id"] == "CHAT2" and rig.adapter.details()["video_id"] == "VID2"


async def test_live_chat_ended_error_is_handled_like_the_event(make_rig):
    rig = make_rig()
    rig.api.script["list_messages"] = [LiveChatEnded(403, "liveChatEnded")]
    await rig.start()
    await eventually(lambda: any(kind == "disconnected" and msg == "live chat ended" for kind, msg in rig.events))
    assert 60.0 in rig.sleeper.delays  # then waits before looking for the next broadcast


async def test_waits_for_a_broadcast_without_hammering_the_api(make_rig):
    rig = make_rig()
    rig.api.script["active_broadcast"] = [None, None, BroadcastInfo("VID1", "CHAT1")]
    await rig.start()
    await rig.until_state(ConnectionState.CONNECTED)
    assert rig.sleeper.delays[:2] == [60.0, 60.0]
    assert any("waiting for a live broadcast" in msg for _, msg in rig.events)


async def test_an_offline_stream_ends_the_session_and_rediscovers(make_rig):
    rig = make_rig()
    rig.api.script["list_messages"] = [page([], "t1", offline_at="2026-11-20T11:00:00Z")]
    await rig.start()
    await eventually(lambda: 60.0 in rig.sleeper.delays)
    assert rig.api.count("active_broadcast") >= 1


# ---------------------------------------------------------------------------- discovery order
async def test_explicit_live_chat_id_skips_discovery(make_rig):
    rig = make_rig(youtube_live_chat_id="CHATX")
    await rig.start()
    await rig.until_state(ConnectionState.CONNECTED)
    assert rig.api.count("active_broadcast") == 0 and rig.api.count("video_chat_id") == 0
    assert rig.list_calls()[0] == ("CHATX", None)


async def test_video_id_is_resolved_to_its_chat(make_rig):
    rig = make_rig(youtube_video_id="V9")
    await rig.start()
    await rig.until_state(ConnectionState.CONNECTED)
    assert ("video_chat_id", "V9") in rig.api.calls and rig.api.count("active_broadcast") == 0
    assert rig.list_calls()[0][0] == "CHAT-FROM-VIDEO"


async def test_streamer_channel_search_is_the_last_resort(make_rig):
    rig = make_rig(youtube_streamer_channel_id="UC_STREAMER")
    rig.api.script["active_broadcast"] = [None]
    await rig.start()
    await rig.until_state(ConnectionState.CONNECTED)
    assert ("find_live_video", "UC_STREAMER") in rig.api.calls and ("video_chat_id", "VID-FOUND") in rig.api.calls


# ---------------------------------------------------------------------------- auth and quota
@pytest.mark.parametrize("exc", [NotAuthorized("no credentials"), ReauthRequired("invalid_grant")])
async def test_authorisation_problems_stop_api_traffic_and_say_how_to_fix_it(make_rig, exc):
    rig = make_rig()
    rig.api.script["own_channel"] = [exc] * 50
    await rig.start()
    await eventually(lambda: len(rig.sleeper.delays) >= 3)
    assert rig.state is ConnectionState.ERROR and "youtube_login" in (rig.adapter.status().detail or "")
    assert set(rig.sleeper.delays) == {300}  # re-checks every 5 minutes, never in a tight loop


async def test_exhausted_quota_waits_for_the_reset_and_makes_no_calls(make_rig):
    rig = make_rig()
    rig.quota.mark_exhausted()
    await rig.start()
    await eventually(lambda: len(rig.sleeper.delays) >= 2)
    assert rig.state is ConnectionState.ERROR and "quota" in (rig.adapter.status().detail or "")
    assert rig.api.calls == [] and all(60 <= d <= 600 for d in rig.sleeper.delays)


async def test_quota_error_from_google_pauses_the_adapter(make_rig):
    rig = make_rig()
    rig.api.script["list_messages"] = [QuotaExceeded(403, "quotaExceeded")]
    await rig.start()
    await eventually(lambda: any(kind == "error" and "quota" in msg for kind, msg in rig.events))


async def test_polling_is_paced_by_the_quota_governor(make_rig):
    rig = make_rig()
    rig.api.script["list_messages"] = [page([], "t1", hint=0.05), page([chat_item("m1")], "t2", hint=0.05)]
    await rig.start()
    await eventually(lambda: len(rig.sleeper.delays) >= 3)
    assert all(d >= 10.0 for d in rig.sleeper.delays)  # never the 50 ms YouTube hint: the 6 h plan floors it at ~12 s


# ---------------------------------------------------------------------------- sending
async def test_send_requires_a_live_connection(make_rig):
    rig = make_rig()
    result = await rig.adapter.send("hello")
    assert not result.ok and result.retryable and "not connected" in result.error
    assert rig.api.count("insert_message") == 0


async def test_send_success_returns_the_platform_message_id(make_rig):
    rig = make_rig()
    await rig.start()
    await rig.until_state(ConnectionState.CONNECTED)
    result = await rig.adapter.send("hello chat")
    assert result.ok and result.platform_message_id == "MSG-1"
    assert ("insert_message", ("CHAT1", "hello chat")) in rig.api.calls and rig.adapter.stats["sends_ok"] == 1


async def test_very_long_message_ids_are_hashed_consistently_with_the_echo(make_rig):
    rig = make_rig()
    rig.api.script["insert_message"] = ["LCC." + "z" * 300]
    await rig.start()
    await rig.until_state(ConnectionState.CONNECTED)
    result = await rig.adapter.send("hi there")
    assert result.ok and len(result.platform_message_id) <= 128 and result.platform_message_id.startswith("h:")


async def test_send_refuses_when_it_would_eat_the_quota_reserved_for_reading(make_rig):
    rig = make_rig()
    await rig.start()
    await rig.until_state(ConnectionState.CONNECTED)
    rig.quota._used = 9_600  # reserve is 5% = 500 units: 10,000 - 9,600 - 20 = 380 < 500
    result = await rig.adapter.send("hello")
    assert not result.ok and not result.retryable and "reserved" in result.error
    assert rig.api.count("insert_message") == 0


@pytest.mark.parametrize("exc,retryable", [
    (RateLimited(403, "rateLimitExceeded"), True), (NetworkError("timeout"), True), (ServerError(503, "x"), True),
    (QuotaExceeded(403, "quotaExceeded"), False), (BadRequest(400, "messageTextInvalid"), False),
    (Forbidden(403, "forbidden"), False), (NotAuthorized("x"), False),
])
async def test_send_failures_are_reported_with_retryability(make_rig, exc, retryable):
    rig = make_rig()
    rig.api.script["insert_message"] = [exc]
    await rig.start()
    await rig.until_state(ConnectionState.CONNECTED)
    result = await rig.adapter.send("hello")
    assert not result.ok and result.retryable is retryable and result.error
    assert rig.adapter.stats["sends_failed"] == 1


async def test_send_to_an_ended_chat_disconnects(make_rig):
    rig = make_rig()
    rig.api.script["insert_message"] = [LiveChatEnded(403, "liveChatEnded")]
    await rig.start()
    await rig.until_state(ConnectionState.CONNECTED)
    result = await rig.adapter.send("hello")
    assert not result.ok and result.error == "liveChatEnded" and not result.retryable
    assert rig.adapter.details()["live_chat_id"] is None


# ---------------------------------------------------------------------------- experimental streaming transport
async def test_streaming_transport_delivers_and_falls_back_when_connections_keep_ending(make_rig):
    rig = make_rig(youtube_use_streaming=True)
    rig.api.stream_pages = [[page([chat_item("old")], "s1"), page([chat_item("new", "hello chat")], "s2")]]
    await rig.start()
    await eventually(lambda: "new" in rig.ids())
    assert rig.ids() == ["new"] and rig.api.count("stream_messages") >= 1
    await eventually(lambda: rig.adapter.details()["transport"] == "polling")  # 3 quick ends => give up on streaming
    assert rig.api.count("stream_messages") >= 3
    await eventually(lambda: rig.api.count("list_messages") >= 1)


async def test_unusable_stream_falls_back_to_polling_immediately(make_rig):
    rig = make_rig(youtube_use_streaming=True)
    rig.api.stream_pages = [[BadRequest(400, "streamFraming")]]
    await rig.start()
    await eventually(lambda: rig.api.count("list_messages") >= 1)
    assert rig.adapter.details()["transport"] == "polling" and rig.api.count("stream_messages") == 1


# ---------------------------------------------------------------------------- shutdown
async def test_stop_cancels_cleanly_and_marks_disconnected(make_rig):
    rig = make_rig()
    await rig.start()
    await rig.until_state(ConnectionState.CONNECTED)
    await rig.adapter.stop()
    assert rig.state is ConnectionState.DISCONNECTED and rig.adapter.status().detail == "stopped"
    assert rig.adapter._task is None and not rig.adapter._bg
    assert rig.events[-1] == ("disconnected", "stopped")

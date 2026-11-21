"""The stream director: watch list, live detection, the fail-safe choice, adapter control, API and saving."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from app.platforms.base import ConnectionState, ConnectionStatus
from app.streams.choose import DOWN_PROBES, Live, TargetState, choose, observe
from app.streams.director import StreamDirector
from app.streams.model import (
    MAX_TARGETS, StreamsConfig, StreamsError, StreamTarget, parse_youtube_ref,
)

CH_A = "UC" + "a" * 22
CH_B = "UC" + "b" * 22
VID_1, VID_2 = "v" * 11, "w" * 11


def target(tid: str, platform: str = "youtube", kind: str = "own", ref: str = "", priority: int = 50, enabled: bool = True):
    return StreamTarget(tid, platform, kind, ref, tid, priority, enabled)


def cfg_with(*targets: StreamTarget, pinned: str | None = None, **kw: Any) -> StreamsConfig:
    return StreamsConfig(targets=list(targets), pinned=pinned, **kw)


def live_states(**flags: bool) -> dict[str, TargetState]:
    return {tid: TargetState(live=up, offline_since=None if up else 0.0) for tid, up in flags.items()}


# ---------------------------------------------------------------------------- pasted links
@pytest.mark.parametrize("text,expected", [
    (VID_1, ("video", VID_1)),
    (CH_A, ("channel", CH_A)),
    (f"https://www.youtube.com/watch?v={VID_1}", ("video", VID_1)),
    (f"https://www.youtube.com/watch?v={VID_1}&t=5s", ("video", VID_1)),
    (f"youtu.be/{VID_1}?si=abc", ("video", VID_1)),
    (f"https://www.youtube.com/live/{VID_1}?feature=share", ("video", VID_1)),
    (f"https://m.youtube.com/channel/{CH_A}", ("channel", CH_A)),
    (f"  https://youtube.com/channel/{CH_A}/live  ", ("channel", CH_A)),
])
def test_pasted_youtube_links_are_understood(text, expected):
    assert parse_youtube_ref(text) == expected


@pytest.mark.parametrize("text,fragment", [
    ("", "paste"), ("@StealthModeGG", "handles"), ("https://www.youtube.com/@StealthModeGG", "handles"),
    ("https://example.com/watch?v=" + VID_1, "not a YouTube"), ("https://www.youtube.com/playlist?list=x", "not a video"),
    ("short", "not a valid"), (VID_1 + "x", "not a valid"), ("UC" + "a" * 5, "not a valid"),
])
def test_bad_youtube_references_are_refused_with_a_reason(text, fragment):
    with pytest.raises(StreamsError) as exc:
        parse_youtube_ref(text)
    assert fragment in str(exc.value)


# ---------------------------------------------------------------------------- saved config
def test_saved_config_round_trips_and_ignores_garbage():
    cfg = cfg_with(target("youtube-own"), target("yt-vi-1", kind="video", ref=VID_1, priority=80), pinned="yt-vi-1",
                   auto_select=False, failover_after_s=30)
    again = StreamsConfig.from_saved(json.loads(json.dumps(cfg.to_saved())))
    assert again.to_saved() == cfg.to_saved()

    messy = {"targets": [{"id": "ok-one", "platform": "youtube", "kind": "video", "ref": VID_1},
                         {"id": "bad", "platform": "twitch", "kind": "own"}, "junk", None,
                         {"id": "ok-one", "platform": "youtube", "kind": "video", "ref": VID_2},  # duplicate id
                         {"id": "bad-ref", "platform": "youtube", "kind": "video", "ref": "x"}],
             "pinned": "nonexistent", "auto_select": "yes", "failover_after_s": -5, "poll_interval_s": "60",
             "youtube_channel_probe_s": True}
    cfg = StreamsConfig.from_saved(messy)
    assert [t.id for t in cfg.targets] == ["ok-one"] and cfg.targets[0].ref == VID_1
    assert cfg.pinned is None and cfg.auto_select is True
    assert (cfg.failover_after_s, cfg.poll_interval_s, cfg.youtube_channel_probe_s) == (180, 60, 900)  # bad values -> defaults
    assert StreamsConfig.from_saved("not a dict").targets == [] and StreamsConfig.from_saved(None).pinned is None


def test_settings_changes_are_all_or_nothing():
    cfg = StreamsConfig()
    assert cfg.validated_settings({"failover_after_s": 60, "auto_select": False}) == {"failover_after_s": 60, "auto_select": False}
    with pytest.raises(StreamsError) as exc:
        cfg.validated_settings({"failover_after_s": 60, "poll_interval_s": 5, "auto_select": "x", "bogus": 1, "youtube_channel_probe_s": 1.5})
    fields = {e["loc"][0] for e in exc.value.errors}
    assert fields == {"poll_interval_s", "auto_select", "bogus", "youtube_channel_probe_s"}
    assert cfg.failover_after_s == 180  # nothing applied


# ---------------------------------------------------------------------------- debouncing live checks
def test_one_failed_check_is_not_an_outage_but_a_failed_probe_says_nothing():
    st = TargetState()
    observe(st, Live(True, "vid"), 1.0)
    assert st.live and st.video_id == "vid" and st.offline_since is None
    observe(st, Live(None, error="timeout"), 2.0)  # the CHECK failed: state untouched
    assert st.live and st.error == "timeout"
    for n in range(DOWN_PROBES - 1):
        observe(st, Live(False), 3.0 + n)
        assert st.live, "must survive fewer than DOWN_PROBES negative checks"
    observe(st, Live(False), 10.0)
    assert not st.live and st.offline_since == 10.0 and st.video_id is None
    observe(st, Live(False), 20.0)
    assert st.offline_since == 10.0  # offline "since" does not move while it stays offline
    observe(st, Live(True, "vid2"), 30.0)
    assert st.live and st.offline_since is None and st.video_id == "vid2"


def test_a_single_negative_check_never_takes_a_live_stream_offline():
    """Written without the DOWN_PROBES constant on purpose: one blip (a flaky API answer) must not move the bot."""
    st = TargetState()
    observe(st, Live(True, "vid"), 0.0)
    observe(st, Live(False), 1.0)
    assert st.live and st.offline_since is None
    observe(st, Live(False), 2.0)
    assert not st.live


def test_a_positive_check_resets_the_failure_count():
    st = TargetState()
    observe(st, Live(True), 0)
    observe(st, Live(False), 1)
    observe(st, Live(True), 2)
    for n in range(DOWN_PROBES - 1):
        observe(st, Live(False), 3 + n)
    assert st.live


# ---------------------------------------------------------------------------- the decision rules
def decide(cfg, states, previous=None, now=1000.0):
    return choose(cfg, states, previous or {}, now)


def test_priority_beats_list_order_and_list_order_breaks_ties():
    cfg = cfg_with(target("first", kind="video", ref=VID_1, priority=50), target("second", kind="video", ref=VID_2, priority=80))
    assert decide(cfg, live_states(first=True, second=True))["youtube"].target_id == "second"
    cfg = cfg_with(target("first", kind="video", ref=VID_1, priority=50), target("second", kind="video", ref=VID_2, priority=50))
    assert decide(cfg, live_states(first=True, second=True))["youtube"].target_id == "first"


def test_nothing_live_means_no_stream_and_says_why():
    cfg = cfg_with(target("a", kind="video", ref=VID_1), target("b", kind="video", ref=VID_2))
    d = decide(cfg, live_states(a=False, b=False))["youtube"]
    assert d.target_id is None and "no stream on your list is live" in d.reason


def test_the_bot_stays_in_a_live_stream_instead_of_flapping_to_a_better_one():
    cfg = cfg_with(target("low", kind="video", ref=VID_1, priority=10), target("high", kind="video", ref=VID_2, priority=90))
    d = decide(cfg, live_states(low=True, high=True), previous={"youtube": "low"})
    assert d["youtube"].target_id == "low" and d["youtube"].reason == "still live"
    d = decide(cfg, live_states(low=False, high=True), previous={"youtube": "low"})
    assert d["youtube"].target_id == "high"  # the one it was in ended


def test_a_selected_live_stream_always_wins():
    cfg = cfg_with(target("low", kind="video", ref=VID_1, priority=10), target("high", kind="video", ref=VID_2, priority=90),
                   pinned="low")
    d = decide(cfg, live_states(low=True, high=True), previous={"youtube": "high"})["youtube"]
    assert d.target_id == "low" and d.reason == "selected by you"


def test_failsafe_waits_for_the_selected_stream_then_fails_over_then_comes_back():
    cfg = cfg_with(target("sel", kind="video", ref=VID_1), target("other", kind="video", ref=VID_2), pinned="sel",
                   failover_after_s=180)
    states = {"sel": TargetState(live=False, offline_since=900.0), "other": TargetState(live=True)}
    d = decide(cfg, states, now=1000.0)["youtube"]  # offline for 100 s: still inside the grace period
    assert d.target_id is None and "80 s" in d.reason
    d = decide(cfg, states, now=1081.0)["youtube"]  # 181 s: fail over
    assert d.target_id == "other" and "offline" in d.reason
    states["sel"] = TargetState(live=True)
    assert decide(cfg, states, previous={"youtube": "other"}, now=1100.0)["youtube"].target_id == "sel"  # back to it


def test_during_the_grace_period_the_bot_keeps_a_stream_it_is_already_in():
    cfg = cfg_with(target("sel", kind="video", ref=VID_1), target("other", kind="video", ref=VID_2), pinned="sel")
    states = {"sel": TargetState(live=False, offline_since=990.0), "other": TargetState(live=True)}
    d = decide(cfg, states, previous={"youtube": "other"}, now=1000.0)["youtube"]
    assert d.target_id == "other"


def test_failsafe_with_nothing_else_live_says_so():
    cfg = cfg_with(target("sel", kind="video", ref=VID_1), pinned="sel", failover_after_s=0)
    d = decide(cfg, {"sel": TargetState(live=False, offline_since=0.0)})["youtube"]
    assert d.target_id is None and "no other stream" in d.reason


def test_with_automatic_selection_off_only_the_selected_stream_is_used():
    cfg = cfg_with(target("sel", kind="video", ref=VID_1), target("other", kind="video", ref=VID_2), pinned="sel",
                   auto_select=False, failover_after_s=0)
    states = {"sel": TargetState(live=False, offline_since=0.0), "other": TargetState(live=True)}
    d = decide(cfg, states)["youtube"]
    assert d.target_id is None and "automatic selection is off" in d.reason  # never fails over
    states["sel"].live = True
    assert decide(cfg, states)["youtube"].target_id == "sel"


def test_strict_mode_with_no_selection_is_idle_everywhere_even_if_streams_are_live():
    cfg = cfg_with(target("youtube-own"), target("kick-own", platform="kick"), auto_select=False)
    d = decide(cfg, live_states(**{"youtube-own": True, "kick-own": True}))
    assert d["youtube"].target_id is None and d["kick"].target_id is None


def test_strict_mode_restricts_the_bot_to_the_platform_of_the_selected_stream():
    cfg = cfg_with(target("youtube-own"), target("kick-own", platform="kick"), pinned="kick-own", auto_select=False)
    d = decide(cfg, live_states(**{"youtube-own": True, "kick-own": False}))
    assert d["kick"].target_id == "kick-own" and d["youtube"].target_id is None


def test_kick_is_never_switched_off_because_the_channel_is_offline():
    cfg = cfg_with(target("kick-own", platform="kick"))
    assert decide(cfg, live_states(**{"kick-own": False}))["kick"].target_id == "kick-own"
    cfg = cfg_with(target("kick-own", platform="kick", enabled=False))
    assert decide(cfg, {})["kick"].target_id is None  # only when YOU switch it off


def test_a_switched_off_stream_is_never_chosen_and_never_counts_as_selected():
    cfg = cfg_with(target("off", kind="video", ref=VID_1, enabled=False, priority=99), target("on", kind="video", ref=VID_2),
                   pinned="off")
    d = decide(cfg, live_states(off=True, on=True))["youtube"]
    assert d.target_id == "on"


def test_platforms_decide_independently():
    cfg = cfg_with(target("youtube-own"), target("kick-own", platform="kick"), pinned="youtube-own", failover_after_s=0)
    d = decide(cfg, live_states(**{"youtube-own": False, "kick-own": True}))
    assert d["kick"].target_id == "kick-own" and d["youtube"].target_id is None


# ---------------------------------------------------------------------------- the whole app (real adapter, fake Google)
from tests.youtube_fakes import FakeGoogle, eventually  # noqa: E402

APP_YT = dict(youtube_enabled=True, youtube_discovery_interval_s=0.05, youtube_poll_active_s=0.05, youtube_poll_idle_s=0.05,
              youtube_poll_quiet_s=0.05, youtube_planned_stream_hours=0.001, streams_first_check_delay_s=3600)


async def test_api_end_to_end_select_a_stream_and_the_real_adapter_follows_it(make_app_client):
    google = FakeGoogle()
    google.videos["VIDTWO000AA"] = "CHAT2"  # a second live stream on the watch list
    client, app = await make_app_client(youtube=google, **APP_YT)
    adapter = app.state.container.youtube
    await eventually(lambda: adapter.details()["live_chat_id"] == "CHAT1")  # normal discovery: the account's own stream

    snap = (await client.get("/api/streams")).json()
    assert [t["id"] for t in snap["targets"]] == ["youtube-own"] and snap["settings"]["auto_select"] is True

    r = await client.post("/api/streams/targets", json={"platform": "youtube", "ref": "https://youtu.be/VIDTWO000AA",
                                                       "label": "Second stream", "priority": 20})
    assert r.status_code == 201
    second = r.json()["target"]["id"]
    r = await client.post("/api/streams/select", json={"target_id": second})
    assert r.status_code == 200 and r.json()["selected"] == second
    r = await client.post("/api/streams/check")
    row = next(t for t in r.json()["targets"] if t["id"] == second)
    assert row["live"] is True and row["selected"] is True and row["active"] is True
    await eventually(lambda: adapter.details()["live_chat_id"] == "CHAT2")  # the REAL adapter moved to the chosen stream
    assert adapter.details()["video_override"] == "VIDTWO000AA"

    # un-select: nothing selected + sticky = it stays; switching the stream off returns to the own stream
    assert (await client.post("/api/streams/select", json={"target_id": None})).json()["selected"] is None
    await client.post(f"/api/streams/targets/{second}", json={"enabled": False})
    await eventually(lambda: adapter.details()["live_chat_id"] == "CHAT1")
    assert adapter.details()["video_override"] is None

    # the selection and list survive in the database (what a restart loads)
    saved = json.loads(await app.state.container.repo.get_setting("streams.config"))
    assert [t["ref"] for t in saved["targets"] if t["kind"] == "video"] == ["VIDTWO000AA"]


async def test_api_validation_errors_and_auth(make_app_client):
    google = FakeGoogle()
    client, app = await make_app_client(youtube=google, **APP_YT)
    for body, status in [
        ({"platform": "kick", "ref": "x"}, 422), ({"platform": "youtube", "ref": "@handle"}, 422),
        ({"platform": "youtube", "ref": "https://example.com/x"}, 422), ("not json", 422),
    ]:
        r = await client.post("/api/streams/targets", content=json.dumps(body) if not isinstance(body, str) else body)
        assert r.status_code == status, (body, r.text)
    assert (await client.post("/api/streams/select", json={"target_id": "nope"})).status_code == 404
    assert (await client.post("/api/streams/select", json={"target_id": 5})).status_code == 422
    assert (await client.post("/api/streams/select", json={})).status_code == 422
    assert (await client.post("/api/streams/targets/nope", json={"enabled": False})).status_code == 404
    assert (await client.post("/api/streams/targets/nope/remove")).status_code == 404
    r = await client.post("/api/streams/targets/youtube-own/remove")
    assert r.status_code == 422 and "switch it off" in r.text
    r = await client.post("/api/streams/settings", json={"poll_interval_s": 1, "mystery": 1})
    assert r.status_code == 422 and {e["loc"][0] for e in r.json()["detail"]} == {"poll_interval_s", "mystery"}
    r = await client.post("/api/streams/settings", json={"failover_after_s": 45, "auto_select": False})
    assert r.status_code == 200 and r.json()["settings"]["failover_after_s"] == 45 and r.json()["settings"]["auto_select"] is False

    anonymous = httpx_client_without_key(client)
    for method, path in [("get", "/api/streams"), ("post", "/api/streams/check"), ("post", "/api/streams/select")]:
        r = await getattr(anonymous, method)(path)
        assert r.status_code in (401, 403), (path, r.status_code)
    await anonymous.aclose()


def httpx_client_without_key(client):
    import httpx
    return httpx.AsyncClient(transport=client._transport, base_url="http://127.0.0.1")


async def test_the_app_works_with_no_platform_enabled(make_app_client):
    client, app = await make_app_client(streams_first_check_delay_s=3600)
    snap = (await client.get("/api/streams")).json()
    assert snap["targets"] == [] and snap["available"] == {"youtube": False, "kick": False}
    r = await client.post("/api/streams/targets", json={"platform": "youtube", "ref": VID_1})
    assert r.status_code == 422 and "not enabled" in r.text


# ---------------------------------------------------------------------------- director with fake platforms
class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class FakeYouTube:
    """Stands in for YouTubeAdapter: what the director reads (status/details) and calls (retarget/start/stop)."""

    def __init__(self) -> None:
        self.state = ConnectionState.DISCONNECTED
        self.video_id: str | None = None
        self.calls: list[tuple[str, Any]] = []

    def status(self) -> ConnectionStatus:
        return ConnectionStatus(self.state, "fake")

    def details(self) -> dict[str, Any]:
        return {"video_id": self.video_id}

    async def retarget(self, video_id):
        self.calls.append(("retarget", video_id))
        if video_id:
            self.state, self.video_id = ConnectionState.CONNECTED, video_id  # the fake "finds" the video at once

    async def stop(self):
        self.calls.append(("stop", None))
        self.state = ConnectionState.DISCONNECTED

    async def start(self, cb):
        self.calls.append(("start", cb))


class FakeKick(FakeYouTube):
    def __init__(self) -> None:
        super().__init__()
        self.live: bool | None = True

    def details(self) -> dict[str, Any]:
        return {"is_live": self.live}


class FakeYtApi:
    def __init__(self) -> None:
        self.live_videos: set[str] = set()
        self.channel_video: dict[str, str | None] = {}
        self.own: SimpleNamespace | None = None
        self.calls: list[tuple[str, str | None]] = []
        self.fail: Exception | None = None

    async def video_chat_id(self, video_id):
        self.calls.append(("videos.list", video_id))
        if self.fail:
            raise self.fail
        return "chat-" + video_id if video_id in self.live_videos else None

    async def find_live_video(self, channel_id):
        self.calls.append(("search.list", channel_id))
        if self.fail:
            raise self.fail
        return self.channel_video.get(channel_id)

    async def active_broadcast(self):
        self.calls.append(("liveBroadcasts.list", None))
        if self.fail:
            raise self.fail
        return self.own

    def count(self, name: str) -> int:
        return sum(1 for n, _ in self.calls if n == name)


class FakeQuota:
    def __init__(self) -> None:
        self.refuse: set[str] = set()

    def can_read(self, method: str) -> bool:
        return method not in self.refuse


@dataclass
class Rig:
    director: StreamDirector
    yt: FakeYouTube
    kick: FakeKick
    api: FakeYtApi
    clock: FakeClock
    quota: FakeQuota
    saved: list[dict[str, Any]]
    events: list[tuple[str, str]]


async def make_rig(*, youtube: bool = True, kick: bool = True, saved: Any = None) -> Rig:
    yt, kk, api, clock, quota = FakeYouTube(), FakeKick(), FakeYtApi(), FakeClock(), FakeQuota()
    saves: list[dict[str, Any]] = []
    events: list[tuple[str, str]] = []

    async def save(d):
        saves.append(d)

    async def log_event(kind, message):
        events.append((kind, message))

    d = StreamDirector(youtube=yt if youtube else None, youtube_api=api if youtube else None, quota=quota,
                       kick=kk if kick else None, save=save, log_event=log_event, clock=clock, first_delay_s=0)
    d.ingest = lambda m: None
    await d.load(json.dumps(saved) if saved is not None else None)
    return Rig(d, yt, kk, api, clock, quota, saves, events)


async def test_own_streams_exist_without_any_setup_and_cannot_be_removed():
    rig = await make_rig()
    ids = [t.id for t in rig.director.config.targets]
    assert ids == ["youtube-own", "kick-own"]
    with pytest.raises(StreamsError, match="switch it off instead"):
        await rig.director.remove_target("youtube-own")
    rig = await make_rig(youtube=False, kick=False)
    assert rig.director.config.targets == []  # nothing connected, nothing listed


async def test_default_behaviour_is_unchanged_when_nothing_is_configured():
    rig = await make_rig()
    rig.yt.state, rig.yt.video_id = ConnectionState.CONNECTED, "ownvid"
    await rig.director.tick()
    snap = rig.director.snapshot()
    own = next(t for t in snap["targets"] if t["id"] == "youtube-own")
    assert own["live"] is True and own["video_id"] == "ownvid" and own["active"] is True
    assert rig.yt.calls == [] and rig.kick.calls == []  # no retargeting, no start/stop
    assert rig.api.calls == []  # reading the adapter's own state costs no YouTube quota


async def test_the_bot_starts_with_a_live_stream_from_the_list_when_nothing_is_selected():
    rig = await make_rig()
    added = await rig.director.add_target({"platform": "youtube", "ref": f"https://youtu.be/{VID_1}", "label": " Main  stream "})
    assert added["label"] == "Main stream" and added["kind"] == "video"
    rig.api.live_videos.add(VID_1)
    await rig.director.tick()
    assert rig.yt.calls == [("retarget", VID_1)]
    snap = rig.director.snapshot()
    assert snap["now"]["youtube"]["target_id"] == added["id"]
    assert next(t for t in snap["targets"] if t["id"] == added["id"])["active"] is True
    await rig.director.flush()
    assert any("Main stream" in text for _, text in rig.events)  # the change was written to the connection log


async def test_a_selected_stream_is_followed_and_survives_a_restart():
    rig = await make_rig()
    a = await rig.director.add_target({"platform": "youtube", "ref": VID_1})
    b = await rig.director.add_target({"platform": "youtube", "ref": VID_2})
    rig.api.live_videos.update({VID_1, VID_2})
    await rig.director.select(b["id"])
    await rig.director.tick()
    assert rig.yt.calls[-1] == ("retarget", VID_2) and rig.director.snapshot()["selected"] == b["id"]
    # restart: a new director loads what was saved
    again = await make_rig(saved=rig.saved[-1])
    assert again.director.config.pinned == b["id"] and [t.ref for t in again.director.config.targets if t.kind == "video"] == [VID_1, VID_2]
    assert a["id"] != b["id"]


async def test_failover_moves_the_adapter_when_the_selected_stream_stays_offline_and_returns_when_it_is_back():
    rig = await make_rig(saved={"failover_after_s": 120})
    sel = await rig.director.add_target({"platform": "youtube", "ref": VID_1})
    other = await rig.director.add_target({"platform": "youtube", "ref": VID_2})
    rig.api.live_videos.add(VID_2)
    await rig.director.select(sel["id"])
    await rig.director.tick()
    assert ("retarget", VID_2) not in rig.yt.calls  # still waiting for the selected one
    assert "wait" in rig.director.snapshot()["now"]["youtube"]["reason"]
    rig.clock.t += 130
    await rig.director.tick()
    assert rig.yt.calls[-1] == ("retarget", VID_2)
    assert rig.director.snapshot()["now"]["youtube"]["target_id"] == other["id"]
    rig.api.live_videos.add(VID_1)  # the selected stream finally starts
    rig.clock.t += 60
    await rig.director.tick()
    assert rig.yt.calls[-1] == ("retarget", VID_1)


async def test_an_unchanged_decision_does_not_touch_the_adapter_again():
    rig = await make_rig()
    await rig.director.add_target({"platform": "youtube", "ref": VID_1})
    rig.api.live_videos.add(VID_1)
    for _ in range(4):
        await rig.director.tick()
        rig.clock.t += 60
    assert rig.yt.calls == [("retarget", VID_1)]


async def test_a_manual_disconnect_is_not_undone_by_the_director():
    rig = await make_rig()
    await rig.director.add_target({"platform": "youtube", "ref": VID_1})
    rig.api.live_videos.add(VID_1)
    await rig.director.tick()
    rig.yt.state = ConnectionState.DISCONNECTED  # the streamer pressed Disconnect in the dashboard
    rig.yt.calls.clear()
    await rig.director.tick()
    assert rig.yt.calls == []  # decision unchanged -> hands off


async def test_the_selected_stream_check_does_not_spend_quota_while_the_adapter_is_already_there():
    rig = await make_rig()
    await rig.director.add_target({"platform": "youtube", "ref": VID_1})
    rig.api.live_videos.add(VID_1)
    await rig.director.tick()
    before = rig.api.count("videos.list")
    await rig.director.tick()
    await rig.director.tick()
    assert rig.api.count("videos.list") == before  # connected on that video: the adapter state is the answer


async def test_own_stream_is_asked_from_youtube_only_while_the_adapter_is_elsewhere():
    rig = await make_rig()
    await rig.director.add_target({"platform": "youtube", "ref": VID_1})
    rig.api.live_videos.add(VID_1)
    await rig.director.tick()  # adapter is moved to VID_1
    rig.api.own = SimpleNamespace(video_id="ownvid", title="Own")
    await rig.director.tick()
    assert rig.api.count("liveBroadcasts.list") >= 1
    own = next(t for t in rig.director.snapshot()["targets"] if t["id"] == "youtube-own")
    assert own["live"] is True and own["title"] == "Own"


async def test_channels_are_checked_rarely_and_a_found_stream_is_followed():
    rig = await make_rig()
    ch = await rig.director.add_target({"platform": "youtube", "ref": f"https://www.youtube.com/channel/{CH_A}"})
    assert ch["kind"] == "channel"
    rig.api.channel_video[CH_A] = VID_1
    rig.api.live_videos.add(VID_1)
    await rig.director.tick()
    assert rig.api.count("search.list") == 1 and rig.yt.calls[-1] == ("retarget", VID_1)
    rig.yt.state = ConnectionState.DISCONNECTED
    rig.clock.t += 60  # well inside the channel check interval
    await rig.director.tick()
    assert rig.api.count("search.list") == 1


async def test_two_channels_share_the_search_budget():
    rig = await make_rig()
    await rig.director.add_target({"platform": "youtube", "ref": CH_A})
    await rig.director.add_target({"platform": "youtube", "ref": CH_B})
    await rig.director.tick()
    assert rig.api.count("search.list") == 2
    rig.clock.t += 900 + 5  # one channel's interval is now 2 x 900 s
    await rig.director.tick()
    assert rig.api.count("search.list") == 2
    rig.clock.t += 900
    await rig.director.tick()
    assert rig.api.count("search.list") == 4


async def test_the_quota_guard_blocks_a_check_and_that_is_unknown_not_offline():
    rig = await make_rig()
    ch = await rig.director.add_target({"platform": "youtube", "ref": CH_A})
    rig.quota.refuse.add("search.list")
    await rig.director.tick()
    assert rig.api.count("search.list") == 0
    row = next(t for t in rig.director.snapshot()["targets"] if t["id"] == ch["id"])
    assert row["live"] is None and "quota" in row["error"]


async def test_a_failing_check_never_breaks_the_loop_or_marks_a_live_stream_offline():
    rig = await make_rig()
    v = await rig.director.add_target({"platform": "youtube", "ref": VID_1})
    rig.api.live_videos.add(VID_1)
    await rig.director.tick()
    rig.yt.state = ConnectionState.DISCONNECTED  # force an API check next time
    rig.api.fail = RuntimeError("boom")
    for _ in range(DOWN_PROBES + 2):
        await rig.director.tick()
    row = next(t for t in rig.director.snapshot()["targets"] if t["id"] == v["id"])
    assert row["live"] is True and row["error"] == "RuntimeError"


async def test_with_no_stream_live_and_own_switched_off_the_adapter_stops_polling_and_restarts_later():
    rig = await make_rig()
    await rig.director.update_target("youtube-own", {"enabled": False})
    v = await rig.director.add_target({"platform": "youtube", "ref": VID_1})
    await rig.director.tick()
    assert rig.yt.calls == [("stop", None)]
    rig.api.live_videos.add(VID_1)
    await rig.director.tick()
    assert [c[0] for c in rig.yt.calls] == ["stop", "retarget", "start"] and rig.yt.calls[1] == ("retarget", VID_1)
    assert v["id"] == rig.director.snapshot()["now"]["youtube"]["target_id"]


async def test_switching_kick_off_stops_it_and_switching_it_on_starts_it():
    rig = await make_rig()
    await rig.director.update_target("kick-own", {"enabled": False})
    assert [c[0] for c in rig.kick.calls] == ["stop"]
    await rig.director.update_target("kick-own", {"enabled": True})
    assert [c[0] for c in rig.kick.calls] == ["stop", "start"]
    await rig.director.tick()
    assert [c[0] for c in rig.kick.calls] == ["stop", "start"]


async def test_strict_mode_idles_the_other_platforms_and_the_adapter():
    rig = await make_rig()
    v = await rig.director.add_target({"platform": "youtube", "ref": VID_1})
    rig.api.live_videos.add(VID_1)
    await rig.director.update_settings({"auto_select": False})
    assert rig.yt.calls[-1] == ("stop", None) and [c[0] for c in rig.kick.calls] == ["stop"]  # nothing selected: idle
    await rig.director.select(v["id"])
    await rig.director.tick()
    assert rig.yt.calls[-1] == ("start", rig.director.ingest) and ("retarget", VID_1) in rig.yt.calls
    assert [c[0] for c in rig.kick.calls] == ["stop"]  # the selected stream is on YouTube: Kick stays idle


# ---------------------------------------------------------------------------- editing rules
async def test_kick_channels_cannot_be_added_and_the_reason_is_given():
    rig = await make_rig()
    with pytest.raises(StreamsError, match="signed in with"):
        await rig.director.add_target({"platform": "kick", "ref": "someone"})


async def test_adding_validates_everything():
    rig = await make_rig()
    for body, fragment in [
        ("x", "object"), ({"platform": "twitch", "ref": VID_1}, "must be one of"), ({"platform": "youtube", "ref": ""}, "paste"),
        ({"platform": "youtube", "ref": VID_1, "priority": 101}, "0 to 100"),
        ({"platform": "youtube", "ref": VID_1, "priority": True}, "0 to 100"),
    ]:
        with pytest.raises(StreamsError, match=fragment):
            await rig.director.add_target(body)
    await rig.director.add_target({"platform": "youtube", "ref": VID_1})
    with pytest.raises(StreamsError, match="already on the list"):
        await rig.director.add_target({"platform": "youtube", "ref": f"https://youtu.be/{VID_1}"})
    off = await make_rig(youtube=False)
    with pytest.raises(StreamsError, match="not enabled"):
        await off.director.add_target({"platform": "youtube", "ref": VID_1})


async def test_the_list_has_a_size_limit():
    rig = await make_rig()
    for n in range(MAX_TARGETS - 2):
        await rig.director.add_target({"platform": "youtube", "ref": f"{n:011d}"})
    with pytest.raises(StreamsError, match="full"):
        await rig.director.add_target({"platform": "youtube", "ref": VID_1})


async def test_select_remove_and_update_rules():
    rig = await make_rig()
    v = await rig.director.add_target({"platform": "youtube", "ref": VID_1})
    with pytest.raises(KeyError):
        await rig.director.select("nope")
    await rig.director.select(v["id"])
    await rig.director.update_target(v["id"], {"enabled": False})
    assert rig.director.config.pinned is None  # a switched-off stream cannot stay selected
    with pytest.raises(StreamsError, match="switched off"):
        await rig.director.select(v["id"])
    await rig.director.update_target(v["id"], {"enabled": True, "priority": 70, "label": "  Renamed   one "})
    row = next(t for t in rig.director.snapshot()["targets"] if t["id"] == v["id"])
    assert (row["priority"], row["label"], row["enabled"]) == (70, "Renamed one", True)
    for bad in [{"enabled": "yes"}, {"priority": 200}, {"ref": "x"}, {"platform": "kick"}]:
        with pytest.raises(StreamsError):
            await rig.director.update_target(v["id"], bad)
    await rig.director.select(v["id"])
    await rig.director.remove_target(v["id"])
    assert rig.director.config.pinned is None and rig.director.config.target(v["id"]) is None
    with pytest.raises(KeyError):
        await rig.director.remove_target(v["id"])
    with pytest.raises(KeyError):
        await rig.director.update_target("nope", {})


async def test_every_change_is_saved_and_a_save_failure_does_not_break_the_bot():
    rig = await make_rig()
    n = len(rig.saved)
    await rig.director.add_target({"platform": "youtube", "ref": VID_1})
    await rig.director.update_settings({"failover_after_s": 30})
    assert len(rig.saved) == n + 2 and rig.saved[-1]["failover_after_s"] == 30

    async def broken(_):
        raise OSError("disk full")
    rig.director._save = broken
    await rig.director.add_target({"platform": "youtube", "ref": VID_2})  # must not raise


async def test_the_background_loop_survives_a_crashing_check_and_wakes_on_changes():
    rig = await make_rig()
    rig.director.config.poll_interval_s = 3600
    ticks: list[int] = []
    original = rig.director.tick

    async def tick():
        ticks.append(1)
        if len(ticks) == 1:
            raise RuntimeError("surprise")
        await original()
    rig.director.tick = tick
    task = asyncio.create_task(rig.director.run())
    await asyncio.sleep(0.05)
    assert len(ticks) == 1  # crashed once, still alive, now sleeping for an hour
    rig.director.wake()
    await asyncio.sleep(0.05)
    assert len(ticks) == 2
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_snapshot_marks_unknown_offline_and_live_differently():
    rig = await make_rig()
    v = await rig.director.add_target({"platform": "youtube", "ref": VID_1})
    row = lambda: next(t for t in rig.director.snapshot()["targets"] if t["id"] == v["id"])  # noqa: E731
    assert row()["live"] is None  # never checked
    await rig.director.tick()
    assert row()["live"] is False and row()["checked_s_ago"] == 0
    rig.api.live_videos.add(VID_1)
    await rig.director.tick()
    assert row()["live"] is True and row()["active"] is True and row()["offline_s"] is None
    assert json.dumps(rig.director.snapshot())  # serialisable

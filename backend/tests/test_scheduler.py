"""Proactive engagement (spec Mode B). The scheduler runs against the real app (queue, processor, simulator platform,
database) with a fake clock; tick() is called by hand so every timing decision is deterministic."""
import asyncio
import random

import pytest

from app.chat.models import Category, ChatMessage, Platform
from app.config import Settings
from app.platforms.simulator import SimulatorAdapter
from tests.conftest import FakeClock

SIM = Platform.SIMULATOR


@pytest.fixture
def make_sched(make_app_client):
    """-> (client, app, container, sim_adapter, clock). The scheduler's own background loop is parked (tick 1 h)."""
    async def _make(**overrides):
        overrides.setdefault("proactive_platforms", ["simulator"])
        overrides.setdefault("proactive_tick_s", 3600)
        client, app = await make_app_client(**overrides)
        c = app.state.container
        await asyncio.sleep(0.05)  # let the parked background loop run its first pass on the real clock, then take over
        clock = FakeClock(1000.0)
        c.scheduler._clock = clock
        c.scheduler._rng = random.Random(7)
        c.scheduler._states.clear()  # the parked loop may already have run once on the real clock
        c.scheduler._last_sent_any = None
        await c.scheduler.tick()  # first look: connected -> timers start now
        return client, app, c, c.adapters[SIM], clock
    return _make


def sent(sim) -> list[str]:
    return list(sim.sent)


async def viewer_says(client, text="hello everyone!", user="alice"):
    return (await client.post("/api/simulate/message", json={"username": user, "message": text})).json()


# ---------------------------------------------------------------------------- when it speaks
async def test_nothing_is_sent_until_chat_is_quiet_and_an_activity_is_due(make_sched):
    client, app, c, sim, clock = await make_sched()
    clock.advance(400)  # quiet (>360 s) but no activity is due yet (earliest is 480 s)
    assert await c.scheduler.tick() == [] and sent(sim) == []
    clock.advance(400)  # 800 s: the general-engagement timer (480-720 s) has run out
    attempts = await c.scheduler.tick()
    assert [a["action"] for a in attempts] == ["replied"] and attempts[0]["activity"] == "general"
    assert attempts[0]["personality"] == "StreamBuddy" and sent(sim) == [attempts[0]["reply"]]
    assert c.metrics.counters()["proactive_sent"] == 1
    rows = (await client.get("/api/messages")).json()  # it is stored like every bot message, answering nobody
    assert any(r["is_bot_message"] and r["message"] == sent(sim)[0] for r in rows)


async def test_viewer_activity_pauses_proactive_messages(make_sched):
    client, app, c, sim, clock = await make_sched()
    clock.advance(800)  # an activity is due...
    await viewer_says(client)  # ...but a viewer just spoke (and was answered)
    replies = len(sim.sent)
    assert await c.scheduler.tick() == []
    clock.advance(300)  # still inside the 360 s quiet window
    assert await c.scheduler.tick() == [] and len(sim.sent) == replies
    clock.advance(70)  # 370 s of silence
    attempts = await c.scheduler.tick()
    assert [a["action"] for a in attempts] == ["replied"]


async def test_spam_and_ignored_messages_still_count_as_activity(make_sched):
    client, app, c, sim, clock = await make_sched()
    clock.advance(800)
    out = await viewer_says(client, "check out https://spam.example/free-stuff now")
    assert out["action"] == "ignored"  # the bot does not answer it...
    assert await c.scheduler.tick() == []  # ...but the chat is not "quiet" either


async def test_minimum_gap_after_any_bot_message(make_sched):
    client, app, c, sim, clock = await make_sched()
    await c.scheduler.update_settings({"quiet_after_s": 10, "min_gap_s": 600})
    clock.advance(800)
    await viewer_says(client)  # the bot answers at t=1800
    clock.advance(100)  # quiet for 100 s (> 10) but only 100 s since the bot spoke (< 600)
    assert await c.scheduler.tick() == []
    clock.advance(600)
    assert [a["action"] for a in await c.scheduler.tick()] == ["replied"]


async def test_disabled_scheduler_does_nothing_and_never_wakes_the_ai(make_sched, fake_ollama):
    client, app, c, sim, clock = await make_sched()
    assert (await client.post("/api/control/proactive", json={"enabled": False})).json()["proactive_enabled"] is False
    clock.advance(5000)
    assert await c.scheduler.tick() == [] and sent(sim) == [] and not fake_ollama.real_calls
    await client.post("/api/control/proactive", json={"enabled": True})
    assert [a["action"] for a in await c.scheduler.tick()] == ["replied"]


async def test_an_unlisted_platform_is_never_used(make_sched):
    client, app, c, sim, clock = await make_sched(proactive_platforms=["youtube"])  # the simulator is not listed
    clock.advance(5000)
    assert await c.scheduler.tick() == [] and sent(sim) == []


# ---------------------------------------------------------------------------- which activity
async def test_highest_priority_personality_goes_first_then_most_overdue(make_sched):
    client, app, c, sim, clock = await make_sched()
    clock.advance(5000)  # general, gaming_question and trivia are all overdue
    due = dict(c.scheduler._states[SIM].next_due)
    order = []
    for _ in range(3):
        attempts = await c.scheduler.tick()
        assert len(attempts) == 1 and attempts[0]["action"] == "replied"  # one message per tick
        order.append(attempts[0]["activity"])
        clock.advance(250)  # past the 240 s minimum gap
    # QuizBot (priority 2) beats StreamBuddy (priority 1) even though trivia has been overdue the shortest time;
    # between equals, the activity that has been waiting longest (earliest due time) goes first.
    assert due["trivia"] > min(due["general"], due["gaming_question"])  # the premise of the test
    assert order == ["trivia"] + sorted(["general", "gaming_question"], key=due.get)


async def test_activity_with_no_enabled_personality_is_not_attempted(make_sched):
    client, app, c, sim, clock = await make_sched()
    await client.post("/api/personalities/QuizBot/enabled", json={"enabled": False})
    await c.scheduler.update_settings({"activities": {"general": {"enabled": False}, "gaming_question": {"enabled": False}}})
    clock.advance(5000)
    assert await c.scheduler.tick() == []  # only trivia is left, and nobody handles it


async def test_sent_message_restarts_only_its_own_timer(make_sched):
    client, app, c, sim, clock = await make_sched()
    clock.advance(800)
    await c.scheduler.tick()
    status = c.scheduler.status()["platforms"]["simulator"]
    assert 480 <= status["next_in_s"]["general"] <= 720  # new random interval from the configured range
    assert status["sent"] == 1 and status["last_activity"] == "general"


# ---------------------------------------------------------------------------- protective checks
async def test_paused_bot_skips_and_retries_soon_keeping_the_activity_due(make_sched):
    client, app, c, sim, clock = await make_sched()
    clock.advance(800)
    await client.post("/api/control/pause")
    attempts = await c.scheduler.tick()
    assert attempts[0]["action"] == "skipped" and attempts[0]["reason"] == "outgoing_paused" and sent(sim) == []
    clock.advance(10)
    assert await c.scheduler.tick() == []  # waits proactive_retry_s (30 s) before looking again
    await client.post("/api/control/resume")
    clock.advance(30)
    assert [a["action"] for a in await c.scheduler.tick()] == ["replied"]  # the activity had stayed due


async def test_failed_send_backs_off_exponentially_and_recovers(make_sched):
    client, app, c, sim, clock = await make_sched()
    clock.advance(800)
    sim.fail_next = "platform said no"
    first = await c.scheduler.tick()
    assert first[0]["action"] == "send_failed" and c.scheduler.status()["platforms"]["simulator"]["failures"] == 1
    clock.advance(30)  # 1st backoff is 60 s
    assert await c.scheduler.tick() == []
    clock.advance(31)
    sim.fail_next = "still no"
    assert (await c.scheduler.tick())[0]["action"] == "send_failed"  # 2nd backoff is 120 s
    clock.advance(100)
    assert await c.scheduler.tick() == []
    clock.advance(21)
    assert (await c.scheduler.tick())[0]["action"] == "replied"
    assert c.scheduler.status()["platforms"]["simulator"]["failures"] == 0  # success resets the backoff


async def test_a_share_of_the_platform_allowance_is_kept_for_viewers(make_sched):
    limits = {"simulator": {"min_gap_s": 0, "max_per_window": 5, "window_s": 600, "user_cooldown_s": 0, "max_chars": 500}}
    client, app, c, sim, clock = await make_sched(rate_limits=limits, proactive_viewer_reserve=0.4)
    await c.scheduler.update_settings({"quiet_after_s": 10, "min_gap_s": 10})
    for i in range(3):  # three answered viewers use 3 of the 5 messages in the window
        assert (await viewer_says(client, f"hello number {i}!", user=f"user{i}"))["action"] == "replied"
    clock.advance(800)
    attempts = await c.scheduler.tick()  # 40% of 5 = 2 are reserved for viewers: proactive may only use messages 1-3
    assert attempts[0]["action"] == "skipped" and attempts[0]["reason"] == "viewer_reserve"


async def test_viewer_speaking_while_the_message_is_being_written_cancels_it(make_sched, fake_ollama):
    client, app, c, sim, clock = await make_sched()
    clock.advance(800)

    def reply(body):  # a viewer speaks while the model is still generating
        c.scheduler.viewer_message(SIM)
        return "Chat, which car would you pick for a getaway from the cops tonight?"

    fake_ollama.reply = reply
    attempts = await c.scheduler.tick()
    assert attempts[0]["action"] == "skipped" and attempts[0]["reason"] == "chat_active" and sent(sim) == []
    assert c.metrics.counters()["proactive_skipped_chat_active"] == 1


async def test_viewer_messages_are_processed_before_a_queued_proactive_one(make_sched, fake_ollama):
    client, app, c, sim, clock = await make_sched()

    def reply(body):
        text = " ".join(m["content"] for m in body["messages"])
        return ("Chat, what is the best car you have ever driven in any game?"
                if "Say something engaging that invites viewers to reply" in text  # the proactive_general instruction
                else "Hey there, welcome to the stream, glad you could join us today!")

    fake_ollama.reply = reply
    await c.processor.stop()  # hold the worker so both are queued, the proactive one FIRST
    proactive = asyncio.create_task(
        c.processor.submit_proactive(SIM, Category.PROACTIVE_GENERAL, still_ok=lambda: True))
    await asyncio.sleep(0)
    viewer = asyncio.create_task(c.processor.submit(
        ChatMessage(platform=SIM, viewer_id="v1", username="alice", message="hello everyone!")))
    await asyncio.sleep(0)
    await c.processor.start()
    assert (await viewer).action == "replied" and (await proactive).action == "replied"
    assert sent(sim)[0].startswith("Hey there, welcome") and sent(sim)[1].startswith("Chat, what is the best car")


async def test_never_repeats_itself_when_the_ai_is_down_and_prepared_messages_run_out(make_sched, fake_ollama):
    client, app, c, sim, clock = await make_sched()
    await c.scheduler.update_settings({"activities": {"gaming_question": {"enabled": False}, "trivia": {"enabled": False}}})
    fake_ollama.version_ok = False  # the AI is unreachable: prepared rule messages are used instead
    texts = []
    for _ in range(2):  # "proactive_general" has exactly two prepared messages
        clock.advance(1000)
        attempts = await c.scheduler.tick()
        assert attempts[0]["action"] == "replied"
        texts.append(attempts[0]["reply"])
    assert len(set(texts)) == 2
    clock.advance(1000)
    attempts = await c.scheduler.tick()
    assert attempts[0]["action"] == "skipped" and attempts[0]["reason"] == "nothing_new_to_say"
    assert sent(sim) == texts  # silence beats a repeat
    assert c.scheduler.status()["platforms"]["simulator"]["failures"] == 1  # and it backs off instead of retrying at once


# ---------------------------------------------------------------------------- connections and platforms
async def test_timers_restart_after_a_reconnect(make_sched):
    client, app, c, sim, clock = await make_sched()
    await sim.stop()
    clock.advance(20_000)  # offline for hours: timers that ran out meanwhile must not fire on reconnect
    assert await c.scheduler.tick() == []
    await sim.start(None)
    assert await c.scheduler.tick() == []
    assert min(c.scheduler.status()["platforms"]["simulator"]["next_in_s"].values()) >= 480


async def test_two_platforms_are_staggered_and_write_different_messages(make_sched):
    client, app, c, sim, clock = await make_sched(proactive_platforms=["simulator", "kick"])
    kick = SimulatorAdapter()
    await kick.start(None)
    c.adapters[Platform.KICK] = kick  # stands in for a second connected platform
    c.scheduler._states.clear()
    await c.scheduler.tick()
    clock.advance(5000)  # both platforms have activities due
    first = await c.scheduler.tick()
    assert len(first) == 1  # the second platform is held back by the stagger
    clock.advance(46)
    second = await c.scheduler.tick()
    assert len(second) == 1 and second[0]["platform"] != first[0]["platform"]
    assert first[0]["reply"] != second[0]["reply"]  # never identical text on both platforms
    assert len(sim.sent) + len(kick.sent) == 2


# ---------------------------------------------------------------------------- settings
async def test_settings_can_be_changed_live_are_saved_and_survive_a_restart(make_sched, make_app_client):
    client, app, c, sim, clock = await make_sched()
    body = {"quiet_after_s": 120, "activities": {"trivia": {"min_interval_s": 600, "max_interval_s": 900, "enabled": False}}}
    out = await client.post("/api/scheduler/settings", json=body)
    assert out.status_code == 200 and out.json()["quiet_after_s"] == 120
    assert out.json()["activities"]["trivia"]["enabled"] is False and out.json()["min_gap_s"] == 240  # untouched value kept
    saved = await c.repo.get_setting("scheduler.settings")
    assert saved and '"quiet_after_s": 120' in saved
    client2, app2 = await make_app_client(proactive_platforms=["simulator"], proactive_tick_s=3600)
    restored = app2.state.container.scheduler.config
    assert restored.quiet_after_s == 120 and restored.activities["trivia"].min_interval_s == 600
    assert restored.activities["trivia"].enabled is False


async def test_changing_an_interval_reschedules_from_now(make_sched):
    client, app, c, sim, clock = await make_sched()
    await c.scheduler.update_settings({"activities": {"general": {"min_interval_s": 3000, "max_interval_s": 3100}}})
    assert 3000 <= c.scheduler.status()["platforms"]["simulator"]["next_in_s"]["general"] <= 3100
    await c.scheduler.update_settings({"activities": {"general": {"min_interval_s": 30, "max_interval_s": 60}}})
    assert c.scheduler.status()["platforms"]["simulator"]["next_in_s"]["general"] <= 60  # not stuck behind the old timer


@pytest.mark.parametrize("body", [
    {"quiet_after_s": 5},  # below the 10 s floor
    {"quiet_after_s": 10**7},
    {"quiet_after_s": "soon"},
    {"quiet_after_s": True},  # a bool is not a number of seconds
    {"unknown_key": 1},
    {"activities": {"nonsense": {"min_interval_s": 100}}},
    {"activities": {"general": {"min_interval_s": 900, "max_interval_s": 600}}},
    {"activities": {"general": {"enabled": "yes"}}},
    {"activities": {"general": {"colour": "red"}}},
    {"activities": []},
    [],
])
async def test_invalid_settings_are_rejected_without_changing_anything(make_sched, body):
    client, app, c, sim, clock = await make_sched()
    before = c.scheduler.config.to_dict()
    assert (await client.post("/api/scheduler/settings", json=body)).status_code == 422
    assert c.scheduler.config.to_dict() == before
    assert await c.repo.get_setting("scheduler.settings") is None


async def test_one_bad_value_rejects_the_whole_update(make_sched):
    client, app, c, sim, clock = await make_sched()
    before = c.scheduler.config.to_dict()
    resp = await client.post("/api/scheduler/settings", json={"quiet_after_s": 200, "min_gap_s": 1})
    assert resp.status_code == 422 and c.scheduler.config.to_dict() == before  # the valid half was not applied either


async def test_scheduler_routes_need_the_key_and_status_has_the_documented_shape(make_sched):
    client, app, c, sim, clock = await make_sched()
    for method, path in [("get", "/api/scheduler"), ("post", "/api/scheduler/settings"), ("post", "/api/control/proactive")]:
        assert (await getattr(client, method)(path, headers={"X-API-Key": ""})).status_code == 401
    data = (await client.get("/api/scheduler")).json()
    assert data["enabled"] is True and set(data["settings"]["activities"]) == {"general", "gaming_question", "trivia"}
    platform = data["platforms"]["simulator"]
    assert platform["connected"] is True and platform["may_speak_now"] is False and platform["quiet_for_s"] == 0
    assert (await client.get("/api/status")).json()["controls"]["proactive_enabled"] is True


# ---------------------------------------------------------------------------- configuration
def test_spec_default_intervals_and_validation():
    s = Settings()
    assert (s.proactive_general_min_s, s.proactive_general_max_s) == (480, 720)  # 8-12 min
    assert (s.proactive_question_min_s, s.proactive_question_max_s) == (900, 1200)  # 15-20 min
    assert (s.proactive_trivia_min_s, s.proactive_trivia_max_s) == (1800, 2400)  # 30-40 min
    assert s.proactive_quiet_after_s == 360  # quiet-chat detection 5-7 min
    assert "simulator" not in s.proactive_platforms
    for bad in [dict(proactive_general_min_s=900, proactive_general_max_s=600), dict(proactive_viewer_reserve=1.0),
                dict(proactive_tick_s=0), dict(proactive_quiet_after_s=-1), dict(proactive_trivia_min_s=0)]:
        with pytest.raises(ValueError):
            Settings(**bad)

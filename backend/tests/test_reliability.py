"""Regression tests for the gaps the Phase 7 reliability work found (see docs/phase7-reliability.md)."""
import asyncio
import sqlite3

from app.chat.models import ChatMessage, EventType, Platform
from tests.conftest import FakeClock

SIM = Platform.SIMULATOR


def say(text="hello everyone!", user="alice", message_id=None, **extra) -> ChatMessage:
    return ChatMessage(platform=SIM, viewer_id=f"sim-{user}", username=user, message=text, message_id=message_id, **extra)


# ---------------------------------------------------------------------------- no duplicates across a restart
async def test_replayed_message_ids_after_a_restart_are_not_answered_again(make_app_client):
    client, app = await make_app_client()
    ids = [f"m{i}" for i in range(4)]
    for i, mid in enumerate(ids):  # different viewers and texts: identical repeats would be ignored as spam, not answered
        out = await client.post("/api/simulate/message", json={"username": f"viewer{i}", "message": f"hello everyone number {i}!", "message_id": mid})
        assert out.json()["action"] == "replied"
    # the platform re-delivers the same messages after the bot restarted (same database, new process)
    client2, app2 = await make_app_client()
    for i, mid in enumerate(ids):
        out = await client2.post("/api/simulate/message", json={"username": f"viewer{i}", "message": f"hello everyone number {i}!", "message_id": mid})
        assert out.json()["action"] == "duplicate", mid
    assert not app2.state.container.adapters[SIM].sent  # nothing was sent by the restarted bot


# ---------------------------------------------------------------------------- statistics survive a crash
async def test_statistics_are_saved_periodically_not_only_at_a_clean_shutdown(make_app_client):
    client, app = await make_app_client(stats_flush_interval_s=0.05)
    await client.post("/api/simulate/message", json={"username": "alice", "message": "hello everyone!"})
    session_id = app.state.container.session_manager.session_id
    await asyncio.sleep(0.4)  # the app is still running: no shutdown has happened
    con = sqlite3.connect(f"file:{app.state.container.settings.database_path}?mode=ro", uri=True)
    try:
        saved = {name for (name,) in con.execute("SELECT metric FROM session_statistics WHERE session_id = ?", (session_id,))}
    finally:
        con.close()
    assert "messages_received" in saved and "replies_sent" in saved


# ---------------------------------------------------------------------------- backlog after an outage
async def test_a_message_that_waited_too_long_in_the_queue_is_not_answered_late(make_app_client):
    client, app = await make_app_client(max_queue_age_s=120)
    c = app.state.container
    clock = FakeClock(1000.0)
    c.processor._clock = clock
    sim = c.adapters[SIM]
    await c.processor.stop()  # the worker is stuck (a hung AI, say) while viewers keep writing
    late = asyncio.create_task(c.processor.submit(say("what game is this?", message_id="late-1")))
    donation = asyncio.create_task(c.processor.submit(say("", user="bob", message_id="late-2", event_type=EventType.DONATION, amount="5.00")))
    await asyncio.sleep(0)
    clock.advance(300)  # five minutes pass
    fresh = asyncio.create_task(c.processor.submit(say("hello again!", user="carol", message_id="fresh-1")))
    await asyncio.sleep(0)
    await c.processor.start()
    late_result, donation_result, fresh_result = await late, await donation, await fresh
    assert late_result.action == "ignored" and late_result.reason == "stale_in_queue"  # kept in history, not answered
    assert donation_result.action == "replied"  # a thank-you is still welcome late
    assert fresh_result.action == "replied"
    assert len(sim.sent) == 2
    assert c.metrics.counters()["stale_dropped"] == 1
    assert await c.repo.message_exists("simulator", "late-1")  # it is still in the chat history


async def test_stale_proactive_tasks_are_dropped_too(make_app_client):
    from app.chat.models import Category

    client, app = await make_app_client(max_queue_age_s=120)
    c = app.state.container
    clock = FakeClock(1000.0)
    c.processor._clock = clock
    await c.processor.stop()
    task = asyncio.create_task(c.processor.submit_proactive(SIM, Category.PROACTIVE_GENERAL, still_ok=lambda: True))
    await asyncio.sleep(0)
    clock.advance(300)
    await c.processor.start()
    result = await task
    assert result.action == "skipped" and result.reason == "stale" and not c.adapters[SIM].sent


async def test_queue_overflow_keeps_the_newest_messages_and_the_bot_recovers(make_app_client):
    client, app = await make_app_client(incoming_queue_max=5)
    c = app.state.container
    await c.processor.stop()
    for i in range(1, 13):  # a raid: 12 messages arrive while nothing is being processed
        await c.processor.submit(say(f"message number {i}", user=f"user{i}", message_id=f"r{i}"), wait=False)
    assert c.processor.incoming.qsize() == 5 and c.processor.incoming.dropped == 7
    await c.processor.start()
    for _ in range(200):
        if c.processor.incoming.qsize() == 0 and await c.repo.message_exists("simulator", "r12"):
            break
        await asyncio.sleep(0.02)
    assert not await c.repo.message_exists("simulator", "r1")  # the oldest were dropped...
    assert await c.repo.message_exists("simulator", "r12")  # ...the newest were kept
    out = await client.post("/api/simulate/message", json={"username": "dave", "message": "are you still there?"})
    assert out.json()["action"] == "replied"  # and the bot carries on normally


# ---------------------------------------------------------------------------- bounded memory
async def test_the_per_viewer_reply_memory_is_pruned_so_it_cannot_grow_all_day(make_app_client):
    client, app = await make_app_client()
    c = app.state.container
    clock = FakeClock(100_000.0)
    c.processor._clock = clock
    for i in range(5200):  # viewers who were answered long ago
        c.processor._last_user_reply[("simulator", f"old{i}")] = clock() - 7200
    out = await client.post("/api/simulate/message", json={"username": "alice", "message": "hello everyone!"})
    assert out.json()["action"] == "replied"
    assert len(c.processor._last_user_reply) < 10  # old entries were dropped when the dictionary passed its limit

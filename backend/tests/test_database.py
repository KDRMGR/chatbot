from datetime import datetime, timezone

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import StatementError

from app.chat.normalizer import now_utc
from app.database.database import make_engine, make_session_factory, run_migrations
from app.database.repository import Repository
from app.personalities.defaults import default_personality_rows

EXPECTED_TABLES = {
    "stream_sessions", "viewers", "chat_messages", "bot_responses", "personalities", "bot_settings",
    "connection_logs", "session_statistics",
}


@pytest.fixture
async def repo(tmp_path):
    db = tmp_path / "t.db"
    await run_migrations(db)
    engine = make_engine(db)
    yield Repository(make_session_factory(engine)), engine
    engine.dispose()


async def test_migrations_create_every_spec_table_and_use_wal(repo):
    _, engine = repo
    with engine.connect() as conn:
        assert EXPECTED_TABLES <= set(inspect(conn).get_table_names())
        assert conn.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1
        assert conn.execute(text("PRAGMA busy_timeout")).scalar() == 30000


async def test_migrations_are_idempotent(tmp_path):
    db = tmp_path / "again.db"
    await run_migrations(db)
    await run_migrations(db)  # second run is a no-op, not an error


async def test_viewer_upsert_tracks_new_then_returning(repo):
    r, _ = repo
    first = await r.upsert_viewer("simulator", "u1", "alice", now_utc())
    second = await r.upsert_viewer("simulator", "u1", "alice", now_utc())
    other = await r.upsert_viewer("youtube", "u1", "alice", now_utc())  # same id on another platform is a different viewer
    assert (first.is_new, first.message_count) == (True, 1)
    assert (second.is_new, second.message_count, second.pk) == (False, 2, first.pk)
    assert other.is_new and other.pk != first.pk


async def test_platform_message_id_is_unique(repo):
    r, _ = repo
    kw = dict(session_id=None, platform="youtube", viewer_pk=None, message="hi", category="greeting",
              filter_reason=None, timestamp=now_utc())
    assert await r.add_chat_message(platform_message_id="abc", **{**kw, "platform": "youtube"}) is not None
    assert await r.add_chat_message(platform_message_id="abc", **{**kw, "platform": "youtube"}) is None
    assert await r.message_exists("youtube", "abc") and not await r.message_exists("kick", "abc")
    # NULL ids never collide
    assert await r.add_chat_message(platform_message_id=None, **kw) is not None
    assert await r.add_chat_message(platform_message_id=None, **kw) is not None


async def test_recent_activity_joins_responses(repo):
    r, _ = repo
    v = await r.upsert_viewer("simulator", "u1", "alice", now_utc())
    mid = await r.add_chat_message(session_id=None, platform="simulator", platform_message_id=None,
                                   viewer_pk=v.pk, message="hello", category="greeting", filter_reason=None,
                                   timestamp=now_utc())
    rid = await r.add_bot_response(platform="simulator", original_message_id=mid, personality="ChatBuddy",
                                   response="Welcome!", source="ai", model="m", latency_ms=12, now=now_utc())
    await r.set_delivery_status(rid, "sent")
    row = (await r.recent_activity(10))[-1]
    assert row["username"] == "alice" and row["response"]["text"] == "Welcome!"
    assert row["response"]["delivery_status"] == "sent"


async def test_seeding_never_overwrites_edited_personalities(repo):
    r, engine = repo
    assert await r.seed_personalities(default_personality_rows(), now_utc()) == 5
    with engine.begin() as conn:
        conn.execute(text("UPDATE personalities SET system_prompt='EDITED', enabled=0 WHERE name='HypeBot'"))
    assert await r.seed_personalities(default_personality_rows(), now_utc()) == 0
    hype = next(p for p in await r.list_personalities() if p["name"] == "HypeBot")
    assert hype["system_prompt"] == "EDITED" and hype["enabled"] is False


async def test_stale_running_sessions_are_marked_crashed(repo):
    r, _ = repo
    await r.start_session(now_utc())
    assert await r.mark_stale_sessions_crashed(now_utc()) == 1
    assert await r.mark_stale_sessions_crashed(now_utc()) == 0


async def test_settings_roundtrip(repo):
    r, _ = repo
    assert await r.get_setting("k") is None
    await r.set_setting("k", "v1", now_utc())
    await r.set_setting("k", "v2", now_utc())
    assert await r.get_setting("k") == "v2"


async def test_naive_datetimes_are_refused(repo):
    r, _ = repo
    with pytest.raises(StatementError, match="naive datetime"):
        await r.start_session(datetime.now())  # naive: classic timezone bug, rejected loudly
    await r.start_session(datetime.now(timezone.utc))

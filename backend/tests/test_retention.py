from datetime import timedelta

import pytest

from app.chat.normalizer import now_utc
from app.config import Settings
from app.database.database import make_engine, make_session_factory, run_migrations
from app.database.repository import Repository
from app.services.retention import RetentionService


@pytest.fixture
async def repo(tmp_path):
    db = tmp_path / "t.db"
    await run_migrations(db)
    engine = make_engine(db)
    yield Repository(make_session_factory(engine))
    engine.dispose()


async def add(repo, platform: str, user: str, when, text: str) -> None:
    viewer = await repo.upsert_viewer(platform, user, user.upper(), when)
    mid = await repo.add_chat_message(session_id=None, platform=platform, platform_message_id=None, viewer_pk=viewer.pk,
                                      message=text, category="general", filter_reason=None, timestamp=when)
    await repo.add_bot_response(platform=platform, original_message_id=mid, personality="ChatBuddy",
                                response=f"Hi {user.upper()}!", source="ai", model="m", latency_ms=1, now=when)


async def seed(repo):
    now = now_utc()
    await add(repo, "youtube", "old_yt", now - timedelta(days=40), "old youtube message")
    await add(repo, "youtube", "new_yt", now - timedelta(days=5), "recent youtube message")
    await add(repo, "simulator", "old_sim", now - timedelta(days=40), "old simulator message")


async def remaining(repo) -> set[str]:
    return {row["message"] for row in await repo.recent_activity(100)}


async def test_only_old_youtube_data_is_purged(repo):
    await seed(repo)
    counts = await repo.purge_platform_data("youtube", now_utc() - timedelta(days=30))
    assert counts == {"bot_responses": 1, "chat_messages": 1, "viewers": 1}
    assert await remaining(repo) == {"recent youtube message", "old simulator message"}
    rows = {r["message"]: r for r in await repo.recent_activity(100)}
    assert rows["recent youtube message"]["response"]["text"] == "Hi NEW_YT!"  # its reply survives with it


async def test_purging_everything_for_a_platform_leaves_other_platforms_alone(repo):
    await seed(repo)
    counts = await repo.purge_platform_data("youtube")
    assert counts == {"bot_responses": 2, "chat_messages": 2, "viewers": 2}
    assert await remaining(repo) == {"old simulator message"}


async def test_bot_replies_that_contain_viewer_names_are_deleted_too(repo):
    await seed(repo)
    await repo.purge_platform_data("youtube")
    rows = await repo.recent_activity(100)
    assert all("OLD_YT" not in str(r) and "NEW_YT" not in str(r) for r in rows)


async def test_retention_service_uses_the_configured_window(repo, tmp_path):
    await seed(repo)
    service = RetentionService(repo, Settings(state_dir=tmp_path, youtube_data_retention_days=3))
    counts = await service.run_once()
    assert counts["chat_messages"] == 2  # both youtube messages are older than 3 days
    assert await remaining(repo) == {"old simulator message"}


async def test_default_window_is_the_30_day_policy_limit(repo, tmp_path):
    await seed(repo)
    assert Settings(state_dir=tmp_path).youtube_data_retention_days == 30
    counts = await RetentionService(repo, Settings(state_dir=tmp_path)).run_once()
    assert counts["chat_messages"] == 1  # only the 40-day-old one


async def test_running_twice_is_harmless(repo, tmp_path):
    await seed(repo)
    service = RetentionService(repo, Settings(state_dir=tmp_path))
    await service.run_once()
    assert not any((await service.run_once()).values())

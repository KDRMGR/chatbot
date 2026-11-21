"""All database access. Every method is one short transaction run in a worker thread, so a database call never
blocks the event loop and a slow AI call can never hold a database lock."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, TypeVar

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from . import models as m

T = TypeVar("T")


@dataclass(frozen=True)
class ViewerInfo:
    pk: int
    is_new: bool
    message_count: int
    username: str


class Repository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sf = session_factory

    # ------------------------------------------------------------------ plumbing
    def _execute(self, work: Callable[[Session], T]) -> T:
        with self._sf() as session:
            try:
                result = work(session)
                session.commit()
                return result
            except BaseException:
                session.rollback()
                raise

    async def _run(self, work: Callable[[Session], T]) -> T:
        return await asyncio.to_thread(self._execute, work)

    # ------------------------------------------------------------------ sessions
    async def start_session(self, now: datetime, platform_configuration: dict | None = None) -> int:
        def work(s: Session) -> int:
            row = m.StreamSession(started_at=now, status="running", platform_configuration=platform_configuration)
            s.add(row)
            s.flush()
            return row.id

        return await self._run(work)

    async def end_session(self, session_id: int, now: datetime, status: str = "ended") -> None:
        def work(s: Session) -> None:
            row = s.get(m.StreamSession, session_id)
            if row is not None:
                row.ended_at, row.status = now, status
                row.duration_seconds = int((now - row.started_at).total_seconds())

        await self._run(work)

    async def mark_stale_sessions_crashed(self, now: datetime) -> int:
        """A session still 'running' at startup means the previous run did not shut down cleanly."""
        def work(s: Session) -> int:
            rows = s.execute(select(m.StreamSession).where(m.StreamSession.status == "running")).scalars().all()
            for row in rows:
                row.status, row.ended_at = "crashed", now
                row.duration_seconds = int((now - row.started_at).total_seconds())
            return len(rows)

        return await self._run(work)

    async def add_statistic(self, session_id: int, metric: str, value: float, now: datetime) -> None:
        def work(s: Session) -> None:
            s.add(m.SessionStatistic(session_id=session_id, metric=metric, metric_value=value, recorded_at=now))

        await self._run(work)

    async def list_sessions(self, limit: int = 30) -> list[dict[str, Any]]:
        """Newest sessions first, each with the LAST value saved for every statistic."""
        def work(s: Session) -> list[dict[str, Any]]:
            rows = s.execute(select(m.StreamSession).order_by(m.StreamSession.id.desc()).limit(limit)).scalars().all()
            out: list[dict[str, Any]] = []
            for row in rows:
                stats: dict[str, float] = {}
                stat_rows = s.execute(
                    select(m.SessionStatistic.metric, m.SessionStatistic.metric_value)
                    .where(m.SessionStatistic.session_id == row.id).order_by(m.SessionStatistic.id)
                ).all()
                for metric, value in stat_rows:  # oldest to newest: the newest value of each metric wins
                    stats[metric] = value
                out.append({
                    "id": row.id, "started_at": row.started_at.isoformat(),
                    "ended_at": row.ended_at.isoformat() if row.ended_at else None,
                    "duration_seconds": row.duration_seconds, "status": row.status,
                    "platform_configuration": row.platform_configuration or {}, "stats": stats,
                })
            return out

        return await self._run(work)

    async def list_connection_logs(self, limit: int = 100, platform: str | None = None) -> list[dict[str, Any]]:
        def work(s: Session) -> list[dict[str, Any]]:
            stmt = select(m.ConnectionLog).order_by(m.ConnectionLog.id.desc()).limit(limit)
            if platform:
                stmt = stmt.where(m.ConnectionLog.platform == platform)
            return [
                {"id": r.id, "platform": r.platform, "event": r.event_type, "message": r.message,
                 "timestamp": r.timestamp.isoformat()}
                for r in s.execute(stmt).scalars()
            ]

        return await self._run(work)

    # ------------------------------------------------------------------ viewers / messages
    async def message_exists(self, platform: str, platform_message_id: str) -> bool:
        def work(s: Session) -> bool:
            stmt = (
                select(m.ChatMessage.id)
                .where(m.ChatMessage.platform == platform, m.ChatMessage.platform_message_id == platform_message_id)
                .limit(1)
            )
            return s.execute(stmt).first() is not None

        return await self._run(work)

    async def upsert_viewer(self, platform: str, platform_user_id: str, username: str, now: datetime) -> ViewerInfo:
        def work(s: Session) -> ViewerInfo:
            for _ in range(2):  # second pass only if two writers raced to create the same viewer
                viewer = s.execute(
                    select(m.Viewer).where(m.Viewer.platform == platform, m.Viewer.platform_user_id == platform_user_id)
                ).scalar_one_or_none()
                if viewer is None:
                    viewer = m.Viewer(
                        platform=platform, platform_user_id=platform_user_id, username=username,
                        first_seen=now, last_seen=now, message_count=1,
                    )
                    s.add(viewer)
                    try:
                        s.flush()
                    except IntegrityError:
                        s.rollback()
                        continue
                    return ViewerInfo(viewer.id, True, 1, username)
                viewer.last_seen, viewer.username = now, username
                viewer.message_count += 1
                s.flush()
                return ViewerInfo(viewer.id, False, viewer.message_count, username)
            raise RuntimeError("could not upsert viewer")

        return await self._run(work)

    async def add_chat_message(
        self, *, session_id: int | None, platform: str, platform_message_id: str | None, viewer_pk: int | None,
        message: str, category: str | None, filter_reason: str | None, timestamp: datetime, is_bot: bool = False,
    ) -> int | None:
        """Returns the new row id, or None if (platform, platform_message_id) was already stored."""
        def work(s: Session) -> int | None:
            row = m.ChatMessage(
                session_id=session_id, platform=platform, platform_message_id=platform_message_id,
                viewer_id=viewer_pk, message=message, category=category, filter_reason=filter_reason,
                timestamp=timestamp, is_bot_message=is_bot,
            )
            s.add(row)
            try:
                s.flush()
            except IntegrityError:
                s.rollback()
                return None
            return row.id

        return await self._run(work)

    async def add_bot_response(
        self, *, platform: str, original_message_id: int | None, personality: str | None, response: str,
        source: str | None, model: str | None, latency_ms: int | None, now: datetime,
    ) -> int:
        def work(s: Session) -> int:
            row = m.BotResponse(
                platform=platform, original_message_id=original_message_id, personality=personality,
                response=response, created_at=now, delivery_status="pending", source=source, model=model,
                latency_ms=latency_ms,
            )
            s.add(row)
            s.flush()
            return row.id

        return await self._run(work)

    async def set_delivery_status(self, response_id: int, status: str, error: str | None = None) -> None:
        def work(s: Session) -> None:
            row = s.get(m.BotResponse, response_id)
            if row is not None:
                row.delivery_status = status
                row.delivery_error = error[:200] if error else None

        await self._run(work)

    async def recent_activity(self, limit: int = 50) -> list[dict[str, Any]]:
        """Newest ``limit`` chat messages (oldest first), each with the bot response to it, if any."""
        def work(s: Session) -> list[dict[str, Any]]:
            stmt = (
                select(m.ChatMessage, m.Viewer.username)
                .join(m.Viewer, m.Viewer.id == m.ChatMessage.viewer_id, isouter=True)
                .order_by(m.ChatMessage.id.desc())
                .limit(limit)
            )
            rows = s.execute(stmt).all()
            ids = [r[0].id for r in rows]
            responses: dict[int, m.BotResponse] = {}
            if ids:
                found = s.execute(select(m.BotResponse).where(m.BotResponse.original_message_id.in_(ids))).scalars()
                responses = {r.original_message_id: r for r in found if r.original_message_id is not None}
            out: list[dict[str, Any]] = []
            for msg, username in reversed(rows):
                resp = responses.get(msg.id)
                out.append({
                    "id": msg.id, "platform": msg.platform,
                    "username": username or ("bot" if msg.is_bot_message else None),
                    "message": msg.message, "category": msg.category, "filter_reason": msg.filter_reason,
                    "timestamp": msg.timestamp.isoformat(), "is_bot_message": msg.is_bot_message,
                    "response": None if resp is None else {
                        "text": resp.response, "personality": resp.personality, "source": resp.source,
                        "model": resp.model, "latency_ms": resp.latency_ms, "delivery_status": resp.delivery_status,
                    },
                })
            return out

        return await self._run(work)

    async def count_messages(self) -> int:
        return await self._run(lambda s: int(s.execute(select(func.count()).select_from(m.ChatMessage)).scalar_one()))

    # ------------------------------------------------------------------ data retention
    async def purge_platform_data(self, platform: str, before: datetime | None = None) -> dict[str, int]:
        """Delete stored data that came from ``platform``: all of it, or only records older than ``before``.

        Required by Google's YouTube developer policy (stored API data must be deleted or refreshed within 30 days, and
        deleted on revocation). Bot responses go first because they can contain a viewer's display name.
        """
        def work(s: Session) -> dict[str, int]:
            resp = delete(m.BotResponse).where(m.BotResponse.platform == platform)
            msgs = delete(m.ChatMessage).where(m.ChatMessage.platform == platform)
            viewers = delete(m.Viewer).where(m.Viewer.platform == platform)
            if before is not None:
                resp = resp.where(m.BotResponse.created_at < before)
                msgs = msgs.where(m.ChatMessage.timestamp < before)
                viewers = viewers.where(m.Viewer.last_seen < before)
            return {
                "bot_responses": s.execute(resp).rowcount or 0,
                "chat_messages": s.execute(msgs).rowcount or 0,
                "viewers": s.execute(viewers).rowcount or 0,
            }

        return await self._run(work)

    # ------------------------------------------------------------------ personalities
    async def seed_personalities(self, defaults: list[dict[str, Any]], now: datetime) -> int:
        """Insert default personalities that don't exist yet. Existing rows (possibly edited) are untouched."""
        def work(s: Session) -> int:
            existing = set(s.execute(select(m.Personality.name)).scalars().all())
            added = 0
            for d in defaults:
                if d["name"] not in existing:
                    s.add(m.Personality(updated_at=now, **d))
                    added += 1
            return added

        return await self._run(work)

    async def list_personalities(self) -> list[dict[str, Any]]:
        def work(s: Session) -> list[dict[str, Any]]:
            rows = s.execute(select(m.Personality).order_by(m.Personality.id)).scalars().all()
            return [
                {
                    "name": r.name, "description": r.description, "system_prompt": r.system_prompt,
                    "enabled": r.enabled, "cooldown_seconds": r.cooldown_seconds, "frequency": r.frequency,
                    "priority": r.priority, "categories": list(r.categories or []),
                    "configuration": dict(r.configuration or {}),
                }
                for r in rows
            ]

        return await self._run(work)

    async def update_bot_response_text(self, response_id: int, text: str) -> None:
        """The streamer edited a held message before approving it."""
        def work(s: Session) -> None:
            row = s.get(m.BotResponse, response_id)
            if row is not None:
                row.response = text

        await self._run(work)

    async def update_personality(self, name: str, fields: dict[str, Any], now: datetime) -> bool:
        """Persist edited fields (cooldown_seconds, frequency, priority, system_prompt, description) of one personality."""
        allowed = {"cooldown_seconds", "frequency", "priority", "system_prompt", "description"}
        if not fields or not set(fields) <= allowed:
            raise ValueError(f"fields must be a non-empty subset of {sorted(allowed)}")

        def work(s: Session) -> bool:
            row = s.execute(select(m.Personality).where(m.Personality.name == name)).scalar_one_or_none()
            if row is None:
                return False
            for key, value in fields.items():
                setattr(row, key, value)
            row.updated_at = now
            return True

        return await self._run(work)

    async def set_personality_enabled(self, name: str, enabled: bool, now: datetime) -> bool:
        """Persist a dashboard on/off switch (it survives restarts because seeding never overwrites existing rows)."""
        def work(s: Session) -> bool:
            row = s.execute(select(m.Personality).where(m.Personality.name == name)).scalar_one_or_none()
            if row is None:
                return False
            row.enabled, row.updated_at = enabled, now
            return True

        return await self._run(work)

    # ------------------------------------------------------------------ settings / logs
    async def get_setting(self, key: str) -> str | None:
        def work(s: Session) -> str | None:
            row = s.execute(select(m.BotSetting).where(m.BotSetting.setting_key == key)).scalar_one_or_none()
            return None if row is None else row.setting_value

        return await self._run(work)

    async def set_setting(self, key: str, value: str, now: datetime) -> None:
        def work(s: Session) -> None:
            row = s.execute(select(m.BotSetting).where(m.BotSetting.setting_key == key)).scalar_one_or_none()
            if row is None:
                s.add(m.BotSetting(setting_key=key, setting_value=value, updated_at=now))
            else:
                row.setting_value, row.updated_at = value, now

        await self._run(work)

    async def log_connection(self, platform: str, event_type: str, message: str | None, now: datetime) -> None:
        def work(s: Session) -> None:
            s.add(m.ConnectionLog(platform=platform, event_type=event_type, message=message, timestamp=now))

        await self._run(work)

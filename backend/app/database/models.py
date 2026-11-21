"""SQLAlchemy models (SQLite). Datetimes are always timezone-aware UTC in Python and stored as naive UTC."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator):
    """Refuses naive datetimes (a classic source of silent timezone bugs) and returns aware UTC values."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime passed to the database; use timezone-aware UTC")
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        return None if value is None else value.replace(tzinfo=timezone.utc)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class StreamSession(Base):
    __tablename__ = "stream_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="running")  # running | ended | crashed
    platform_configuration: Mapped[dict | None] = mapped_column(JSON)


class Viewer(Base):
    __tablename__ = "viewers"
    __table_args__ = (UniqueConstraint("platform", "platform_user_id", name="uq_viewer_platform_user"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[str] = mapped_column(String(20))
    platform_user_id: Mapped[str] = mapped_column(String(128))
    username: Mapped[str] = mapped_column(String(64))
    first_seen: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    message_count: Mapped[int] = mapped_column(Integer, default=0)


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    __table_args__ = (
        # Unique per platform message id => the same message can never be stored (or answered) twice.
        # NULL ids (bot messages, simulator messages without ids) do not collide in SQLite.
        UniqueConstraint("platform", "platform_message_id", name="uq_message_platform_id"),
        Index("ix_chat_messages_timestamp", "timestamp"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int | None] = mapped_column(ForeignKey("stream_sessions.id", ondelete="SET NULL"), index=True)
    platform: Mapped[str] = mapped_column(String(20))
    platform_message_id: Mapped[str | None] = mapped_column(String(128))
    viewer_id: Mapped[int | None] = mapped_column(ForeignKey("viewers.id", ondelete="SET NULL"), index=True)
    message: Mapped[str] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(String(32))
    filter_reason: Mapped[str | None] = mapped_column(String(64))
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    is_bot_message: Mapped[bool] = mapped_column(Boolean, default=False)


class BotResponse(Base):
    __tablename__ = "bot_responses"

    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[str] = mapped_column(String(20))
    original_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("chat_messages.id", ondelete="SET NULL"), index=True
    )
    personality: Mapped[str | None] = mapped_column(String(64))
    response: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    delivery_status: Mapped[str] = mapped_column(String(20), default="pending")  # pending | sent | failed
    delivery_error: Mapped[str | None] = mapped_column(String(200))
    source: Mapped[str | None] = mapped_column(String(16))  # ai | rule | command
    model: Mapped[str | None] = mapped_column(String(64))
    latency_ms: Mapped[int | None] = mapped_column(Integer)


class Personality(Base):
    __tablename__ = "personalities"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    system_prompt: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    cooldown_seconds: Mapped[int] = mapped_column(Integer, default=30)
    frequency: Mapped[float] = mapped_column(Float, default=1.0)  # relative weight when several fit
    priority: Mapped[int] = mapped_column(Integer, default=1)
    categories: Mapped[list | None] = mapped_column(JSON)
    configuration: Mapped[dict | None] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class BotSetting(Base):
    __tablename__ = "bot_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    setting_key: Mapped[str] = mapped_column(String(100), unique=True)
    setting_value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class ConnectionLog(Base):
    __tablename__ = "connection_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[str] = mapped_column(String(20))
    event_type: Mapped[str] = mapped_column(String(32))
    message: Mapped[str | None] = mapped_column(Text)
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class SessionStatistic(Base):
    __tablename__ = "session_statistics"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("stream_sessions.id", ondelete="CASCADE"), index=True)
    metric: Mapped[str] = mapped_column(String(64))
    metric_value: Mapped[float] = mapped_column(Float)
    recorded_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

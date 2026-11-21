"""Platform-neutral chat types. Every platform adapter normalises into ChatMessage."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from .normalizer import MAX_MESSAGE_CHARS, sanitize_text


class Platform(str, Enum):
    YOUTUBE = "youtube"
    KICK = "kick"
    SIMULATOR = "simulator"


class EventType(str, Enum):
    MESSAGE = "message"
    DONATION = "donation"
    MEMBERSHIP = "membership"


class Category(str, Enum):
    GREETING = "greeting"
    QUESTION = "question"
    GAMEPLAY_SUGGESTION = "gameplay_suggestion"
    JOKE = "joke"
    GENERAL = "general"
    SPAM = "spam"
    ABUSE = "abuse"
    REPEATED = "repeated"
    BOT_COMMAND = "bot_command"
    EVENT = "event"
    # Internal categories for bot-initiated messages (used by the Phase 5 scheduler / previews)
    PROACTIVE_QUIET = "proactive_quiet"
    PROACTIVE_GENERAL = "proactive_general"
    PROACTIVE_QUIZ = "proactive_quiz"
    PROACTIVE_HYPE = "proactive_hype"


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class ChatMessage:
    """A single normalised incoming chat event. Text fields are sanitised on construction, so no code path
    can hold an unsanitised viewer string. Invalid input raises ValueError."""

    platform: Platform
    viewer_id: str
    username: str
    message: str = ""
    timestamp: datetime = field(default_factory=_now)
    message_id: str | None = None
    event_type: EventType = EventType.MESSAGE
    is_bot_self: bool = False  # echo of our own message coming back from the platform: must be ignored
    is_streamer: bool = False
    via_voice: bool = False  # the streamer SAID this (hold-to-speak, turned into text): it never appeared in the platform's chat
    amount: str | None = None  # donation amount text, display only

    def __post_init__(self) -> None:
        self.platform = Platform(self.platform)
        self.event_type = EventType(self.event_type)
        self.viewer_id = str(self.viewer_id).strip()
        if not 1 <= len(self.viewer_id) <= 128:
            raise ValueError("viewer_id must be 1-128 characters")
        self.username = sanitize_text(self.username if self.username is not None else "", 64) or "viewer"
        self.message = sanitize_text(self.message if self.message is not None else "", MAX_MESSAGE_CHARS)
        if self.message_id is not None:
            self.message_id = str(self.message_id)
            if not 1 <= len(self.message_id) <= 128:
                raise ValueError("message_id must be 1-128 characters")
        if self.amount is not None:
            self.amount = sanitize_text(self.amount, 32)
        self.timestamp = (
            self.timestamp.replace(tzinfo=timezone.utc)
            if self.timestamp.tzinfo is None
            else self.timestamp.astimezone(timezone.utc)
        )

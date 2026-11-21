"""Manual approval of the bot's messages (spec dashboard section B).

With approval switched on, every message the bot composes (except answers to its own ``!commands``) waits here instead of
being sent. The streamer approves it (optionally after editing the text) or rejects it; a message nobody decides about
expires after ``approval_timeout_s``, because an answer minutes late is worse than none. Nothing here sends anything: the
processor does that, with all its usual checks, when a message is approved.
"""
from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..chat.models import Category, ChatMessage, Platform


@dataclass
class PendingReply:
    platform: Platform
    text: str
    personality: str | None
    source: str
    model: str | None
    latency_ms: int | None
    response_id: int  # the database row that records this reply (status "awaiting_approval")
    category: Category | None = None
    reply_to: ChatMessage | None = None
    viewer_id: str | None = None
    id: int = 0
    created_at: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)

    def public(self, now: float, ttl_s: float) -> dict[str, Any]:
        return {
            "id": self.id, "platform": self.platform.value, "text": self.text, "personality": self.personality,
            "source": self.source, "model": self.model, "category": self.category.value if self.category else None,
            "viewer": self.reply_to.username if self.reply_to else None,
            "viewer_message": (self.reply_to.message[:300] if self.reply_to else None),
            "age_s": round(now - self.created_at), "expires_in_s": max(0, round(self.created_at + ttl_s - now)),
        }


class ApprovalQueue:
    def __init__(self, max_size: int = 50, ttl_s: float = 120.0, clock: Callable[[], float] = time.monotonic) -> None:
        self._max, self.ttl_s, self._clock = max_size, ttl_s, clock
        self._items: dict[int, PendingReply] = {}
        self._ids = itertools.count(1)

    def __len__(self) -> int:
        return len(self._items)

    def add(self, entry: PendingReply) -> list[PendingReply]:
        """Queue a reply; returns the oldest entries pushed out to keep the queue bounded."""
        entry.id, entry.created_at = next(self._ids), self._clock()
        self._items[entry.id] = entry
        evicted: list[PendingReply] = []
        while len(self._items) > self._max:
            oldest = next(iter(self._items))
            evicted.append(self._items.pop(oldest))
        return evicted

    def expire(self) -> list[PendingReply]:
        now = self._clock()
        gone = [e for e in self._items.values() if now - e.created_at >= self.ttl_s]
        for entry in gone:
            del self._items[entry.id]
        return gone

    def peek(self, entry_id: int) -> PendingReply | None:
        return self._items.get(entry_id)

    def take(self, entry_id: int) -> PendingReply | None:
        return self._items.pop(entry_id, None)

    def items(self) -> list[PendingReply]:
        return list(self._items.values())

    def public(self) -> list[dict[str, Any]]:
        now = self._clock()
        return [e.public(now, self.ttl_s) for e in self._items.values()]

    def drain(self) -> list[PendingReply]:
        items = list(self._items.values())
        self._items.clear()
        return items

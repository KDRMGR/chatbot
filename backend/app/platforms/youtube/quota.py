"""YouTube Data API quota accounting.

Costs and the daily reset come from Google's documentation (read 2026-11-20): default 10,000 units/day for all
methods used here; the quota resets at midnight Pacific Time; liveChatMessages.list/streamList 5, insert 20,
liveBroadcasts.list / videos.list / channels.list 1. ``search.list`` has its own separate bucket (100 calls/day),
so it is counted separately and does not consume the main pool.

The tracker only knows about calls made by THIS app. Anything else using the same Cloud project is invisible to it,
and a server-side ``quotaExceeded`` error always wins (``mark_exhausted``).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

from ...chat.normalizer import now_utc

PACIFIC = ZoneInfo("America/Los_Angeles")

COSTS: dict[str, int] = {
    "liveChatMessages.list": 5,
    "liveChatMessages.stream": 5,  # per connection as documented; real billing of a long stream is UNVERIFIED
    "liveChatMessages.insert": 20,
    "liveBroadcasts.list": 1,
    "videos.list": 1,
    "channels.list": 1,
}
SEARCH_DAILY_CALLS = 100  # search.list has its own bucket


@dataclass
class QuotaTracker:
    daily_limit: int = 10_000
    send_reserve_fraction: float = 0.05  # keep this share of the day for READING chat; sends stop first
    clock: Callable[[], datetime] = field(default=now_utc, repr=False)

    def __post_init__(self) -> None:
        self._day: date = self._today()
        self._used = 0
        self._read_used = 0  # units spent on everything except sending: lets the poll pace protect the send budget
        self._search_calls = 0
        self._exhausted = False

    def _today(self) -> date:
        return self.clock().astimezone(PACIFIC).date()

    def _roll(self) -> None:
        today = self._today()
        if today != self._day:
            self._day, self._used, self._read_used, self._search_calls, self._exhausted = today, 0, 0, 0, False

    # ------------------------------------------------------------------ accounting
    def record(self, method: str) -> None:
        self._roll()
        if method == "search.list":
            self._search_calls += 1
            return
        self._used += COSTS[method]
        if method != "liveChatMessages.insert":
            self._read_used += COSTS[method]

    @property
    def reads_used(self) -> int:
        self._roll()
        return self._read_used

    def mark_exhausted(self) -> None:
        """Google said quotaExceeded: believe it, regardless of our own count."""
        self._roll()
        self._exhausted = True
        self._used = max(self._used, self.daily_limit)

    @property
    def used(self) -> int:
        self._roll()
        return self._used

    @property
    def remaining(self) -> int:
        return max(0, self.daily_limit - self.used)

    @property
    def exhausted(self) -> bool:
        self._roll()
        return self._exhausted

    # ------------------------------------------------------------------ decisions
    def can_read(self, method: str) -> bool:
        self._roll()
        if self._exhausted:
            return False
        if method == "search.list":
            return self._search_calls < SEARCH_DAILY_CALLS
        return self._used + COSTS[method] <= self.daily_limit

    def can_send(self) -> bool:
        """Sending is refused while it would eat into the share reserved for reading chat."""
        self._roll()
        if self._exhausted:
            return False
        reserve = int(self.daily_limit * self.send_reserve_fraction)
        return self.daily_limit - self._used - COSTS["liveChatMessages.insert"] >= reserve

    def seconds_until_reset(self) -> float:
        now = self.clock().astimezone(PACIFIC)
        midnight = datetime.combine(now.date() + timedelta(days=1), time.min, tzinfo=PACIFIC)
        return max(0.0, (midnight - now).total_seconds())

    # ------------------------------------------------------------------ persistence (survive restarts mid-day)
    def dump(self) -> dict[str, Any]:
        self._roll()
        return {"date": self._day.isoformat(), "used": self._used, "read_used": self._read_used,
                "search_calls": self._search_calls, "exhausted": self._exhausted}

    def load(self, data: dict[str, Any]) -> bool:
        """Restore a saved count only if it is from the current Pacific-time day."""
        try:
            saved = date.fromisoformat(data["date"])
        except (KeyError, ValueError, TypeError):
            return False
        self._roll()
        if saved != self._day:
            return False
        self._used = int(data.get("used", 0))
        self._read_used = int(data.get("read_used", self._used))  # older saves: assume it was all reading (conservative)
        self._search_calls = int(data.get("search_calls", 0))
        self._exhausted = bool(data.get("exhausted", False))
        return True

    def snapshot(self) -> dict[str, Any]:
        self._roll()
        return {
            "date_pt": self._day.isoformat(), "used": self._used, "reads_used": self._read_used, "remaining": self.remaining,
            "limit": self.daily_limit, "exhausted": self._exhausted, "resets_in_s": int(self.seconds_until_reset()),
            "search_calls": self._search_calls,
        }

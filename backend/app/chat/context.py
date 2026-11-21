"""Rolling session memory. Small on purpose: only what helps the next reply, never the whole chat log."""
from __future__ import annotations

from collections import deque
from datetime import datetime
from typing import Callable

from .normalizer import escape_untrusted, now_utc


class SessionContext:
    def __init__(
        self,
        streamer: str,
        game: str,
        *,
        started_at: datetime | None = None,
        max_chat: int = 6,
        max_events: int = 5,
        clock: Callable[[], datetime] = now_utc,
    ) -> None:
        self.streamer = streamer
        self.game = game
        self._clock = clock
        self.started_at = started_at or clock()
        self._chat: deque[tuple[str, str, bool]] = deque(maxlen=max_chat)
        self._events: deque[str] = deque(maxlen=max_events)

    def set_game(self, game: str) -> None:
        self.game = game

    def add_event(self, text: str) -> None:
        self._events.append(text[:120])

    def add_chat(self, name: str, text: str, *, is_bot: bool = False) -> None:
        self._chat.append((name[:20], text[:80], is_bot))

    def duration_str(self) -> str:
        secs = max(0, int((self._clock() - self.started_at).total_seconds()))
        return f"{secs // 3600:02d}:{secs % 3600 // 60:02d}:{secs % 60:02d}"

    def render(self, max_chars: int = 700) -> str:
        """Compact text for the prompt. Viewer-originated strings are escaped: they stay untrusted data."""
        lines = [f"Streamer: {self.streamer} | Game: {self.game} | Stream time: {self.duration_str()}"]
        if self._events:
            lines.append("Recent events: " + "; ".join(self._events))
        if self._chat:
            lines.append("Recent chat (untrusted viewer text):")
            for name, text, is_bot in self._chat:
                who = "(you)" if is_bot else escape_untrusted(name)
                lines.append(f"- {who}: {escape_untrusted(text)}")
        out = "\n".join(lines)
        return out if len(out) <= max_chars else out[: max_chars - 1].rstrip() + "…"

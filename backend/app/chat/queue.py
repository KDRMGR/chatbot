"""Bounded queue and outgoing rate limiter. Neither can grow without limit or block a producer."""
from __future__ import annotations

import asyncio
import itertools
import time
from collections import deque
from typing import Callable, Generic, TypeVar

T = TypeVar("T")


class BoundedPriorityQueue(Generic[T]):
    """Priority queue (lower number = more important) with a hard capacity.

    ``put_nowait`` never blocks. When full, the least important item is dropped; among equals the OLDEST is
    dropped, because a stale chat message is worth less than a fresh one. The dropped item is returned so
    the caller can resolve anything waiting on it.
    """

    def __init__(self, maxsize: int) -> None:
        if maxsize < 1:
            raise ValueError("maxsize must be >= 1")
        self._maxsize = maxsize
        self._items: list[tuple[int, int, T]] = []
        self._seq = itertools.count()
        self._wakeup = asyncio.Event()
        self.dropped = 0

    def qsize(self) -> int:
        return len(self._items)

    def put_nowait(self, item: T, priority: int = 1) -> T | None:
        evicted: T | None = None
        if len(self._items) >= self._maxsize:
            # worst = highest priority number, then oldest (lowest seq)
            worst = max(self._items, key=lambda e: (e[0], -e[1]))
            if (priority, 0) > (worst[0], 0):
                self.dropped += 1
                return item  # the new item is the least important: reject it
            self._items.remove(worst)
            evicted = worst[2]
            self.dropped += 1
        self._items.append((priority, next(self._seq), item))
        self._wakeup.set()
        return evicted

    async def get(self) -> T:
        while not self._items:
            self._wakeup.clear()
            await self._wakeup.wait()
        entry = min(self._items, key=lambda e: (e[0], e[1]))
        self._items.remove(entry)
        return entry[2]

    def drain(self) -> list[T]:
        items = [e[2] for e in sorted(self._items, key=lambda e: (e[0], e[1]))]
        self._items.clear()
        return items


class RateLimiter:
    """Sliding-window limiter with a minimum gap. ``record`` only when a message was actually sent."""

    def __init__(
        self,
        min_gap_s: float,
        max_per_window: int,
        window_s: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._min_gap = min_gap_s
        self._max = max_per_window
        self._window = window_s
        self._clock = clock
        self._sent: deque[float] = deque()

    def reconfigure(self, min_gap_s: float, max_per_window: int, window_s: float) -> None:
        """Apply new limits to a running limiter without forgetting what was already sent in the current window."""
        self._min_gap, self._max, self._window = min_gap_s, max_per_window, window_s

    def _prune(self, now: float) -> None:
        while self._sent and self._sent[0] <= now - self._window:
            self._sent.popleft()

    def retry_after(self) -> float:
        now = self._clock()
        self._prune(now)
        wait = 0.0
        if self._sent and self._min_gap > 0:
            wait = max(wait, self._sent[-1] + self._min_gap - now)
        if len(self._sent) >= self._max:
            wait = max(wait, self._sent[0] + self._window - now)
        return max(0.0, wait)

    def allowed(self) -> bool:
        return self.retry_after() <= 0.0

    def record(self) -> None:
        self._sent.append(self._clock())

    def used_in_window(self) -> int:
        self._prune(self._clock())
        return len(self._sent)

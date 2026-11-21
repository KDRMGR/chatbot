"""In-memory counters and latency tracking. Bounded: nothing here grows over a 6-hour session."""
from __future__ import annotations

import statistics
import time
from collections import Counter, deque
from typing import Any


class Metrics:
    def __init__(self) -> None:
        self._counters: Counter[str] = Counter()
        self._latency: deque[int] = deque(maxlen=200)
        self._started = time.monotonic()

    def inc(self, name: str, amount: int = 1) -> None:
        self._counters[name] += amount

    def observe_latency(self, ms: int) -> None:
        self._latency.append(ms)

    def counters(self) -> dict[str, int]:
        return dict(self._counters)

    def snapshot(self) -> dict[str, Any]:
        lat = sorted(self._latency)
        return {
            "uptime_s": int(time.monotonic() - self._started),
            "counters": self.counters(),
            "ai_latency_ms": {
                "samples": len(lat),
                "median": int(statistics.median(lat)) if lat else None,
                "p95": lat[min(len(lat) - 1, int(0.95 * (len(lat) - 1) + 0.5))] if lat else None,
                "last": self._latency[-1] if self._latency else None,
            },
        }

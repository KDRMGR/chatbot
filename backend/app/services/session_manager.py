"""Stream session lifecycle (start/end rows, crash detection, statistics snapshot)."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from ..chat.normalizer import now_utc
from ..config import Settings
from ..database.repository import Repository
from .statistics import Metrics

logger = logging.getLogger(__name__)


class SessionManager:
    def __init__(self, repo: Repository, settings: Settings, metrics: Metrics) -> None:
        self._repo, self._s, self._metrics = repo, settings, metrics
        self.session_id: int | None = None
        self.started_at: datetime = now_utc()

    async def start(self) -> int:
        self.started_at = now_utc()
        stale = await self._repo.mark_stale_sessions_crashed(self.started_at)
        if stale:
            logger.warning("%d previous session(s) were not closed cleanly; marked as crashed", stale)
        self.session_id = await self._repo.start_session(
            self.started_at, {"primary_model": self._s.primary_model, "fallback_model": self._s.fallback_model}
        )
        logger.info("Session %s started", self.session_id)
        return self.session_id

    async def flush_metrics(self) -> None:
        if self.session_id is None:
            return
        now = now_utc()
        for name, value in self._metrics.counters().items():
            await self._repo.add_statistic(self.session_id, name, float(value), now)
        latency = self._metrics.snapshot()["ai_latency_ms"]
        for key, name in (("median", "ai_latency_ms_median"), ("p95", "ai_latency_ms_p95")):
            if latency[key] is not None:
                await self._repo.add_statistic(self.session_id, name, float(latency[key]), now)
        await self._repo.add_statistic(self.session_id, "uptime_s", float(self._metrics.snapshot()["uptime_s"]), now)

    async def run_flush_loop(self) -> None:
        """Save the statistics every few minutes, so a crash or power cut loses minutes of numbers, not the whole session."""
        while True:
            await asyncio.sleep(self._s.stats_flush_interval_s)
            try:
                await self.flush_metrics()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - statistics must never take the bot down
                logger.exception("could not save session statistics")

    async def end(self, status: str = "ended") -> None:
        if self.session_id is None:
            return
        await self.flush_metrics()
        await self._repo.end_session(self.session_id, now_utc(), status)
        logger.info("Session %s ended (%s)", self.session_id, status)
        self.session_id = None

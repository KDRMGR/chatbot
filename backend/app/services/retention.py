"""Data retention. YouTube's developer policy limits how long API-derived data (channel ids, display names, chat text)
may be stored without being refreshed: this service deletes YouTube-derived records older than the configured number of
days (30 at most). Viewer recognition for YouTube therefore only "remembers" viewers seen in the last 30 days."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Callable

from ..chat.normalizer import now_utc
from ..config import Settings
from ..database.repository import Repository

logger = logging.getLogger(__name__)


class RetentionService:
    def __init__(self, repo: Repository, settings: Settings, clock: Callable[[], datetime] = now_utc) -> None:
        self._repo, self._s, self._clock = repo, settings, clock

    async def run_once(self) -> dict[str, int]:
        cutoff = self._clock() - timedelta(days=self._s.youtube_data_retention_days)
        counts = await self._repo.purge_platform_data("youtube", cutoff)
        if any(counts.values()):
            logger.info("Retention: deleted YouTube data older than %d days: %s", self._s.youtube_data_retention_days, counts)
        return counts

    async def run_loop(self, interval_s: float = 6 * 3600) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - retention must never take the app down
                logger.exception("retention run failed")
            await asyncio.sleep(interval_s)

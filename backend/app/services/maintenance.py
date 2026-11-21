"""Housekeeping that keeps a many-hour session small.

Every database call hands work to a thread and waits for it, and the objects that carry that hand-off (futures, callbacks,
events) form reference cycles. Python frees such cycles only when its cyclic garbage collector runs, and for objects that
lived long enough to be promoted that can be rare: measured on this bot, memory climbed about 0.35 MB/s under 6 messages/s
until a collection freed ~20 MB at once (a sawtooth up to 320 MB). That is not a leak, but it is a poor use of a 16 GB PC that
is also running a game. A full collection every minute costs a few milliseconds and keeps the curve flat.
"""
from __future__ import annotations

import asyncio
import gc
import logging
import time
from typing import Callable

logger = logging.getLogger(__name__)

SLOW_COLLECTION_S = 0.25  # worth a warning: the whole event loop pauses while Python collects


async def run_gc_loop(interval_s: float, *, sleep: Callable = asyncio.sleep, clock: Callable[[], float] = time.perf_counter,
                      collect: Callable[[], int] = gc.collect) -> None:
    if interval_s <= 0:
        return  # disabled
    while True:
        await sleep(interval_s)
        started = clock()
        try:
            freed = collect()
        except Exception:  # noqa: BLE001 - housekeeping must never take the bot down
            logger.exception("garbage collection failed")
            continue
        took = clock() - started
        if took > SLOW_COLLECTION_S:
            logger.warning("garbage collection took %.0f ms (%d objects freed)", took * 1000, freed)
        else:
            logger.debug("garbage collection: %d objects freed in %.1f ms", freed, took * 1000)

import asyncio
import logging

import pytest

from app.config import Settings
from app.services.maintenance import SLOW_COLLECTION_S, run_gc_loop


async def test_the_garbage_collector_runs_on_every_interval():
    calls = []

    async def sleep(seconds):
        calls.append(("sleep", seconds))
        if len([c for c in calls if c[0] == "collect"]) >= 3:
            raise asyncio.CancelledError  # stop the endless loop after three rounds

    def collect():
        calls.append(("collect",))
        return 7

    with pytest.raises(asyncio.CancelledError):
        await run_gc_loop(60.0, sleep=sleep, collect=collect)
    assert [c[0] for c in calls[:4]] == ["sleep", "collect", "sleep", "collect"]
    assert calls[0] == ("sleep", 60.0)


async def test_a_zero_interval_turns_it_off_without_looping():
    await asyncio.wait_for(run_gc_loop(0, collect=lambda: pytest.fail("must not collect")), timeout=1)


async def test_a_failing_collection_does_not_stop_the_loop(caplog):
    rounds = []

    async def sleep(_):
        if len(rounds) >= 2:
            raise asyncio.CancelledError

    def collect():
        rounds.append(1)
        raise RuntimeError("boom")

    with caplog.at_level(logging.ERROR), pytest.raises(asyncio.CancelledError):
        await run_gc_loop(1.0, sleep=sleep, collect=collect)
    assert len(rounds) == 2 and "garbage collection failed" in caplog.text


async def test_a_slow_collection_is_reported(caplog):
    ticks = iter([0.0, SLOW_COLLECTION_S + 0.5])  # start, end

    async def sleep(_):
        if not hasattr(sleep, "first"):
            sleep.first = True
            return
        raise asyncio.CancelledError

    with caplog.at_level(logging.WARNING), pytest.raises(asyncio.CancelledError):
        await run_gc_loop(1.0, sleep=sleep, clock=lambda: next(ticks), collect=lambda: 12)
    assert "garbage collection took 750 ms" in caplog.text


def test_the_interval_defaults_to_a_minute_and_zero_is_allowed():
    assert Settings().gc_interval_s == 60.0
    assert Settings(gc_interval_s=0).gc_interval_s == 0


async def test_the_app_runs_the_housekeeping_task(make_app_client):
    client, app = await make_app_client(gc_interval_s=3600)
    names = {t.get_name() for t in asyncio.all_tasks()}
    assert "memory-housekeeping" in names

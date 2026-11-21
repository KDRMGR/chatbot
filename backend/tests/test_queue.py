import asyncio

import pytest

from app.chat.queue import BoundedPriorityQueue, RateLimiter
from tests.conftest import FakeClock


async def test_priority_then_fifo_order():
    q = BoundedPriorityQueue[str](10)
    q.put_nowait("normal-1", 1)
    q.put_nowait("urgent", 0)
    q.put_nowait("normal-2", 1)
    assert [await q.get(), await q.get(), await q.get()] == ["urgent", "normal-1", "normal-2"]


def test_overflow_drops_the_oldest_of_equal_priority():
    q = BoundedPriorityQueue[str](2)
    q.put_nowait("a", 1)
    q.put_nowait("b", 1)
    assert q.put_nowait("c", 1) == "a"
    assert q.qsize() == 2 and q.dropped == 1
    assert q.drain() == ["b", "c"]


def test_overflow_rejects_a_less_important_new_item():
    q = BoundedPriorityQueue[str](1)
    q.put_nowait("important", 0)
    assert q.put_nowait("meh", 1) == "meh"
    assert q.drain() == ["important"]


def test_overflow_evicts_less_important_for_a_more_important_item():
    q = BoundedPriorityQueue[str](1)
    q.put_nowait("meh", 1)
    assert q.put_nowait("important", 0) == "meh"
    assert q.drain() == ["important"]


async def test_get_waits_for_an_item():
    q = BoundedPriorityQueue[int](5)
    task = asyncio.create_task(q.get())
    await asyncio.sleep(0.01)
    assert not task.done()
    q.put_nowait(7)
    assert await asyncio.wait_for(task, 1) == 7


def test_capacity_must_be_positive():
    with pytest.raises(ValueError):
        BoundedPriorityQueue(0)


def test_rate_limiter_min_gap_and_window():
    clock = FakeClock()
    rl = RateLimiter(min_gap_s=8, max_per_window=3, window_s=100, clock=clock)
    assert rl.allowed()
    rl.record()
    assert not rl.allowed() and rl.retry_after() == pytest.approx(8)
    clock.advance(8)
    assert rl.allowed()
    rl.record()
    clock.advance(8)
    rl.record()
    clock.advance(8)
    assert not rl.allowed()  # 3 sent inside the 100 s window
    assert rl.retry_after() == pytest.approx(100 - 24)
    clock.advance(77)
    assert rl.allowed() and rl.used_in_window() == 2

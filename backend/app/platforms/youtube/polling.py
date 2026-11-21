"""Quota-aware polling pace.

At 5 units per ``liveChatMessages.list`` call a flat 10-second poll costs ~10,800 units over six hours: more than the
whole 10,000-unit daily quota, before the bot has said a word (an insert costs 20). So the poll interval adapts:

* **tiers** by chat activity (fast while people are talking, slow when quiet);
* never faster than the interval YouTube asks for (``pollingIntervalMillis``);
* **budget pacing**: stretch the interval so the quota that is left lasts for the planned stream. Reading gets only
  part of the day (by default 50%: 10% is kept as a final reserve and 40% is set aside for SENDING), otherwise a busy chat
  would poll the whole quota away and leave the bot unable to speak.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from .quota import COSTS, QuotaTracker


@dataclass(frozen=True)
class PollingPlan:
    active_interval_s: float = 10.0  # someone wrote within the last `active_window_s`
    idle_interval_s: float = 30.0
    quiet_interval_s: float = 60.0  # nobody wrote for `idle_window_s`
    active_window_s: float = 120.0
    idle_window_s: float = 600.0
    max_interval_s: float = 120.0
    planned_session_s: float = 6 * 3600.0
    final_reserve_fraction: float = 0.10  # aim to still have this share of the daily quota left at the end
    send_budget_fraction: float = 0.40  # share of the daily quota that polling must never touch: it is for sending


class PollingGovernor:
    def __init__(self, quota: QuotaTracker, plan: PollingPlan = PollingPlan(),
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._quota, self._plan, self._clock = quota, plan, clock
        self._started: float | None = None

    def start_session(self) -> None:
        self._started = self._clock()

    def next_interval(self, api_hint_s: float, seconds_since_message: float | None) -> float:
        p = self._plan
        if seconds_since_message is not None and seconds_since_message <= p.active_window_s:
            tier = p.active_interval_s
        elif seconds_since_message is not None and seconds_since_message <= p.idle_window_s:
            tier = p.idle_interval_s
        else:
            tier = p.quiet_interval_s
        interval = max(tier, api_hint_s)

        if self._started is not None:
            planned_left = max(60.0, p.planned_session_s - (self._clock() - self._started))
            limit = self._quota.daily_limit
            read_budget_left = limit * (1 - p.final_reserve_fraction - p.send_budget_fraction) - self._quota.reads_used
            spendable = min(read_budget_left, self._quota.remaining - limit * p.final_reserve_fraction)
            if spendable <= 0:
                interval = p.max_interval_s
            else:
                affordable_polls = spendable / COSTS["liveChatMessages.list"]
                interval = max(interval, planned_left / affordable_polls)
        return min(interval, p.max_interval_s)

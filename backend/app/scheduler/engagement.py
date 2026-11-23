"""Proactive engagement (spec Mode B): the bot starts a conversation when chat is quiet.

How it decides, once every few seconds and separately for each platform:

    1. Is the platform connected? (Timers restart whenever it reconnects, so a late-starting stream is not greeted with a
       backlog of "due" messages.)
    2. Has chat been quiet for ``quiet_after_s``?  Any viewer message counts as activity, including ones the bot ignores
       (spam, commands): the bot never piles onto a busy or messy chat.
    3. Has ``min_gap_s`` passed since ANY bot message, including replies to viewers?
    4. Did another platform get a proactive message in the last ``platform_stagger_s``? (Two platforms never get one at the
       same moment, and each platform writes its own text, so they are not identical.)
    5. Is an activity due? Each activity (general chat, gaming question, trivia) has its own timer with a random interval
       inside a configured range. If several are due, the one handled by the highest-priority personality goes first; ties
       go to the most overdue.

The message is then queued BEHIND all viewer messages (see ChatProcessor.submit_proactive) and every protective check runs
again at the moment of sending: pause, per-platform rate limit with a share kept for viewers, a viewer speaking meanwhile.
A skipped attempt keeps its timer (it stays due), a sent message restarts it. Failures back off exponentially.

Nothing here talks to a platform: it only asks the processor to send, so rate limits, storage, quota protection and the output
guard are exactly the ones used for replies.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable, Mapping

from ..chat.models import Category, Platform
from ..chat.processor import ChatProcessor
from ..config import Settings
from ..personalities.manager import PersonalityManager
from ..platforms.base import ConnectionState, PlatformAdapter
from ..services.controls import RuntimeControls

logger = logging.getLogger(__name__)

MIN_SETTING_S = 10.0  # shortest interval or quiet time that can be set live
MAX_SETTING_S = 6 * 3600.0
MAX_BACKOFF_S = 900.0
_TOP_LEVEL_KEYS = {"quiet_after_s", "min_gap_s", "activities"}
_ACTIVITY_KEYS = {"enabled", "min_interval_s", "max_interval_s", "category"}  # category is shown but fixed


class SchedulerSettingsError(ValueError):
    def __init__(self, errors: list[dict[str, Any]]) -> None:
        super().__init__(f"{len(errors)} invalid scheduler setting(s)")
        self.errors = errors


@dataclass
class Activity:
    name: str
    category: Category
    min_interval_s: float
    max_interval_s: float
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "min_interval_s": self.min_interval_s, "max_interval_s": self.max_interval_s,
                "category": self.category.value}


@dataclass
class SchedulerConfig:
    quiet_after_s: float
    min_gap_s: float
    activities: dict[str, Activity]

    @classmethod
    def from_settings(cls, s: Settings) -> "SchedulerConfig":
        return cls(
            quiet_after_s=s.proactive_quiet_after_s, min_gap_s=s.proactive_min_gap_s,
            activities={
                "general": Activity("general", Category.PROACTIVE_GENERAL, s.proactive_general_min_s, s.proactive_general_max_s),
                # "gaming question": the quiet-chat starter asks the chat one question about the game
                "gaming_question": Activity("gaming_question", Category.PROACTIVE_QUIET, s.proactive_question_min_s,
                                            s.proactive_question_max_s),
                "trivia": Activity("trivia", Category.PROACTIVE_QUIZ, s.proactive_trivia_min_s, s.proactive_trivia_max_s),
                # "screen": react to what the bot's eyes just saw. Only due while the eyes report a change it has not talked about yet.
                "screen": Activity("screen", Category.PROACTIVE_HYPE, s.proactive_screen_min_s, s.proactive_screen_max_s),
                # "buzz": bring up something people online are saying. Only due while there is something new GUY has not mentioned.
                "buzz": Activity("buzz", Category.PROACTIVE_GENERAL, s.proactive_buzz_min_s, s.proactive_buzz_max_s),
            },
        )

    def to_dict(self) -> dict[str, Any]:
        return {"quiet_after_s": self.quiet_after_s, "min_gap_s": self.min_gap_s,
                "activities": {n: a.to_dict() for n, a in self.activities.items()}}

    def to_saved(self) -> dict[str, Any]:
        """Only what the streamer can change (what is stored in the database)."""
        data = self.to_dict()
        for activity in data["activities"].values():
            del activity["category"]
        return data

    def updated(self, update: Mapping[str, Any]) -> "SchedulerConfig":
        """A validated copy with ``update`` applied; raises SchedulerSettingsError listing every problem.
        Used for live edits from the dashboard and for restoring saved ones, so both obey the same rules."""
        errors: list[dict[str, Any]] = []

        def number(loc: list[Any], value: Any) -> float | None:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                errors.append({"loc": loc, "msg": "must be a number of seconds"})
                return None
            if not MIN_SETTING_S <= value <= MAX_SETTING_S:
                errors.append({"loc": loc, "msg": f"must be between {MIN_SETTING_S:.0f} and {MAX_SETTING_S:.0f} seconds"})
                return None
            return float(value)

        if not isinstance(update, Mapping):
            raise SchedulerSettingsError([{"loc": ["body"], "msg": "must be a JSON object"}])
        for key in update:
            if key not in _TOP_LEVEL_KEYS:
                errors.append({"loc": [key], "msg": "unknown setting"})
        new = replace(self, activities={n: replace(a) for n, a in self.activities.items()})
        for key in ("quiet_after_s", "min_gap_s"):
            if key in update:
                value = number([key], update[key])
                if value is not None:
                    setattr(new, key, value)
        acts = update.get("activities", {})
        if not isinstance(acts, Mapping):
            errors.append({"loc": ["activities"], "msg": "must be an object"})
            acts = {}
        for name, values in acts.items():
            activity = new.activities.get(name)
            if activity is None:
                errors.append({"loc": ["activities", name], "msg": f"unknown activity (known: {sorted(new.activities)})"})
                continue
            if not isinstance(values, Mapping):
                errors.append({"loc": ["activities", name], "msg": "must be an object"})
                continue
            for key, value in values.items():
                loc = ["activities", name, key]
                if key not in _ACTIVITY_KEYS:
                    errors.append({"loc": loc, "msg": "unknown setting"})
                elif key == "category":
                    if value != activity.category.value:  # echoing the current value back is fine; changing it is not
                        errors.append({"loc": loc, "msg": "the personality category of an activity cannot be changed"})
                elif key == "enabled":
                    if isinstance(value, bool):
                        activity.enabled = value
                    else:
                        errors.append({"loc": loc, "msg": "must be true or false"})
                else:
                    number_value = number(loc, value)
                    if number_value is not None:
                        setattr(activity, key, number_value)
            if activity.min_interval_s > activity.max_interval_s:
                errors.append({"loc": ["activities", name], "msg": "min_interval_s must not be larger than max_interval_s"})
        if errors:
            raise SchedulerSettingsError(errors)
        return new


@dataclass
class _PlatformState:
    next_due: dict[str, float] = field(default_factory=dict)
    quiet_ref: float = 0.0  # quiet is counted from here at the earliest (connect time)
    last_viewer: float | None = None
    last_bot: float | None = None
    retry_not_before: float = 0.0
    failures: int = 0
    needs_reset: bool = True
    sent: int = 0
    last_outcome: str | None = None
    last_activity: str | None = None


class EngagementScheduler:
    def __init__(
        self, settings: Settings, processor: ChatProcessor, controls: RuntimeControls, personalities: PersonalityManager,
        adapters: dict[Platform, PlatformAdapter], *, clock: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None, save: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        self._s, self._processor, self._controls = settings, processor, controls
        self._personalities, self._adapters, self._clock = personalities, adapters, clock
        self._rng = rng or random.Random()
        self._save = save
        self.config = SchedulerConfig.from_settings(settings)
        self._platforms: list[Platform] = []
        for name in settings.proactive_platforms:
            try:
                self._platforms.append(Platform(name))
            except ValueError:
                logger.warning("Ignoring unknown proactive platform %r", name)
        self._states: dict[Platform, _PlatformState] = {}
        self._tick_lock = asyncio.Lock()
        self._last_sent_any: tuple[Platform, float] | None = None
        self.screen_ready: Callable[[], bool] | None = None  # set by the application: has the picture changed since we last spoke of it?
        self.screen_spoken: Callable[[], None] | None = None  # ... and: we just spoke about it
        self.buzz_ready: Callable[[], bool] | None = None  # is there something new that people online are saying?
        self.buzz_spoken: Callable[[], None] | None = None
        self.counters = {"sent": 0, "skipped": 0, "failed": 0}

    # ------------------------------------------------------------------ what the pipeline tells us
    def _state(self, platform: Platform) -> _PlatformState:
        return self._states.setdefault(platform, _PlatformState())

    def viewer_message(self, platform: Platform) -> None:
        self._state(platform).last_viewer = self._clock()

    def bot_message(self, platform: Platform) -> None:
        self._state(platform).last_bot = self._clock()

    # ------------------------------------------------------------------ timing helpers
    def _interval(self, activity: Activity) -> float:
        return self._rng.uniform(activity.min_interval_s, activity.max_interval_s)

    def _reset(self, st: _PlatformState, now: float, index: int) -> None:
        """(Re)start the timers: on the first connection and after every reconnect. Platforms are offset from each
        other so that they do not become due at the same moment."""
        st.quiet_ref = now
        st.retry_not_before = now
        st.failures = 0
        st.needs_reset = False
        offset = index * self._s.proactive_platform_stagger_s
        st.next_due = {n: now + offset + self._interval(a) for n, a in self.config.activities.items()}

    def _quiet_for(self, st: _PlatformState, now: float) -> float:
        since = max(st.quiet_ref, st.last_viewer if st.last_viewer is not None else st.quiet_ref)
        return now - since

    def _may_speak(self, platform: Platform, st: _PlatformState, now: float) -> bool:
        if not self._controls.proactive_enabled:
            return False
        if self._quiet_for(st, now) < self.config.quiet_after_s:
            return False
        if st.last_bot is not None and now - st.last_bot < self.config.min_gap_s:
            return False
        last = self._last_sent_any
        if last is not None and last[0] is not platform and now - last[1] < self._s.proactive_platform_stagger_s:
            return False
        return True

    # ------------------------------------------------------------------ the loop
    async def tick(self) -> list[dict[str, Any]]:
        """One pass over every platform. Returns what was attempted (used by tests and for logging).
        Passes never overlap: a second caller waits, then sees the first one's bookkeeping and so cannot double-send."""
        async with self._tick_lock:
            return await self._tick()

    async def _tick(self) -> list[dict[str, Any]]:
        attempts: list[dict[str, Any]] = []
        if not self._controls.proactive_enabled:
            return attempts
        for index, platform in enumerate(self._platforms):
            adapter = self._adapters.get(platform)
            if adapter is None:
                continue
            st = self._state(platform)
            now = self._clock()
            if adapter.status().state is not ConnectionState.CONNECTED:
                st.needs_reset = True  # timers restart when it comes back
                continue
            if st.needs_reset:
                self._reset(st, now, index)
            if now < st.retry_not_before or not self._may_speak(platform, st, now):
                continue
            activity = self._pick(st, now)
            if activity is None:
                continue
            attempts.append(await self._attempt(platform, st, activity))
        return attempts

    def _pick(self, st: _PlatformState, now: float) -> Activity | None:
        due: list[tuple[int, float, Activity]] = []
        for name, activity in self.config.activities.items():
            if not activity.enabled or st.next_due.get(name, float("inf")) > now:
                continue
            if name == "screen" and not (self.screen_ready is not None and self.screen_ready()):
                continue  # nothing new to react to
            if name == "buzz" and not (self.buzz_ready is not None and self.buzz_ready()):
                continue  # nothing new people are saying
            priority = self._personalities.best_priority(activity.category)
            if priority is None:  # no enabled personality handles it: nothing to say
                continue
            due.append((priority, now - st.next_due[name], activity))
        if not due:
            return None
        return max(due, key=lambda d: (d[0], d[1]))[2]  # highest personality priority, then most overdue

    async def _attempt(self, platform: Platform, st: _PlatformState, activity: Activity) -> dict[str, Any]:
        reply: str | None = None
        personality: str | None = None
        try:
            result = await self._processor.submit_proactive(
                platform, activity.category,
                still_ok=lambda: self._may_speak(platform, self._state(platform), self._clock()),
                timeout=self._s.submit_timeout_s,
            )
            action, reason, reply, personality = result.action, result.reason, result.reply, result.personality
        except asyncio.TimeoutError:
            action, reason = "error", "timeout"
        now = self._clock()
        st.last_activity, st.last_outcome = activity.name, f"{action}{':' + reason if reason else ''}"

        if action == "replied":
            st.next_due[activity.name] = now + self._interval(activity)
            st.failures = 0
            st.sent += 1
            self._last_sent_any = (platform, now)
            self.counters["sent"] += 1
            if activity.name == "screen" and self.screen_spoken is not None:
                self.screen_spoken()
            if activity.name == "buzz" and self.buzz_spoken is not None:
                self.buzz_spoken()
            logger.info("Proactive %s message sent on %s", activity.name, platform.value)
        elif action == "held":
            # Approval mode: the message waits for the streamer. For timing it counts as said, or the same activity would
            # generate a new message every few seconds while the first one is still waiting.
            st.next_due[activity.name] = now + self._interval(activity)
            st.failures = 0
            self.counters["held"] = self.counters.get("held", 0) + 1
        elif action == "skipped" and reason != "nothing_new_to_say":
            # Nothing is wrong: the world just was not right. The activity stays due; look again soon.
            st.retry_not_before = now + self._s.proactive_retry_s
            self.counters["skipped"] += 1
        else:
            # The AI had nothing usable, the send failed, the queue dropped it, ... back off so a broken platform is not hammered.
            st.failures += 1
            st.retry_not_before = now + min(MAX_BACKOFF_S, self._s.proactive_retry_s * 2 ** min(st.failures, 6))
            self.counters["failed"] += 1
            logger.warning("Proactive %s message on %s not sent (%s); retrying in %.0fs", activity.name, platform.value,
                           st.last_outcome, st.retry_not_before - now)
        return {"platform": platform.value, "activity": activity.name, "action": action, "reason": reason,
                "personality": personality, "reply": reply}

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the scheduler must never die; viewers are answered without it
                logger.exception("scheduler tick failed")
            await asyncio.sleep(self._s.proactive_tick_s)

    # ------------------------------------------------------------------ settings and status
    async def update_settings(self, update: Mapping[str, Any]) -> dict[str, Any]:
        """Apply validated changes live (raises SchedulerSettingsError, changing nothing, if anything is invalid)."""
        new = self.config.updated(update)
        now = self._clock()
        for name, activity in new.activities.items():
            old = self.config.activities[name]
            if (old.min_interval_s, old.max_interval_s) != (activity.min_interval_s, activity.max_interval_s):
                for st in self._states.values():  # a new interval takes effect from now, not after the old timer
                    if name in st.next_due:
                        st.next_due[name] = now + self._interval(activity)
        self.config = new
        if self._save is not None:
            await self._save(new.to_saved())
        return new.to_dict()

    def restore(self, saved: Mapping[str, Any]) -> None:
        """Re-apply settings saved by the dashboard. Invalid saved data is ignored (the configured values stay)."""
        try:
            self.config = self.config.updated(saved)
        except SchedulerSettingsError as exc:
            logger.warning("Ignoring invalid saved scheduler settings: %s", exc.errors)

    def status(self) -> dict[str, Any]:
        now = self._clock()
        platforms: dict[str, Any] = {}
        for platform in self._platforms:
            adapter = self._adapters.get(platform)
            st = self._state(platform)
            connected = adapter is not None and adapter.status().state is ConnectionState.CONNECTED
            platforms[platform.value] = {
                "connected": connected,
                "quiet_for_s": round(self._quiet_for(st, now)) if connected and not st.needs_reset else None,
                "may_speak_now": bool(connected and not st.needs_reset and self._may_speak(platform, st, now)),
                "next_in_s": {n: max(0, round(t - now)) for n, t in st.next_due.items()} if connected else {},
                "sent": st.sent, "failures": st.failures, "last_outcome": st.last_outcome,
                "last_activity": st.last_activity,
            }
        return {"enabled": self._controls.proactive_enabled, "settings": self.config.to_dict(),
                "platforms": platforms, "counters": dict(self.counters)}

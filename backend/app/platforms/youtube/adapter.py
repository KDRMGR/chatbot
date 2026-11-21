"""YouTube Live chat adapter.

Isolation: every failure is caught inside this adapter's own task, so a YouTube problem can never stop the app or another
platform. Reconnection uses exponential backoff with a cap; auth and quota problems are NOT retried in a tight loop.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import deque
from typing import Any, Awaitable, Callable

from ...chat.models import Platform
from ...chat.normalizer import now_utc
from ...config import Settings
from ..base import ConnectionState, ConnectionStatus, OnMessage, PlatformAdapter, SendResult
from .api import (
    AuthError, BadRequest, BroadcastInfo, ChatPage, Forbidden, LiveChatDisabled, LiveChatEnded, LiveChatNotFound,
    NetworkError, QuotaExceeded, RateLimited, ServerError, YouTubeApi, YouTubeApiError,
)
from .credentials import NotAuthorized, ReauthRequired
from .normalizer import is_chat_ended, normalize, stable_id
from .oauth import OAuthError
from .polling import PollingGovernor, PollingPlan
from .quota import QuotaTracker

logger = logging.getLogger(__name__)
LogEvent = Callable[[str, str], Awaitable[None]]
SaveQuota = Callable[[dict[str, Any]], Awaitable[None]]


class Backoff:
    """Exponential backoff with a cap and 'subtractive' jitter (never exceeds the cap)."""

    def __init__(self, initial: float = 2.0, factor: float = 2.0, maximum: float = 60.0, jitter: float = 0.2,
                 rng: random.Random | None = None) -> None:
        self._initial, self._factor, self._max, self._jitter = initial, factor, maximum, jitter
        self._rng = rng or random.Random()
        self.attempts = 0

    def next(self) -> float:
        delay = min(self._initial * (self._factor ** self.attempts), self._max)
        self.attempts += 1
        return delay * (1.0 - self._jitter * self._rng.random())

    def reset(self) -> None:
        self.attempts = 0


class YouTubeAdapter(PlatformAdapter):
    platform = Platform.YOUTUBE
    _END_REASONS = (LiveChatEnded, LiveChatDisabled, LiveChatNotFound)

    def __init__(
        self, settings: Settings, api: YouTubeApi, quota: QuotaTracker, *, log_event: LogEvent | None = None,
        save_quota: SaveQuota | None = None, clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep, rng: random.Random | None = None,
    ) -> None:
        self._s, self._api, self._quota = settings, api, quota
        self._log_event, self._save_quota = log_event, save_quota
        self._clock, self._sleep = clock, sleep
        self._rng = rng or random.Random()
        self._plan = PollingPlan(
            active_interval_s=settings.youtube_poll_active_s, idle_interval_s=settings.youtube_poll_idle_s,
            quiet_interval_s=settings.youtube_poll_quiet_s, planned_session_s=settings.youtube_planned_stream_hours * 3600,
            send_budget_fraction=settings.youtube_send_budget_fraction,
        )
        self._on_message: OnMessage | None = None
        self._task: asyncio.Task[None] | None = None
        self._bg: set[asyncio.Task[Any]] = set()
        self._status = ConnectionStatus()
        self._stopping = False
        self._send_lock = asyncio.Lock()
        self._own_channel_id: str | None = None
        self._own_channel_title: str = ""
        self._chat_id: str | None = None
        self._video_id: str | None = None
        self._resume: dict[str, str] = {}  # live chat id -> last nextPageToken (resume without duplicates)
        self._seen: deque[str] = deque(maxlen=5000)
        self._seen_set: set[str] = set()
        self._last_message_at: float | None = None
        self._stream_disabled = False
        self.stats = {"reconnects": 0, "messages_delivered": 0, "polls": 0, "sends_ok": 0, "sends_failed": 0}

    # ------------------------------------------------------------------ PlatformAdapter
    async def start(self, on_message: OnMessage) -> None:
        self._on_message = on_message
        self._stopping = False
        if self._task is None or self._task.done():
            self._set(ConnectionState.CONNECTING, "starting")
            self._task = asyncio.create_task(self._run(), name="youtube-adapter")

    async def stop(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self._flush_bg()
        self._set(ConnectionState.DISCONNECTED, "stopped")
        await self._flush_bg()

    def status(self) -> ConnectionStatus:
        return self._status

    def details(self) -> dict[str, Any]:
        return {
            "channel": self._own_channel_title or None, "video_id": self._video_id, "live_chat_id": self._chat_id,
            "permission": "youtube.force-ssl (read chat, post messages, find the stream)",
            "transport": "stream" if (self._s.youtube_use_streaming and not self._stream_disabled) else "polling",
            "quota": self._quota.snapshot(), **self.stats,
        }

    # ------------------------------------------------------------------ status / logging helpers
    def _set(self, state: ConnectionState, detail: str | None) -> None:
        changed = state is not self._status.state or detail != self._status.detail
        since = now_utc() if state is not self._status.state else self._status.since
        self._status = ConnectionStatus(state, detail, since)
        if changed:
            logger.info("YouTube %s: %s", state.value, detail)
            if self._log_event:
                self._spawn(self._log_event(state.value, detail or ""))

    def _spawn(self, coro: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    async def _flush_bg(self) -> None:
        if self._bg:
            await asyncio.gather(*list(self._bg), return_exceptions=True)

    async def _persist_quota(self) -> None:
        if self._save_quota:
            try:
                await self._save_quota(self._quota.dump())
            except Exception:  # noqa: BLE001 - bookkeeping must never break chat
                logger.exception("could not persist YouTube quota state")

    def _guard(self, method: str) -> None:
        if not self._quota.can_read(method):
            raise QuotaExceeded(403, "localQuotaGuard", f"daily quota would be exceeded by {method}")

    # ------------------------------------------------------------------ main loop
    async def _run(self) -> None:
        backoff = Backoff(rng=self._rng)
        while not self._stopping:
            try:
                await self._ensure_identity()
                found = await self._discover()
                if found is None:
                    self._set(ConnectionState.DISCONNECTED, "waiting for a live broadcast with chat")
                    await self._sleep(self._s.youtube_discovery_interval_s)
                    continue
                self._chat_id, self._video_id = found
                self._set(ConnectionState.CONNECTED, "live chat connected")
                # NOTE: the backoff is reset only after a successful poll/stream page inside _pump. Resetting it here
                # would make a consistently failing read retry every 2 s forever (a reconnection loop).
                await self._pump(found[0], backoff)
                self._end_of_chat("live chat ended or stream went offline")
                await self._sleep(self._s.youtube_discovery_interval_s)
            except asyncio.CancelledError:
                raise
            except (NotAuthorized, ReauthRequired) as exc:
                self._set(ConnectionState.ERROR, f"not authorised: {exc} (run scripts\\youtube_login.py)")
                await self._sleep(300)  # no API traffic while unauthorised; re-check in case the user signed in again
            except QuotaExceeded as exc:
                wait = min(max(self._quota.seconds_until_reset(), 60.0), 600.0)
                self._set(ConnectionState.ERROR, f"quota exhausted ({exc.reason}); waiting for the Pacific-time reset")
                await self._sleep(wait)
            except self._END_REASONS as exc:
                self._end_of_chat(self._end_detail(exc))
                await self._sleep(self._s.youtube_discovery_interval_s)
            except (NetworkError, ServerError, RateLimited, AuthError, Forbidden, BadRequest, OAuthError) as exc:
                delay = backoff.next()
                self.stats["reconnects"] += 1
                reason = getattr(exc, "reason", None) or getattr(exc, "error", None) or exc.__class__.__name__
                self._set(ConnectionState.ERROR, f"{reason}; retrying in {delay:.0f}s")
                await self._sleep(delay)
            except Exception as exc:  # noqa: BLE001 - last line of defence: the adapter must never die
                logger.exception("Unexpected YouTube adapter error")
                delay = backoff.next()
                self._set(ConnectionState.ERROR, f"unexpected {exc.__class__.__name__}; retrying in {delay:.0f}s")
                await self._sleep(delay)

    @staticmethod
    def _end_detail(exc: YouTubeApiError) -> str:
        if exc.reason in ("liveChatEnded", "chatEndedEvent"):
            return "live chat ended"
        return f"live chat unavailable ({exc.reason})"

    def _end_of_chat(self, detail: str) -> None:
        self._chat_id = None
        self._set(ConnectionState.DISCONNECTED, detail)

    async def _ensure_identity(self) -> None:
        if self._own_channel_id is None:
            self._guard("channels.list")
            info = await self._api.own_channel()
            self._own_channel_id, self._own_channel_title = info["id"], info["title"]
            await self._persist_quota()

    async def _discover(self) -> tuple[str, str | None] | None:
        s = self._s
        if s.youtube_live_chat_id:
            return s.youtube_live_chat_id, s.youtube_video_id
        if s.youtube_video_id:
            self._guard("videos.list")
            chat_id = await self._api.video_chat_id(s.youtube_video_id)
            return (chat_id, s.youtube_video_id) if chat_id else None
        self._guard("liveBroadcasts.list")
        broadcast: BroadcastInfo | None = await self._api.active_broadcast()
        await self._persist_quota()
        if broadcast:
            return broadcast.live_chat_id, broadcast.video_id
        if s.youtube_streamer_channel_id:
            self._guard("search.list")
            video_id = await self._api.find_live_video(s.youtube_streamer_channel_id)
            if video_id:
                self._guard("videos.list")
                chat_id = await self._api.video_chat_id(video_id)
                if chat_id:
                    return chat_id, video_id
        return None

    # ------------------------------------------------------------------ reading chat
    async def _pump(self, chat_id: str, backoff: Backoff) -> None:
        if self._s.youtube_use_streaming and not self._stream_disabled:
            await self._pump_stream(chat_id, backoff)
        else:
            await self._pump_polling(chat_id, backoff)

    def _seconds_since_message(self) -> float | None:
        return None if self._last_message_at is None else self._clock() - self._last_message_at

    async def _pump_polling(self, chat_id: str, backoff: Backoff) -> None:
        governor = PollingGovernor(self._quota, self._plan, self._clock)
        governor.start_session()
        while not self._stopping:
            self._guard("liveChatMessages.list")
            token = self._resume.get(chat_id)
            page = await self._api.list_messages(chat_id, token)
            self.stats["polls"] += 1
            backoff.reset()
            self._set(ConnectionState.CONNECTED, "live chat connected")
            if page.next_page_token:
                self._resume[chat_id] = page.next_page_token
            await self._deliver(page, backlog=(token is None and self._s.youtube_skip_backlog))
            await self._persist_quota()
            if page.offline_at:
                return
            await self._sleep(governor.next_interval(page.polling_interval_s, self._seconds_since_message()))

    async def _pump_stream(self, chat_id: str, backoff: Backoff) -> None:
        """EXPERIMENTAL transport. If connections keep ending almost immediately (framing or support problem), fall back
        to polling for good instead of burning quota in a reconnect loop."""
        quick_ends = 0
        while not self._stopping:
            self._guard("liveChatMessages.stream")
            started = self._clock()
            token = self._resume.get(chat_id)
            try:
                async for page in self._api.stream_messages(chat_id, token):
                    backoff.reset()
                    if page.next_page_token:
                        self._resume[chat_id] = page.next_page_token
                    await self._deliver(page, backlog=(token is None and self._s.youtube_skip_backlog))
                    token = self._resume.get(chat_id)
                    if page.offline_at:
                        return
            except BadRequest as exc:
                logger.warning("YouTube stream not usable (%s); falling back to polling", exc.reason)
                self._stream_disabled = True
                return await self._pump_polling(chat_id, backoff)
            await self._persist_quota()
            quick_ends = quick_ends + 1 if (self._clock() - started) < 10.0 else 0
            if quick_ends >= 3:
                logger.warning("YouTube stream keeps closing immediately; falling back to polling")
                self._stream_disabled = True
                return await self._pump_polling(chat_id, backoff)
            await self._sleep(2.0)

    async def _deliver(self, page: ChatPage, *, backlog: bool) -> None:
        ended = False
        for item in page.items:
            mid = item.get("id")
            if not mid or mid in self._seen_set:
                continue
            if len(self._seen) == self._seen.maxlen:
                self._seen_set.discard(self._seen[0])
            self._seen.append(mid)
            self._seen_set.add(mid)
            if is_chat_ended(item):
                ended = True
                continue
            if backlog:
                continue  # history from before we connected: never answer old messages
            msg = normalize(item, self._own_channel_id)
            if msg is None:
                continue
            if not msg.is_bot_self:
                self._last_message_at = self._clock()
            self.stats["messages_delivered"] += 1
            if self._on_message:
                try:
                    await self._on_message(msg)
                except Exception:  # noqa: BLE001 - one bad message must not break the read loop
                    logger.exception("on_message handler failed")
        if ended:
            raise LiveChatEnded(403, "chatEndedEvent", "the live chat has ended")

    # ------------------------------------------------------------------ sending
    async def send(self, text: str, *, reply_to: Any = None) -> SendResult:
        if self._status.state is not ConnectionState.CONNECTED or not self._chat_id:
            return self._fail("youtube is not connected", retryable=True)
        if not self._quota.can_send():
            return self._fail("quota reserved for reading chat", retryable=False)
        async with self._send_lock:
            try:
                self._guard_send()
                message_id = await self._api.insert_message(self._chat_id, text)
            except RateLimited as exc:
                return self._fail(exc.reason, retryable=True)
            except QuotaExceeded as exc:
                return self._fail(exc.reason, retryable=False)
            except self._END_REASONS as exc:
                self._end_of_chat(self._end_detail(exc))
                return self._fail(exc.reason, retryable=False)
            except (NotAuthorized, ReauthRequired):
                return self._fail("not authorised", retryable=False)
            except (AuthError, Forbidden, BadRequest) as exc:
                return self._fail(exc.reason, retryable=False)
            except (NetworkError, ServerError, OAuthError) as exc:
                return self._fail(getattr(exc, "reason", None) or getattr(exc, "error", "error"), retryable=True)
            await self._persist_quota()
        self.stats["sends_ok"] += 1
        return SendResult(ok=True, platform_message_id=stable_id(message_id) if message_id else None)

    def _guard_send(self) -> None:
        if not self._quota.can_read("liveChatMessages.insert"):
            raise QuotaExceeded(403, "localQuotaGuard", "daily quota would be exceeded by an insert")

    def _fail(self, reason: str, *, retryable: bool) -> SendResult:
        self.stats["sends_failed"] += 1
        return SendResult(ok=False, error=reason, retryable=retryable)

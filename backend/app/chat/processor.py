"""The message pipeline. One worker consumes the bounded incoming queue, so messages are handled strictly one at
a time (one AI generation at a time) while producers (platform adapters, the API) never block.

    ingest -> self-echo check -> dedupe -> viewer upsert -> classify -> store -> (ignore?) -> pause check
           -> rate limits -> compose (command | event | AI | rule fallback) -> output guard -> store -> send
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from ..ai.response_generator import GenerationFailed, ResponseGenerator
from ..config import Settings
from ..database.repository import Repository
from ..personalities.manager import PersonalityManager
from ..platforms.base import ConnectionState, PlatformAdapter, SendResult
from ..services.approvals import ApprovalQueue, PendingReply
from ..services.controls import RuntimeControls
from ..services.statistics import Metrics
from .angles import AnglePicker
from .context import SessionContext
from .filters import Classification, MessageFilter
from .models import Category, ChatMessage, EventType, Platform
from .normalizer import compare_key, now_utc
from .output_guard import RecentReplies, fit_to_limit
from .queue import BoundedPriorityQueue, RateLimiter
from .rules import RuleEngine

logger = logging.getLogger(__name__)

PROACTIVE_PRIORITY = 2  # queue priority: viewers' events (0) and messages (1) always go first


class ApprovalBlocked(Exception):
    """An approved message could not be sent right now; it stays in the approval queue."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code, self.reason = code, reason


class ActivityListener(Protocol):
    """What the proactive scheduler needs to know about the chat (kept as a protocol so the pipeline does not import it)."""

    def viewer_message(self, platform: Platform) -> None: ...

    def bot_message(self, platform: Platform) -> None: ...

_CONTEXT_CATEGORIES = {
    Category.GREETING, Category.QUESTION, Category.GAMEPLAY_SUGGESTION, Category.JOKE, Category.GENERAL,
}


@dataclass
class ProcessResult:
    # replied | ignored | duplicate | paused | rate_limited | no_reply | send_failed | dropped | error
    # | skipped (a proactive message that was not sent for a stated reason: chat active, rate limit, ...)
    # | held (approval mode: the message waits for the streamer to approve it)
    action: str
    category: Category | None = None
    reason: str | None = None
    message_id: int | None = None
    viewer_is_new: bool = False
    reply: str | None = None
    personality: str | None = None
    source: str | None = None  # ai | rule | command
    model: str | None = None
    latency_ms: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action, "category": self.category.value if self.category else None, "reason": self.reason,
            "message_id": self.message_id, "viewer_is_new": self.viewer_is_new, "reply": self.reply,
            "personality": self.personality, "source": self.source, "model": self.model,
            "latency_ms": self.latency_ms,
        }


@dataclass
class _Reply:
    text: str
    personality: str | None
    source: str
    model: str | None = None
    latency_ms: int | None = None


@dataclass(frozen=True)
class _Proactive:
    platform: Platform
    category: Category
    still_ok: Callable[[], bool]  # asked again right before sending: has a viewer spoken meanwhile?


@dataclass
class _Item:
    msg: ChatMessage | None
    future: asyncio.Future[ProcessResult] | None = field(default=None)
    proactive: _Proactive | None = None
    enqueued_at: float = 0.0  # processor clock when it was queued: a message that waited too long is not answered late


class ChatProcessor:
    def __init__(
        self, *, settings: Settings, repo: Repository, msg_filter: MessageFilter, personalities: PersonalityManager,
        generator: ResponseGenerator, rules: RuleEngine, adapters: dict[Platform, PlatformAdapter],
        context: SessionContext, recent: RecentReplies, metrics: Metrics, controls: RuntimeControls,
        session_id: Callable[[], int | None], clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._s, self._repo, self._filter = settings, repo, msg_filter
        self._personalities, self._generator, self._rules = personalities, generator, rules
        self._adapters, self._ctx, self._recent = adapters, context, recent
        self._metrics, self._controls, self._session_id, self._clock = metrics, controls, session_id, clock
        self.incoming: BoundedPriorityQueue[_Item] = BoundedPriorityQueue(settings.incoming_queue_max)
        self._limiters: dict[str, RateLimiter] = {}
        self._last_user_reply: dict[tuple[str, str], float] = {}
        self._last_voice: dict[tuple[str, str], tuple[str, float]] = {}  # who the bot last answered as, per viewer (conversations)
        self._task: asyncio.Task[None] | None = None
        self.activity: ActivityListener | None = None  # the proactive scheduler, set by the application
        self.learning: Any = None  # LearningService (notes, feedback, what gets chat talking), set by the application
        self.researcher: Any = None  # Researcher (looks up a question's key words), set by the application
        self._angles = AnglePicker()  # what GUY brings up when it speaks first
        self.on_viewer_arrival: Callable[[], Any] | None = None  # set by the application: stop a background look at the stream
        self.approvals = ApprovalQueue(settings.approval_queue_max, settings.approval_timeout_s, clock)

    # ------------------------------------------------------------------ queue / worker
    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="chat-worker")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        for item in self.incoming.drain():  # nobody is left waiting on a dead worker
            if item.future and not item.future.done():
                item.future.set_result(ProcessResult("dropped", reason="shutdown"))

    async def submit(self, msg: ChatMessage, *, wait: bool = True, timeout: float | None = None) -> ProcessResult | None:
        """Queue a message. With ``wait`` the call resolves once the message was fully processed."""
        future: asyncio.Future[ProcessResult] | None = asyncio.get_running_loop().create_future() if wait else None
        priority = 0 if msg.event_type is not EventType.MESSAGE else 1  # donations/memberships first
        if self.on_viewer_arrival is not None and not msg.is_bot_self:
            self.on_viewer_arrival()  # a picture being described must not keep this viewer waiting
        dropped = self.incoming.put_nowait(_Item(msg, future, enqueued_at=self._clock()), priority)
        if dropped is not None:
            self._metrics.inc("queue_dropped")
            if dropped.future and not dropped.future.done():
                dropped.future.set_result(ProcessResult("dropped", reason="queue_full"))
        if future is None:
            return None
        return await asyncio.wait_for(future, timeout or self._s.submit_timeout_s)

    async def submit_proactive(
        self, platform: Platform, category: Category, *, still_ok: Callable[[], bool], timeout: float | None = None,
    ) -> ProcessResult:
        """Queue a bot-initiated message (Mode B). It waits behind every viewer message and is the first thing dropped
        when the queue is full, so real viewers always come first."""
        future: asyncio.Future[ProcessResult] = asyncio.get_running_loop().create_future()
        dropped = self.incoming.put_nowait(
            _Item(None, future, _Proactive(platform, category, still_ok), enqueued_at=self._clock()), PROACTIVE_PRIORITY)
        if dropped is not None:
            self._metrics.inc("queue_dropped")
            if dropped.future and not dropped.future.done():
                dropped.future.set_result(ProcessResult("dropped", reason="queue_full"))
        return await asyncio.wait_for(future, timeout or self._s.submit_timeout_s)

    async def _run(self) -> None:
        while True:
            item = await self.incoming.get()
            waited = self._clock() - item.enqueued_at
            try:
                if item.proactive is not None:
                    if waited > self._s.max_queue_age_s:  # it was meant for a moment that has passed
                        result = self._skip("stale", item.proactive.category)
                    else:
                        result = await self.process_proactive(item.proactive)
                else:
                    assert item.msg is not None
                    result = await self.process(item.msg, queued_for_s=waited)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one bad message must never stop the worker
                logger.exception("Unhandled error while processing a message")
                self._metrics.inc("processing_errors")
                result = ProcessResult("error", reason=type(exc).__name__)
            if item.future and not item.future.done():
                item.future.set_result(result)

    # ------------------------------------------------------------------ pipeline
    async def process(self, msg: ChatMessage, queued_for_s: float = 0.0) -> ProcessResult:
        self._metrics.inc("messages_received")
        platform = msg.platform.value
        if msg.is_bot_self:
            return ProcessResult("ignored", reason="self_message")
        if msg.message_id and await self._repo.message_exists(platform, msg.message_id):
            self._metrics.inc("duplicates")
            return ProcessResult("duplicate", reason="message_id_seen")
        if self.activity is not None:
            self.activity.viewer_message(msg.platform)  # chat is not quiet (spam and commands count too: don't pile on)

        viewer = await self._repo.upsert_viewer(platform, msg.viewer_id, msg.username, msg.timestamp)
        cls: Classification = self._filter.classify(msg)
        stored_id = await self._repo.add_chat_message(
            session_id=self._session_id(), platform=platform, platform_message_id=msg.message_id,
            viewer_pk=viewer.pk, message=msg.message or f"[{msg.event_type.value}]", category=cls.category.value,
            filter_reason=cls.reason, timestamp=msg.timestamp,
        )
        if stored_id is None:  # lost a race with another copy of the same platform message id
            self._metrics.inc("duplicates")
            return ProcessResult("duplicate", reason="message_id_seen")
        base = {"category": cls.category, "message_id": stored_id, "viewer_is_new": viewer.is_new}

        if not cls.respond:
            self._metrics.inc(f"ignored_{cls.category.value}")
            return ProcessResult("ignored", reason=cls.reason, **base)
        if queued_for_s > self._s.max_queue_age_s and msg.event_type is EventType.MESSAGE:
            # It waited so long (an AI outage, a raid) that an answer now would be confusing. It stays in the history;
            # donations and memberships are still thanked, late is better than never for those.
            self._metrics.inc("stale_dropped")
            return ProcessResult("ignored", reason="stale_in_queue", **base)
        if cls.category in _CONTEXT_CATEGORIES:
            self._ctx.add_chat(msg.username, msg.message)
        if self.learning is not None:
            self.learning.on_viewer(msg, cls.category)
        if self._controls.outgoing_paused:
            return ProcessResult("paused", reason="outgoing_paused", **base)
        blocked = self._gate(msg)
        if blocked:
            self._metrics.inc("rate_limited")
            return ProcessResult("rate_limited", reason=blocked, **base)

        reply = await self._compose(msg, cls, viewer.is_new)
        if reply is None:
            return ProcessResult("no_reply", reason="no_personality_available", **base)
        return await self._deliver(msg, stored_id, reply, base)

    # ------------------------------------------------------------------ steps
    def _limiter(self, platform: str) -> RateLimiter:
        if platform not in self._limiters:
            lim = self._s.limits_for(platform)
            self._limiters[platform] = RateLimiter(lim.min_gap_s, lim.max_per_window, lim.window_s, self._clock)
        return self._limiters[platform]

    def _gate(self, msg: ChatMessage) -> str | None:
        platform = msg.platform.value
        lim = self._s.limits_for(platform)
        # The same-viewer cooldown protects chat from one noisy viewer. The streamer is in a conversation with the bot, so it does
        # not apply to them; the platform-wide limits below still do.
        if msg.event_type is EventType.MESSAGE and lim.user_cooldown_s > 0 and not msg.is_streamer:
            last = self._last_user_reply.get((platform, msg.viewer_id))
            if last is not None and self._clock() - last < lim.user_cooldown_s:
                return "user_cooldown"
        if not self._limiter(platform).allowed():
            return "platform_rate_limit"
        return None

    async def _compose(self, msg: ChatMessage, cls: Classification, is_new: bool) -> _Reply | None:
        if cls.category is Category.BOT_COMMAND:
            text = self._rules.handle_command(msg)
            return _Reply(text, None, "command") if text else None
        if cls.category is Category.EVENT:
            return _Reply(self._rules.event_reply(msg), None, "rule")

        profile = self._personalities.select(cls.category, prefer=self._conversation_voice(msg))
        if profile is None:
            return None
        if self._controls.ai_enabled and self._s.ai_enabled:
            viewer_note = self.learning.note_for(msg) if self.learning is not None else None
            try:
                if self.researcher is not None and cls.category is Category.QUESTION:
                    self._ctx.lookup_note = await self.researcher.lookup(msg.message) or ""  # skipped when off, too soon, or too slow
                gen = await self._generator.generate(
                    profile, cls.category, platform=msg.platform.value, username=msg.username,
                    message=msg.message, is_new_viewer=is_new, is_streamer=msg.is_streamer, is_voice=msg.via_voice,
                    viewer_note=viewer_note, speech_lang=msg.voice_lang if msg.via_voice else "",
                )
                return _Reply(gen.text, profile.name, "ai", gen.model, gen.latency_ms)
            except GenerationFailed as exc:
                logger.info("AI reply unavailable (%s); using rule-based fallback", exc.reason)
            finally:
                self._ctx.lookup_note = ""  # it belonged to this one question
        self._metrics.inc("rule_fallbacks")
        text = self._rules.fallback_reply(cls.category, msg.username, self._recent.all())
        return _Reply(text, profile.name, "rule")

    def _conversation_voice(self, msg: ChatMessage) -> str | None:
        """Which personality should keep this conversation going. The streamer always talks with the conversational one; a viewer
        the bot answered a moment ago keeps getting the same voice (so a chat with the bot does not flip between characters)."""
        if msg.is_streamer:
            return self._s.conversation_personality
        last = self._last_voice.get((msg.platform.value, msg.viewer_id))
        if last is not None and self._clock() - last[1] < self._s.conversation_sticky_s:
            return last[0]
        return None

    async def _deliver(self, msg: ChatMessage, stored_id: int, reply: _Reply, base: dict[str, Any]) -> ProcessResult:
        return await self._send_reply(msg.platform, msg, stored_id, reply, base, msg.viewer_id)

    async def _send_reply(
        self, platform_enum: Platform, reply_to: ChatMessage | None, stored_id: int | None, reply: _Reply,
        base: dict[str, Any], viewer_id: str | None,
    ) -> ProcessResult:
        """Store, send and book-keep one finished message. ``reply_to`` / ``stored_id`` / ``viewer_id`` are None for a
        proactive message (it answers nobody)."""
        platform = platform_enum.value
        adapter = self._adapters.get(platform_enum)
        if adapter is None:
            return ProcessResult("error", reason="no_adapter", **base)
        if self._controls.outgoing_paused:  # paused while the reply was being generated
            return ProcessResult("paused", reason="outgoing_paused", **base)

        text = fit_to_limit(reply.text, self._s.limits_for(platform).max_chars)
        response_id = await self._repo.add_bot_response(
            platform=platform, original_message_id=stored_id, personality=reply.personality, response=text,
            source=reply.source, model=reply.model, latency_ms=reply.latency_ms, now=now_utc(),
        )
        if self._controls.approval_required and reply.source != "command":
            return await self._hold(platform_enum, reply_to, viewer_id, response_id, text, reply, base)
        return await self._dispatch(platform_enum, adapter, reply_to, viewer_id, response_id, text, reply, base)

    async def _dispatch(
        self, platform_enum: Platform, adapter: PlatformAdapter, reply_to: ChatMessage | None, viewer_id: str | None,
        response_id: int, text: str, reply: _Reply, base: dict[str, Any],
    ) -> ProcessResult:
        """Send a stored reply and do the bookkeeping that follows a successful send."""
        platform = platform_enum.value
        try:
            sent: SendResult = await adapter.send(text, reply_to=reply_to)
        except Exception as exc:  # noqa: BLE001 - adapters should not raise, but never trust that
            logger.exception("Adapter %s raised while sending", platform)
            sent = SendResult(ok=False, error=f"{type(exc).__name__}: {exc}")
        await self._repo.set_delivery_status(response_id, "sent" if sent.ok else "failed", sent.error)

        result = dict(reply=text, personality=reply.personality, source=reply.source, model=reply.model,
                      latency_ms=reply.latency_ms, **base)
        if not sent.ok:
            self._metrics.inc("send_failures")
            logger.warning("Send to %s failed: %s", platform, sent.error)
            return ProcessResult("send_failed", reason=sent.error, **result)

        self._metrics.inc("replies_sent")
        self._limiter(platform).record()
        if viewer_id is not None:
            self._last_user_reply[(platform, viewer_id)] = self._clock()
            if reply.personality:
                self._last_voice[(platform, viewer_id)] = (reply.personality, self._clock())
        if len(self._last_user_reply) > 5000:  # bounded memory over long sessions
            cutoff = self._clock() - 3600
            self._last_user_reply = {k: v for k, v in self._last_user_reply.items() if v >= cutoff}
            self._last_voice = {k: v for k, v in self._last_voice.items() if v[1] >= cutoff}
        self._recent.add(platform, text)
        self._ctx.add_chat(self._s.bot_display_name, text, is_bot=True)
        if self.learning is not None:
            self.learning.on_bot(platform_enum, text, reply.personality, reply.source)
        if reply.personality:
            self._personalities.mark_used(reply.personality)
        await self._repo.add_chat_message(
            session_id=self._session_id(), platform=platform, platform_message_id=sent.platform_message_id,
            viewer_pk=None, message=text, category=None, filter_reason=None, timestamp=now_utc(), is_bot=True,
        )
        if self.activity is not None:
            self.activity.bot_message(platform_enum)
        return ProcessResult("replied", **result)

    # ------------------------------------------------------------------ manual approval (dashboard section B)
    async def _hold(
        self, platform_enum: Platform, reply_to: ChatMessage | None, viewer_id: str | None, response_id: int, text: str,
        reply: _Reply, base: dict[str, Any],
    ) -> ProcessResult:
        """Keep a finished message for the streamer to approve instead of sending it."""
        await self._repo.set_delivery_status(response_id, "awaiting_approval")
        entry = PendingReply(
            platform=platform_enum, text=text, personality=reply.personality, source=reply.source, model=reply.model,
            latency_ms=reply.latency_ms, response_id=response_id, category=base.get("category"), reply_to=reply_to,
            viewer_id=viewer_id,
        )
        for pushed_out in self.approvals.add(entry):
            await self._repo.set_delivery_status(pushed_out.response_id, "expired", "approval queue full")
        self._metrics.inc("replies_held")
        return ProcessResult("held", reply=text, personality=reply.personality, source=reply.source, model=reply.model,
                             latency_ms=reply.latency_ms, **base)

    async def sweep_approvals(self) -> int:
        """Drop approvals nobody decided about in time (called by a background task and before the list is shown)."""
        gone = self.approvals.expire()
        for entry in gone:
            await self._repo.set_delivery_status(entry.response_id, "expired", "not approved in time")
        if gone:
            self._metrics.inc("approvals_expired", len(gone))
        return len(gone)

    async def run_approval_sweeper(self, interval_s: float = 10.0) -> None:
        while True:
            await asyncio.sleep(interval_s)
            try:
                await self.sweep_approvals()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - housekeeping must never stop the bot
                logger.exception("approval sweep failed")

    async def approve(self, entry_id: int, edited_text: str | None = None) -> ProcessResult:
        """Send a held message (optionally with the streamer's edit). Raises KeyError if it is gone and ApprovalBlocked if
        it cannot be sent right now (the message then stays in the queue so the streamer can try again)."""
        entry = self.approvals.peek(entry_id)
        if entry is None:
            raise KeyError(entry_id)
        adapter = self._adapters.get(entry.platform)
        platform = entry.platform.value
        if adapter is None or adapter.status().state is not ConnectionState.CONNECTED:
            raise ApprovalBlocked("not_connected", f"{platform} is not connected")
        if self._controls.outgoing_paused:
            raise ApprovalBlocked("outgoing_paused", "sending is paused: resume first")
        limiter = self._limiter(platform)
        if not limiter.allowed():
            raise ApprovalBlocked("platform_rate_limit", f"{platform} message limit reached; try again in {limiter.retry_after():.0f} s")
        self.approvals.take(entry_id)
        text = fit_to_limit(edited_text if edited_text is not None else entry.text, self._s.limits_for(platform).max_chars)
        if text != entry.text:
            await self._repo.update_bot_response_text(entry.response_id, text)
        reply = _Reply(text, entry.personality, entry.source, entry.model, entry.latency_ms)
        base = {"category": entry.category}
        result = await self._dispatch(entry.platform, adapter, entry.reply_to, entry.viewer_id, entry.response_id, text, reply, base)
        self._metrics.inc("approvals_approved")
        return result

    async def reject(self, entry_id: int) -> None:
        entry = self.approvals.take(entry_id)
        if entry is None:
            raise KeyError(entry_id)
        await self._repo.set_delivery_status(entry.response_id, "rejected", "rejected by the streamer")
        self._metrics.inc("approvals_rejected")

    def apply_rate_limits(self, platform: str) -> None:
        """Tell a running limiter about edited limits (a limiter that does not exist yet reads them when it is created)."""
        limiter = self._limiters.get(platform)
        if limiter is not None:
            lim = self._s.limits_for(platform)
            limiter.reconfigure(lim.min_gap_s, lim.max_per_window, lim.window_s)

    # ------------------------------------------------------------------ proactive messages (Mode B)
    def _skip(self, reason: str, category: Category) -> ProcessResult:
        self._metrics.inc(f"proactive_skipped_{reason}")
        return ProcessResult("skipped", category=category, reason=reason)

    async def process_proactive(self, task: _Proactive) -> ProcessResult:
        """Say something unprompted. Every check that protects viewers, the platform and the quota runs here, at the
        moment of sending, because the world may have changed while the task waited in the queue."""
        platform_enum, category = task.platform, task.category
        platform = platform_enum.value
        adapter = self._adapters.get(platform_enum)
        if adapter is None:
            return self._skip("no_adapter", category)
        if self._controls.outgoing_paused:
            return self._skip("outgoing_paused", category)
        if not self._controls.proactive_enabled:
            return self._skip("proactive_disabled", category)
        if adapter.status().state is not ConnectionState.CONNECTED:
            return self._skip("not_connected", category)
        if not task.still_ok():
            return self._skip("chat_active", category)

        # Keep part of the platform's message allowance for answering viewers.
        limiter, limits = self._limiter(platform), self._s.limits_for(platform)
        if not limiter.allowed():
            return self._skip("platform_rate_limit", category)
        reserve = int(limits.max_per_window * self._s.proactive_viewer_reserve)
        if limiter.used_in_window() >= limits.max_per_window - reserve:
            return self._skip("viewer_reserve", category)

        profile = self._personalities.select(category)
        if profile is None:
            return self._skip("no_personality_ready", category)
        if self._controls.ai_enabled and self._s.ai_enabled:
            self._ctx.angle = self._angles.pick(self._ctx, prefer="screen" if category is Category.PROACTIVE_HYPE else None) or ""
            try:
                gen = await self._generator.generate(
                    profile, category, platform=platform, username=None, message=None,
                )
                reply = _Reply(gen.text, profile.name, "ai", gen.model, gen.latency_ms)
            except GenerationFailed as exc:
                logger.info("Proactive AI message unavailable (%s); trying a prepared one", exc.reason)
                reply = None
            finally:
                self._ctx.angle = ""  # it belonged to this one message
        else:
            reply = None
        if reply is None:
            self._metrics.inc("rule_fallbacks")
            text = self._rules.fallback_reply(category, "", self._recent.all())
            if compare_key(text) in {compare_key(r) for r in self._recent.all()}:
                return self._skip("nothing_new_to_say", category)  # never repeat ourselves just to fill the silence
            reply = _Reply(text, profile.name, "rule")

        # Generating took seconds; a viewer may have spoken, chat may have been paused: check again.
        if not task.still_ok():
            return self._skip("chat_active", category)
        if self._controls.outgoing_paused:
            return self._skip("outgoing_paused", category)
        result = await self._send_reply(platform_enum, None, None, reply, {"category": category}, None)
        self._metrics.inc({"replied": "proactive_sent", "held": "proactive_held"}.get(result.action, "proactive_failed"))
        return result

"""Kick chat adapter.

How it works (the official route; see docs/phase4-kick-research.md): Kick delivers chat messages to a webhook URL, so the adapter
runs a small signed-webhook receiver on 127.0.0.1 (a tunnel program publishes it), keeps the webhook subscription alive, and posts
replies through the REST API. Everything is isolated in this adapter's own task: a Kick problem never stops the app or another
platform, auth problems are not retried in a tight loop, and failures back off exponentially.

State meaning: CONNECTED = signed in, webhook listener running and the subscription exists. Whether Kick can actually REACH the
listener (your tunnel, the URL typed into Kick's settings) cannot be known from here: until the first event arrives the detail
says so, and the first live check is the proof.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import deque
from typing import Any, Awaitable, Callable

from ...chat.models import ChatMessage, Platform
from ...chat.normalizer import compare_key
from ...config import Settings
from ..base import ConnectionState, ConnectionStatus, OnMessage, PlatformAdapter, SendResult
from ..youtube.adapter import Backoff
from .api import (
    AuthError, BadRequest, Forbidden, KickApi, KickApiError, NetworkError, NotFound, RateLimited, ServerError,
)
from .credentials import KickAuth, NotAuthorized, ReauthRequired
from .normalizer import CHAT_EVENT, normalize
from .oauth import SCOPES, KickOAuthError
from .webhook import VerifiedEvent, WebhookServer, WebhookVerifier, build_webhook_app

logger = logging.getLogger(__name__)
LogEvent = Callable[[str, str], Awaitable[None]]
ServerFactory = Callable[[Any, int], WebhookServer]
RECENT_ECHO_WINDOW_S = 60.0


class MissingScopes(Exception):
    """The sign-in did not grant everything the bot needs: sign in again."""


class KickAdapter(PlatformAdapter):
    platform = Platform.KICK

    def __init__(
        self, settings: Settings, api: KickApi, auth: KickAuth, *, log_event: LogEvent | None = None,
        clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        rng: random.Random | None = None, server_factory: ServerFactory = WebhookServer,
    ) -> None:
        self._s, self._api, self._auth = settings, api, auth
        self._log_event, self._clock, self._sleep = log_event, clock, sleep
        self._rng = rng or random.Random()
        self._server_factory = server_factory
        self._on_message: OnMessage | None = None
        self._task: asyncio.Task[None] | None = None
        self._bg: set[asyncio.Task[Any]] = set()
        self._status = ConnectionStatus()
        self._stopping = False
        self._send_lock = asyncio.Lock()
        self._me: dict[str, Any] | None = None
        self._channel: dict[str, Any] = {}  # the signed-in account's OWN channel
        # The channel whose chat we read and answer in: our own, or (kick_target_channel / retarget) another one. Chat of another
        # channel is read through the APP token, and answers are posted as the signed-in account (a normal user message).
        self._target_slug: str | None = settings.kick_target_channel or None
        self._answer: dict[str, Any] | None = None  # {"slug", "broadcaster_user_id", "is_live", "own": bool, ...}
        self._server: WebhookServer | None = None
        self.verifier: WebhookVerifier | None = None
        self._subscribed: set[str] = set()
        self._ever_subscribed = False
        self._last_event_at: float | None = None
        self._connected_at: float | None = None
        self._sent_ids: deque[str] = deque(maxlen=200)
        self._sent_texts: deque[tuple[str, float]] = deque(maxlen=30)
        self.stats = {"events": 0, "messages_delivered": 0, "ignored_events": 0, "other_channel_events": 0, "reconnects": 0,
                      "resubscribes": 0, "sends_ok": 0, "sends_failed": 0}

    # ------------------------------------------------------------------ PlatformAdapter
    async def start(self, on_message: OnMessage) -> None:
        self._on_message = on_message
        self._stopping = False
        if self._task is None or self._task.done():
            self._set(ConnectionState.CONNECTING, "starting")
            self._task = asyncio.create_task(self._run(), name="kick-adapter")

    async def stop(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._server is not None:
            await self._server.stop()
            self._server = None
        await self._flush_bg()
        self._set(ConnectionState.DISCONNECTED, "stopped")
        await self._flush_bg()

    def status(self) -> ConnectionStatus:
        return self._status

    async def retarget(self, slug: str | None) -> None:
        """Chat in another channel (``None`` = the signed-in account's own). Reconnects if the adapter is running; if it was
        stopped (for example from the dashboard) it stays stopped."""
        slug = (slug or "").strip().lower() or None
        if slug == self._target_slug:
            return
        self._target_slug = slug
        self._answer = None
        self._subscribed = set()
        self._last_event_at = None
        if self._task is not None and not self._task.done() and self._on_message is not None:
            await self.stop()
            await self.start(self._on_message)

    def details(self) -> dict[str, Any]:
        age = None if self._last_event_at is None else round(self._clock() - self._last_event_at)
        answer = self._answer or self._channel
        return {
            "channel": answer.get("slug") or None, "account": (self._me or {}).get("name") or None,
            "own_channel": self._channel.get("slug") or None, "target_channel": self._target_slug,
            "answering_in_own_channel": bool(self._answer is None or self._answer.get("own")),
            "is_live": answer.get("is_live"), "message_type": self._s.kick_message_type,
            "webhook_port": self._server.port if self._server else None, "webhook_public_url": self._s.kick_webhook_public_url,
            "subscribed_events": sorted(self._subscribed), "last_event_s_ago": age,
            "permission": " ".join(sorted(self._auth.scopes())) or None, **self.stats,
        }

    # ------------------------------------------------------------------ status / logging helpers
    def _set(self, state: ConnectionState, detail: str | None) -> None:
        from ...chat.normalizer import now_utc

        changed = state is not self._status.state or detail != self._status.detail
        since = now_utc() if state is not self._status.state else self._status.since
        self._status = ConnectionStatus(state, detail, since)
        if changed:
            logger.info("Kick %s: %s", state.value, detail)
            if self._log_event:
                task = asyncio.ensure_future(self._log_event(state.value, detail or ""))
                self._bg.add(task)
                task.add_done_callback(self._bg.discard)

    async def _flush_bg(self) -> None:
        if self._bg:
            await asyncio.gather(*list(self._bg), return_exceptions=True)

    def _connected_detail(self) -> str:
        port = self._server.port if self._server else "?"
        if self._answer is not None and not self._answer.get("own"):
            who = f"signed in as {(self._me or {}).get('name') or '?'}; chatting in {self._answer.get('slug')}"
        else:
            who = f"signed in as {self._channel.get('slug') or (self._me or {}).get('name') or '?'}"
        base = f"{who}; webhook listener on 127.0.0.1:{port}"
        if self._last_event_at is None:
            waited = 0 if self._connected_at is None else self._clock() - self._connected_at
            hint = ""
            if waited > 120:
                hint = ("; NO event received yet: check that the tunnel is running and that its public URL is the webhook URL in "
                        "Kick's developer settings")
            return base + "; waiting for the first event" + hint
        return base + "; receiving events"

    # ------------------------------------------------------------------ main loop
    async def _run(self) -> None:
        backoff = Backoff(rng=self._rng)
        while not self._stopping:
            try:
                await self._ensure_identity()
                await self._ensure_server()
                await self._ensure_subscriptions()
                backoff.reset()
                self._connected_at = self._clock()
                self._set(ConnectionState.CONNECTED, self._connected_detail())
                await self._maintain()
            except asyncio.CancelledError:
                raise
            except (NotAuthorized, ReauthRequired) as exc:
                self._set(ConnectionState.ERROR, f"not authorised: {exc} (run scripts\\kick_login.py)")
                await self._sleep(300)  # no API traffic while unauthorised; re-check in case the user signed in again
            except MissingScopes as exc:
                self._set(ConnectionState.ERROR, f"{exc} (run scripts\\kick_login.py again)")
                await self._sleep(300)
            except Forbidden as exc:
                self._set(ConnectionState.ERROR, f"Kick refused: {exc.detail or 'forbidden'} (missing permission?)")
                await self._sleep(300)
            except OSError as exc:  # the webhook port is taken, or the listener died
                delay = backoff.next()
                self.stats["reconnects"] += 1
                self._set(ConnectionState.ERROR, f"webhook listener problem: {exc}; retrying in {delay:.0f}s")
                await self._sleep(delay)
            except RateLimited as exc:
                delay = exc.retry_after if exc.retry_after else backoff.next()
                self.stats["reconnects"] += 1
                self._set(ConnectionState.ERROR, f"Kick is rate limiting us; retrying in {delay:.0f}s")
                await self._sleep(delay)
            except (NetworkError, ServerError, AuthError, BadRequest, NotFound, KickApiError, KickOAuthError) as exc:
                delay = backoff.next()
                self.stats["reconnects"] += 1
                reason = getattr(exc, "detail", None) or getattr(exc, "error", None) or exc.__class__.__name__
                self._set(ConnectionState.ERROR, f"{reason}; retrying in {delay:.0f}s")
                await self._sleep(delay)
            except Exception as exc:  # noqa: BLE001 - last line of defence: the adapter must never die
                logger.exception("Unexpected Kick adapter error")
                delay = backoff.next()
                self._set(ConnectionState.ERROR, f"unexpected {exc.__class__.__name__}; retrying in {delay:.0f}s")
                await self._sleep(delay)

    async def _ensure_identity(self) -> None:
        if self._me is None:
            me = await self._api.me()
            channel = await self._api.channel()
            granted = self._auth.scopes()
            missing = sorted(set(SCOPES) - granted) if granted else []
            if missing:
                raise MissingScopes(f"the sign-in did not grant: {', '.join(missing)}")
            if self._s.kick_message_type == "user" and channel.get("broadcaster_user_id") is None:
                raise BadRequest(None, "Kick did not tell us the channel id, which messages sent as 'user' need")
            self._me, self._channel = me, channel
            logger.info("Kick account identified (channel %s)", channel.get("slug") or "?")
        if self._answer is None:
            self._answer = await self._resolve_answer()
            if not self._answer["own"]:
                logger.info("Kick: chatting in channel %s (read through the app token, answers posted as the signed-in account)",
                            self._answer.get("slug"))

    async def _resolve_answer(self) -> dict[str, Any]:
        slug = self._target_slug
        if not slug or slug == (self._channel.get("slug") or "").lower():
            return {**self._channel, "own": True}
        info = await self._api.channel_by_slug(slug)  # GET /channels?slug=: works with the signed-in account's token
        if info is None:
            raise BadRequest(404, f"Kick has no channel called '{slug}'")
        return {**info, "own": False}

    async def _ensure_server(self) -> None:
        if self._server is not None and self._server.running:
            return
        if self._server is not None:
            await self._server.stop()
        self.verifier = WebhookVerifier(self._api.public_key, tolerance_s=self._s.kick_webhook_tolerance_s)
        server = self._server_factory(build_webhook_app(self.verifier, self._on_event), self._s.kick_webhook_port)
        await server.start()  # OSError if the port is taken
        self._server = server

    async def _ensure_subscriptions(self) -> None:
        wanted = [(name, 1) for name in self._s.kick_events]
        answer = self._answer or {"own": True}
        if answer["own"]:
            existing = await self._api.list_subscriptions()
        else:  # another channel: the app token lists every subscription of the app; only this channel's count
            channel_id = str(answer["broadcaster_user_id"])
            existing = [d for d in await self._api.list_subscriptions(app=True) if str(d.get("broadcaster_user_id")) == channel_id]
        have = {(d.get("event"), int(d.get("version") or 1)) for d in existing}
        missing = [w for w in wanted if w not in have]
        if missing:  # re-subscribing an existing event is NOT DOCUMENTED, so only the missing ones are requested
            if answer["own"]:
                results = await self._api.subscribe(missing)
            else:
                results = await self._api.subscribe(missing, broadcaster_user_id=int(answer["broadcaster_user_id"]), app=True)
            failed = [f"{r.get('name')}: {r.get('error')}" for r in results if r.get("error")]
            if failed:
                raise BadRequest(None, "Kick refused the subscription (" + "; ".join(failed)[:200] + ")")
            if self._ever_subscribed:
                self.stats["resubscribes"] += 1
                if self._log_event:
                    await self._log_event("resubscribed", ", ".join(n for n, _ in missing))
            logger.info("Subscribed to Kick events: %s", ", ".join(n for n, _ in missing))
        self._subscribed = {n for n, _ in wanted}
        self._ever_subscribed = True

    async def _maintain(self) -> None:
        """Stay connected: keep the subscription alive (Kick drops one whose webhook failed for a day) and watch the listener."""
        while not self._stopping:
            await self._sleep(self._s.kick_resubscribe_interval_s)
            if self._server is None or not self._server.running:
                raise OSError("the webhook listener stopped")
            await self._ensure_subscriptions()
            self._channel = {**self._channel, **await self._api.channel()}
            if self._answer is not None and self._answer["own"]:
                self._answer = {**self._channel, "own": True}
            elif self._answer is not None:
                info = await self._api.channel_by_slug(str(self._answer.get("slug")))  # is the channel we chat in live?
                if info is not None:
                    self._answer = {**info, "own": False}
            self._set(ConnectionState.CONNECTED, self._connected_detail())

    # ------------------------------------------------------------------ receiving (called by the webhook app)
    def _own_ids(self) -> set[str]:
        ids = {str(self._me["user_id"])} if self._me else set()
        if self._s.kick_bot_user_id:
            ids.add(str(self._s.kick_bot_user_id))
        return ids

    def _is_own_echo(self, msg: ChatMessage) -> bool:
        if msg.message_id in self._sent_ids:
            return True
        now = self._clock()
        key = compare_key(msg.message)
        # The bot's identity on Kick is not documented, and the webhook can arrive before the send call returns its message id:
        # so a message with exactly the text we just sent is treated as our own echo (a viewer repeating us word for word is lost).
        return bool(key) and any(t == key and now - at < RECENT_ECHO_WINDOW_S for t, at in self._sent_texts)

    async def _on_event(self, event: VerifiedEvent) -> None:
        self.stats["events"] += 1
        self._last_event_at = self._clock()
        if self.stats["events"] <= 30:  # ids only (no names, no text): enough to see WHICH channel and account an event is about
            payload = event.payload if isinstance(event.payload, dict) else {}
            logger.info("Kick event #%d: %s in channel '%s' (%s) from user id %s", self.stats["events"], event.event_type,
                        str((payload.get("broadcaster") or {}).get("channel_slug") or "?")[:30],
                        (payload.get("broadcaster") or {}).get("user_id"), (payload.get("sender") or {}).get("user_id"))
        if event.version != 1 or event.event_type not in self._s.kick_events or self._on_message is None:
            self.stats["ignored_events"] += 1
            return
        sent_in = (event.payload.get("broadcaster") or {}).get("user_id") if isinstance(event.payload.get("broadcaster"), dict) else None
        own_channel = (self._answer or self._channel).get("broadcaster_user_id")
        if sent_in is not None and own_channel is not None and str(sent_in) != str(own_channel):
            # The app may also be subscribed to other channels (an old subscription, or the one we just left). Replies are posted to
            # the channel we answer in, so answering chat from somewhere else would put the answer in the wrong chat: count it,
            # do not process it.
            self.stats["ignored_events"] += 1
            self.stats["other_channel_events"] += 1
            if self.stats["other_channel_events"] <= 10:  # ids and the channel name only, never the message
                logger.info("Kick event for another channel ignored (it is for broadcaster %s '%s'; we answer in %s)", sent_in,
                            str((event.payload.get("broadcaster") or {}).get("channel_slug") or "?")[:40], own_channel)
            return
        msg = normalize(event.event_type, event.payload, event.message_id, self._own_ids())
        if msg is None:
            self.stats["ignored_events"] += 1
            return
        if not msg.is_bot_self and event.event_type == CHAT_EVENT and self._is_own_echo(msg):
            msg.is_bot_self = True
        self.stats["messages_delivered"] += 1
        await self._on_message(msg)

    # ------------------------------------------------------------------ sending
    async def send(self, text: str, *, reply_to: ChatMessage | None = None) -> SendResult:
        if self._status.state is not ConnectionState.CONNECTED or self._me is None:
            return SendResult(ok=False, error="kick is not connected", retryable=True)
        reply_id = reply_to.message_id if (self._s.kick_reply_threads and reply_to is not None) else None
        answer = self._answer or {**self._channel, "own": True}
        # The 'bot' type always goes to the signed-in account's OWN channel (documented), so another channel is always a
        # normal user message from the signed-in account, addressed to that channel's id.
        kind = self._s.kick_message_type if answer.get("own", True) else "user"
        async with self._send_lock:
            self._sent_texts.append((compare_key(text), self._clock()))  # before the call: the echo may beat the answer
            try:
                result = await self._api.send_chat(
                    text, kind=kind, reply_to=reply_id, broadcaster_user_id=answer.get("broadcaster_user_id"),
                )
            except (NotAuthorized, ReauthRequired) as exc:
                self.stats["sends_failed"] += 1
                self._set(ConnectionState.ERROR, f"not authorised: {exc} (run scripts\\kick_login.py)")
                return SendResult(ok=False, error="kick is not authorised")
            except Forbidden as exc:
                self.stats["sends_failed"] += 1
                return SendResult(ok=False, error=f"Kick refused the message ({exc.detail or 'forbidden'}): needs chat:write")
            except (RateLimited, NetworkError, ServerError) as exc:
                self.stats["sends_failed"] += 1
                return SendResult(ok=False, error=f"kick send failed: {exc.detail or exc.__class__.__name__}", retryable=True)
            except KickApiError as exc:
                self.stats["sends_failed"] += 1
                return SendResult(ok=False, error=f"kick send failed: {exc.detail or exc.__class__.__name__}")
        if not result["is_sent"]:
            self.stats["sends_failed"] += 1
            return SendResult(ok=False, error="Kick did not send the message")
        message_id = result.get("message_id")
        if message_id:
            self._sent_ids.append(str(message_id))
        self.stats["sends_ok"] += 1
        return SendResult(ok=True, platform_message_id=str(message_id) if message_id else None)

"""The relay loop. Every ``relay_poll_s`` it takes the pending commands the web panel left in Supabase and runs them; every
``relay_status_s`` it publishes a status snapshot for the panel.

Commands are never executed by new code paths: each one is translated into a call to the bot's OWN local API, in-process (ASGI),
so every rule of the local control panel (validation, limits, all-or-nothing settings) applies unchanged. What a command may do is
fixed here (``COMMANDS``), by the role of the person who sent it (``ROLE_KINDS``, looked up in public.profiles by the bot) and,
a layer further out, by the database policies in supabase/schema.sql. A command that is too old, from a user without a role, of an
unknown kind, or with fields we do not know is refused and the refusal is written back so the panel can show it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

import httpx

from .supabase import RelayAuthError, RelayHttpError, SupabaseClient

logger = logging.getLogger(__name__)

# kind -> (path on the local API, payload fields it accepts). "{id}" in a path is filled from payload["id"].
COMMANDS: dict[str, tuple[str, frozenset[str]]] = {
    "pause": ("/api/control/pause", frozenset()),
    "resume": ("/api/control/resume", frozenset()),
    "set_approval": ("/api/control/approval", frozenset({"enabled"})),
    "set_ai": ("/api/control/ai", frozenset({"enabled"})),
    "set_proactive": ("/api/control/proactive", frozenset({"enabled"})),
    "approve": ("/api/approvals/{id}/approve", frozenset({"id", "text"})),
    "reject": ("/api/approvals/{id}/reject", frozenset({"id"})),
    "select_stream": ("/api/streams/select", frozenset({"target_id"})),
    "add_stream": ("/api/streams/targets", frozenset({"platform", "ref", "label", "priority", "enabled"})),
    "update_stream": ("/api/streams/targets/{id}", frozenset({"id", "label", "priority", "enabled"})),
    "remove_stream": ("/api/streams/targets/{id}/remove", frozenset({"id"})),
    "stream_settings": ("/api/streams/settings",
                        frozenset({"auto_select", "failover_after_s", "poll_interval_s", "youtube_channel_probe_s"})),
    "set_topic": ("/api/topic", frozenset({"game", "hint", "notes", "lookup"})),
}
ROLE_KINDS: dict[str, frozenset[str]] = {
    "admin": frozenset(COMMANDS) | {"say"},
    "streamer": frozenset({"say"}),  # the hold-to-speak role: it can talk, nothing else
}
_ID_RE = {"approve": r"\d{1,9}", "reject": r"\d{1,9}", "update_stream": r"[a-z0-9-]{3,40}", "remove_stream": r"[a-z0-9-]{3,40}"}
SAY_MAX_CHARS = 500
STATUS_MAX_BYTES = 120_000
PRUNE_EVERY_S = 600.0

SayHandler = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


def _parse_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class RelayService:
    def __init__(self, app: Any, settings: Any, client: SupabaseClient, *, clock: Callable[[], float] = time.monotonic,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep) -> None:
        self._app, self._s, self._client = app, settings, client
        self._clock, self._now, self._sleep = clock, now, sleep
        self._local: httpx.AsyncClient | None = None
        self._failures = 0
        self._last_prune = float("-inf")
        self.say_handler: SayHandler | None = None  # set when voice replies are switched on
        self.counters = {"done": 0, "failed": 0, "rejected": 0, "status_published": 0}
        self.error: str | None = None
        self.last_command: dict[str, Any] | None = None
        self.last_status_at: float | None = None

    async def aclose(self) -> None:
        if self._local is not None:
            await self._local.aclose()
        await self._client.aclose()

    def public_state(self) -> dict[str, Any]:
        return {"connected": self.error is None and self.last_status_at is not None, "error": self.error,
                "say_enabled": self.say_handler is not None,
                "signed_in_as": self._client.signed_in_as, "commands": dict(self.counters), "last_command": self.last_command,
                "last_status_s_ago": None if self.last_status_at is None else round(self._clock() - self.last_status_at)}

    # ------------------------------------------------------------------ the bot's own API, in-process
    def _api(self) -> httpx.AsyncClient:
        if self._local is None:
            self._local = httpx.AsyncClient(transport=httpx.ASGITransport(app=self._app), base_url="http://127.0.0.1", timeout=30.0,
                                            headers={"X-API-Key": self._app.state.container.api_token})
        return self._local

    async def _get(self, path: str) -> Any:
        resp = await self._api().get(path)
        return resp.json() if resp.status_code == 200 else None

    # ------------------------------------------------------------------ commands
    async def process_commands(self) -> int:
        handled = 0
        for row in await self._client.pending_commands(20):
            cid = row.get("id")
            if not isinstance(cid, int) or not await self._client.claim(cid):  # someone else took it, or it vanished
                continue
            try:
                status, result = await self._execute(row)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad command must never stop the loop
                logger.exception("relay command %s crashed", cid)
                status, result = "failed", {"error": "the bot could not run this command"}
            await self._client.finish(cid, status, result)
            self.counters[status if status in self.counters else "failed"] += 1
            self.last_command = {"id": cid, "kind": str(row.get("kind"))[:30], "status": status}
            logger.info("Relay command %s (%s): %s", cid, str(row.get("kind"))[:30], status)
            handled += 1
        return handled

    async def _execute(self, row: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        kind, payload, creator = row.get("kind"), row.get("payload"), row.get("created_by")
        created = _parse_time(row.get("created_at"))
        if created is None or self._now() - created > timedelta(seconds=self._s.relay_command_max_age_s):
            return "rejected", {"error": "the command was too old to run safely (sent while the bot was away)"}
        if not isinstance(kind, str) or not isinstance(creator, str):
            return "rejected", {"error": "malformed command"}
        role = await self._client.role_of(creator)
        if role is None:
            return "rejected", {"error": "the sender has no role"}
        if kind not in ROLE_KINDS.get(role, frozenset()):
            return "rejected", {"error": f"the {role} role cannot run '{kind}'"}
        if not isinstance(payload, dict):
            return "rejected", {"error": "the payload must be an object"}
        if kind == "say":
            return await self._say(payload, role, creator)
        path, fields = COMMANDS[kind]
        extra = sorted(set(payload) - fields)
        if extra:
            return "rejected", {"error": f"unknown field '{str(extra[0])[:30]}' for '{kind}'"}
        body = dict(payload)
        if "{id}" in path:
            ident = str(body.pop("id", ""))
            if not re.fullmatch(_ID_RE[kind], ident):
                return "rejected", {"error": "bad id"}
            path = path.replace("{id}", ident)
        resp = await self._api().post(path, **({"json": body} if body else {}))
        try:
            answer = resp.json()
        except ValueError:
            answer = None
        if 200 <= resp.status_code < 300:
            result: dict[str, Any] = {"ok": True}
            if answer is not None and len(json.dumps(answer)) <= 1500:
                result["response"] = answer
            return "done", result
        detail = answer.get("detail") if isinstance(answer, dict) else None
        return "failed", {"ok": False, "http": resp.status_code, "error": json.dumps(detail)[:300] if detail is not None else "refused"}

    async def _say(self, payload: dict[str, Any], role: str, creator: str) -> tuple[str, dict[str, Any]]:
        text = payload.get("text")
        extra = sorted(set(payload) - {"text", "lang"})
        if extra or not isinstance(text, str) or not 1 <= len(text.strip()) <= SAY_MAX_CHARS:
            return "rejected", {"error": f"'say' needs text of 1-{SAY_MAX_CHARS} characters and nothing else (text, lang)"}
        if self.say_handler is None:
            return "failed", {"ok": False, "error": "voice replies are not switched on yet"}
        result = await self.say_handler(text.strip(), {"role": role, "user": creator, "lang": str(payload.get("lang") or "")[:10]})
        if result.get("ok", True):
            return "done", {"ok": True, **result}
        return "failed", {**result, "ok": False}

    # ------------------------------------------------------------------ status
    async def build_status(self) -> dict[str, Any]:
        n = self._s.relay_feed_items
        status, streams, topic, approvals, feed = await asyncio.gather(
            self._get("/api/status"), self._get("/api/streams"), self._get("/api/topic"), self._get("/api/approvals"),
            self._get(f"/api/messages?limit={n}") if n else asyncio.sleep(0, result=[]))
        lines = []
        for m in (feed or [])[:n]:
            reply = m.get("response") or {}
            lines.append({"time": m.get("timestamp"), "platform": m.get("platform"), "user": str(m.get("username"))[:30],
                          "text": str(m.get("message"))[:200], "category": m.get("category"), "bot": m.get("is_bot_message"),
                          "reply": str(reply.get("text"))[:200] if reply.get("text") else None, "delivery": reply.get("delivery_status")})
        data = {"v": 1, "published_at": self._now().isoformat(), "status": status, "streams": streams, "topic": topic,
                "approvals": approvals, "feed": lines, "relay": self.public_state()}
        if len(json.dumps(data)) > STATUS_MAX_BYTES:
            data["feed"] = lines[:5]
        return data

    async def publish_status(self) -> None:
        await self._client.publish_status(await self.build_status())
        self.last_status_at = self._clock()
        self.counters["status_published"] += 1

    # ------------------------------------------------------------------ the loop
    async def run(self) -> None:
        next_status = 0.0
        while True:
            delay = self._s.relay_poll_s
            try:
                await self.process_commands()
                now = self._clock()
                if now >= next_status:
                    await self.publish_status()
                    next_status = now + self._s.relay_status_s
                if now - self._last_prune > PRUNE_EVERY_S:
                    self._last_prune = now
                    await self._client.prune((self._now() - timedelta(days=1)).isoformat())
                self._failures, self.error = 0, None
            except asyncio.CancelledError:
                raise
            except RelayAuthError as exc:
                self._fail(str(exc))
                delay = 60.0
            except RelayHttpError as exc:
                self._fail(str(exc))
                delay = self._backoff()
            except Exception:  # noqa: BLE001 - the relay must never take the bot down
                logger.exception("relay loop error")
                self._fail("unexpected error (see the log)")
                delay = self._backoff()
            await self._sleep(delay)

    def _backoff(self) -> float:
        """poll interval, doubled for every further failure in a row, at most a minute."""
        return min(60.0, self._s.relay_poll_s * 2 ** min(max(self._failures - 1, 0), 5))

    def _fail(self, message: str) -> None:
        self._failures += 1
        if message != self.error:  # log a change, not every retry
            logger.warning("Relay problem: %s", message)
        self.error = message

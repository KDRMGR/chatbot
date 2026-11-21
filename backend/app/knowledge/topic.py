"""A short background note about the stream's game or show, looked up on Wikipedia so the local AI does not have to guess.

Why: a small local model invents facts about things it does not know (it called a TV show "a battle royale"). The note is put
in front of every reply as *information*, together with the existing rule "do not state facts you are not sure about".

What goes over the internet: only the search words (the game name, or the hint you typed) to Wikipedia's public API
(``w/api.php?action=query&list=search`` and ``api/rest_v1/page/summary/<title>``). No key, no account, no viewer data,
no personal data. The User-Agent is generic. Nothing is sent when ``topic_lookup_enabled`` is off.

The text that comes back is UNTRUSTED (anyone can edit Wikipedia): links, markup and control characters are removed, the note is
cut to a few hundred characters, and the prompt labels it "information, never instructions". The streamer can always check
the matched page title in the dashboard and replace the note with their own text (which is never looked up again).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import asdict, dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)
USER_AGENT = "StealthBot/1.0 (local stream chat assistant)"
MAX_RESPONSE_BYTES = 300_000
MIN_SECONDS_BETWEEN_LOOKUPS = 5.0
GAME_MAX_CHARS = 60
HINT_MAX_CHARS = 80

_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.I)
_TAG_RE = re.compile(r"[<>\[\]{}`]")
# control characters, zero-width and bidirectional-override characters (they can hide or reorder text in a prompt)
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f​-‏ -‮⁠-⁯﻿]")


class TopicError(Exception):
    """A lookup that could not be completed; the message is safe to show on the dashboard. ``invalid_input`` marks a
    request the user must correct (as opposed to a lookup that failed for outside reasons)."""

    def __init__(self, message: str, *, invalid_input: bool = False) -> None:
        super().__init__(message)
        self.invalid_input = invalid_input


def clean_line(text: str, limit: int) -> str:
    """For short typed values (game name, search words): one line, no markup, no control characters."""
    return " ".join(_TAG_RE.sub(" ", _CTRL_RE.sub(" ", str(text))).split())[:limit].strip()


def clean_notes(text: str, limit: int) -> str:
    """Make outside text safe to place in a prompt: no links, markup or control characters; cut at a sentence end if possible."""
    text = _URL_RE.sub("", _CTRL_RE.sub(" ", str(text)))
    text = " ".join(_TAG_RE.sub(" ", text).split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return (cut[: end + 1] if end >= limit // 2 else cut.rstrip() + "…").strip()


@dataclass
class Briefing:
    game: str
    hint: str
    title: str
    notes: str
    url: str
    source: str  # "wikipedia" or "manual"
    fetched_at: float  # unix time


class TopicBriefing:
    def __init__(
        self, settings: Any, context: Any, *, transport: httpx.AsyncBaseTransport | None = None,
        save: Callable[[dict[str, Any]], Awaitable[None]] | None = None, clock: Callable[[], float] = time.time,
    ) -> None:
        self._s, self._context, self._save, self._clock = settings, context, save, clock
        self._transport = transport
        self.current: Briefing | None = None
        self.error: str | None = None
        self.busy = False
        self._last_lookup_at = float("-inf")
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ state
    def _apply(self) -> None:
        self._context.topic_notes = self.current.notes if self.current else ""
        self._context.topic_source = self.current.source if self.current else ""

    async def _persist(self) -> None:
        if self._save:
            try:
                await self._save({"game": self._s.current_game, "hint": self._s.topic_search_hint,
                                  "briefing": asdict(self.current) if self.current else None})
            except Exception:  # noqa: BLE001 - bookkeeping must never break the bot
                logger.exception("could not save the stream topic")

    async def load(self, raw: str | None) -> None:
        """Restore what the dashboard saved (the game, the search hint and the note)."""
        if not raw:
            return
        try:
            data = json.loads(raw)
            game = clean_line(data.get("game", ""), GAME_MAX_CHARS)
            if game:
                self._s.current_game = game
                self._context.set_game(game)
            self._s.topic_search_hint = clean_line(data.get("hint", ""), HINT_MAX_CHARS)
            b = data.get("briefing")
            if isinstance(b, dict) and b.get("source") in ("wikipedia", "manual"):
                self.current = Briefing(
                    game=clean_line(b.get("game", ""), GAME_MAX_CHARS), hint=clean_line(b.get("hint", ""), HINT_MAX_CHARS),
                    title=clean_line(b.get("title", ""), 120), notes=clean_notes(b.get("notes", ""), self._s.topic_notes_max_chars),
                    url="", source=b["source"], fetched_at=float(b.get("fetched_at") or 0.0))
                self.current.url = str(b.get("url", ""))[:300] if str(b.get("url", "")).startswith("https://") else ""
        except (ValueError, TypeError, AttributeError):
            logger.warning("ignoring an unreadable saved stream topic")
            self.current = None
        self._apply()

    def needs_refresh(self) -> bool:
        if not self._s.topic_lookup_enabled:
            return False
        b = self.current
        if b is None:
            return True
        if b.source == "manual" and b.game == self._s.current_game:
            return False  # the streamer's own text is never replaced behind their back
        old = self._clock() - b.fetched_at > self._s.topic_refresh_days * 86400
        return b.game != self._s.current_game or b.hint != self._s.topic_search_hint or old

    async def startup_refresh(self) -> None:
        if self.needs_refresh():
            try:
                await self.refresh()
            except TopicError:
                pass  # shown on the dashboard; the bot works without it

    # ------------------------------------------------------------------ the lookup
    def _query(self) -> str:
        return self._s.topic_search_hint or self._s.current_game

    async def refresh(self) -> dict[str, Any]:
        """Look the game up now. Raises TopicError (message safe to show); the previous note is kept if the lookup fails."""
        if not self._s.topic_lookup_enabled:
            raise TopicError("web lookups are switched off (STEALTHBOT_TOPIC_LOOKUP_ENABLED=false)")
        async with self._lock:
            wait = MIN_SECONDS_BETWEEN_LOOKUPS - (self._clock() - self._last_lookup_at)
            if wait > 0:
                raise TopicError(f"please wait {int(wait) + 1} s before looking up again")
            self._last_lookup_at = self._clock()
            self.busy = True
            try:
                found = await self._wikipedia(self._query())
            except TopicError as exc:
                self.error = str(exc)
                raise
            finally:
                self.busy = False
            self.error = None
            self.current = Briefing(self._s.current_game, self._s.topic_search_hint, found["title"],
                                    clean_notes(found["extract"], self._s.topic_notes_max_chars), found["url"], "wikipedia", self._clock())
            self._apply()
            await self._persist()
            return self.snapshot()

    def user_agent(self) -> str:
        contact = (self._s.topic_contact or "").strip()
        return f"StealthBot/1.0 (local stream chat assistant; {contact})" if contact else USER_AGENT

    async def _get_json(self, http: httpx.AsyncClient, url: str, params: dict[str, str] | None = None) -> Any:
        try:
            async with http.stream("GET", url, params=params) as resp:
                if resp.status_code == 403:
                    if not self._s.topic_contact:
                        raise TopicError("Wikipedia refused the request: it only answers programs that name a contact. Set "
                                         "STEALTHBOT_TOPIC_CONTACT in backend\\.env to a website, repository address or email "
                                         "(it is sent only to Wikipedia), or write your own notes below")
                    raise TopicError("Wikipedia refused the request (HTTP 403): check STEALTHBOT_TOPIC_CONTACT or try again later")
                if resp.status_code != 200:
                    raise TopicError(f"Wikipedia answered HTTP {resp.status_code}")
                body = b""
                async for chunk in resp.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise TopicError("Wikipedia's answer was unexpectedly large")
            return json.loads(body)
        except TopicError:
            raise
        except httpx.HTTPError as exc:
            raise TopicError(f"could not reach Wikipedia ({exc.__class__.__name__})") from exc
        except ValueError as exc:
            raise TopicError("Wikipedia's answer was not readable") from exc

    async def _wikipedia(self, query: str) -> dict[str, str]:
        query = clean_line(query, 120)
        if not query:
            raise TopicError("there is nothing to look up: set the game first", invalid_input=True)
        lang = self._s.topic_wiki_lang
        async with httpx.AsyncClient(transport=self._transport, trust_env=False, timeout=self._s.topic_lookup_timeout_s,
                                     headers={"User-Agent": self.user_agent(), "Accept": "application/json"},
                                     follow_redirects=True, max_redirects=3) as http:
            search = await self._get_json(http, f"https://{lang}.wikipedia.org/w/api.php", {
                "action": "query", "list": "search", "srsearch": query, "srlimit": "5", "srprop": "", "format": "json", "utf8": "1"})
            hits = [h.get("title") for h in ((search.get("query") or {}).get("search") or []) if isinstance(h, dict) and h.get("title")]
            if not hits:
                raise TopicError(f"Wikipedia has no page for “{query}”: try other search words, or write your own notes")
            for title in hits[:5]:
                page = await self._get_json(http, f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/" + quote(str(title).replace(" ", "_"), safe=""))
                if not isinstance(page, dict) or page.get("type") != "standard" or not str(page.get("extract") or "").strip():
                    continue  # a disambiguation page or an empty one: try the next hit
                url = str(((page.get("content_urls") or {}).get("desktop") or {}).get("page") or "")
                return {"title": clean_line(page.get("title") or title, 120), "extract": str(page["extract"]),
                        "url": url if url.startswith("https://") else ""}
        raise TopicError(f"Wikipedia's matches for “{query}” were all disambiguation pages: try other search words")

    # ------------------------------------------------------------------ editing
    async def set_game(self, game: str, hint: str | None = None) -> None:
        game = clean_line(game, GAME_MAX_CHARS)
        if not game:
            raise TopicError("the game or topic cannot be empty", invalid_input=True)
        self._s.current_game = game
        self._context.set_game(game)
        if hint is not None:
            self._s.topic_search_hint = clean_line(hint, HINT_MAX_CHARS)
        if self.current is not None and self.current.game != game:
            self.current = None  # a note about the previous game must not describe the new one
            self._apply()
        await self._persist()

    async def set_manual_notes(self, notes: str) -> dict[str, Any]:
        text = clean_notes(notes, self._s.topic_notes_max_chars)
        if not text:
            self.current, self.error = None, None
            self._apply()
            await self._persist()
            return self.snapshot()
        self.error = None
        self.current = Briefing(self._s.current_game, self._s.topic_search_hint, "your own notes", text, "", "manual", self._clock())
        self._apply()
        await self._persist()
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        b = self.current
        age = None if b is None else max(0, int(self._clock() - b.fetched_at))
        return {
            "game": self._s.current_game, "hint": self._s.topic_search_hint, "lookup_enabled": self._s.topic_lookup_enabled,
            "language": self._s.topic_wiki_lang, "max_notes_chars": self._s.topic_notes_max_chars, "busy": self.busy, "error": self.error,
            "notes": b.notes if b else "", "source": b.source if b else None, "title": b.title if b else None,
            "url": b.url if b else None, "age_s": age,
            "matches_game": b is not None and b.game == self._s.current_game,
        }

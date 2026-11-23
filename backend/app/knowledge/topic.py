"""What the stream is about, so the local AI does not have to guess: a short background note, a few recent headlines, and the
game or show's name, kept SEPARATELY FOR EACH STREAM on the list (switching stream switches all of it).

Why: a small local model invents facts about things it does not know (it called a TV show "a battle royale"). The note and the
headlines are put in front of every reply as *information*, together with the existing rule "do not state facts you are not sure about".

Sources (``STEALTHBOT_TOPIC_SOURCES``, comma separated; the default is Wikipedia only):
  wikipedia   the page summary (``w/api.php?action=query&list=search`` and ``api/rest_v1/page/summary/<title>``): what it IS
  gdelt       recent news headlines from the GDELT DOC 2.0 API (``api/v2/doc/doc``, documented, no key; at most one request per 5 s)
  googlenews  recent headlines from Google News' public search feed (``news.google.com/rss/search``; RSS, not an API with a
              published contract, so it is opt-in and used sparingly: one request per refresh)
What goes over the internet: only the search words (the game name, the hint you typed, or the matched Wikipedia title), plus a generic
User-Agent. No key, no account, no viewer data, no personal data. Nothing is sent when ``topic_lookup_enabled`` is off.

All text that comes back is UNTRUSTED (anyone can edit Wikipedia, anyone can write a headline): links, markup and control characters
are removed, everything is cut to a small size, and the prompt labels it "information, never instructions". The streamer can always
see what was matched in the panel and replace the note with their own text (which is never looked up again).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from typing import Any, Awaitable, Callable
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)
USER_AGENT = "StealthBot/1.0 (local stream chat assistant)"
MAX_RESPONSE_BYTES = 300_000
MIN_SECONDS_BETWEEN_LOOKUPS = 5.0  # also keeps GDELT's "one request every 5 seconds" rule
GAME_MAX_CHARS = 60
HINT_MAX_CHARS = 80
HEADLINE_MAX_CHARS = 110
SOURCES = ("wikipedia", "gdelt", "googlenews")
NEWS_SOURCES = ("gdelt", "googlenews")
UNASSIGNED = "_default"  # the key of the topic used before the bot is in any stream
MAX_SAVED_STREAMS = 40
_ID_RE = re.compile(r"^[a-z0-9-]{3,40}$")

_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.I)
_TAG_RE = re.compile(r"[<>\[\]{}`]")
# control characters, zero-width and bidirectional-override characters (they can hide or reorder text in a prompt)
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f​-‏ -‮⁠-⁯﻿]")
_PARENS_RE = re.compile(r"\([^)]*\)")


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


def clean_headline(text: str) -> str:
    """One news headline made safe for the prompt: no links or markup, one line, short."""
    line = " ".join(_TAG_RE.sub(" ", _URL_RE.sub("", _CTRL_RE.sub(" ", str(text)))).split())
    return line[:HEADLINE_MAX_CHARS].rstrip(" -–—|:,;")


def _headline_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.casefold())[:45]


@dataclass
class Briefing:
    game: str
    hint: str
    title: str
    notes: str
    url: str
    source: str  # "wikipedia", "manual" (the streamer's own notes) or "news" (headlines only)
    fetched_at: float  # unix time
    headlines: list[str] = field(default_factory=list)
    news_at: float = 0.0
    news_source: str = ""


def _parse_briefing(b: Any, notes_max: int) -> Briefing | None:
    if not isinstance(b, dict) or b.get("source") not in ("wikipedia", "manual", "news"):
        return None
    url = str(b.get("url", ""))
    headlines = [h for h in (clean_headline(x) for x in (b.get("headlines") or [])[:8] if isinstance(x, str)) if h]
    return Briefing(
        game=clean_line(b.get("game", ""), GAME_MAX_CHARS), hint=clean_line(b.get("hint", ""), HINT_MAX_CHARS),
        title=clean_line(b.get("title", ""), 120), notes=clean_notes(b.get("notes", ""), notes_max),
        url=url[:300] if url.startswith("https://") else "", source=b["source"], fetched_at=float(b.get("fetched_at") or 0.0),
        headlines=headlines, news_at=float(b.get("news_at") or 0.0), news_source=clean_line(b.get("news_source", ""), 30))


class TopicBriefing:
    def __init__(
        self, settings: Any, context: Any, *, transport: httpx.AsyncBaseTransport | None = None,
        save: Callable[[dict[str, Any]], Awaitable[None]] | None = None, clock: Callable[[], float] = time.time,
    ) -> None:
        self._s, self._context, self._save, self._clock = settings, context, save, clock
        self._transport = transport
        self.current: Briefing | None = None
        self.error: str | None = None
        self.news_error: str | None = None
        self.busy = False
        self.focus_id: str | None = None  # the stream whose topic is in use (None until the bot is in one)
        self.focus_label = ""
        self.streams: dict[str, dict[str, Any]] = {}  # every OTHER stream's saved topic: {"game", "hint", "briefing"}
        self._last_lookup_at = float("-inf")
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()

    # ------------------------------------------------------------------ state
    def sources(self) -> list[str]:
        return [s for s in (getattr(self._s, "topic_sources", None) or ["wikipedia"]) if s in SOURCES]

    def _news_sources(self) -> list[str]:
        return [s for s in self.sources() if s in NEWS_SOURCES]

    def _apply(self) -> None:
        b = self.current
        self._context.topic_notes = b.notes if b else ""
        self._context.topic_source = b.source if b else ""
        self._context.topic_headlines = list(b.headlines) if b else []

    def _current_dict(self) -> dict[str, Any]:
        return {"game": self._s.current_game, "hint": self._s.topic_search_hint,
                "briefing": asdict(self.current) if self.current else None}

    def _load_dict(self, data: dict[str, Any]) -> None:
        game = clean_line(data.get("game", ""), GAME_MAX_CHARS)
        self._s.current_game = game
        self._context.set_game(game)
        self._s.topic_search_hint = clean_line(data.get("hint", ""), HINT_MAX_CHARS)
        self.current = _parse_briefing(data.get("briefing"), self._s.topic_notes_max_chars)
        self.error = self.news_error = None

    def _clear_topic(self) -> None:
        self._s.current_game = ""
        self._context.set_game("")
        self._s.topic_search_hint = ""
        self.current = None
        self.error = self.news_error = None

    def _saved_state(self) -> dict[str, Any]:
        streams = {k: v for k, v in self.streams.items() if k != self.focus_id}
        if len(streams) > MAX_SAVED_STREAMS:
            streams = dict(list(streams.items())[-MAX_SAVED_STREAMS:])
        return {**self._current_dict(), "focus": self.focus_id, "streams": streams}

    async def _persist(self) -> None:
        if self._save:
            try:
                await self._save(self._saved_state())
            except Exception:  # noqa: BLE001 - bookkeeping must never break the bot
                logger.exception("could not save the stream topic")

    async def load(self, raw: str | None) -> None:
        """Restore what the dashboard saved (the topic in use, which stream it belongs to, and the other streams' topics)."""
        if not raw:
            return
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("not an object")
            game = clean_line(data.get("game", ""), GAME_MAX_CHARS)
            if game or data.get("focus"):
                self._load_dict(data)
            focus = data.get("focus")
            self.focus_id = focus if isinstance(focus, str) and _ID_RE.match(focus) else None
            for key, value in (data.get("streams") or {}).items():
                if isinstance(key, str) and _ID_RE.match(key) and isinstance(value, dict):
                    self.streams[key] = {
                        "game": clean_line(value.get("game", ""), GAME_MAX_CHARS), "hint": clean_line(value.get("hint", ""), HINT_MAX_CHARS),
                        "briefing": (asdict(b) if (b := _parse_briefing(value.get("briefing"), self._s.topic_notes_max_chars)) else None)}
        except (ValueError, TypeError, AttributeError):
            logger.warning("ignoring an unreadable saved stream topic")
            self.current = None
        self._apply()

    # ------------------------------------------------------------------ one topic per stream
    async def switch_to(self, target_id: str, label: str = "") -> bool:
        """The bot is now in another stream: put that stream's own topic in use (a stream with no topic yet starts empty, so a
        note about the previous game never describes the new stream). Returns True when the topic changed."""
        self.focus_label = label
        if target_id == self.focus_id:
            return False
        first = self.focus_id is None
        if not first:
            self.streams[self.focus_id] = self._current_dict()  # type: ignore[index]
        self.focus_id = target_id
        saved = self.streams.pop(target_id, None)
        if saved is not None:
            self._load_dict(saved)
        elif not first:
            self._clear_topic()  # nothing saved for this stream yet. (The very first stream keeps the topic set before streams had their own.)
        self._apply()
        await self._persist()
        self._wake.set()  # look the new topic up soon
        return True

    def forget(self, target_id: str) -> None:
        self.streams.pop(target_id, None)

    async def set_stream_topic(self, target_id: str, *, game: str | None = None, hint: str | None = None, notes: str | None = None) -> None:
        """Edit the saved topic of a stream the bot is NOT in right now (it is looked up when the bot moves there)."""
        data = self.streams.setdefault(target_id, {"game": "", "hint": "", "briefing": None})
        if game is not None:
            game = clean_line(game, GAME_MAX_CHARS)
            if not game:
                raise TopicError("the game or topic cannot be empty", invalid_input=True)
            if game != data["game"]:
                data["briefing"] = None  # a note about the previous game must not describe the new one
            data["game"] = game
        if hint is not None:
            data["hint"] = clean_line(hint, HINT_MAX_CHARS)
        if notes is not None:
            text = clean_notes(notes, self._s.topic_notes_max_chars)
            old = _parse_briefing(data.get("briefing"), self._s.topic_notes_max_chars)
            headlines = old.headlines if old else []
            if text:
                data["briefing"] = asdict(Briefing(data["game"], data["hint"], "your own notes", text, "", "manual", self._clock(), headlines))
            else:
                data["briefing"] = None
        await self._persist()
        self._wake.set()

    # ------------------------------------------------------------------ when a lookup is due
    def needs_refresh(self) -> bool:
        if not self._s.topic_lookup_enabled or not self._s.current_game:
            return False
        b = self.current
        if b is None:
            return True
        if b.source == "manual" and b.game == self._s.current_game:
            return False  # the streamer's own text is never replaced behind their back
        old = self._clock() - b.fetched_at > self._s.topic_refresh_days * 86400
        return b.game != self._s.current_game or b.hint != self._s.topic_search_hint or old

    def news_due(self) -> bool:
        if not self._s.topic_lookup_enabled or not self._s.current_game or not self._news_sources() or self.current is None:
            return False
        every = float(getattr(self._s, "topic_news_refresh_min", 30.0)) * 60
        return self._clock() - self.current.news_at > every

    async def startup_refresh(self) -> None:
        await self.maintain()

    async def maintain(self) -> None:
        """One round of keeping the topic fresh: a full lookup when there is none (or it is old), else just the headlines."""
        if not self._s.topic_lookup_enabled or not self._s.current_game:
            return
        try:
            if self.needs_refresh():
                await self.refresh()
            elif self.news_due():
                await self.refresh_news()
        except TopicError:
            pass  # shown in the panel; the bot works without it

    async def run_loop(self, check_every_s: float = 60.0) -> None:
        """Background task: look the topic up at start, after a stream switch, and refresh the headlines now and then."""
        while True:
            await self.maintain()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=check_every_s)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    # ------------------------------------------------------------------ the lookup
    def _query(self) -> str:
        return self._s.topic_search_hint or self._s.current_game

    def _news_words(self, wikipedia_title: str) -> str:
        """Words for the news search: the matched Wikipedia title (without its bracket) is the most reliable name of the show."""
        title = clean_line(_PARENS_RE.sub(" ", wikipedia_title or ""), 80)
        if len(title.split()) >= 2:
            return title
        return clean_line(self._s.topic_search_hint or self._s.current_game, 80)

    def _news_query(self) -> str:
        b = self.current
        return self._news_words(b.title if (b is not None and b.source == "wikipedia") else "")

    def _throttle(self) -> None:
        wait = MIN_SECONDS_BETWEEN_LOOKUPS - (self._clock() - self._last_lookup_at)
        if wait > 0:
            raise TopicError(f"please wait {int(wait) + 1} s before looking up again")
        self._last_lookup_at = self._clock()

    async def refresh(self) -> dict[str, Any]:
        """Look the game up now (Wikipedia, then the news). Raises TopicError (message safe to show) when Wikipedia fails; the
        previous note is kept then. A news problem is only reported in ``news_error``."""
        if not self._s.topic_lookup_enabled:
            raise TopicError("web lookups are switched off (STEALTHBOT_TOPIC_LOOKUP_ENABLED=false)")
        async with self._lock:
            self._throttle()
            self.busy = True
            focus, game, hint = self.focus_id, self._s.current_game, self._s.topic_search_hint
            try:
                found: dict[str, str] | None = None
                if "wikipedia" in self.sources():
                    try:
                        found = await self._wikipedia(self._query())
                    except TopicError as exc:
                        self.error = str(exc)
                        raise
                title = found["title"] if found else ""
                query = self._news_words(title)
                headlines, news_source, self.news_error = await self._headlines(query) if self._news_sources() else ([], "", None)
                if self.focus_id != focus:  # the bot moved to another stream meanwhile: this answer is for the old one
                    return self.snapshot()
                self.error = None
                self.current = Briefing(
                    game, hint, title, clean_notes(found["extract"], self._s.topic_notes_max_chars) if found else "",
                    found["url"] if found else "", "wikipedia" if found else "news", self._clock(), headlines, self._clock(), news_source)
                self._apply()
                await self._persist()
                return self.snapshot()
            finally:
                self.busy = False

    async def refresh_news(self) -> dict[str, Any]:
        """Only the headlines (the background note is kept). Used every ``topic_news_refresh_min`` minutes and by the panel."""
        if not self._s.topic_lookup_enabled:
            raise TopicError("web lookups are switched off (STEALTHBOT_TOPIC_LOOKUP_ENABLED=false)")
        if not self._news_sources():
            raise TopicError("no news source is switched on (STEALTHBOT_TOPIC_SOURCES=wikipedia,gdelt,googlenews)")
        if not self._s.current_game:
            raise TopicError("set what the stream is about first", invalid_input=True)
        async with self._lock:
            self._throttle()
            self.busy = True
            focus = self.focus_id
            try:
                headlines, news_source, error = await self._headlines(self._news_query())
                if self.focus_id != focus:
                    return self.snapshot()
                self.news_error = error
                b = self.current or Briefing(self._s.current_game, self._s.topic_search_hint, "", "", "", "news", self._clock())
                b.news_at = self._clock()  # a failed attempt also waits for the next round
                if headlines:
                    b.headlines, b.news_source = headlines, news_source
                self.current = b
                self._apply()
                await self._persist()
                return self.snapshot()
            finally:
                self.busy = False

    def user_agent(self) -> str:
        contact = (self._s.topic_contact or "").strip()
        return f"StealthBot/1.0 (local stream chat assistant; {contact})" if contact else USER_AGENT

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self._transport, trust_env=False, timeout=self._s.topic_lookup_timeout_s,
                                 headers={"User-Agent": self.user_agent(), "Accept": "application/json, application/rss+xml, */*"},
                                 follow_redirects=True, max_redirects=3)

    async def _get_bytes(self, http: httpx.AsyncClient, url: str, params: dict[str, str] | None, who: str) -> bytes:
        try:
            async with http.stream("GET", url, params=params) as resp:
                if resp.status_code == 403:
                    if who == "Wikipedia" and not self._s.topic_contact:
                        raise TopicError("Wikipedia refused the request: it only answers programs that name a contact. Set "
                                         "STEALTHBOT_TOPIC_CONTACT in backend\\.env to a website, repository address or email "
                                         "(it is sent only to Wikipedia), or write your own notes below")
                    raise TopicError(f"{who} refused the request (HTTP 403): check STEALTHBOT_TOPIC_CONTACT or try again later")
                if resp.status_code == 429:
                    raise TopicError(f"{who} asked us to slow down (HTTP 429): it will be tried again later")
                if resp.status_code != 200:
                    raise TopicError(f"{who} answered HTTP {resp.status_code}")
                body = b""
                async for chunk in resp.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise TopicError(f"{who}'s answer was unexpectedly large")
            return body
        except TopicError:
            raise
        except httpx.HTTPError as exc:
            raise TopicError(f"could not reach {who} ({exc.__class__.__name__})") from exc

    async def _get_json(self, http: httpx.AsyncClient, url: str, params: dict[str, str] | None = None, who: str = "Wikipedia") -> Any:
        body = await self._get_bytes(http, url, params, who)
        try:
            return json.loads(body)
        except ValueError as exc:
            raise TopicError(f"{who}'s answer was not readable") from exc

    async def _wikipedia(self, query: str) -> dict[str, str]:
        query = clean_line(query, 120)
        if not query:
            raise TopicError("there is nothing to look up: set the game first", invalid_input=True)
        lang = self._s.topic_wiki_lang
        async with self._client() as http:
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

    # ------------------------------------------------------------------ the news
    async def quick_lookup(self, query: str) -> str | None:
        """A short piece of background for one viewer question: the matching Wikipedia summary (cut to 260 characters) and up to two
        headlines. Does not touch the stream's saved topic, and is not rate limited here (``knowledge/research.py`` does that).
        Raises TopicError when nothing could be looked up."""
        query = clean_line(query, 100)
        if not query:
            raise TopicError("nothing to look up", invalid_input=True)
        parts: list[str] = []
        problems: list[str] = []
        if "wikipedia" in self.sources():
            try:
                found = await self._wikipedia(query)
                parts.append(f"{found['title']}: {clean_notes(found['extract'], 260)}")
            except TopicError as exc:
                problems.append(str(exc))
        if self._news_sources():
            headlines, _source, problem = await self._headlines(query, wanted=2, budget=220)
            if headlines:
                parts.append("Recent headlines: " + " | ".join(headlines))
            elif problem:
                problems.append(problem)
        if not parts:
            raise TopicError("; ".join(problems)[:160] or "nothing found")
        return " ".join(parts)

    async def _headlines(self, query: str, *, wanted: int | None = None, budget: int | None = None) -> tuple[list[str], str, str | None]:
        """(headlines, which source gave them, problem). Every source is tried in order until there are enough headlines; a source
        that fails only adds to the problem text."""
        query = clean_line(query, 80)
        if not query:
            return [], "", "there is nothing to search the news for"
        wanted = wanted or int(getattr(self._s, "topic_news_max_items", 4))
        budget = budget or int(getattr(self._s, "topic_news_max_chars", 360))
        found: list[str] = []
        used: list[str] = []
        problems: list[str] = []
        seen: set[str] = set()
        async with self._client() as http:
            for source in self._news_sources():
                if len(found) >= wanted:
                    break
                try:
                    titles = await (self._gdelt(http, query) if source == "gdelt" else self._google_news(http, query))
                except TopicError as exc:
                    problems.append(str(exc))
                    continue
                before = len(found)
                for title in titles:
                    key = _headline_key(title)
                    if len(title) < 12 or key in seen:
                        continue
                    if len(found) >= wanted or sum(len(x) for x in found) + len(title) > budget:
                        break
                    seen.add(key)
                    found.append(title)
                if len(found) > before:
                    used.append(source)
        return found, "+".join(used), ("; ".join(problems)[:200] if problems and not found else None)

    async def _gdelt(self, http: httpx.AsyncClient, query: str) -> list[str]:
        data = await self._get_json(http, "https://api.gdeltproject.org/api/v2/doc/doc", {
            "query": query, "mode": "artlist", "maxrecords": "15", "format": "json", "timespan": "7d", "sort": "datedesc"}, who="GDELT")
        articles = data.get("articles") if isinstance(data, dict) else None
        return [h for h in (clean_headline(a.get("title", "")) for a in (articles or []) if isinstance(a, dict)) if h]

    async def _google_news(self, http: httpx.AsyncClient, query: str) -> list[str]:
        lang, region = self._s.topic_news_lang, self._s.topic_news_region
        body = await self._get_bytes(http, "https://news.google.com/rss/search",
                                     {"q": query, "hl": f"{lang}-{region}", "gl": region, "ceid": f"{region}:{lang}"}, "Google News")
        try:
            root = ET.fromstring(body)
        except ET.ParseError as exc:
            raise TopicError("Google News' answer was not readable") from exc
        titles = []
        for item in root.iter("item"):
            raw = (item.findtext("title") or "").strip()
            head, sep, publisher = raw.rpartition(" - ")  # "Headline - The Times of India": the publisher is not part of the news
            title = clean_headline(head if sep and len(publisher) <= 40 else raw)
            if title:
                titles.append(title)
        return titles

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
        old = self.current
        headlines = old.headlines if (old and old.game == self._s.current_game) else []
        if not text:
            self.current, self.error = None, None
            self._apply()
            await self._persist()
            return self.snapshot()
        self.error = None
        self.current = Briefing(self._s.current_game, self._s.topic_search_hint, "your own notes", text, "", "manual", self._clock(),
                                headlines, old.news_at if old else 0.0, old.news_source if old else "")
        self._apply()
        await self._persist()
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        b = self.current
        age = None if b is None else max(0, int(self._clock() - b.fetched_at))
        news_age = None if (b is None or not b.news_at) else max(0, int(self._clock() - b.news_at))
        return {
            "game": self._s.current_game, "hint": self._s.topic_search_hint, "lookup_enabled": self._s.topic_lookup_enabled,
            "language": self._s.topic_wiki_lang, "max_notes_chars": self._s.topic_notes_max_chars, "busy": self.busy, "error": self.error,
            "notes": b.notes if b else "", "source": b.source if b else None, "title": b.title if b else None,
            "url": b.url if b else None, "age_s": age,
            "matches_game": b is not None and b.game == self._s.current_game,
            "headlines": list(b.headlines) if b else [], "news_age_s": news_age, "news_error": self.news_error,
            "news_source": b.news_source if b else "", "sources": self.sources(),
            "is_set": bool(self._s.current_game),
            "stream": {"id": self.focus_id, "label": self.focus_label} if self.focus_id else None,
            # what the other streams are about, so the panel can show it next to each stream
            "streams": {k: str(v.get("game") or "") for k, v in self.streams.items() if k != self.focus_id},
        }

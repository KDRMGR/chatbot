"""Inbound message filter + rule-based classifier.

Stateful (tracks per-viewer history for flood / repeat detection), so call ``classify`` exactly once per
message. No LLM is involved here: spam, abuse and prompt-injection attempts never reach the model.
"""
from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

from ..config import Settings
from .lexicon import URL_RE, Lexicon, build_lexicon
from .models import Category, ChatMessage, EventType
from .normalizer import compare_key, word_count

_I = re.IGNORECASE
GREETING_RE = re.compile(
    r"^(?:hi+|hello+|hey+|heya|hiya|yo+|sup|wassup|what'?s up|hola|namaste|gm|howdy|greetings"
    r"|good\s+(?:morning|afternoon|evening|night))\b",
    _I,
)
QUESTION_START_RE = re.compile(
    r"^(?:what|whats|what's|how|why|who|whom|whose|where|when|which|can|could|would|will|should"
    r"|do|does|did|is|are|was|were|has|have|anyone|anybody)\b",
    _I,
)
SUGGESTION_RE = re.compile(
    r"^(?:try|go|use|steal|jump|drive|shoot|take|switch|get|buy|grab|run|escape|attack|rob|hit|ram|crash"
    r"|fly|swim|climb|hide|stop|dont|don't)\b"
    r"|\b(?:you should|you gotta|you need to|why don'?t you|how about|let'?s|try to|try using|go for)\b",
    _I,
)
JOKE_RE = re.compile(r"\b(?:lol+|lmao+|lmfao|rofl|haha+|hehe+|bruh|bro+|xd+)\b|[😂🤣💀😭]", _I)
_REPEAT_CHAR = re.compile(r"(.)\1{9,}")


@dataclass(frozen=True)
class Classification:
    category: Category
    reason: str | None = None
    respond: bool = True


class MessageFilter:
    def __init__(
        self,
        settings: Settings,
        lexicon: Lexicon | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._s = settings
        self._lex = lexicon or build_lexicon(settings.extra_lexicon_file)
        self._clock = clock
        self._by_user: dict[tuple[str, str], deque[tuple[float, str]]] = {}
        self._by_text: dict[str, deque[tuple[float, str]]] = {}
        self._calls = 0

    # ------------------------------------------------------------------ public
    def classify(self, msg: ChatMessage) -> Classification:
        if msg.event_type is not EventType.MESSAGE:
            return Classification(Category.EVENT)
        text = msg.message
        if not text:
            return Classification(Category.SPAM, "empty", respond=False)

        now = self._clock()
        key = compare_key(text)
        flood, repeated, copy_users = self._observe(msg, key, now)

        if self._lex.matches_injection(text):
            return Classification(Category.ABUSE, "prompt_injection", respond=False)
        if self._lex.matches_abuse(text):
            return Classification(Category.ABUSE, "harassment", respond=False)
        if flood:
            return Classification(Category.SPAM, "flood", respond=False)
        if repeated:
            return Classification(Category.REPEATED, "same_viewer_repeat", respond=False)
        if text.startswith("!"):
            command = text.split()[0].lower()
            if command in {c.lower() for c in self._s.allowed_commands}:
                return Classification(Category.BOT_COMMAND, command)
            return Classification(Category.BOT_COMMAND, "unknown_command", respond=False)
        reason = self._spam_reason(text)
        if reason:
            return Classification(Category.SPAM, reason, respond=False)
        if copy_users >= self._s.copypasta_min_users:
            return Classification(Category.SPAM, "copypasta", respond=False)
        return Classification(self._categorize(text))

    # ------------------------------------------------------------------ helpers
    def _observe(self, msg: ChatMessage, key: str, now: float) -> tuple[bool, bool, int]:
        """Record this message and report (flood, repeated-by-same-viewer, distinct viewers sending it)."""
        s = self._s
        history = self._by_user.setdefault((msg.platform.value, msg.viewer_id), deque())
        horizon = now - max(s.flood_window_s, s.repeat_window_s)
        while history and history[0][0] < horizon:
            history.popleft()
        flood = sum(1 for t, _ in history if t >= now - s.flood_window_s) >= s.flood_max_messages
        repeated = any(k == key and t >= now - s.repeat_window_s for t, k in history)
        history.append((now, key))

        copy_users = 0
        if len(key) >= 8:
            texts = self._by_text.setdefault(key, deque())
            while texts and texts[0][0] < now - s.copypasta_window_s:
                texts.popleft()
            copy_users = len({v for _, v in texts} | {msg.viewer_id})
            texts.append((now, msg.viewer_id))

        self._calls += 1
        if self._calls % 200 == 0:
            self._gc(now)
        return flood, repeated, copy_users

    def _gc(self, now: float) -> None:
        horizon = now - max(self._s.flood_window_s, self._s.repeat_window_s, self._s.copypasta_window_s)
        for store in (self._by_user, self._by_text):
            for k in [k for k, d in store.items() if not d or d[-1][0] < horizon]:
                del store[k]

    @staticmethod
    def _spam_reason(text: str) -> str | None:
        if URL_RE.search(text):
            return "link"
        if _REPEAT_CHAR.search(text):
            return "char_repeat"
        letters = [c for c in text if c.isalpha()]
        if len(letters) >= 15 and sum(c.isupper() for c in letters) / len(letters) > 0.8:
            return "all_caps"
        words = text.split()
        if len(words) >= 6 and len({w.lower() for w in words}) <= 2:
            return "word_repeat"
        symbols = sum(1 for c in text if not c.isalnum() and not c.isspace())
        if len(text) >= 12 and symbols / len(text) > 0.6:
            return "symbol_flood"
        return None

    @staticmethod
    def _categorize(text: str) -> Category:
        wc = word_count(text)
        greeting = GREETING_RE.match(text)
        if greeting and wc <= 6:
            rest = text[greeting.end():].strip(" ,.!")
            if "?" in rest or QUESTION_START_RE.match(rest):
                return Category.QUESTION
            return Category.GREETING
        if "?" in text or (wc >= 3 and QUESTION_START_RE.match(text)):
            return Category.QUESTION
        if SUGGESTION_RE.search(text):
            return Category.GAMEPLAY_SUGGESTION
        if JOKE_RE.search(text):
            return Category.JOKE
        return Category.GENERAL

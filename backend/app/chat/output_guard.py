"""Outbound guard: everything the model produces is untrusted text until it passes here.

The AI only ever generates text. This module decides whether that text may be sent at all.
"""
from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass
from difflib import SequenceMatcher

from .lexicon import URL_RE, Lexicon
from .normalizer import compare_key, word_count

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.S | re.I)
_LABEL = re.compile(r"^\s*(?:stealthbot|bot|assistant|reply|message|chat message)\s*[:\-–]\s*", re.I)
_MENTION = re.compile(r"(?<!\w)@\S+")
_EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿]")
_QUOTES = "\"'`“”‘’"

# Phrases that only appear if the model is echoing its own instructions
LEAK_MARKERS = (
    "untrusted chat",
    "<viewer",
    "</viewer",
    "system prompt",
    "my instructions",
    "rules you must always follow",
    "stream context:",
)


@dataclass(frozen=True)
class GuardResult:
    text: str | None
    reason: str | None = None


class RecentReplies:
    """Rolling memory of what the bot recently said, across ALL platforms (so YouTube and Kick never get
    the same message at about the same time)."""

    def __init__(self, size: int = 20) -> None:
        self._size = size
        self._by_platform: dict[str, deque[str]] = {}

    def add(self, platform: str, text: str) -> None:
        self._by_platform.setdefault(platform, deque(maxlen=self._size)).append(text)

    def all(self) -> list[str]:
        return [t for d in self._by_platform.values() for t in d]

    def last(self, n: int = 3) -> list[str]:
        return self.all()[-n:]


_SHOUT = re.compile(r"\b[A-Z]{5,}\b")
_ORPHAN_JOINERS = re.compile("(?<![\U0001F300-\U0001FAFF☀-➿])[️‍]")


def _limit_emoji(text: str, max_emoji: int) -> str:
    """Keep the first ``max_emoji`` emoji and drop the rest (the model ignores 'at most two' now and then)."""
    seen = 0

    def keep_first(match: re.Match[str]) -> str:
        nonlocal seen
        seen += 1
        return match.group(0) if seen <= max_emoji else ""

    out = _ORPHAN_JOINERS.sub("", _EMOJI.sub(keep_first, text))
    return re.sub(r"\s{2,}", " ", out).strip()


def _deshout(text: str) -> str:
    """'OHHHH CAROL' -> 'Ohhhh Carol'. Short all-caps words (GG, LOL, WOAH) are left alone."""
    return _SHOUT.sub(lambda m: m.group(0).capitalize(), text)


def truncate_words(text: str, max_words: int) -> str:
    words = text.split()
    if len(words) <= max_words:
        return text
    cut = " ".join(words[:max_words])
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if end >= len(cut) * 0.5:
        return cut[: end + 1]
    return cut.rstrip(",;:- ") + "…"


def fit_to_limit(text: str, limit: int) -> str:
    """Trim to a platform's character limit on a word boundary."""
    if len(text) <= limit:
        return text
    cut = text[: max(limit - 1, 1)]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(",;:- ") + "…"


def clean_reply(
    raw: str,
    *,
    lexicon: Lexicon,
    recent: list[str],
    max_words: int = 35,
    min_words: int = 3,
    max_chars: int = 200,
    similarity_threshold: float = 0.85,
    max_emoji: int = 2,
) -> GuardResult:
    text = raw or ""
    low = text.lower()
    if "<think>" in low and "</think>" not in low:
        return GuardResult(None, "think_unterminated")
    text = _THINK_BLOCK.sub("", text)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    text = " ".join(lines)
    text = _LABEL.sub("", text).replace("**", "").replace("__", "").strip()
    if len(text) >= 2 and text[0] in _QUOTES and text[-1] in _QUOTES:
        text = text[1:-1].strip()
    text = re.sub(r"\s{2,}", " ", _MENTION.sub("", text)).strip()
    if not text:
        return GuardResult(None, "empty")
    if URL_RE.search(text):
        return GuardResult(None, "link")
    if text[0] in "!/":
        return GuardResult(None, "command_like")
    low = text.lower()
    if any(marker in low for marker in LEAK_MARKERS):
        return GuardResult(None, "prompt_leak")
    if lexicon.matches_banned(text):
        return GuardResult(None, "banned_content")
    if len(_EMOJI.findall(text)) > 6:
        return GuardResult(None, "emoji_flood")
    text = _deshout(_limit_emoji(text, max_emoji))  # tone: at most 2 emoji, no SHOUTING words

    text = fit_to_limit(truncate_words(text, max_words), max_chars)
    if word_count(text) < min_words:
        return GuardResult(None, "too_short")

    key = compare_key(text)
    for previous in recent:
        prev_key = compare_key(previous)
        if prev_key == key or SequenceMatcher(None, key, prev_key).ratio() >= similarity_threshold:
            return GuardResult(None, "repeat")
    return GuardResult(text, None)

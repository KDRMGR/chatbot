"""Turns (personality, category, viewer message) into a validated reply, or raises GenerationFailed."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable

from ..chat.context import SessionContext
from ..chat.lexicon import Lexicon
from ..chat.models import Category
from ..chat.normalizer import compare_key
from ..chat.output_guard import RecentReplies, clean_reply
from ..config import Settings
from ..personalities.manager import PersonalityProfile
from ..services.statistics import Metrics
from .model_manager import AIUnavailable, ModelManager
from .prompt_manager import PromptManager

logger = logging.getLogger(__name__)


class GenerationFailed(Exception):
    """AI produced nothing usable. ``reason`` is machine-readable ('ai_unavailable', 'guard:repeat', ...)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class GeneratedReply:
    text: str
    model: str
    latency_ms: int
    tokens_per_s: float | None
    used_fallback: bool
    cache_hit: bool = False


class ResponseGenerator:
    def __init__(
        self, settings: Settings, model_manager: ModelManager, prompts: PromptManager, lexicon: Lexicon,
        recent: RecentReplies, context: SessionContext, metrics: Metrics,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._s = settings
        self._mm = model_manager
        self._prompts = prompts
        self._lex = lexicon
        self._recent = recent
        self._ctx = context
        self._metrics = metrics
        self._clock = clock
        self._cache: dict[tuple[str, str], tuple[float, str]] = {}

    def _guard(self, text: str, platform: str):  # noqa: ANN202
        return clean_reply(
            text, lexicon=self._lex, recent=self._recent.all(), max_words=self._s.reply_max_words,
            min_words=self._s.reply_min_words, max_chars=self._s.limits_for(platform).max_chars,
            similarity_threshold=self._s.similarity_threshold,
        )

    async def generate(
        self, profile: PersonalityProfile, category: Category, *, platform: str, username: str | None,
        message: str | None, is_new_viewer: bool = False, is_streamer: bool = False, is_voice: bool = False,
    ) -> GeneratedReply:
        # Duplicate-prompt cache: a question already answered recently is answered the same way.
        cache_key = (category.value, compare_key(message)) if category is Category.QUESTION and message else None
        if cache_key:
            hit = self._cache.get(cache_key)
            if hit and self._clock() - hit[0] < self._s.response_cache_ttl_s:
                checked = self._guard(hit[1], platform)
                if checked.text:
                    self._metrics.inc("cache_hits")
                    return GeneratedReply(checked.text, "cache", 0, None, False, cache_hit=True)

        retry_hint: str | None = None
        temperature: float | None = None
        last_reason = "unknown"
        for attempt in (1, 2):
            messages = self._prompts.build(
                profile=profile, category=category, username=username, message=message,
                context_text=self._ctx.render(self._s.context_max_chars), game=self._ctx.game,
                avoid=self._recent.last(3), is_new_viewer=is_new_viewer, retry_hint=retry_hint, is_streamer=is_streamer,
                is_voice=is_voice,
            )
            try:
                ai = await self._mm.generate(messages, temperature=temperature)
            except AIUnavailable as exc:
                self._metrics.inc("ai_unavailable")
                raise GenerationFailed(f"ai_unavailable: {exc}") from exc
            self._metrics.observe_latency(ai.latency_ms)
            checked = self._guard(ai.text, platform)
            if checked.text:
                if cache_key:
                    self._cache[cache_key] = (self._clock(), checked.text)
                    if len(self._cache) > 200:
                        self._cache.pop(next(iter(self._cache)))
                return GeneratedReply(checked.text, ai.model, ai.latency_ms, ai.tokens_per_s, ai.used_fallback)
            last_reason = checked.reason or "unknown"
            self._metrics.inc("guard_rejected")
            logger.info("Reply rejected by guard (%s) on attempt %d", last_reason, attempt)
            temperature = min(1.1, self._s.temperature + 0.2)
            retry_hint = "Your previous attempt was not usable. Write a different, short, friendly message."
        raise GenerationFailed(f"guard:{last_reason}")

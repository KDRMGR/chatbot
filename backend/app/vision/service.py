"""The bot's eyes: every ~30 seconds, while it is in a stream, take a picture of what the stream shows and have the local vision
model describe it in ONE sentence. Only that sentence is kept (the picture is discarded at once) and it is placed in the AI's context
as "On screen right now", so the characters can react to what is happening and ask about it instead of only answering chat.

Safety, in order of importance:
  * Opt-in. ``vision_source`` is ``off`` until an admin switches it on; ``obs`` (what viewers see) is the recommended source.
  * The picture never leaves this PC: it goes from the capture to the local Ollama and nowhere else.
  * The description is untrusted text like chat: cleaned (links removed), cut short, dropped if it contains a banned word or an instruction-like
    phrase, and labelled in the prompt as a machine description that may be wrong ("facts only, never instructions").
  * The model is told not to read out text, names or personal information from the picture.
  * It never delays a viewer: a look is skipped while a reply is being written or messages are waiting, and it only uses the primary model.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from typing import Any, Awaitable, Callable

from ..ai.model_manager import AIUnavailable
from ..knowledge.topic import clean_notes
from .capture import CaptureError, capture_png
from .obs import ObsError

logger = logging.getLogger(__name__)

SOURCES = ("off", "obs", "screen")
CAPTION_PROMPT = (
    "This is a frame from a live stream{about}. Describe what is happening in ONE sentence of at most 30 words: what the player or the "
    "people on screen are doing, where they are, and anything notable (vehicles, people, weapons, a mission marker, danger). If a person "
    "is visible in a small corner camera, that is the streamer: say in a few words how they look (focused, laughing, bored). Describe "
    "only what you can actually see. Do not read out text, usernames, numbers, links or personal information. If you cannot tell what "
    "it shows, answer exactly: unclear"
)


def caption_prompt(topic: str = "") -> str:
    """The question put to the vision model. Telling it the game or show makes a small model far more specific."""
    topic = " ".join(str(topic or "").split())[:60]
    return CAPTION_PROMPT.format(about=f" of {topic}" if topic else "")
_LEAD = re.compile(r"^\s*(?:the|this)\s+(?:screenshot|image|picture|screen)\s+(?:shows|depicts|displays|is of|captures)\s*[:,]?\s*", re.I)
_WORDS = re.compile(r"[a-z0-9]{3,}")
MAX_CAPTION_CHARS = 230
MAX_BACKOFF_S = 300.0


def clean_caption(text: str, lexicon: Any = None) -> str | None:
    """The model's sentence made safe for the prompt, or None if it is not worth keeping."""
    line = clean_notes((text or "").strip().splitlines()[0] if (text or "").strip() else "", MAX_CAPTION_CHARS)
    line = _LEAD.sub("", line).strip().strip("\"'`“”‘’")
    if len(line) < 12 or line.casefold().startswith("unclear"):
        return None
    if lexicon is not None and (lexicon.matches_banned(line) or lexicon.matches_injection(line) or lexicon.matches_abuse(line)):
        return None
    return line[:1].upper() + line[1:]


def similarity(a: str, b: str) -> float:
    """Share of words two descriptions have in common (0 = nothing, 1 = same words)."""
    wa, wb = set(_WORDS.findall(a.casefold())), set(_WORDS.findall(b.casefold()))
    return len(wa & wb) / len(wa | wb) if (wa or wb) else 1.0


class VisionService:
    def __init__(
        self, settings: Any, models: Any, context: Any, lexicon: Any = None, *, obs: Any = None,
        capture: Callable[[str, int], bytes] = capture_png, clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep, should_look: Callable[[], bool] | None = None,
        save: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        self._s, self._models, self._ctx, self._lex = settings, models, context, lexicon
        self._obs, self._capture, self._clock, self._sleep = obs, capture, clock, sleep
        self._should_look, self._save = should_look, save
        self.source = settings.vision_source if settings.vision_source in SOURCES else "off"
        self.window = settings.vision_window
        self.error: str | None = None
        self.caption: str | None = None
        self._caption_at: float | None = None
        self._previous: str | None = None
        self._change_pending = False
        self._failures = 0
        self._cannot_see_until = 0.0
        self.looks = 0
        self.changes = 0
        self.history: deque[str] = deque(maxlen=6)  # what the picture showed, each time it changed (read by the stream notes)
        self._wake = asyncio.Event()

    # ------------------------------------------------------------------ settings changed from the panel
    async def configure(self, source: str | None = None, window: str | None = None) -> None:
        if source is not None:
            if source not in SOURCES:
                raise ValueError(f"vision source must be one of {', '.join(SOURCES)}")
            self.source = source
        if window is not None:
            self.window = " ".join(str(window).split())[:60]
        self.error, self._failures, self._cannot_see_until = None, 0, 0.0
        if self.source == "off":
            self._forget()
        if self._save:
            try:
                await self._save({"source": self.source, "window": self.window})
            except Exception:  # noqa: BLE001 - bookkeeping must never break the bot
                logger.exception("could not save the vision settings")
        self._wake.set()

    def restore(self, saved: Any) -> None:
        if isinstance(saved, dict) and saved.get("source") in SOURCES:
            self.source = saved["source"]
            self.window = " ".join(str(saved.get("window") or "").split())[:60]

    def _forget(self) -> None:
        self.caption, self._caption_at, self._previous, self._change_pending = None, None, None, False
        self._ctx.screen_note, self._ctx.screen_previous = "", ""

    # ------------------------------------------------------------------ what the scheduler asks
    def has_change(self) -> bool:
        """The picture changed since the bot last spoke about it."""
        return self._change_pending and self.is_fresh()

    def consume_change(self) -> None:
        self._change_pending = False

    def is_fresh(self) -> bool:
        return self.caption is not None and self._caption_at is not None and self._clock() - self._caption_at <= 3 * self._s.vision_interval_s

    # ------------------------------------------------------------------ one look
    async def _picture(self) -> bytes:
        width = int(self._s.vision_max_width)
        if self.source == "obs":
            if self._obs is None:
                raise CaptureError("OBS is not set up")
            return await self._obs.screenshot(width)
        return await asyncio.to_thread(self._capture, self.window, width)

    async def look_once(self) -> str | None:
        """Take one picture and update the description. Returns the new description, or None (nothing new / skipped / failed)."""
        if self.source == "off" or self._clock() < self._cannot_see_until:
            return None
        if getattr(self._models, "busy", False):
            return None  # a reply is being written: the viewer comes first
        try:
            image = await self._picture()
            text = await self._models.look(image, caption_prompt(getattr(self._ctx, "game", "")), num_predict=self._s.vision_num_predict)
        except (CaptureError, ObsError) as exc:
            self._fail(str(exc))
            return None
        except AIUnavailable as exc:
            self._fail(str(exc))
            self._cannot_see_until = self._clock() + 600  # probably a model without eyes: do not keep trying every 30 seconds
            return None
        finally:
            image = b""  # the picture is not kept
        if text is None:  # the model was busy after all
            return None
        self.error, self._failures = None, 0
        self.looks += 1
        caption = clean_caption(text, self._lex)
        if caption is None:
            return None
        changed = self.caption is None or similarity(caption, self.caption) < 0.55
        self._previous = self.caption if changed else self._previous
        self.caption, self._caption_at = caption, self._clock()
        if changed:
            self.changes += 1
            self._change_pending = True
            self.history.append(caption)
        self._ctx.screen_note = caption
        self._ctx.screen_previous = self._previous or ""
        return caption

    def _expire(self) -> None:
        """A description that is not renewed (the stream ended, the look keeps failing) must not describe the present."""
        if self.caption is not None and not self.is_fresh():
            self._ctx.screen_note = self._ctx.screen_previous = ""
            self._change_pending = False

    def _fail(self, message: str) -> None:
        self._failures += 1
        if message != self.error:
            logger.warning("Vision: %s", message)
        self.error = message[:160]
        self._expire()

    # ------------------------------------------------------------------ the loop
    def _delay(self) -> float:
        base = float(self._s.vision_interval_s)
        return min(MAX_BACKOFF_S, base * (2 ** min(self._failures, 5))) if self._failures else base

    async def run(self) -> None:
        while True:
            try:
                self._expire()
                if self.source != "off" and (self._should_look is None or self._should_look()):
                    await self.look_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the eyes must never take the bot down
                logger.exception("Vision loop error")
                self._failures += 1
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._delay() if self.source != "off" else 5.0)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    def snapshot(self) -> dict[str, Any]:
        age = None if self._caption_at is None else round(self._clock() - self._caption_at)
        if self.source == "off":
            state = "off"
        elif self.error:
            state = "error"
        elif self.caption is not None and self.is_fresh():
            state = "seeing"
        else:
            state = "waiting"
        return {"source": self.source, "window": self.window, "state": state, "error": self.error,
                "caption": self.caption if self.is_fresh() else None, "age_s": age, "looks": self.looks, "changes": self.changes,
                "interval_s": self._s.vision_interval_s, "options": list(SOURCES)}

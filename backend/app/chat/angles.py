"""Choosing WHAT GUY brings up when it speaks first.

Before, an unprompted message got the whole context and tended to say something generic. Now every one gets ONE concrete angle: what is
on screen, something people online are saying, a headline, its own notes on the stream, or something a viewer said a moment ago. The
choice is weighted (the picture and fresh social buzz first), never repeats an angle it used recently, and the prompt says "talk about
exactly this". The angle is information taken from untrusted places, so it is labelled "never instructions" like the rest.
"""
from __future__ import annotations

import random
from collections import deque
from typing import Any

WEIGHTS = {"screen": 4.0, "buzz": 3.0, "headline": 2.0, "notes": 2.0, "chat": 2.0, "about": 1.0}


class AnglePicker:
    def __init__(self, rng: random.Random | None = None, memory: int = 10) -> None:
        self._rng = rng or random.Random()
        self._used: deque[str] = deque(maxlen=memory)

    def candidates(self, ctx: Any) -> list[tuple[str, str, float]]:
        out: list[tuple[str, str, float]] = []
        if ctx.screen_note:
            out.append(("screen", f"On screen right now: {ctx.screen_note}", WEIGHTS["screen"]))
        out += [("buzz", f"People online are saying: {line}", WEIGHTS["buzz"]) for line in getattr(ctx, "buzz", [])[:4]]
        out += [("headline", f"In the news: {h}", WEIGHTS["headline"]) for h in ctx.topic_headlines[:4]]
        if ctx.memory_note:
            out.append(("notes", f"What has been happening on this stream: {ctx.memory_note}", WEIGHTS["notes"]))
        out += [("chat", f"Chat said: {line}", WEIGHTS["chat"]) for line in ctx.recent_viewer_lines(2)]
        if ctx.topic_notes:
            out.append(("about", f"About {ctx.game_label}: {ctx.topic_notes[:200]}", WEIGHTS["about"]))
        return out

    def pick(self, ctx: Any, *, prefer: str | None = None) -> str | None:
        """One angle (text), or None when there is nothing to base a message on. ``prefer`` forces a kind when it is available."""
        options = [c for c in self.candidates(ctx) if c[1] not in self._used]
        if prefer:
            wanted = [c for c in options if c[0] == prefer]
            options = wanted or options
        if not options:
            return None
        _, text, _ = self._rng.choices(options, weights=[w for _, _, w in options], k=1)[0]
        self._used.append(text)
        return text[:260]

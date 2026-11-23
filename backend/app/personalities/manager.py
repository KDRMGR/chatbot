"""Personality profiles and selection. ONE shared model serves all of them; a personality is just a prompt
plus scheduling metadata. Only one public bot identity exists per platform."""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from ..chat.models import Category

# Real viewers' greetings and questions are answered even if every fitting personality is on cooldown.
MUST_ANSWER = frozenset({Category.GREETING, Category.QUESTION})


@dataclass
class PersonalityProfile:
    name: str
    system_prompt: str
    description: str = ""
    enabled: bool = True
    cooldown_seconds: int = 30
    frequency: float = 1.0  # relative weight when several personalities fit the same message
    priority: int = 1  # higher = preferred by the Phase 5 scheduler when proactive tasks collide
    categories: list[str] = field(default_factory=list)

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "PersonalityProfile":
        return cls(
            name=row["name"], system_prompt=row["system_prompt"], description=row.get("description", ""),
            enabled=row.get("enabled", True), cooldown_seconds=row.get("cooldown_seconds", 30),
            frequency=row.get("frequency", 1.0), priority=row.get("priority", 1),
            categories=list(row.get("categories") or []),
        )


class PersonalityManager:
    def __init__(
        self, profiles: Iterable[PersonalityProfile], *, clock: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
    ) -> None:
        self._profiles = {p.name: p for p in profiles}
        self._clock = clock
        self._rng = rng or random.Random()
        self._last_used: dict[str, float] = {}
        self.weight_of: Callable[[str], float] | None = None  # set by the application: how well a character's messages get answered

    def all(self) -> list[PersonalityProfile]:
        return list(self._profiles.values())

    def get(self, name: str) -> PersonalityProfile | None:
        return self._profiles.get(name)

    def select(self, category: Category, prefer: str | None = None) -> PersonalityProfile | None:
        """Pick one enabled personality that handles ``category`` (weighted random by frequency), preferring
        those off cooldown. Selecting does not start the cooldown; call ``mark_used`` once a message was sent.

        ``prefer`` keeps a conversation with the same voice: if that personality is enabled and handles ``category`` it is
        chosen even while it is cooling down (the cooldown spaces out unprompted chatter; a reply inside a conversation is
        not that, and the per-viewer cooldown and the platform rate limiter still protect against spam).

        Greetings and questions come from real viewers and must never go unanswered just because the only
        fitting personality spoke recently: if every candidate is cooling down, the one that spoke LONGEST ago
        answers. (The per-viewer cooldown and the platform rate limiter still protect against spam.)
        """
        if prefer:
            wanted = self._profiles.get(prefer)
            if wanted is not None and wanted.enabled and category.value in wanted.categories and wanted.frequency > 0:
                return wanted
        now = self._clock()
        candidates = [
            p for p in self._profiles.values() if p.enabled and category.value in p.categories and p.frequency > 0
        ]
        if not candidates:
            return None
        ready = [p for p in candidates if now - self._last_used.get(p.name, float("-inf")) >= p.cooldown_seconds]
        if ready:
            pool = ready
        elif category in MUST_ANSWER:
            pool = [min(candidates, key=lambda p: self._last_used.get(p.name, float("-inf")))]
        else:
            return None
        learned = self.weight_of or (lambda _name: 1.0)
        return self._rng.choices(pool, weights=[p.frequency * learned(p.name) for p in pool], k=1)[0]

    def best_priority(self, category: Category) -> int | None:
        """Highest priority among the enabled personalities that handle ``category`` (cooldowns ignored), or None if
        nobody does. The scheduler uses it to decide which due activity goes first when several are due at once."""
        priorities = [
            p.priority for p in self._profiles.values()
            if p.enabled and category.value in p.categories and p.frequency > 0
        ]
        return max(priorities) if priorities else None

    def mark_used(self, name: str) -> None:
        self._last_used[name] = self._clock()

    def seconds_until_ready(self, name: str) -> float:
        p = self._profiles.get(name)
        if p is None or name not in self._last_used:
            return 0.0
        return max(0.0, self._last_used[name] + p.cooldown_seconds - self._clock())

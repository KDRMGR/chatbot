"""Deterministic replies: allowed commands, donation/membership thanks, and the fallback used when the AI
is unavailable or its output is rejected. The bot must keep chatting even with Ollama completely down."""
from __future__ import annotations

import random

from ..config import Settings
from .context import SessionContext
from .lexicon import URL_RE, Lexicon
from .models import Category, ChatMessage, EventType
from .normalizer import compare_key

_TEMPLATES: dict[str, list[str]] = {
    "greeting": [
        "Welcome to the stream, {user}! 👋 What are you playing lately?",
        "Hey {user}, glad you're here! Grab a seat and enjoy the chaos.",
        "Hi {user}! Welcome in, how long have you been following the stream?",
        "Welcome {user}! Chat's friendly, jump in anytime.",
    ],
    "question": [
        "Good question, {user}! Chat, anyone want to jump in on that? 🤔",
        "Hmm, not sure about that one, {user}. What do you think yourself?",
        "Ooh, that's a fun one {user}! Chat, what's your take?",
    ],
    "gameplay_suggestion": [
        "Ha, bold plan {user}! Let's see if the streamer is brave enough to try it.",
        "Love the energy, {user}! That could either go great or very wrong. 😄",
        "Chat, should we all vote on {user}'s idea?",
    ],
    "joke": [
        "Haha, good one {user}! 😄",
        "{user} coming in with the jokes today! 😂",
        "Okay {user}, that one got me. 😄",
    ],
    "general": [
        "Thanks for hanging out in chat, {user}! What's been your favourite moment so far?",
        "Good to have you here, {user}! Anything you'd like to see next?",
        "Nice one, {user}! Chat, what do you think?",
    ],
    "proactive_quiet": [
        "Chat, quick question: what's the best way to escape a police chase? 🚓",
        "It's quiet in here! Which vehicle would you pick for a getaway?",
        "Chat, should we explore the city or cause some chaos next?",
    ],
    "proactive_general": [
        "How's everyone doing? Anyone playing something good lately?",
        "Chat, what's your all-time favourite open-world game?",
    ],
    "proactive_quiz": [
        "Quick one for chat: which vehicle would you steal first, and why? 🚗",
        "Challenge time! Describe your dream heist crew in three words.",
    ],
    "proactive_hype": [
        "Let's go! The stream's looking good today! 🔥",
        "Great vibes in chat today, keep it up everyone!",
    ],
}


class RuleEngine:
    def __init__(
        self, settings: Settings, context: SessionContext, lexicon: Lexicon, rng: random.Random | None = None
    ) -> None:
        self._s = settings
        self._ctx = context
        self._lex = lexicon
        self._rng = rng or random.Random()

    def _safe_name(self, username: str) -> str:
        """Usernames are viewer-controlled; never echo one that is a link or trips the abuse/profanity lists."""
        if URL_RE.search(username) or self._lex.matches_abuse(username) or self._lex.matches_banned(username):
            return "friend"
        return username

    def handle_command(self, msg: ChatMessage) -> str | None:
        command = msg.message.split()[0].lower() if msg.message else ""
        if command in ("!bot", "!about"):
            return (
                f"I'm {self._s.bot_display_name}, an automated chat assistant for "
                f"{self._s.streamer_name}'s stream. I'm a bot, not a person. Be kind and have fun! 🎮"
            )
        if command == "!commands":
            return "Commands: " + ", ".join(sorted(self._s.allowed_commands))
        if command == "!game":
            return f"Currently playing: {self._ctx.game}. Ask me anything about it!"
        return None

    def event_reply(self, msg: ChatMessage) -> str:
        name = self._safe_name(msg.username)
        if msg.event_type is EventType.DONATION:
            return f"Thank you so much for the support, {name}! 💜"
        if msg.event_type is EventType.MEMBERSHIP:
            return f"Welcome to the crew, {name}! Thanks for joining! 🎉"
        return f"Thanks for being here, {name}!"

    def fallback_reply(self, category: Category, username: str, avoid: list[str]) -> str:
        name = self._safe_name(username)
        templates = _TEMPLATES.get(category.value) or _TEMPLATES["general"]
        rendered = [t.format(user=name) for t in templates]
        avoid_keys = {compare_key(a) for a in avoid}
        fresh = [t for t in rendered if compare_key(t) not in avoid_keys]
        return self._rng.choice(fresh or rendered)

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


# The same fallbacks in Hinglish (Hindi in the English alphabet). Neutral about the game: the bot may be on a TV-show stream.
_TEMPLATES_HINGLISH: dict[str, list[str]] = {
    "greeting": [
        "Welcome {user}! 👋 Chat mein aapka swagat hai, maze karo!",
        "Arre {user}, aao aao! Stream mein aapka swagat hai.",
        "Hi {user}! Kaise ho? Chat mein bindaas baat karo.",
        "Namaste {user}! Yahan sab dost hain, jud jao.",
    ],
    "question": [
        "Accha sawaal hai {user}! Chat, koi iska jawab dega? 🤔",
        "Hmm, mujhe pakka nahi pata {user}. Aap kya sochte ho?",
        "Arre waah, mast sawaal {user}! Chat, aapki kya raay hai?",
    ],
    "gameplay_suggestion": [
        "Waah {user}, kya idea hai! Dekhte hain streamer himmat dikhata hai ya nahi.",
        "Mast josh hai {user}! Ye ya to kamaal karega ya gadbad. 😄",
        "Chat, {user} ke idea par vote kar lete hain?",
    ],
    "joke": [
        "Haha, ek number {user}! 😄",
        "{user} aaj full comedy mood mein hain! 😂",
        "Okay {user}, ye wala mast tha. 😄",
    ],
    "general": [
        "Chat mein bane rehne ke liye thanks {user}! Ab tak ka sabse mast moment kaunsa laga?",
        "Accha laga aapko yahan dekhkar {user}! Aage kya dekhna chahoge?",
        "Mast {user}! Chat, aap sab kya sochte ho?",
    ],
    "proactive_quiet": [
        "Chat, thoda sannata hai! Ab tak ka sabse mast moment kaunsa tha? 🤔",
        "Kya haal hai sab ke? Koi kuch bolo, thodi baat karte hain!",
        "Chat, aap logon ko aage kya dekhna hai? Batao!",
    ],
    "proactive_general": [
        "Kaise ho sab log? Aaj ka din kaisa gaya?",
        "Chat, aaj ke stream ka mood kaisa lag raha hai?",
    ],
    "proactive_quiz": [
        "Chat ke liye ek sawaal: aap kisko jeetta hua dekhna chahte ho, aur kyun? 🤔",
        "Challenge time! Aaj ke stream ko ek line mein describe karo.",
    ],
    "proactive_hype": [
        "Chalo chalo! Aaj stream mein mast maahaul hai! 🔥",
        "Chat ka vibe full on hai aaj, aise hi bane raho sab!",
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

    @property
    def _hinglish(self) -> bool:
        # The prepared (no-AI) messages exist in English and Hinglish; the Hindi and "match the viewer" modes use the Hinglish ones.
        return self._s.reply_language != "english"

    def _safe_name(self, username: str) -> str:
        """Usernames are viewer-controlled; never echo one that is a link or trips the abuse/profanity lists."""
        if URL_RE.search(username) or self._lex.matches_abuse(username) or self._lex.matches_banned(username):
            return "friend"
        return username

    def handle_command(self, msg: ChatMessage) -> str | None:
        command = msg.message.split()[0].lower() if msg.message else ""
        hi = self._hinglish
        if command in ("!bot", "!about"):
            if hi:
                return (f"Main {self._s.bot_display_name} hoon, {self._s.streamer_name} ke stream ka automatic chat assistant. "
                        "Main ek bot hoon, insaan nahi. Pyaar se rehna aur maze karna! 🎮")
            return (
                f"I'm {self._s.bot_display_name}, an automated chat assistant for "
                f"{self._s.streamer_name}'s stream. I'm a bot, not a person. Be kind and have fun! 🎮"
            )
        if command == "!commands":
            return ("Commands: " if not hi else "Commands yeh hain: ") + ", ".join(sorted(self._s.allowed_commands))
        if command == "!game":
            if not self._ctx.game:
                return "Abhi stream ka topic set nahi hai. Chat mein batao, kya chal raha hai?" if hi else "The topic of this stream is not set yet. Tell me what is going on in chat!"
            if hi:
                return f"Abhi chal raha hai: {self._ctx.game}. Iske baare mein kuch bhi poocho!"
            return f"Currently playing: {self._ctx.game}. Ask me anything about it!"
        return None

    def event_reply(self, msg: ChatMessage) -> str:
        name = self._safe_name(msg.username)
        hi = self._hinglish
        if msg.event_type is EventType.DONATION:
            return f"Bahut bahut shukriya support ke liye, {name}! 💜" if hi else f"Thank you so much for the support, {name}! 💜"
        if msg.event_type is EventType.MEMBERSHIP:
            return f"Crew mein aapka swagat hai, {name}! Judne ke liye thanks! 🎉" if hi else f"Welcome to the crew, {name}! Thanks for joining! 🎉"
        return f"Yahan hone ke liye thanks, {name}!" if hi else f"Thanks for being here, {name}!"

    def fallback_reply(self, category: Category, username: str, avoid: list[str]) -> str:
        name = self._safe_name(username)
        table = _TEMPLATES_HINGLISH if self._hinglish else _TEMPLATES
        templates = table.get(category.value) or table["general"]
        rendered = [t.format(user=name) for t in templates]
        avoid_keys = {compare_key(a) for a in avoid}
        fresh = [t for t in rendered if compare_key(t) not in avoid_keys]
        return self._rng.choice(fresh or rendered)

"""Pattern lists used by the inbound filter and the outbound guard.

Deliberately small. It is a safety net, not a moderation system: slurs are NOT enumerated here. Add more
terms (regular expressions, one per line, ``#`` comments allowed) in the file named by
``STEALTHBOT_EXTRA_LEXICON_FILE``; they are applied to both inbound abuse detection and outbound blocking.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

URL_RE = re.compile(
    r"(?i)\b(?:https?://|www\.)\S+"
    r"|\b[a-z0-9-]+\.(?:com|net|org|io|gg|tv|xyz|ly|me|live|stream|shop|ru|cn|tk)\b(?:/\S*)?"
)

_I = re.IGNORECASE

# Harassment aimed at a person (inbound: ignore + flag)
ABUSE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\b(?:kys|kill\s+your\s*self|go\s+die|neck\s+your\s*self|unalive\s+your\s*self)\b", _I),
    re.compile(
        r"\byou(?:'re|\s+are|r)?\s+(?:so\s+|such\s+a\s+|a\s+)?"
        r"(?:trash|garbage|ass|stupid|idiot|moron|dumb|useless|pathetic|worthless|loser)\b",
        _I,
    ),
    re.compile(r"\b(?:quit|stop)\s+streaming\b", _I),
    re.compile(r"\bworst\s+streamer\b", _I),
    re.compile(r"\b(?:shut\s+up|stfu)\b", _I),
]

# Attempts to steer the bot (inbound: ignore + flag, never sent to the model)
INJECTION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"ignore\s+(?:all\s+|any\s+|the\s+|your\s+)?(?:previous|prior|above|earlier)\s+(?:instructions?|prompts?|rules?)", _I),
    re.compile(r"\bdisregard\b.{0,40}\b(?:instructions?|rules?|prompt)\b", _I),
    re.compile(r"\b(?:reveal|show|print|repeat|display|tell\s+me|leak)\b.{0,20}\b(?:system\s+prompt|your\s+(?:prompt|instructions|rules))\b", _I),
    re.compile(r"\bsystem\s+prompt\b", _I),
    re.compile(r"\b(?:jailbreak|developer\s+mode|dan\s+mode)\b", _I),
    re.compile(r"\byou\s+are\s+now\b", _I),
    re.compile(r"\bnew\s+instructions?\s*:", _I),
    re.compile(r"</?\s*(?:viewer|system|assistant|user|instructions?)\s*>", _I),
]

# Never allowed in a bot message (outbound): profanity plus the abuse patterns above
_PROFANITY = re.compile(
    r"\b(?:fuck\w*|shit\w*|bitch\w*|asshole|cunt|bastard|whore|slut|dickhead|wanker|piss\s?off)\b", _I
)


@dataclass
class Lexicon:
    abuse_in: list[re.Pattern[str]] = field(default_factory=lambda: list(ABUSE_PATTERNS))
    injection_in: list[re.Pattern[str]] = field(default_factory=lambda: list(INJECTION_PATTERNS))
    banned_out: list[re.Pattern[str]] = field(default_factory=lambda: [_PROFANITY, *ABUSE_PATTERNS])

    def matches_abuse(self, text: str) -> bool:
        return any(p.search(text) for p in self.abuse_in)

    def matches_injection(self, text: str) -> bool:
        return any(p.search(text) for p in self.injection_in)

    def matches_banned(self, text: str) -> bool:
        return any(p.search(text) for p in self.banned_out)


def build_lexicon(extra_file: Path | None = None) -> Lexicon:
    lex = Lexicon()
    if extra_file and extra_file.exists():
        for raw in extra_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            try:
                pattern = re.compile(line, _I)
            except re.error as exc:
                logger.warning("Ignoring invalid lexicon pattern %r: %s", line, exc)
                continue
            lex.abuse_in.append(pattern)
            lex.banned_out.append(pattern)
    return lex

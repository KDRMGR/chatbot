"""Text sanitising and comparison helpers. Everything a viewer types is untrusted."""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone

MAX_MESSAGE_CHARS = 500
_EMOJI_JOINERS = {"‍", "️", "︎"}  # ZWJ + variation selectors keep emoji sequences intact
_LINE_BREAKS = set("\r\n\t\v\f  \x85")
_NON_WORD = re.compile(r"[^\w\s]", re.UNICODE)
_REPEATS = re.compile(r"(.)\1{2,}")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def sanitize_text(text: object, max_chars: int = MAX_MESSAGE_CHARS) -> str:
    """Strip control, zero-width, bidi-override and Unicode 'tag' characters; collapse whitespace; cap length.

    Format characters (category Cf) are removed because they are used to hide text from humans while
    still being read by an LLM ("ASCII smuggling") and to visually reorder text (bidi overrides).
    Emoji joiners are kept so emoji sequences still render.
    """
    value = unicodedata.normalize("NFC", str(text))
    out: list[str] = []
    for ch in value:
        if ch in _LINE_BREAKS:
            out.append(" ")
        elif ch in _EMOJI_JOINERS:
            out.append(ch)
        elif unicodedata.category(ch) in {"Cc", "Cf", "Cs", "Co", "Cn"}:
            continue
        else:
            out.append(ch)
    cleaned = " ".join("".join(out).split())
    return cleaned[:max_chars].rstrip()


def normalize_for_compare(text: str) -> str:
    """Aggressive normalisation for duplicate / similarity detection (case, punctuation, letter stretching)."""
    value = unicodedata.normalize("NFKC", text).casefold()
    value = _NON_WORD.sub("", value)
    value = _REPEATS.sub(r"\1\1", value)
    return " ".join(value.split())


def compare_key(text: str) -> str:
    """normalize_for_compare, but emoji-only messages keep their emoji instead of collapsing to ''."""
    return normalize_for_compare(text) or text.casefold().strip()


def escape_untrusted(text: str) -> str:
    """Make viewer text safe to embed between <viewer> tags: it can no longer close or forge a tag."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def word_count(text: str) -> int:
    return len(text.split())

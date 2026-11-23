"""Hinglish replies (Hindi in the English alphabet mixed with English): prompt, classification, fallbacks, and the safety nets
that must understand Hindi abuse too (an English-only word list would let it through both ways)."""
from __future__ import annotations

import random
import re

import pytest

from app.ai.prompt_manager import HINGLISH_RULE, PromptManager
from app.chat.context import SessionContext
from app.chat.filters import MessageFilter
from app.chat.lexicon import URL_RE, build_lexicon
from app.chat.models import Category, ChatMessage, EventType, Platform
from app.chat.output_guard import clean_reply
from app.chat.rules import _TEMPLATES, _TEMPLATES_HINGLISH, RuleEngine
from app.config import Settings
from app.personalities.defaults import default_personality_rows
from app.personalities.manager import PersonalityProfile

LEX = build_lexicon()
DEVANAGARI = re.compile(r"[ऀ-ॿ]")


def msg(text: str, viewer: str = "v1", **kw) -> ChatMessage:
    return ChatMessage(platform=Platform.SIMULATOR, viewer_id=viewer, username=viewer, message=text, **kw)


# ---------------------------------------------------------------------------- the language setting and the prompt
def test_the_language_setting_is_validated_and_normalised(tmp_path):
    assert Settings(state_dir=tmp_path).reply_language == "english"  # English stays the default; Hinglish is switched on per stream
    assert Settings(state_dir=tmp_path, reply_language=" HinGlish ").reply_language == "hinglish"
    with pytest.raises(ValueError, match="reply_language"):
        Settings(state_dir=tmp_path, reply_language="klingon")


def test_the_prompt_asks_for_hinglish_only_when_switched_on_and_forbids_devanagari_and_swearing(tmp_path):
    profile = PersonalityProfile.from_row(default_personality_rows()[0])
    english = PromptManager(Settings(state_dir=tmp_path)).system_prompt(profile, "BB20")
    hinglish = PromptManager(Settings(state_dir=tmp_path, reply_language="hinglish")).system_prompt(profile, "BB20")
    assert "Hinglish" not in english and "English only" in english  # English is an instruction now, not an absence
    assert HINGLISH_RULE in hinglish and "NEVER use Devanagari" in hinglish and "no swearing" in hinglish
    assert hinglish.startswith(english.split("\n- Language:")[0])  # the safety rules are all still there, first
    assert "Never include links" in hinglish and "untrusted" in hinglish


# ---------------------------------------------------------------------------- understanding Hinglish chat
@pytest.mark.parametrize("text,expected", [
    ("namaste", Category.GREETING), ("Namaskar sabko", Category.GREETING), ("ram ram bhai", Category.GREETING),
    ("jai shree ram", Category.GREETING), ("sat sri akal", Category.GREETING), ("salaam", Category.GREETING),
    ("kya hua bhai?", Category.QUESTION), ("kaun jeetega aaj", Category.QUESTION), ("kab aayega next episode", Category.QUESTION),
    ("kaise ho streamer", Category.QUESTION), ("kitne baje start hoga", Category.QUESTION), ("koi bata sakta hai ye kya hai", Category.QUESTION),
    ("mast stream hai", Category.GENERAL), ("bhai kya baat hai", Category.GENERAL),
])
def test_hindi_greetings_and_question_words_are_understood(settings, text, expected):
    result = MessageFilter(settings).classify(msg(text))
    assert (result.category, result.respond) == (expected, True), text


# ---------------------------------------------------------------------------- Hindi abuse, both directions
ABUSIVE = ["madarchod", "MADARCHOD", "maderchod", "bhenchod", "behenchod", "bahenchod", "bhosdike", "bhosdi", "chutiya", "chutiye",
           "gandu", "gaandu", "gaand", "lund", "lauda", "randi", "haramkhor", "harami", "kutta", "kutte", "kutiya", "kamina",
           "teri maa", "teri behen"]
BENIGN = ["Arre yaar, kya scene hai!", "Mast hai bhai, aage kya hoga?", "Pagal ho gaye ho kya, kitna mast hai", "bc this show is so good",
          "mc ne bahut accha bola", "Salman bhai ka gussa dekha?", "ghar ka kaam hai", "saala kya twist tha", "kal kya hoga", "bhai ek number",
          "Kutub Minar ghoomne gaya tha", "Randeep ne accha khela", "gandhi ji ki kahani", "chutney achhi thi", "lundon ki baat nahi"]


@pytest.mark.parametrize("word", ABUSIVE)
def test_hindi_abuse_is_ignored_when_it_comes_in_and_blocked_when_it_would_go_out(settings, word):
    sentence = f"tu {word} hai" if " " not in word else f"{word} ki"
    incoming = MessageFilter(settings).classify(msg(sentence, viewer=f"v-{word}"))
    assert (incoming.category, incoming.respond) == (Category.ABUSE, False), sentence  # never reaches the model
    outgoing = clean_reply(f"Haha {sentence}, chat!", lexicon=LEX, recent=[])
    assert outgoing.text is None and outgoing.reason == "banned_content", sentence


@pytest.mark.parametrize("text", BENIGN)
def test_ordinary_hinglish_is_not_mistaken_for_abuse(settings, text):
    assert not LEX.matches_abuse(text) and not LEX.matches_banned(text), text
    assert MessageFilter(settings).classify(msg(text, viewer=f"b-{abs(hash(text))}")).respond is True


def test_a_viewer_name_with_hindi_abuse_is_not_echoed_in_a_fallback_reply(tmp_path):
    engine = RuleEngine(Settings(state_dir=tmp_path, reply_language="hinglish"), SessionContext("S", "BB20"), LEX, random.Random(1))
    assert "madarchod" not in engine.fallback_reply(Category.GREETING, "madarchod123", []).lower()
    assert "friend" in engine.fallback_reply(Category.GREETING, "madarchod123", [])


# ---------------------------------------------------------------------------- the fallback lines
def test_hinglish_fallbacks_cover_every_category_the_english_ones_do():
    assert set(_TEMPLATES_HINGLISH) == set(_TEMPLATES)
    for key, lines in _TEMPLATES_HINGLISH.items():
        assert len(lines) >= len(_TEMPLATES[key]) - 1, key


def test_every_hinglish_fallback_would_pass_the_output_guard_and_is_clean(tmp_path):
    settings = Settings(state_dir=tmp_path, reply_language="hinglish")
    for key, lines in _TEMPLATES_HINGLISH.items():
        for line in lines:
            text = line.format(user="Rahul")
            checked = clean_reply(text, lexicon=LEX, recent=[], max_words=settings.reply_max_words, min_words=3, max_chars=500)
            assert checked.text, (key, line, checked.reason)
            assert not URL_RE.search(text) and not DEVANAGARI.search(text) and "{" not in text, line
            assert not LEX.matches_abuse(text) and not LEX.matches_banned(text), line
            assert not re.search(r"\b(?:GTA|police|heist|getaway|vehicle|steal)\b", text, re.I), line  # neutral about the game


def test_the_rule_engine_speaks_the_configured_language(tmp_path):
    ctx = SessionContext("StealthModeGG", "BB20")
    eng = RuleEngine(Settings(state_dir=tmp_path, reply_language="hinglish"), ctx, LEX, random.Random(2))
    eng_en = RuleEngine(Settings(state_dir=tmp_path), ctx, LEX, random.Random(2))
    greeting = eng.fallback_reply(Category.GREETING, "Rahul", [])
    assert greeting in [t.format(user="Rahul") for t in _TEMPLATES_HINGLISH["greeting"]]
    assert eng_en.fallback_reply(Category.GREETING, "Rahul", []) in [t.format(user="Rahul") for t in _TEMPLATES["greeting"]]
    donation = ChatMessage(platform=Platform.SIMULATOR, viewer_id="d", username="Rahul", message="", event_type=EventType.DONATION)
    assert "shukriya" in eng.event_reply(donation) and "Thank you" in eng_en.event_reply(donation)
    about = msg("!bot")
    assert "Main " in eng.handle_command(about) and "bot hoon" in eng.handle_command(about) and "I'm a bot" in eng_en.handle_command(about)
    assert "BB20" in eng.handle_command(msg("!game")) and "poocho" in eng.handle_command(msg("!game"))

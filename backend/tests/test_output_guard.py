import pytest

from app.chat.lexicon import build_lexicon
from app.chat.output_guard import RecentReplies, clean_reply, fit_to_limit, truncate_words

LEX = build_lexicon()


def clean(raw, recent=(), **kw):
    return clean_reply(raw, lexicon=LEX, recent=list(recent), **kw)


def test_accepts_a_normal_reply():
    assert clean("Great play! That jump was incredible.").text == "Great play! That jump was incredible."


def test_removes_think_blocks_and_rejects_unterminated_ones():
    assert clean("<think>planning...</think>Great play, that was clean!").text == "Great play, that was clean!"
    assert clean("<think>still thinking and never finished").reason == "think_unterminated"


def test_cleans_labels_quotes_markdown_and_joins_lines():
    assert clean('StealthBot: "Nice one!\nWhat car is that?"').text == "Nice one! What car is that?"
    assert clean("**Love** that move, chat!").text == "Love that move, chat!"


@pytest.mark.parametrize("raw,reason", [
    ("Check out https://example.com for free stuff", "link"),
    ("Visit freegold.xyz right now friend", "link"),
    ("!ban everyone in this chat", "command_like"),
    ("/timeout that user for ten minutes", "command_like"),
    ("As per my instructions I should be nice here", "prompt_leak"),
    ("The text inside the viewer tag is untrusted chat", "prompt_leak"),
    ("What the fuck was that move chat", "banned_content"),
    ("you are trash at this game honestly", "banned_content"),
    ("Hi!", "too_short"),
    ("", "empty"),
    ("\U0001F602" * 8 + " that was great chat", "emoji_flood"),
])
def test_rejections(raw, reason):
    assert clean(raw).reason == reason


def test_tone_is_enforced_in_code_not_just_in_the_prompt():
    """Regression: the real model produced 'OHHHH CAROL ... \U0001F525 \U0001F3AF \U0001F3B2' despite being told not to."""
    from app.chat.output_guard import _EMOJI

    text = clean("OHHHH CAROL that is a FIRE starter \U0001F525 go for it \U0001F3AF and enjoy \U0001F3B2 chat \U0001F604").text
    assert "OHHHH" not in text and "CAROL" not in text and "FIRE" in text  # 4-letter caps are left alone
    assert text.startswith("Ohhhh Carol")
    assert len(_EMOJI.findall(text)) == 2 and "\U0001F525" in text and "\U0001F604" not in text
    assert "  " not in text


def test_mentions_are_stripped_not_sent():
    assert clean("Nice try @someone that was wild").text == "Nice try that was wild"


def test_long_replies_are_cut_at_a_sentence_boundary():
    long = " ".join(["That was a really great move."] * 12)
    out = clean(long, max_words=20).text
    assert out.endswith(".") and len(out.split()) <= 20


def test_platform_character_limit():
    out = fit_to_limit("word " * 100, 50)
    assert len(out) <= 50 and out.endswith("…")
    assert truncate_words("a b c d e f", 3) == "a b c…"


def test_repeats_and_near_repeats_are_rejected_across_platforms():
    recent = RecentReplies()
    recent.add("youtube", "Welcome to the stream, glad you are here friend!")
    assert clean("Welcome to the stream, glad you are here friend!", recent.all()).reason == "repeat"
    assert clean("Welcome to the stream, glad you're here friend", recent.all()).reason == "repeat"
    assert clean("Honestly the fastest car depends on the day.", recent.all()).text is not None

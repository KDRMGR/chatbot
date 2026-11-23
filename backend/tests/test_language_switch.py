"""Switching GUY's reply language really switches it: English means English (not a missing instruction), a spoken language decides the answer to
a voice message, and switching forgets the old language's replies instead of copying them."""
from __future__ import annotations

import pytest

from app.ai.prompt_manager import PromptManager
from app.chat.context import SessionContext
from app.chat.lexicon import build_lexicon
from app.chat.models import Category, ChatMessage, Platform
from app.chat.output_guard import RecentReplies, clean_reply, looks_hinglish
from app.config import RateLimitConfig, Settings
from app.personalities.defaults import default_personality_rows
from app.personalities.manager import PersonalityProfile

LEX = build_lexicon()


@pytest.mark.parametrize("text", [
    "Arre yaar, this green circle is moving so fast!", "That was so good bhai, what do you think?", "Kya scene hai, honestly",
    "Mast play, let us see what happens next", "I think toh the plan is good and aap should try it",
])
def test_hinglish_in_a_reply_is_recognised(text):
    assert looks_hinglish(text)


@pytest.mark.parametrize("text", [
    "That car chase looks wild, who do you think wins this one?", "Chat, what is your favourite mission so far?", "Nice run! Is that a new route?",
    "He walks along the fence toward the marker, 92 feet away.",
])
def test_plain_english_is_not_mistaken_for_hinglish(text):
    assert not looks_hinglish(text)


def test_the_guard_only_applies_when_the_language_is_english():
    text = "Arre yaar, this green circle is moving so fast, what do you think?"
    assert clean_reply(text, lexicon=LEX, recent=[], allow_hinglish=False).reason == "language"
    assert clean_reply(text, lexicon=LEX, recent=[]).text  # Hinglish, Hindi and auto modes are untouched
    assert clean_reply("That green circle is moving so fast, what do you think?", lexicon=LEX, recent=[], allow_hinglish=False).text


async def test_english_mode_gets_english_even_when_the_model_keeps_answering_in_hinglish(make_app_client, fake_ollama):
    client, app = await make_app_client(reply_language="english")
    c = app.state.container
    answers = iter(["Arre yaar, mast hai bhai, kya baat hai!", "That is a great point, chat, what do you all think about it?"])
    fake_ollama.reply = lambda body: next(answers)
    result = await c.processor.submit(ChatMessage(platform=Platform.SIMULATOR, viewer_id="v", username="v", message="hello everyone, how is the stream"))
    assert result.reply == "That is a great point, chat, what do you all think about it?" and result.source == "ai"
    assert "English only" in fake_ollama.real_calls[0]["messages"][0]["content"]  # the system prompt said so
    assert "no Hindi words" in fake_ollama.real_calls[-1]["messages"][-1]["content"]  # and the retry said exactly what was wrong
    fake_ollama.reply = lambda body: "Arre yaar mast hai bhai!"  # the model never obeys: the bot still answers, in English, from a prepared message
    again = await c.processor.submit(ChatMessage(platform=Platform.SIMULATOR, viewer_id="w", username="w", message="hello again everyone, nice play"))
    assert again.source == "rule" and not looks_hinglish(again.reply)


async def test_switching_language_forgets_the_old_language_and_the_answers_kept_for_repeated_questions(make_app_client, fake_ollama):
    client, app = await make_app_client(reply_language="hinglish")
    c = app.state.container
    fake_ollama.reply = "Arre yaar, mast sawaal hai, kya lagta hai aapko?"
    await c.processor.submit(ChatMessage(platform=Platform.SIMULATOR, viewer_id="v", username="v", message="who will win tonight?"))
    assert c.generator._recent.all() and any(is_bot for _, _, is_bot in c.context._chat) and c.generator._cache
    out = await client.post("/api/language", json={"language": "english"})
    assert out.status_code == 200 and c.settings.reply_language == "english"
    assert c.generator._recent.all() == [] and not c.generator._cache  # nothing to copy, and no cached Hinglish answer to replay
    assert not any(is_bot for _, _, is_bot in c.context._chat)
    assert any(not is_bot for _, _, is_bot in c.context._chat)  # what viewers said is kept


def test_recent_replies_and_the_context_can_forget_the_bots_own_lines():
    r = RecentReplies()
    r.add("kick", "something")
    r.clear()
    assert r.all() == []
    ctx = SessionContext("S", "BB20")
    ctx.add_chat("rahul", "hello")
    ctx.add_chat("GUY", "hi rahul", is_bot=True)
    ctx.forget_bot_lines()
    assert "(you)" not in ctx.render(700) and "rahul: hello" in ctx.render(700)


# ---------------------------------------------------------------------------- the language the streamer SPEAKS
def test_a_spoken_language_decides_the_answer_to_that_voice_message(tmp_path):
    pm = PromptManager(Settings(state_dir=tmp_path, reply_language="hinglish"))
    profile = PersonalityProfile.from_row(default_personality_rows()[0])
    common = dict(profile=profile, category=Category.GENERAL, username="Boss (voice)", message="what do you think", context_text="c", game="GTA VI",
                  avoid=[], is_streamer=True, is_voice=True)
    assert "They spoke English: answer in English only" in pm.build(**common, speech_lang="en-IN")[1]["content"]
    assert "They spoke Hindi" in pm.build(**common, speech_lang="hi-IN")[1]["content"]
    plain = pm.build(**common)[1]["content"]
    assert "They spoke" not in plain  # no microphone language given: only the reply-language setting applies
    typed = pm.build(**{**common, "is_voice": False}, speech_lang="en-IN")[1]["content"]
    assert "They spoke" not in typed


async def test_the_microphone_language_travels_with_the_voice_message_and_forces_english_replies(make_app_client, fake_ollama):
    from app.relay.voice import make_voice_handler

    fast = RateLimitConfig(min_gap_s=0, max_per_window=1000, user_cooldown_s=0, max_chars=500)
    client, app = await make_app_client(reply_language="hinglish", rate_limits={"kick": fast, "simulator": fast, "youtube": fast})
    c = app.state.container
    c.adapters[Platform.KICK] = c.adapters[Platform.SIMULATOR]
    handler = make_voice_handler(c)
    answers = iter(["Arre yaar, mast hai bhai!", "Sounds great, boss, I think you should go for the white car!"])
    fake_ollama.reply = lambda body: next(answers)
    result = await handler("what do you think about the white car", {"role": "streamer", "user": "u", "lang": "en-IN"})
    assert result["ok"] and result["reply"] == "Sounds great, boss, I think you should go for the white car!"  # the Hinglish try was refused
    assert "They spoke English" in fake_ollama.real_calls[0]["messages"][-1]["content"]
    fake_ollama.reply = "Arre yaar, bilkul sahi bola bhai, ye toh mast hai!"
    hi = await handler("ye gaadi kaisi hai", {"role": "streamer", "user": "u", "lang": "hi-IN"})
    assert hi["ok"] and "yaar" in hi["reply"]  # a Hindi-speaking streamer still gets Hinglish back

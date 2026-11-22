"""Reply languages beyond English (Hinglish, Hindi, match-the-viewer), switchable while the bot runs, and conversations that keep the
same personality instead of flipping between characters."""
from __future__ import annotations

import random

import pytest

from app.ai.prompt_manager import AUTO_RULE, HINDI_RULE, HINGLISH_RULE, PromptManager
from app.chat.lexicon import build_lexicon
from app.chat.models import Category, ChatMessage, Platform
from app.chat.output_guard import clean_reply
from app.chat.rules import _TEMPLATES_HINGLISH, RuleEngine
from app.chat.context import SessionContext
from app.config import REPLY_LANGUAGES, Settings
from app.personalities.defaults import default_personality_rows
from app.personalities.manager import PersonalityManager, PersonalityProfile
from tests.supabase_fakes import STREAMER_ID
from tests.test_relay import rig as relay_rig

LEX = build_lexicon()
PROFILE = PersonalityProfile.from_row(default_personality_rows()[0])


def msg(text: str, viewer: str = "v1", **kw) -> ChatMessage:
    return ChatMessage(platform=Platform.SIMULATOR, viewer_id=viewer, username=viewer, message=text, **kw)


# ---------------------------------------------------------------------------- the setting and the prompt
def test_four_languages_exist_and_english_stays_the_default(tmp_path):
    assert REPLY_LANGUAGES == ("english", "hinglish", "hindi", "auto")
    assert Settings(state_dir=tmp_path).reply_language == "english"
    for language in REPLY_LANGUAGES:
        assert Settings(state_dir=tmp_path, reply_language=f" {language.upper()} ").reply_language == language
    with pytest.raises(ValueError, match="reply_language"):
        Settings(state_dir=tmp_path, reply_language="klingon")


def test_every_language_gets_its_own_rule_and_none_of_them_can_drop_the_safety_rules(tmp_path):
    prompts = {lang: PromptManager(Settings(state_dir=tmp_path, reply_language=lang)).system_prompt(PROFILE, "BB20") for lang in REPLY_LANGUAGES}
    safety = prompts["english"].split("\n\nYour personality")[0]
    assert "Language:" not in prompts["english"]
    assert HINGLISH_RULE in prompts["hinglish"] and "NEVER use Devanagari" in prompts["hinglish"]
    assert HINDI_RULE in prompts["hindi"] and "Devanagari script" in prompts["hindi"] and "NEVER use Devanagari" not in prompts["hindi"]
    assert AUTO_RULE in prompts["auto"] and "same language" in prompts["auto"] and "viewer message" in prompts["auto"]
    for language, prompt in prompts.items():
        assert prompt.startswith(safety), language  # the code-owned safety rules come first, in every language
        assert "Never include links" in prompt and "untrusted" in prompt, language
        if language != "english":
            assert "no swearing" in prompt or "no insulting slang" in prompt, language


def test_an_unset_topic_is_said_plainly_and_the_ai_is_told_not_to_guess_it(tmp_path):
    pm = PromptManager(Settings(state_dir=tmp_path))
    assert "topic is: not set yet" in pm.system_prompt(PROFILE, "")
    assert "do not guess what the stream is about" in pm.system_prompt(PROFILE, "")
    ctx = SessionContext("Streamer", "")
    assert "Game: not set yet" in ctx.render(500) and ctx.game_label == "not set yet"


def test_prepared_replies_without_the_ai_use_the_hinglish_ones_for_hindi_and_auto(tmp_path):
    for language, hinglish in (("english", False), ("hinglish", True), ("hindi", True), ("auto", True)):
        s = Settings(state_dir=tmp_path, reply_language=language)
        rules = RuleEngine(s, SessionContext("S", "BB20"), LEX, rng=random.Random(1))
        text = rules.fallback_reply(Category.GREETING, "Rahul", [])
        in_hinglish = any(t.format(user="Rahul") == text for t in _TEMPLATES_HINGLISH["greeting"])
        assert in_hinglish is hinglish, language


def test_the_game_command_works_with_no_topic_set(tmp_path):
    rules = RuleEngine(Settings(state_dir=tmp_path), SessionContext("S", ""), LEX)
    assert "not set yet" in rules.handle_command(msg("!game"))


# ---------------------------------------------------------------------------- Devanagari: the safety nets understand it too
DEVANAGARI_ABUSE = ["मादरचोद", "तू बहनचोद है", "भोसड़ीके", "भोसड़ीके", "चूतिया", "गांडू", "रंडी", "हरामखोर", "कमीना", "तेरी माँ", "माँ की आँख"]
DEVANAGARI_BENIGN = ["नमस्ते दोस्तों", "आज का एपिसोड कमाल था", "लंडन में बारिश हो रही है", "सलमान खान ने क्या बोला", "मज़ा आ गया भाई", "बिग बॉस 20 शुरू हो गया"]


@pytest.mark.parametrize("text", DEVANAGARI_ABUSE)
def test_devanagari_abuse_is_ignored_coming_in_and_blocked_going_out(text):
    assert LEX.matches_abuse(text) and LEX.matches_banned(text), text


@pytest.mark.parametrize("text", DEVANAGARI_BENIGN)
def test_ordinary_devanagari_is_not_mistaken_for_abuse(text):
    assert not LEX.matches_abuse(text) and not LEX.matches_banned(text), text


# ---------------------------------------------------------------------------- replies may only use scripts the word lists understand
def guard(text: str, **kw):
    return clean_reply(text, lexicon=LEX, recent=[], **kw)


@pytest.mark.parametrize("text", [
    "नमस्ते सीता! আশা করি আজকের এপিসোডটা দারুণ হবে।",  # Hindi that drifts into Bengali: seen from a real model
    "Arre yaar, مرحبا kya haal hai", "Haha bhai 你好 kya scene hai", "Hey Привет kya baat hai bhai", "अरे यार ผมชอบมาก क्या बात है",
])
def test_a_reply_in_any_other_script_is_never_sent_because_it_could_not_be_checked_for_abuse(text):
    assert guard(text, allow_devanagari=True).reason == "script"
    assert guard(text, allow_devanagari=False).reason == "script"


def test_latin_letters_with_accents_digits_and_emoji_are_fine_in_every_language():
    for language in (True, False):
        out = guard("Qué bueno, café at 8 pm? Hola! 😄 Haha ek number bhai, maza aa gaya", allow_devanagari=language)
        assert out.text and out.reason is None


def test_devanagari_is_only_sent_when_the_language_setting_allows_it():
    hindi = "अरे अर्जुन, स्वागत है! क्या बात है भाई, मज़ा आ गया।"
    assert guard(hindi, allow_devanagari=True).text == hindi
    assert guard(hindi, allow_devanagari=False).reason == "script"  # English and Hinglish are written in English letters
    assert guard("अरे Arjun, स्वागत है BB20 में, Salman bhai ke saath!", allow_devanagari=True).text  # names may stay in English letters


async def test_a_half_bengali_answer_is_replaced_by_a_clean_retry_or_a_prepared_message(make_app_client, fake_ollama):
    client, app = await make_app_client(reply_language="auto")
    bad = "नमस्ते सीता! আশা করি আজকের এপিসোডটা দারুণ হবে। कौन जीतेगा!"
    good = "नमस्ते सीता! आज का एपिसोड बहुत मज़ेदार रहेगा, देखते हैं क्या होता है।"
    answers = iter([bad, good])
    fake_ollama.reply = lambda body: next(answers)  # first attempt drifts into Bengali, the retry is clean
    c = app.state.container
    result = await c.processor.submit(msg("नमस्ते दोस्तों, आज का एपिसोड कैसा रहेगा?", "sita"))
    assert result.action == "replied" and result.reply == good and result.source == "ai"
    fake_ollama.reply = lambda body: bad  # the model keeps drifting: the bot still answers, with a prepared message
    again = await c.processor.submit(msg("आज क्या होगा भाई, कुछ बताओ?", "sita2"))
    assert again.action == "replied" and again.source == "rule" and "আ" not in again.reply


# ---------------------------------------------------------------------------- switching the language while the bot runs
async def test_the_panel_can_read_and_change_the_language_and_the_choice_is_checked(make_app_client):
    client, app = await make_app_client()
    shown = (await client.get("/api/language")).json()
    assert shown["language"] == "english" and [o["id"] for o in shown["options"]] == list(REPLY_LANGUAGES)
    for bad in ({"language": "klingon"}, {"language": 5}, {}, {"language": "auto", "extra": 1}, []):
        assert (await client.post("/api/language", json=bad)).status_code == 422, bad
    assert app.state.container.settings.reply_language == "english"
    ok = await client.post("/api/language", json={"language": "hindi"})
    assert ok.status_code == 200 and ok.json()["language"] == "hindi" and ok.json()["saved"] is True
    assert app.state.container.settings.reply_language == "hindi"  # the next reply is already written in it
    assert (await client.post("/api/language", content=b"not json")).status_code == 422


async def test_the_api_needs_the_key_for_the_language(make_app_client):
    client, _ = await make_app_client()
    client.headers.pop("X-API-Key")
    assert (await client.get("/api/language")).status_code == 401
    assert (await client.post("/api/language", json={"language": "auto"})).status_code == 401


async def test_the_language_chosen_in_the_panel_wins_after_a_restart(make_app_client):
    client, _ = await make_app_client(reply_language="hinglish")
    await client.post("/api/language", json={"language": "auto"})
    client2, app2 = await make_app_client(reply_language="hinglish")  # a restart with the same saved state
    assert app2.state.container.settings.reply_language == "auto"
    assert (await client2.get("/api/language")).json()["language"] == "auto"


async def test_a_saved_language_that_is_not_valid_is_ignored(make_app_client):
    client, app = await make_app_client()
    await app.state.container.repo.set_setting("reply.language", '"klingon"', __import__("app.chat.normalizer", fromlist=["now_utc"]).now_utc())
    _, app2 = await make_app_client(reply_language="hinglish")
    assert app2.state.container.settings.reply_language == "hinglish"


async def test_the_panel_relay_can_change_the_language_and_only_for_admins(make_app_client):
    fake, svc, app, client, _ = await relay_rig(make_app_client)
    admin = fake.add_command("set_language", {"language": "hindi"})
    streamer = fake.add_command("set_language", {"language": "auto"}, by=STREAMER_ID)
    bad = fake.add_command("set_language", {"language": "klingon"})
    await svc.process_commands()
    assert fake.command(admin)["status"] == "done" and app.state.container.settings.reply_language == "hindi"
    assert fake.command(streamer)["status"] == "rejected" and "cannot run" in fake.command(streamer)["result"]["error"]
    assert fake.command(bad)["status"] == "failed"
    assert app.state.container.settings.reply_language == "hindi"


async def test_the_published_status_lists_the_language_the_characters_and_who_wrote_each_reply(make_app_client):
    fake, svc, app, client, _ = await relay_rig(make_app_client)
    result = await app.state.container.processor.submit(msg("hello everyone", "v1"))
    assert result.action == "replied"
    data = await svc.build_status()
    assert data["language"]["language"] == "english" and "auto" in [o["id"] for o in data["language"]["options"]]
    names = [p["name"] for p in data["personalities"]]
    assert names == ["ChatBuddy", "HypeBot", "RoastBot", "QuizBot", "StreamBuddy"]
    assert all(set(p) == {"name", "enabled", "description", "ready_in_s"} for p in data["personalities"])  # no prompt text leaves the PC
    answered = next(line for line in data["feed"] if line["reply"])
    assert answered["personality"] == "ChatBuddy"


# ---------------------------------------------------------------------------- conversations keep their voice
def manager(**freq: float) -> PersonalityManager:
    rows = default_personality_rows()
    for row in rows:
        row["cooldown_seconds"] = 0
        row["frequency"] = freq.get(row["name"], row["frequency"])
    return PersonalityManager((PersonalityProfile.from_row(r) for r in rows), rng=random.Random(7))


def test_a_preferred_personality_is_used_when_it_can_handle_the_message():
    m = manager(ChatBuddy=0.0001, HypeBot=1000, RoastBot=0.0001)
    picks = {m.select(Category.JOKE, prefer="RoastBot").name for _ in range(30)}
    assert picks == {"RoastBot"}  # however unlikely it would have been
    assert {m.select(Category.JOKE).name for _ in range(30)} == {"HypeBot"}  # no preference: the normal weighted choice


def test_a_preference_is_ignored_when_that_personality_cannot_or_may_not_speak():
    m = manager()
    assert m.select(Category.GREETING, prefer="HypeBot").name == "ChatBuddy"  # HypeBot does not handle greetings
    assert m.select(Category.GREETING, prefer="NoSuchBot").name == "ChatBuddy"
    m.get("RoastBot").enabled = False
    assert m.select(Category.GAMEPLAY_SUGGESTION, prefer="RoastBot").name == "HypeBot"  # switched off in the panel
    m.get("HypeBot").frequency = 0
    assert m.select(Category.GAMEPLAY_SUGGESTION, prefer="HypeBot") is None  # frequency 0 means "never" and nobody else handles it


def test_a_preferred_personality_may_answer_inside_a_conversation_while_cooling_down():
    now = [0.0]
    rows = default_personality_rows()
    m = PersonalityManager((PersonalityProfile.from_row(r) for r in rows), clock=lambda: now[0], rng=random.Random(3))
    m.mark_used("RoastBot")
    assert m.get("RoastBot").cooldown_seconds > 0 and m.seconds_until_ready("RoastBot") > 0
    assert m.select(Category.JOKE, prefer="RoastBot").name == "RoastBot"


async def test_a_viewer_keeps_the_same_personality_for_the_conversation_and_others_do_not_inherit_it(make_app_client):
    client, app = await make_app_client()
    c = app.state.container
    for p in c.personalities.all():
        p.frequency = 0.0001
    c.personalities.get("RoastBot").frequency = 1000
    first = await c.processor.submit(msg("haha lol that was great", "viewer-a"))
    assert first.action == "replied" and first.personality == "RoastBot"
    c.personalities.get("RoastBot").frequency = 0.0001
    c.personalities.get("ChatBuddy").frequency = 1000  # normally ChatBuddy would win now
    again = await c.processor.submit(msg("lol another one", "viewer-a"))
    assert again.personality == "RoastBot"  # same viewer, same conversation: same voice
    other = await c.processor.submit(msg("lol me too", "viewer-b"))
    assert other.personality == "ChatBuddy"  # a different viewer starts with the normal choice


async def test_the_conversation_ends_after_the_sticky_time(make_app_client):
    client, app = await make_app_client(conversation_sticky_s=0.0)
    c = app.state.container
    for p in c.personalities.all():
        p.frequency = 0.0001
    c.personalities.get("RoastBot").frequency = 1000
    assert (await c.processor.submit(msg("lol that was wild", "viewer-a"))).personality == "RoastBot"
    c.personalities.get("RoastBot").frequency = 0.0001
    c.personalities.get("ChatBuddy").frequency = 1000
    assert (await c.processor.submit(msg("haha what a moment", "viewer-a"))).personality == "ChatBuddy"  # no memory of the last voice


async def test_the_streamer_always_talks_with_the_conversational_personality(make_app_client):
    client, app = await make_app_client()
    c = app.state.container
    for p in c.personalities.all():
        p.frequency = 0.0001
    c.personalities.get("StreamBuddy").frequency = 1000  # for a plain message the other character would almost always win
    result = await c.processor.submit(msg("mast stream hai aaj", "boss", is_streamer=True))  # plain chat: ChatBuddy AND StreamBuddy handle it
    assert result.category is Category.GENERAL and result.action == "replied" and result.personality == "ChatBuddy"
    c.personalities.get("ChatBuddy").enabled = False  # switched off: the normal choice takes over instead of silence
    result = await c.processor.submit(msg("bhai kya baat hai", "boss", is_streamer=True))
    assert result.action == "replied" and result.personality == "StreamBuddy"

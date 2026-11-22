# Reply language, the five characters, and the model

## Reply language (switchable in the panel, remembered)
| Setting | The bot writes | Notes |
|---|---|---|
| `english` | English | the default in the code |
| `hinglish` | Hindi in English letters mixed with English, the way Indian chats talk | never Devanagari |
| `hindi` | everyday Hindi in Devanagari | names may stay in English letters |
| `auto` | in the language each viewer wrote in (English gets English, Hinglish gets Hinglish, Devanagari gets Devanagari; nothing to answer to = Hinglish) | any other language falls back to simple English |

`STEALTHBOT_REPLY_LANGUAGE` is only the starting value: the admin page's "Reply language" menu (or `POST /api/language`) changes it for
the very next reply and the choice is saved and wins over `.env` after a restart. Code-owned rules in `prompt_manager.py`, so an edited
personality prompt can never drop them. The prepared messages used when the AI is down exist in English and Hinglish (Hindi and auto use
the Hinglish ones).

**Safety in every language.** The abuse word lists now cover English, Hinglish and Devanagari Hindi, both ways (a viewer message with
such a word is ignored and never reaches the model; the bot may never say one). Because the lists cannot cover every language, a reply
may only contain **Latin and Devanagari letters**: a reply with Bengali, Arabic, Chinese or Cyrillic letters is not sent (one real test
reply came out half Bengali), and Devanagari is only sent when the setting is `hindi` or `auto`. The bot retries once, then falls back to
a prepared message. Tested with deliberate-break checks.

## The five characters: one at a time, per message, with conversation memory
ChatBuddy, HypeBot, RoastBot, QuizBot and StreamBuddy are five **prompts on one shared model**, never all at once and never several
for one message. For every message the bot:
1. works out what kind of message it is (greeting, question, joke, gameplay suggestion, general chat);
2. takes the enabled characters that handle that kind (greeting and question: only ChatBuddy; joke: ChatBuddy, HypeBot, RoastBot;
   gameplay suggestion: HypeBot, RoastBot; general chat: ChatBuddy, StreamBuddy), drops those still on their cooldown, and picks one by
   weight (frequency). A greeting or question is always answered, even if the only fitting character spoke recently;
3. **keeps the conversation in one voice (new):** a viewer the bot answered within the last 2 minutes
   (`STEALTHBOT_CONVERSATION_STICKY_S`) keeps getting the same character if it handles the new message too, so a chat with the bot does not
   flip between characters; and **the streamer's own words (voice or typed) always go to ChatBuddy**
   (`STEALTHBOT_CONVERSATION_PERSONALITY`), unless it is switched off.
Unprompted messages (the proactive scheduler) use QuizBot (a question about the show, every 25 minutes at most), StreamBuddy (when chat is
quiet) and HypeBot (hype). The panel shows who wrote each reply and lists the characters; their prompts stay on this PC.

## The model: gemma3:4b
Compared on this PC with the bot's real prompts (9 messages: Hinglish, Devanagari, English, match-the-viewer; both models fully on the
RTX 3050; output guard on):

| | qwen3:4b-instruct-2507 (before) | gemma3:4b (now) |
|---|---|---|
| speed | 48.9 tokens/s, 1.5 s per reply | 52.5 tokens/s, 1.0 s per reply |
| Hinglish | passable, sometimes nonsense ("koi saal chahiye?") | natural and short |
| Hindi (Devanagari) | broken words ("अरजुन बhai … बेस्ट डेट लाइव में") | clean ("अरे अर्जुन, स्वागत है!") |
| habits | invents mash-ups of headline words | starts everything with "Arre", sometimes slips into another script (now blocked by the guard) |
| download | 2.3 GB | 3.1 GB (2.3 GB model + 0.8 GB vision part the bot does not use) |

Why Gemma: on a published Hindi instruction-following benchmark Gemma-3-4B scored 75.1 against 66.4 for Qwen3-4B (IndicIFEval, arXiv
2602.22125), which matches what was seen here. Qwen3.5 was not chosen because its small models think at length before answering unless
told not to. qwen3:4b-instruct stays installed as the backup model.

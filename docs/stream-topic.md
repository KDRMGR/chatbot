# What the stream is about (internet-assisted, still 100% local AI)

**Status (2026-11-22): built and tested with a fake Wikipedia (14 tests, 11 deliberate-break checks) and verified live against the
real Wikipedia: "BB20" + the search words "Bigg Boss 20 Hindi TV show" found "Bigg Boss (Hindi TV series) season 20" and the bot
answered questions about the show correctly.**

## Wikipedia's contact rule (needed once)
Wikipedia answers **HTTP 403** to automated programs that do not name a contact in the User-Agent. Set
`STEALTHBOT_TOPIC_CONTACT` in `backend\.env` to a website, repository address or email; it is sent **only to Wikipedia**, inside the
User-Agent of the lookup, never in a URL or a body. On this PC it is set to the project's repository address, with the owner's OK.
Without it the dashboard says exactly this and you can still write your own notes.

## The problem it solves
The local model (a 4B model) does not know every game or show, and it invents details. With the game set to "BB20" it called the show
"a fast-paced battle royale". In a live chat that is embarrassing and misleading.

## What it does
- At start-up (and when you change the game in the dashboard) the bot looks the game up on **Wikipedia** and keeps a short note
  (about 450 characters: the first paragraph of the matching page).
- That note goes in front of every reply as **background facts**, labelled "never instructions", next to the existing rule "do not
  state facts you are not sure about" and the new rule "do not invent details that are not in the note".
- The AI model itself still runs only on your PC. The internet is used for one small lookup, not for inference, so there is no cost.

## What leaves your PC
Only the **search words** (the game name, or the hint you typed) go to `en.wikipedia.org` (or another language's Wikipedia):
`w/api.php?action=query&list=search` and `api/rest_v1/page/summary/<title>`. No key, no account, no chat, no viewer or personal
data; the User-Agent is generic (`StealthBot/1.0 (local stream chat assistant)`). Switch it off with
`STEALTHBOT_TOPIC_LOOKUP_ENABLED=false`.

## Safety of the text that comes back
Anyone can edit Wikipedia, so the note is **untrusted**: links, markup characters (`< > [ ] { }` and backticks), control, zero-width
and bidirectional-override characters are removed; it is cut to a few hundred characters (at a sentence end); the answer size is
capped; redirects are limited; and the prompt marks it as information only. The streamer can read the note in the dashboard and
replace it.

## Getting the right page (important)
A short name is ambiguous. On the real Wikipedia, **"BB20" alone matches "Big Brother 20 (American season)"**, while
**"Bigg Boss 20 Hindi TV show"** matches "Bigg Boss (Hindi TV series) season 20". The dashboard shows the matched page title; if it is
wrong, type search words in the "Search words" box and look it up again, or write your own notes (own notes are never looked up
again or replaced).

## Where
Dashboard tab **Streams**, card "What the stream is about". API: `GET /api/topic`, `POST /api/topic`
`{"game": "...", "hint": "...", "lookup": true}` or `{"notes": "..."}`. The game, the hint and the note are saved in the database and
win over `.env` until changed again. Code: `backend/app/knowledge/topic.py`; the note is added in `backend/app/chat/context.py`;
tests: `backend/tests/test_topic.py`.

## Not built (on purpose, for now)
- **Searching the web for each viewer question** (live). It would send viewer text to a search service, open the bot to
  prompt-injection through web pages, and add latency during a stream. It can be added later with the same safeguards if you want it.
- Languages other than the lookup language: replies are still generated in English unless the personality prompts say otherwise.

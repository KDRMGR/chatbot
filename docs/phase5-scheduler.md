# STEALTHBOT – Phase 5: proactive engagement (scheduler)

**Status (2026-11-21): built and tested** with the simulator platform: 33 automated scheduler tests (10 deliberate-break checks, all
caught) and a live run with the real Qwen3 model in the dashboard. **It has not run on real YouTube or Kick yet**, because those
connections are not verified (see `phase3-youtube-setup.md`). The scheduling logic does not depend on which platform it posts to.

## What Phase 5 covered

| Phase 5 task (spec) | State |
|---|---|
| Five personalities | Done in Phase 2 |
| Conversation context, viewer recognition, cooldowns, spam filtering | Done in Phase 2 |
| Message caching | Done in Phase 2: a question that was already answered in the last 10 minutes gets the same (re-checked) answer without asking the model again. Only repeated viewer *questions* are cached; proactive messages are never cached because they must not repeat |
| **Scheduling** | **Built now** (this document) |

## How it behaves (spec "Mode B: proactive engagement")

The scheduler looks at the clock every 5 seconds, separately for each platform:

1. **Connected?** If not, nothing happens and its timers restart when it reconnects, so a stream that starts late is not greeted
   with a backlog of "overdue" messages.
2. **Chat quiet?** No viewer message for `quiet_after` (default 6 min; the spec says 5-7). Every viewer message counts as activity,
   including ones the bot ignores (spam, commands): it never piles onto a busy or messy chat.
3. **Gap after the bot's own messages?** At least `min_gap` (default 4 min) since *any* bot message, including replies to viewers.
4. **Other platform?** A proactive message on one platform holds the others back for 45 s, so two platforms never get one at the
   same moment. Each platform generates its own text, and the repeat guard compares against messages from every platform, so the
   same text is never posted on both.
5. **Something due?** Three activities, each with its own timer and a random interval inside a range:

| Activity | Default range (spec) | Personality category | Who speaks |
|---|---|---|---|
| General engagement | 8-12 min | `proactive_general` | StreamBuddy |
| Gaming question | 15-20 min | `proactive_quiet` ("chat has gone quiet, ask one question about the game") | StreamBuddy |
| Trivia | 30-40 min | `proactive_quiz` | QuizBot |

If several are due, the one handled by the **highest-priority personality** goes first (QuizBot 2 beats StreamBuddy 1); equal
priorities go to the one that has waited longest. Only one message is sent per pass.

"Welcome message: when appropriate" and "viewer response: when relevant" are the reactive (Mode A) behaviour from Phase 2.

### Protections at the moment of sending
The message goes into the same queue as viewer messages, **behind all of them**, and is the first thing dropped if the queue is
full. Then every check runs again, because things change while a task waits and while the model writes:
paused? proactive off? platform connected? viewer spoke in the meantime? platform rate limit? **40% of each platform's message
allowance is kept for answering viewers** (YouTube: 7 per 10 min, so proactive may use at most 5). If the AI is down, a prepared
message is used, but **never one that was already posted recently**: silence beats a repeat. All the usual output checks apply
(no links, commands, mentions, shouting, over-long text).

### When something goes wrong
- *Skipped* (chat got busy, paused, rate limit, no personality ready): the activity stays due and it looks again after 30 s.
- *Failed* (send error, model produced nothing usable, queue dropped it): backs off 60 s, 120 s, 240 s ... up to 15 min, so a
  broken platform is not hammered. A successful send resets the backoff.
- The scheduler loop catches every error; it can never stop viewers from being answered.

## Controls

- **Dashboard card "Proactive chat"**: on/off switch (off = the bot only answers viewers), live status per platform (quiet for,
  may speak now, next activity in, messages sent), and the timing form (quiet time, gap, and min/max per activity, in minutes,
  with on/off per activity). Invalid input is refused with the reason and nothing is changed (all or nothing).
- Saved in the database, so edits survive a restart. The on/off switch itself is a runtime control like Pause and AI on/off: it
  starts from `STEALTHBOT_PROACTIVE_ENABLED` (default on) at every start.
- API: `GET /api/scheduler`, `POST /api/scheduler/settings`, `POST /api/control/proactive` (all need the API key).
- `backend/.env` / environment: `STEALTHBOT_PROACTIVE_ENABLED`, `_PLATFORMS` (default `youtube,kick`; the simulator is not
  included), `_QUIET_AFTER_S`, `_MIN_GAP_S`, `_PLATFORM_STAGGER_S`, `_VIEWER_RESERVE`, `_RETRY_S`, `_GENERAL_MIN_S/_MAX_S`,
  `_QUESTION_MIN_S/_MAX_S`, `_TRIVIA_MIN_S/_MAX_S`. Settings are validated at start.

## What was verified, and how

- **33 automated tests** drive the real app (queue, processor, database, simulator platform) with a fake clock: nothing before
  quiet and due; viewer activity (including spam) pauses it; minimum gap; on/off and unlisted platforms; priority order and
  most-overdue tie-break; timer restart; pause then recover; exponential backoff and recovery; viewer reserve; a viewer speaking
  while the message is written cancels it; viewers are processed before a queued proactive message; no repeats when the AI is down;
  reconnect resets timers; two platforms staggered with different text; live settings, persistence across a restart, 11 kinds of
  invalid settings, all-or-nothing updates, the API shape and key requirement.
- **Deliberate-break checks:** 10 safeguards were disabled one at a time (re-check after writing, proactive queue priority,
  reporting viewer activity, viewer reserve, repeat check, priority order, minimum gap, platform stagger, backoff, reset on
  reconnect); each was caught by at least one test, then restored.
- **Live (simulator + real `qwen3:4b-instruct-2507-q4_K_M`, in the dashboard):** a proactive StreamBuddy message was generated and
  posted when chat had been quiet; a viewer message made it wait again; the timing form saved, and refused an invalid pair; the
  on/off switch worked.
- Bugs found by testing and fixed: saved settings were rejected at the next start (a read-only field was saved with them);
  two overlapping scheduler passes could in principle double-send (now serialised with a lock); in the dashboard, the browser's own
  validation blocked saving ordinary values such as 20 seconds (0.33 minutes).

## Not verified / limits

- **Never run on real YouTube or Kick.** Real platform behaviour (slow mode, quota, message limits) is unknown until Phase 3/4 pass.
  For YouTube the adapter already refuses to send when the quota reserve is low, and a refused send is handled as a failure.
- QuizBot asks questions but does **not** track answers or scores.
- Interval defaults are the spec's starting values; whether they feel natural needs a real stream.
- Timers are not saved across restarts: a restart starts them fresh (with the usual quiet requirement first).
- Not yet editable in the dashboard: viewer reply cooldown and "maximum messages per window" (spec section F). They can be set in
  `.env` today; the dashboard form is part of the rest of Phase 6.
- The AI was slow during the live check because the laptop was on battery (see `dashboard-and-models.md`).

# STEALTHBOT – Phase 3: YouTube Live integration

**Status (2026-11-20): implemented and tested against a *fake* Google. It has NOT been run against real YouTube.** Everything
that needs your Google account (sign-in, a live stream) is pending; see `phase3-youtube-setup.md`. Until the live checks in §8
pass, "YouTube connects and receives chat" is **not** verified.

## 1. What was built

`backend/app/platforms/youtube/` (all pure Python, httpx only; no Google client libraries, no gRPC):

| Module | Job |
|---|---|
| `oauth.py` | Desktop-app OAuth: loopback redirect on `127.0.0.1`, PKCE (S256), state check, code exchange, refresh, revoke |
| `credentials.py` | Stores the OAuth client + tokens **DPAPI-encrypted** (`security/credential_store.py`), refreshes access tokens, flags "sign in again" |
| `api.py` | The few API methods needed, typed errors (`QuotaExceeded`, `RateLimited`, `LiveChatEnded`, ...), 401 → refresh once → retry |
| `quota.py` | Daily quota accounting with the midnight-Pacific reset; separate `search.list` bucket; survives restarts |
| `polling.py` | Activity-tiered, quota-paced poll interval (see §4) |
| `normalizer.py` | YouTube `liveChatMessage` → platform-neutral `ChatMessage` (messages, Super Chats, memberships; everything else ignored) |
| `adapter.py` | Discovery, polling, resume tokens, de-duplication, backoff, chat-ended handling, sending, status |
| `streaming.py` | EXPERIMENTAL tolerant decoder for an HTTP JSON stream (off by default; see §3) |

Plus `services/retention.py` (30-day data deletion), `scripts/youtube_login.py | youtube_check.py | youtube_logout.py`, and the
wiring in `main.py` (YouTube starts only if `STEALTHBOT_YOUTUBE_ENABLED=true`; any YouTube failure stays inside its own task).

## 2. Facts taken from Google's documentation (read 2026-11-20)

| Fact | Source |
|---|---|
| `liveChatMessages.list`: `GET https://youtube.googleapis.com/youtube/v3/liveChat/messages`, 5 units; honour `pollingIntervalMillis` | [list reference](https://developers.google.com/youtube/v3/live/reference/rest/v3/liveChatMessages/list), API discovery document rev. 20261117 |
| `liveChatMessages.insert`: `POST` the same path, `part=snippet`, 20 units; body `snippet.liveChatId`, `type=textMessageEvent`, `textMessageDetails.messageText` | [insert reference](https://developers.google.com/youtube/v3/live/reference/rest/v3/liveChatMessages/insert) |
| One scope, `youtube.force-ssl`, is accepted by list, insert, `liveBroadcasts.list`, `videos.list`, `channels.list`, `search.list` | discovery document |
| Default quota 10,000 units/day, reset at midnight Pacific; every request, even a failed one, costs at least 1 unit; `liveBroadcasts.list`/`videos.list`/`channels.list` = 1; `search.list` has its own 100-call/day bucket | [quota docs](https://developers.google.com/youtube/v3/determine_quota_cost) |
| The live chat id is `liveBroadcasts.list → snippet.liveChatId` (owner only) or `videos.list → liveStreamingDetails.activeLiveChatId` | reference pages |
| Loopback OAuth: `http://127.0.0.1:<port>`, PKCE recommended, token endpoint `https://oauth2.googleapis.com/token`, `invalid_grant` = sign in again | [native-app guide](https://developers.google.com/identity/protocols/oauth2/native-app) |
| Refresh tokens of an **External app in "Testing" expire after 7 days** | [OAuth expiration rules](https://developers.google.com/identity/protocols/oauth2#expiration) |
| Developer policy: stored API data (incl. channel ids / names) must be deleted or refreshed within **30 days**; delete on revocation (7 days); don't automate comments without the user's express consent | [developer policies](https://developers.google.com/youtube/terms/developer-policies) §III.E.4, III.I.2 |

Consent for automation (III.I.2): the account that posts is signed in by you, explicitly, in the browser consent screen.

## 3. What is NOT documented or NOT verified (stated plainly)

1. **`streamList` over plain HTTP.** Google's guide for it is gRPC-only (`grpcio` is a native extension that Smart App Control
   blocks here). The discovery document lists a nested HTTP path (`GET /youtube/v3/liveChat/messages/stream`, 5 units), but **no
   document describes its response framing, how long a connection lives, or how it is billed**. So the **default transport is
   polling `list`** (fully documented). The streaming transport exists, is **off by default** (`STEALTHBOT_YOUTUBE_USE_STREAMING`),
   decodes a JSON array, newline-delimited or concatenated JSON (tested with synthetic data only), and falls back to polling by itself
   if the stream misbehaves. `youtube_check.py --probe-stream` records the real framing so this can be confirmed or deleted.
2. **Maximum chat message length.** Google's API docs state none. The config uses 200 characters (the commonly known live-chat
   limit); unverified. Longer text is cut at a word boundary before sending.
3. **Slow mode / subscribers-only chat / new-account limits** and whether a moderator bot bypasses them: unverified.
4. **Whether the Google Auth Platform's personal-use mode exists on your screen**: reported by Google's docs, not confirmed.
5. **Actual quota billing** of anything beyond the documented per-call costs.
6. The unverified-app sign-in screen, the exact console labels, and the real latency of replies.

## 4. Quota: why the bot paces itself

At 5 units per poll, a flat 10 s poll costs ~10,800 units over six hours: more than the whole day. The governor therefore:

- polls every 10 s while chat is active, 30 s when it has been quiet for 2+ minutes, 60 s after 10 quiet minutes, and never faster
  than the interval YouTube asks for;
- gives **reading only 50% of the day** (10% is a final reserve, **40% is protected for sending**), spreading that over the planned
  stream length (6 h). With the default quota this means **≥ 21.6 s between polls** in a busy chat;
- refuses to send once sending would eat the 5% share reserved for reading, which leaves **~200+ messages per 6-hour stream**;
- trusts Google over itself: a server-side `quotaExceeded` marks the day exhausted until the Pacific-time reset;
- persists the day's count in the database so a restart does not reset it.

The per-platform rate limiter for YouTube defaults to 7 messages per 10 minutes (the Phase 2 default of 15 would not fit the quota).

## 5. Reliability design

- **Resume without duplicates:** the last `nextPageToken` per chat is kept; after any failure the read resumes there. A bounded
  set of recent message ids is a second guard, the database's unique `(platform, message id)` a third.
- **Backlog is never answered:** the first page of a new chat session (history from before the bot joined) is read and discarded.
- **Backoff:** 2 s doubling to a 60 s cap with jitter that never exceeds the cap. It resets only after a *successful poll*.
- **Chat ended / stream offline / chat disabled:** the adapter reports "disconnected", waits 60 s, and looks for the next broadcast;
  it does not hammer the API.
- **Auth problems** (no credentials, expired grant): the adapter reports `not authorised … run scripts\youtube_login.py`, makes no
  API calls and re-checks every 5 minutes. **Quota exhaustion** waits for the reset. Neither is retried in a tight loop.
- **Own echo:** the bot's channel id is read at start; messages from it are marked `is_bot_self` and ignored. The id of each message
  the bot posts is also stored, so the echo is a duplicate even if that check failed.
- **Isolation:** the adapter runs in its own task and catches everything; a YouTube outage leaves the simulator, the API and the
  database untouched (tested end to end).

## 6. Security and policy

DPAPI-encrypted credentials (tokens never in plaintext, the database, `.env`, logs or the repo); tokens are only sent in an
`Authorization` header and never appear in exceptions or `repr()`; the log filter redacts any that slip into text; OAuth uses PKCE
and a `state` check, and the loopback listener rejects forged requests without aborting the real sign-in and never echoes input;
30-day retention (`youtube_data_retention_days`, hard maximum 30) and a one-command logout that revokes, deletes credentials and
deletes all stored YouTube data.

## 7. Tests (268 mocked tests in the suite, +143 for Phase 3; 8 live Ollama tests unchanged)

Credential store (real Windows DPAPI: round trip, nothing in plaintext, wrong key fails, atomic writes, name validation) 7 ·
OAuth (RFC 7636 test vector, URL parameters, exchange/refresh/`invalid_grant`, loopback receiver incl. forged-state rejection and a
full simulated sign-in) 15 · quota (costs, reserve, Pacific reset across standard and daylight time, persistence) 11 ·
normalizer 11 · API client (headers, payloads, 401 → refresh → retry-once, 14 error mappings, transport errors, discovery, stream)
32 · stream decoder and poll governor 15 · adapter state machine 35 · full app with YouTube 8 · retention 6 · config 3.

Bugs the tests found while writing Phase 3 (all fixed): a **reconnection-loop bug** (backoff reset after every successful discovery,
so a consistently failing read retried every 2 s forever); a **design flaw** (reading could consume ~90% of the quota and starve
sending, now fixed with the protected send budget); a stream decoder that accepted a stream ending mid-document. Two mutation
checks (answering chat history; flipping the 30-day cutoff) were caught by 5 + 1 and 2 tests respectively, then reverted.

## 8. Acceptance status for Phase 3

| Requirement | Status |
|---|---|
| Authenticate with OAuth 2.0 | Implemented; **not tried against Google** |
| Identify the active broadcast / chat id | Implemented (video id, own broadcast, channel search); **not tried** |
| Receive messages | Implemented via polling; mocked tests only; **not verified live** |
| Send replies | Implemented; mocked tests only; **not verified live** |
| Reconnect after interruptions | Implemented and tested with simulated failures; **not tested with a real network drop** |
| Respect quotas | Implemented (accounting + pacing + reserve); real billing unverified |
| Handle ended/disabled chat | Implemented and tested with simulated errors |
| Connection-status indicator | In `/api/status` (`adapters.youtube`); dashboard widget is Phase 6 |
| Security | Done; see §6 |

**To finish Phase 3 I need the output of the three commands in `phase3-youtube-setup.md` step 6** (`--video-id`, `--send-test`,
`--probe-stream`) from a real unlisted/private live stream, plus a short supervised run of the bot in that chat.

## 9. Known limitations

- Reply latency is up to ~22 s in a busy chat (quota pacing); the streaming transport could remove that if it works.
- Quota usage by anything else in the same Cloud project is invisible to the bot.
- Messages from the streamer's own account are treated like any viewer (no special commands yet).
- Only text, Super Chat/Sticker and membership events are handled; gifts, polls and bans are ignored.

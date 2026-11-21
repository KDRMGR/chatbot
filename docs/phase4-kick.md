# STEALTHBOT – Phase 4: Kick integration

**Status (2026-11-21): implemented and tested against a *fake* Kick (77 tests). It has NOT been run against the real Kick.** It needs
your Kick developer app, a tunnel and a live stream (`phase4-kick-setup.md`). Until those checks pass, "Kick connects and receives
chat" is **not** verified. Route chosen: the official webhooks (option A in `phase4-kick-research.md`); the unofficial chat connection
was not built.

## What was built (`backend/app/platforms/kick/`, pure Python, httpx + the stack already in use)

| Module | Job |
|---|---|
| `oauth.py` | OAuth 2.1 code flow with PKCE (S256) to `id.kick.com`, fixed loopback redirect `http://localhost:8789/callback`, state check, refresh, revoke |
| `credentials.py` | Client and tokens stored **DPAPI-encrypted**; refresh (the refresh token is saved again at every refresh: Kick uses a sliding window); "sign in again" when Kick rejects it |
| `api.py` | The few endpoints needed: `GET /users`, `GET /channels`, `GET /public-key`, events subscriptions (list/create/delete), `POST /chat`; typed errors; 401 -> refresh once -> retry; tokens never in logs, errors or reprs |
| `rsa_verify.py` | **Pure-Python RSA PKCS#1 v1.5 / SHA-256 signature verification** (strict DER reader, rebuilds and compares the padded block in constant time, rejects short keys and `s >= n`) |
| `webhook.py` | The signed-webhook receiver: verifier (signature, timestamp window, replay memory, key rotation) + a separate loopback-only HTTP server with exactly one working route |
| `normalizer.py` | Webhook payload -> platform-neutral message: chat, new/renewed/gifted subscriptions (-> memberships), KICKs gifted (-> donations) |
| `adapter.py` | Connect, subscribe and keep subscribed, run the listener, own-echo detection, sending, status, reconnect/backoff |
| `scripts/kick_login.py`, `kick_check.py`, `kick_logout.py` | Sign in, read-only check (+ tunnel probe, optional subscribe/test post), revoke and delete |

## How it works
1. At start the adapter signs in with the stored credentials, asks Kick who the account is (`GET /users`) and for its channel
   (`GET /channels`), and checks the granted scopes (`user:read channel:read chat:write events:subscribe`).
2. It starts the **webhook listener on `127.0.0.1:8787`** (bound itself, loopback only) and subscribes to the wanted events
   (`chat.message.sent` by default). It lists first and only requests the missing ones, because re-subscribing is not documented.
3. Every `kick_resubscribe_interval_s` (5 min) it checks the subscription again (Kick unsubscribes an app whose webhook failed for a
   day), refreshes the channel's live flag, and restarts the listener if it died.
4. A delivery is accepted only if: the headers are present and well-formed, the timestamp is within 10 minutes, the RSA signature over
   `message_id + "." + timestamp + "." + body` matches Kick's public key (fetched from the API, re-fetched at most once a minute if
   verification fails, since Kick may rotate it), and the message id was not seen before. Then it is acknowledged (200) **before**
   our own processing, and trouble on our side never becomes an error response (Kick would drop the subscription).
5. The message enters the same pipeline as YouTube: filters, personalities, AI, output guard, rate limits, approval mode, the
   scheduler. Replies go out through `POST /chat` as type `bot` (documented bot identity) or `user` (setting).

## Security of the internet-facing part
- A **separate server on a separate port**; the control API and dashboard are not reachable through it (tested: `/api/*`,
  `/dashboard`, `/` answer 404 on the webhook port). Config refuses the same port for both.
- Bound to **127.0.0.1** (tested): only the tunnel program on this PC can connect.
- Nothing is parsed before the signature is valid; the verifier is **pure Python on purpose** (a native crypto library may be blocked
  by Smart App Control) and was checked against malformed padding, truncated/odd keys, 512-bit keys, a non-canonical second
  encoding of a valid signature, forged signatures, old and future timestamps, replays and key rotation.
- Body size limit 64 KB (enforced while reading, also without a Content-Length), answers never echo request data.
- Bad signatures cannot make the bot hammer Kick's key endpoint (one re-fetch per minute).
- If the webhook port is already taken, that is an ordinary error shown on the dashboard and retried (embedded uvicorn would have
  called `sys.exit()` and killed the bot, so the socket is bound by us first).

## What the documentation says and what is NOT verified (read 2026-11-21)

| Fact | Status |
|---|---|
| OAuth endpoints, PKCE S256, scopes, client secret required in token requests | Documented |
| `GET /users` and `GET /channels` without parameters return the token's own user/channel | Documented (swagger) |
| `POST /chat` type `bot`: `broadcaster_user_id` ignored, posts to the token's channel; type `user` needs it; 500 chars and 2048 bytes | Documented |
| Webhook headers, signature algorithm and signed string | Documented |
| **Under which name a `bot` message appears in chat** | **NOT documented: the first live post shows it** |
| `/oauth/authorize` is GET (docs page) or POST (swagger) | **Contradiction: GET is used; live sign-in decides** |
| Token error body: only `{"error": ...}` is documented | A 400/401 on refresh is treated as "sign in again"; unverified |
| Public key endpoint: JSON `{"data": {"public_key"}}` (swagger) or raw PEM (docs example) | Both accepted |
| Whether re-subscribing is an error | Not documented: the adapter only requests missing events |
| Whether `http://localhost:<port>/callback` is accepted as a redirect | Docs recommend `localhost`; the sign-in is the proof |
| Rate limits (only the 429 status; no `Retry-After`) | Not documented: backoff, and the adapter honours `Retry-After` if one comes |
| Webhook retry behaviour | Only "unsubscribed after a day of failures" is documented |
| Whether your tunnel + the URL typed into Kick actually deliver | Cannot be known from here: first live event |
| Stored-data / privacy rules for developers | Not documented; read Kick's Developer Terms. `kick_logout.py` deletes stored Kick data |

## Tests (77, all against a fake Kick; the real network was never touched)
Covered: signature and key handling (valid, tampered, wrong key, wrong size, sloppy padding, second encoding, junk and weak keys), the
verifier (every refusal with its status, old and future timestamps, replays, key rotation, a key-fetch outage, bounded memory), the
webhook app and the real embedded listener (loopback only, nothing else served, size limits, a taken port), payload mapping, OAuth
(PKCE URL, exact token requests, a real loopback sign-in that rejects a forged request, a taken port, revoke), the API client (every
error class, refresh-and-retry, the documented chat bodies and both length limits), the adapter (connect, delivery, forged and
repeated deliveries, echo handling, send failures, no sign-in, missing permissions, backoff and recovery, dropped subscription, taken
port, dead listener, disconnect/connect), and three end-to-end tests (a signed webhook over real HTTP through the whole pipeline to
the fake Kick chat API, a Kick outage that cannot touch the rest of the app, dashboard disconnect/connect). Deliberate-break checks:
17 safeguards disabled one at a time (signature check, signed string, timestamp window, replay handling, key re-fetch limit, size
limits, padding comparison, key size, `s >= n`, echo by id and by text, loopback-only bind, resubscription, refresh handling, secret
sent, 401 retry, acknowledgement on our own errors); every one is caught. Three were missed at first (the streamed size limit,
`s >= n`, echo by id) and got tests.

## Chatting in ANOTHER channel (added 2026-11-22; verified live up to the first reply)
A separate **bot account** can chat in a different channel (for example the bot account `saraxak` in `stealhmodegg`):
`STEALTHBOT_KICK_TARGET_CHANNEL=stealhmodegg` in `backend\.env`.

| Part | How |
|---|---|
| Reading that channel's chat | a webhook subscription made with the **app token** (`client_credentials`, no account, about 60 days; the channel must be named). **Verified live:** Kick accepted it and delivered chat from that channel to the bot through the tunnel |
| Answering | `POST /chat` as `type: "user"` with that channel's `broadcaster_user_id`, with the **signed-in account's** token (the `bot` type can only post to the token's own channel; the app token cannot post). The message appears under the bot account's name |
| Own messages | recognised by the sender id of the signed-in account, so the bot never answers itself |
| Safety | events for any other channel (old subscriptions) are counted and ignored; approval mode, rate limits and the output guard apply as everywhere |

Documented in Kick's swagger: subscription with a user token ignores `broadcaster_user_id`; with an app token it is required. **Not
yet verified:** a posted reply appearing in the other channel's chat (needs your approval click), and Kick's rules for bots in a
channel you do not own. Use it only in channels you own or have permission for. The bot account's name or bio should say it is a bot.

## Acceptance for Phase 4

| Requirement | Status |
|---|---|
| Authenticate | Implemented; **not tried against Kick** |
| Receive messages | Implemented via official webhooks; **not verified live** |
| Send messages | Implemented; **not verified live** (bot identity unknown) |
| Reconnect logic | Implemented and tested with simulated failures; no real network drop |
| Separate rate limits per platform | Kick has its own limiter (dashboard: Scheduler & limits) |
| Security | Done; see above |

**To finish Phase 4 I need the outputs described at the end of `phase4-kick-setup.md`:** the `kick_check.py --probe-tunnel` output,
what appeared in chat for a real viewer message, and the name the bot's reply was posted under.

## Known limitations
- Needs a tunnel and a hand-typed webhook URL (a changing quick-tunnel address means editing Kick's settings each stream).
- Only chat and the subscription/KICKs events are understood; follows, stream status and moderation events are ignored.
- A message with exactly the text the bot just posted is treated as its own echo (the bot's Kick identity is unknown); a viewer
  repeating the bot word for word within a minute is not answered.
- Threaded replies (`reply_to_message_id`) are off until seen working.

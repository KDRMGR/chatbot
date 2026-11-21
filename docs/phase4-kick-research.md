# STEALTHBOT – Phase 4 research: Kick integration (nothing built yet)

**Update 2026-11-21: option A below (official webhooks + tunnel) has since been built and tested against a fake Kick: see
`phase4-kick.md` and `phase4-kick-setup.md`. This document is the research it was based on.**

**Status when written (2026-11-21): research only. No Kick code existed and nothing had been tried against Kick.** Facts below come from Kick's
official developer documentation (the `KickEngineering/KickDevDocs` repository that backs docs.kick.com, plus
`https://api.kick.com/swagger/doc.yaml`). Where two official sources disagree, or the docs are silent, it says so. Per the project
rules no endpoint is used that is not documented here.

## The one finding that shapes everything

**Kick's official API delivers chat messages by webhook only.** There is no documented websocket or polling endpoint for chat.

> "Your webhook URL must be accessible over the public internet. Localhost URLs … won't work unless you expose them using tools like
> Cloudflare Tunnel, ngrok, or similar services." (`events/webhook-security.md`)

STEALTHBOT is deliberately local-only (the API listens on loopback and nothing is exposed to the internet). Receiving Kick chat
therefore needs one of these, and **this is your decision**:

| Option | How it works | Cost / risk |
|---|---|---|
| **A. Official webhooks + a tunnel** (recommended) | A small separate listener (loopback only, one route, verifies Kick's signature) receives events; a tunnel program (ngrok, Cloudflare Tunnel) forwards Kick's public URL to it. The control API and dashboard stay private. | Free tier of a tunnel service (check its current terms). Needs a **stable public URL**: the webhook URL is typed into Kick's developer settings by hand, so a URL that changes every run (e.g. a quick tunnel) means editing Kick's settings before every stream. A free static domain avoids that. The tunnel program is a native `.exe`; Smart App Control may block it if unsigned (to be tested). |
| **B. Unofficial chat websocket** | Reverse-engineered connection used by community tools. | Not documented by Kick, can break without notice, and may conflict with Kick's terms. **Against the project rule "never invent API endpoints"**, so I will not build it unless you explicitly decide to accept that risk. |
| **C. Send-only on Kick** | Bot can post to Kick chat but cannot read it. | Not useful for a chat companion. |

Kick also warns that if webhook delivery keeps failing for over a day it **auto-unsubscribes** the app from that event, so the bot
must re-subscribe at every start (cheap) and say so in its status.

## What the official docs say

### OAuth (`getting-started/generating-tokens-oauth2-flow.md`, `kick-apps-setup.md`)
- OAuth host `https://id.kick.com`; API host `https://api.kick.com` (paths `/public/v1/...`).
- Authorize: `GET https://id.kick.com/oauth/authorize` with `client_id`, `response_type=code`, `redirect_uri`, `state`, `scope`,
  `code_challenge`, `code_challenge_method=S256` (PKCE, same as the Google flow already built).
- Token: `POST https://id.kick.com/oauth/token` (form-encoded): `authorization_code` (+`code_verifier`), `refresh_token`,
  `client_credentials`. **A client secret is required** in every one of these bodies.
- Revoke: `POST https://id.kick.com/oauth/revoke`.
- Token lifetime: not in the markdown docs. The swagger **example** shows access 7200 s and a refresh token with a sliding 30-day
  window (resets on every refresh). Treat the numbers as unconfirmed.
- Redirect URI must match the app settings **exactly**. The docs recommend `http://localhost/...` for locally running apps (and give
  a workaround for `127.0.0.1`), so the sign-in listener will need a fixed port that you register once.
- Creating a Kick app requires **2FA** on the Kick account.

### Scopes needed (`scopes/scopes.md`)
`chat:write` (send chat messages and "allow chat bots to post in your chat"), `events:subscribe` (subscribe to chat messages and
other channel events), `user:read` (identify the account) and `channel:read` (channel info). Nothing else is needed for the
current plan. (`chat:read` does not exist in the scope list.)

### Receiving chat (`events/*`)
- `POST /public/v1/events/subscriptions`, body `{"broadcaster_user_id", "events":[{"name":"chat.message.sent","version":1}], "method":"webhook"}`.
  `GET` lists, `DELETE ?id=` removes. Limit for unverified apps: 1,000 subscriptions of `chat.message.sent` (irrelevant for one channel).
- Payload fields: `message_id`, `content`, `emotes`, `created_at`, `replies_to`, `broadcaster` and `sender`
  (`user_id`, `username`, `is_anonymous`, `is_verified`, `channel_slug`, `identity.badges`, ...).
- Headers: `Kick-Event-Message-Id` (idempotency key), `Kick-Event-Subscription-Id`, `Kick-Event-Signature`,
  `Kick-Event-Message-Timestamp`, `Kick-Event-Type`, `Kick-Event-Version`.
- **Signature check (mandatory for us, since the listener is reachable from the internet):** RSA PKCS#1 v1.5 with SHA-256 over
  `message_id + "." + timestamp + "." + raw body`, signature base64-decoded. The public key comes from
  `GET https://api.kick.com/public/v1/public-key` and "can be rotated at any time" (so it must be fetched, never hardcoded).
  Implementation note: `cryptography` is a native extension that Smart App Control may block, so the plan is a small pure-Python RSA
  public-key verifier (one modular exponentiation plus a strict padding comparison), tested with generated keys.
- Retries: only the one-day auto-unsubscribe rule is documented; no retry count or schedule.

### Sending chat (`apis/chat.md`, swagger)
- `POST /public/v1/chat` with `content` (max 500 characters), `type` (`"user"` or `"bot"`), optional `broadcaster_user_id`,
  `reply_to_message_id`. Response `data.is_sent`, `data.message_id`. Statuses 200/400/401/403/404/429.
- "You can send a message as a Bot account or your User account." Using `type: "bot"` is how the bot would be a **disclosed bot
  identity**; how that identity is named and displayed is **not documented** and must be seen live.
- No numeric rate limit is documented (only the 429 status), so the existing per-platform limiter will be set conservatively and
  adjusted from what the live test shows.
- Verified apps get a bot badge and higher limits (apply by emailing Kick). Not required.

## Not documented or contradicting (must be checked live)
1. Whether `http://localhost:<port>` redirect URIs work for a desktop app, and whether the client secret may be left out with PKCE.
2. Swagger lists `/oauth/authorize` as `POST` and the markdown as `GET`; swagger names the revoke hint `token_type_hint`, the markdown
   `token_hint_type`. Live behaviour decides.
3. Real token lifetimes, real rate limits, whether `chat:write` as `type: "bot"` appears under the app name, slow-mode handling.
4. Whether HTTPS is mandatory for the webhook URL (the docs say "publicly accessible"; every tunnel provides HTTPS anyway).
5. Webhook reliability: community reports (undated) describe delayed or missing `chat.message.sent` deliveries. Treat as a risk to
   measure in the Phase 7 soak test.
6. Kick's developer terms for self-hosted chat bots: the docs only say to read the Developer Terms and Conditions. **You should read
   them before creating the app.**

## What I need from you (only when we start Phase 4)
1. **Pick A, B or C above** (I recommend A).
2. For A: a tunnel account and a stable URL (I will give exact steps and check the provider's current free-plan terms first).
3. A Kick developer app (needs 2FA on the streamer's Kick account), then you sign in once in the browser for the OAuth consent.

I do **not** need any of this to keep building: the Kick adapter, webhook listener and signature verifier can be written and tested
against fakes first, exactly as Phase 3 was, and stay "unverified" until a real stream proves them.

## Sources (read 2026-11-21)
`KickEngineering/KickDevDocs` on GitHub: `getting-started/generating-tokens-oauth2-flow.md`, `getting-started/kick-apps-setup.md`,
`scopes/scopes.md`, `apis/chat.md`, `apis/public-key.md`, `apis/faqs.md`, `events/introduction.md`, `events/subscribe-to-events.md`,
`events/webhook-security.md`, `events/event-types.md`; and `https://api.kick.com/swagger/doc.yaml` (large file: part of it was read
in summary only, so figures from it are marked as examples above).

# Online panel through Supabase (Vercel-hosted web page, login with roles)

**Status (2026-11-22): the bot side and the database design are built and tested against a fake Supabase (21 tests, 14 deliberate-break
checks). NOT done: applying the schema to your Supabase project, creating the logins, the web page itself, and the Vercel deploy.
Nothing has been changed in your Supabase project yet (only read-only calls were made to look at it).**

## Why a relay and not a tunnel
The control API stays **loopback only**. Nothing on your PC listens for the internet. Instead the bot calls **out** to Supabase:

```
 web panel (Vercel, static page)  --login-->  Supabase Auth
        |  insert a command / read the status               ^
        v                                                    |  the bot signs in as its OWN limited user (role "bot")
   Supabase tables  <---------- publishes status, takes commands, writes the result ---------- bot on your PC
```

If Supabase or the internet is down, the bot keeps chatting and the local dashboard keeps working; only the online panel is out.

## Roles (who can do what, enforced by the database, then again by the bot)
| Role | Can | Cannot |
|---|---|---|
| **admin** (you) | read the live status; send any control command (pause/resume, approval, streams, topic, ...) | - |
| **streamer** | read the live status; send **only `say`** (hold-to-speak) | anything else, even if sent |
| **bot** (the bot's own login) | write the status; claim and finish commands; read profiles | read secrets, send commands, change roles |
| anyone else (no profile) | nothing | everything |

Roles live in `public.profiles`, which **nobody can change through the API** (no insert/update/delete policy): only the project owner
sets them. Signing up must be switched off in the project, so strangers cannot even create an account.

## What is built (backend, in this repository)
- `supabase/schema.sql`: tables `profiles`, `bot_status` (one row), `bot_commands` (the mailbox), row-level-security policies, column
  grants (a streamer can only write `kind` and `payload` of a new command), live updates. Safe to run twice.
- `backend/app/relay/supabase.py`: the bot's Supabase client (password sign-in, refresh, PostgREST). Holds **only the public key** and the
  bot user's login (encrypted with Windows DPAPI by `scripts\relay_login.py`). Tokens never appear in logs, errors or URLs.
- `backend/app/relay/service.py`: every 2 s it takes pending commands, every 3 s it publishes a snapshot (status, streams, topic, approvals,
  the last chat lines, relay health). A command is translated into a call to the bot's **own local API, in-process**, so every rule of the
  local dashboard (validation, limits, all-or-nothing settings) applies unchanged. It refuses a command that is too old (5 minutes), from a
  sender without a role, of a kind the sender's role may not run, or with fields it does not know; the refusal is written back.
- `GET /api/relay` on the local API shows the relay's state; the settings are `STEALTHBOT_RELAY_ENABLED`, `STEALTHBOT_SUPABASE_URL`,
  `STEALTHBOT_SUPABASE_PUBLISHABLE_KEY` (a powerful `service_role` / `sb_secret_` key pasted there by mistake is refused at start).

## Commands the panel can send (`kind`)
`pause`, `resume`, `set_approval`, `set_ai`, `set_proactive`, `approve`, `reject`, `select_stream`, `add_stream`, `update_stream`,
`remove_stream`, `stream_settings`, `set_topic`, and `say` (streamer/admin; **not switched on yet**: see below).

## Hold-to-speak (not built yet)
Planned: a hold-to-speak button on the streamer's page; the browser's speech recognition turns the speech into text, which is sent as a
`say` command. What the bot then does with it is **not decided**: post it under the streamer's own Kick account (needs a one-time
chat-permission sign-in as that account), hand it to the AI as a message from the streamer, or post it through the bot account with a
label. Until then the relay answers `say` with "voice replies are not switched on yet". The browser sends the audio to its vendor's speech
service (local engines are blocked on this PC by Windows Smart App Control).

## Setup (when the cloud steps are approved)
1. Apply `supabase/schema.sql` to the project; switch off sign-ups; create the three users and their profiles (admin, streamer, bot).
2. Put the project URL and the **public** key in `backend\.env`, run
   `& "$env:LOCALAPPDATA\stealthbot\venv\Scripts\python.exe" scripts\relay_login.py --email <the bot's email>`, set
   `STEALTHBOT_RELAY_ENABLED=true`, restart the bot.
3. Build and deploy the web page to Vercel (connect the GitHub repository), then set the project's site URL so password links return there.
4. **Rotate the Supabase personal access token** that was used for setup (it controls the whole account), and keep it out of OneDrive.

## Limits to know
- The status snapshot contains the recent chat lines (public chat, usernames and text) and is stored in your Supabase project: keep the
  project private and the sign-ups off. Finished commands are deleted after a day.
- A stolen **admin** login controls the bot (that is what admin means): turn on two-factor authentication for it.
- Latency is about 2-3 seconds (polling), fine for controls; hold-to-speak will feel slightly delayed.

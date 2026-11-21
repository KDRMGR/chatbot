# YouTube setup: what you need to do

About 20–30 minutes, once. **Nothing here costs money.** The YouTube Data API is free within its daily quota, and no billing
should be needed. If Google asks for a payment method at any step, stop and tell me.

I cannot do these steps for you: they create a Google Cloud project and sign in to a Google account, and I will not touch your
accounts. The console's screen labels change from time to time; the names below are the ones in Google's current documentation
(read 2026-11-20) and may differ slightly on your screen.

## Step 0 – Decide who the bot posts as (recommended: its own account)

The bot posts as whichever Google account signs in. **Use a separate account for the bot**, for example a new Google account with a
YouTube channel called "StealthBot", and say so in the channel's About text ("Automated chat assistant for StealthModeGG"). That
gives you the clearly disclosed bot identity the project requires, and the bot cannot be mistaken for you. If you used your own
account, the bot's messages would appear to come from you.

Consequences of a separate bot account:
- The bot cannot look up *your* live broadcast by itself, so you give it the stream's **video id** (the 11 characters after `v=` in
  the watch URL, or in YouTube Studio's stream page). Alternatively set your channel id and it will search for your live video.
- If your chat uses **slow mode** or **subscribers-only** chat, YouTube may block the bot unless it is a moderator or subscriber.
  I have not verified this; the live test below will show it.

## Step 1 – Create the Google Cloud project (sign in as the bot account)

1. Open <https://console.cloud.google.com/> signed in as the **bot's Google account**. Create a project, e.g. `stealthbot`.
2. Menu → **APIs & Services → Library**, search **YouTube Data API v3**, click **Enable**.

## Step 2 – Consent screen ("Google Auth Platform")

1. Menu → **Google Auth platform → Branding → Get started**. Enter the app name (`StealthBot`) and a support email.
2. Audience: choose **External**. Add a contact email, accept the Google API Services User Data Policy, **Create**.
3. **Data Access → Add or Remove Scopes**: add `https://www.googleapis.com/auth/youtube.force-ssl`, **Save**. (This single scope
   covers everything the bot does: reading chat, posting, finding the stream.)
4. **Avoid the 7-day trap.** While the app's publishing status is **Testing**, Google expires its refresh tokens after **7 days**,
   so the bot would silently lose access every week. On the **Audience** page, either:
   - click **Publish app** (status "In production"). Google will show an "unverified app" warning once when you sign in; for one
     personal account that is expected and fine; or
   - if the console offers a personal-use mode for this project, that avoids both the warning and the expiry, but it can only be
     used by the account that created the project and cannot be changed later (this is from Google's documentation; I could not
     confirm the exact screen).

   If you stay in Testing instead, add the bot account under **Audience → Test users** and expect to re-run the sign-in script
   every 7 days.

## Step 3 – Create the OAuth client

1. **Google Auth platform → Clients → Create client**.
2. Application type: **Desktop app**. Give it a name. **Create**.
3. **Download the JSON** if offered. The client secret may be shown **only once**, so download it or copy the Client ID and the
   secret now.

## Step 4 – Sign the bot in (you do this; it opens your browser)

Run, replacing the path with where the JSON was saved:

```powershell
& "$env:LOCALAPPDATA\stealthbot\venv\Scripts\python.exe" scripts\youtube_login.py --client-json "C:\Users\USER\Downloads\client_secret_xxx.json"
```

Without the JSON, use `--client-id <id>` instead; the secret is then asked for with hidden input (it never goes on the command line).

Your browser opens. **Sign in as the bot account**, click through the "Google hasn't verified this app" screen (Advanced → Go to
StealthBot), and approve. The terminal then prints `Authorised as YouTube channel: …`, so check that is the bot's channel and not
yours. The client id/secret and tokens are stored **encrypted with Windows DPAPI** in `%LOCALAPPDATA%\stealthbot\data`. Then
**delete the downloaded JSON file.**

## Step 5 – Point the bot at your stream

Create `backend\.env` (copy `backend\.env.example`) and set:

```
STEALTHBOT_YOUTUBE_ENABLED=true
STEALTHBOT_YOUTUBE_VIDEO_ID=<video id of today's stream>
```

(Or `STEALTHBOT_YOUTUBE_STREAMER_CHANNEL_ID=UC...` to let the bot find your live video itself. Pasting the video id is the most
reliable.)

## Step 6 – Test with a real live stream before going public

Start a stream as **Unlisted** or **Private** with live chat on, then, with the bot **not** running:

```powershell
& "$env:LOCALAPPDATA\stealthbot\venv\Scripts\python.exe" scripts\youtube_check.py --video-id <VIDEO_ID>
```

This is read-only: it checks the credentials, tells you which channel the bot is, finds the live chat and reads one page (counts
only, never message text). Then post one test message from the bot (asks you to type YES first; costs 20 quota units):

```powershell
& "$env:LOCALAPPDATA\stealthbot\venv\Scripts\python.exe" scripts\youtube_check.py --video-id <VIDEO_ID> --send-test "Hello! I'm StealthBot, an automated assistant."
```

Finally, Google documents a faster "stream" way of reading chat only for gRPC, which this PC cannot run. A plain-HTTP version
exists in Google's API description but its format is undocumented. This probe records what it really looks like (message text and
names are redacted from the output):

```powershell
& "$env:LOCALAPPDATA\stealthbot\venv\Scripts\python.exe" scripts\youtube_check.py --video-id <VIDEO_ID> --probe-stream
```

**Please send me the output of all three.** That is the evidence that decides whether Phase 3 works for real.

## Step 7 – Run it

```powershell
scripts\start.bat
```

Check `GET /api/status` (or the terminal client's `/status`) for `adapters.youtube`: state, channel, quota used.

## What the bot costs in quota

YouTube gives **10,000 units per day** (resetting at midnight Pacific Time). Reading chat costs 5 units per poll and posting costs
20 units per message. So the bot cannot poll every few seconds for six hours: it polls every ~22 s when chat is busy and slows down
when it is quiet, and it keeps 40% of the day for sending, which allows roughly 200 messages in a 6-hour stream. Replies
therefore appear up to about 20 seconds after a viewer's message. If that is too slow, the options are Google's quota-increase
request form (takes time) or the experimental stream transport, if the probe shows it works.

## Privacy and policy

- **30-day rule.** Google's developer policy requires deleting stored YouTube data (channel ids, names, chat text) after 30 days
  unless refreshed. The bot deletes YouTube-derived records older than 30 days at startup and every 6 hours. So viewer recognition
  for YouTube only "remembers" viewers seen in the last 30 days.
- **Disconnect completely:** stop the bot, then run the logout script. It revokes Google's access, deletes the encrypted
  credentials and deletes every stored YouTube record:

```powershell
& "$env:LOCALAPPDATA\stealthbot\venv\Scripts\python.exe" scripts\youtube_logout.py
```

  You can also revoke at <https://myaccount.google.com/permissions>.

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| "Sign-in failed: access_denied" | You declined, or the app is in Testing and the account is not a Test user (Audience → Test users). |
| "redirect_uri_mismatch" | The client is not of type **Desktop app**. Create a new Desktop client. |
| Sign-in worked, then `invalid_grant` days later | The consent screen is still in Testing (7-day expiry). Publish it (Step 2.4) and sign in again. |
| `Signed in, but the channel lookup failed` | The YouTube Data API v3 is not enabled in the project, or the account has no YouTube channel. |
| Status says "not authorised" | Run `youtube_login.py` again. |
| Status says "waiting for a live broadcast with chat" | Wrong video id, stream not live yet, or chat disabled. |
| Status says "quota exhausted" | The day's 10,000 units are used; it resumes at midnight Pacific. Check the usage in the Cloud console. |
| Bot connected but its messages do not appear | Slow mode / subscribers-only chat, or the account is too new. Try making the bot a moderator. |

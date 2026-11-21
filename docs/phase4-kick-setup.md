# Kick setup: what you need to do

About 30-40 minutes, once. **The bot side is built and tested against a fake Kick; nothing here has been tried against the real
Kick yet.** I cannot do these steps for you: they create an app in your Kick account and sign in. Kick's screen labels change from
time to time, so the names below may differ slightly; if something does not match, stop and tell me what you see.

**Costs:** Kick's developer API is not documented as paid. The tunnel (step 2) is the only place a paid plan could come up: check the
provider's current free-plan terms yourself before you create an account, because I have not verified them.

## Why a tunnel is needed
Kick sends chat messages to a **public web address** (a "webhook"). This bot runs only on your PC, so a small program called a
*tunnel* (ngrok, Cloudflare Tunnel, ...) gives your PC a public address and forwards Kick's calls to the bot's webhook listener
(`127.0.0.1:8787`). The listener accepts **only** Kick's calls (it checks Kick's digital signature) and serves nothing else: the
dashboard and the control API are never reachable through it. Details: `phase4-kick-research.md`.

## Step 1: the Kick developer app
1. Turn on **two-factor authentication** on the Kick account you will use (Kick requires it for developer tools).
2. In Kick: account **Settings -> Developer** (label may differ) -> **Create app**. Use the Kick account of the channel the bot works
   in (StealthModeGG), or ask me if you want a separate bot account.
3. **Redirect URL:** exactly `http://localhost:8789/callback` (Kick compares it character by character).
4. Note the **Client ID** and **Client Secret** (the secret may be shown only once).
5. Leave the webhook URL empty for now (step 3).
6. Read Kick's **Developer Terms and Conditions** that the app page links to. I could not read them; they may contain rules for bots.

## Step 2: a tunnel with a stable public address
Pick one. The webhook address is typed into Kick by hand, so an address that **changes every time** means editing Kick's settings
before every stream.

| Option | Address | Notes |
|---|---|---|
| **ngrok** free plan | one automatically assigned `...ngrok-free.app` domain that stays the same after restarts (you cannot choose the name; a reserved static domain is a paid feature) | needs an ngrok account; `ngrok http 8787`. **Free limits as ngrok's docs state them on 2026-11-21: 20,000 HTTP requests and 1 GB per month**; every Kick chat message is one request, so a busy chat can use it up (re-check on ngrok's own limits page and your dashboard's Usage page) |
| **Cloudflare quick tunnel** (`cloudflared tunnel --url http://localhost:8787`) | **changes each run** | no account; Cloudflare documents it as testing-only with no uptime guarantee; fine for the first test |

The tunnel program is a native `.exe`; Windows Smart App Control may block it. If it does, tell me, do **not** switch Smart App
Control off.

Start the tunnel and note its public address, e.g. `https://example.ngrok-free.app`. The webhook URL for Kick is that address plus
`/kick/webhook`.

## Step 3: tell the bot (and Kick)
1. In `backend\.env` (copy `backend\.env.example` first) add:
```
STEALTHBOT_KICK_ENABLED=true
STEALTHBOT_KICK_WEBHOOK_PUBLIC_URL=https://example.ngrok-free.app/kick/webhook
```
   (the second line is only shown in the dashboard; the real setting lives in Kick, next step)
2. In the Kick developer app: switch **Webhooks** on and paste the same URL (`.../kick/webhook`). Save.

## Step 4: sign in
```powershell
& "$env:LOCALAPPDATA\stealthbot\venv\Scripts\python.exe" scripts\kick_login.py --client-id YOUR_CLIENT_ID
```
You are asked for the secret (hidden). Your browser opens: sign in to Kick and approve. The terminal then prints the account and
channel and the permissions granted. Credentials are stored **encrypted with Windows DPAPI**, never in `.env`, the database or logs.
If it says a permission is missing, enable it in the Kick app and run the script again.

## Step 5: check, with the bot stopped
```powershell
& "$env:LOCALAPPDATA\stealthbot\venv\Scripts\python.exe" scripts\kick_check.py --probe-tunnel https://example.ngrok-free.app
```
It checks the sign-in, whether the channel is live, that Kick's public key can be read, which subscriptions exist, and that your
public address really reaches the bot's listener (`/kick/health`). **Please send me this output.**

## Step 6: go live (test first)
1. Start the bot (`scripts\start.bat`) and the tunnel. Open the dashboard (`scripts\open_dashboard.bat`): the **Kick** pill and card
   show the state. "connected" means signed in, listener running and subscribed.
2. For the first test turn **"Ask me before anything is sent"** on, so every message waits for your approval.
3. Write a message in your Kick chat from another account. The dashboard's Live chat should show it within seconds. If the Kick card
   says "NO event received yet" after two minutes, the tunnel or the webhook URL in Kick is wrong.
4. Approve the reply. Look in Kick chat: **under which name did the bot's message appear?** That is the bot's identity on Kick
   (not documented by Kick), and I need to know.
5. Optional one-off post from the command line (asks you to type YES):
```powershell
& "$env:LOCALAPPDATA\stealthbot\venv\Scripts\python.exe" scripts\kick_check.py --send-test "Hello chat, I'm StealthBot, an automated assistant."
```

## Settings you may want (`backend\.env`, all optional)
```
STEALTHBOT_KICK_MESSAGE_TYPE=bot          # "bot" (documented bot identity) or "user" (posts as you)
STEALTHBOT_KICK_EVENTS=chat.message.sent,channel.subscription.new,channel.subscription.gifts,kicks.gifted
STEALTHBOT_KICK_WEBHOOK_PORT=8787         # must match what the tunnel forwards to
STEALTHBOT_KICK_OAUTH_PORT=8789           # must match the redirect URL registered in Kick
STEALTHBOT_KICK_REPLY_THREADS=false       # reply as a threaded reply (try later)
```
Message limits for Kick (gap, per-window maximum, length 500) are edited in the dashboard: **Scheduler & limits**.

## Disconnect completely
Stop the bot, then:
```powershell
& "$env:LOCALAPPDATA\stealthbot\venv\Scripts\python.exe" scripts\kick_logout.py
```
It revokes the grant at Kick, deletes the encrypted credentials and deletes every stored record that came from Kick. Also switch
webhooks off in the Kick app.

## Troubleshooting
| Symptom | Likely cause and fix |
|---|---|
| Sign-in says `invalid_request` | The redirect URL in the Kick app is not exactly `http://localhost:8789/callback` |
| Sign-in says `port_in_use` | Something uses port 8789: close it, or change `STEALTHBOT_KICK_OAUTH_PORT` and the Kick redirect URL together |
| Dashboard: "not authorised ... kick_login" | Run `kick_login.py` again (Kick rejected the stored sign-in) |
| Dashboard: "the sign-in did not grant ..." | Enable the missing permission in the Kick app, then sign in again |
| Connected, but "NO event received yet" | Tunnel not running, wrong forwarding port, or the webhook URL in Kick is wrong or lacks `/kick/webhook` |
| Webhook listener problem: port in use | Another program uses 8787: change `STEALTHBOT_KICK_WEBHOOK_PORT` and the tunnel together |
| Replies are generated but never appear in chat | Missing `chat:write`, or Kick limits the account; the dashboard shows the error |

# Control panel (Phase 6) and runtime model switching

**Status (2026-11-21): built and tested** (79 automated tests for the dashboard and its API, plus a live run in a browser with the
real Qwen3 model: approval flow, every tab, saving forms, Restart and Stop). Kick is not built yet, so nothing in the panel shows it
until Phase 4.

## Open it

1. Start the bot with `scripts\start.bat` (it now runs `python -m app.run`, which is what lets the dashboard restart the bot).
2. Run `scripts\open_dashboard.bat`. It asks the running bot for a **one-time sign-in ticket** (valid 60 s, single use) and opens
   `http://127.0.0.1:8000/dashboard`. The real API key never appears in a URL or in your browser history; the page removes the
   ticket from the address bar at once and keeps the key only for that tab.
3. Without the script, open `http://127.0.0.1:8000/dashboard` and paste the key from `%LOCALAPPDATA%\stealthbot\data\api_token`.

## What is where (your spec's sections A to H)

| Spec section | Where | What it does |
|---|---|---|
| A Overview | **Live** tab, right column and header | Status pills; **CPU, RAM, GPU, VRAM** bars with GPU name, temperature and power (CPU/RAM through Windows, GPU through `nvidia-smi`); stream length; the bot's own memory; platforms with state and YouTube quota; session counters (messages received, replies sent, AI speed, queue) |
| B Live chat monitor | **Live** tab | Messages with platform, user, time, category, why one was not answered, the reply under it (personality, model, speed, delivery status). **Manual approval**: see below |
| C Personality manager | **Personalities** tab | A card per personality: on/off, description, **prompt editor**, cooldown, frequency, priority, **preview a reply** (nothing is sent), **reset the prompt to the default** |
| D AI configuration | **AI** tab | Installed model picker and backup model (as before), **context size, longest reply (tokens and words), temperature, generation timeout**, **Test AI generation** button |
| E Platform settings | **Live** tab, Emergency card + Platforms card | **Connect / Disconnect** per platform, connection state, YouTube channel, video id, live chat id, permission scope and quota. Signing an account in or out is done with the scripts (`youtube_login.py` / `youtube_logout.py`) because it needs you in a browser |
| F Scheduler settings | **Scheduler & limits** tab | Proactive on/off, quiet-chat time, gap, interval ranges per activity (general, gaming question, trivia), plus **per platform: gap between messages, maximum messages per window, window length, same-viewer cooldown, message length** |
| G Session history | **History & logs** tab | Sessions with start, length, status (a crash is shown), messages, replies, errors, AI speed (median and p95); connection and reconnection events |
| H Emergency controls | **Live** tab | **Pause** (instant, needs no AI), AI on/off, proactive on/off, **disconnect/connect a platform**, **Restart bot**, **Stop bot** |
| (extra) Log viewer | **History & logs** tab | The newest log entries with a level filter and auto-refresh; secrets are removed before display |

### Manual approval ("Ask me before anything is sent")
With the switch on, every message the bot composes (replies and bot-started messages; not answers to its own `!commands`) waits in a
card at the top of the Live tab with the viewer's message and the proposed text. You can **edit the text and approve**, or reject it.
Approval is checked at the moment you press the button: it is refused (and the message keeps waiting) while sending is paused, a
platform is disconnected or its message limit is reached. A message nobody decides about is dropped after 2 minutes. Every held,
approved, rejected or expired message is recorded in the database with that status. Off by default; `STEALTHBOT_APPROVAL_REQUIRED=true`
starts it on.

### What survives a restart
Saved in the database and re-applied at the next start (they win over `.env` until changed again): the model choice, personality
on/off, cooldown, frequency, priority, prompt and description, the AI generation settings, the message limits, and the proactive-chat
timings. **Not** saved (they return to their start value, like a pause button): Pause, AI on/off, proactive on/off, approval mode,
platform connect/disconnect.

## How the model switch stays safe
- The new model is **loaded and asked for a real reply first**, using the same context size and GPU settings as live replies. Only if
  that works does it become the primary. On any failure the old model stays active and the page says why. Chat replies are delayed
  while the swap runs, never lost.
- The old model is unloaded afterwards. If you switch to the model that was the backup, the old primary becomes the backup.
- The backup model is only checked for being installed (loading it would push the primary out of a 4 GB GPU).
- Installing a new model is not done from the page (large download): run `ollama pull NAME`, then "Reload model list".
- Changing the **context size** makes Ollama reload the model once (a slow first reply). On a 4 GB GPU keep it at 4096 or lower.

## Why there is no Node/Vite
Smart App Control on this PC blocks unsigned native programs, and a Node toolchain would add many. The panel is plain HTML and
JavaScript (`backend/app/dashboard/index.html`, `app.js`, `panels.js`, `style.css`) served by the bot itself. No build step.

## Security
- The page contains no data and no secrets. All data comes from the protected API (every route needs the key).
- Chat text and names are shown with `textContent` only. A test fails if any dashboard script uses `innerHTML`, `outerHTML`,
  `insertAdjacentHTML`, `document.write`, `eval` or string timers.
- Strict Content-Security-Policy (no inline script or style, nothing from other sites), `frame-ancestors 'none'`, `nosniff`,
  `no-store`. Only the four whitelisted files are served. The Host header must be `127.0.0.1` or `localhost`.
- Editing a personality prompt cannot remove the safety rules: they are added in code to every prompt.
- Every number you can edit has hard limits checked by the server, and an invalid update changes nothing (all or nothing).
- Stop and Restart work only when the bot was started with `scripts\start.bat`; otherwise the server answers 501 and says so.

## Not verified / limits
- Connect and Disconnect were tested on the simulator and with mocked platforms; the real YouTube/Kick connections are untested
  (Phase 3/4 evidence is still outstanding).
- GPU numbers need `nvidia-smi` (it ships with your driver); without it the panel says "No NVIDIA GPU readings available".
- The panel's JavaScript has no automated browser tests (no Node on this PC): the static checks run automatically, the behaviour was
  checked by hand in a browser. History is a table, not charts.
- A message held for approval is not counted against the platform's message limit until it is really sent.

## Found while testing it live
1. **The AI was about 7x slower than the Phase 1 benchmark on battery** (7.6 tokens/s; 52 on AC). Stream plugged in.
2. Bugs found and fixed in the page: long model names squeezed into one-letter columns; each reply shown twice; the 2-second refresh
   reset a model pick; the browser's own validation blocked ordinary timings such as 20 seconds (0.33 minutes).
3. The first reply after starting the bot is slower while the model loads.

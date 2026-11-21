# STEALTHBOT

Local, self-hosted AI chat companion for StealthModeGG's live streams (YouTube + Kick). All AI inference runs
on this laptop through [Ollama](https://ollama.com); there is no paid inference API.

**Status:** Phase 1 (hardware + model benchmark) done, Phase 2 (core engine + chat simulator) built and tested,
Phase 3 (YouTube) **implemented and tested against a fake Google, awaiting live verification** (needs your Google account:
`docs/phase3-youtube-setup.md`). The control panel (Phase 6) is complete (`docs/dashboard-and-models.md`: live chat with manual
approval, PC load, prompts, AI settings, limits, history, logs, stop/restart, model switching); Kick
is built and tested against a fake Kick, **awaiting live verification** (`docs/phase4-kick.md`; setup in
`docs/phase4-kick-setup.md`: Kick only offers webhooks, which need a tunnel). Proactive
engagement (the bot starts conversations when chat is quiet) is built and tested on the simulator (`docs/phase5-scheduler.md`).
The bot can follow a **watch list of streams** and, as a fail-safe, start with whichever one is live when you have not selected a stream
(dashboard tab Streams, `docs/stream-selection.md`; tested with fakes, YouTube side not yet run live).
Reliability testing is partly done (`docs/phase7-reliability.md`: failure injection against the real bot, memory and duplicate
checks; the real 3-6 hour runs still need your accounts). Gameplay awareness is a later phase. See `docs/` for the details and
measured results.

**Run with the charger plugged in.** Measured: on battery the AI makes 7.6 tokens/s, plugged in 52 (same PC, same power plan).

**Stack note:** the whole backend is pure Python (Starlette, sync SQLAlchemy in worker threads, dataclasses) because
Windows Smart App Control on this PC blocks unsigned native extensions such as Pydantic's. Details in
`docs/phase2-core-engine.md`. Do not add dependencies with compiled extensions without testing them first.

## Layout

```
backend/    FastAPI app, chat pipeline, AI layer, database, tests
scripts/    setup, start, health check, benchmark and the terminal chat simulator
docs/       phase documents and benchmark data
```

Runtime state (SQLite database, logs, API token, virtualenv) lives in `%LOCALAPPDATA%\stealthbot\`,
deliberately **outside** this OneDrive-synced folder.

## First-time setup (Windows PowerShell)

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
```

Creates the virtualenv at `%LOCALAPPDATA%\stealthbot\venv` and installs `backend\requirements.lock`.
Use `py` (not `python`): on this machine bare `python` resolves to the Microsoft Store stub.

Make sure Ollama is running and both models are pulled (see `docs/phase1-hardware-and-benchmark.md`):

```powershell
ollama list
```

## Run

```powershell
scripts\start.bat
```

Open the control panel (live chat with manual approval, PC load, personalities and prompts, AI settings, message limits,
scheduler, session history, logs, restart/stop), already signed in:

```powershell
scripts\open_dashboard.bat
```

Or chat with the bot as a fake viewer from a second terminal:

```powershell
& "$env:LOCALAPPDATA\stealthbot\venv\Scripts\python.exe" scripts\simulate_chat.py --user alice
```

Health probe: `scripts\health_check.bat`. The API listens on `127.0.0.1:8000` only and requires an
`X-API-Key` header (the token is created on first start in `%LOCALAPPDATA%\stealthbot\data\api_token`).

## Reliability tools

`scripts\chaos_soak.py` runs the real bot under injected failures (Ollama refused, frozen, slow, dropping connections, hard kill,
restart with re-delivered messages) and checks the database, log, recovery time and memory. `scripts\fault_proxy.py` and
`scripts\fake_ollama.py` are its parts. See `docs/phase7-reliability.md` for how to run it and what it found.

## Tests

```powershell
cd backend
& "$env:LOCALAPPDATA\stealthbot\venv\Scripts\python.exe" -m pytest          # fast, mocked Ollama
& "$env:LOCALAPPDATA\stealthbot\venv\Scripts\python.exe" -m pytest -m live  # real Ollama + models (uses the GPU)
```

## Configuration

Everything has a default in `backend/app/config.py`. Override with `STEALTHBOT_*` environment variables or
`backend/.env` (copy `backend/.env.example`). Never put credentials in `.env`.

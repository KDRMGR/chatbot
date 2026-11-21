# STEALTHBOT – Phase 2: Core bot engine and local chat simulator

Status (2026-11-20): **built and tested** (125 automated tests + 8 live tests + an end-to-end run against the real server and model).
Not yet done: platform connectors (Phases 3–4), proactive scheduler (Phase 5), dashboard (Phase 6), long-session reliability (Phase 7).

## 1. What it does

A viewer message goes through this pipeline (`backend/app/chat/processor.py`). One worker handles messages one at a time; producers
never block.

```
simulated viewer ─► sanitise ─► de-duplicate ─► viewer record ─► classify ─► store ─┬─► ignore (spam / abuse / injection / repeat)
(API or terminal)   (strip hidden   (platform      (new vs        (rules, no   (SQLite) │
                     characters)     message id)    returning)     LLM)                 └─► pause? ─► rate limits ─► choose reply source
                                                                                                        │
              command (!bot ...) ─► deterministic text ◄───────────────┤                       personality picked
              donation / membership ─► deterministic thanks ◄──────────┤                       (one shared model)
                                                                       ▼                              │
        rule-based fallback ◄── AI unavailable / output rejected ◄── Ollama (4B ► retry ► 1.7B) ◄──────┘
                 │                                                      │
                 └──────────────► output guard (links, commands, leaks, profanity, repeats, tone, length)
                                                   │
                                          store ─► adapter.send() ─► delivery status saved
```

| Area | Where | Notes |
|---|---|---|
| Config | `config.py` | Dataclasses + env loader. `STEALTHBOT_*` env vars or `backend/.env`. Loopback-only guard for Ollama and the API. |
| Chat core | `chat/` | `normalizer` (text sanitising), `filters` (classifier), `lexicon`, `output_guard`, `queue` (bounded queue + rate limiter), `context` (rolling session memory), `rules`, `processor` |
| AI | `ai/` | `ollama_client`, `model_manager` (retry, fallback, backoff, health, warm-up), `prompt_manager`, `response_generator` |
| Personalities | `personalities/` | 5 profiles (prompt files in `prompts/`), seeded into SQLite once, selection by category / cooldown / weight |
| Storage | `database/` | SQLite (WAL, 30 s busy timeout), all 8 spec tables, Alembic migration `0001` |
| Platforms | `platforms/` | `PlatformAdapter` contract + `SimulatorAdapter`. YouTube / Kick implement the same contract in Phases 3–4. |
| API | `api/`, `main.py` | Starlette, loopback only, token auth, Host-header check |

Categories implemented: greeting, question, gameplay suggestion, joke, general, spam, abuse, repeated, bot command, event
(donation / membership). Internal `proactive_*` categories exist for Phase 5 and for personality previews.

## 2. Important decision: a pure-Python stack, because of Windows Smart App Control

The brief specifies FastAPI, Pydantic Settings and async SQLAlchemy. **None of them can run on this laptop**, and it is a documented
technical reason to deviate (brief §12).

- **Evidence.** `HKLM:\SYSTEM\CurrentControlSet\Control\CI\Policy` → `VerifiedAndReputablePolicyState = 1` (Smart App Control **enforcing**).
  It blocks every unsigned native extension that pip installs: `pydantic_core`, SQLAlchemy 2.1's compiled modules and MarkupSafe's
  speedups all fail with *"DLL load failed ... An Application Control policy has blocked this file."* (all `NotSigned`). Pure-Python
  packages load fine.
- **Why FastAPI is out.** It requires Pydantic v2, whose core is a Rust extension with no pure-Python fallback. Pydantic v1 is pure
  Python but unsupported on Python 3.14 and unmaintained.
- **Why async SQLAlchemy is out.** Its asyncio layer requires `greenlet`, also a native extension.

| Brief | Used instead | Effect |
|---|---|---|
| Pydantic / Pydantic Settings | stdlib dataclasses + a ~60-line env loader + hand-written request validation | same behaviour, no auto-generated OpenAPI docs |
| FastAPI | **Starlette** (the framework FastAPI is built on) + uvicorn | same routes, same HTTP behaviour |
| SQLAlchemy async + aiosqlite | **SQLAlchemy 2.1 (pure-Python wheel)**, sync, run in worker threads via `asyncio.to_thread` | the event loop is never blocked; every DB call is one short transaction |

`scripts\setup.ps1` installs SQLAlchemy's `py3-none-any` wheel first (the default Windows wheel contains the blocked compiled modules).
The only compiled module left in the venv is MarkupSafe's optional speedup; it falls back to pure Python automatically.

**I did not touch Smart App Control, and recommend you do not either:** turning it off is permanent (re-enabling needs a Windows reset)
and lowers the machine's protection. If you ever decide to, the code can be ported back to FastAPI/Pydantic. **Heads-up for Phase 6:**
the same policy will probably block the unsigned native binaries in a normal Vite/esbuild/Rollup toolchain. We will need
WASM/pure-JS builds of those tools, and I'll verify that before writing any frontend.

## 3. Run it (Windows PowerShell)

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1      # once: venv in %LOCALAPPDATA%\stealthbot\venv
scripts\start.bat                                               # API on 127.0.0.1:8000
& "$env:LOCALAPPDATA\stealthbot\venv\Scripts\python.exe" scripts\simulate_chat.py --user alice   # second terminal
scripts\health_check.bat
```

Terminal commands: `/user NAME`, `/history`, `/status`, `/personalities`, `/pause`, `/resume`, `/ai on|off`, `/quit`.
API (all need the `X-API-Key` header except `/api/health`): `POST /api/simulate/message`, `GET /api/messages`, `GET /api/personalities`,
`POST /api/personalities/{name}/preview`, `GET /api/status`, `POST /api/control/pause|resume|ai`.
State lives in `%LOCALAPPDATA%\stealthbot\` (database, logs, `data\api_token`), outside OneDrive.

## 4. What was tested, and how

| Check | Result |
|---|---|
| Automated suite (`pytest`, Ollama mocked) | **125 passed**: normalizer 7, filters 23, output guard 19, queue/limiter 7, personalities + prompts 8, Ollama client 8, model manager 10, database 9, config 5, **pipeline end-to-end 19**, API security 10 |
| Live suite (`pytest -m live`, real Ollama + GPU) | **8 passed**: warm-up, 4B model 100% on GPU, five real replies pass the output guard (0.5–1.1 s, 52–54 tok/s), prompt-injection attempt neither leaked nor obeyed |
| Mutation check | Deliberately disabled viewer-text escaping: 3 tests failed (unit, prompt builder, full pipeline). Restored; suite green. |
| Real server + real model over HTTP | 13 scenarios: replies, spam/abuse/injection ignored (1–3 ms), `!bot`, donation, repeat, emergency pause, AI off → rule fallback, 4 personality previews. Median AI latency 751 ms, model 100% on GPU. |
| Clean rebuild | Deleted the venv and rebuilt it with `setup.ps1` from `requirements.lock`; `pip check` clean; 125 tests pass |

**Two bugs found only by running the real system** (now fixed, with regression tests):
1. *Questions went unanswered.* ChatBuddy is the only personality for greetings and questions; its 20 s cooldown made the bot ignore
   a viewer's question right after greeting someone. Greetings and questions now ignore personality cooldowns when nobody else fits.
2. *Shouting and emoji spam* (`OHHHH CAROL ... 🔥 🎯 🎲`) despite the prompt. Tone is now enforced in code: at most 2 emoji and no
   all-caps words of 5+ letters.

## 5. Security measures in place

Viewer text is sanitised on construction (zero-width, bidi-override and Unicode "tag" characters stripped), escaped and delimited
in the prompt (cannot close or forge a tag), and prompt-injection phrases are ignored before the model ever sees them. The safety rules
live in code, not in the editable personality prompts. The AI only produces text; the output guard blocks links, `!`/`/` commands,
`@mentions`, prompt-leak phrases, profanity and repeats (across all platforms), and trims length and tone. API: loopback bind, secret
token (constant-time compare), Host-header validation, input validation. Logs redact tokens/keys. No credentials are stored yet
(OAuth storage is a Phase 3 task). Ollama is only reachable on loopback, and the client refuses non-loopback hosts.

## 6. Known limitations and honest gaps

- **Only mocked failures so far.** Ollama outage, primary-model failure, GPU-placement failure and timeouts are tested with a fake
  Ollama; I did *not* stop the real Ollama service while the bot ran. That belongs in Phase 7.
- **Abuse list is deliberately tiny** (harassment phrases + profanity on output; no slurs enumerated). Extend via
  `STEALTHBOT_EXTRA_LEXICON_FILE`. It is a safety net, not moderation.
- **Reply quality is decent, not perfect.** The 4B model still invents game facts and sometimes speaks as the driver. The prompt tells it
  not to; further tuning is Phase 5 work.
- Rule-based fallback has only 2–4 templates per category (it is a safety net for outages, not a conversation partner).
- Platform character limits (YouTube 200, Kick 500) and rate-limit defaults are **unverified placeholders** until Phases 3–4 check the
  official docs.
- The project folder is in OneDrive; code is fine there, but `node_modules` (Phase 6) and any future `.git` would be a problem. Consider
  moving the project to a non-synced folder before Phase 6. No `git init` has been done for that reason.

## 7. Before Phase 3 (needs you)

Phase 3 (YouTube) needs actions only you can do: create a Google Cloud project, enable the YouTube Data API v3, configure the OAuth
consent screen and credentials, and later sign in with the Google account that owns the channel. I will list the exact steps and ask for
approval before touching any account or cloud resource.

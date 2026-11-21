# STEALTHBOT – Phase 7: reliability

**Status (2026-11-21): the part that can be tested on this PC without your accounts is done. The real 3-hour and 6-hour sessions,
a real internet drop and the real YouTube/Kick failures are NOT done** (they need your Google/Kick setup and hours of PC time).
Everything below was run against the real bot process with real failures injected; the numbers are measured, not estimated.
The project's automated tests pass (469 at the time of the last run). Raw reports: `docs/benchmarks/phase7-*.json`.

## Spec checklist

| Phase 7 task | State | Evidence |
|---|---|---|
| Test 3-hour session | **Done with the stand-in model (no GPU)**; not yet with the real model on a real platform | 180 minutes of the real bot at about 1 message/s with every failure injected, 22 of 22 checks passed (below) |
| Test 6-hour session | **Not done** | Needs the real platforms and a free PC; see "What is left" |
| Simulate internet disconnection | **Partly** | The bot-to-Ollama link was cut 4 ways (below). A real Wi-Fi drop on the YouTube side is only covered by the Phase 3 mocked tests; cutting your internet is yours to do |
| Simulate Ollama failure | **Done**, with the real Ollama too | Refused, dropped mid-request, frozen, very slow; hard kill of the bot |
| Simulate platform API failure | **Mocked only** (Phase 3) | Needs real YouTube/Kick to test for real |
| Test token expiration | **Mocked only** (Phase 3) | `invalid_grant` handling is tested with a fake Google; real expiry unverified |
| Test queue recovery | **Done** | 15x message bursts, queue full, stale messages, restart |
| Test memory usage | **Done** for 30 minutes; found and fixed a problem | Flat at 71-75 MB for 15 minutes at 5 msg/s after the fix |
| Verify no message duplication | **Done** | Database checks after hard kills, restarts and re-deliveries |

## The test rig (all in `scripts/`, standard library only, never imported by the bot)

- `fault_proxy.py`: sits between the bot and Ollama and can **refuse** connections, **reset** them mid-request, **hang** (accept
  and never answer) or **delay** them. It never touches the real Ollama process.
- `fake_ollama.py`: a stand-in Ollama that needs no GPU, so long runs do not use your graphics card.
- `chaos_soak.py`: starts the real bot (own temporary state folder), sends simulated viewers (greetings, questions, spam,
  prompt-injection attempts, duplicate message ids, quiet spells, 15x bursts), injects failures on a timeline, probes recovery
  every 2 s, samples memory and queue every 10 s, then checks the database and log and prints PASS/FAIL per check.

```powershell
python scripts\chaos_soak.py --minutes 25                                   # all faults, stand-in Ollama
python scripts\chaos_soak.py --minutes 10 --backend 127.0.0.1:11434 --light # all faults, REAL Ollama (uses the GPU)
python scripts\chaos_soak.py --minutes 15 --rate 5 --no-faults              # steady load, memory test
```

## Results

### Failures injected into a 25-minute run (stand-in Ollama), 22 of 22 checks passed

| Fault | What the bot did | AI replies again |
|---|---|---|
| Ollama refused (48 s) | Switched to prepared replies at once, backed off 5 s, 10 s, 20 s... | 8 s after it returned |
| Ollama frozen/hung (90 s) | First viewers waited up to ~53 s (30 s timeout, one retry), then prepared replies | 18 s |
| Ollama slow (2 s per call) plus a 25 s burst of 15x chat | Queue filled to 193; the oldest messages were dropped (233 of 764), the rest answered | 2 s |
| Ollama drops every connection mid-request (48 s) | Prepared replies | 8 s |
| **Bot hard-killed** (like a crash or power cut) | New process started 5 s later | First reply 0.9 s after it was up |
| Graceful restart, then the platform re-delivers 60 old message ids | All 60 recognised as duplicates, none answered again | n/a |

Normal operation in the same run: median 0.03 s, p95 0.1 s per viewer message (stand-in model).

Database after 3,328 requests, 3 bot processes, 1 crash: integrity check OK; **0 messages answered twice; 0 duplicated message
ids; 0 replies the client was told about but missing from the database; 0 replies left in an unknown state**; sessions recorded as
`crashed, ended, ended` (the hard kill was detected at the next start); statistics saved for all three sessions; 10 proactive
messages sent during the two quiet spells; **0 unhandled errors or tracebacks** in the log.

### The same failures with the REAL Ollama and real model (10 minutes, light load), 22 of 22 checks passed
Recoveries: refused 1.8 s, frozen 20.7 s, slow 0.5 s, dropped mid-request 15.6 s. Hard kill: first reply 8.2 s after restart
(the model has to be found again). Normal replies: median 0.8 s. Same database results (0 duplicates, crash recorded).

### The 3-hour run (2026-11-21, stand-in Ollama, all failures, 22 of 22 checks passed)
180 minutes with the real bot process: about 11,000 viewer messages (about 1 per second, with spam, prompt-injection attempts,
duplicates, quiet spells and one 15x burst), then the full fault list spread over the three hours: Ollama refused, frozen, slow
(with the burst), dropping connections; a hard kill; a graceful restart with 60 message ids re-delivered. Report:
`docs/benchmarks/phase7-soak-3h-fake-ollama.json`.

| | Result |
|---|---|
| Requests answered normally | 11,009 of 11,009 (the only HTTP errors were 8 during the two restarts, as expected) |
| Replies / ignored (spam, repeats, floods) / duplicates / no personality ready / dropped under the 15x burst | 2,606 / 7,098 / 594 / 603 / 100 |
| Recovery after each Ollama fault (AI answering again) | refused 2.1 s, frozen 14.4 s, slow 0.1 s, dropping connections 12.3 s; after the kill 5.0 s |
| Messages answered twice / stored twice / reported sent but missing / left in an unknown state | 0 / 0 / 0 / 0 |
| Re-delivered ids after the restart | all 60 recognised, none answered again |
| Database integrity, crash recorded, statistics saved for every session | OK / `crashed, ended, ended` / 3 of 3 |
| Unhandled errors or tracebacks in the log | 0 |
| Memory of the bot (private MB) | **71 at the start, 74 after 15 minutes, 78 after 75 minutes, 76-78 until the end of the three hours**; the only peak was 102 MB during the burst, which came back down |
| Proactive messages sent in the quiet spells | 11 |

What it does not prove: the model was the stand-in (fast, canned replies), there was no real platform, and the PC was on AC power
and otherwise idle. It shows the bot itself (pipeline, database, scheduler, queues, restart handling) stays stable for three hours
and does not grow; it says nothing yet about the real model on the GPU for three hours or real YouTube/Kick behaviour.

### Memory: a problem found and fixed
The first 30-minute steady run (8,900 messages) passed every check **but the memory trace was a sawtooth: 71 MB climbing to 321 MB,
dropping, climbing again.** Memory climbed about 0.35 MB/s until Python's garbage collector ran. I reproduced it in one process:
the objects that carry every database call's hand-off to a thread form reference cycles, and a manual `gc.collect()` freed about
9,000 objects and 21 MB at once. So it was **not a leak** (the pipeline alone grew only 67 to 73 MB over 8,000 messages), but peaks of
300 MB are a bad fit for a PC that is also running a game.

Fix: a full collection every 60 s (`gc_interval_s`, `backend/app/services/maintenance.py`). Result on the real bot process,
15 minutes at 5 msg/s: **71 MB at the start, 74 MB after 5 minutes, 75 MB after 15**, peak 93 MB. In the full failure run, memory
stayed at about 76-78 MB, with one peak of 126 MB during the 15x burst (the queue was full), which came back down.

## Gaps this work found, all fixed

1. **Stale answers after an outage.** The queue answers oldest-first, so after an AI stall the bot would have answered messages
   from minutes ago. A viewer message that waited more than `max_queue_age_s` (default 120 s) is now kept in the history but not
   answered; donations and memberships are still thanked, and proactive tasks that waited that long are dropped.
2. **Statistics lost in a crash.** Session statistics were only saved at a clean shutdown. They are now saved every
   `stats_flush_interval_s` (default 5 min); the crashed session in the test kept its numbers.
3. **Memory sawtooth** (above).
4. **Personality settings could not be edited.** Cooldown, frequency and priority now have an API
   (`POST /api/personalities/{name}/settings`, saved across restarts). This is also part of the Phase 6 personality management.
5. **Test-rig mistakes that looked like bot bugs** (worth knowing): this PC's virtual-environment `python.exe` is a small launcher
   that starts the real interpreter as a child, so the first runs measured a 1 MB stub; recovery probes repeated the same question
   and the bot's own spam filter (correctly) ignored them. Both fixed in the rig.

## Confirmed: the AI slowdown was the battery

On battery (Balanced plan) the model produced **7.6 tokens/s**; plugged in, the same model, the same plan and the same PC give
**52 tokens/s** (Phase 1 measured 53). **Stream with the charger plugged in.** Everything in this phase's real-Ollama run was on AC.

## Known limits (not hidden)

- **A frozen (not refused) Ollama makes the first few viewers wait up to about 55 s** (30 s per attempt, one retry) before prepared
  replies take over. On AC, where replies take about 1 s, `STEALTHBOT_GENERATION_TIMEOUT_S=12` would cut that. I did not change the
  default because on battery a reply can legitimately take 16 s.
- **In a raid the oldest waiting messages are dropped** (queue limit 200) and are not stored. Most raid messages are spam, repeats
  or flooding and are ignored by the filters anyway (about two thirds in the test).
- **A hard kill during a send can leave one reply stored as "pending"** (the platform may or may not have received it). The test
  saw none; at most one is possible, because messages are handled one at a time.
- Memory peaks at about 125 MB after a raid, about 75-80 MB otherwise. Measured over 30 minutes, not hours.
- The stand-in Ollama's replies are fast and canned; only the 10-minute real run used the real model, at a light load.

## What is left for Phase 7 (needs you)

1. **Google setup**, then the Phase 3 live checks.
2. A **3-hour and a 6-hour supervised run** in an unlisted stream with the real model, on AC power, with your game running (the
   3-hour stand-in run is already done, see above). I need your OK for the PC time, because it keeps the GPU busy for hours.
3. A **real internet drop** during that run (pull the cable/Wi-Fi for a minute): I cannot do this without cutting your connection.
4. **Real platform failures and token expiry** (quota exhaustion, `invalid_grant`) once the accounts are connected.

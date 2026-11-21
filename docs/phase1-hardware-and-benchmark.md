# STEALTHBOT – Phase 1: Hardware verification and local AI benchmark

**Status (2026-11-20): COMPLETE.** NVIDIA driver 621.24 installed, Ollama 0.40.16 running with both models, GPU acceleration
verified, 30-minute soak passed. **Chosen runtime: `qwen3:4b-instruct-2507-q4_K_M`, `num_ctx=4096`, all layers forced onto the
GPU (`num_gpu=99`), thinking off; fallback `qwen3:1.7b`; then rule-based replies.** Measured results are in §6.

## 1. What the laptop actually is

| Item | Spec said | Found | Verdict |
|---|---|---|---|
| Machine | – | ASUS Vivobook K6502HC | – |
| CPU | i9 **13th** gen | **Intel i9-11900H (11th gen)**, 8C/16T | Spec typo. Still plenty for Ollama. |
| RAM | 16 GB | 15.7 GB total, ~6.5 GB free (Chrome + Claude app running) | OK, but tight – see §3 |
| Disk | 512 GB SSD | 475 GB C:, **358 GB free** | OK |
| OS | Windows (verify) | Windows 11 Home Single Language, build 26200 | OK |
| NVIDIA GPU | RTX 3050 Laptop 4 GB | Present on PCI bus (`10DE:25A2`, GA107M-class) – **no driver installed** | **BLOCKER** |
| NVIDIA driver | 512.39 | **None bound to the GPU.** No `nvidia-smi`. Device shows as a generic "Display" with empty driver provider/INF/service. | **BLOCKER** |
| Intel iGPU | UHD | Intel UHD, driver 30.0.101.1371, OK – drives the display | OK |
| Python | 3.12+ | **3.14.3** via `py` launcher (real install) | OK – but see below |
| Node.js | needed (Phase 6) | **not installed** | Not needed until Phase 6 |
| Git | needed | 2.53.0 | OK |
| Ollama | needed | **not installed** | Expected |
| Power | – | AC connected, plan **Balanced** | Change to High performance before long runs |

### Things that differ from what the brief assumed

1. **The GPU has no driver at all**, so today Ollama could only run on CPU. The NVIDIA App (11.0.9) is installed, which is the easiest way to fix this. The 512.39 driver mentioned in the brief is not present now and would be too old anyway.
2. **Driver minimum for Ollama:** the official docs say NVIDIA driver **550+** on the [hardware page](https://docs.ollama.com/gpu) and **551.61+** on the [Windows page](https://docs.ollama.com/windows). The two pages disagree slightly; target **551.61 or newer** (any current Game Ready/Studio driver is far above it). An RTX 3050 (compute capability 8.6) is within the supported range.
3. **`python` on PATH is unreliable.** `WindowsApps\python.exe` (the Microsoft Store stub) comes before the real interpreter on PATH. Use **`py`** (e.g. `py -3 --version`) in every command and script. Disabling the "App execution alias" for Python is the permanent fix (a system setting – needs your OK).
4. **The project folder is inside OneDrive** (`...\OneDrive\Documents\chatbot`). That is risky for a SQLite database and rotating log files that are written constantly for 6 hours (sync locking and churn). Proposal for Phase 2: keep code here, but make the `data/` and `logs/` locations configurable and default them to `%LOCALAPPDATA%\stealthbot\`.
5. **Python 3.14** is newer than the brief's "3.12+" floor. I will test the whole dependency set (FastAPI, SQLAlchemy, pydantic, httpx) in a virtualenv at the start of Phase 2. If anything lacks 3.14 wheels, I'll install 3.12 side by side.

## 2. Model choice

| Model (Ollama tag) | Download | Use |
|---|---|---|
| `qwen3:1.7b` | 1.4 GB | **Primary candidate.** |
| `qwen3:4b-instruct-2507-q4_K_M` | 2.5 GB | Benchmark candidate / quality upgrade. |

Notes on tags (checked against the [Qwen3 tags page](https://ollama.com/library/qwen3/tags)):

- Plain `qwen3:4b` currently shares a hash with the **thinking** variant. Thinking models emit long reasoning before the answer, which wastes tokens and latency in live chat, and there are [reports](https://github.com/ollama/ollama/issues/12022) of `think: false` being ignored on it. So the benchmark uses the **instruct** tag `qwen3:4b-instruct-2507-q4_K_M`.
- `qwen3:1.7b` is the original hybrid model; the benchmark sends `think: false` and counts any `<think>` leakage as a failure.
- The 4B instruct tag advertises a 256K context; we cap `num_ctx` at 4096 so the KV cache stays small.

### Expected memory (estimates – to be replaced by measurements)

Derived from published model sizes and architecture (KV cache = layers × KV heads × head dim × 2 × 2 bytes/token, fp16). Runtime overhead is a rough allowance. The real answer comes from `ollama ps` during the benchmark.

| Model | Weights | KV cache @ 4K ctx | Overhead (rough) | Est. total | Fit in ~4 GB VRAM? |
|---|---|---|---|---|---|
| qwen3:1.7b (Q4_K_M) | 1.4 GB | ~0.45 GB | 0.3–0.5 GB | **~2.2–2.4 GB** | Yes, with headroom |
| qwen3:4b instruct (Q4_K_M) | 2.5 GB | ~0.6 GB | 0.4–0.6 GB | **~3.5–3.7 GB** | Borderline – may spill layers to CPU |

Windows also reserves some VRAM; because the Intel iGPU drives the display, the NVIDIA GPU should be mostly free, but we will measure rather than assume. System RAM: the Ollama runner adds roughly 1–3 GB on top of Windows and whatever else is open. With ~6.5 GB free right now, **close Chrome and other heavy apps during streams**. Intel shared graphics memory cannot be added to NVIDIA VRAM.

**Initial recommendation:** start with `qwen3:1.7b` at `num_ctx=4096`, one model loaded, one request at a time. Treat the 4B model as an upgrade only if it is fully (or almost fully) on the GPU **and** its latency is still acceptable.

## 3. Approvals needed (nothing below has been done yet)

These change the system, so I'm waiting for your go-ahead:

1. **Update the NVIDIA driver** (you click this one – it's a GUI install and may blank the screen briefly, then ask for a reboot): open the **NVIDIA App → Drivers**, install the latest **Studio** (preferred for long-run stability) or Game Ready driver. Then verify:
   ```powershell
   nvidia-smi
   ```
2. **Install Ollama** (installs per-user, no admin required): official installer from <https://ollama.com/download>, or
   ```powershell
   winget search ollama
   winget install -e --id Ollama.Ollama
   ```
   (confirm the exact id in the `search` output first).
3. **Download models** (about 4 GB total):
   ```powershell
   ollama pull qwen3:1.7b
   ollama pull qwen3:4b-instruct-2507-q4_K_M
   ```
4. **Set High-performance power plan** for benchmark/stream sessions (optional, reversible).
5. **Optional Ollama tuning**, applied only *after* the baseline benchmark so we can measure its effect: `OLLAMA_MAX_LOADED_MODELS=1`, `OLLAMA_NUM_PARALLEL=1`, `OLLAMA_KEEP_ALIVE=30m`, `OLLAMA_FLASH_ATTENTION=1`, `OLLAMA_KV_CACHE_TYPE=q8_0` (user-level environment variables; restart Ollama afterwards). `OLLAMA_HOST` must stay unset / loopback – Ollama must never be exposed.

## 4. Benchmark procedure

Tools already in this repo (both tested; the benchmark has been run end to end against `qwen3:1.7b` on CPU – see §6):

- `scripts\phase1_check.ps1` – read-only environment check (PASS/WARN/FAIL).
- `scripts\benchmark_ollama.py` – standard-library-only benchmark and soak test. Writes JSON to `docs\benchmarks\`.

```powershell
# 0. Re-check environment (read-only)
powershell -ExecutionPolicy Bypass -File scripts\phase1_check.ps1

# 1. Quick benchmark of both models (cold load, tok/s, latency, GPU placement, VRAM/RAM, think-leak, prompt-leak)
py scripts\benchmark_ollama.py --models qwen3:1.7b qwen3:4b-instruct-2507-q4_K_M --num-ctx 4096 --passes 2

# 2. Confirm where Ollama put the model while it is loaded (PROCESSOR column should say 100% GPU)
ollama ps

# 3. 30-minute inference soak on the chosen model
py scripts\benchmark_ollama.py --models qwen3:1.7b --passes 1 --soak-minutes 30
```

### Proposed pass criteria (my suggestion – adjust if you disagree)

| Metric | Target |
|---|---|
| GPU placement (`ollama ps` / `size_vram`) | ≥ 95% on GPU for the chosen model (else documented CPU/partial fallback) |
| Median reply latency | ≤ 3 s; p95 ≤ 6 s |
| Generation speed | ≥ 20 tok/s |
| Reply length | ≥ 90% of replies within 10–35 words |
| `<think>` leakage / empty replies | 0 |
| System-prompt leak on the injection canary | 0 |
| Soak (30 min) | 0 errors; latency drift < 25%; RAM growth < 0.5 GB; GPU temp < 85 °C |

A 3–6 hour thermal/stability run belongs to Phase 7; the 30-minute soak here is only the go/no-go gate.

## 5. Phase 1 exit checklist

- [x] NVIDIA driver ≥ 551.61 installed (621.24); `nvidia-smi` works and shows 4096 MiB
- [x] Ollama installed (0.40.16); `http://127.0.0.1:11434` answers; bound to loopback only (verified with `Get-NetTCPConnection`)
- [x] `qwen3:1.7b` pulled and benchmarked (CPU and GPU)
- [x] `qwen3:4b-instruct-2507-q4_K_M` pulled and benchmarked (auto placement and all-GPU)
- [x] GPU acceleration verified with `ollama ps` (100% GPU) and `nvidia-smi`
- [x] 30-minute soak passed on the chosen model (120 requests, 0 errors)
- [x] Results and chosen model/config recorded in `docs\benchmarks\` and in this file

Phase 1 was closed on 2026-11-20 and Phase 2 started (see `phase2-core-engine.md`).

## 6. Progress log

### 2026-11-20 – install and CPU smoke test

- Ollama 0.40.16 (package `Ollama.Ollama`) was already on disk when I went to install it (installed 08:27, after my first inspection), so no reinstall was done. Installed per-user in `%LOCALAPPDATA%\Programs\Ollama`. Server listens on `127.0.0.1:11434` only; no `OLLAMA_*` environment variables are set. Shells opened before the install may not have `ollama` on PATH – open a new terminal, or use the full path.
- Pulled `qwen3:1.7b` (1.4 GB) and `qwen3:4b-instruct-2507-q4_K_M` (2.5 GB); `ollama list` shows both; disk free afterwards 353.9 GB.
- NVIDIA driver: **still not installed** at the time of this run (`nvidia-smi` not found).

**CPU-only baseline, `qwen3:1.7b`, `num_ctx=4096`, 1 pass × 10 prompts** (raw data: `docs\benchmarks\benchmark_20261120_083428.json`). This is the CPU fallback, *not* the GPU result:

| Metric | Result | Proposed target | |
|---|---|---|---|
| Placement | 0% on GPU (1799 MB model, all in system RAM) | ≥ 95% GPU | expected – no driver |
| Cold load | 1.6 s | – | |
| Generation speed | 27.0 tok/s median | ≥ 20 tok/s | met on CPU |
| Reply latency | 0.95 s median, 1.36 s p95 | ≤ 3 s / ≤ 6 s | met |
| Reply length | median 15 words; 1 of 10 outside 10–35 (a 6-word reply) | ≥ 90% in range | met |
| `<think>` leaks / empty replies | 0 / 0 | 0 | met |
| System-prompt leak on injection canary | 0 | 0 | met |
| Errors | 0 | 0 | met |
| System RAM | 9.91 → 11.58 GB used (+1.7 GB) | – | |

Caveats – read before trusting these numbers:

- Only 10 requests, one pass, on a machine with Chrome and other apps open. It validates the script and shows CPU fallback is usable; it is not the 30-minute soak.
- **Quality is mediocre, not just short.** Several replies are generic or slightly off-prompt (e.g. the "playful roast" prompt got praise, not a roast; the "abuse" prompt got a vague deflection). That is a prompt-engineering and model-size question for Phase 2/5, and a reason to compare against the 4B model with identical prompts. Speed alone is not the deciding factor.
- The `ollama RSS` figure (171 MB) printed by the script is **unreliable** (memory-mapped model weights don't show in the working set). Trust the system-RAM delta instead.
- The 4B model has not been benchmarked yet.

### 2026-11-20 – driver installed: GPU benchmarks and soak

Driver 621.24 (CUDA 13.5), RTX 3050 Laptop GPU, 4096 MiB (3962 MiB free at idle: the Intel iGPU drives the display), 50 W power limit.

**Benchmarks** (`num_ctx=4096`, thinking off, 10 prompts × 1–2 passes; raw data in `docs\benchmarks\`):

| | 1.7B on CPU (no driver) | 1.7B on GPU | 4B, Ollama auto-placement | **4B, all layers on GPU** |
|---|---|---|---|---|
| Placement (`ollama ps`) | 0% GPU | 100% (1628 MB) | **66% GPU / 34% CPU** | **100% (3033 MB)** |
| Generation | 27.0 tok/s | 110.4 tok/s | 27.8 tok/s | **53.4 tok/s** |
| Reply latency median / p95 | 0.95 / 1.36 s | 0.25 / 0.32 s | 1.31 / 1.57 s | **0.69 / 0.75 s** |
| Median words; outside 10–35 | 15; 1 of 10 | 14; 1 of 20 | 19; 0 of 20 | 22; 0 of 20 |
| VRAM in use | – | 1.7 GB | 2.3 GB | 3.1 GB (~1 GB headroom) |
| `<think>` leaks / prompt leaks / errors | 0 / 0 / 0 | 0 / 0 / 0 | 0 / 0 / 0 | 0 / 0 / 0 |

**30-minute soak** (4B, all layers on GPU, one request every 15 s, tray-managed Ollama server): **120 requests, 0 errors.**
Latency median 1.00 s, p95 1.18 s, max 1.52 s; first-10% median 0.94 s vs last-10% 0.93 s (**no drift**). 51.4 tok/s median.
System RAM 7.82 → 7.42 GB (min 7.41, max 7.82: **no growth**). VRAM constant at 3115 MB. GPU temperature max **65.8 °C**.
(An earlier 22-minute run on a hand-started server, 90 requests, also had 0 errors; it was cut short by a reboot and is kept as
`soak_20261120_085318_PARTIAL_...jsonl`.) Every proposed pass criterion in §4 is met. A 3–6 hour run remains a Phase 7 task.

**Findings and gotchas (all measured, none assumed):**

1. **Ollama detects GPUs only when its server starts.** The server that was running before the driver existed saw CPU only (log line
   `inference compute id=cpu`). After installing a GPU driver, restart Ollama (or reboot).
2. **Auto-placement left the 4B model 34% on the CPU even though 1.7 GB of VRAM was free.** The server log explains it: Ollama wants
   1 GiB of free VRAM and was 751 MiB short. Forcing `num_gpu: 99` per request fits all 37 layers using 3.1 GB and doubles the speed.
   *Risk:* if another application takes VRAM, forced placement could fail. The Phase 2 model manager therefore retries once with
   automatic placement, then falls back to the 1.7B model, then to rule-based replies.
3. **The first-ever model load is slow** (28 s load time, 58 s for the first request: one-time CUDA setup). Later cold loads took
   3–11 s. The app warms the model at startup and uses a long timeout for the first request after an idle period.
4. **The Ollama tray app would not start its server while a stray elevated `ollama.exe` (not mine) was running** (inferred cause;
   unproven). A reboot cleared it, and Ollama now autostarts from the Startup folder and brings up the server by itself.
5. **Model quality notes.** The 4B model is clearly more fluent and always lands in the 10–35 word range, but both models invent game
   facts (e.g. a made-up car name) and sometimes speak as the driver. Phase 2 therefore tells the model not to state facts it is unsure
   of, and enforces tone (≤ 2 emoji, no SHOUTING words) in code, not just in the prompt.
6. The `ollama RSS` number printed by the benchmark script is unreliable (memory-mapped weights); use the system-RAM figures.

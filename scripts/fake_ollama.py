"""A stand-in for Ollama's HTTP API for reliability tests: no GPU, no model, instant start. Replies are generated from
word lists so every one is different (the bot's repeat guard rejects near-identical replies, which would make a soak
test measure the guard instead of the pipeline).

    python scripts/fake_ollama.py --port 11600 --latency-ms 40 --models qwen3:4b-instruct-2507-q4_K_M qwen3:1.7b

Implements only what the bot calls: GET /api/version, /api/tags, /api/ps and POST /api/chat, /api/generate.
Standard library only. Loopback only. Development tool: the bot itself never imports this.
"""
from __future__ import annotations

import argparse
import json
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

OPENERS = ["Honestly", "Wow", "Okay chat", "Not gonna lie", "Respect", "Big mood", "Fair point", "Ha", "Right on", "Love that"]
SUBJECTS = ["that driving", "this heist plan", "the police chase", "the skyline view", "that jump", "the soundtrack",
            "the car choice", "that getaway", "the city lights", "the cockpit view", "this mission", "the traffic chaos"]
ENDINGS = ["looks seriously smooth today", "deserves a replay right now", "has chat hyped for the next move",
           "is how legends get made", "proves why we all stay for this", "feels like a movie scene",
           "keeps getting better every minute", "makes this stream worth the wait", "is exactly the kind of chaos we love"]
TAILS = ["What do you think?", "Tell me your favourite part.", "Who is with me?", "Drop your best guess below.", "Let us see what happens next!"]


class Handler(BaseHTTPRequestHandler):
    models: list[str] = []
    latency_s = 0.0
    rng = random.Random()
    lock = threading.Lock()
    calls = 0

    def log_message(self, *args) -> None:  # noqa: ANN002 - keep the console quiet
        pass

    def _send(self, status: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/api/version":
            self._send(200, {"version": "fake-0.0"})
        elif self.path == "/api/tags":
            self._send(200, {"models": [{"name": m} for m in self.models]})
        elif self.path == "/api/ps":
            self._send(200, {"models": [{"name": self.models[0], "size": 100, "size_vram": 100}]})
        else:
            self._send(404, {"error": "unknown"})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/api/generate":  # unload
            self._send(200, {})
            return
        if self.path != "/api/chat":
            self._send(404, {"error": "unknown"})
            return
        if body.get("model") not in self.models:
            self._send(404, {"error": "model not found"})
            return
        with Handler.lock:
            Handler.calls += 1
            text = f"{self.rng.choice(OPENERS)}, {self.rng.choice(SUBJECTS)} {self.rng.choice(ENDINGS)}. {self.rng.choice(TAILS)}"
        if (body.get("options") or {}).get("num_predict") == 1:
            text = "hi"
        elif self.latency_s:
            time.sleep(self.latency_s * self.rng.uniform(0.5, 1.5))
        self._send(200, {"message": {"role": "assistant", "content": text}, "eval_count": 24,
                         "eval_duration": 400_000_000, "done_reason": "stop"})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=11600)
    parser.add_argument("--latency-ms", type=float, default=40.0)
    parser.add_argument("--models", nargs="+", default=["qwen3:4b-instruct-2507-q4_K_M", "qwen3:1.7b"])
    args = parser.parse_args()
    Handler.models, Handler.latency_s = args.models, args.latency_ms / 1000
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"fake ollama on 127.0.0.1:{args.port} (models: {args.models})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

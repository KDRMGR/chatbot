#!/usr/bin/env python3
"""Interactive terminal chat against the running STEALTHBOT API (simulator platform).

Start the API first (scripts\\start.bat), then run with the project's venv:
    %LOCALAPPDATA%\\stealthbot\\venv\\Scripts\\python.exe scripts\\simulate_chat.py --user alice

Type a message to send it as the current viewer. Commands:
  /user NAME    switch viewer          /history   recent chat + bot replies
  /pause        emergency-stop sending /resume    resume sending
  /ai on|off    toggle AI (rule-based only when off)
  /status       model / queue / metrics    /personalities    list personalities
  /quit
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx


def read_token() -> str:
    if os.environ.get("STEALTHBOT_API_TOKEN"):
        return os.environ["STEALTHBOT_API_TOKEN"]
    state = Path(os.environ.get("STEALTHBOT_STATE_DIR") or Path(os.environ.get("LOCALAPPDATA", Path.home())) / "stealthbot")
    token_file = state / "data" / "api_token"
    if not token_file.exists():
        sys.exit(f"API token not found at {token_file}. Start the API once (scripts\\start.bat) to create it.")
    return token_file.read_text(encoding="utf-8").strip()


def show_result(r: dict) -> None:
    head = f"[{r['category'] or '-'}] {r['action']}" + (f" ({r['reason']})" if r.get("reason") else "")
    if r.get("viewer_is_new"):
        head += "  *new viewer*"
    print(head)
    if r.get("reply"):
        meta = "/".join(str(x) for x in (r.get("personality"), r.get("source"), r.get("model")) if x)
        lat = f" {r['latency_ms']} ms" if r.get("latency_ms") else ""
        print(f"  bot [{meta}{lat}]: {r['reply']}")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # emoji on a cp1252 console
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--user", default="viewer1")
    args = ap.parse_args()

    client = httpx.Client(base_url=args.url, headers={"X-API-Key": read_token()}, timeout=120, trust_env=False)
    try:
        health = client.get("/api/health").json()
    except httpx.HTTPError as exc:
        sys.exit(f"Cannot reach the API at {args.url}: {exc}")
    print(f"Connected. Ollama up: {health['ollama_up']}, model: {health['active_model']}. Type /quit to exit.")

    user = args.user
    while True:
        try:
            line = input(f"{user}> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line:
            continue
        if line.startswith("/"):
            cmd, _, arg = line[1:].partition(" ")
            if cmd == "quit":
                return
            if cmd == "user" and arg:
                user = arg.strip()
            elif cmd in ("pause", "resume"):
                print(client.post(f"/api/control/{cmd}").json())
            elif cmd == "ai" and arg in ("on", "off"):
                print(client.post("/api/control/ai", json={"enabled": arg == "on"}).json())
            elif cmd == "status":
                print(json.dumps(client.get("/api/status").json(), indent=2))
            elif cmd == "history":
                for row in client.get("/api/messages", params={"limit": 15}).json():
                    who = row["username"] or "?"
                    print(f"  {who}: {row['message']}  <{row['category']}>")
            elif cmd == "personalities":
                for p in client.get("/api/personalities").json():
                    print(f"  {p['name']:<12} enabled={p['enabled']} cooldown={p['cooldown_seconds']}s ready_in={p['ready_in_s']}s")
            else:
                print("unknown command")
            continue
        resp = client.post("/api/simulate/message", json={"username": user, "message": line})
        if resp.status_code != 200:
            print(f"error {resp.status_code}: {resp.text}")
            continue
        show_result(resp.json())


if __name__ == "__main__":
    main()

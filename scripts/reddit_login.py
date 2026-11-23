#!/usr/bin/env python3
"""Store your Reddit app's id and secret so GUY can read what Reddit says about your stream's topic. Run this yourself.

Reddit blocks anonymous scripts, but its official API is free for personal use with an app login that reads PUBLIC posts only:
  1. Sign in at reddit.com, open https://www.reddit.com/prefs/apps and press "create another app...".
  2. Name: anything (for example guy-bot). Type: **script**. Redirect uri: http://localhost (it is not used).
  3. Create it. The short text under the app's name is the client id; "secret" is the client secret.
  4. Run:  %LOCALAPPDATA%\\stealthbot\\venv\\Scripts\\python.exe scripts\\reddit_login.py
     (the secret is asked for with hidden input; or use --secret-file C:\\path\\file.txt whose first line is the secret)
  5. Add reddit to STEALTHBOT_SOCIAL_SOURCES in backend\\.env (for example wikipedia-free: mastodon,trends,reddit) and restart the bot.

It only asks Reddit for an app-only token (no account password is ever used) and stores the id and secret encrypted with Windows DPAPI in
%LOCALAPPDATA%\\stealthbot\\data. --check proves the login works; --forget deletes it.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import getpass
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.config import get_settings  # noqa: E402
from app.knowledge.social import REDDIT_KEY  # noqa: E402
from app.security.credential_store import CredentialStore  # noqa: E402


async def check(client_id: str, secret: str, contact: str) -> int:
    basic = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
    async with httpx.AsyncClient(trust_env=False, timeout=15, headers={"User-Agent": f"StealthBot/1.0 (local stream chat assistant; {contact})"}) as http:
        resp = await http.post("https://www.reddit.com/api/v1/access_token", data={"grant_type": "client_credentials"}, headers={"Authorization": f"Basic {basic}"})
    if resp.status_code == 200 and resp.json().get("access_token"):
        print("Reddit accepted the login (a read-only app token was issued).")
        return 0
    print(f"Reddit refused the login (HTTP {resp.status_code}). Check the client id (under the app's name) and the secret.")
    return 1


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--client-id")
    ap.add_argument("--secret-file", help="a text file whose first line is the client secret")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--forget", action="store_true")
    args = ap.parse_args()
    settings = get_settings()
    settings.ensure_dirs()
    assert settings.data_dir is not None
    store = CredentialStore(settings.data_dir)
    contact = settings.topic_contact or "https://github.com/KDRMGR/chatbot"
    if args.forget:
        print("The stored Reddit login was deleted." if store.delete(REDDIT_KEY) else "There was no stored Reddit login.")
        return 0
    if args.check and not (args.client_id or args.secret_file):
        saved = store.load(REDDIT_KEY) or {}
        return await check(str(saved.get("client_id", "")), str(saved.get("client_secret", "")), contact) if saved else print("Nothing is stored yet.") or 1
    client_id = (args.client_id or input("Reddit client id (the short text under the app's name): ")).strip()
    if args.secret_file:
        lines = Path(args.secret_file).read_text(encoding="utf-8").splitlines()
        secret = lines[0].strip() if lines else ""
    else:
        secret = getpass.getpass("Reddit client secret (input is hidden): ").strip()
    if not client_id or not secret:
        print("Both the id and the secret are needed: nothing was stored.")
        return 1
    if await check(client_id, secret, contact) != 0:
        return 1
    store.save(REDDIT_KEY, {"client_id": client_id, "client_secret": secret})
    print("Stored (encrypted for this Windows user). Add reddit to STEALTHBOT_SOCIAL_SOURCES and restart the bot.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

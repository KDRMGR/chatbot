#!/usr/bin/env python3
"""Sign STEALTHBOT in to YouTube. Run this yourself; YOU sign in and approve in your own browser.

First time (import the OAuth client you downloaded from Google Cloud, then sign in):
    %LOCALAPPDATA%\\stealthbot\\venv\\Scripts\\python.exe scripts\\youtube_login.py --client-json "C:\\path\\client_secret_xxx.json"

Later (e.g. Google says the sign-in expired):
    %LOCALAPPDATA%\\stealthbot\\venv\\Scripts\\python.exe scripts\\youtube_login.py

The client id/secret and the tokens are stored encrypted with Windows DPAPI in %LOCALAPPDATA%\\stealthbot\\data,
never in the repository, .env, the database or the logs. Sign in with the Google account the BOT should post as.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import httpx  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.platforms.youtube.api import YouTubeApi, YouTubeApiError  # noqa: E402
from app.platforms.youtube.credentials import YouTubeAuth  # noqa: E402
from app.platforms.youtube.oauth import ClientConfig, OAuthError, run_loopback_login  # noqa: E402
from app.platforms.youtube.quota import QuotaTracker  # noqa: E402
from app.security.credential_store import CredentialError, CredentialStore  # noqa: E402

HINTS = {
    "access_denied": "You declined (or the account is not allowed). If the app is in 'Testing', add this Google account under Audience > Test users.",
    "redirect_uri_mismatch": "The OAuth client must be of type 'Desktop app'. Create a new Desktop client and import its JSON.",
    "invalid_client": "Google does not recognise this client id/secret. Re-import the client JSON (--client-json).",
    "timeout": "No sign-in arrived within 5 minutes. Run the script again.",
    "disallowed_useragent": "Open the printed address in a normal browser (Chrome/Edge), not an embedded window.",
}


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--client-json", type=Path, help="client_secret_*.json downloaded from Google Cloud (first time only)")
    ap.add_argument("--client-id", help="alternative to --client-json: the Client ID (you are then asked for the secret, hidden)")
    ap.add_argument("--login-hint", help="pre-select this Google account (an email address)")
    args = ap.parse_args()

    settings = get_settings()
    settings.ensure_dirs()
    assert settings.data_dir is not None
    async with httpx.AsyncClient(trust_env=False, timeout=20.0) as http:
        auth = YouTubeAuth(CredentialStore(settings.data_dir), http)
        try:
            if args.client_json or args.client_id:
                if args.client_json:
                    client = ClientConfig.from_json(args.client_json.read_text(encoding="utf-8"))
                else:
                    secret = getpass.getpass("Client secret (input hidden; press Enter if there is none): ").strip()
                    client = ClientConfig(args.client_id.strip(), secret or None)
                auth.save_client(client)
                print(f"Stored the OAuth client (id {client.client_id[:12]}...) encrypted.")
                if args.client_json:
                    print("You can now delete the downloaded client_secret_*.json file.\n")
            else:
                client = auth.load_client()
                if client is None:
                    print("No OAuth client stored yet. Run again with --client-json <file>.")
                    return 2
        except (OSError, ValueError, CredentialError) as exc:
            print(f"Could not read the client JSON: {exc}")
            return 2

        try:
            tokens = await run_loopback_login(http, client, login_hint=args.login_hint)
        except OAuthError as exc:
            print(f"\nSign-in failed: {exc.error}. {HINTS.get(exc.error, exc.description)}")
            return 1
        auth.save_login(client, tokens)
        print("\nSigned in. Verifying with YouTube (costs 1 quota unit)...")
        quota = QuotaTracker(settings.youtube_daily_quota)
        api = YouTubeApi(auth, quota)
        try:
            channel = await api.own_channel()
        except YouTubeApiError as exc:
            print(f"Signed in, but the channel lookup failed: {exc}")
            print("Is the YouTube Data API v3 enabled in your Google Cloud project, and does this account have a YouTube channel?")
            return 1
        finally:
            await api.aclose()
        print(f"Authorised as YouTube channel: {channel['title']}  ({channel['id']})")
        print("\nThat is the account the bot will post as. Next steps are in docs\\phase3-youtube-setup.md.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

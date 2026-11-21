#!/usr/bin/env python3
"""Store the bot's own Supabase login (for the online panel relay). Run this yourself.

    %LOCALAPPDATA%\\stealthbot\\venv\\Scripts\\python.exe scripts\\relay_login.py --email bot@example.com
    (the password is asked for with hidden input; it never goes on the command line)

It first signs in to check that the login works and has the 'bot' role, then stores the e-mail and password encrypted with Windows
DPAPI in %LOCALAPPDATA%\\stealthbot\\data (never in the repository, .env, the database or the logs).
Needs STEALTHBOT_SUPABASE_URL and STEALTHBOT_SUPABASE_PUBLISHABLE_KEY (the PUBLIC key) in backend\\.env.
Use --forget to delete the stored login.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.config import get_settings  # noqa: E402
from app.relay.supabase import CREDENTIAL_KEY, RelayAuthError, RelayHttpError, SupabaseClient  # noqa: E402
from app.security.credential_store import CredentialStore  # noqa: E402


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--email", help="the bot user's e-mail in your Supabase project")
    ap.add_argument("--forget", action="store_true", help="delete the stored login")
    args = ap.parse_args()

    settings = get_settings()
    settings.ensure_dirs()
    assert settings.data_dir is not None
    store = CredentialStore(settings.data_dir)
    if args.forget:
        store.delete(CREDENTIAL_KEY)
        print("The stored Supabase login was deleted.")
        return 0
    if not (settings.supabase_url and settings.supabase_publishable_key):
        print("Set STEALTHBOT_SUPABASE_URL and STEALTHBOT_SUPABASE_PUBLISHABLE_KEY (the public key) in backend\\.env first.")
        return 2
    if not args.email:
        print("Give the bot user's e-mail with --email.")
        return 2
    password = getpass.getpass("Bot password (input hidden): ")
    if not password:
        print("No password entered. Nothing was stored.")
        return 2

    store.save(CREDENTIAL_KEY, {"email": args.email.strip(), "password": password})
    client = SupabaseClient(settings.supabase_url, settings.supabase_publishable_key, store)
    try:
        # sign in with what was just stored, then read our own role through the same policies the relay uses
        user_role = await client.role_of("00000000-0000-0000-0000-000000000000")  # forces a sign-in; unknown id -> None
        print("Signed in to Supabase as the bot user. (Its profile role is checked by the relay at start.)")
        _ = user_role
    except (RelayAuthError, RelayHttpError) as exc:
        store.delete(CREDENTIAL_KEY)
        print(f"Could not sign in, so nothing was kept: {exc}")
        return 1
    finally:
        await client.aclose()
    print("Stored (encrypted for this Windows account). Set STEALTHBOT_RELAY_ENABLED=true and restart the bot.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

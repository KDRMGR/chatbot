#!/usr/bin/env python3
"""Sign STEALTHBOT in to Kick. Run this yourself; YOU sign in and approve in your own browser.

First time (the Client ID and Client Secret come from your Kick developer app, see docs\\phase4-kick-setup.md):
    %LOCALAPPDATA%\\stealthbot\\venv\\Scripts\\python.exe scripts\\kick_login.py --client-id YOUR_CLIENT_ID
    (you are then asked for the secret with hidden input; it never goes on the command line)

Later (the sign-in expired, or you changed the app's permissions):
    %LOCALAPPDATA%\\stealthbot\\venv\\Scripts\\python.exe scripts\\kick_login.py

The client id/secret and the tokens are stored encrypted with Windows DPAPI in %LOCALAPPDATA%\\stealthbot\\data,
never in the repository, .env, the database or the logs. Sign in with the Kick account of the channel the bot should work in.
The app's redirect URI in Kick's developer settings must be exactly  http://localhost:8789/callback  (or your kick_oauth_port).
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
from app.platforms.kick.api import KickApi, KickApiError  # noqa: E402
from app.platforms.kick.credentials import KickAuth  # noqa: E402
from app.platforms.kick.oauth import SCOPES, KickOAuthError, redirect_uri_for, run_loopback_login  # noqa: E402
from app.platforms.youtube.oauth import ClientConfig  # noqa: E402
from app.security.credential_store import CredentialError, CredentialStore  # noqa: E402

HINTS = {
    "access_denied": "You declined the permissions. Run the script again and approve them.",
    "port_in_use": "Another program is using that port. Close it, or set STEALTHBOT_KICK_OAUTH_PORT to a free port and register the matching redirect URI in Kick.",
    "timeout": "No sign-in arrived within 5 minutes. Run the script again.",
    "missing_client_secret": "Kick requires the client secret. Run again with --client-id and enter the secret when asked.",
    "invalid_client": "Kick does not recognise this client id/secret. Check them in your Kick developer app.",
    "invalid_request": "Kick rejected the request. The most common cause is a redirect URI that does not match the one saved in the Kick app EXACTLY.",
}


def read_secret_file(path: str) -> str:
    """The secret from a file that holds ONLY the secret. Tolerates a BOM, UTF-16 (PowerShell's ``>``), CRLF and quotes.
    Error messages never contain the file's text."""
    raw = Path(path).read_bytes()
    text = raw.decode("utf-16") if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else raw.decode("utf-8-sig")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) != 1:
        raise ValueError(f"the file must contain only the secret on one line, but it has {len(lines)} non-empty lines")
    secret = lines[0]
    if len(secret) >= 2 and secret[0] == secret[-1] and secret[0] in "\"'":
        secret = secret[1:-1].strip()
    if not secret or any(ch.isspace() for ch in secret):
        raise ValueError("the line is empty or contains spaces (it should be the secret and nothing else, e.g. no 'secret=' label)")
    return secret


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--client-id", help="your Kick app's Client ID (first time only; the secret is asked for, hidden)")
    ap.add_argument("--no-browser", action="store_true",
                    help="print the sign-in address but do not open any browser (open it yourself in a private window or "
                         "another browser, signed in as the account the bot should work for)")
    ap.add_argument("--secret-file", metavar="PATH", help="read the secret from this file (one line, nothing else) instead of the hidden prompt; delete the file afterwards")
    ap.add_argument("--port", type=int, help="sign-in redirect port (default: STEALTHBOT_KICK_OAUTH_PORT, 8789)")
    args = ap.parse_args()

    settings = get_settings()
    settings.ensure_dirs()
    assert settings.data_dir is not None
    port = args.port or settings.kick_oauth_port
    async with httpx.AsyncClient(trust_env=False, timeout=20.0) as http:
        auth = KickAuth(CredentialStore(settings.data_dir), http)
        try:
            if args.client_id:
                if args.secret_file:
                    try:
                        secret = read_secret_file(args.secret_file)
                    except (OSError, ValueError) as exc:
                        print(f"Could not use the secret file: {exc}. Nothing was stored.")
                        return 2
                else:
                    secret = getpass.getpass("Client secret (input hidden): ").strip()
                if not secret:
                    print("Kick needs the client secret. Nothing was stored.")
                    return 2
                print(f"Received a client secret of {len(secret)} characters (the value is not shown).")
                client = ClientConfig(args.client_id.strip(), secret)
            else:
                client = auth.load_client()
                if client is None:
                    print("No Kick client stored yet. Run again with --client-id <id>.")
                    return 2
        except (OSError, ValueError, CredentialError) as exc:
            print(f"Could not read the stored client: {exc}")
            return 2

        print(f"Redirect URI used for this sign-in: {redirect_uri_for(port)}")
        print("It must be saved EXACTLY like this in your Kick developer app, or Kick will refuse the sign-in.\n")
        try:
            if args.no_browser:
                tokens = await run_loopback_login(http, client, port=port, opener=lambda _url: True)
            else:
                tokens = await run_loopback_login(http, client, port=port)
        except KickOAuthError as exc:
            print(f"\nSign-in failed: {exc.error}. {HINTS.get(exc.error, exc.description)}")
            if exc.status or exc.description:  # what Kick itself said; it never contains our secret
                print(f"(Kick answered HTTP {exc.status or '?'}: {exc.description or 'no description'})")
            return 1
        auth.save_login(client, tokens)
        if args.secret_file:
            print(f"The secret is now stored encrypted. DELETE the file {args.secret_file} and empty the Recycle Bin (OneDrive may have copied it).")
        granted = set(tokens.scope.split())
        print("\nSigned in. Verifying with Kick...")
        api = KickApi(auth, http)
        try:
            me, channel = await api.me(), await api.channel()
        except KickApiError as exc:
            print(f"Signed in, but the account lookup failed: {exc}")
            return 1
        print(f"Authorised as Kick account: {me['name']}  (user id {me['user_id']}), channel: {channel['slug'] or '?'}")
        if granted:
            missing = sorted(set(SCOPES) - granted)
            print("Permissions granted: " + ", ".join(sorted(granted)))
            if missing:
                print("WARNING: missing permissions: " + ", ".join(missing) + ". Enable them in the Kick app and sign in again.")
        else:
            print("Kick did not list the granted permissions; the check script will show what works.")
        print("\nThat is the account the bot works for. Next steps are in docs\\phase4-kick-setup.md.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

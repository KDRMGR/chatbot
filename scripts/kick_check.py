#!/usr/bin/env python3
"""Check the Kick connection WITHOUT starting the bot. Safe to run any time; stop the bot first if you use --probe-tunnel.

    %LOCALAPPDATA%\\stealthbot\\venv\\Scripts\\python.exe scripts\\kick_check.py
        read-only: sign-in works, which account/channel, is the channel live, Kick's public key can be read, which webhook
        subscriptions exist
    ... --probe-tunnel https://YOUR-PUBLIC-URL
        starts the bot's webhook listener for a few seconds and asks YOUR PUBLIC URL for /kick/health: proves that the tunnel
        reaches this program (it cannot prove that Kick has the URL; the first real chat message does)
    ... --subscribe
        creates the webhook subscriptions the bot needs at Kick (the bot does this itself at start-up too)
    ... --send-test "Hello chat, I'm StealthBot, an automated assistant."
        posts ONE message to the channel's chat as the bot (asks you to type YES first)

Prints chat TEXT and names never; only counts and status.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import httpx  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.platforms.kick.api import KickApi, KickApiError  # noqa: E402
from app.platforms.kick.credentials import KickAuth, NotAuthorized, ReauthRequired  # noqa: E402
from app.platforms.kick.oauth import SCOPES  # noqa: E402
from app.platforms.kick.rsa_verify import KeyError_, parse_public_key  # noqa: E402
from app.platforms.kick.webhook import WebhookServer, WebhookVerifier, build_webhook_app  # noqa: E402
from app.security.credential_store import CredentialStore  # noqa: E402


def line(ok: bool | None, text: str) -> None:
    print(f"  [{'OK' if ok else 'WARN' if ok is None else 'FAIL'}] {text}")


async def probe_tunnel(url: str, port: int, http: httpx.AsyncClient) -> None:
    async def fetch_key() -> str:
        raise RuntimeError("not needed for the health probe")

    server = WebhookServer(build_webhook_app(WebhookVerifier(fetch_key), _ignore), port)
    try:
        await server.start()
    except OSError:
        line(False, f"port {port} is already in use (is the bot running? stop it first, or open {url}/kick/health in a browser)")
        return
    try:
        resp = await http.get(url.rstrip("/") + "/kick/health", timeout=15.0)
        ok = resp.status_code == 200 and "stealthbot kick webhook" in resp.text
        line(ok, f"{url.rstrip('/')}/kick/health answered HTTP {resp.status_code}"
                 + ("" if ok else ": that is not this program; check which port the tunnel forwards to"))
    except httpx.HTTPError as exc:
        line(False, f"could not reach {url}: {exc.__class__.__name__}. Is the tunnel running?")
    finally:
        await server.stop()


async def _ignore(event) -> None:  # noqa: ANN001
    return None


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--probe-tunnel", metavar="URL")
    ap.add_argument("--subscribe", action="store_true")
    ap.add_argument("--send-test", metavar="TEXT")
    args = ap.parse_args()
    settings = get_settings()
    assert settings.data_dir is not None
    failures = 0
    async with httpx.AsyncClient(trust_env=False, timeout=20.0) as http:
        auth = KickAuth(CredentialStore(settings.data_dir), http)
        api = KickApi(auth, http)
        print("Credentials")
        if not auth.is_configured():
            line(False, "not signed in: run scripts\\kick_login.py")
            return 1
        line(True, "encrypted client and tokens are stored")
        try:
            me, channel = await api.me(), await api.channel()
        except (NotAuthorized, ReauthRequired) as exc:
            line(False, f"sign-in is no longer valid ({exc}): run scripts\\kick_login.py again")
            return 1
        except KickApiError as exc:
            line(False, f"Kick answered with an error: {exc}")
            return 1
        line(True, f"signed in as {me['name']} (user id {me['user_id']}), channel {channel['slug'] or '?'}, "
                   f"{'LIVE now' if channel['is_live'] else 'not live'}")
        granted = auth.scopes()
        missing = sorted(set(SCOPES) - granted) if granted else []
        line(None if not granted else not missing,
             "permissions: " + (", ".join(sorted(granted)) if granted else "Kick did not list them")
             + (f"; MISSING {', '.join(missing)}" if missing else ""))
        failures += bool(missing)

        print("Webhook signature key")
        try:
            key = parse_public_key(await api.public_key())
            line(True, f"Kick's public key was read ({key[0].bit_length()}-bit RSA)")
        except (KickApiError, KeyError_) as exc:
            line(False, f"could not read Kick's public key: {exc}")
            failures += 1

        print("Webhook subscriptions")
        try:
            subs = await api.list_subscriptions()
            have = {(s.get("event"), int(s.get("version") or 1)) for s in subs}
            for name in settings.kick_events:
                line(True if (name, 1) in have else None, f"{name}: {'subscribed' if (name, 1) in have else 'not subscribed yet (the bot subscribes at start)'}")
            if args.subscribe:
                missing_events = [(n, 1) for n in settings.kick_events if (n, 1) not in have]
                if missing_events:
                    results = await api.subscribe(missing_events)
                    for r in results:
                        line(not r.get("error"), f"subscribe {r.get('name')}: {r.get('error') or 'done'}")
                else:
                    line(True, "nothing to do: all wanted events are already subscribed")
        except KickApiError as exc:
            line(False, f"could not list/create subscriptions: {exc}")
            failures += 1

        print(f"Webhook listener (port {settings.kick_webhook_port}, public URL: {settings.kick_webhook_public_url or 'not set'})")
        if args.probe_tunnel:
            await probe_tunnel(args.probe_tunnel, settings.kick_webhook_port, http)
        else:
            line(None, "tunnel not checked: add --probe-tunnel https://YOUR-PUBLIC-URL (bot stopped)")

        if args.send_test:
            print(f"About to post to the channel's chat as the bot ({settings.kick_message_type}):\n  {args.send_test}")
            if input("Type YES to send it: ").strip() != "YES":
                print("Not sent.")
            else:
                try:
                    result = await api.send_chat(args.send_test, kind=settings.kick_message_type,
                                                 broadcaster_user_id=channel["broadcaster_user_id"])
                    line(result["is_sent"], f"message {'sent' if result['is_sent'] else 'NOT sent'} (id {result['message_id']})")
                    print("  Look in the chat: under which NAME did it appear? That is the bot's identity on Kick.")
                except KickApiError as exc:
                    line(False, f"sending failed: {exc}")
                    failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

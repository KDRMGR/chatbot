#!/usr/bin/env python3
"""Read-only YouTube connection check (costs a few quota units). Run it yourself while a stream is live.

    %LOCALAPPDATA%\\stealthbot\\venv\\Scripts\\python.exe scripts\\youtube_check.py --video-id <id of the live stream>

It verifies, in order: stored credentials, access token, which channel the bot is, finding the live chat, and reading one
page of chat (prints counts only, never message text). Optional extras:

  --probe-stream      connect to Google's HTTP stream endpoint for ~20 s and print its response FRAMING (message text and
                      names are redacted). Google does not document this framing for plain HTTP: the output tells us
                      whether the experimental streaming transport can be used.
  --send-test "text"  post ONE message to the chat as the bot (20 quota units). Asks you to type YES first.
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import httpx  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.platforms.youtube.api import BASE_URL, CHAT_PART, YouTubeApi, YouTubeApiError  # noqa: E402
from app.platforms.youtube.credentials import NotAuthorized, ReauthRequired, YouTubeAuth  # noqa: E402
from app.platforms.youtube.oauth import OAuthError  # noqa: E402
from app.platforms.youtube.quota import QuotaTracker  # noqa: E402
from app.security.credential_store import CredentialError, CredentialStore  # noqa: E402

_STRING_VALUE = re.compile(r'(:\s*)"(?:[^"\\]|\\.)*"')


def ok(msg: str) -> None:
    print(f"[ OK ] {msg}")


def fail(msg: str) -> int:
    print(f"[FAIL] {msg}")
    return 1


def redact(text: str) -> str:
    """Keep keys and structure, replace every string VALUE (chat text, names, ids)."""
    return _STRING_VALUE.sub(r'\1"..."', text)


async def probe_stream(auth: YouTubeAuth, chat_id: str, seconds: float = 20.0) -> None:
    token = await auth.access_token()
    print(f"\nProbing the stream endpoint for up to {seconds:.0f}s (costs ~5 quota units)...")
    received = b""
    async with httpx.AsyncClient(base_url=BASE_URL, trust_env=False, timeout=httpx.Timeout(10.0, read=seconds)) as http:
        try:
            async with http.stream("GET", "/liveChat/messages/stream",
                                   params={"liveChatId": chat_id, "part": CHAT_PART},
                                   headers={"Authorization": f"Bearer {token}"}) as resp:
                print(f"HTTP {resp.status_code}; content-type={resp.headers.get('content-type')}; "
                      f"transfer-encoding={resp.headers.get('transfer-encoding')}; http_version={resp.http_version}")
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode("utf-8", "replace")
                    print("error body (redacted):", redact(body)[:600])
                    return
                started = time.monotonic()
                async for chunk in resp.aiter_bytes():
                    received += chunk
                    if len(received) >= 4096 or time.monotonic() - started > seconds:
                        break
        except httpx.ReadTimeout:
            print("(no data within the probe window)")
        except httpx.HTTPError as exc:
            print(f"transport error: {exc.__class__.__name__}")
    text = received.decode("utf-8", "replace")
    print(f"received {len(received)} bytes; first character: {text[:1]!r}")
    print("framing sample (values redacted):")
    print(redact(text)[:700] or "(empty)")
    print("\nInterpretation hints: starts with '[' => a streamed JSON array; starts with '{' => concatenated or "
          "newline-delimited JSON. Send this output back so the experimental transport can be confirmed or dropped.")


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--video-id", help="video id of the live stream (the 11 characters after v= in its URL)")
    ap.add_argument("--probe-stream", action="store_true")
    ap.add_argument("--send-test", metavar="TEXT")
    args = ap.parse_args()

    settings = get_settings()
    assert settings.data_dir is not None
    async with httpx.AsyncClient(trust_env=False, timeout=20.0) as http:
        auth = YouTubeAuth(CredentialStore(settings.data_dir), http)
        quota = QuotaTracker(settings.youtube_daily_quota)
        api = YouTubeApi(auth, quota)
        try:
            try:
                if not auth.is_configured():
                    return fail("no stored credentials: run scripts\\youtube_login.py first")
                ok("credentials found and decryptable")
                await auth.access_token()
                ok("access token is valid (refreshed if needed)")
                channel = await api.own_channel()
                ok(f"the bot is YouTube channel '{channel['title']}' ({channel['id']})")

                video_id = args.video_id or settings.youtube_video_id
                chat_id = settings.youtube_live_chat_id
                if not chat_id and video_id:
                    chat_id = await api.video_chat_id(video_id)
                if not chat_id and not video_id:
                    broadcast = await api.active_broadcast()
                    if broadcast:
                        chat_id, video_id = broadcast.live_chat_id, broadcast.video_id
                if not chat_id:
                    return fail("no live chat found: is the stream live, with chat enabled? Pass --video-id <id>")
                ok(f"live chat found (video {video_id or '?'})")

                page = await api.list_messages(chat_id)
                ok(f"read one page of chat: {len(page.items)} message(s); YouTube asks for polling every "
                   f"{page.polling_interval_s:.1f}s; stream offline marker: {bool(page.offline_at)}")

                if args.send_test:
                    if input(f'Post "{args.send_test}" to the live chat as {channel["title"]}? Type YES: ').strip() != "YES":
                        print("skipped sending")
                    else:
                        message_id = await api.insert_message(chat_id, args.send_test)
                        ok(f"message posted (id {message_id[:12]}...)")
                if args.probe_stream:
                    await probe_stream(auth, chat_id)
            except (NotAuthorized, ReauthRequired) as exc:
                return fail(f"not authorised ({exc}). Run scripts\\youtube_login.py again")
            except (YouTubeApiError, OAuthError, CredentialError) as exc:
                return fail(str(exc))
        finally:
            await api.aclose()
        snap = quota.snapshot()
        print(f"\nThis check used about {snap['used']} quota units (daily limit {snap['limit']}).")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

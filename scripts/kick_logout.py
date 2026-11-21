#!/usr/bin/env python3
"""Disconnect STEALTHBOT from Kick and delete what it stored.

    %LOCALAPPDATA%\\stealthbot\\venv\\Scripts\\python.exe scripts\\kick_logout.py

1. revokes the OAuth grant at Kick, 2. deletes the encrypted client + tokens, 3. deletes every stored record that came from Kick
(viewers, chat messages, bot replies). Stop the bot first (the scripts\\start.bat window) so nothing writes to the database meanwhile.
Also remove the webhook subscription / disable webhooks in your Kick developer app if you will not use the bot any more.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import httpx  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.database.database import make_engine, make_session_factory  # noqa: E402
from app.database.repository import Repository  # noqa: E402
from app.platforms.kick.credentials import KickAuth  # noqa: E402
from app.security.credential_store import CredentialStore  # noqa: E402


async def main() -> int:
    settings = get_settings()
    assert settings.data_dir is not None
    async with httpx.AsyncClient(trust_env=False, timeout=20.0) as http:
        revoked = await KickAuth(CredentialStore(settings.data_dir), http).revoke_and_forget()
    print(f"Kick grant revoked: {'yes' if revoked else 'no / nothing to revoke (also check your Kick account settings)'}")
    print("Stored encrypted credentials deleted.")
    if settings.database_path.exists():
        engine = make_engine(settings.database_path)
        counts = await Repository(make_session_factory(engine)).purge_platform_data("kick")
        engine.dispose()
        print(f"Deleted stored Kick data: {counts}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

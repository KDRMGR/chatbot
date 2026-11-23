#!/usr/bin/env python3
"""Store OBS Studio's WebSocket password so the bot's eyes can look through OBS. Run this yourself, only if you set a password in OBS.

In OBS: Tools > WebSocket Server Settings > tick "Enable WebSocket server" (port 4455). A password is optional: when it is switched off
OBS needs no password and you do not need this script at all (the server only listens on this PC).

    %LOCALAPPDATA%\\stealthbot\\venv\\Scripts\\python.exe scripts\\obs_login.py
    (the password is asked for with hidden input; it never goes on the command line)
    %LOCALAPPDATA%\\stealthbot\\venv\\Scripts\\python.exe scripts\\obs_login.py --password-file C:\\path\\to\\file.txt   (first line = password)

It is stored encrypted with Windows DPAPI in %LOCALAPPDATA%\\stealthbot\\data (never in the repository, .env, the database or the logs).
--check takes one picture through OBS to prove it works (OBS must be running); --forget deletes the stored password.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.config import get_settings  # noqa: E402
from app.security.credential_store import CredentialStore  # noqa: E402
from app.vision.obs import CREDENTIAL_KEY, ObsClient, ObsError  # noqa: E402


async def check(host: str, port: int, password: str | None) -> int:
    try:
        image = await ObsClient(host, port, lambda: password).screenshot(640)
    except ObsError as exc:
        print(f"OBS check failed: {exc}")
        return 1
    print(f"OBS answered and gave a picture of {len(image) // 1024} KB. The bot's eyes can use it (choose OBS in the panel).")
    return 0


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--password-file", help="a text file whose first line is the password")
    ap.add_argument("--forget", action="store_true", help="delete the stored password")
    ap.add_argument("--check", action="store_true", help="take one picture through OBS to prove it works")
    args = ap.parse_args()

    settings = get_settings()
    settings.ensure_dirs()
    assert settings.data_dir is not None
    store = CredentialStore(settings.data_dir)
    if args.forget:
        print("The stored OBS password was deleted." if store.delete(CREDENTIAL_KEY) else "There was no stored OBS password.")
        return 0
    if args.check and not args.password_file:
        stored = (store.load(CREDENTIAL_KEY) or {}).get("password")
        return await check(settings.obs_host, settings.obs_port, stored)

    if args.password_file:
        lines = Path(args.password_file).read_text(encoding="utf-8").splitlines()
        password = lines[0].strip() if lines else ""
    else:
        password = getpass.getpass("OBS WebSocket password (input is hidden): ").strip()
    if not password:
        print("No password given: nothing was stored.")
        return 1
    store.save(CREDENTIAL_KEY, {"password": password})
    print("The OBS password was stored (encrypted for this Windows user).")
    return await check(settings.obs_host, settings.obs_port, password) if args.check else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
